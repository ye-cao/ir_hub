#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""空调「按遥控器电源键配对」的匹配引擎验证（不依赖 Home Assistant）。

    PY="C:/Users/ye_ca/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
    $PY tools/verify_learn_ac.py [抽样数]      # 默认 120

做法：从空调码库随机抽 N 个真实 bin → 取它真实能生成的某一帧 → 注入接收抖动
（±8% 比例噪声 + 10% 概率丢前导元素）→ 喂给 learn_match_ac.match_ac()。

与按键类（verify_learn_match.py）的判定口径不同，这里要过三道：

  1. **结构指纹不变式**：同一台空调的所有状态帧，「帧长 + 首个 mark」必须恒定。
     两级过滤的第一级就建立在这条上，若被打破，预筛会漏掉真型号。
  2. **bin 命中**：候选里出现了抽样那个 bin 本身（型号级命中）。
  3. **内容命中（md5）**：候选里出现了与真 bin **逐字节相同**的 bin。
     这一条才是"用户能不能配成"的真判据 —— 码库里同协议族的 bin 有大量近似
     型号（top1 常常是隔壁型号，分数 0.99x），但内容相同的 bin 一定存在。

另外按 UI 实际推荐的用法（按电源键，再按一个温度/风速键）跑一次**两键取交集**：
同一 bin 的另一个状态帧也必须把它找出来，否则交集会把真型号剔掉。

护栏：内容命中 top-N = 100%、top1 ≥ 80%、结构不变式违反数 = 0、
两键交集 100% 含真型号、单次匹配 ≤ 8 s（粗检，防数量级退化）。
退出码：全过 = 0，护栏破 = 1。
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import random
import sys
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
COMPONENT = os.path.join(os.path.dirname(HERE), "custom_components", "ir_hub")

shim = types.ModuleType("_ir_hub_shim")
shim.__path__ = [COMPONENT]
sys.modules["_ir_hub_shim"] = shim


