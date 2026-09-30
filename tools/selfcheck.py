#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IR Hub 自检：不依赖 Home Assistant，可在开发机直接跑。

    C:/Users/ye_ca/.workbuddy/binaries/python/envs/default/Scripts/python.exe tools/selfcheck.py

逐段断言码库、翻译、配置流程、匹配引擎、发码路径的行为；正常时末行是「结果：全部 N 项通过」。
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
# 用户实测能开关电视的那份码（HA 侧 script），用来把"本集成的输出"锚在已验证数据上。
PROVEN_CODE_YAML = os.path.join(os.path.dirname(ROOT), "ir-remote", "ha", "tcl-tv.yaml")

# 单段时长的合理上限：ESPHome base64url 路径硬性限制 500 ms，超出基本可判定数据坏了。
MAX_SANE_TIMING_US = 500_000

FAILURES: list[str] = []
CHECKS = 0
SKIPPED: list[str] = []


def _first_int_array(node) -> list[int] | None:
    """在任意嵌套的 YAML 里找出第一个整数序列（长度 > 10 即视作时序数组）。"""
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
    """给 library.py / ir_command.py 等建一个包外壳，好让它们的相对 import 成立。

    __init__ 会 import homeassistant（本机没有），所以只挂需要的子模块、不走真包。
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

    # 翻译键要和 config_flow.py 实际用到的 step / abort reason 对齐。
    # 同一个 step_id 会同时出现在两个 flow 里，翻译分别落在 config.step / options.step，
    # 故按 "class IrHubOptionsFlow" 切成两段统计。
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

    # error 键同理：`_learn_capture_error` 返回的每个键都必须在翻译里存在 ——
    # 少一个，用户在表单顶部看到的就是**原始英文键名**（例如 receiver_unavailable），
    # 比不报错还难懂。0 帧的三种病因（订阅失败 / 实体不在 / 真没信号）就靠这条兜住。
    _seq = src.split("def _learn_capture_error", 1)
    code_errors = (
        {
            chunk.split('"', 1)[0]
            # 只取这一个方法的函数体（到下一个方法定义为止），否则会把后面
            # _learn_capture_report 之类的 return "..." 也算进来
            for chunk in _seq[1].split("\n    def ", 1)[0].split('return "')[1:]
        }
        if len(_seq) > 1
        else set()
    )
    check(
        len(code_errors) == 3,
        f"_learn_capture_error 解析出 3 种 0 帧病因 {sorted(code_errors)}",
    )
    missing_error_keys: list[str] = []

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
        # data 键要和 schema 里的 CONF_* 名一致（拼错表单会显示原始键名）
        check(
            set(cfg.get("step", {}).get("user", {}).get("data", {}))
            == {"tx_type", "mode"},
            f"{lang}: user 步 data 键 = tx_type/mode",
        )
        check(
            set(cfg.get("step", {}).get("tx", {}).get("data", {}))
            == {"tx_target", "category"},
            f"{lang}: tx 步 data 键 = tx_target/category",
        )
        check(
            set(cfg.get("step", {}).get("learn_setup", {}).get("data", {}))
            == {"tx_target", "receiver", "category"},
            f"{lang}: learn_setup 步 data 键 = tx_target/receiver/category",
        )
        check(
            cfg.get("step", {}).get("learn_press", {}).get("data", {}) == {}
            and set(cfg.get("step", {}).get("learn_press2", {}).get("data", {}))
            == {"learn_action"}
            and set(cfg.get("step", {}).get("learn_pick", {}).get("data", {}))
            == {"device"}
            and set(cfg.get("step", {}).get("learn_test", {}).get("data", {}))
            == {"test_result"},
            f"{lang}: 学习步 data 键对齐（press 无 / press2 learn_action / "
            f"pick device / test test_result）",
        )
        check(
            set(
                blob.get("options", {}).get("step", {}).get("init", {}).get("data", {})
            )
            == {
                "carrier",
                "repeats",
                "tx_delay",
                "tx_type",
                "tx_target",
                "mqtt_format",
                "temperature_sensor",
                "humidity_sensor",
                "power_sensor",
            },
            f"{lang}: options 步 data 键 = 载波/次数/延迟/通道/目标/mqtt格式/温湿度功率传感器",
        )
        # options 的 error 键（发射目标校验失败时用户看到的不是原始英文键名）
        check(
            {"target_missing", "mqtt_missing"}
            <= set(blob.get("options", {}).get("error", {})),
            f"{lang}: options.error 覆盖 target_missing / mqtt_missing",
        )
        missing_error_keys += [
            f"{lang}.{key}"
            for key in sorted(code_errors - set(cfg.get("error", {})))
        ]

    check(
        not missing_error_keys,
        f"每个语言文件都覆盖 0 帧的三种 error 键（{sorted(code_errors)}）",
        f"缺 {missing_error_keys}",
    )


def check_learn_match() -> None:
    """学习匹配引擎（learn_match.py）的核心不变式。

    每条都对应一个踩过的坑：回退时只表现为指标变差、不易察觉，故用确定性断言钉死。
    （统计口径的护栏见 tools/verify_learn_match.py。）
    """
    print("\n[13] 学习匹配引擎（learn_match.py）不变式")
    learn = load_module(
        "_ir_hub_shim.learn_match", os.path.join(COMPONENT, "learn_match.py")
    )
    library_mod = load_module(
        "_ir_hub_shim.library", os.path.join(COMPONENT, "library.py")
    )
    library = library_mod.CodeLibrary()
    normalize = learn.normalize_capture
    sim = learn._similarity

    # ① 符号规整：HA 的 InfraredReceivedSignal.timings 可能给全正，也可能给交替符号
    check(
        normalize([9000, 4500, 560, 1690, 560, 560, 560, 560])
        == [9000, -4500, 560, -1690, 560, -560, 560, -560],
        "normalize_capture：全正数组 -> 偶正奇负",
    )
    check(
        normalize([9000, -4500, 560, -1690, 560, -560, 560, -560])
        == normalize([9000, 4500, 560, 1690, 560, 560, 560, 560]),
        "normalize_capture：带符号与全正输入结果一致（幂等）",
    )
    check(
        normalize(None) is None
        and normalize([9000, 4500, 0, 1690]) is None
        and normalize([9000, 4500, 560, 1690]) is None,
        "normalize_capture：空 / 太短(<8) / 含 0 -> None（0 会让奇偶错位）",
    )

    frame = library.get_timings(47, library.key_names(library.get_device(47))[0])
    check(abs(sim(frame, frame) - 1.0) < 1e-9, "自比 = 1.0")

    # ② 丢前导码后仍须对齐（缺"库去头"方向的匹配，真帧会崩到 ~0.75 而被错帧挤下）
    dropped = sim(frame[1:], frame)
    check(
        dropped >= 0.99,
        "丢 1 个前导元素仍 ≥0.99（缺『库去头』对齐方向时真帧会崩到 ~0.75）",
        f"实际 {dropped:.3f}",
    )

    # ③ 分数不许饱和（否则容差内的帧全给 1.0，top_n 退化为随机截断）
    near = list(frame)
    for i, value in enumerate(near):
        if value < -800:
            near[i] = int(value * 0.4)
            break
    s_near = sim(near, frame)
    check(
        s_near < 0.95,
        "只差 1 个 bit 的帧分数明显低于真帧（避免饱和在 1.0）",
        f"实际 {s_near:.3f}",
    )

    # ④ match_timings 端到端：真帧在无抖动输入下必须排第 1
    category = 2
    sampled = matched = 0
    for device in library.devices:
        if device["category"] != category:
            continue
        if sampled >= 8:
            break
        keys = library.key_names(device)
        if not keys:
            continue
        timings = library.get_timings(device["id"], keys[0])
        if not timings or len(timings) < 8:
            continue
        # sampled 必须在所有 continue 之后自增：把取不到键/时序不可用的设备
        # 算进分母会造出假失败。
        sampled += 1
        frames = learn.match_timings(library, category, timings, top_n=1)
        if frames and learn.frame_signature(frames[0]["frame"]) == learn.frame_signature(
            timings
        ):
            matched += 1
    check(
        sampled > 3 and matched == sampled,
        f"match_timings：真帧全部排第 1（{matched}/{sampled}，电视机大类）",
    )

    # ⑤ 接收端把**重复发送**合并成一帧时的容错（2026-09-30 定位的真实故障）。
    #    `remote_receiver` 的 "Signal is done after 10000 us" 只按静默切帧，
    #    间隔 <10 ms 的重复会被粘成一整帧 ⇒ 同一次按键可能 200 段也可能 300 段，
    #    而码库存固定遍数。帧长预筛只容差 ±3 段，不加 burst 前缀就**整库被灭**，
    #    症状与"库外遥控"一模一样（实测：300 段原样 → 0.632 判"不在库"；
    #    截到 200 段 → 0.866 命中真型号，且两个 burst 块与库帧逐位相同）。
    merged_frame = frame + frame[2:52]
    prefixes = learn.burst_prefixes(merged_frame)
    check(
        frame in prefixes and all(len(p) >= learn._MIN_VARIANT_LEN for p in prefixes),
        "burst_prefixes：能在合并帧的内部 gap 处截断出原帧（前缀不短于最小长度）",
        f"前缀段数 {[len(p) for p in prefixes]}",
    )
    check(
        learn.capture_candidates(merged_frame)[:2] == [merged_frame, merged_frame[1:]],
        "capture_candidates：前两项仍是『原帧 / 丢首元素』（向后兼容，不掉旧档）",
    )
    merged_top = learn.match_timings(library, category, merged_frame, top_n=1)
    check(
        bool(merged_top)
        and learn.frame_signature(merged_top[0]["frame"]) == learn.frame_signature(frame),
        "match_timings：合并过重复块的捕获仍排第 1（未修时被帧长预筛整库灭掉）",
        f"最高分 {merged_top[0]['score']:.3f}" if merged_top else "无候选",
    )


# ------------------------------------------------------ 3./4./5. 码库 / 不变式 / 统计
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
    # 打包器只在开发树里（且依赖 irext sqlite 源库），独立发布的本集成不带它
    # ⇒ 找不到就跳过逐字节往返，其余不变量照查（它们只看集成自己的数据）。
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

    # 锚点 1：本机 TCL 电视（device 47），钉住长度与开头两值
    anchor = library.get_timings(47, "power")
    check(
        anchor is not None and len(anchor) == 156 and anchor[0] == 3963 and anchor[1] == -3985,
        "回归锚点 device 47 'power' = 156 项 / 首值 3963 / 次值 -3985",
        f"实际 {len(anchor) if anchor else None} 项，前 2 值 {(anchor[:2] if anchor else None)}",
    )

    # 锚点 2：与"用户实测可开关电视"的那份码逐项比对，把集成输出锚在已验证有效的
    # 字节上 —— 部署后若电视无响应，即可排除取码问题，只剩传输路径一个变量。
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
    """用 stub 顶掉 infrared_protocols，单测发码路径（parse / build / 拒收分支）。"""
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

    # 默认值防御：carrier=0 回落 38000，repeats=0 视为 1
    clamped = mod.build_raw_command([100, -200], carrier=0, repeats=0)
    check(
        clamped.get_raw_timings() == [100, -200] and clamped.modulation == 38000,
        "carrier=0 回落默认 38000；repeats=0 视为 1",
    )

    # 拒收分支：全正数组 / 空数组
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
    """造一套最小的 `homeassistant.*` 外壳，让 remote/climate 等能在无 HA 的机器上 import。

    有 `from __future__ import annotations`，类型注解不求值，只需类名/常量/基类存在。
    返回 `HomeAssistantError` 供断言用。
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

        def async_on_remove(self, func) -> None:
            # 真 HA 里是 Entity.async_on_remove：登记"实体移除时一并注销"的回调
            self.__dict__.setdefault("_stub_on_remove", []).append(func)

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

    # `homeassistant.util.slugify` 真身是 python-slugify（+ Unidecode）的薄封装。
    util = mod("homeassistant.util")

    def _slugify(value, separator: str = "_") -> str:
        """优先用真 python-slugify；没装则退化为等价简化实现。

        键名都是纯 ASCII（power / vol+ / up …），两套实现对它们结果一致，
        所以缺依赖时这条测试依然有效。
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

    # `homeassistant.const`（`__init__.py` 从这里 import Platform）
    ha_const = mod("homeassistant.const")

    class Platform:
        REMOTE = "remote"
        BUTTON = "button"
        CLIMATE = "climate"
        SENSOR = "sensor"

    ha_const.Platform = Platform
    ha_const.ATTR_TEMPERATURE = "temperature"
    # SmartAC 参照实现（re/smartac/controller.py 差分对拍用）
    ha_const.ATTR_ENTITY_ID = "entity_id"
    # 可选传感器（climate 的功率/温湿度订阅用）
    ha_const.STATE_ON = "on"
    ha_const.STATE_OFF = "off"
    ha_const.STATE_UNKNOWN = "unknown"
    ha_const.STATE_UNAVAILABLE = "unavailable"

    # climate 的传感器订阅：只记账，不真挂总线。测试里直接调回调（带
    # `{"new_state": ..., "old_state": ...}` 的假 event）来驱动状态机。
    event_mod = mod("homeassistant.helpers.event")

    def _stub_track_state_change_event(hass, entity_ids, action):
        event_mod._STUB_SUBS.append((tuple(entity_ids), action))
        return lambda: None

    event_mod._STUB_SUBS = []
    event_mod.async_track_state_change_event = _stub_track_state_change_event

    # ServiceCall 只被 `__init__.py` 用作类型注解，名字存在即可（不会求值）。
    class ServiceCall:
        def __init__(self) -> None:
            self.data: dict = {}
            self.context = None

    core.ServiceCall = ServiceCall

    # `__init__.py` 把 config_validation import 成 cv，用到 entity_id / positive_int
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
    ha.helpers.event = event_mod
    return HomeAssistantError


def check_remote_entity() -> None:
    """用 stub 跑 `remote.py` 的真实逻辑：发码、重复次数、裸时序、错误分支。

    无硬件也能测，且跑的正是上线后会执行的代码。
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

        # --- 裸字符串（直接调本方法时服务 schema 不生效）---
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

        # --- 未知键：应抛清晰错误，而非静默失败 ---
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

    `ConfigFlow.__init_subclass__` 必须收 `domain=`，因为 flow 声明是
    `class IrHubConfigFlow(config_entries.ConfigFlow, domain=DOMAIN)`。
    """
    config_entries = sys.modules["homeassistant.config_entries"]
    infrared = sys.modules["homeassistant.components.infrared"]

    class ConfigFlow:
        VERSION = 1
        # 放类属性：IrHubConfigFlow.__init__ 没调 super().__init__()，实例上没有
        # 这些属性，放类级才能在"某个 step 没被调用过"时安全读取。
        shown = None
        created = None
        aborted = None

        def __init_subclass__(cls, *, domain=None, **kwargs):
            super().__init_subclass__(**kwargs)
            if domain is not None:
                cls.domain = domain

        def __init__(self):
            self.hass = None

        # 这三个在真 HA 里是协程，但 config_flow 写的是 `return self.async_show_form(...)`、
        # 不 await ⇒ stub 必须做成同步方法，否则返回的协程不执行、断言永远看到 None。
        # 这里测的是 config_flow 的分支逻辑，不是 HA 的协程契约，同步即可。
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

    # 实测步会直接调 infrared.async_send_command(...)；记下调用供 test 步断言确实发了。
    infrared.STUB_SENT = []

    async def _stub_async_send_command(hass, emitter, command, **kwargs):
        infrared.STUB_SENT.append((emitter, command))

    infrared.async_send_command = _stub_async_send_command

    # ---- 学习模式（遥控器配对）需要的接收端 stub ----
    # config_flow 用 hasattr 探测这两个 API（老版本 HA 只有发射端），必须在
    # load_module(config_flow) 之前挂上，否则学习模式会被判成"HA 不支持"而 abort。
    infrared.STUB_RECEIVERS = []
    infrared.async_get_receivers = lambda hass: list(infrared.STUB_RECEIVERS)

    infrared.STUB_SUBSCRIBERS = []      # [(entity_id, callback)]
    infrared.STUB_EMIT_RECEIVED = None  # 由下面的闭包赋值
    infrared.STUB_SUBSCRIBE_ERROR = None  # 设成异常实例则订阅时抛错（测"订阅失败"路径）

    def _stub_subscribe_receiver(hass, entity_id, callback):
        if infrared.STUB_SUBSCRIBE_ERROR is not None:
            raise infrared.STUB_SUBSCRIBE_ERROR
        subscriber = (entity_id, callback)
        infrared.STUB_SUBSCRIBERS.append(subscriber)

        def _unsubscribe():
            try:
                infrared.STUB_SUBSCRIBERS.remove(subscriber)
            except ValueError:
                pass

        return _unsubscribe

    infrared.async_subscribe_receiver = _stub_subscribe_receiver

    def _stub_emit_received(entity_id, timings, modulation=None):
        """模拟接收器收到一帧：把信号喂给所有订阅了该接收器的回调。"""
        signal = types.SimpleNamespace(timings=timings, modulation=modulation)
        for subscribed_id, callback in list(infrared.STUB_SUBSCRIBERS):
            if subscribed_id == entity_id:
                callback(signal)

    infrared.STUB_EMIT_RECEIVED = _stub_emit_received


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

    voluptuous 把 default= 包进 default_factory：`marker.default` 是
    `lambda: 38000` 而非 38000 ⇒ 必须调用一次。
    """
    out = {}
    for marker in schema.schema:
        key = getattr(marker, "schema", marker)
        default = getattr(marker, "default", None)
        if callable(default):
            default = default()
        out[key] = default
    return out


