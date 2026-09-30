"""空调（状态码库）的学习匹配 ——「拿遥控器按电源键配对」在空调上的实现。

按键类设备的一个键 = 一帧定长码，直接比就行（见 `learn_match.py`）。
空调不是：一个 bin 描述**整套**编码规则，帧要按 [模式][风速][温度] 现场生成
（`ac_library.iter_frames`）。所以这里做的事是：把每个候选 bin 能生成的帧枚举
出来跟捕获帧比 —— 匹配结果就是「型号 + 该帧对应的状态」。

全库 509 个 bin × 每台最多 301 帧 ≈ 15 万帧，不可能都建出来比。拆成两级：

  1. **结构指纹预筛**：每个 bin 只生成 1 帧，取 `(帧长, 首个 mark)`。
     同一台空调的所有状态帧帧长相同、引导码相同 ⇒ 这两项就是型号指纹，能筛掉九成
     （全库 509 bin 实测：违反这条的 bin 数 = 0，幸存中位 27 个）。指纹建一次约 0.5 s，之后走缓存。
  2. **全帧打分**：只对预筛留下的 bin 逐帧生成、逐帧打分（≤301 帧 ≈ 150 ms）。

⚠️ 枚举范围**刻意等于 `decode_bin` 能生成的范围**（同一套 supported_modes /
supported_speeds / temperature_range）。否则可能匹配到一台"认得出来但发不出去"
的空调：集成配好了，点任意温度都报不支持。

实测（`tools/verify_learn_ac.py`，120 个真实 bin 注入 ±8% 抖动 + 10% 丢首元素）：
真型号落在候选前 8 = 100%，第 1 名 85.8%（落差全是同协议族近似型号，所以候选要
交给人按品牌挑）；两键交集仍 100% 留住真型号；结构预筛 0 误杀；单次匹配均值 1.2 s。
"""

from __future__ import annotations

import logging

from .ac_library import iter_frames
from .learn_match import (
    _PREFIX_RETRY_SCORE,
    _SCORE_MIN,
    _prefilter_pass,
    burst_prefixes,
    capture_candidates,
    capture_features,
    score_candidates,
)

_LOGGER = logging.getLogger(__name__)

# 结构预筛幸存数低于这个值时，怀疑"捕获被接收端合并过"，用 burst 前缀重数一遍
# （见 `structure_survivors`）。取 3 是因为"库里没有这个形状"的判据本来就是
# 幸存 ≈1~2 —— 真匹配的幸存数是十几~几十。
_STRUCT_RETRY_BELOW = 3

# 结构指纹缓存：{(路径, 品牌数, 型号数): [(bin, 帧长, 首个 mark)]}
_STRUCT_CACHE: dict[tuple, list[tuple[str, int, int]]] = {}


def bin_structures(ac_library) -> list[tuple[str, int, int]]:
    """每个 bin 的 `(bin, 帧长, 首个 mark)`，带缓存。

    只生成 1 帧就够 —— 同一台空调的所有状态帧里，这两项是恒定的。
    """
    key = (
        "ac-struct",
        ac_library.index_path,
        ac_library.brand_count,
        ac_library.device_count,
    )
    cached = _STRUCT_CACHE.get(key)
    if cached is not None:
        return cached

    out: list[tuple[str, int, int]] = []
    for bin_name in ac_library.bin_names():
        try:
            _state, frame = next(iter_frames(ac_library.read_raw(bin_name)))
        except (StopIteration, ValueError, KeyError, IndexError, OSError):
            _LOGGER.debug("IR Hub learn(AC): %s 解析失败，跳过", bin_name)
            continue
        count, first, _total = capture_features(frame)
        out.append((bin_name, count, first))

    _STRUCT_CACHE[key] = out
    _LOGGER.debug("IR Hub learn(AC): 结构指纹建好（%d 个 bin）", len(out))
    return out


def _survivor_count(ac_library, candidates: list[list[int]]) -> int:
    """给定候选变体，数结构指纹预筛后还剩多少个 bin。"""
    cap_variants = [capture_features(c) for c in candidates]
    return sum(
        1
        for _bin_name, count, first in bin_structures(ac_library)
        # 第三个参数 0 = 与 match_ac 一致：首轮不检查总时长
        if _prefilter_pass(cap_variants, count, first, 0)
    )


