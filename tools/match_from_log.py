"""把 ESPHome 日志里收到的红外原始时序，在本机拿**真码库**跑一遍匹配。

**为什么需要这个**：改一次集成 / 重启一次 HA 才能看一次匹配结果，迭代太慢
（用户原话："hacs 每次都要重启，你这样搞一天要重启很多次"）。
这个脚本把"识别"这一步从 HA 搬到开发机：**贴日志 → 出候选与结论**，不动 HA。

## 日志从哪来

接收端的 `dump` 里带上 `raw`（必须**显式**写 `dump: raw`）就会打印：

    [I][remote.raw:012]: Received Raw: 9000, -4500, 560, -1690, ...
    [I][remote.raw:012]:   560, -1690, 560, -56560

超过 256 字节自动折行，**续行是同 tag 的 "  <num>, <num>..."**。
⚠️ 该 dumper 的循环是 `for (i = 0; i < size - 1; i++)` ⇒ **会丢掉帧的最后一个元素**
（通常是结尾的 idle）。匹配引擎的 `(0, 0, 0, 1)` 对齐方向正是为这种情况准备的，
所以少这一个元素不影响识别 —— 但分析时要知道。

⚠️ **`dump: all` 不会给出 raw**。`RawDumper::is_secondary()` 返回 true，而
`call_dumpers_()` 是"先跑 primary，**全部 primary 返回 false 才跑 secondary**"，
协议 dumper（pronto 等）只要解出一帧就返回 true ⇒ secondary 永远轮不到。
所以 `dump: all` 的日志里只有 `remote.pronto`，本脚本也支持直接吃 pronto
（会把十六进制字反解回 µs，见 `decode_pronto`）。**真想看 raw 就把 `dump:` 改成 `raw`。**

## 用法

    PY="C:/Users/ye_ca/.workbuddy/binaries/python/envs/default/Scripts/python.exe"

    $PY tools/match_from_log.py capture.txt      # 从日志文件
    $PY tools/match_from_log.py -                # 从 stdin 贴（推荐）
    $PY tools/match_from_log.py --text "Received Raw: 9000, -4500, ..."
    $PY tools/match_from_log.py --selftest       # 自测：真帧 -> 渲染成 remote.raw -> 回灌

    --category auto|KEY|ac   默认 auto（按键类全 16 大类；帧长 ≥150 再附带空调）
    --top N                  默认 10
    --json                   机器可读
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ir_hub_loader as loader  # noqa: E402

# ---------------------------------------------------------------- 日志解析

RAW_TAG = re.compile(r"remote\.raw[^\]]*\]\s*:?\s*(.*)$")
PRONTO_TAG = re.compile(r"remote\.pronto[^\]]*\]\s*:?\s*(.*)$")
NUM = re.compile(r"-?\d+")
HEX4 = re.compile(r"\b[0-9A-Fa-f]{4}\b")
RAW_MARK = "Received Raw:"
PRONTO_MARK = "Received Pronto: data="

# Pronto 换算常量（照抄 ESPHome 2026.9.0 `pronto_protocol.cpp`，别自己另发明）
REFERENCE_FREQUENCY = 4145146
MARK_EXCESS_MICROS = 20

# 匹配引擎的分档（与集成侧一致，见 config_flow._LEARN_*）
TRUST = 0.85
WEAK = 0.75
AC_MIN_FRAME = 150  # 帧长 ≥ 此值才顺手跑空调库（本库空调帧中位 180 / 最小 51）

# 单元素相对误差 ≥ 此值 = "离群"。空调帧里一个离群几乎总是**一个 bit 对不上**
# （短 space 548 ↔ 长 space 1690，比值 3 倍，e≈0.68），不是抖动。
# 实测：干净捕获 0 个、同族另一型号 3~15 个、捕获被记坏则**均误**先炸（>12%）。
OUTLIER_E = 0.30


def _err_profile(captured: list[int], lib: list[int]) -> dict:
    """逐元素误差画像：分数被单个最差元素主导，只有画像能说清"差在哪"。

    为什么要有这个：`_similarity = 1 - 均误 - 0.20*最差`，而**最差那一项会饱和** ——
    1 个 bit 对不上（最差 0.68）和 6 个 bit 对不上（最差还是 0.68）得分几乎一样
    （0.81 vs 0.82）。所以：

        均误低 + 离群少(0~1)  → 真匹配
        均误低 + 离群多(3~15) → **同协议族的另一个型号**（结构一模一样，差几个 bit）
        均误高(>12%)          → **捕获被接收端记坏了**（丢脉冲/截断）

    （232 段空调帧实测：库帧自比 0.945、离群 0；用户真机捕获 0.821、离群 5。）
    """
    n = min(len(captured), len(lib))
    errs: list[float] = []
    for i in range(n):
        va, vb = captured[i], lib[i]
        denom = abs(va) if abs(va) > abs(vb) else abs(vb)
        if denom < 1:
            denom = 1
        errs.append(abs(va - vb) / denom)
    if not errs:
        return {"n": 0, "mean": 0.0, "worst": 0.0, "outliers": 0, "extra": 0}
    return {
        "n": n,
        "mean": sum(errs) / len(errs),
        "worst": max(errs),
        "outliers": sum(1 for e in errs if e >= OUTLIER_E),
        "extra": abs(len(captured) - len(lib)),
    }


def _bin_profile(ac_mod, ac_library, bin_name: str, captured: list[int],
                 learn_match) -> dict:
    """某个 bin 里最接近捕获的那一帧的误差画像。"""
    try:
        raw = ac_library.read_raw(bin_name)
    except OSError:
        return {}
    best = None
    for _state, frame in ac_mod.iter_frames(raw):
        score = learn_match._similarity(captured, frame)
        if best is None or score > best[0]:
            best = (score, frame)
    if best is None:
        return {}
    return _err_profile(captured, best[1])


def decode_pronto(words: list[str]) -> list[int] | None:
    """Pronto 十六进制字 → 带符号时序（µs）。**严格照 ESPHome 的编码算法反解**。

    正向（`compensate_and_dump_sequence_` + `dump_duration_`）：

        timebase = (1e6 * freq_code + REFERENCE_FREQUENCY/2) / REFERENCE_FREQUENCY   # 38000Hz -> 26
        mark  : w = (L - 20 + tb/2) / tb      =>  L ≈ w*tb + 20
        space : w = (S + 20 + tb/2) / tb      =>  S ≈ w*tb - 20

    ⚠️ 两个容易搞错的地方：
      1. `decode()` 里 frequency **硬编码 38000**（不是量出来的），所以头两字恒定；
      2. mark 多减 20µs、space 多加 20µs 是**发射补偿**，反解时要还回去，否则
         每个元素都带一个固定偏差。
    """
    if len(words) < 6:
        return None
    try:
        values = [int(word, 16) for word in words]
    except ValueError:
        return None
    if values[0] not in (0x0000, 0x0100):
        return None
    freq_code = values[1]
    if not freq_code:
        return None
    timebase = (1_000_000 * freq_code + REFERENCE_FREQUENCY // 2) // REFERENCE_FREQUENCY
    data_words = values[4:]
    pairs = values[2]
    if pairs and len(data_words) > pairs * 2:
        data_words = data_words[: pairs * 2]
    if not data_words:
        return None
    out: list[int] = []
    for index, word in enumerate(data_words):
        duration = word * timebase
        if index % 2 == 0:  # mark
            out.append(duration + MARK_EXCESS_MICROS)
        else:  # space
            out.append(-(duration - MARK_EXCESS_MICROS))
    return out


def pronto_frequency_khz(words: list[str]) -> int | None:
    """头字里的载波频率（仅用于展示，不参与匹配）。"""
    try:
        code = int(words[1], 16)
    except (IndexError, ValueError):
        return None
    return ((REFERENCE_FREQUENCY // code) + 500) // 1000 if code else None


def _collect(text: str, tag: re.Pattern, mark: str, parse):
    """通用收集器：按 tag 抓取标记块 + 其续行，交给 parse 转成时序。"""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in text.splitlines():
        match = tag.search(line)
        if match:
            body = match.group(1)
            if mark in body:  # 新的一块
                if current:
                    blocks.append(current)
                current = parse(body.split(mark, 1)[1])
            elif current is not None:  # 同 tag 的续行
                current.extend(parse(body))
            continue
        # 别的行：空行不算断（日志里常有），有内容就结束当前块
        if current is not None and line.strip():
            blocks.append(current)
            current = None
    if current:
        blocks.append(current)
    return blocks


def parse_frames(text: str) -> tuple[list[list[int]], str]:
    """从日志文本里抽出所有帧，返回 (帧列表, 来源说明)。

    优先认 `remote.raw`（最精确）；没有就退到 `remote.pronto`（**默认 `dump: all`
    时只有它**，因为 raw 是 secondary dumper、主 dumper 成功时永不执行）；
    再没有就把整段当数字数组。
    """
    raw_blocks = _collect(text, RAW_TAG, RAW_MARK, lambda s: [int(n) for n in NUM.findall(s)])
    raw_frames = [b for b in raw_blocks if len(b) >= 2]
    if raw_frames:
        return raw_frames, "remote.raw"

    hex_blocks = _collect(text, PRONTO_TAG, PRONTO_MARK, lambda s: HEX4.findall(s))
    pronto_frames = [
        frame for frame in (decode_pronto(b) for b in hex_blocks) if frame
    ]
    if pronto_frames:
        return pronto_frames, "remote.pronto（已反解为 µs）"

    blob = [int(n) for n in NUM.findall(text)]  # 退路：用户只贴了数字
    if len(blob) >= 2:
        return [blob], "裸数字"
    return [], "无"


def render_raw_log(frames: list[list[int]], drop_last: bool = True) -> str:
    """把帧渲染成 `remote.raw` 的日志文本 —— 只为自测（复刻折行 + 丢尾元素）。"""
    out: list[str] = []
    for index, frame in enumerate(frames, start=1):
        values = frame[:-1] if drop_last else frame
        chunks: list[str] = []
        line = ""
        for i, value in enumerate(values):
            piece = f"{value}, " if i + 1 < len(values) else f"{value}"
            if len(line) + len(piece) > 250 and line:
                chunks.append(line.rstrip())
                line = "  "
            line += piece
        if line.strip():
            chunks.append(line.rstrip())
        head = f"[I][remote.raw:{index:03d}]: Received Raw: "
        out.append(head + chunks[0])
        out.extend(f"[I][remote.raw:{index:03d}]: {chunk}" for chunk in chunks[1:])
    return "\n".join(out)


# ---------------------------------------------------------------- 匹配

def flatten(hit: dict) -> dict:
    """`match_timings` 的品牌/型号/键在 `hit["devices"][0]` 里，这里拍平供展示。

    ⚠️ 不是顶层字段 —— 曾经在顶层取 `hit["brand_name"]` 直接 KeyError。
    """
    device = hit["devices"][0] if hit.get("devices") else {}
    return {
        "score": hit["score"],
        "category": hit.get("category", ""),
        "device_id": device.get("device_id"),
        "brand_name": device.get("brand_name", ""),
        "model": device.get("model", ""),
        "key": device.get("key", ""),
        "n_devices": hit.get("n_devices", 0),
    }


def run_key(library, learn_match, capture: list[int], top_n: int):
    """按键类：逐大类匹配后合并排序。返回 (合并 top_n, 每个大类的摘要)。"""
    rows: list[dict] = []
    per_category: list[tuple[str, int, float]] = []
    for cid in sorted(library.categories):
        name = library.categories[cid]
        hits = learn_match.match_timings(library, cid, capture, top_n)
        per_category.append(
            (name, len(hits), hits[0]["score"] if hits else 0.0)
        )
        for hit in hits:
            hit["category"] = name
            rows.append(flatten(hit))
    rows.sort(key=lambda item: -item["score"])
    return rows[:top_n], per_category


def ac_label(ac_library, bin_name: str) -> str:
    """空调候选名：多品牌共用码**不冒认**单一品牌（与集成侧口径一致）。"""
    model = bin_name.removesuffix(".bin").replace("irda_new_ac_", "")
    brands = ac_library.brands_of(bin_name)
    if len(brands) == 1:
        return f"{brands[0]} 空调 {model}"
    return f"空调 {model}（{len(brands)} 个品牌共用）"


# ---------------------------------------------------------------- 结论

def verdict_key(top: list[dict]) -> str:
    if not top:
        return (
            "结论：按键类码库里**一个候选都没有**（连 0.62 的门槛都没过）。\n"
            "      这只遥控的帧在库里找不到任何近邻 —— 基本可以断定不在库里。"
        )
    best, count = top[0]["score"], len(top)
    if best >= TRUST and count >= 5:
        return (
            f"结论：**库里很可能有这只遥控** —— 候选 {count} 个、最高 {best:.3f}。\n"
            f"      第 1 个候选基本就是它（{top[0]['category']} / "
            f"{top[0]['brand_name']} {top[0]['model']} / 键 {top[0]['key']}）。"
        )
    if best >= WEAK:
        return (
            f"结论：**偏低但有可能** —— 候选 {count} 个、最高 {best:.3f}。\n"
            "      可能是同协议族的近似型号：换一个键（如音量+）再抓一次，"
            "两键取交集能把候选收敛掉大半。"
        )
    return (
        f"结论：**库里没有这只遥控** —— 只有 {count} 个候选、最高 {best:.3f}"
        "（那是最近邻，不是匹配）。\n"
        "      请改走「手动选择码库」按品牌型号挑，或换能自学习的方案。"
    )


def verdict_ac(survivors: int, top: list[dict] | None = None) -> str:
    if not survivors:
        return ""
    if survivors <= 3:
        return (
            f"结论：空调库里只有 {survivors} 个型号的**帧形状**对得上 ⇒ "
            "**不在码库里**，重配对救不回来（改走手动选型号）。"
        )
    head = (
        f"结论：空调库里有 {survivors} 个型号的帧形状对得上（结构同族）。"
        "**别只看分数** —— 空调帧长，`1 - 均误 - 0.20*最差` 里那一项会饱和，"
        "差 1 个 bit 和差 6 个 bit 都是 0.81~0.82。看**均误 / 离群**："
    )
    if not top:
        return head
    best = top[0]
    outliers = best.get("outliers")
    mean = best.get("mean")
    if outliers is None or mean is None:
        return head
    if outliers <= 1:
        tail = (
            f"\n  ★ 第 1 候选（{best['label']}）只有 {outliers} 个元素对不上、"
            f"均误 {mean * 100:.1f}% ⇒ **几乎肯定就是它**，直接按它配对实测。"
        )
    elif mean <= 0.08:
        tail = (
            f"\n  ⚠ 第 1 候选（{best['label']}）均误只有 {mean * 100:.1f}%（结构完全同族），"
            f"但有 **{outliers} 个元素对不上** ⇒ 是**同族的另一个型号**，"
            "这只遥控的精确帧不在库里。\n"
            "     处置：从候选里按**品牌**挑（海尔系就在前几名），用两段式实测确认；"
            "别指望再采一次会变高，差异是设备本身的 bit 差，不是噪声。"
        )
    else:
        tail = (
            f"\n  ⚠ 第 1 候选均误 {mean * 100:.1f}% 偏高（>8%）⇒ 更像**捕获被记坏了**"
            "（丢脉冲/被 50µs 滤波削掉/信号弱），先换位置或换 ESP32 接收端再采一次。"
        )
    return head + tail


# ---------------------------------------------------------------- 主流程

def _burst_prefixes(frame: list[int], min_len: int = 20) -> list[list[int]]:
    """候选捕获 = 原帧 + 在**内部长 gap** 处截断出的各个前缀。

    为什么需要：很多遥控器把同一个「状态帧」**连发 N 遍**，而接收端的
    `Signal is done after 10000 us`（默认 10 ms）会把间隔 <10 ms 的重复
    **合并成一帧**。于是同一次按键可能捕成 200 段、也可能 300 段，而码库里
    存的是固定遍数。帧长预筛只容差 ±3 段 ⇒ **多合并一遍就会整库被灭掉**，
    症状与"库外遥控"一模一样。

    实测（2026-09-30，`irda_new_ac_25065` 家族，200 段库帧）：
        原样 300 段 → 幸存 2、最高 0.632（判"不在码库"，**错**）
        截到 200 段 → 幸存 27、最高 0.866（★ 就是它）
    两个 burst 块的 bit 与库内 ('on','cool','auto',26) / ('off',) **逐位相同**。

    所以这里把「原帧」与「截到第 1/2/… 个 burst」都当候选，谁分高用谁。
    """
    cands = [frame]
    n = len(frame)
    # 跳过 index 0/1：那是引导码的 mark / space（引导 space 也常 >3000µs）
    for i in range(2, n - 1):
        if frame[i] <= -3000 and i + 1 >= min_len:   # 末尾那个长 gap 是 idle，不在范围内
            cands.append(frame[: i + 1])
    return cands


def _best_score(result: dict) -> float:
    """结果里所有候选中最高分 —— 用来挑"该用哪个候选捕获"。"""
    best = 0.0
    for section in result.get("sections", []):
        for hit in section.get("top", []):
            score = hit.get("score")
            if score and score > best:
                best = score
    return best


_LIB_CACHE: dict = {}


def _get_library():
    """按键库实例（进程内复用 —— 每个候选都重建索引会白费 3 倍时间）。"""
    lib = _LIB_CACHE.get("key")
    if lib is None:
        lib = loader.load("library").CodeLibrary()
        _LIB_CACHE["key"] = lib
    return lib


def _get_ac_library():
    """空调库实例（同上）。"""
    lib = _LIB_CACHE.get("ac")
    if lib is None:
        lib = loader.load("ac_library").AcLibrary()
        _LIB_CACHE["ac"] = lib
    return lib


def _analyze_one(cap: list[int], category: str, top_n: int) -> dict:
    """对**单个**候选捕获跑按键类 + 空调两个库，返回结果（不打印）。"""
    learn_match = loader.load("learn_match")
    features = learn_match.capture_features(cap)
    result: dict = {
        "used": {"count": features[0], "first_mark": features[1], "total": features[2]},
        "sections": [],
    }
    want_ac = category == "ac" or (category == "auto" and features[0] >= AC_MIN_FRAME)
    want_key = category != "ac"

    if want_key:
        library = _get_library()
        top, per_category = run_key(library, learn_match, cap, top_n)
        result["sections"].append({
            "name": "按键类",
            "top": top,
            "per_category": per_category,
            "n_devices": len(library.devices),
            "n_categories": len(library.categories),
            "verdict": verdict_key(top),
        })

    if want_ac:
        ac_mod = loader.load("ac_library")
        ac_library = _get_ac_library()
        ac_match = loader.load("learn_match_ac")
        survivors = ac_match.structure_survivors(ac_library, cap)
        hits = ac_match.match_ac(ac_library, cap, top_n)
        # 补"误差画像"：分数会饱和，均误/离群才分得清"真匹配 / 同族另一型号 / 记坏"
        for hit in hits:
            hit.update(_bin_profile(ac_mod, ac_library, hit["bin"], cap, learn_match))
        ac_top = [
            {
                "bin": hit["bin"],
                "score": hit["score"],
                "label": ac_label(ac_library, hit["bin"]),
                "n_states": hit.get("n_states"),
                "mean": hit.get("mean"),
                "worst": hit.get("worst"),
                "outliers": hit.get("outliers"),
            }
            for hit in hits
        ]
        result["sections"].append({
            "name": "空调",
            "survivors": survivors,
            "top": ac_top,
            "n_brands": ac_library.brand_count,
            "n_models": ac_library.device_count,
            "verdict": verdict_ac(survivors, ac_top),
        })
    return result


def analyze(text: str, category: str, top_n: int, quiet: bool = False) -> dict:
    frames, source = parse_frames(text)
    if not frames:
        return {
            "error": (
                "没有从输入里解析出任何帧。\n"
                "支持两种日志行：\n"
                "  [I][remote.raw:012]: Received Raw: 9000, -4500, 560, -1690, ...\n"
                "  [I][remote.pronto:231]: Received Pronto: data=\n"
                "  [I][remote.pronto:239]: 0000 006D 0096 0000 ...\n"
                "⚠️ `dump: all` 时**只会**出现 pronto（raw 是 secondary dumper，"
                "主 dumper 成功时永远不会执行）—— 想直接看 raw 就把 `dump:` 改成 `raw`。"
            )
        }

    longest = max(frames, key=len)
    cands = _burst_prefixes(longest)
    tried = []
    for cand in cands:
        res = _analyze_one(cand, category, top_n)
        tried.append((_best_score(res), cand, res))
    # 平分时保留先出现的（= 原帧），所以比较用 > 而不是 >=
    best_pair = tried[0]
    for pair in tried[1:]:
        if pair[0] > best_pair[0]:
            best_pair = pair
    result = best_pair[2]

    used = result["used"]
    result["frames_found"] = len(frames)
    result["frame_lengths"] = [len(f) for f in frames]
    result["candidates_tried"] = [len(c) for c in cands]

    if not quiet:
        print(f"解析到 {len(frames)} 帧，段数 {'/'.join(str(len(f)) for f in frames)}")
        print(
            f"用最长的那帧做匹配：{used['count']} 段 / 首脉冲 {used['first_mark']} µs / "
            f"全长 {used['total']} µs"
        )
        if len(cands) > 1:
            print(
                f"  ⚠ 本帧内部有长 gap ⇒ 候选段数 {[len(c) for c in cands]}"
                "（接收端把多次重复的 burst 合并成一帧时会这样）——"
                f" 已逐个试过，**采用 {used['count']} 段那个**（分数最高）。"
            )

    for section in result["sections"]:
        if quiet:
            break
        if section["name"] == "按键类":
            print(
                f"\n=== 按键类码库（{section['n_devices']} 设备 / "
                f"{section['n_categories']} 大类）==="
            )
            for rank, hit in enumerate(section["top"], start=1):
                shared = (
                    f"  (同帧设备 {hit['n_devices']} 个)"
                    if hit["n_devices"] > 1
                    else ""
                )
                print(
                    f"[{rank}] {hit['score']:.3f}  {hit['category']}  "
                    f"{hit['brand_name']} {hit['model']} · 键 {hit['key']}{shared}"
                )
            if not section["top"]:
                print("（无候选）")
            print()
            print(section["verdict"])
        else:
            print(
                f"\n=== 空调状态码库（{section['n_brands']} 品牌 / "
                f"{section['n_models']} 型号）==="
            )
            print(
                f"结构预筛幸存 {section['survivors']} 个 bin → "
                f"打分后候选 {len(section['top'])} 个"
            )
            print("  分数 / 均误（平均相对误差）/ 离群（单元素误差≥30% 的段数 ≈ 差几个 bit）")
            for rank, hit in enumerate(section["top"], start=1):
                mean = hit["mean"]
                if mean is None:
                    print(f"[{rank}] {hit['score']:.3f}  {hit['label']}")
                else:
                    print(
                        f"[{rank}] {hit['score']:.3f}  {hit['label']}  "
                        f"均误 {mean * 100:.1f}%  离群 {hit['outliers']}"
                    )
            if not section["top"]:
                print("（无候选）")
            print()
            print(section["verdict"])

    if not quiet and category == "auto" and not any(
        s["name"] == "空调" for s in result["sections"]
    ):
        print(
            f"\n（帧长 {used['count']} 段，看着像按键类；空调状态帧通常更长 —— "
            "要一并试空调库请加 `--category ac`）"
        )

    return result


def encode_pronto_ref(frame: list[int], freq_code: int = 0x006D) -> str:
    """按 ESPHome 的算法把时序编成 Pronto 十六进制字 —— 只为自测（正向）。

    与 `decode_pronto` 成对，用来验证我的反解没有搞错 timebase / 发射补偿。
    freq_code 默认 0x006D = 109（`REFERENCE_FREQUENCY // 38000`，与日志里的头两字一致）。
    """
    timebase = (1_000_000 * freq_code + REFERENCE_FREQUENCY // 2) // REFERENCE_FREQUENCY
    words = [0x0000, freq_code, (len(frame) + 1) // 2, 0]
    for value in frame:
        duration = (
            value - MARK_EXCESS_MICROS if value > 0 else -value + MARK_EXCESS_MICROS
        )
        words.append((duration + timebase // 2) // timebase)
    return " ".join(f"{word:04X}" for word in words)


def render_pronto_log(frames: list[list[int]], chunk_words: int = 46) -> str:
    """把帧渲染成 `remote.pronto` 的日志块（含折行），用于自测解析器。"""
    out: list[str] = []
    for index, frame in enumerate(frames, start=1):
        words = encode_pronto_ref(frame).split()
        out.append(f"[I][remote.pronto:{index:03d}]: Received Pronto: data=")
        for start in range(0, len(words), chunk_words):
            chunk = " ".join(words[start : start + chunk_words]) + " "
            out.append(f"[I][remote.pronto:239]: {chunk}")
    return "\n".join(out)


def selftest() -> int:
    """自测：真帧 → 渲染成 remote.raw / remote.pronto 日志 → 解析回来 → 跑匹配。"""
    loader.install()
    learn_match = loader.load("learn_match")
    library = loader.load("library").CodeLibrary()

    fails = 0
    device_id = 47  # TCL电视-1
    key = library.key_names(library.get_device(device_id))[0]
    timings = library.get_timings(device_id, key)
    assert timings

    text = render_raw_log([timings])
    frames, source = parse_frames(text)
    expect = timings[:-1]  # dumper 丢尾元素
    ok = len(frames) == 1 and frames[0] == expect and source == "remote.raw"
    print(f"[{'OK' if ok else 'FAIL'}] remote.raw 折行 + 丢尾元素后仍能原样解析回来"
          f"（{len(timings)} 段 -> 解析 {len(frames[0]) if frames else 0} 段）")
    fails += 0 if ok else 1

    verdict = learn_match.match_timings(
        library, library.get_device(device_id)["category"], frames[0], 8
    )
    hit = next(
        (i for i, row in enumerate(verdict) if device_id in row["device_ids"]), None
    )
    ok = hit is not None and hit < 8 and verdict[hit]["score"] >= TRUST
    print(
        f"[{'OK' if ok else 'FAIL'}] 丢尾元素后仍能识别出真设备 "
        f"(TCL电视-1 排第 {(hit + 1) if hit is not None else '—'}，"
        f"最高分 {verdict[0]['score']:.3f}）"
    )
    fails += 0 if ok else 1

    # 多帧 + 噪声行混在一起也要能分开
    other = library.get_timings(device_id, library.key_names(library.get_device(device_id))[-1])
    noisy = "noise line\n" + render_raw_log([timings, other]) + "\n[I][wifi]: connected\n"
    frames2, _ = parse_frames(noisy)
    ok = len(frames2) == 2 and frames2[0] == timings[:-1] and frames2[1] == other[:-1]
    print(f"[{'OK' if ok else 'FAIL'}] 多帧 + 其他组件的日志行混排时能正确切分"
          f"（得到 {len(frames2)} 帧）")
    fails += 0 if ok else 1

    # Pronto 往返：量化误差必须 ≤ timebase/2（26/2 = 13µs），否则说明
    # timebase 或 ±20µs 发射补偿有一处搞反了。
    pronto_text = render_pronto_log([timings])
    pframes, psource = parse_frames(pronto_text)
    if len(pframes) == 1:
        err = max(abs(a - b) for a, b in zip(pframes[0], timings))
        ok = len(pframes[0]) == len(timings) and err <= 14
        detail = f"{len(pframes[0])} 段，最大误差 {err} µs"
    else:
        ok, detail = False, f"解析出 {len(pframes)} 帧"
    print(f"[{'OK' if ok else 'FAIL'}] Pronto 往返（编码->日志->反解）误差 ≤ 13µs"
          f"（{detail}，来源 {psource}）")
    fails += 0 if ok else 1

    pv = learn_match.match_timings(
        library, library.get_device(device_id)["category"], pframes[0], 8
    )
    phit = next((i for i, row in enumerate(pv) if device_id in row["device_ids"]), None)
    ok = phit is not None and phit < 8 and pv[phit]["score"] >= WEAK
    print(
        f"[{'OK' if ok else 'FAIL'}] 只有 Pronto 日志时（dump: all 的默认情况）"
        f"仍能识别出真设备（排第 {(phit + 1) if phit is not None else '—'}，"
        f"最高分 {pv[0]['score']:.3f}）"
    )
    fails += 0 if ok else 1

    # 误差画像：整体缩 5% 只抬均误、不该出离群；把单独一段 space 拉长 3 倍
    # （= 一个 bit 对不上）必须出离群。这是"同族另一型号"判词的地基。
    scaled = [int(round(v * 1.05)) for v in timings]
    prof_scale = _err_profile(scaled, timings)
    tampered = list(timings)
    for i, v in enumerate(tampered):
        if v < -1000:
            tampered[i] = v * 3
            break
    else:
        tampered[1] *= 3
    prof_bit = _err_profile(tampered, timings)
    ok = (
        prof_scale["outliers"] == 0
        and prof_scale["mean"] <= 0.06
        and prof_bit["outliers"] >= 1
    )
    print(
        f"[{'OK' if ok else 'FAIL'}] 误差画像：整体缩 5% → 均误 "
        f"{prof_scale['mean'] * 100:.1f}% / 离群 {prof_scale['outliers']}；"
        f"单独拉长 1 段 → 离群 {prof_bit['outliers']}"
    )
    fails += 0 if ok else 1

    # 接收端把重复 burst 合并成一帧时，候选前缀必须能把原帧还原出来
    # （原帧的末尾 idle 到了合并帧里就成了"内部长 gap"）
    merged = timings + timings[2:52]
    cands_burst = _burst_prefixes(merged)
    ok = len(cands_burst) >= 2 and timings in cands_burst
    print(
        f"[{'OK' if ok else 'FAIL'}] 合并的重复 burst：{len(merged)} 段里能还原出 "
        f"{len(timings)} 段原帧前缀（候选段数 {[len(c) for c in cands_burst]}）"
    )
    fails += 0 if ok else 1

    print("\n" + ("自测 FAIL" if fails else "自测 PASS"))
    return 1 if fails else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="拿 ESPHome 日志里的原始时序，本机离线跑码库匹配")
    ap.add_argument("source", nargs="?", help="日志文件；`-` 表示从 stdin 读")
    ap.add_argument("--text", help="直接给日志文本")
    ap.add_argument("--category", default="auto", help="auto（默认）/ 大类 id / 名字 / ac")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    if args.text is not None:
        text = args.text
    elif args.source == "-":
        text = sys.stdin.read()
    elif args.source:
        text = open(args.source, encoding="utf-8", errors="replace").read()
    else:
        ap.print_help()
        return 2

    result = analyze(text, args.category, args.top, quiet=args.json)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if "error" in result:
        print(result["error"])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