def check_tx() -> None:
    """发射通道抽象：Broadlink 打包与 SmartAC 参照实现逐字节对拍 + 各通道路由。

    SmartAC（re/smartac，MIT）是兼容性锚点：Broadlink 包必须与它的 raw2broadlink
    输出完全一致，现成遥控宝才直接可用。re/ 副本缺失时该项自动跳过。
    """
    import asyncio
    import base64 as b64mod
    import json as jsonmod
    import random
    import types

    print("\n[tx] 发射通道抽象（broadlink 对拍 / esphome / mqtt / infrared）")
    _install_protocols_stub()
    _install_ha_stubs()
    _install_config_flow_stubs()

    tx_mod = load_module(
        "_ir_hub_shim.transmitter", os.path.join(COMPONENT, "transmitter.py")
    )
    infrared = sys.modules["homeassistant.components.infrared"]
    EMITTER = "infrared.ir_control_ir_transmitter"

    class RecServices:
        def __init__(self, have: set | None = None):
            self.calls: list[tuple] = []
            self._have = have or set()

        def has_service(self, domain: str, service: str) -> bool:
            return (domain, service) in self._have

        async def async_call(self, domain, service, data=None, **kwargs):
            self.calls.append((domain, service, data))

    class FakeStates:
        def __init__(self, ids: list[str]):
            self._ids = ids

        def async_all(self, domain: str):
            return [
                types.SimpleNamespace(entity_id=e, domain=e.split(".")[0])
                for e in self._ids
                if e.split(".")[0] == domain
            ]

        def get(self, entity_id: str):
            for e in self._ids:
                if e == entity_id:
                    return types.SimpleNamespace(entity_id=e, domain=e.split(".")[0])
            return None

    def make_hass(have: set | None = None, state_ids: list[str] | None = None):
        return types.SimpleNamespace(
            services=RecServices(have), states=FakeStates(state_ids or [])
        )

    async def run():
        # --- 1. Broadlink 打包：与 SmartAC raw2broadlink 逐字节对拍 ---
        smartac_path = os.path.join(ROOT, "re", "smartac", "controller.py")
        if os.path.exists(smartac_path):
            smartac = load_module("_smartac_controller_ref", smartac_path)
            rng = random.Random(20260928)
            for trial in range(5):
                pulses = [rng.randrange(200, 20000) for _ in range(rng.randrange(30, 200))]
                ours = tx_mod.raw_to_broadlink_packet(pulses)
                theirs = smartac.BroadlinkController.raw2broadlink(None, pulses)
                check(
                    ours == theirs,
                    f"broadlink 包与 SmartAC 参照逐字节一致（样本 {trial + 1}，"
                    f"{len(pulses)} 脉冲 / {len(ours)} 字节）",
                )
        else:
            skip("broadlink 对拍", "re/smartac 参照副本缺失")

        # 包结构：头 0x26 0x00 + 小端长度 + 0x0d 0x05（其后是 AES 补零）+ 16 字节对齐
        packet = tx_mod.raw_to_broadlink_packet([9000, 4500] * 40)
        array_len = int.from_bytes(packet[2:4], "little")
        check(
            packet[0:2] == b"\x26\x00"
            and packet[4 + array_len : 6 + array_len] == b"\x0d\x05"
            and (len(packet) + 4) % 16 == 0,
            f"broadlink 包结构：0x26 头 / 小端长度 / 0x0d05 在补零前（{len(packet)} 字节）",
        )

        # --- 2. infrared 通道：走 infrared.async_send_command ---
        hass = make_hass()
        infrared.STUB_SENT.clear()
        await tx_mod.async_send_timings(
            hass, "infrared", EMITTER, [1000, -500, 300, -400], carrier=38000
        )
        check(
            len(infrared.STUB_SENT) == 1
            and infrared.STUB_SENT[0][0] == EMITTER
            and infrared.STUB_SENT[0][1].get_raw_timings() == [1000, -500, 300, -400]
            and infrared.STUB_SENT[0][1].modulation == 38000,
            "infrared 通道 -> infrared.async_send_command（时序/载波原样）",
        )

        # --- 3. esphome 通道：服务名去前缀 + command 带符号透传 ---
        hass = make_hass()
        await tx_mod.async_send_timings(
            hass,
            "esphome",
            "esphome.ir_control_send_raw_command",
            [1000, -500, 300, -400],
            carrier=38000,
        )
        check(
            hass.services.calls == [
                ("esphome", "ir_control_send_raw_command",
                 {"command": [1000, -500, 300, -400]})
            ],
            "esphome 通道 -> 服务名去 esphome. 前缀，command 带符号透传（SmartAC 契约）",
            f"实际 {hass.services.calls!r}",
        )

        # --- 4. broadlink 通道：b64 包 + delay_secs ---
        hass = make_hass()
        await tx_mod.async_send_timings(
            hass, "broadlink", "remote.bl", [9000, 4500, 560, -560], carrier=38000
        )
        call = hass.services.calls[0]
        b64 = call[2]["command"][0]
        check(
            call[0] == "remote"
            and call[1] == "send_command"
            and call[2]["entity_id"] == "remote.bl"
            and call[2]["delay_secs"] == 0.5
            and b64.startswith("b64:")
            and b64mod.b64decode(b64[4:]) == tx_mod.raw_to_broadlink_packet([9000, 4500, 560, 560]),
            "broadlink 通道 -> remote.send_command（b64 包 = 全正脉冲打包，delay 0.5）",
        )

        # --- 5. mqtt 通道：默认 SmartAC 裸数组；tasmota 格式可选 ---
        hass = make_hass()
        await tx_mod.async_send_timings(
            hass, "mqtt", "tcl_ir/ir_send", [1000, -500, 300, -400], carrier=38000
        )
        domain, service, data = hass.services.calls[0]
        check(
            (domain, service) == ("mqtt", "publish")
            and data["topic"] == "tcl_ir/ir_send"
            and jsonmod.loads(data["payload"]) == [1000, 500, 300, 400],
            "mqtt 通道默认 = SmartAC 裸全正数组（tcl-ir 桥接固件的契约）",
            f"实际 {data.get('payload')!r}",
        )

        hass = make_hass()
        await tx_mod.async_send_timings(
            hass, "mqtt", "tasmota_ir/cmnd/ir", [1000, -500, 300, -400],
            carrier=38000, mqtt_format="tasmota",
        )
        domain, service, data = hass.services.calls[0]
        payload = jsonmod.loads(data["payload"])
        check(
            (domain, service) == ("mqtt", "publish")
            and data["topic"] == "tasmota_ir/cmnd/ir"
            and payload == {
                "Protocol": "RAW", "Bits": 0,
                "Raw": "1000,500,300,400", "Frequency": 38000,
            },
            "mqtt 通道 tasmota 格式 -> IRMQTTServer RAW JSON（正值序列 + 载波）",
            f"实际 {payload!r}",
        )

        # --- 6. 未知通道 -> HomeAssistantError ---
        from homeassistant.exceptions import HomeAssistantError

        try:
            await tx_mod.async_send_timings(
                make_hass(), "carrier_pigeon", "x", [1, -1], carrier=38000,
            )
        except HomeAssistantError:
            check(True, "未知通道 -> HomeAssistantError")
        else:
            check(False, "未知通道 -> HomeAssistantError", "竟然没抛")

        # --- 7. async_validate_target：四通道 ---
        hass = make_hass(
            have={("esphome", "ir_control_send_raw_command"), ("mqtt", "publish")},
            state_ids=["remote.broadlink_livingroom", "light.x"],
        )
        infrared.STUB_EMITTERS = [EMITTER]
        check(
            await tx_mod.async_validate_target(hass, "infrared", EMITTER) is None
            and await tx_mod.async_validate_target(hass, "infrared", "infrared.ghost")
            == "target_missing",
            "validate infrared：存在通过 / 不存在 target_missing",
        )
        check(
            await tx_mod.async_validate_target(
                hass, "esphome", "esphome.ir_control_send_raw_command"
            )
            is None
            and await tx_mod.async_validate_target(hass, "esphome", "esphome.ghost")
            == "target_missing",
            "validate esphome：服务存在通过 / 不存在 target_missing",
        )
        check(
            await tx_mod.async_validate_target(hass, "broadlink", "remote.broadlink_livingroom") is None
            and await tx_mod.async_validate_target(hass, "broadlink", "light.x")
            == "target_missing",
            "validate broadlink：remote 实体通过 / 非 remote target_missing",
        )
        check(
            await tx_mod.async_validate_target(hass, "mqtt", "tasmota/cmnd/ir") is None
            and await tx_mod.async_validate_target(
                make_hass(), "mqtt", "tasmota/cmnd/ir"
            )
            == "mqtt_missing",
            "validate mqtt：服务在通过 / 未加载 mqtt_missing",
        )

    asyncio.run(run())