def structure_survivors(ac_library, captured: list[int]) -> int:
    """只跑第 1 级（结构指纹预筛），数还剩多少个 bin —— 纯诊断用。

    为什么要单独测这一级：`match_ac` 的最终候选数把"预筛灭了"和"逐帧打分没过"
    混在一起，而这两者**处置完全不同**：

    - 幸存数正常（十几~几十）但候选寥寥 → 帧长/引导码对得上，是逐帧分数低，
      多半**捕获被接收端记坏了**（抖动/截断），换接收端或改善信号能救。
    - 幸存数 ≈ 1~2 → 这一帧的结构在库里根本没有对应（`_prefilter_pass` 的帧长
      只容差 ±3 段），基本可断定**这只遥控不在码库里**，再怎么调也配不上。

    ⚠️ 变体口径必须与 `match_ac` 一致（都走 `capture_candidates` 的两趟规则），
    否则这条诊断数出来的"幸存数"会跟真实匹配对不上，反而误导 —— 比如捕获被**合并**
    过（300 段 vs 库 200 段）时，按原样数只有 2 个幸存 ⇒ 会错报"不在码库"，而
    截开后其实有 59 个、真型号就在里面（实测）。

    ⚠️ 前缀重数只在**原样幸存数已很低**（≤ `_STRUCT_RETRY_BELOW`）时做，这是有意的
    偏置：前缀变体会让一批"长度只有一半"的库帧也过预筛，对**库外遥控**可能把
    1~2 个幸存抬到十几~几十 ⇒ 判词从"不在码库里"变成"捕获被记坏"，用户会白白
    多按一次遥控。但两种误判的代价不对称：多按一次只是一轮，而把"其实在库、
    只是被合并了"误判成"不在库"会让用户直接放弃配对。所以宁可偏向后者。
    """
    base = _survivor_count(ac_library, capture_candidates(captured))
    if base <= _STRUCT_RETRY_BELOW and burst_prefixes(captured):
        alt = _survivor_count(ac_library, capture_candidates(captured, True))
        if alt > base:
            return alt
    return base


def _best_of_bin(
    ac_library,
    bin_name: str,
    candidates: list[list[int]],
    cap_variants: list[tuple[int, int, int]],
) -> tuple[dict | None, int]:
    """逐帧过一遍某个 bin，返回 (最高分那帧, 达门槛的帧数)。"""
    best: dict | None = None
    hits = 0
    for state, frame in iter_frames(ac_library.read_raw(bin_name)):
        feats = capture_features(frame)
        if not _prefilter_pass(cap_variants, *feats):
            continue
        score = score_candidates(candidates, cap_variants, feats[0], frame)
        if score < _SCORE_MIN:
            continue
        hits += 1
        if best is None or score > best["score"]:
            best = {"bin": bin_name, "score": score, "state": list(state)}
    return best, hits


def _ac_pass(
    ac_library,
    captured: list[int],
    include_prefixes: bool,
    top_n: int | None,
) -> list[dict]:
    """一趟空调匹配：`include_prefixes` 决定要不要把 burst 前缀算进候选变体。"""
    candidates = capture_candidates(captured, include_prefixes)
    cap_variants = [capture_features(c) for c in candidates]

    results: list[dict] = []
    for bin_name, count, first in bin_structures(ac_library):
        # 帧长 + 首个 mark（第三个参数 0 = 首轮不检查总时长）
        if not _prefilter_pass(cap_variants, count, first, 0):
            continue
        try:
            best, hits = _best_of_bin(ac_library, bin_name, candidates, cap_variants)
        except (StopIteration, ValueError, KeyError, IndexError, OSError):
            continue
        if best is not None:
            best["n_states"] = hits
            results.append(best)

    ranked = sorted(results, key=lambda item: -item["score"])
    return ranked if top_n is None else ranked[:top_n]


def match_ac(
    ac_library,
    captured: list[int],
    top_n: int | None = 8,
) -> list[dict]:
    """在大类（= 全部空调型号）内反查，返回按相似度降序的候选，每个 bin 一项。

    元素：
        {"bin": bin 文件名, "score": 最高分,
         "state": ["on", 模式, 风速, 温度] 或 ["off"], "n_states": 达门槛的帧数}

    ⚠️ `state` 是**该 bin 里得分最高的那个状态帧**的状态，不保证等于用户按下的
    那个键对应的状态（同一个 bin 的相邻状态帧只差几个 bit，实测量到约 1/10 的
    情况下最高分帧是隔壁状态）。它只用于日志/排障 —— 流程只消费 `bin` 和
    `score`，选完型号后是重新发「开机帧 → 关机帧」两段式实测，与这帧无关。

    **两趟**：与 `learn_match.match_timings` 同一套规则（第一趟零额外开销；只有
    捕获有内部长 gap 且第一趟最高分 < `_PREFIX_RETRY_SCORE` 时才用 burst 前缀
    重跑）。空调遥控器的电源键发的是**完整状态帧**、常被连发多遍，接收端把间隔
    <10 ms 的重复粘成一帧是实测故障（见 `learn_match.burst_prefixes`）。
    """
    ranked = _ac_pass(ac_library, captured, False, top_n)
    if not burst_prefixes(captured):
        return ranked
    if ranked and ranked[0]["score"] >= _PREFIX_RETRY_SCORE:
        return ranked
    retry = _ac_pass(ac_library, captured, True, top_n)
    if retry and (not ranked or retry[0]["score"] > ranked[0]["score"]):
        return retry
    return ranked
