"""码库加载器：读取 `library/` 下打包好的 irext 码库。

    index.json.gz            元数据 + 每个键的 (offset, count)
    data/<category>.bin.gz   时序数据，varint(zigzag) 无损编码

按类别分块加载：只有真正用到某个类别时才解压那一块（机顶盒单块原始 16 MB），
不会把全库 20 MB 常驻内存。

符号约定（关键）：irext 原始数据是**全正**的 mark/space 长度，不含符号。
而 ESPHome / HA 要求"正 = pulse、负 = space"，所以符号在**读取时**补，
见 `sign_timings()`。这样打包数据一个字节都不用改。
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
    """从 `offset` 起解码 `count` 个 zigzag-varint。

    与 tools/pack_irext.py 的编码严格互逆（自检做过全库逐字节往返验证）。
    返回的是**无符号**长度值（µs），符号由 `sign_timings()` 补。
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

    ⚠️ 这一步不能省：ESPHome `transmit_raw` 与 HA `get_raw_timings()` 都要求
    正负交替，传全正数组会被 `RawTimingsCommand` 以 ValueError 拒收，一次都发不出去。

    帧长为奇数很正常（NEC 标准帧就是"引导码 + 32×2 位 + stop_mark"，以 mark 收尾）。
    """
    return [value if index % 2 == 0 else -value for index, value in enumerate(values)]


class CodeLibrary:
    """码库的只读视图，进程内共享一份。"""

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
        """品牌 + 型号的展示名；型号已自带品牌前缀时不重复。

        ⚠️ 这不是美化，是必须的：irext 的型号名常常已经带品牌（"TCL电视-1"），
        无脑拼 `f"{brand} {name}"` 会得到 "TCL TCL电视-1"，
        而 HA 的 entity_id 取自设备名 ⇒ 实体变成 `remote.tcl_tcl电视_1`。
        """
        brand_name = (brand_name or "").strip()
        model_name = (model_name or "").strip()
        if not brand_name:
            return model_name
        if model_name.lower().startswith(brand_name.lower()):
            return model_name
        return f"{brand_name} {model_name}"

    def categories_available(self) -> list[tuple[int, str, int]]:
        """有设备的类别：(id, 名称, 设备数)。按设备数从多到少排。"""
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
        """该类别下的品牌：(id, 名称, 型号数)。按型号数从多到少排。"""
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
        """某类别下某品牌的全部型号，按名称排序。"""
        return sorted(
            (
                d
                for d in self.devices
                if d["category"] == category and d["brand"] == brand
            ),
            key=lambda d: d["name"],
        )

    def get_device(self, device_id: int) -> dict | None:
        """按 id 取设备条目。"""
        return self._by_id.get(device_id)

    @staticmethod
    def key_names(device: dict) -> list[str]:
        """该设备可用的键名，power 等常用键排在前面。

        只列**可用**的键：count < 2 的在 irext 里是占位死数据（码只有单个 0），
        列出来只会让人按了没反应。打包时已剔除，这里再用索引里的 count 挡一道
        （不解压数据块）。
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
        """返回某键的**带符号**裸时序；不可用时返回 None。

        正 = mark、负 = space，单位 µs，可直接喂给 `RawTimingsCommand`。

        返回 None 的两种情况：
          ① 码库里没有这个键；
          ② 有，但 count < 2（占位死数据）—— 这种数组没有 space，
             交给 emitter 会被拒收，所以在这里就挡掉，不让它变成运行时异常。
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
        """取（并缓存）某类别的时序数据块。"""
        cached = self._chunk_cache.get(category)
        if cached is not None:
            return cached

        rel = CATEGORY_DATA_FILE % category
        full = os.path.join(self._path, rel)
        with gzip.open(full, "rb") as handle:
            data = handle.read()

        # 只缓存最近一个块：同时用两个类别的场景很少，单块缓存足够避免反复解压，
        # 又不会把 20 MB 全塞进内存。
        self._chunk_cache.clear()
        self._chunk_cache[category] = data
        return data

    def release(self) -> None:
        """释放缓存的数据块。"""
        self._chunk_cache.clear()
