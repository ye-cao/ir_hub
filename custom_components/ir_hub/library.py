"""码库加载器。

读取 `library/` 下打包好的 irext 码库：
    index.json.gz        元数据 + 每个键的 (offset, count)
    data/<category>.bin.gz   时序数据，varint(zigzag) 无损编码

分块加载：只有真正用到某个类别时才解压那一个块（机顶盒原始 16 MB），
不会把全库 20 MB 常驻内存。

⚠️ 存储格式的一个关键事实（自检实测，全库 12,582,610 个时序值）：
    irext 的 `collect_key.key_value` 是**全正**的 mark/space 交替长度，
    **不含符号**（实测负值数量 = 0）。而 ESPHome / HA 的红外体系用的是
    "正 = pulse（载波开）、负 = space（载波空闲）"。
    所以符号必须在**读取时**补，见 `sign_timings()`。
    这么做的好处：3.9 MB 打包数据一个字节都不用改。
"""

from __future__ import annotations

import gzip
import json
import logging
import os

from .const import CATEGORY_DATA_FILE

_LOGGER = logging.getLogger(__name__)

LIBRARY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "library")
SUPPORTED_FORMAT = 1

# 配置文件里用得到的键名（可选，缺了也能跑）
CONF_KEY_ORDER = "key_order"


def decode_varints(data: bytes, offset: int, count: int) -> list[int]:
    """Decode `count` zigzag-varints starting at `offset`.

    与 tools/pack_irext.py 的 zigzag()/put_varint() 严格互逆
    （自检里用打包器自己的编码器做了全库逐字节往返验证）。
    返回的是**无符号**长度值（µs）—— 符号由 sign_timings() 补。
    """
    out: list[int] = []
    i = offset
    for _ in range(count):
        shift = 0
        value = 0
        while True:
            byte = data[i]
            i += 1
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
            shift += 7
        out.append(-((value >> 1) + 1) if value & 1 else value >> 1)
    return out


def sign_timings(values: list[int]) -> list[int]:
    """无符号长度数组 -> 带符号时序数组。

    偶数下标是 mark（载波开，正），奇数下标是 space（载波空闲，负）。

    ⚠️ 这一步**不能省**：ESPHome 的 `remote_transmitter.transmit_raw` 与 HA 的
    infrared `Command.get_raw_timings()` 都要求"正负交替"；传全正数组会被
    `RawTimingsCommand` 直接以 "raw timings must alternate pulse/space" 拒绝，
    也就是**一次都发不出去**（这个坑由 tools/selfcheck.py 抓出）。

    帧长奇数很正常（NEC 标准帧就是 "引导码 + 32×2 位 + stop_mark"，以 mark 收尾）。
    """
    return [value if index % 2 == 0 else -value for index, value in enumerate(values)]