def load_module(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_ac_mod = load_module("_ir_hub_shim.ac_library", os.path.join(COMPONENT, "ac_library.py"))
load_module("_ir_hub_shim.learn_match", os.path.join(COMPONENT, "learn_match.py"))
_learn_ac = load_module(
    "_ir_hub_shim.learn_match_ac", os.path.join(COMPONENT, "learn_match_ac.py")
)
AcLibrary = _ac_mod.AcLibrary
iter_frames = _ac_mod.iter_frames
match_ac = _learn_ac.match_ac
bin_structures = _learn_ac.bin_structures

SAMPLE_N = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 120
TOP_N = 8
TWO_KEY_N = 40            # 两键交集统计只跑前 N 个（每次 = 2 次全库匹配，很贵）
TOP1_MIN = 0.80
# 耗时护栏是**粗检**（防"某处退化成全库枚举"这类数量级问题），不是性能指标 ——
# 本机干净跑最大 ~4.2 s，但如果同时有别的重进程（实测并发时量到 5.5 s）就会抖动，
# 所以留足余量，别让机器负载把报告刷成 FAIL。
MATCH_MS_MAX = 8000


def jitter(timings: list[int], rng: random.Random) -> list[int]:
    """模拟接收端抖动：±8% 比例噪声；10% 概率丢 1 个前导元素（保留符号）。"""
    out = []
    for v in timings:
        j = int(v * (1.0 + rng.uniform(-0.08, 0.08)))
        if j == 0:
            j = 1 if v >= 0 else -1
        out.append(j)
    if rng.random() < 0.10 and len(out) > 10:
        out = out[1:]
    return out


def md5(raw: bytes) -> str:
    return hashlib.md5(raw).hexdigest()


def main() -> int:
    rng = random.Random(20260930)
    ac_library = AcLibrary()
    print(f"空调码库：{ac_library.brand_count} 品牌 / {ac_library.device_count} 型号")

    # 结构指纹构建耗时（第一次走真算，后面走缓存）
    t0 = time.perf_counter()
    structs = bin_structures(ac_library)
    build_s = time.perf_counter() - t0
    print(f"结构指纹：{len(structs)} 个 bin，构建 {build_s:.2f}s")

    # ---- 不变式 1：同一 bin 的所有状态帧，帧长 + 首个 mark 恒定 ----
    violations = []
    for bin_name in ac_library.bin_names():
        try:
            seen = set()
            for _state, frame in iter_frames(ac_library.read_raw(bin_name)):
                count, first, _total = _learn_ac.capture_features(frame)
                seen.add((count, first))
            if len(seen) > 1:
                violations.append((bin_name, sorted(seen)[:3]))
        except Exception as exc:                      # noqa: BLE001 - 探针要报全
            violations.append((bin_name, repr(exc)))
    print(f"结构不变式：违反 {len(violations)} 个 bin"
          + (f"（前 3：{violations[:3]}）" if violations else ""))

    # ---- 抽样比对 ----
    pool: list[tuple[str, str, list[int], list[int]]] = []
    for bin_name in rng.sample(ac_library.bin_names(), min(SAMPLE_N, len(structs))):
        try:
            frames = list(iter_frames(ac_library.read_raw(bin_name)))
        except Exception:                             # noqa: BLE001
            continue
        if not frames:
            continue
        state, frame = frames[rng.randrange(len(frames))]
        pool.append((bin_name, state, frame, frames))
    assert len(pool) >= min(50, SAMPLE_N), f"抽样护栏：只抽到 {len(pool)} 个可用 bin"

    raws = {name: md5(ac_library.read_raw(name)) for name in ac_library.bin_names()}
    bin_hit1 = bin_hitn = content_hit1 = content_hitn = state_hit = 0
    cand_sizes: list[int] = []
    times: list[float] = []
    struct_miss = 0
    for bin_name, state, frame, _frames in pool:
        captured = jitter(frame, rng)
        t0 = time.perf_counter()
        matches = match_ac(ac_library, captured, top_n=TOP_N)
        times.append((time.perf_counter() - t0) * 1000)

        # 结构预筛是否把**真 bin** 留下（第一级过滤器不能漏；第二个变体对齐"丢首元素"）
        cand_variants = [_learn_ac.capture_features(captured)]
        if len(captured) > 9:
            cand_variants.append(_learn_ac.capture_features(captured[1:]))
        true_struct = next((s for s in structs if s[0] == bin_name), None)
        if true_struct is not None and not _learn_ac._prefilter_pass(
            cand_variants, true_struct[1], true_struct[2], 0
        ):
            struct_miss += 1

        names = [m["bin"] for m in matches]
        cand_sizes.append(len(names))
        truth_md5 = raws[bin_name]
        if names[:1] == [bin_name]:
            bin_hit1 += 1
        if bin_name in names:
            bin_hitn += 1
        if names and raws.get(names[0]) == truth_md5:
            content_hit1 += 1
        content1 = [n for n in names if raws.get(n) == truth_md5]
        if content1:
            content_hitn += 1
            got = next(m for m in matches if m["bin"] in content1)
            if got["state"] == list(state):
                state_hit += 1

    total = len(pool)
    times.sort()
    avg_ms = sum(times) / total
    p95_ms = times[int(total * 0.95) - 1]
    max_ms = times[-1]

    print(f"\n抽样 {total} 个真实帧（注入 ±8% 抖动）")
    print(f"  bin 命中： top1 {bin_hit1}/{total} = {bin_hit1 / total:.1%}"
          f" | top{TOP_N} {bin_hitn}/{total} = {bin_hitn / total:.1%}")
    print(f"  内容命中：top1 {content_hit1}/{total} = {content_hit1 / total:.1%}"
          f" | top{TOP_N} {content_hitn}/{total} = {content_hitn / total:.1%}"
          f"   ← 真判据")
    print(f"  内容命中项的 state 一致：{state_hit}/{content_hitn or 1}"
          f" = {state_hit / (content_hitn or 1):.1%}")
    print(f"  候选规模：均值 {sum(cand_sizes) / total:.1f} / 最大 {max(cand_sizes)}")
    print(f"  单次匹配：均值 {avg_ms:.0f} ms | P95 {p95_ms:.0f} ms | 最大 {max_ms:.0f} ms")
    print(f"  结构预筛漏掉真型号：{struct_miss}/{total}")

    # ---- 两键取交集（UI 推荐的路径：电源键 + 温度/风速键）----
    # 第二次按同一个遥控的另一类键 ⇒ 同一 bin 的**另一个状态帧**。
    # 取交集后真 bin 必须还在（交集的规模就是"用户要翻几个候选"）。
    two_n = min(total, TWO_KEY_N)
    inter_hit = 0
    two_tried = 0
    two_skipped = 0
    inter_sizes: list[int] = []
    for bin_name, _state, frame, frames in pool[:two_n]:
        others = [f for _s, f in frames if f != frame]
        if not others:
            # 该 bin 所有状态帧逐字节相同（只有一个可用状态）⇒ 没有第二键可用，
            # 记"跳过"而不是记"失败"。
            two_skipped += 1
            continue
        two_tried += 1
        sets = []
        for press in (frame, others[len(others) // 2]):
            ms = match_ac(ac_library, jitter(press, rng), top_n=None)
            sets.append({m["bin"] for m in ms})
        common = sets[0] & sets[1]
        truth_md5 = raws[bin_name]
        if any(raws.get(n) == truth_md5 for n in common):
            inter_hit += 1
            inter_sizes.append(len(common))
        else:
            print(f"  2KEY-MISS {bin_name} |A|={len(sets[0])} |B|={len(sets[1])} "
                  f"公共={len(common)}")
    print(f"  两键交集含真型号：{inter_hit}/{two_tried}"
          + (f" = {inter_hit / two_tried:.1%}" if two_tried else "")
          + (f"（另有 {two_skipped} 个 bin 只有单一状态，无第二键可用，已跳过）"
             if two_skipped else "")
          + (f" | 交集规模均值 {sum(inter_sizes) / len(inter_sizes):.1f}"
             f" / 最大 {max(inter_sizes)}" if inter_sizes else ""))

    checks = {
        f"结构不变式违反 0（实际 {len(violations)}）": not violations,
        f"内容命中 top{TOP_N} = 100%（实际 {content_hitn / total:.1%}）":
            content_hitn == total,
        f"内容命中 top1 ≥ {TOP1_MIN:.0%}（实际 {content_hit1 / total:.1%}）":
            content_hit1 / total >= TOP1_MIN,
        f"结构预筛 0 漏（实际 {struct_miss}）": struct_miss == 0,
        f"两键交集 100% 含真型号（实际 {inter_hit}/{two_tried}）": inter_hit == two_tried,
        f"单次匹配 ≤ {MATCH_MS_MAX} ms（实际最大 {max_ms:.0f}）": max_ms <= MATCH_MS_MAX,
    }
    print()
    for label, passed in checks.items():
        print(f"  [{' OK ' if passed else 'FAIL'}] {label}")
    ok = all(checks.values())
    print("结果：", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
