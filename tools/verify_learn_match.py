#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""学习匹配引擎验证（不依赖 Home Assistant，可在开发机直接跑）。

    PY="C:/Users/ye_ca/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
    $PY tools/verify_learn_match.py [抽样数]      # 默认 120

做法：从码库随机抽 N 个真实帧 → 注入接收抖动（±8% 比例噪声 + 10% 概率丢前导
元素）→ 喂给 learn_match.match_timings()，按**帧级**判定命中。

判据是"找到那个码"而不是"找到同型号"：大量设备共用逐字节相同的帧（多家有线
机顶盒的 power 帧就是同一份），所以只要源帧指纹出现在候选帧里就算命中。
护栏：帧命中率 ≥ 90%；有第二键时，双键交集必须 100% 含真设备。

顺带输出性能证据：每个大类首次匹配（建特征缓存）与后续匹配的分档耗时。
退出码：全过 = 0，护栏破 = 1。
"""

from __future__ import annotations

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


_library_mod = load_module("_ir_hub_shim.library", os.path.join(COMPONENT, "library.py"))
_learn_mod = load_module("_ir_hub_shim.learn_match", os.path.join(COMPONENT, "learn_match.py"))
CodeLibrary = _library_mod.CodeLibrary
match_timings = _learn_mod.match_timings
frame_signature = _learn_mod.frame_signature
_similarity = _learn_mod._similarity

SAMPLE_N = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 120
FRAME_HIT_MIN = 0.90


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


def main() -> int:
    rng = random.Random(20260930)
    library = CodeLibrary()
    print(f"码库：{len(library.devices)} 设备 / {len(library.categories)} 类别")

    # 抽样池：每设备取第 1 个可用键（key_names 已把 power 排在最前）
    pool: list[tuple[dict, str, list[int]]] = []
    devices = [d for d in library.devices if library.key_names(d)]
    for device in rng.sample(devices, min(SAMPLE_N, len(devices))):
        key = library.key_names(device)[0]
        timings = library.get_timings(device["id"], key)
        if timings and len(timings) >= 8:
            pool.append((device, key, timings))
    assert len(pool) >= min(50, SAMPLE_N), f"抽样护栏：只抽到 {len(pool)} 个可用键"

    by_cat: dict[int, list] = {}
    for device, key, timings in pool:
        by_cat.setdefault(device["category"], []).append((device, key, timings))

    frame_hit = 0
    multi_hit = 0
    multi_total = 0
    common_sizes: list[int] = []
    total = len(pool)
    slow_cached = 0.0
    for category, group in sorted(by_cat.items()):
        cat_name = library.categories.get(category, category)

        # 首次匹配 = 建特征缓存（单独计时）
        t0 = time.perf_counter()
        match_timings(library, category, jitter(group[0][2], rng))
        build_s = time.perf_counter() - t0

        t0 = time.perf_counter()
        hit_cat = 0
        for device, key, timings in group:
            captured = jitter(timings, rng)
            matches = match_timings(library, category, captured, top_n=8)
            sig = frame_signature(timings)
            got = {frame_signature(m["frame"]) for m in matches}
            if sig in got:
                frame_hit += 1
                hit_cat += 1
            else:
                best = max(
                    (_similarity(captured, m["frame"]) for m in matches), default=0.0
                )
                src_score = _similarity(captured, timings)
                # 诊断：源帧的真实排名 + 同分簇大小（区分"算法错"与"数据本身歧义"）
                allm = match_timings(library, category, captured, top_n=None)
                rank = next(
                    (i + 1 for i, m in enumerate(allm)
                     if frame_signature(m["frame"]) == sig),
                    None,
                )
                top_sc = allm[0]["score"] if allm else 0.0
                tie = sum(1 for m in allm if top_sc - m["score"] < 1e-9) if allm else 0
                print(f"  MISS[{cat_name}] {device['name']}#{key} "
                      f"src_score={src_score:.2f} rank={rank}/{len(allm)} "
                      f"top={top_sc:.2f} tie={tie} best_in_top8={best:.2f}")

            # 双键累积排名（真实用法：多按几个键，按"每次都高分"的设备收敛）
            # ⚠️ 交集必须按 device_id —— 同一设备的 power 帧与 up 帧本就是不同的帧，
            #    按帧签名取交集恒为空。共享帧的成员要**列全**（一个帧可能有几百个
            #    设备在用），截断会把真设备挤出去。
            keys = library.key_names(device)
            if len(keys) >= 2:
                key2 = keys[1]
                timings2 = library.get_timings(device["id"], key2)
                if timings2 and len(timings2) >= 8:
                    multi_total += 1
                    press_maps = []
                    for press_timings in (timings, timings2):
                        ms = match_timings(
                            library, category, jitter(press_timings, rng), top_n=32,
                        )
                        scores: dict[int, float] = {}
                        for m in ms:
                            for did in m["device_ids"]:
                                scores[did] = max(scores.get(did, 0.0), m["score"])
                        press_maps.append(scores)

                    common = set(press_maps[0]) & set(press_maps[1])
                    if device["id"] in common:
                        multi_hit += 1
                        common_sizes.append(len(common))
                    else:
                        top3 = sorted(
                            common,
                            key=lambda did: -min(
                                press_maps[0].get(did, 0.0),
                                press_maps[1].get(did, 0.0),
                            ),
                        )[:3]
                        print(f"  MULTI-MISS[{cat_name}] {device['name']} "
                              f"|A|={len(press_maps[0])} |B|={len(press_maps[1])} "
                              f"公共={len(common)} top3={top3}")

        per_ms = (time.perf_counter() - t0) / max(len(group), 1) * 1000
        slow_cached = max(slow_cached, per_ms)
        print(f"  类别 {cat_name}: {len(group)} 键 | 缓存构建 {build_s:.2f}s | "
              f"缓存后 {per_ms:.0f} ms/键 | 帧命中 {hit_cat}/{len(group)}")

    print(f"\n帧命中率：{frame_hit}/{total} = {frame_hit / total:.1%}（护栏 ≥ {FRAME_HIT_MIN:.0%}）")
    if multi_total:
        print(f"双键交集命中：{multi_hit}/{multi_total} = {multi_hit / multi_total:.1%}（护栏 = 100%）")
        if common_sizes:
            avg = sum(common_sizes) / len(common_sizes)
            print(f"双键交集规模（精度）：均值 {avg:.1f} / 最大 {max(common_sizes)}"
                  f"（越小越准；1 = 完全锁定单只遥控）")
    print(f"缓存后最慢单键匹配：{slow_cached:.0f} ms")
    ok = frame_hit / total >= FRAME_HIT_MIN and (not multi_total or multi_hit == multi_total)
    print("结果：", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