def check_config_flow() -> None:
    """跑 `config_flow.py` 的真实逻辑（真 voluptuous + stub homeassistant）。

    刻意用真 voluptuous：顺带验证「vol.In(字典) 提交回来的是 key 字符串」这个假设 ——
    config_flow 紧接着 int(user_input[...])，假设错了就全盘崩。
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

    class FakeState:
        def __init__(self, entity_id: str, **attributes) -> None:
            self.entity_id = entity_id
            self.domain = entity_id.split(".")[0]
            self.state = attributes.pop("state", "unknown")
            self.attributes = attributes

    class FakeStates:
        # 用例里现挂的状态（如红外接收器）。`get()` 每次现查这张表，所以可以
        # 先注册状态、再 `new_flow()`。
        extra: dict[str, "FakeState"] = {}

        def __init__(self) -> None:
            self._all = [
                FakeState("remote.broadlink_livingroom"),
                FakeState("remote.broadlink_bedroom"),
                FakeState("light.not_a_remote"),
            ]

        def async_all(self, domain: str):
            return [s for s in self._all if s.domain == domain]

        def get(self, entity_id: str):
            if entity_id in FakeStates.extra:
                return FakeStates.extra[entity_id]
            for state in self._all:
                if state.entity_id == entity_id:
                    return state
            return None

    class FakeServices:
        def __init__(self) -> None:
            self.calls: list[tuple] = []
            self._have = {
                ("esphome", "ir_control_send_raw_command"),
                ("remote", "send_command"),
            }

        def has_service(self, domain: str, service: str) -> bool:
            return (domain, service) in self._have

        async def async_call(self, domain, service, data=None, **kwargs):
            self.calls.append((domain, service, data))

    class FakeHass:
        def __init__(self) -> None:
            self.states = FakeStates()
            self.services = FakeServices()

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
        # ① 无 emitter -> infrared 通道在 tx 步 abort（提示先去配 ESPHome）
        infrared.STUB_EMITTERS = []
        flow = new_flow()
        await flow.async_step_user()
        check(flow.shown.get("step_id") == "user", "第 1 步 = 选发射通道（无 emitter 也出表单）")
        await flow.async_step_user({"tx_type": "infrared"})
        check(flow.aborted == "no_emitters", "infrared 通道无 emitter -> abort(no_emitters)")

        # ② 有 emitter -> user 出通道下拉
        infrared.STUB_EMITTERS = [EMITTER]
        flow = new_flow()
        await flow.async_step_user()
        check(flow.shown["step_id"] == "user", "第 1 步 step_id = user")
        tx_opts = _schema_options(flow.shown["data_schema"], "tx_type") or {}
        check(
            list(tx_opts) == ["infrared", "broadlink", "esphome", "mqtt"],
            f"user 步给出 4 种发射通道（{list(tx_opts)}）",
        )

        # ③ 真 voluptuous 校验：确认 vol.In(字典) 提交回来的是 key 字符串
        validated = flow.shown["data_schema"]({"tx_type": "infrared"})
        check(validated["tx_type"] == "infrared", "vol.In(字典) 返回 key 字符串")

        # ④ infrared 通道 -> tx 步（目标下拉 + 大类下拉）
        flow = new_flow()
        await flow.async_step_user({"tx_type": "infrared"})
        check(flow.shown.get("step_id") == "tx", "选 infrared -> 进 tx 步")
        schema = flow.shown["data_schema"]
        check(
            _schema_options(schema, "tx_target") == {EMITTER: None},
            "tx 步 emitter 下拉列出全部可用发射器",
        )
        cats = _schema_options(schema, "category") or {}
        check(
            "电视机" in cats.get("2", "") and "空调" in cats.get("ac", ""),
            f"category 下拉框给出中文类别名 + AC 特殊项（{len(cats)} 项）",
            f"2 -> {cats.get('2')!r}, ac -> {cats.get('ac')!r}",
        )
        validated = schema({"tx_target": EMITTER, "category": "2"})
        check(
            validated["category"] == "2" and validated["tx_target"] == EMITTER,
            "vol.In(字典) 返回 key 字符串（后续 int() 转换才成立）",
            f"实际 {validated!r}",
        )

        # ⑤ 提交 tx -> 进 brand 步
        await flow.async_step_tx({"tx_target": EMITTER, "category": "2"})
        check(flow.shown.get("step_id") == "brand", "选完通道+大类 -> 进 brand 步")
        brands = _schema_options(flow.shown["data_schema"], "brand") or {}
        check(
            len(brands) > 10 and all(k.isdigit() for k in brands),
            f"brand 步给出 {len(brands)} 个品牌（key 为数字字符串）",
        )

        # ⑤-b 选品牌 -> 进 device 步
        await flow.async_step_brand({"brand": "14"})
        check(flow.shown.get("step_id") == "device", "选完品牌 -> 进 device 步")
        devices = _schema_options(flow.shown["data_schema"], "device") or {}
        label47 = devices.get("47", "")
        check("TCL电视-1" in label47, "device 步列出 TCL电视-1", f"实际 {label47!r}")
        check("RCA" in label47, f"型号附 irext 协议提示 -> {label47!r}")

        # ⑥ 选型号 -> 进 test 步（发实测码，不直接建 entry）
        flow = new_flow()
        flow._tx_type = "infrared"
        flow._tx_target = EMITTER
        flow._category = 2
        flow._brand = 14
        await flow.async_step_device({"device": "47"})
        check(flow.shown.get("step_id") == "test", "选完型号 -> 进 test 步（实测确认）")
        check(
            47 in flow._key_candidates
            and flow._key_index == flow._key_candidates.index(47),
            "device 步记下品牌内全部候选与当前位次（『自动试下一个』用）",
        )
        check(
            len(infrared.STUB_SENT) == 1 and infrared.STUB_SENT[0][0] == EMITTER,
            "test 步立即经所选 emitter 发出一帧测试码",
            f"实际 {infrared.STUB_SENT!r}",
        )
        cmd = infrared.STUB_SENT[0][1]
        check(
            getattr(cmd, "modulation", None) == 38000
            and len(cmd.get_raw_timings()) >= 2
            and any(t < 0 for t in cmd.get_raw_timings()),
            "测试码 = 38 kHz 载波 + 非空带符号时序",
            f"实际 modulation={getattr(cmd, 'modulation', None)}, "
            f"n={len(cmd.get_raw_timings())}",
        )
        opts = _schema_options(flow.shown["data_schema"], "test_result") or {}
        check(
            list(opts) == ["ok", "next", "retry", "skip"],
            f"test 步下拉 = ok/next/retry/skip（{opts}）",
        )
        check(
            flow.shown["description_placeholders"].get("model") == "TCL电视-1"
            and "index" in flow.shown["description_placeholders"],
            "test 步占位符带型号与进度（index/total）",
        )

        # ⑥-a2 没反应 -> next 自动前进到品牌内下一个型号
        devs14 = flow._key_candidates
        await flow.async_step_test({"test_result": "next"})
        if len(devs14) > 1:
            expect_next = devs14[devs14.index(47) + 1]
            check(
                flow.shown.get("step_id") == "test" and flow._device_id == expect_next,
                f"『自动试下一个』前进到品牌内下一型号（{expect_next}）",
            )
        else:
            check(
                flow.aborted == "test_exhausted",
                "『自动试下一个』无更多候选 -> abort(test_exhausted)",
            )

        # ⑥-b 没反应 -> retry 退回 device 步重选
        await flow.async_step_test({"test_result": "retry"})
        check(flow.shown.get("step_id") == "device", "选『没反应』-> 退回 device 步重选")

        # ⑥-c 重选后确认有反应 -> 建 entry
        await flow.async_step_device({"device": "47"})
        infrared.STUB_SENT.clear()
        await flow.async_step_test({"test_result": "ok"})
        created = flow.created
        check(
            created["title"] == "TCL电视-1",
            f"entry title 不重复品牌前缀（{created['title']!r}，不是 'TCL TCL电视-1'）",
        )
        data = created["data"]
        check(
            data["emitter"] == EMITTER
            and data["tx_type"] == "infrared"
            and data["tx_target"] == EMITTER
            and data["device"] == 47
            and data["category"] == 2
            and data["brand"] == 14
            and data["carrier"] == 38000
            and data["repeats"] == 1,
            "entry data 完整（tx 通道 + emitter 兼容别名 + category/brand/device/carrier/repeats）",
            f"实际 {data!r}",
        )

        # ⑧ 其它发射通道：esphome / broadlink / mqtt 的 config flow 分支
        f2 = new_flow()
        await f2.async_step_user({"tx_type": "esphome"})
        check(f2.shown.get("step_id") == "tx", "选 esphome -> 进 tx 步")
        await f2.async_step_tx({"tx_target": "esphome.no_such_action", "category": "2"})
        check(
            (f2.shown.get("errors") or {}).get("base") == "target_missing",
            "esphome 动作不存在 -> errors.target_missing",
        )
        await f2.async_step_tx(
            {"tx_target": "esphome.ir_control_send_raw_command", "category": "2"}
        )
        check(f2.shown.get("step_id") == "brand", "esphome 动作存在 -> 进 brand 步")

        f3 = new_flow()
        await f3.async_step_user({"tx_type": "broadlink"})
        bl_opts = _schema_options(f3.shown["data_schema"], "tx_target") or {}
        check(
            "remote.broadlink_livingroom" in bl_opts
            and "light.not_a_remote" not in bl_opts,
            f"broadlink 通道下拉只列 remote 域实体（{sorted(bl_opts)}）",
        )
        await f3.async_step_tx({"tx_target": "remote.broadlink_livingroom", "category": "2"})
        check(f3.shown.get("step_id") == "brand", "broadlink 实体存在 -> 进 brand 步")

        f4 = new_flow()
        f4.hass.services.has_service = lambda domain, service: False
        await f4.async_step_user({"tx_type": "mqtt"})
        await f4.async_step_tx({"tx_target": "tasmota_ir/cmnd/ir", "category": "2"})
        check(
            (f4.shown.get("errors") or {}).get("base") == "mqtt_missing",
            "MQTT 未加载 -> errors.mqtt_missing",
        )

        # ⑨ esphome 通道走完 test -> entry data 记 tx 通道且无 emitter 别名
        infrared.STUB_SENT.clear()
        f5 = new_flow()
        f5.hass = new_flow().hass  # 独立 hass（防串扰）
        f5._tx_type = "esphome"
        f5._tx_target = "esphome.ir_control_send_raw_command"
        f5._category = 2
        f5._brand = 14
        await f5.async_step_device({"device": "47"})
        await f5.async_step_test({"test_result": "ok"})
        d5 = f5.created["data"]
        check(
            d5["tx_type"] == "esphome"
            and d5["tx_target"] == "esphome.ir_control_send_raw_command"
            and "emitter" not in d5,
            "esphome 通道 entry 只记 tx 字段（不写 emitter 别名）",
        )

        # ⑩ 学习模式（遥控器配对）：user(mode=learn) → … → 建 entry。
        #    用真码库 + 真设备 ID（47 = TCL电视-1）跑完整链路，含真实的匹配一遍。
        RECEIVER = "infrared.hjy_ir_receiver"

        infrared.STUB_RECEIVERS = []
        fl = new_flow()
        await fl.async_step_user()
        mode_opts = _schema_options(fl.shown["data_schema"], "mode") or {}
        check(
            list(mode_opts) == ["browse", "learn"],
            f"第 1 步给出配置方式下拉（{list(mode_opts)}）",
        )
        await fl.async_step_user({"tx_type": "infrared", "mode": "learn"})
        check(fl.aborted == "no_receivers", "learn 模式但无接收器 -> abort(no_receivers)")

        # 老版本 HA 无接收端 API 时须给出明确 abort，而不是 AttributeError
        fl_no_api = new_flow()
        saved_flag = flow_mod._HAS_RECEIVER_API
        flow_mod._HAS_RECEIVER_API = False
        try:
            await fl_no_api.async_step_learn_setup()
            check(
                fl_no_api.aborted == "receiver_api_missing",
                "HA 无接收端 API -> abort(receiver_api_missing)（不是崩栈）",
            )
        finally:
            flow_mod._HAS_RECEIVER_API = saved_flag

        infrared.STUB_RECEIVERS = [RECEIVER]
        # 接收器实体挂上 friendly_name / state：下拉要显示设备名，learn_press 要显示状态
        FakeStates.extra = {
            RECEIVER: FakeState(RECEIVER, friendly_name="HJY IR 红外接收")
        }
        fl = new_flow()
        await fl.async_step_user({"tx_type": "infrared", "mode": "learn"})
        check(fl.shown.get("step_id") == "learn_setup", "learn 模式 -> 进 learn_setup 步")
        ls = fl.shown["data_schema"]
        check(
            _schema_options(ls, "receiver")
            == {RECEIVER: f"HJY IR 红外接收（{RECEIVER}）"},
            "learn_setup 接收器下拉带设备名（多台 IR 设备时唯一能分辨「该按哪一台」的线索）",
            f"实际 {_schema_options(ls, 'receiver')!r}",
        )
        lcats = _schema_options(ls, "category") or {}
        check(
            "2" in lcats and "ac" in lcats and "空调" in lcats.get("ac", ""),
            f"learn 大类下拉含按键类与空调（共 {len(lcats)} 项，ac={lcats.get('ac')!r}）",
        )
        await fl.async_step_learn_setup(
            {"tx_target": EMITTER, "receiver": RECEIVER, "category": "2"}
        )
        check(fl.shown.get("step_id") == "learn_press", "选完接收器+大类 -> 进 learn_press 步")
        check(
            len(infrared.STUB_SUBSCRIBERS) == 1
            and infrared.STUB_SUBSCRIBERS[0][0] == RECEIVER,
            "learn_press 已订阅所选接收器",
        )
        ph_press = fl.shown["description_placeholders"]
        check(
            ph_press.get("receiver") == f"HJY IR 红外接收（{RECEIVER}）"
            # unknown 会被解释成"这台接收器从未收到过任何一帧"（正是"选错设备"的指纹）
            and ph_press.get("receiver_state", "").startswith("unknown")
            and ph_press.get("subscribe_error") == "",
            "learn_press 显示接收器设备名 + 实体状态（帧有没有进 HA 的探针）+ 订阅结果",
            f"实际 receiver={ph_press.get('receiver')!r} "
            f"state={ph_press.get('receiver_state')!r}",
        )

        # 订阅失败必须报 subscribe_failed 并带出原因 —— 报 no_signal（「对准接收器」）
        # 是误导：用户会一直重按遥控器，而根因在实体上。
        infrared.STUB_SUBSCRIBE_ERROR = RuntimeError("receiver_not_found")
        try:
            fl_sub = new_flow()
            await fl_sub.async_step_user({"tx_type": "infrared", "mode": "learn"})
            await fl_sub.async_step_learn_setup(
                {"tx_target": EMITTER, "receiver": RECEIVER, "category": "2"}
            )
            await fl_sub.async_step_learn_press({})
            ph_sub = fl_sub.shown["description_placeholders"]
            check(
                (fl_sub.shown.get("errors") or {}).get("base") == "subscribe_failed"
                and "receiver_not_found" in ph_sub.get("subscribe_error", ""),
                "订阅失败 -> errors.subscribe_failed 且原因带到表单（不再冒充 no_signal）",
                f"实际 errors={fl_sub.shown.get('errors')!r} "
                f"subscribe_error={ph_sub.get('subscribe_error')!r}",
            )
        finally:
            infrared.STUB_SUBSCRIBE_ERROR = None

        # 接收器实体根本不存在（设备被删/改名/换机）-> receiver_unavailable。
        # 这种 0 帧既不是"订阅失败"也不是"你对准了却没按到"，必须单独报，
        # 否则用户会一直重复"对准 + 重按"，方向完全错。
        saved_extra = FakeStates.extra
        saved_subs = list(infrared.STUB_SUBSCRIBERS)
        FakeStates.extra = {}
        try:
            fl_gone = new_flow()
            await fl_gone.async_step_user({"tx_type": "infrared", "mode": "learn"})
            await fl_gone.async_step_learn_setup(
                {"tx_target": EMITTER, "receiver": RECEIVER, "category": "2"}
            )
            await fl_gone.async_step_learn_press({})
            ph_gone = fl_gone.shown["description_placeholders"]
            check(
                (fl_gone.shown.get("errors") or {}).get("base") == "receiver_unavailable"
                and "不存在" in ph_gone.get("receiver_state", ""),
                "接收器实体不存在 -> errors.receiver_unavailable（0 帧的第三种病因）",
                f"实际 errors={fl_gone.shown.get('errors')!r} "
                f"state={ph_gone.get('receiver_state')!r}",
            )
        finally:
            FakeStates.extra = saved_extra
            # 这个探测流程是丢弃的，它订上的接收器不会自己退 ⇒ 复原订阅表，
            # 否则后面「订阅数恰为 1」那条断言会被这里的残留搞红。
            infrared.STUB_SUBSCRIBERS[:] = saved_subs

        # 没按遥控器就提交 -> no_signal，且订阅保持（用户可直接再按再提交）
        await fl.async_step_learn_press({})
        check(
            (fl.shown.get("errors") or {}).get("base") == "no_signal"
            and fl.shown.get("step_id") == "learn_press"
            and len(infrared.STUB_SUBSCRIBERS) == 1,
            "learn_press 无信号 -> errors.no_signal（订阅保持，不用重进这一步）",
        )

        # ---- 回调侧账本：把"没信号"拆到具体一环 ----
        # 实测踩到（用户截图）：接收器 state 明明在跳时间戳（帧进了 HA），界面上却只有一句
        # 笼统的"没有捕获到红外信号" —— 帧到了 `_learn_on_signal` 之后被 `normalize_capture`
        # 静默丢掉，不留痕迹。下面把四种断法逐一钉住，尤其不能把它们混成一句。
        saved_subs_diag = list(infrared.STUB_SUBSCRIBERS)
        probe = FakeStates.extra.get(RECEIVER)
        saved_probe_state = getattr(probe, "state", None)
        try:
            fl_diag = new_flow()
            await fl_diag.async_step_user({"tx_type": "infrared", "mode": "learn"})
            await fl_diag.async_step_learn_setup(
                {"tx_target": EMITTER, "receiver": RECEIVER, "category": "2"}
            )
            # 首次打开这一页时用户还没按遥控器 —— 此时挂判词必然误报"帧根本没进 HA"。
            check(
                fl_diag.shown["description_placeholders"].get("capture_diag") == "",
                "learn_press 首次打开不带诊断判词（用户还没按，挂出来必然误报）",
                f"实际 {fl_diag.shown['description_placeholders'].get('capture_diag')!r}",
            )

            # A. 没按 + state 未变 -> 帧根本没进 HA（去查接收器/对准）
            FakeStates.extra[RECEIVER].state = "unknown"
            await fl_diag.async_step_learn_press({})
            diag_a = fl_diag.shown["description_placeholders"].get("capture_diag", "")
            check(
                "帧根本没进 HA" in diag_a and "remote.pronto" in diag_a,
                "0 帧 + state 未变 -> 判词直指「帧没进 HA」并给出 ESPHome 日志核对法",
                f"实际 {diag_a[:70]!r}",
            )

            # B. state 已前进但回调 0 次 -> 帧进了 HA、断在订阅上（与 A 处置相反）
            FakeStates.extra[RECEIVER].state = "2026-09-30T01:58:20.927+00:00"
            await fl_diag.async_step_learn_press({})
            diag_b = fl_diag.shown["description_placeholders"].get("capture_diag", "")
            check(
                "帧进了 HA" in diag_b
                and "订阅" in diag_b
                and fl_diag._learn_raw_events == 0,
                "state 前进但回调 0 次 -> 判词区分开「帧进了 HA，断在订阅」（与「帧没进 HA」相反）",
                f"实际 raw_events={fl_diag._learn_raw_events} diag={diag_b[:50]!r}",
            )

            # C. 重进本步（重置账本）后灌一帧含 0 的 -> 回调有事件但被丢
            await fl_diag.async_step_learn_press()
            infrared.STUB_EMIT_RECEIVED(RECEIVER, [9000, 0, 560, -560] * 4)
            await fl_diag.async_step_learn_press({})
            diag_c = fl_diag.shown["description_placeholders"].get("capture_diag", "")
            check(
                "含 0" in diag_c
                and "不是" in diag_c
                and "码库" in diag_c
                and fl_diag._learn_raw_events == 1,
                "帧含 0 -> 判词点名原因，并明确「不是码库的问题」（账本记到 1 次）",
                f"实际 raw={fl_diag._learn_raw_events} drops={fl_diag._learn_dropped}",
            )

            # D/E. 再灌两帧坏帧（不重置）-> 账本按原因**分类累计**，判词一次列全
            infrared.STUB_EMIT_RECEIVED(RECEIVER, [9000, -4500, 560])
            await fl_diag.async_step_learn_press({})
            diag_d = fl_diag.shown["description_placeholders"].get("capture_diag", "")
            check(
                "太短" in diag_d and "3 段" in diag_d,
                "帧太短（3 段）-> 判词点名「太短（只有 3 段…）」",
                f"实际 {diag_d[:70]!r}",
            )

            infrared.STUB_EMIT_RECEIVED(
                RECEIVER, [9000, -4500, "x", -560, 560, -560, 560, -560]
            )
            await fl_diag.async_step_learn_press({})
            diag_e = fl_diag.shown["description_placeholders"].get("capture_diag", "")
            check(
                "非数字" in diag_e,
                "时序含非数字项 -> 判词单独点名（与「含 0」「太短」分开）",
                f"实际 {diag_e[:70]!r}",
            )

            # F. 分类器互不混淆 + 账本累计 + 留下原始样本（供反向复现）
            drops = dict(fl_diag._learn_dropped)
            check(
                fl_diag._classify_drop([1, 0, 2, 3, 4, 5, 6, 7])
                == "时序里含 0（奇偶会错位）"
                and fl_diag._classify_drop([1, 2, 3]).startswith("太短")
                and fl_diag._classify_drop([1, "a", 3]) == "时序里有非数字项"
                and fl_diag._classify_drop([1] * 8) == "时序不合法（未被接纳）"
                and sum(drops.values()) == 3
                and fl_diag._learn_last_raw
                and fl_diag._learn_to_int("x") == "x",
                "帧丢弃分类器 4 类互不混淆、账本累计 = 3、保留原始样本、非数字项容错",
                f"实际 drops={drops} raw={fl_diag._learn_last_raw[:6]}",
            )
        finally:
            # 这个探测流程是丢弃的，它订上的接收器不会自己退 ⇒ 复原订阅表，
            # 否则后面「旧订阅已退、订阅数恰为 1」那条断言会被残留搞红。
            infrared.STUB_SUBSCRIBERS[:] = saved_subs_diag
            if probe is not None and saved_probe_state is not None:
                probe.state = saved_probe_state

        lib = fl._library
        key1 = lib.key_names(lib.get_device(47))[0]
        t1 = lib.get_timings(47, key1)
        infrared.STUB_EMIT_RECEIVED(RECEIVER, t1)      # 模拟按下电源键
        await fl.async_step_learn_press({})
        check(fl.shown.get("step_id") == "learn_press2", "收到电源键 -> 进 learn_press2 步")
        # 订阅数恰为 1 ⇒ 电源键那次订阅已退掉、只剩 press2 新订的这一个；
        # 若 learn_press 退订泄漏，这里会是 2 —— 泄漏就靠这个数字抓出来。
        check(
            fl._learn_capture1 == t1 and len(infrared.STUB_SUBSCRIBERS) == 1,
            "捕获到完整电源键时序，且旧订阅已退（旧泄漏则订阅数=2）、press2 重新订阅",
            f"capture1 len={len(fl._learn_capture1 or [])} / subs={len(infrared.STUB_SUBSCRIBERS)}",
        )

        # 跳过第二键 -> 单键候选
        await fl.async_step_learn_press2({"learn_action": "skip"})
        check(fl.shown.get("step_id") == "learn_pick", "跳过第二键 -> 进 learn_pick 步")
        cands1 = _schema_options(fl.shown["data_schema"], "device") or {}
        check("47" in cands1, f"单键匹配候选含真值 TCL电视-1（{len(cands1)} 个候选）")
        check(fl._learn_used_two_keys is False, "跳过后标记为『仅电源键』")

        # 候选下拉末尾必须有「退回重按」出口 —— 分数太低时（库外遥控）列表里全是错的，
        # 没有这一项用户就只能关掉对话框从头再来，等于"配对没有出口"。
        ph_pick = fl.shown["description_placeholders"]
        check(
            "__back__" in cands1,
            "learn_pick 有「退回重按」出口（低分/库外遥控时唯一的出路）",
            f"实际选项 {sorted(cands1)[:2]}…（共 {len(cands1)}）",
        )
        check(
            ph_pick.get("count") == str(len(cands1) - 1),
            "候选数不含「退回重按」这一项（{count} 不能虚报）",
            f"实际 count={ph_pick.get('count')!r} / 选项 {len(cands1)}",
        )
        # 实测踩到：learn_press 上那句"已收到 N 帧"是快照，用户先看页面后按遥控器
        # ⇒ 几乎总显示 0，被读成"没收到码"甚至"0 帧也能匹配 = 乱选"。所以匹配页必须
        # 摊开真正用的帧（段数/首脉冲）并给一句判词。
        check(
            "段" in ph_pick.get("capture", "")
            and "µs" in ph_pick.get("capture", "")
            and "相似度" in ph_pick.get("verdict", ""),
            "learn_pick 摊开真实匹配数据（段数/首脉冲/全长）+ 给出可信度判词",
            f"实际 capture={ph_pick.get('capture')!r} verdict={ph_pick.get('verdict')!r}",
        )
        await fl.async_step_learn_pick({"device": "__back__"})
        check(
            fl.shown.get("step_id") == "learn_press"
            and fl._learn_candidates is None
            and fl._learn_capture1 is None,
            "选「退回重按」-> 回 learn_press 且清空候选/捕获",
            f"实际 step={fl.shown.get('step_id')!r} cands={fl._learn_candidates!r}",
        )

        # 两键路径（真值必须在交集里）
        fl2 = new_flow()
        await fl2.async_step_user({"tx_type": "infrared", "mode": "learn"})
        await fl2.async_step_learn_setup(
            {"tx_target": EMITTER, "receiver": RECEIVER, "category": "2"}
        )
        infrared.STUB_EMIT_RECEIVED(RECEIVER, t1)
        await fl2.async_step_learn_press({})
        key2 = lib.key_names(lib.get_device(47))[1]
        t2 = lib.get_timings(47, key2)
        infrared.STUB_EMIT_RECEIVED(RECEIVER, t2)
        await fl2.async_step_learn_press2({"learn_action": "next"})
        check(fl2.shown.get("step_id") == "learn_pick", "两键都收到 -> 进 learn_pick 步")
        cands2 = _schema_options(fl2.shown["data_schema"], "device") or {}
        check(
            "47" in cands2,
            f"两键交集候选含真值（交集 {len(cands2)} 个 / 单键 {len(cands1)} 个）",
        )
        check(fl2._learn_used_two_keys is True, "两键路径标记为『两键交集』")
        ph2 = fl2.shown["description_placeholders"]
        check(
            ph2.get("keys") == "两键交集" and "score" in ph2 and "count" in ph2
            and ph2.get("category") == "电视机",
            "learn_pick 占位符带 category/keys/score/count",
            f"实际 {ph2!r}",
        )

        # 选候选 -> learn_test（真的发出一帧）-> ok 建 entry
        infrared.STUB_SENT.clear()
        await fl2.async_step_learn_pick({"device": "47"})
        check(fl2.shown.get("step_id") == "learn_test", "选完候选 -> 进 learn_test 步")
        check(
            len(infrared.STUB_SENT) == 1 and infrared.STUB_SENT[0][0] == EMITTER,
            "learn_test 立即经所选 emitter 发出测试码",
            f"实际 {infrared.STUB_SENT!r}",
        )
        ltest = _schema_options(fl2.shown["data_schema"], "test_result") or {}
        check(
            list(ltest) == ["ok", "retry", "skip"],
            f"learn_test 下拉 = ok/retry/skip（学习模式没有『下一个型号』，{list(ltest)}）",
        )
        await fl2.async_step_learn_test({"test_result": "retry"})
        check(fl2.shown.get("step_id") == "learn_pick", "learn_test 选『没反应』-> 退回 learn_pick")
        await fl2.async_step_learn_pick({"device": "47"})
        await fl2.async_step_learn_test({"test_result": "ok"})
        d_learn = fl2.created["data"]
        check(
            fl2.created["title"] == "TCL电视-1"
            and d_learn["category"] == 2
            and d_learn["brand"] == 14
            and d_learn["device"] == 47
            and d_learn["tx_target"] == EMITTER,
            f"learn 建的 entry 与真值一致（{fl2.created['title']!r} "
            f"{d_learn.get('category')}/{d_learn.get('brand')}/{d_learn.get('device')}）",
        )

        # ⑪ 空调也能配对：走状态码库（learn_match_ac），实测复用「开机 → 关机」两段式
        ac_mod = sys.modules["_ir_hub_shim.ac_library"]
        ac_lib = ac_mod.AcLibrary()
        AC_BIN = "irda_new_ac_11272.bin"          # 美的，索引里真实存在
        ac_frames = list(ac_mod.iter_frames(ac_lib.read_raw(AC_BIN)))
        ac_on1 = ac_frames[0][1]
        ac_on2 = next(f for _s, f in ac_frames if f != ac_on1)   # 同型号的另一个状态帧

        fl_ac = new_flow()
        await fl_ac.async_step_user({"tx_type": "infrared", "mode": "learn"})
        await fl_ac.async_step_learn_setup(
            {"tx_target": EMITTER, "receiver": RECEIVER, "category": "ac"}
        )
        check(
            fl_ac.shown.get("step_id") == "learn_press" and fl_ac._category == "ac",
            "配对流程可选大类「空调」（category 存 'ac'，不是数字 id）",
        )
        infrared.STUB_EMIT_RECEIVED(RECEIVER, ac_on1)
        await fl_ac.async_step_learn_press({})
        check(
            fl_ac.shown.get("step_id") == "learn_press2"
            and fl_ac._learn_capture1 == ac_on1,
            "空调电源键捕获成功（整帧入 capture1）",
        )
        ph_ac = fl_ac.shown["description_placeholders"]
        check(
            ph_ac.get("key_hint") == "温度+（或风速键）" and ph_ac.get("category") == "空调",
            "空调的按键提示与 category 占位符按空调改写（不是音量+/电视机）",
            f"实际 {ph_ac!r}",
        )
        await fl_ac.async_step_learn_press2({"learn_action": "skip"})
        ac_opts = _schema_options(fl_ac.shown["data_schema"], "device") or {}
        check(
            AC_BIN in ac_opts and len(ac_opts) > 1,
            f"空调单键匹配候选含真值（{AC_BIN} 命中，共 {len(ac_opts)} 个候选）",
        )
        # 结构预筛幸存数 = 「这一帧的形状库里有对应吗」。它是区分「库外遥控」与
        # 「捕获被记坏」的唯一判别量 —— 这两者的**解法完全相反**，而旧界面把它们
        # 混成了同一个低分，用户只能瞎试。
        saved_top = fl_ac._learn_top_score
        saved_surv = fl_ac._learn_ac_survivors
        check(
            saved_surv > 3,
            f"真值帧的『结构预筛幸存数』已记录且远离阈值（幸存 {saved_surv} 个 bin）",
            f"实际 _learn_ac_survivors={saved_surv}",
        )
        # ⑫ 接收端把**重复发送**合并成一帧时的容错（空调路径）。
        #    不修的话：捕获 300 段 vs 库帧 200 段，帧长预筛 ±3 把**整库灭掉**，
        #    报出来是"幸存 ≤3 ⇒ 不在码库"，用户会以为型号不对 —— 而实测
        #    两个 burst 块与库帧逐位相同，本来完全匹配得上。
        ac_match_mod = sys.modules["_ir_hub_shim.learn_match_ac"]
        merged_ac = ac_on1 + ac_on1[2:52]          # 末尾 idle 变成内部 gap，再接一段重复
        merged_surv = ac_match_mod.structure_survivors(ac_lib, merged_ac)
        merged_bins = [h["bin"] for h in ac_match_mod.match_ac(ac_lib, merged_ac, 8)]
        check(
            merged_surv > 3 and AC_BIN in merged_bins,
            f"空调：合并过重复块的捕获仍命中真型号（{AC_BIN}，幸存 {merged_surv} 个 bin）",
            f"实际候选 {merged_bins[:4]}",
        )
        fl_ac._learn_top_score = 0.20
        fl_ac._learn_ac_survivors = 1
        verdict_gone = fl_ac._learn_verdict()
        check(
            "不在码库里" in verdict_gone and "手动选择码库" in verdict_gone,
            "低分 + 幸存数 ≈1 -> 判词断定『不在码库里』（改走手动选型号，别重配对）",
            f"实际 {verdict_gone[-90:]!r}",
        )
        fl_ac._learn_ac_survivors = 40
        verdict_bad = fl_ac._learn_verdict()
        check(
            "记坏" in verdict_bad and "不在码库里" not in verdict_bad,
            "低分 + 幸存数正常 -> 判词改说『捕获被接收端记坏』（与『不在库』结论相反）",
            f"实际 {verdict_bad[-90:]!r}",
        )
        fl_ac._learn_top_score = saved_top
        fl_ac._learn_ac_survivors = saved_surv

        AC_BRANDS = ac_lib.brands_of(AC_BIN)
        check(
            fl_ac.shown["description_placeholders"].get("category") == "空调"
            and ac_opts.get(AC_BIN) == f"空调 11272（{len(AC_BRANDS)} 个品牌共用）",
            f"多品牌共用码的候选名标出共用数、不冒认单一品牌（{ac_opts.get(AC_BIN)!r}）",
            f"实际 {ac_opts.get(AC_BIN)!r}，该 bin 共有 {len(AC_BRANDS)} 个品牌",
        )
        infrared.STUB_SENT.clear()
        await fl_ac.async_step_learn_pick({"device": AC_BIN})
        check(
            fl_ac.shown.get("step_id") == "ac_test_on" and fl_ac._from_learn is True,
            "选中空调候选 -> 进第一段实测（开机帧）",
        )
        check(
            len(infrared.STUB_SENT) == 1 and infrared.STUB_SENT[0][0] == EMITTER,
            "空调配对同样真的发出开机帧（经所选 emitter）",
            f"实际 {infrared.STUB_SENT!r}",
        )
        check(
            fl_ac.shown["description_placeholders"].get("brand")
            == f"{len(AC_BRANDS)} 个品牌共用",
            "多品牌共用码的实测文案说成『N 个品牌共用』（不写成某个牌子）",
            f"实际 {fl_ac.shown['description_placeholders'].get('brand')!r}",
        )
        await fl_ac.async_step_ac_test_on({"test_result": "retry"})
        check(
            fl_ac.shown.get("step_id") == "learn_pick",
            "空调实测『没反应』-> 退回候选列表（而不是手动选型号步）",
        )
        await fl_ac.async_step_learn_pick({"device": AC_BIN})
        await fl_ac.async_step_ac_test_on({"test_result": "ok"})
        check(
            fl_ac.shown.get("step_id") == "ac_test",
            "开机有反应 -> 进第二段（关机帧）实测",
        )
        await fl_ac.async_step_ac_test({"test_result": "ok"})
        d_ac_learn = fl_ac.created["data"]
        check(
            d_ac_learn["category"] == "ac"
            and d_ac_learn["device"] == AC_BIN
            and d_ac_learn["brand"] == ""
            and fl_ac.created["title"] == "空调 11272",
            f"空调配对建的 entry 与真值一致（{fl_ac.created['title']!r} "
            f"{d_ac_learn.get('category')}/{d_ac_learn.get('brand')!r}/{d_ac_learn.get('device')}）"
            " —— 共用码不写品牌名",
        )

        # 配对路径的「自动试下一个」= 下一个**匹配候选**（跨品牌），品牌标签要跟着换
        fl_ac3 = new_flow()
        await fl_ac3.async_step_user({"tx_type": "infrared", "mode": "learn"})
        await fl_ac3.async_step_learn_setup(
            {"tx_target": EMITTER, "receiver": RECEIVER, "category": "ac"}
        )
        infrared.STUB_EMIT_RECEIVED(RECEIVER, ac_on1)
        await fl_ac3.async_step_learn_press({})
        await fl_ac3.async_step_learn_press2({"learn_action": "skip"})
        cands3 = fl_ac3._learn_candidates or []
        await fl_ac3.async_step_learn_pick({"device": cands3[0]})
        if len(cands3) > 1:
            await fl_ac3.async_step_ac_test_on({"test_result": "next"})
            next_bin = fl_ac3._ac_bin
            want_label = (
                f"{fl_ac3._ac_brand_count} 个品牌共用"
                if fl_ac3._ac_brand_count > 1
                else ac_lib.brand_of(next_bin)
            )
            check(
                next_bin == cands3[1]
                and fl_ac3._ac_index == 1
                and fl_ac3._ac_brand_count == len(ac_lib.brands_of(next_bin))
                and fl_ac3.shown["description_placeholders"].get("brand") == want_label,
                f"配对路径『自动试下一个』切到下一个匹配候选并同步品牌标签"
                f"（{cands3[0]} -> {next_bin}）",
                f"实际 index={fl_ac3._ac_index} brand={fl_ac3._ac_brand!r} "
                f"count={fl_ac3._ac_brand_count}",
            )
        else:
            check(False, "配对路径『自动试下一个』切到下一个匹配候选", "候选只有 1 个")

        fl_ac2 = new_flow()
        await fl_ac2.async_step_user({"tx_type": "infrared", "mode": "learn"})
        await fl_ac2.async_step_learn_setup(
            {"tx_target": EMITTER, "receiver": RECEIVER, "category": "ac"}
        )
        infrared.STUB_EMIT_RECEIVED(RECEIVER, ac_on1)
        await fl_ac2.async_step_learn_press({})
        infrared.STUB_EMIT_RECEIVED(RECEIVER, ac_on2)
        await fl_ac2.async_step_learn_press2({"learn_action": "next"})
        ac2_opts = _schema_options(fl_ac2.shown["data_schema"], "device") or {}
        check(
            fl_ac2.shown.get("step_id") == "learn_pick"
            and AC_BIN in ac2_opts
            and fl_ac2._learn_used_two_keys is True,
            f"空调两键取交集仍含真值（交集 {len(ac2_opts)} 个候选）",
        )

        check(
            flow_mod.IrHubConfigFlow.async_get_options_flow(None).__class__.__name__
            == "IrHubOptionsFlow",
            "async_get_options_flow 返回 IrHubOptionsFlow",
        )

        # ⑦ OptionsFlow（0.3.12 起含发射通道与可选传感器）
        options = flow_mod.IrHubOptionsFlow()
        options.hass = FakeHass()
        options.config_entry = FakeEntry()
        infrared.STUB_EMITTERS = [EMITTER]
        await options.async_step_init()
        check(options.shown["step_id"] == "init", "options 步 step_id = init")
        defaults = _schema_defaults(options.shown["data_schema"])
        check(
            defaults.get("carrier") == 38000
            and defaults.get("repeats") == 1
            and defaults.get("tx_delay") == 0.5
            and defaults.get("tx_type") == "infrared",
            "options 表单默认值来自 entry（38000 / 1 / delay 0.5 / infrared）",
            f"实际 {defaults!r}",
        )
        check(
            _schema_options(options.shown["data_schema"], "tx_target")
            == {EMITTER: None},
            "options 的发射目标按通道出下拉（infrared -> emitter 实体）",
        )
        # 传感器字段**只有空调条目显示**（2026-09-30 用户指正：电视/机顶盒用不到）
        check(
            all(
                _schema_defaults(options.shown["data_schema"]).get(k) is None
                for k in ("temperature_sensor", "humidity_sensor", "power_sensor")
            )
            and options.shown["description_placeholders"]["sensors_hint"] == "",
            "非空调条目 -> 选项里不出现传感器字段（说明占位符也是空）",
            f"实际 defaults={_schema_defaults(options.shown['data_schema'])!r}",
        )

        class FakeAcEntry(FakeEntry):
            data = {
                **FakeEntry.data,
                "category": "ac",
                "device": "irda_new_ac_11272.bin",
            }

        options = flow_mod.IrHubOptionsFlow()
        options.hass = FakeHass()
        options.config_entry = FakeAcEntry()
        await options.async_step_init()
        ac_defaults = _schema_defaults(options.shown["data_schema"])
        check(
            ac_defaults.get("temperature_sensor") == ""
            and ac_defaults.get("humidity_sensor") == ""
            and ac_defaults.get("power_sensor") == "",
            "空调条目 -> 可选传感器三个字段出现且默认空（留空 = 不用）",
            f"实际 {ac_defaults!r}",
        )
        check(
            options.shown["description_placeholders"]["sensors_hint"] != "",
            "空调条目 -> 传感器说明占位符非空",
        )
        _full = {
            "carrier": 38000, "repeats": 1, "tx_delay": 0.5,
            "tx_type": "infrared", "tx_target": EMITTER,
        }
        validated = options.shown["data_schema"](
            {**_full, "carrier": "40000", "repeats": "3", "tx_delay": "1"}
        )
        check(
            validated["carrier"] == 40000 and validated["repeats"] == 3
            and validated["tx_delay"] == 1.0,
            "options 对输入做 Coerce（字符串 -> int/float）",
        )
        # 保存：tx 字段 + 传感器一起写进 options（HA 会自动 reload 条目）
        options = flow_mod.IrHubOptionsFlow()
        options.hass = FakeHass()
        options.config_entry = FakeAcEntry()
        await options.async_step_init({
            **_full, "carrier": 56000, "repeats": 2, "tx_delay": 1.5,
            "temperature_sensor": "sensor.room_temp",
            "humidity_sensor": "sensor.room_hum",
            "power_sensor": "switch.ac_plug",
        })
        check(
            options.created["data"] == {
                "carrier": 56000, "repeats": 2, "tx_delay": 1.5,
                "tx_type": "infrared", "tx_target": EMITTER,
                "mqtt_format": "smartac",
                "temperature_sensor": "sensor.room_temp",
                "humidity_sensor": "sensor.room_hum",
                "power_sensor": "switch.ac_plug",
            },
            "options 保存载波/次数/延迟/通道/目标/传感器（改完自动 reload）",
            f"实际 {options.created!r}",
        )
        # 目标失效（emitter 被删）-> 表单能打开（旧值留在下拉里），提交被拦下并给报错
        infrared.STUB_EMITTERS = ["infrared.another"]
        options = flow_mod.IrHubOptionsFlow()
        options.hass = FakeHass()
        options.config_entry = FakeEntry()
        await options.async_step_init()
        check(
            EMITTER in _schema_options(options.shown["data_schema"], "tx_target"),
            "存的旧 emitter 已不在列表 -> 仍留在下拉里（表单不能打不开）",
        )
        await options.async_step_init({**_full, "tx_target": EMITTER})
        check(
            options.created is None
            and options.shown.get("errors") == {"base": "target_missing"},
            "旧 emitter 已失效 -> 提交被拦下并报 target_missing",
            f"实际 created={options.created!r} errors={options.shown.get('errors')!r}",
        )
        infrared.STUB_EMITTERS = [EMITTER]
        # 换发射通道：通道变了 -> 目标字段按新通道重画（mqtt 是自由文本），不丢已填值
        options = flow_mod.IrHubOptionsFlow()
        options.hass = FakeHass()
        options.config_entry = FakeEntry()
        await options.async_step_init()
        await options.async_step_init({
            **_full, "carrier": 40000, "tx_type": "mqtt", "tx_target": "tcl_ir/ir_send",
        })
        check(
            options.created is None and options.shown["step_id"] == "init",
            "切换通道类型 -> 重画表单（不直接保存）",
            f"实际 created={options.created!r}",
        )
        check(
            _schema_options(options.shown["data_schema"], "tx_target") is None
            and _schema_defaults(options.shown["data_schema"]).get("tx_target")
            == "tcl_ir/ir_send"
            and _schema_defaults(options.shown["data_schema"]).get("carrier") == 40000,
            "重画后 mqtt 的目标是自由文本、用户已填的 topic 与载波保留",
            f"实际 defaults={_schema_defaults(options.shown['data_schema'])!r}",
        )
        # mqtt 通道保存（需要 mqtt.publish 服务在场才会过校验）
        options.hass.services._have.add(("mqtt", "publish"))
        await options.async_step_init({
            **_full, "carrier": 40000, "tx_type": "mqtt", "tx_target": "tcl_ir/ir_send",
            "mqtt_format": "tasmota",
        })
        check(
            options.created is not None
            and options.created["data"]["tx_type"] == "mqtt"
            and options.created["data"]["tx_target"] == "tcl_ir/ir_send"
            and options.created["data"]["mqtt_format"] == "tasmota",
            "options 换成 mqtt 通道保存（含 mqtt_format）",
            f"实际 created={options.created!r}",
        )

        # ⑧ mqtt 通道的 OptionsFlow 才出现 mqtt_format 字段（默认 smartac）
        class FakeMqttEntry(FakeEntry):
            data = {**FakeEntry.data, "tx_type": "mqtt", "tx_target": "tcl_ir/ir_send"}

        options = flow_mod.IrHubOptionsFlow()
        options.hass = FakeHass()
        options.config_entry = FakeMqttEntry()
        await options.async_step_init()
        mdefaults = _schema_defaults(options.shown["data_schema"])
        check(
            mdefaults.get("mqtt_format") == "smartac"
            and mdefaults.get("tx_type") == "mqtt",
            "mqtt 条目的 options 出现 mqtt_format（默认 smartac）",
            f"实际 {mdefaults!r}",
        )
        options = flow_mod.IrHubOptionsFlow()
        options.hass = FakeHass()
        options.config_entry = FakeMqttEntry()
        # mqtt 通道校验要求 mqtt.publish 服务在场
        options.hass.services._have.add(("mqtt", "publish"))
        await options.async_step_init({
            **_full, "tx_type": "mqtt", "tx_target": "tcl_ir/ir_send",
            "carrier": 40000, "repeats": 2, "tx_delay": 1.0, "mqtt_format": "tasmota",
        })
        check(
            options.created["data"].get("mqtt_format") == "tasmota",
            "options 保存 mqtt_format=tasmota",
        )

    asyncio.run(run())


def check_button_platform() -> None:
    """button 平台：每键一个按钮，重点在 entity_id 的撞名防护。

    无硬件也能测，且跑的正是上线后会执行的代码。
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

    # --- 2. 不变量：同一台设备内按键 object_id 不能撞名 ---
    # 先给全库键名（仅 61 种）建 object_id 映射表，再逐设备查表；不要逐键调 slugify
    # （Unidecode 开销大，会卡死）。
    # 判据是"同一设备内不撞名"而非"全局键名不撞名"：全库同时有 'Shake' 与 'shake'
    # （slugify 后同为 'shake'），却没有任何设备同时含这两键，故实际不撞。
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

    def build(key: str, **entry_overrides):
        ent = button_mod.IrHubButton(
            entry=FakeEntry(),
            library=library,
            device=device,
            key=key,
            carrier=38000,
            repeats=1,
            tx_type="infrared",
            tx_target=emitter,
            tx_delay=0.5,
            mqtt_format="smartac",
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
        btn._infrared_emitter_entity_id == emitter
        and btn._tx_type == "infrared"
        and btn._tx_target == emitter,
        "把发射通道交给实体（infrared：emitter id 供基类跟踪可用性）",
    )
    check(button_mod.PARALLEL_UPDATES == 0, "PARALLEL_UPDATES = 0（红外无状态，不轮询）")

    # --- 4. async_press 真的把码交给 emitter ---
    # 下面三条必须无条件执行（不能包进 `if btn.sent:`）：发送失败时会静默少跑两项，
    # 那正是项数护栏要防的假通过。
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
    """services.yaml 与 SEND_RAW_SCHEMA 的字段一致性。

    services.yaml 只影响 UI 展示，但格式错会被 HA 静默拒绝加载，故值得一条断言；
    字段还要和真正校验的 schema 一致（多写用户填了没用，少写 UI 里看不到）。
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
    """AC 状态码库 + climate 平台。

    A. 码库数据（index 引用的 bin 齐全、关键品牌在位）
    B. 解码器（全库 bin 解码 + 符号交替不变式 + 锚点）
    C. climate 实体逻辑（stub homeassistant，跑真实代码路径）
    D. config flow 的 AC 分支（真 voluptuous）
    """
    import asyncio

    print("\n[12] AC 状态码库 + climate 平台")
    _install_protocols_stub()
    HomeAssistantError = _install_ha_stubs()

    const_mod = sys.modules["_ir_hub_shim.const"]
    ha_const = sys.modules["homeassistant.const"]
    _climate_mod = sys.modules["homeassistant.components.climate"]
    HVACMode = _climate_mod.HVACMode
    # 0.3.11 起 supported_features 是**随模式变化的 property**，断言要直接读它
    ClimateEntityFeature = _climate_mod.ClimateEntityFeature

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
    # 摆风三分类计数（见 ac_library._swing_options）
    swing_kinds = {"状态帧": 0, "独立帧": 0, "无": 0}
    for bin_name in bins:
        try:
            code = ac_lib.load_device(bin_name)
            assert code["modes"], bin_name
            assert code["off"] and code["off"][0] > 0, bin_name

            # 能力表必须与 commands 的实际层数自洽（面板就是照能力表声明的）
            fans_by_mode = code["fans_by_mode"]
            temps_by_mode = code["temps_by_mode"]
            names = code["swing_modes"]
            assert list(fans_by_mode) == code["modes"], "fans_by_mode 的模式集不符"
            assert list(temps_by_mode) == code["modes"], "temps_by_mode 的模式集不符"
            assert names == [o["name"] for o in code["swing"]], "swing_modes 与 swing 不符"
            for mode, mode_fans in fans_by_mode.items():
                assert mode_fans == [
                    f for f in code["fan_modes"] if f in mode_fans
                ], f"{mode}: 风速顺序与并集不一致"
                expect_temps = max(1, len(temps_by_mode[mode]))
                for fan in mode_fans:
                    node = code["commands"][mode][fan]
                    layers = [node[n] for n in names] if names else [node]
                    assert all(
                        len(temps) == expect_temps for temps in layers
                    ), f"{mode}/{fan}: 温度键数与能力表不符"

            if names:
                swing_kinds[
                    "状态帧" if code["swing"][1]["level"] is not None else "独立帧"
                ] += 1
            else:
                swing_kinds["无"] += 1

            all_frames = [code["off"]]
            for fans in code["commands"].values():
                for node in fans.values():
                    # 支持摆风的型号多一层（commands[mode][fan][摆风档][温度]）
                    layers = (
                        [node[name] for name in code["swing_modes"]]
                        if code["swing_modes"]
                        else [node]
                    )
                    for temps in layers:
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
    check(
        sum(swing_kinds.values()) == len(bins) and swing_kinds["状态帧"] > 300,
        f"摆风三分类：状态帧 {swing_kinds['状态帧']} / 独立帧 {swing_kinds['独立帧']}"
        f" / 无 {swing_kinds['无']}（合计 {len(bins)}）",
    )

    # --- B2. 门禁：任何组合都必须能取到帧 ---
    # 用户原话（2026-09-30）：「不应该有按了不执行情况」「绝大部分品牌空调器面板
    # 都有这个问题」。所以这里**穷举全库**：每型号 × 每模式 × 每风速 × 边界温度
    # （低于/高于库范围 + 该型号 min/max）× 每摆风档（含"不指定"），
    # `frame_for` 必须永远给出非空时序 —— 空就是面板上会「按了不执行」的角落。
    dead: list[str] = []
    combos = 0
    for bin_name in bins:
        code = ac_lib.load_device(bin_name)
        swing_axes = [None] + list(code["swing_modes"] or [])
        for mode in code["modes"]:
            for fan in code["fans_by_mode"][mode]:
                for temp in (code["min_temp"] - 5, code["min_temp"],
                             code["max_temp"], code["max_temp"] + 5):
                    for swing in swing_axes:
                        combos += 1
                        frame, _notes = ac_mod.frame_for(
                            code, mode, fan, temp, swing
                        )
                        if not frame:
                            dead.append(f"{bin_name} {mode}/{fan}/{temp}/{swing}")
    check(
        not dead,
        f"全库 {len(bins)} 型号 × {combos} 种组合 frame_for 均给出非空时序"
        f"（面板不会『按了不执行』）",
        "; ".join(dead[:3]),
    )

    # 锚点：美的 11837 —— 用户 2026-09-30 实测报「fan_only 组合报错 + 没有摆风」的那台。
    # 钉住它，就钉住了"按模式的能力表"和"独立摆风帧"两条修复。
    code_11837 = ac_lib.load_device("irda_new_ac_11837.bin")
    check(
        code_11837["fans_by_mode"]["auto"] == ["auto"]
        and code_11837["fans_by_mode"]["dry"] == ["auto"]
        and code_11837["fans_by_mode"]["cool"] == ["auto", "low", "medium", "high"],
        "锚点 11837：auto/dry 只有自动风、cool/heat/fan_only 四档",
        f"实际 {code_11837['fans_by_mode']}",
    )
    check(
        code_11837["temps_by_mode"]["fan_only"] == []
        and code_11837["temps_by_mode"]["cool"] == list(range(17, 31)),
        "锚点 11837：fan_only 无温度维度（旧实现正是在这里报『不支持组合』）、cool 17~30",
        f"实际 {code_11837['temps_by_mode']}",
    )
    check(
        code_11837["swing_modes"] == ["off", "on"]
        and code_11837["swing"][1]["function"] == 6
        and code_11837["swing"][1]["level"] is None,
        "锚点 11837：摆风走独立功能帧（function 6）而不是状态位",
        f"实际 {code_11837['swing']}",
    )
    _f, _n = ac_mod.frame_for(code_11837, "fan_only", "high", 30)
    check(
        _f is not None and len(_f) == 200 and _n == [],
        "frame_for 在 11837 的 fan_only/high/30°C 上直接给出一帧且无需替换",
        f"实际 notes={_n}",
    )
    _f, _n = ac_mod.frame_for(code_11837, "auto", "high", 30)
    check(
        _f is not None and len(_n) == 1 and "auto" in _n[0],
        "frame_for 在 11837 的 auto/high（库码只有自动风）上替换风速并说明原因",
        f"实际 notes={_n}",
    )

    # 锚点：美的 11272（SmartAC 实配过的型号），钉住模式集与引导码
    code = ac_lib.load_device("irda_new_ac_11272.bin")
    frame, _frames_notes = ac_mod.frame_for(code, "cool", "auto", 26)
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

    class FakeStates:
        """极简状态注册表：`hass.states.get()` / `set()`，传感器用例够用。"""

        def __init__(self) -> None:
            self._s: dict = {}

        def set(self, entity_id: str, state: str) -> None:
            self._s[entity_id] = _types.SimpleNamespace(
                entity_id=entity_id, state=state
            )

        def get(self, entity_id: str):
            return self._s.get(entity_id)

    FakeHass = _types.SimpleNamespace(
        config=_types.SimpleNamespace(
            units=_types.SimpleNamespace(temperature_unit="°C")
        ),
        data={},
        states=FakeStates(),
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

    def build(extra_data: dict | None = None):
        data = {**FakeEntry.data, **(extra_data or {})}
        entity = climate_mod.IrHubClimate(FakeHass, FakeEntry(), dict(data), dict(code))
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
    check(
        entity.fan_modes == ["auto", "low", "medium", "high"],
        "fan_modes（cool 模式）= 4 档",
    )
    check(
        entity.min_temp == 17.0 and entity.max_temp == 30.0,
        "min/max 温度随当前模式（cool → 17/30）",
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
        # ---- 按模式的动态能力（本次修复的核心，2026-09-30 用户实测）----
        # 库码里每个模式的风速/温度范围并**不相同**（实测 342/509 个 bin 至少有一个
        # 模式没有温度维度、330/509 个 bin 各模式风速集合不同）。声明成全模式并集
        # 就会让面板给出不存在的组合，用户看到的是"按了报错不执行"。
        entity = build()
        check(
            entity.fan_modes == ["auto", "low", "medium", "high"],
            "cool 模式声明 4 档风速",
        )
        entity._attr_hvac_mode = HVACMode.AUTO
        check(entity.fan_modes == ["auto"], "auto 模式只声明 1 档风速（库码如此）")
        check(
            entity.min_temp == 17.0 and entity.max_temp == 30.0,
            "auto 模式有温度维度 -> 17~30",
        )
        entity._attr_hvac_mode = HVACMode.FAN_ONLY
        check(
            not (entity.supported_features & ClimateEntityFeature.TARGET_TEMPERATURE),
            "fan_only 无温度维度 -> 不声明 TARGET_TEMPERATURE（面板不显示温度滑条）",
        )
        entity = build()
        entity._attr_hvac_mode = HVACMode.DRY
        check(entity.fan_modes == ["auto"], "dry 模式只声明 1 档风速")

        # ---- 「按了必执行」：库码里不存在的组合也发得出去（不再抛异常）----
        entity = build()
        entity._attr_hvac_mode = HVACMode.AUTO
        await entity.async_set_fan_mode("high")          # auto 模式没有 high
        check(
            entity._attr_fan_mode == "auto" and len(entity.sent) == 1,
            "auto + high（库码没有）-> 自动换成 auto 并真的发出 1 帧",
        )
        entity = build()
        entity._attr_hvac_mode = HVACMode.FAN_ONLY
        await entity.async_set_temperature(temperature=30)   # fan_only 无温度
        check(
            len(entity.sent) == 1
            and len(entity.sent[0].get_raw_timings()) == 200,
            "fan_only + 30°C（该模式无温度）-> 照样发出状态帧，不报错",
        )
        entity = build()
        entity._attr_hvac_mode = HVACMode.COOL
        await entity.async_set_temperature(temperature=35)   # 超出 17~30
        check(
            entity._attr_target_temperature == 30.0 and len(entity.sent) == 1,
            "cool + 35°C（超范围）-> 收敛到最近的 30°C 并发出",
        )
        # 切模式时把不再可用的旧风速收敛掉（否则面板上留着无效值）
        entity = build()
        entity._attr_hvac_mode = HVACMode.COOL
        entity._attr_fan_mode = "high"
        await entity.async_set_hvac_mode(HVACMode.AUTO)
        check(
            entity._attr_fan_mode == "auto" and len(entity.sent) == 1,
            "cool/high -> 切 auto（只有 auto 档）-> 风速收敛为 auto 并发出",
        )

        # ---- 摆风 ----
        check(
            entity.swing_modes == ["off", "on"]
            and bool(entity.supported_features & ClimateEntityFeature.SWING_MODE),
            "11272 库码有独立摆风帧 -> 声明 off/on 两档摆风",
        )
        _plain = code["commands"]["cool"]["auto"]["off"]["26"]
        entity = build()
        entity._attr_hvac_mode = HVACMode.COOL
        await entity.async_set_swing_mode("on")
        check(
            len(entity.sent) == 1
            and entity.sent[0].get_raw_timings() != _plain,
            "set_swing_mode(on) -> 发出的帧与 off 档不同（function 6 已叠加）",
        )
        # 库码里没有摆风的型号 -> 不该声明 SWING_MODE
        entity = build()
        entity._swing_modes = []
        check(
            entity.swing_modes is None
            and not (entity.supported_features & ClimateEntityFeature.SWING_MODE),
            "库码无摆风的型号 -> swing_modes 为 None 且不声明 SWING_MODE",
        )
        # RestoreEntity：历史状态 heat / medium / 25
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

        # ---- 可选传感器（0.3.12，对齐 SmartAC）----
        # 配了温湿度/功率传感器 -> async_added_to_hass 挂 3 个订阅
        event_mod = sys.modules["homeassistant.helpers.event"]
        event_mod._STUB_SUBS.clear()
        entity = build({
            "temperature_sensor": "sensor.room_temp",
            "humidity_sensor": "sensor.room_hum",
            "power_sensor": "switch.ac_plug",
        })
        await entity.async_added_to_hass()
        check(
            len(event_mod._STUB_SUBS) == 3,
            "配了温湿度/功率传感器 -> async_added_to_hass 挂 3 个订阅",
            f"实际 {len(event_mod._STUB_SUBS)} 个",
        )
        check(
            entity.current_temperature is None and entity.current_humidity is None,
            "传感器还没上报 -> current_temperature / humidity 为 None（不伪造）",
        )
        # 无历史状态也要挂上订阅（旧实现 last is None 直接 return，传感器永远挂不上）
        entity = build({"power_sensor": "switch.ac_plug"})
        event_mod._STUB_SUBS.clear()
        await entity.async_added_to_hass()
        check(
            len(event_mod._STUB_SUBS) == 1
            and event_mod._STUB_SUBS[0][0] == ("switch.ac_plug",),
            "无历史状态（last None）时订阅照样挂上（旧实现会整段跳过）",
        )
        # 温度/湿度：事件驱动 + 初始值读一次
        entity = build({
            "temperature_sensor": "sensor.room_temp",
            "humidity_sensor": "sensor.room_hum",
        })
        FakeHass.states.set("sensor.room_temp", "27.5")
        FakeHass.states.set("sensor.room_hum", "61")
        await entity.async_added_to_hass()
        check(
            entity.current_temperature == 27.5 and entity.current_humidity == 61.0,
            "added_to_hass 时读一次传感器现值（27.5°C / 61%）",
        )
        FakeHass.states.set("sensor.room_temp", "unknown")
        await entity._async_temp_sensor_changed_event(
            _types.SimpleNamespace(
                data={"new_state": FakeHass.states.get("sensor.room_temp")}
            )
        )
        FakeHass.states.set("sensor.room_temp", "26.0")
        await entity._async_temp_sensor_changed_event(
            _types.SimpleNamespace(
                data={"new_state": FakeHass.states.get("sensor.room_temp")}
            )
        )
        check(
            entity.current_temperature == 26.0,
            "温度事件 unknown 跳过、26.0 生效（读不懂的值不覆盖旧值）",
        )
        # 功率传感器：ON 且当前关机 -> 同步成开机状态、不发红外
        entity = build({"power_sensor": "switch.ac_plug"})
        await entity.async_added_to_hass()
        entity._last_on_operation = HVACMode.HEAT
        await entity._async_power_sensor_changed_event(
            _types.SimpleNamespace(data={
                "new_state": _types.SimpleNamespace(entity_id="switch.ac_plug", state="on"),
                "old_state": _types.SimpleNamespace(entity_id="switch.ac_plug", state="off"),
            })
        )
        check(
            entity._attr_hvac_mode == HVACMode.HEAT
            and entity._on_by_remote
            and len(entity.sent) == 0,
            "功率 ON 且当前关机 -> 同步成上次开的模式（不发红外）",
        )
        # 同状态事件（on -> on）-> 忽略
        await entity._async_power_sensor_changed_event(
            _types.SimpleNamespace(data={
                "new_state": _types.SimpleNamespace(entity_id="switch.ac_plug", state="on"),
                "old_state": _types.SimpleNamespace(entity_id="switch.ac_plug", state="on"),
            })
        )
        check(
            entity._attr_hvac_mode == HVACMode.HEAT and len(entity.sent) == 0,
            "功率 on -> on（状态没变）-> 忽略",
        )
        # OFF -> 关机
        await entity._async_power_sensor_changed_event(
            _types.SimpleNamespace(data={
                "new_state": _types.SimpleNamespace(entity_id="switch.ac_plug", state="off"),
                "old_state": _types.SimpleNamespace(entity_id="switch.ac_plug", state="on"),
            })
        )
        check(
            entity._attr_hvac_mode == HVACMode.OFF and not entity._on_by_remote,
            "功率 OFF -> 面板同步成关机",
        )
        # 没配传感器 -> 完全无订阅（零开销）
        event_mod._STUB_SUBS.clear()
        entity = build()
        await entity.async_added_to_hass()
        check(
            len(event_mod._STUB_SUBS) == 0,
            "没配传感器 -> 不挂任何订阅",
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
        await flow.async_step_user({"tx_type": "infrared"})
        check(flow.shown.get("step_id") == "tx", "选 infrared -> 进 tx 步")
        cats = _schema_options(flow.shown["data_schema"], "category") or {}
        check(
            "空调" in cats.get("ac", "") and "品牌" in cats.get("ac", ""),
            f"category 下拉含「空调 · 温控面板」特殊项（{cats.get('ac')!r}）",
        )
        flow = new_flow()
        await flow.async_step_user({"tx_type": "infrared"})
        await flow.async_step_tx({"tx_target": EMITTER, "category": "ac"})
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
        check(
            flow.shown.get("step_id") == "ac_test_on",
            "选完空调型号 -> 进 ac_test_on 步（两段式第一段：发开机帧）",
        )
        check(
            len(infrared.STUB_SENT) == 1 and infrared.STUB_SENT[0][0] == EMITTER,
            "ac_test_on 立即发出一帧开机测试码",
            f"实际 {infrared.STUB_SENT!r}",
        )
        on_timings = infrared.STUB_SENT[0][1].get_raw_timings()
        check(
            len(on_timings) >= 2 and any(t < 0 for t in on_timings),
            f"开机帧非空且带符号（{len(on_timings)} 个时序值）",
        )
        # 选帧对齐 SmartAC async_test：mode/fan 优先 auto，温度优先 26、否则取第一个
        # （11272 的 auto 模式 17~30 全在，故实发 auto/auto/26°C）
        # ⚠️ 0.3.11 起 commands 层数随摆风变化（四层），**不能自己摸层数** ——
        #    统一用 frame_for() 取帧，它按能力表就近替换并返回 note 列表。
        ac_code = flow._ac_code
        _m = "auto" if "auto" in ac_code["commands"] else next(iter(ac_code["commands"]))
        _f = "auto" if "auto" in ac_code["commands"][_m] else next(iter(ac_code["commands"][_m]))
        expect_on, _notes = ac_mod.frame_for(ac_code, _m, _f, 26)
        check(
            on_timings == expect_on,
            f"开机帧 = {_m}/{_f}/26°C（对齐 SmartAC async_test 选帧逻辑）",
        )
        check(
            _notes == [],
            f"开机帧取的是 {_m}/{_f}/26°C 原样组合、无需就近替换",
            f"实际替换说明 {_notes!r}",
        )
        # 有反应 -> 进第二段（关机帧）
        infrared.STUB_SENT.clear()
        await flow.async_step_ac_test_on({"test_result": "ok"})
        check(
            flow.shown.get("step_id") == "ac_test",
            "开机确认 -> 进 ac_test 步（第二段：发关机帧）",
        )
        check(
            len(infrared.STUB_SENT) == 1 and infrared.STUB_SENT[0][0] == EMITTER,
            "ac_test 立即发出一帧关机测试码",
        )
        ac_timings = infrared.STUB_SENT[0][1].get_raw_timings()
        check(
            len(ac_timings) >= 2 and any(t < 0 for t in ac_timings),
            f"关机帧非空且带符号（{len(ac_timings)} 个时序值）",
        )
        # 没反应 -> next 自动试下一个；有反应 -> 建 entry
        ac_bins = [d["bin"] for d in flow._ac_library.devices_in("美的")]
        await flow.async_step_ac_test({"test_result": "next"})
        if len(ac_bins) > 1:
            expect_bin = ac_bins[ac_bins.index("irda_new_ac_11272.bin") + 1]
            check(
                flow.shown.get("step_id") == "ac_test"
                and flow._ac_bin == expect_bin,
                f"空调『自动试下一个』前进到下一型号（{expect_bin}）",
            )
        else:
            check(flow.aborted == "test_exhausted", "无更多候选 -> abort(test_exhausted)")
        # 开机段也能 next（在当前候选上继续前进并重发开机帧）
        infrared.STUB_SENT.clear()
        await flow.async_step_ac_test_on({"test_result": "next"})
        if ac_bins.index(expect_bin) + 1 < len(ac_bins):
            expect_bin2 = ac_bins[ac_bins.index(expect_bin) + 1]
            check(
                flow.shown.get("step_id") == "ac_test_on"
                and flow._ac_bin == expect_bin2,
                f"开机段『自动试下一个』前进到 {expect_bin2} 并重发开机帧",
            )
        else:
            check(flow.aborted == "test_exhausted", "无更多候选 -> abort(test_exhausted)")
        # 回到 11272 再走一遍两段 ok
        await flow.async_step_ac_test_on({"test_result": "retry"})
        await flow.async_step_ac_device({"device": "irda_new_ac_11272.bin"})
        infrared.STUB_SENT.clear()
        await flow.async_step_ac_test_on({"test_result": "ok"})
        await flow.async_step_ac_test({"test_result": "ok"})
        created = flow.created
        data = created["data"]
        check(
            data["category"] == "ac"
            and data["brand"] == "美的"
            and data["device"] == "irda_new_ac_11272.bin"
            and data["tx_type"] == "infrared"
            and data["tx_target"] == EMITTER
            and data["emitter"] == EMITTER
            and data["carrier"] == 38000,
            "AC entry data 完整（tx 通道 + emitter 别名 + category/brand/device bin/carrier）",
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
    check_tx()
    check_config_flow()
    check_button_platform()
    check_services_yaml()
    check_ac()
    check_learn_match()

    # 自检自身的护栏（务必保留）：某段 check 若因异常/条件分支/回退旧版本而整段
    # 没执行，CHECKS 会变小，但脚本照旧打印"全部通过"，不看数字发现不了 —— 本项目
    # 真发生过一次。`CHECKS + len(SKIPPED)` 恒定等于下面的常数，与发布形态无关
    # （被外部依赖挡掉的项计入 SKIPPED，两者相加仍相等）。新增/删除 check 时须同步。
    #      [1]~[8] + [9] config_flow 23 项 = 84   （⑥ 实测步 test +5）
    #      + [10] button 平台 21 项        = 105
    #      + [11] services.yaml 4 项       = 109
    #      + [12] AC 码库 + climate 40 项  = 149  （ac_test 实测步 +4）
    #      + 学习模式 [9] 内 23 项 + 翻译 data 键 4 项 = 189 → 216
    #        详见「⑩ 学习模式」段：mode/abort/订阅/no_signal/单键/双键/pick/test/entry
    #      + [13] learn_match 不变式 7 项  = 223
    #      + ⑪ 空调也能配对 13 项          = 236
    #        （⑪ 明细：选空调大类 / 捕获电源键 / 占位符改写 / 单键候选含真值 /
    #          共用码候选名不冒认品牌 / 进 ac_test_on + 真发帧 /
    #          共用码实测文案 / retry 退回候选 / ok 进 ac_test /
    #          entry 校验（共用码品牌留空）/ 跨品牌『自动试下一个』 /
    #          两键交集仍含真值）
    #      + 接收端诊断 2 项（接收器状态探针 / 订阅失败不再冒充没信号） = 238
    #      + 0 帧病因护栏 3 项（解析出 3 种病因 / 每语言都覆盖这 3 个 error 键 /
    #        实体不存在 -> receiver_unavailable）                    = 241
    #      + learn_pick 3 项（「退回重按」出口 / {count} 不含该项 /
    #        摊开真实匹配数据 + 可信度判词）                          = 244
    #      + 「退回重按」导航 1 项（回 learn_press 且清空候选/捕获）   = 245
    #      + 回调侧账本 7 项（首次打开不挂判词 / state 未变 = 帧没进 HA /
    #        state 已变但回调 0 次 = 断在订阅 / 含 0 / 太短 / 非数字 /
    #        分类器 4 类互不混淆 + 账本累计 = 3 + 留原始样本）          = 252
    #      + 空调结构预筛幸存数 3 项（真值帧幸存数远离阈值 / 低分+幸存≈1 说
    #        『不在码库』/ 低分+幸存正常 说『捕获被记坏』——两者结论相反）  = 255
    #      + 0.3.10 burst 前缀容错 3 项（burst_prefixes 能在内部 gap 截出原帧 /
    #        capture_candidates 前两项仍向后兼容 / 合并帧仍排第 1）        = 258
    #      + 0.3.10 空调配对实测帧 1 项（ac_test_on 立即发开机帧）        = 259
    #      + 0.3.11 按模式能力表 + 摆风 20 项（本段新增；同时删掉旧用例
    #        「不存在的模式×风速组合 -> HomeAssistantError」——那个行为正是
    #        用户 2026-09-30 要求废除的，故净 +19 = 278）：
    #        ① 摆风三分类合计 = bin 数（状态帧 / 独立帧 / 无）            +1
    #        ② 全库「无所不能」门禁 1 条：525 型号 × 91936 种组合
    #           （模式 × 风速 × 边界温度 × 每摆风档）frame_for 均非空
    #           —— 直接钉死用户那句「不应该有按了不执行情况」            +1
    #        ③ 11837 锚点 5 条（用户实测报错的那台）：auto/dry 只有自动风 /
    #           fan_only 无温度维度 / 摆风走独立功能帧 function 6 /
    #           frame_for 在 fan_only·high·30°C 直接给帧 /
    #           frame_for 在 auto·high 替换风速并说明原因                +5
    #        ④ 11272 声明 off/on 摆风 + 开机帧取 auto/auto/26 无需替换    +2
    #        ⑤ climate 动态能力与「永不报错」11 条：cool 4 档风 /
    #           auto 只 1 档风 / auto 有 17~30 / dry 只 1 档风 /
    #           fan_only 不声明 TARGET_TEMPERATURE / auto+high 自动换 auto
    #           并真的发出 / fan_only+30°C 照样发 / cool+35°C 收敛到 30 /
    #           切模式收敛风速 / set_swing_mode 的帧与 off 档不同 /
    #           无摆风的型号不声明 SWING_MODE                            +11
    #      + 0.3.12 SmartAC 移植 20 项（本段新增）：
    #        ① 翻译 2 项（每语言 options init data 9 键 + options.error 覆盖
    #           target_missing/mqtt_missing —— 按语言循环展开）
    #        ② options flow 9 项（下拉按通道出 / 非空调条目不出现传感器字段 /
    #           空调条目传感器默认空 + 说明非空 / Coerce / 完整保存 tx+传感器 /
    #           旧 emitter 留在下拉 / 失效目标提交被拦 / 换通道重画且保留已填值
    #           + mqtt 保存 = ⑦⑧ 共 15 项，旧 6 项 => 净 +9）
    #        ③ climate 传感器 9 项（配传感器挂 3 订阅 / 无传感器上报时不伪造
    #           current_* / 无历史状态也挂订阅 / added 时读现值 / unknown 跳过
    #           26.0 生效 / 功率 ON 同步开机且不发红外 / on->on 忽略 /
    #           功率 OFF 同步关机 / 没配传感器零订阅）
    expected_total = 298
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