class CodeLibrary:
    """Read-only view over the packaged irext code library."""

    def __init__(self, path: str = LIBRARY_DIR) -> None:
        self._path = path
        index_path = os.path.join(path, "index.json.gz")
        with gzip.open(index_path, "rt", encoding="utf-8") as handle:
            index = json.load(handle)

        fmt = index.get("format")
        if fmt != SUPPORTED_FORMAT:
            raise ValueError(
                f"unsupported library format {fmt!r}, expected {SUPPORTED_FORMAT}"
            )

        self.generated: str = index.get("generated", "")
        self.categories: dict[int, str] = {
            int(k): v for k, v in index.get("categories", {}).items()
        }
        self.brands: dict[int, str] = {
            int(k): v for k, v in index.get("brands", {}).items()
        }
        self.devices: list[dict] = index["devices"]
        self._by_id: dict[int, dict] = {d["id"]: d for d in self.devices}
        self._chunk_cache: dict[int, bytes] = {}

        _LOGGER.debug(
            "IR Hub library loaded: %d devices / %d categories (%s)",
            len(self.devices),
            len(self.categories),
            self.generated,
        )

    # ------------------------------------------------------------------ 查询

    @staticmethod
    def display_name(brand_name: str, model_name: str) -> str:
        """品牌 + 型号的展示名；型号**已自带品牌前缀**时不重复。

        ⚠️ 这条不是美化，是必须的：irext 的 `collect_remote.name` 常常已经是
        「TCL电视-1」「格力空调-2」这种（自带品牌）。无脑 `f"{brand} {name}"`
        会得到「TCL TCL电视-1」，而 HA 的 entity_id = slugify(设备名) ⇒
        实体变成 `remote.tcl_tcl电视_1`（品牌重复一遍），用户每次调服务都要
        看着这个别扭 id。
        """
        brand_name = (brand_name or "").strip()
        model_name = (model_name or "").strip()
        if not brand_name:
            return model_name
        if model_name.lower().startswith(brand_name.lower()):
            return model_name
        return f"{brand_name} {model_name}"

    def categories_available(self) -> list[tuple[int, str, int]]:
        """Categories that actually contain devices: (id, name, device_count).

        Sorted biggest-first — the config flow shows them as a dropdown and the
        big categories (机顶盒 / 电视机) are what people almost always want.
        """
        counts: dict[int, int] = {}
        for device in self.devices:
            counts[device["category"]] = counts.get(device["category"], 0) + 1
        return sorted(
            (
                (cid, self.categories.get(cid) or f"类别 {cid}", count)
                for cid, count in counts.items()
            ),
            key=lambda row: (-row[2], row[1]),
        )

    def brands_in(self, category: int) -> list[tuple[int, str, int]]:
        """Brands in `category`: (id, name, device_count), most-populated first.

        按"型号数"排序比按拼音更实用 —— 下拉框里常有 100+ 项。
        """
        seen: dict[int, int] = {}
        for device in self.devices:
            if device["category"] == category:
                seen[device["brand"]] = seen.get(device["brand"], 0) + 1
        return sorted(
            (
                (bid, self.brands.get(bid) or f"品牌 {bid}", count)
                for bid, count in seen.items()
            ),
            key=lambda row: (-row[2], row[1]),
        )

    def devices_in(self, category: int, brand: int) -> list[dict]:
        """Devices of one brand in one category, sorted by name."""
        return sorted(
            (
                d
                for d in self.devices
                if d["category"] == category and d["brand"] == brand
            ),
            key=lambda d: d["name"],
        )

    def get_device(self, device_id: int) -> dict | None:
        return self._by_id.get(device_id)

    @staticmethod
    def key_names(device: dict) -> list[str]:
        """Sorted key names — power first, then the usual remote order.

        只列**可用**的键：码长度 < 2 的在 irext 里是占位死数据（键名进了
        key_mapping，但码就是单个 0，实测 28,022 个），列出来只会让人按了
        没反应。打包时已剔除，这里再挡一道（判据只用索引里的 count，
        不用解压数据块）。
        """
        keys = [
            name
            for name, entry in device.get("keys", {}).items()
            if int(entry[1]) >= 2
        ]
        priority = [
            "power", "mute", "vol+", "vol-", "up", "down", "left", "right",
            "ok", "back", "home", "menu", "input", "set",
        ]
        rank = {name: i for i, name in enumerate(priority)}
        return sorted(keys, key=lambda k: (rank.get(k, len(priority)), k))

    def get_timings(self, device_id: int, key: str) -> list[int] | None:
        """Return the **signed** raw timings for `key`, or None if unusable.

        正 = mark，负 = space，单位 µs —— 可直接喂给
        `ir_command.RawTimingsCommand` / ESPHome `transmit_raw`。

        返回 None 的两种情况：
          ① 码库里没有这个键；
          ② 有，但长度 < 2（占位死数据）。这种数组没有 space，
             交给 emitter 会被 `RawTimingsCommand` 以 ValueError 拒收，
             所以在这里就挡掉，而不是让它变成运行时异常。
        """
        device = self._by_id.get(device_id)
        if device is None:
            return None
        entry = device.get("keys", {}).get(key)
        if entry is None:
            return None
        offset, count = entry
        if int(count) < 2:
            _LOGGER.debug(
                "IR Hub: device %s key %r is placeholder data (count=%s), skipped",
                device_id,
                key,
                count,
            )
            return None
        return sign_timings(decode_varints(self._chunk(device["chunk"]), offset, count))

    # ---------------------------------------------------------------- 数据块

    def _chunk(self, category: int) -> bytes:
        cached = self._chunk_cache.get(category)
        if cached is not None:
            return cached

        rel = CATEGORY_DATA_FILE % category
        full = os.path.join(self._path, rel)
        with gzip.open(full, "rb") as handle:
            data = handle.read()

        # 只缓存一个块：码库是按类别分块的，同时用两个类别的场景很少，
        # 缓存单块足以避免反复解压，又不会把 20 MB 全塞进内存。
        self._chunk_cache.clear()
        self._chunk_cache[category] = data
        return data

    def release(self) -> None:
        self._chunk_cache.clear()
