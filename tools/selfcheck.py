#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IR Hub 自检（不依赖 Home Assistant，可在开发机直接跑）。

    PY="C:/Users/ye_ca/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
    $PY tools/selfcheck.py

检查项：
  [1] 所有 .py 能被 ast 解析（本机没装 HA，做不到完整 import）
  [2] manifest.json / hacs.json / translations/*.json 合法，且翻译的
      step / abort / data 键与 config_flow.py 实际用到的**一一对应**
  [3] 码库可加载：format、chunk 文件齐全、统计
  [4] 全库不变式 + ⭐ 逐字节往返
      · 对全库每一个键，用 library.decode_varints() 解出长度值，
        再用打包器 pack_irext.py **自己的** zigzag()/put_varint() 重编，
        必须与 data/<cat>.bin.gz 原始字节逐字节相等
        —— "打包器 ↔ 读取器"编码一致性的硬证据，不是抽样
      · 存储必须**全无符号**（负值 0 个）
      · 补符号后每个键必须含 space 且严格 mark/space 交替
        —— 否则 RawTimingsCommand 会拒收，一次都发不出去
  [5] 统计（各类别设备/键数）
  [6] key_names() 排序
  [7] ir_command 单元测试（用 stub 顶掉 infrared_protocols）
      parse_timings / build_raw_command / RawTimingsCommand 的拒收分支
"""

from __future__ import annotations

import abc
import ast
import enum
import gzip
import glob
import importlib.util
import json
import os
import sys
import types

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                     # ir-hub-integration/
COMPONENT = os.path.join(ROOT, "custom_components", "ir_hub")
PACKER = os.path.join(os.path.dirname(ROOT), "ir-remote", "tools", "pack_irext.py")
# **用户已实测能开关电视**的那份码（HA 侧 script）。用它把"本集成会发出什么"
# 钉死在"已验证可用的数据"上 —— 剩下的差异就只剩传输路径了。
PROVEN_CODE_YAML = os.path.join(os.path.dirname(ROOT), "ir-remote", "ha", "tcl-tv.yaml")

# ESPHome 的 base64url 路径把单段时长上限设在 500 ms；packed 路径虽然没有该校验，
# 但超出这个量级基本说明数据坏了，值得当红线盯着。
MAX_SANE_TIMING_US = 500_000

FAILURES: list[str] = []
CHECKS = 0
SKIPPED: list[str] = []


def _first_int_array(node) -> list[int] | None:
    """在任意嵌套的 YAML 结构里找出第一个"像时序数组"的整数列表。"""
    if isinstance(node, list):
        if len(node) > 10 and all(isinstance(x, int) for x in node):
            return node
        for item in node:
            found = _first_int_array(item)
            if found is not None:
                return found
    elif isinstance(node, dict):
        for value in node.values():
            found = _first_int_array(value)
            if found is not None:
                return found
    return None


def check(condition: bool, label: str, detail: str = "") -> bool:
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"  [ OK ] {label}")
        return True
    print(f"  [FAIL] {label}" + (f"  <- {detail}" if detail else ""))
    FAILURES.append(label)
    return False


def skip(label: str, why: str) -> None:
    print(f"  [SKIP] {label}\n         {why}")
    SKIPPED.append(label)


def load_module(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def make_shim_package() -> None:
    """library.py / ir_command.py 用相对 import，这里造一个包外壳。

    真正的 `custom_components.ir_hub.__init__` 会 import homeassistant，
    本机没有 HA，所以只把需要的子模块按 shim 包名挂进 sys.modules。
    """
    shim = types.ModuleType("_ir_hub_shim")
    shim.__path__ = [COMPONENT]
    sys.modules["_ir_hub_shim"] = shim


def _step_ids(text: str) -> list[str]:
    """抓出 `step_id="xxx"` 里的 step 名。"""
    return [chunk.split('"', 1)[0] for chunk in text.split('step_id="')[1:]]


# --------------------------------------------------------------------- 1. 语法
def check_syntax() -> None:
    print("\n[1] 语法解析 (ast)")
    py_files = []
    for dirpath, _dirnames, filenames in os.walk(COMPONENT):
        for fn in filenames:
            if fn.endswith(".py"):
                py_files.append(os.path.join(dirpath, fn))
    py_files.append(os.path.abspath(__file__))
    bad = []
    for path in py_files:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                ast.parse(fh.read(), filename=path)
        except SyntaxError as err:
            bad.append(f"{os.path.relpath(path, ROOT)}: {err}")
    check(not bad, f"{len(py_files)} 个 .py 文件可解析", "; ".join(bad))


# ------------------------------------------------------------------- 2. 清单
def check_manifests() -> None:
    print("\n[2] 清单与翻译文件")
    manifest = json.load(open(os.path.join(COMPONENT, "manifest.json"), encoding="utf-8"))
    hacs = json.load(open(os.path.join(ROOT, "hacs.json"), encoding="utf-8"))

    for key in ("domain", "name", "version", "config_flow", "dependencies"):
        check(key in manifest, f"manifest.json 有 '{key}'")
    check(manifest["config_flow"] is True, "manifest.config_flow == true")
    check("infrared" in manifest["dependencies"], "manifest 依赖 infrared 域")
    check(manifest["domain"] == "ir_hub", "manifest.domain == ir_hub")
    check(
        bool(hacs.get("homeassistant")),
        f"hacs.json 声明最低 HA 版本 = {hacs.get('homeassistant')}",
    )

    tdir = os.path.join(COMPONENT, "translations")
    langs = sorted(f for f in os.listdir(tdir) if f.endswith(".json"))
    check(bool(langs), f"translations/ 下有语言文件: {langs}")

    # 与 config_flow.py 实际用到的 step / abort reason 对齐。
    # step_id="x" 同时出现在 ConfigFlow 与 OptionsFlow 里，翻译文件里分别落在
    # config.step / options.step 下，所以按 "class IrHubOptionsFlow" 切分。
    src = open(os.path.join(COMPONENT, "config_flow.py"), encoding="utf-8").read()
    head, _, tail = src.partition("class IrHubOptionsFlow")
    config_steps = set(_step_ids(head))
    option_steps = set(_step_ids(tail)) or {"init"}
    reasons = {
        token.split('"', 1)[0]
        for token in src.split('async_abort(reason="')[1:]
    }

    check(bool(config_steps), f"config_flow 里解析出 config step {sorted(config_steps)}")
    check(bool(reasons), f"config_flow 里解析出 abort reason {sorted(reasons)}")

    for lang in langs:
        blob = json.load(open(os.path.join(tdir, lang), encoding="utf-8"))
        cfg = blob.get("config", {})
        have_steps = set(cfg.get("step", {}))
        have_aborts = set(cfg.get("abort", {}))
        check(
            config_steps <= have_steps,
            f"{lang}: 覆盖所有 config step {sorted(config_steps)}",
            f"缺 {sorted(config_steps - have_steps)}",
        )
        have_opt = set(blob.get("options", {}).get("step", {}))
        check(
            option_steps <= have_opt,
            f"{lang}: 覆盖所有 options step {sorted(option_steps)}",
            f"缺 {sorted(option_steps - have_opt)}",
        )
        check(
            reasons <= have_aborts,
            f"{lang}: 覆盖所有 abort reason {sorted(reasons)}",
            f"缺 {sorted(reasons - have_aborts)}",
        )
        # data 键必须与 schema 里的 CONF_* 名一致（拼错会让表单显示原始键名）
        check(
            set(cfg.get("step", {}).get("user", {}).get("data", {}))
            == {"emitter", "category"},
            f"{lang}: user 步 data 键 = emitter/category",
        )
        check(
            set(
                blob.get("options", {}).get("step", {}).get("init", {}).get("data", {})
            )
            == {"carrier", "repeats"},
            f"{lang}: options 步 data 键 = carrier/repeats",
        )


# ------------------------------------------------------------------ 3./4./5.
def check_library() -> dict:
    print("\n[3] 码库加载")
    library_mod = load_module(
        "_ir_hub_shim.library", os.path.join(COMPONENT, "library.py")
    )
    library = library_mod.CodeLibrary()

    check(len(library.devices) > 1000, f"设备数 = {len(library.devices)}")
    check(bool(library.categories), f"类别数 = {len(library.categories)}")
    check(bool(library.brands), f"品牌数 = {len(library.brands)}")
    check(bool(library.generated), f"generated = {library.generated}")

    missing = [
        cat
        for cat in {d["chunk"] for d in library.devices}
        if not os.path.exists(
            os.path.join(COMPONENT, "library", library_mod.CATEGORY_DATA_FILE % cat)
        )
    ]
    check(not missing, "所有 chunk 文件存在", f"缺 {missing}")
    check(
        set(library.categories) >= {d["category"] for d in library.devices},
        "每个用到的类别都有名字（无 '类别 N' 兜底）",
    )

    print("\n[4] 全库不变式 + 逐字节往返（每一个键，不抽样）")
    # 打包器只在开发树里（且依赖 108 MB 的 irext sqlite 源库），本集成独立发布时
    # 不会带它 ⇒ 找不到就跳过这一项，其余不变量照查（那些只看集成自己的数据）。
    packer = None
    if os.path.exists(PACKER):
        packer = load_module("_pack_irext", PACKER)
    else:
        skip(
            "逐字节往返（需要打包器）",
            f"找不到 {os.path.relpath(PACKER, ROOT)}"
            "（独立发布本集成时属正常），其余不变量仍然全查",
        )

    total_keys = total_values = 0
    roundtrip_bad: list[str] = []
    unsigned_bad: list[str] = []
    no_space: list[str] = []
    alternation_bad: list[str] = []
    empty_arrays: list[str] = []
    max_timing = 0
    chunk_bytes: dict[int, bytes] = {}

    for device in library.devices:
        cat = device["chunk"]
        chunk = chunk_bytes.get(cat)
        if chunk is None:
            chunk = chunk_bytes[cat] = library._chunk(cat)

        for key, (offset, count) in device["keys"].items():
            total_keys += 1
            tag = f"dev={device['id']}/{key}"

            raw = library_mod.decode_varints(chunk, offset, count)
            total_values += len(raw)
            if raw:
                biggest = max(abs(v) for v in raw)
                if biggest > max_timing:
                    max_timing = biggest
            else:
                empty_arrays.append(tag)
                continue

            # ① 存储不变量：全无符号（符号在读取时由 sign_timings 补）
            if any(v < 0 for v in raw):
                unsigned_bad.append(tag)

            # ② 逐字节往返：用打包器自己的编码器重编
            if packer is not None:
                buf = bytearray()
                for value in raw:
                    packer.put_varint(buf, packer.zigzag(value))
                if bytes(buf) != chunk[offset : offset + len(buf)]:
                    roundtrip_bad.append(tag)

            # ③ 补符号后必须能被 emitter 接受
            signed = library_mod.sign_timings(raw)
            if not any(v < 0 for v in signed):
                no_space.append(tag)
            for index, value in enumerate(signed):
                if (index % 2 == 0 and value < 0) or (index % 2 == 1 and value > 0):
                    alternation_bad.append(
                        f"{tag} idx={index} v={value} len={len(signed)}"
                    )
                    break

    if packer is not None:
        check(
            not roundtrip_bad,
            f"全部 {total_keys} 个键往返逐字节一致（{total_values} 个时序值）",
            "; ".join(roundtrip_bad[:5]),
        )
    check(not unsigned_bad, "存储全为无符号长度（负值 0 个）", "; ".join(unsigned_bad[:5]))
    check(
        not no_space,
        "补符号后每个键都含 space（RawTimingsCommand 不会拒收）",
        "; ".join(no_space[:5]),
    )
    check(
        not alternation_bad,
        "补符号后严格 mark/space 交替",
        "; ".join(alternation_bad[:5]),
    )
    check(not empty_arrays, "没有空时序数组", "; ".join(empty_arrays[:5]))
    check(
        max_timing <= MAX_SANE_TIMING_US,
        f"最大单段时长 {max_timing} µs ≤ {MAX_SANE_TIMING_US} µs",
    )

    # 回归锚点 1：本机 TCL 电视（device 47），先钉住长度与开头
    anchor = library.get_timings(47, "power")
    check(
        anchor is not None and len(anchor) == 156 and anchor[0] == 3963 and anchor[1] == -3985,
        "回归锚点 device 47 'power' = 156 项 / 首值 3963 / 次值 -3985",
        f"实际 {len(anchor) if anchor else None} 项，前 2 值 {(anchor[:2] if anchor else None)}",
    )

    # 回归锚点 2（更强）：与**用户已实测能开关电视**的那份码逐项比对。
    # 这一条把"本集成会发出什么"钉死在"已验证对真机有效的字节"上 ——
    # 这样部署后若电视不响应，就可以直接排除"码取错了"，只剩传输路径一个变量。
    if not os.path.exists(PROVEN_CODE_YAML):
        skip(
            "与已实测可用的码逐项比对",
            f"找不到 {os.path.relpath(PROVEN_CODE_YAML, ROOT)}（该文件只在开发树里）",
        )
    else:
        with open(PROVEN_CODE_YAML, encoding="utf-8") as handle:
            proven = _first_int_array(yaml.safe_load(handle))
        same = proven is not None and anchor == proven
        diffs = sum(1 for a, b in zip(proven or [], anchor or []) if a != b)
        check(
            same,
            f"device 47 'power' 与已实测可用的码**逐项完全相同**"
            f"（{len(proven) if proven else 0} 项）",
            f"差异 {diffs} 处（HA script {len(proven or [])} 项 vs 集成 {len(anchor or [])} 项）",
        )

    print("\n[5] 统计")
    for cat, name, count in library.categories_available():
        keys = sum(len(d["keys"]) for d in library.devices if d["category"] == cat)
        raw_mb = len(chunk_bytes.get(cat, b"")) / 1048576
        print(
            f"       cat {cat:<3} {name:<8} {count:>5} 设备 {keys:>6} 键  原始 {raw_mb:5.2f} MB"
        )
    print(
        f"       合计 {len(library.devices)} 设备 / {total_keys} 键 / {total_values} 时序值"
    )

    print("\n[6] key_names() 排序")
    sample = next((d for d in library.devices if "power" in d.get("keys", {})), None)
    if sample:
        names = library_mod.CodeLibrary.key_names(sample)
        check(names[0] == "power", f"power 排第一（样本 device {sample['id']}）", f"实际 {names[:5]}")
        check(
            len(names) == len(sample["keys"]),
            f"key_names 不丢键（{len(names)}/{len(sample['keys'])}）",
        )
    return chunk_bytes


# ------------------------------------------------------------------- 7. 单测
def check_ir_command() -> None:
    """用 stub 顶掉 infrared_protocols，单测发码路径。

    这条路径正是自检抓出"全正数组会被拒收"的地方，必须留测试。
    """
    print("\n[7] ir_command 单元测试（stub infrared_protocols）")

    pkg = types.ModuleType("infrared_protocols")
    pkg.__path__ = []
    sub = types.ModuleType("infrared_protocols.commands")

    class Command(abc.ABC):
        def __init__(self, *, modulation: int, repeat_count: int = 0) -> None:
            self.modulation = modulation
            self.repeat_count = repeat_count

        @abc.abstractmethod
        def get_raw_timings(self) -> list[int]:
            raise NotImplementedError

    sub.Command = Command
    sys.modules["infrared_protocols"] = pkg
    sys.modules["infrared_protocols.commands"] = sub

    mod = load_module(
        "_ir_hub_shim.ir_command", os.path.join(COMPONENT, "ir_command.py")
    )

    check(
        mod.parse_timings("1000 -500 1000") == [1000, -500, 1000],
        'parse_timings("1000 -500 1000")',
    )
    check(
        mod.parse_timings("1000,-500;|1000") == [1000, -500, 1000],
        "parse_timings 支持逗号/分号/竖线分隔",
    )

    plain = mod.build_raw_command([100, -200], carrier=38000, repeats=1)
    check(
        plain.get_raw_timings() == [100, -200] and plain.modulation == 38000,
        "build_raw_command repeats=1 不复制、modulation 透传",
    )

    trip = mod.build_raw_command([100, -200], carrier=56000, repeats=3)
    check(
        trip.get_raw_timings() == [100, -200] * 3 and trip.modulation == 56000,
        "build_raw_command repeats=3 复制三帧",
    )

    # 除零/负数防御
    clamped = mod.build_raw_command([100, -200], carrier=0, repeats=0)
    check(
        clamped.get_raw_timings() == [100, -200] and clamped.modulation == 38000,
        "carrier=0 回落默认 38000；repeats=0 视为 1",
    )

    # 拒收分支：全正数组 / 空数组（正是自检发现的坑）
    for bad_input, label in (([100, 200], "全正数组"), ([], "空数组")):
        try:
            mod.RawTimingsCommand(bad_input, modulation=38000)
        except ValueError:
            check(True, f"RawTimingsCommand 拒收{label}")
        else:
            check(False, f"RawTimingsCommand 拒收{label}", "竟然没抛 ValueError")


def _install_protocols_stub() -> None:
    """顶掉 `infrared_protocols.commands.Command`（HA 自带的库，本机没有）。"""
    pkg = types.ModuleType("infrared_protocols")
    pkg.__path__ = []
    sub = types.ModuleType("infrared_protocols.commands")

    class Command(abc.ABC):
        def __init__(self, *, modulation: int, repeat_count: int = 0) -> None:
            self.modulation = modulation
            self.repeat_count = repeat_count

        @abc.abstractmethod
        def get_raw_timings(self) -> list[int]:
            raise NotImplementedError

    sub.Command = Command
    sys.modules["infrared_protocols"] = pkg
    sys.modules["infrared_protocols.commands"] = sub


def _install_ha_stubs() -> type:
    """造一整套最小的 `homeassistant.*` 外壳。

    只为让 `remote.py` 能在没装 HA 的机器上 import 并跑起来 —— 顶部有
    `from __future__ import annotations`，所以类型注解不求值，只需真实存在的
    类名 / 常量 / 基类。返回 `HomeAssistantError` 供断言用。
    """

    def mod(name: str) -> types.ModuleType:
        module = types.ModuleType(name)
        module.__path__ = []
        sys.modules[name] = module
        return module

    for parent in ("homeassistant", "homeassistant.components", "homeassistant.helpers"):
        mod(parent)

    core = mod("homeassistant.core")

    class HomeAssistant: ...

    core.HomeAssistant = HomeAssistant
    core.callback = lambda fn: fn

    exceptions = mod("homeassistant.exceptions")

    class HomeAssistantError(Exception): ...

    exceptions.HomeAssistantError = HomeAssistantError

    config_entries = mod("homeassistant.config_entries")

    class ConfigEntry: ...

    config_entries.ConfigEntry = ConfigEntry

    infrared = mod("homeassistant.components.infrared")

    class InfraredEmitterConsumerEntity:
        entity_id = "remote.stub"
        _infrared_emitter_entity_id = ""

        def async_write_ha_state(self) -> None:
            self.wrote_state = True

        async def async_added_to_hass(self) -> None:
            pass

        async def _send_command(self, command) -> None:
            self.sent.append(command)

    infrared.InfraredEmitterConsumerEntity = InfraredEmitterConsumerEntity

    remote = mod("homeassistant.components.remote")

    class RemoteEntityFeature:
        LEARN_COMMAND = 1
        DELETE_COMMAND = 2
        ACTIVITY = 4

    class RemoteEntity:
        entity_id = "remote.stub"

        def async_write_ha_state(self) -> None:
            self.wrote_state = True

    remote.RemoteEntityFeature = RemoteEntityFeature
    remote.RemoteEntity = RemoteEntity
    # 值与 HA 的 remote/const.py 一致
    remote.ATTR_ACTIVITY = "activity"
    remote.ATTR_NUM_REPEATS = "num_repeats"

    device_registry = mod("homeassistant.helpers.device_registry")
    # 真实 DeviceInfo 是 TypedDict ⇒ 调用即返回 dict
    device_registry.DeviceInfo = lambda **kwargs: kwargs

    entity_platform = mod("homeassistant.helpers.entity_platform")
    entity_platform.AddEntitiesCallback = list

    # ---- button 平台需要的外壳 ----
    button = mod("homeassistant.components.button")

    class ButtonEntity:
        entity_id = "button.stub"

        def async_write_ha_state(self) -> None:
            self.wrote_state = True

    button.ButtonEntity = ButtonEntity

    # `homeassistant.util.slugify`：HA 的真身是 python-slugify（+ Unidecode）的薄封装。
    util = mod("homeassistant.util")

    def _slugify(value, separator: str = "_") -> str:
        """优先用真的 python-slugify；没装就退化到等价简化实现。

        按键名全是 ASCII（power / vol+ / up / …），简化实现对它们与真实现
        **结果一致** ⇒ 即使发布形态没装 python-slugify，这条测试依然有效。
        """
        try:
            from slugify import slugify as _real

            return _real(value, separator=separator)
        except ImportError:
            import re as _re

            return _re.sub(r"[^a-z0-9]+", separator, str(value).lower()).strip(
                separator
            )

    util.slugify = _slugify

    # ---- climate 平台需要的外壳 ----
    climate = mod("homeassistant.components.climate")

    class ClimateEntity:
        entity_id = "climate.stub"

        def async_write_ha_state(self) -> None:
            self.wrote_state = True

    class ClimateEntityFeature:
        TARGET_TEMPERATURE = 1
        FAN_MODE = 2
        TURN_ON = 4
        TURN_OFF = 8
        SWING_MODE = 16

    class HVACMode(str, enum.Enum):
        OFF = "off"
        COOL = "cool"
        HEAT = "heat"
        AUTO = "auto"
        FAN_ONLY = "fan_only"
        DRY = "dry"

    climate.ClimateEntity = ClimateEntity
    climate.ClimateEntityFeature = ClimateEntityFeature
    climate.HVACMode = HVACMode

    restore_state = mod("homeassistant.helpers.restore_state")

    class RestoreEntity:
        entity_id = "restore.stub"

        def async_write_ha_state(self) -> None:
            self.wrote_state = True

        async def async_get_last_state(self):
            return None

    restore_state.RestoreEntity = RestoreEntity

    # `homeassistant.const`（`__init__.py` 的 `from homeassistant.const import Platform`）
    ha_const = mod("homeassistant.const")

    class Platform:
        REMOTE = "remote"
        BUTTON = "button"
        CLIMATE = "climate"
        SENSOR = "sensor"

    ha_const.Platform = Platform
    ha_const.ATTR_TEMPERATURE = "temperature"

    # `homeassistant.core.ServiceCall` —— `__init__.py` 用它做类型注解，
    # 配合 `from __future__ import annotations` 只需名字存在（不会求值）。
    class ServiceCall:
        def __init__(self) -> None:
            self.data: dict = {}
            self.context = None

    core.ServiceCall = ServiceCall

    # `homeassistant.helpers.config_validation`（`__init__.py` import 成 cv，
    # 用了 cv.entity_id / cv.positive_int）
    config_validation = mod("homeassistant.helpers.config_validation")
    config_validation.entity_id = str
    config_validation.positive_int = int
    config_validation.string = str

    # 把子模块挂到父模块上，`from a.b import c` 才找得到
    ha = sys.modules["homeassistant"]
    ha.const = ha_const
    ha.core, ha.exceptions, ha.config_entries = core, exceptions, config_entries
    ha.components = sys.modules["homeassistant.components"]
    ha.helpers = sys.modules["homeassistant.helpers"]
    ha.components.infrared, ha.components.remote = infrared, remote
    ha.components.button = button
    ha.components.climate = climate
    ha.util = util
    ha.helpers.device_registry, ha.helpers.entity_platform = device_registry, entity_platform
    ha.helpers.restore_state = restore_state
    ha.helpers.config_validation = config_validation
    return HomeAssistantError


def check_remote_entity() -> None:
    """用 stub 跑 `remote.py` 的真实逻辑：发码、重复次数、裸时序、错误分支。

    这条路径没有任何硬件也能测 —— 而它正是上线后会跑的那段代码。
    """
    import asyncio

    print("\n[8] remote 实体逻辑（stub homeassistant）")
    _install_protocols_stub()
    HomeAssistantError = _install_ha_stubs()

    remote_mod = load_module(
        "_ir_hub_shim.remote", os.path.join(COMPONENT, "remote.py")
    )
    library_mod = sys.modules["_ir_hub_shim.library"]
    library = library_mod.CodeLibrary()

    class FakeEntry:
        entry_id = "test-entry"
        options = {}
        data = {
            "emitter": "infrared.ir_control_ir_transmitter",
            "category": 2,
            "brand": 14,
            "device": 47,
            "carrier": 38000,
            "repeats": 1,
        }

    def build():
        entity = remote_mod.IrHubRemote(None, FakeEntry(), library)
        entity.sent = []
        entity.wrote_state = False
        return entity

    # --- 构造 ---
    entity = build()
    check(
        entity._infrared_emitter_entity_id == "infrared.ir_control_ir_transmitter",
        "构造时接收 emitter 实体 id（供基类跟踪可用性）",
    )
    check(entity._carrier == 38000 and entity._repeats == 1, "默认载波 38000 / 发送 1 次")
    check(
        entity._attr_activity_list[0] == "power" and len(entity._attr_activity_list) > 10,
        f"activity_list 来自码库（{len(entity._attr_activity_list)} 个键，power 第一）",
    )
    check(
        entity._attr_device_info["manufacturer"] == "TCL"
        and entity._attr_device_info["model"] == "TCL电视-1",
        "device_info 用码库里的品牌/型号",
    )

    async def run():
        # --- 基本发码 ---
        entity = build()
        await entity.async_send_command(["power"])
        check(
            len(entity.sent) == 1
            and len(entity.sent[0].get_raw_timings()) == 156
            and entity.sent[0].modulation == 38000
            and entity.sent[0].repeat_count == 1,
            "发 1 个键 -> 1 次发送，156 项，载波 38000",
        )
        check(entity._attr_current_activity == "power", "current_activity 记录最后按键")

        # --- 多个键按顺序发 ---
        entity = build()
        await entity.async_send_command(["vol+", "vol+"])
        check(len(entity.sent) == 2, "两个键 -> 两次发送")

        # --- ⚠️ 裸字符串（本方法被直接调用时服务 schema 不生效）----
        entity = build()
        try:
            await entity.async_send_command("power")
            check(len(entity.sent) == 1, "裸字符串 'power' -> 只发 1 次（不被拆成 5 个字符）")
        except HomeAssistantError as err:
            check(False, "裸字符串 'power' -> 只发 1 次", f"被拆成单字符键而报错: {err}")

        # --- 发送次数：服务参数只在显式 >1 时胜出 ---
        entity = build()
        await entity.async_send_command(["power"], num_repeats=1)  # 服务默认值
        check(
            len(entity.sent[-1].get_raw_timings()) == 156,
            "num_repeats=1（服务默认）不覆盖配置 -> 仍是 1 帧",
        )
        entity = build()
        await entity.async_send_command(["power"], num_repeats=3)
        check(
            len(entity.sent[-1].get_raw_timings()) == 468
            and entity.sent[-1].repeat_count == 3,
            "显式 num_repeats=3 -> 时序复制 3 帧（468 项）",
        )

        # --- 逃生口：raw: 前缀 ---
        entity = build()
        await entity.async_send_command(["raw:100 -200 300 -400"])
        check(
            entity.sent[-1].get_raw_timings() == [100, -200, 300, -400],
            "raw: 前缀直接发裸时序",
        )

        # --- turn_on / turn_off 与 activity ---
        entity = build()
        await entity.async_turn_on()
        check(len(entity.sent) == 1 and entity._attr_current_activity == "power",
              "turn_on 默认按 power")
        entity = build()
        await entity.async_turn_on(activity="vol+")
        check(entity._attr_current_activity == "vol+", "turn_on(activity=...) 按 activity 键")
        entity = build()
        await entity.async_turn_off()
        check(len(entity.sent) == 1, "turn_off 默认按 power")

        # --- 未知键：必须抛清晰错误，而不是静默失败 ---
        entity = build()
        try:
            await entity.async_send_command(["no_such_key"])
        except HomeAssistantError as err:
            check(
                "no_such_key" in str(err) and "power" in str(err),
                "未知按键 -> 抛 HomeAssistantError 并列出可用按键",
            )
        else:
            check(False, "未知按键 -> 抛 HomeAssistantError 并列出可用按键", "竟然没抛")

    asyncio.run(run())


def _install_config_flow_stubs() -> None:
    """在 `_install_ha_stubs()` 之上补齐 config_flow 需要的东西。

    ⚠️ `ConfigFlow.__init_subclass__` 必须收 `domain=` —— 我们的 flow 写法是
    `class IrHubConfigFlow(config_entries.ConfigFlow, domain=DOMAIN)`。
    """
    config_entries = sys.modules["homeassistant.config_entries"]
    infrared = sys.modules["homeassistant.components.infrared"]

    class ConfigFlow:
        VERSION = 1
        # ⚠️ 必须放在**类属性**上：`IrHubConfigFlow.__init__` 没有调 `super().__init__()`，
        #    所以实例上不会有这些属性 —— 放类属性能让"未调用过某个 step"时也能安全读取。
        shown = None
        created = None
        aborted = None

        def __init_subclass__(cls, *, domain=None, **kwargs):
            super().__init_subclass__(**kwargs)
            if domain is not None:
                cls.domain = domain

        def __init__(self):
            self.hass = None

        # ⚠️ 这三个在真实 HA 里是**协程**，由 flow manager 消费；但要特别注意，
        #    `config_flow` 里写的是 `return self.async_show_form(...)` —— **不 await**。
        #    所以 stub 必须做成**同步**方法，否则返回的协程根本不会被执行，
        #    断言永远看到 None（本次就踩了这个坑）。被测的是 config_flow 的分支
        #    逻辑，不是 HA 的协程契约，故这样等价且更直接。
        def async_show_form(self, **kwargs):
            self.shown = kwargs
            return kwargs

        def async_create_entry(self, **kwargs):
            self.created = kwargs
            return kwargs

        def async_abort(self, **kwargs):
            self.aborted = kwargs.get("reason")
            return kwargs

    class OptionsFlow:
        config_entry = None
        shown = None
        created = None

        def async_show_form(self, **kwargs):
            self.shown = kwargs
            return kwargs

        def async_create_entry(self, **kwargs):
            self.created = kwargs
            return kwargs

    config_entries.ConfigFlow = ConfigFlow
    config_entries.OptionsFlow = OptionsFlow

    # 测试里改这个列表来控制"当前有哪些红外发射器"
    infrared.STUB_EMITTERS = []
    infrared.async_get_emitters = lambda hass: list(infrared.STUB_EMITTERS)


def _schema_options(schema, key: str) -> dict | None:
    """从 `vol.Schema` 里取出某个键的 `vol.In` 选项（断言下拉框内容用）。

    `vol.In(字典)` 的 key 是提交值、value 是显示文本；`vol.In(列表)` 两者相同。
    """
    for marker, validator in schema.schema.items():
        if getattr(marker, "schema", marker) != key:
            continue
        container = getattr(validator, "container", None)
        if container is None:
            return None
        if isinstance(container, dict):
            return dict(container)
        return {item: None for item in container}
    return None


def _schema_defaults(schema) -> dict:
    """取出 schema 各字段的 default（OptionsFlow 默认值断言用）。

    ⚠️ voluptuous 把 `default=` 包进 `default_factory`：`Required(k, default=38000)`
    的 `marker.default` 是 `lambda: 38000` 而**不是** 38000 ⇒ 必须调用一次。
    """
    out = {}
    for marker in schema.schema:
        key = getattr(marker, "schema", marker)
        default = getattr(marker, "default", None)
        if callable(default):
            default = default()
        out[key] = default
    return out


def check_config_flow() -> None:
    """跑 `config_flow.py` 的真实逻辑（**真 voluptuous** + stub homeassistant）。

    这是用户**第一步**就会走的路 —— 这里坏掉等于集成根本加不上。
    用真 voluptuous 是有意的：能顺带验证「`vol.In(字典)` 提交回来的是 key 字符串」
    这个假设 —— `config_flow` 紧接着 `int(user_input[...])`，假设错了就全盘崩。
    """
    import asyncio

    print("\n[9] config_flow 逻辑（真 voluptuous + stub homeassistant）")
    _install_protocols_stub()
    _install_ha_stubs()
    _install_config_flow_stubs()

    infrared = sys.modules["homeassistant.components.infrared"]
    flow_mod = load_module(
        "_ir_hub_shim.config_flow", os.path.join(COMPONENT, "config_flow.py")
    )
    EMITTER = "infrared.ir_control_ir_transmitter"

    class FakeHass:
        async def async_add_executor_job(self, func, *args):
            return func(*args)

    class FakeEntry:
        title = "TCL电视-1"
        options = {}
        data = {
            "emitter": EMITTER,
            "category": 2,
            "brand": 14,
            "device": 47,
            "carrier": 38000,
            "repeats": 1,
        }

    def new_flow():
        flow = flow_mod.IrHubConfigFlow()
        flow.hass = FakeHass()
        return flow

    check(
        getattr(flow_mod.IrHubConfigFlow, "domain", None) == "ir_hub",
        "flow 注册的 domain = ir_hub",
    )

    async def run():
        # ① 一个 emitter 都没有 -> abort（引导用户先去配 ESPHome）
        infrared.STUB_EMITTERS = []
        flow = new_flow()
        await flow.async_step_user()
        check(flow.aborted == "no_emitters", "无 infrared emitter -> abort(no_emitters)")

        # ② 有 emitter -> 出表单
        infrared.STUB_EMITTERS = [EMITTER]
        flow = new_flow()
        await flow.async_step_user()
        check(flow.shown["step_id"] == "user", "第 1 步 step_id = user")
        schema = flow.shown["data_schema"]
        check(
            _schema_options(schema, "emitter") == {EMITTER: None},
            "emitter 下拉框列出全部可用发射器",
        )
        cats = _schema_options(schema, "category") or {}
        check(
            "电视机" in cats.get("2", "") and "空调" in cats.get("1", ""),
            f"category 下拉框给出中文类别名 + 设备数（{len(cats)} 项）",
            f"2 -> {cats.get('2')!r}, 1 -> {cats.get('1')!r}",
        )

        # ③ 真 voluptuous 校验：确认 `vol.In(字典)` 提交回来的是 **key 字符串**
        validated = schema({"emitter": EMITTER, "category": "2"})
        check(
            validated["category"] == "2" and validated["emitter"] == EMITTER,
            "vol.In(字典) 返回 key 字符串（后续 int() 转换才成立）",
            f"实际 {validated!r}",
        )

        # ④ 选大类 -> 进 brand 步
        flow = new_flow()
        await flow.async_step_user({"emitter": EMITTER, "category": "2"})
        check(flow.shown.get("step_id") == "brand", "选完大类 -> 进 brand 步")
        brands = _schema_options(flow.shown["data_schema"], "brand") or {}
        check(
            len(brands) > 10 and all(k.isdigit() for k in brands),
            f"brand 步给出 {len(brands)} 个品牌（key 为数字字符串）",
        )

        # ⑤ 选品牌 -> 进 device 步
        await flow.async_step_brand({"brand": "14"})
        check(flow.shown.get("step_id") == "device", "选完品牌 -> 进 device 步")
        devices = _schema_options(flow.shown["data_schema"], "device") or {}
        label47 = devices.get("47", "")
        check("TCL电视-1" in label47, "device 步列出 TCL电视-1", f"实际 {label47!r}")
        check("RCA" in label47, f"型号附 irext 协议提示 -> {label47!r}")

        # ⑥ 选型号 -> 建 entry
        flow = new_flow()
        flow._emitter = EMITTER
        flow._category = 2
        flow._brand = 14
        await flow.async_step_device({"device": "47"})
        created = flow.created
        check(
            created["title"] == "TCL电视-1",
            f"entry title 不重复品牌前缀（{created['title']!r}，不是 'TCL TCL电视-1'）",
        )
        data = created["data"]
        check(
            data["emitter"] == EMITTER
            and data["device"] == 47
            and data["category"] == 2
            and data["brand"] == 14
            and data["carrier"] == 38000
            and data["repeats"] == 1,
            "entry data 完整（emitter/category/brand/device/carrier/repeats）",
            f"实际 {data!r}",
        )
        check(
            flow_mod.IrHubConfigFlow.async_get_options_flow(None).__class__.__name__
            == "IrHubOptionsFlow",
            "async_get_options_flow 返回 IrHubOptionsFlow",
        )

        # ⑦ OptionsFlow
        options = flow_mod.IrHubOptionsFlow()
        options.config_entry = FakeEntry()
        await options.async_step_init()
        check(options.shown["step_id"] == "init", "options 步 step_id = init")
        defaults = _schema_defaults(options.shown["data_schema"])
        check(
            defaults.get("carrier") == 38000 and defaults.get("repeats") == 1,
            "options 表单默认值来自 entry（38000 / 1）",
            f"实际 {defaults!r}",
        )
        check(
            options.shown["data_schema"]({"carrier": "40000", "repeats": "3"})
            == {"carrier": 40000, "repeats": 3},
            "options 对输入做 Coerce(int)（字符串 -> 整数）",
        )
        options = flow_mod.IrHubOptionsFlow()
        options.config_entry = FakeEntry()
        await options.async_step_init({"carrier": 56000, "repeats": 2})
        check(
            options.created["data"] == {"carrier": 56000, "repeats": 2},
            "options 保存 carrier/repeats（改完自动 reload）",
        )

    asyncio.run(run())


def check_button_platform() -> None:
    """[10] button 平台 —— 每键一个按钮；重点是 entity_id 的撞名防护。

    这条路径没有硬件也能测，且跑的正是上线后会跑的代码。
    """
    import asyncio

    print("\n[10] button 平台逻辑（stub homeassistant）")
    _install_protocols_stub()
    _install_ha_stubs()

    button_mod = load_module(
        "_ir_hub_shim.button", os.path.join(COMPONENT, "button.py")
    )
    library_mod = sys.modules["_ir_hub_shim.library"]
    const_mod = sys.modules["_ir_hub_shim.const"]
    library = library_mod.CodeLibrary()
    ha_slugify = sys.modules["homeassistant.util"].slugify

    DOMAIN = const_mod.DOMAIN
    CONF_EMITTER = const_mod.CONF_EMITTER
    CONF_CATEGORY = const_mod.CONF_CATEGORY
    CONF_BRAND = const_mod.CONF_BRAND
    CONF_DEVICE = const_mod.CONF_DEVICE
    CONF_CARRIER = const_mod.CONF_CARRIER
    CONF_REPEATS = const_mod.CONF_REPEATS

    koi = button_mod.key_object_id

    # --- 1. 符号映射：这是本平台唯一需要小心的地方 ---
    check(koi("power") == "power", "key_object_id('power') 原样保留")
    check(koi("vol+") == "vol_plus", f"vol+ -> vol_plus（实得 {koi('vol+')!r}）")
    check(koi("vol-") == "vol_minus", f"vol- -> vol_minus（实得 {koi('vol-')!r}）")
    check(koi("page+") == "page_plus", f"page+ -> page_plus（实得 {koi('page+')!r}）")
    check(koi("page-") == "page_minus", f"page- -> page_minus（实得 {koi('page-')!r}）")
    check(
        koi("vol+") != koi("vol-") and koi("page+") != koi("page-"),
        "⭐ 正负号键不再撞名（裸 slugify 会把 vol+/vol- 都压成 vol）",
    )

    # --- 2. ⭐ 不变量：**同一台设备内**按键 object_id 不能撞名 ---
    # 先给全库所有键名（实测只有 61 种）建一张 object_id 映射表 —— 只做 61 次
    # slugify；再逐设备查表（15 万次字典查找，很快）。
    # ⚠️ 别改成"逐设备逐键调 slugify"：实测直接卡死（Unidecode 开销）。
    # ⚠️ 也别把判据写成"全局键名集合无撞名" —— 那个条件**过严**：实测全库同时存在
    #    'Shake' 与 'shake'（slugify 后同为 'shake'），但**没有任何设备同时含这两键**
    #    （含 Shake 的 20 台 / 含 shake 的 364 台，交集 0）⇒ 实际不撞。
    all_key_names = sorted({k for dev in library.devices for k in dev["keys"]})
    oid_of = {k: koi(k) for k in all_key_names}
    dup_devs = []
    for dev in library.devices:
        ids = [oid_of.get(k) or koi(k) for k in dev["keys"]]
        if len(set(ids)) != len(ids):
            dup_devs.append((dev["name"], sorted(ids)))
    check(
        not dup_devs,
        f"全库 {len(library.devices)} 台设备内按键 object_id 均无撞名"
        f"（键名共 {len(all_key_names)} 种）",
        str(dup_devs[:2]),
    )

    # --- 3. 实体构造 ---
    DEV_ID = 47
    device = library.get_device(DEV_ID)
    keys = library.key_names(device)
    brand = library.brands.get(device["brand"]) or f"品牌 {device['brand']}"
    display = library.display_name(brand, device["name"])
    emitter = "infrared.ir_control_ir_transmitter"

    class FakeEntry:
        entry_id = "test-entry"
        data = {
            CONF_EMITTER: emitter,
            CONF_CATEGORY: str(device["category"]),
            CONF_BRAND: str(device["brand"]),
            CONF_DEVICE: str(DEV_ID),
            CONF_CARRIER: 38000,
            CONF_REPEATS: 1,
        }
        options: dict = {}

    def build(key: str):
        ent = button_mod.IrHubButton(
            entry=FakeEntry(),
            library=library,
            device=device,
            key=key,
            carrier=38000,
            repeats=1,
            emitter=emitter,
            device_info={"identifiers": {(DOMAIN, "test-entry")}},
            entity_id=f"button.{ha_slugify(display)}_{koi(key)}",
        )
        ent.sent = []  # stub 的 _send_command 往这里 append
        return ent

    btn = build("vol+")
    check(btn._attr_name == "vol+", "显示名 = 原始按键名（'vol+' 而非 'vol_plus'）")
    check(
        btn.entity_id == f"button.{ha_slugify(display)}_vol_plus",
        f"entity_id = {btn.entity_id}",
    )
    check(btn._attr_unique_id == "test-entry_vol_plus", f"unique_id = {btn._attr_unique_id}")
    check(btn._attr_icon == "mdi:volume-plus", f"图标 = {btn._attr_icon}")
    check(
        btn._infrared_emitter_entity_id == emitter,
        "把 emitter id 交给基类（供其跟踪 emitter 可用性）",
    )
    check(button_mod.PARALLEL_UPDATES == 0, "PARALLEL_UPDATES = 0（红外无状态，不轮询）")

    # --- 4. async_press 真的把码交给 emitter ---
    # ⚠️ 下面三条**必须无条件执行**（不能用 `if btn.sent:` 包起来）：否则一旦发送
    #    失败，就会静默少跑两项 —— 那正是"项数护栏"要防的假通过。
    asyncio.run(btn.async_press())
    check(len(btn.sent) == 1, "async_press 发出 1 次")
    cmd = btn.sent[0] if btn.sent else None
    check(
        cmd is not None and cmd.modulation == 38000,
        f"载波透传 = {getattr(cmd, 'modulation', None)}",
    )
    expect = len(library.get_timings(DEV_ID, "vol+"))
    got = len(cmd.get_raw_timings()) if cmd is not None else -1
    check(got == expect, f"vol+ 时序 {got} 项（应为 {expect}）")

    # --- 5. 全部按键都能构造并发出（全量，不抽样）---
    fails = []
    for k in keys:
        ent = build(k)
        try:
            asyncio.run(ent.async_press())
            if len(ent.sent) != 1:
                fails.append((k, "发送次数≠1"))
        except Exception as ex:  # noqa: BLE001 - 自检要看到任何异常
            fails.append((k, f"{type(ex).__name__}: {ex}"))
    check(not fails, f"设备 {DEV_ID} 全部 {len(keys)} 个按键都能构造并发出", str(fails[:3]))

    ids = [build(k).entity_id for k in keys]
    check(len(set(ids)) == len(ids), f"该设备 {len(ids)} 个 entity_id 互不重复")

    # --- 6. async_setup_entry 的行为 ---
    class FakeHass:
        def __init__(self) -> None:
            self.data = {DOMAIN: {"test-entry": library}}

    got: list = []
    asyncio.run(button_mod.async_setup_entry(FakeHass(), FakeEntry(), got.extend))
    check(
        len(got) == len(keys),
        f"async_setup_entry 生成 {len(got)} 个 button（应为 {len(keys)}）",
    )
    check(
        len({e.entity_id for e in got}) == len(got),
        "生成的 button entity_id 全不重复",
    )

    class BadEntry(FakeEntry):
        data = dict(FakeEntry.data, device="999999999")

    got2: list = []
    asyncio.run(button_mod.async_setup_entry(FakeHass(), BadEntry(), got2.extend))
    check(not got2, "设备 id 不存在时不生成实体、也不抛异常")


def check_services_yaml() -> None:
    """[11] services.yaml 与 SEND_RAW_SCHEMA 的字段一致性。

    services.yaml 只影响 UI 展示（HA「开发者工具 → 操作」里的中文标签与选择器），
    但**格式错会被 HA 拒绝加载，而且是静默的** —— 所以值得一条断言。
    还要防"UI 里写的字段"与"真正校验的 schema"不一致（多写一个用户会填了没用、
    少写一个用户在 UI 里看不到）。
    """
    print("\n[11] services.yaml（服务元数据 / UI 展示）")
    path = os.path.join(COMPONENT, "services.yaml")
    check(os.path.isfile(path), "services.yaml 存在")

    try:
        import yaml
    except ImportError:
        skip("services.yaml 结构校验（需要 PyYAML）", "本机没装 PyYAML")
        return

    data = yaml.safe_load(open(path, encoding="utf-8").read()) or {}
    check(isinstance(data, dict) and bool(data), "services.yaml 可解析且非空")

    const_mod = sys.modules["_ir_hub_shim.const"]
    svc = const_mod.SERVICE_SEND_RAW
    check(svc in data, f"服务名 {svc!r} 与 const.SERVICE_SEND_RAW 一致")

    try:
        init_mod = load_module(
            "_ir_hub_shim.__init__", os.path.join(COMPONENT, "__init__.py")
        )
        schema_keys = {
            getattr(k, "schema", k) for k in init_mod.SEND_RAW_SCHEMA.schema
        }
    except Exception as ex:  # noqa: BLE001 - 自检要看到任何异常
        check(False, "能 load __init__.py 并取到 SEND_RAW_SCHEMA", f"{type(ex).__name__}: {ex}")
        return

    fields = set(((data.get(svc) or {}).get("fields")) or {})
    check(
        fields == schema_keys,
        f"services.yaml 字段集与 SEND_RAW_SCHEMA 一致（{sorted(fields)}）",
        f"schema={sorted(schema_keys)}",
    )


def check_ac() -> None:
    """[12] AC 状态码库 + climate 平台。

    三层：
      A. 码库数据（index 覆盖、bin 齐全、关键品牌在位）
      B. 解码器（全库 399 bin 解码 + 符号交替不变式 + 与实测日志对齐的锚点）
      C. climate 实体逻辑（stub homeassistant，跑真实代码路径）
      D. config flow 的 AC 分支（真 voluptuous）
    """
    import asyncio

    print("\n[12] AC 状态码库 + climate 平台")
    _install_protocols_stub()
    HomeAssistantError = _install_ha_stubs()

    const_mod = sys.modules["_ir_hub_shim.const"]
    ha_const = sys.modules["homeassistant.const"]
    HVACMode = sys.modules["homeassistant.components.climate"].HVACMode

    # --- A. 码库数据 ---
    ac_mod = load_module(
        "_ir_hub_shim.ac_library", os.path.join(COMPONENT, "ac_library.py")
    )
    ac_lib = ac_mod.AcLibrary()
    check(ac_lib.brand_count >= 200, f"AC 品牌 = {ac_lib.brand_count}")
    check(ac_lib.device_count >= 1000, f"AC 型号 = {ac_lib.device_count}")
    for brand in ("美的", "格力", "TCL", "海尔", "奥克斯", "海信"):
        check(brand in ac_lib.brands, f"关键品牌在位：{brand}")

    codes_dir = os.path.join(COMPONENT, "ac_library", "codes")
    missing = [
        dev["bin"]
        for brand in ac_lib._brands.values()
        for dev in brand
        if not os.path.exists(os.path.join(codes_dir, dev["bin"]))
    ]
    check(not missing, "index.json 引用的 bin 文件全部在位", str(missing[:3]))

    # --- B. 解码器：全库（不抽样）---
    bins = sorted(os.path.basename(p) for p in glob.glob(os.path.join(codes_dir, "*.bin")))
    check(len(bins) >= 300, f"bin 文件数 = {len(bins)}（index 条目去重前 1053）")

    bad: list[str] = []
    frames = 0
    max_timing = 0
    for bin_name in bins:
        try:
            code = ac_lib.load_device(bin_name)
            assert code["modes"], bin_name
            assert code["off"] and code["off"][0] > 0, bin_name
            all_frames = [code["off"]]
            for fans in code["commands"].values():
                for temps in fans.values():
                    all_frames.extend(temps.values())
            for frame in all_frames:
                frames += 1
                if frame[0] <= 0 or not any(v < 0 for v in frame):
                    raise ValueError("帧不满足 mark 开头且含 space")
                for index, value in enumerate(frame):
                    if (index % 2 == 0 and value < 0) or (
                        index % 2 == 1 and value > 0
                    ):
                        raise ValueError(f"符号交替破坏 @idx={index} v={value}")
                    if abs(value) > max_timing:
                        max_timing = abs(value)
        except Exception as ex:  # noqa: BLE001
            bad.append(f"{bin_name}: {type(ex).__name__}: {ex}")
    check(
        not bad,
        f"全部 {len(bins)} 个 bin 解码成功（{frames} 帧，帧帧带符号交替）",
        "; ".join(bad[:3]),
    )
    check(
        max_timing <= MAX_SANE_TIMING_US,
        f"AC 最大单段时长 {max_timing} µs ≤ {MAX_SANE_TIMING_US} µs",
    )

    # 锚点：美的 11272（用户 SmartAC 实配的型号）—— 钉住长度与引导码
    code = ac_lib.load_device("irda_new_ac_11272.bin")
    frame = code["commands"]["cool"]["auto"]["26"]
    check(
        code["modes"] == ["cool", "heat", "auto", "fan_only", "dry"]
        and code["min_temp"] == 17
        and code["max_temp"] == 30,
        "锚点 美的 11272：5 模式 / 17~30°C",
        f"实际 modes={code['modes']} temp={code['min_temp']}~{code['max_temp']}",
    )
    check(
        len(frame) == 200 and frame[0] == 4453 and frame[1] == -4453,
        "锚点 美的 11272 cool/26 = 200 项，引导 4453/-4453",
        f"实际 {len(frame)} 项，前 2 值 {frame[:2]}",
    )

    # --- C. climate 实体逻辑 ---
    climate_mod = load_module(
        "_ir_hub_shim.climate", os.path.join(COMPONENT, "climate.py")
    )
    check(climate_mod.PARALLEL_UPDATES == 0, "PARALLEL_UPDATES = 0")

    import types as _types

    FakeHass = _types.SimpleNamespace(
        config=_types.SimpleNamespace(
            units=_types.SimpleNamespace(temperature_unit="°C")
        ),
        data={},
    )

    class FakeEntry:
        entry_id = "ac-entry"
        options: dict = {}
        data = {
            "emitter": "infrared.ir_control_ir_transmitter",
            "category": "ac",
            "brand": "美的",
            "device": "irda_new_ac_11272.bin",
            "carrier": 38000,
            "repeats": 1,
        }

    def build():
        entity = climate_mod.IrHubClimate(
            FakeHass, FakeEntry(), dict(FakeEntry.data), dict(code)
        )
        entity.hass = FakeHass
        entity.sent = []
        entity.wrote_state = False
        return entity

    entity = build()
    check(
        entity._infrared_emitter_entity_id == "infrared.ir_control_ir_transmitter",
        "构造时接收 emitter 实体 id",
    )
    check(
        [m.value for m in entity._attr_hvac_modes]
        == ["off", "cool", "heat", "auto", "fan_only", "dry"],
        "hvac_modes = off + 码库 5 模式",
    )
    check(entity._attr_fan_modes == ["auto", "low", "medium", "high"], "fan_modes 4 档")
    check(
        entity._attr_min_temp == 17.0 and entity._attr_max_temp == 30.0,
        "min/max 温度来自码库（17/30）",
    )

    async def run():
        # 开机 -> 上次模式缺失 -> 制冷，发 1 帧状态码
        entity = build()
        await entity.async_turn_on()
        check(
            entity._attr_hvac_mode == HVACMode.COOL
            and entity._attr_target_temperature == 17.0
            and len(entity.sent) == 1,
            "turn_on（无历史）-> cool + min_temp 17°C，发 1 帧",
        )
        # 调温度 -> 重发状态帧
        entity = build()
        entity._attr_hvac_mode = HVACMode.COOL
        await entity.async_set_temperature(temperature=26)
        check(
            entity._attr_target_temperature == 26.0
            and len(entity.sent) == 1
            and len(entity.sent[0].get_raw_timings()) == 200
            and entity.sent[0].modulation == 38000,
            "set_temperature(26) -> 状态帧 200 项 / 载波 38000",
        )
        # 关机 -> off 帧
        entity = build()
        entity._attr_hvac_mode = HVACMode.COOL
        await entity.async_turn_off()
        check(
            entity._attr_hvac_mode == HVACMode.OFF
            and entity.sent[0].get_raw_timings() == code["off"],
            "turn_off -> 专用 off 帧（与码库 off 帧逐项一致）",
        )
        # 调风速（开机状态）-> 重发
        entity = build()
        entity._attr_hvac_mode = HVACMode.COOL
        await entity.async_set_fan_mode("high")
        check(len(entity.sent) == 1, "set_fan_mode（开机中）-> 重发状态帧")
        # 关机状态下调温度 -> 只改状态不发送
        entity = build()
        await entity.async_set_temperature(temperature=24)
        check(
            entity._attr_target_temperature == 24.0 and len(entity.sent) == 0,
            "关机状态 set_temperature -> 只记状态不发码",
        )
        # 组合不存在 -> 清晰报错（人为裁掉 high 风）
        entity = build()
        entity._code = dict(code)
        entity._code["commands"] = {"cool": {"auto": code["commands"]["cool"]["auto"]}}
        entity._attr_hvac_mode = HVACMode.COOL
        try:
            await entity.async_set_fan_mode("high")
        except HomeAssistantError as err:
            check(
                "不支持组合" in str(err) and "high" in str(err),
                "不存在的模式×风速组合 -> HomeAssistantError 带可用项",
            )
        else:
            check(False, "不存在的模式×风速组合 -> HomeAssistantError", "竟然没抛")
        # 恢复：历史状态 off/heat/25
        entity = build()

        class FakeState:
            state = "heat"
            attributes = {"fan_mode": "medium", "temperature": "25"}

        async def fake_last_state():
            return FakeState

        entity.async_get_last_state = fake_last_state
        await entity.async_added_to_hass()
        check(
            entity._attr_hvac_mode == HVACMode.HEAT
            and entity._attr_fan_mode == "medium"
            and entity._attr_target_temperature == 25.0
            and entity._last_on_operation == HVACMode.HEAT,
            "RestoreEntity：模式/风速/温度/last_on_operation 全恢复",
        )

    asyncio.run(run())

    # async_setup_entry：AC 条目生成 climate，非 AC 条目不生成
    class FakeSetupHass:
        def __init__(self) -> None:
            self.data = {"ir_hub": {"ac-entry": None}}

        async def async_add_executor_job(self, func, *args):
            return func(*args)

    got: list = []
    asyncio.run(climate_mod.async_setup_entry(FakeSetupHass(), FakeEntry(), got.extend))
    check(len(got) == 1, "AC 条目 -> 1 个 climate 实体")

    class TvEntry(FakeEntry):
        data = dict(FakeEntry.data, category=2, device=47)

    got2: list = []
    asyncio.run(
        climate_mod.async_setup_entry(FakeSetupHass(), TvEntry(), got2.extend)
    )
    check(not got2, "按键式条目 -> climate 平台不生成实体")

    # remote/button 平台对 AC 条目的跳过保护
    remote_mod = load_module("_ir_hub_shim.remote", os.path.join(COMPONENT, "remote.py"))
    button_mod = load_module("_ir_hub_shim.button", os.path.join(COMPONENT, "button.py"))
    library_mod = sys.modules["_ir_hub_shim.library"]
    library = library_mod.CodeLibrary()

    class RemoteHass:
        data = {"ir_hub": {"ac-entry": library}}

        async def async_add_executor_job(self, func, *args):
            return func(*args)

    got3: list = []
    asyncio.run(remote_mod.async_setup_entry(RemoteHass(), FakeEntry(), got3.extend))
    got4: list = []
    asyncio.run(button_mod.async_setup_entry(RemoteHass(), FakeEntry(), got4.extend))
    check(
        not got3 and not got4,
        "AC 条目 -> remote / button 平台都不生成实体",
    )

    # --- D. config flow 的 AC 分支 ---
    _install_config_flow_stubs()
    flow_mod = load_module(
        "_ir_hub_shim.config_flow", os.path.join(COMPONENT, "config_flow.py")
    )
    infrared = sys.modules["homeassistant.components.infrared"]
    EMITTER = "infrared.ir_control_ir_transmitter"

    class FlowHass:
        async def async_add_executor_job(self, func, *args):
            return func(*args)

    def new_flow():
        flow = flow_mod.IrHubConfigFlow()
        flow.hass = FlowHass()
        return flow

    async def run_flow():
        infrared.STUB_EMITTERS = [EMITTER]
        flow = new_flow()
        await flow.async_step_user()
        cats = _schema_options(flow.shown["data_schema"], "category") or {}
        check(
            "空调" in cats.get("ac", "") and "品牌" in cats.get("ac", ""),
            f"category 下拉含「空调 · 温控面板」特殊项（{cats.get('ac')!r}）",
        )
        flow = new_flow()
        await flow.async_step_user({"emitter": EMITTER, "category": "ac"})
        check(flow.shown.get("step_id") == "ac_brand", "选空调 -> 进 ac_brand 步")
        brands = _schema_options(flow.shown["data_schema"], "brand") or {}
        check("美的" in brands and len(brands) >= 200, f"ac_brand 列出 {len(brands)} 品牌")
        await flow.async_step_ac_brand({"brand": "美的"})
        check(flow.shown.get("step_id") == "ac_device", "选品牌 -> 进 ac_device 步")
        devices = _schema_options(flow.shown["data_schema"], "device") or {}
        check(
            "irda_new_ac_11272.bin" in devices,
            f"ac_device 列出 {len(devices)} 型号（含 11272）",
        )
        await flow.async_step_ac_device({"device": "irda_new_ac_11272.bin"})
        created = flow.created
        data = created["data"]
        check(
            data["category"] == "ac"
            and data["brand"] == "美的"
            and data["device"] == "irda_new_ac_11272.bin"
            and data["emitter"] == EMITTER
            and data["carrier"] == 38000,
            "AC entry data 完整（category=ac / brand / device bin / emitter / carrier）",
            f"实际 {data!r}",
        )
        check("11272" in created["title"], f"entry title 含型号编号（{created['title']!r}）")

    asyncio.run(run_flow())


def main() -> int:
    print("=" * 68)
    print("IR Hub 自检")
    print("=" * 68)
    make_shim_package()
    check_syntax()
    check_manifests()
    load_module("_ir_hub_shim.const", os.path.join(COMPONENT, "const.py"))
    check_library()
    check_ir_command()
    check_remote_entity()
    check_config_flow()
    check_button_platform()
    check_services_yaml()
    check_ac()

    # ⚠️ 自检自身的护栏（务必保留）：若某一段 check 因为异常、条件分支或脚本被
    #    换回旧版本而**整段没执行**，CHECKS 会变小，但脚本照旧打印"全部通过"，
    #    只是数字变小 —— 不看数字就发现不了。本项目真发生过一次：一段整段未
    #    执行，报告"全部 46 项通过"，而当时基准是 61 项。
    #    `CHECKS + len(SKIPPED)` 恒定等于下面的常数，且**与形态无关**：发布形态下
    #    被外部依赖挡掉的那两项会计入 SKIPPED，所以两者相加仍然相等。
    #    新增/删除 check 时必须同步这个数字。
    #      [1]~[8] + [9] config_flow 18 项 = 79
    #      + [10] button 平台 21 项        = 100
    #      + [11] services.yaml 4 项       = 104
    #      + [12] AC 码库 + climate 36 项  = 140
    expected_total = 140
    seen = CHECKS + len(SKIPPED)
    if seen != expected_total:
        FAILURES.append(
            f"自检项数不符：实际执行 {CHECKS} + 跳过 {len(SKIPPED)} = {seen} 项，"
            f"基准 {expected_total} 项 —— 有 check 段被静默跳过，结果不可信"
        )

    print("\n" + "=" * 68)
    if SKIPPED:
        print(f"跳过 {len(SKIPPED)} 项（不影响其余结论）：")
        for item in SKIPPED:
            print(f"  ~ {item}")
    if FAILURES:
        print(f"结果：{len(FAILURES)}/{CHECKS} 项失败")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    suffix = f"，跳过 {len(SKIPPED)} 项" if SKIPPED else ""
    print(f"结果：全部 {CHECKS} 项通过{suffix}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
