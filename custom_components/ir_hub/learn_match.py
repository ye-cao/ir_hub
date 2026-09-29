"""按键学习匹配引擎 ——「用遥控器配对」的核心。

用户拿实体遥控器对准红外接收器按一个键，固件捕获原始时序（µs 数组）；
本模块在码库里**限定设备大类**逐键比对，反查 品牌 / 型号 / 键名。

三个码库/接收事实决定了本引擎的形态：

1. **帧复用是常态**：大量设备共用完全相同的帧（同一批 OEM 遥控）。所以结果按
   **帧**分组呈现（"这个码有 N 个型号在用"），型号歧义由用户多按几个键取交集解决。
2. **接收可能丢引导码**：丢首元素后首个元素是 space，用"首元素 = mark"的朴素
   预筛会把真帧错杀。⇒ 预筛按「原样 / 丢首元素」双变体放行，打分侧本来就有 ±1 对齐。
3. **大类别很肥 + 同帧海量**（机顶盒 ~10 万键）：特征缓存带帧签名哈希 —— 同签名帧
   只解码/打分一次，结果整组共享。首次匹配建缓存（秒级，在 executor 里跑），
   之后毫秒级。
"""

from __future__ import annotations

import logging

_LOGGER = logging.getLogger(__name__)

# 预筛阈值。接收抖动在 ±10% 量级，阈值宁可放宽让候选多进几个、由打分淘汰
# —— 漏掉真键比多算几个候选糟糕得多。
_COUNT_TOLERANCE = 3              # 帧长（元素个数）允许差
_FIRST_MARK_RATIO = (0.5, 1.7)    # 首个 mark 时长比
_TOTAL_RATIO = (0.45, 1.9)        # 帧总时长比（irext 帧尾带 4 万~20 万 µs 长 gap）

# 打分门槛。连续分下真帧 ≈0.94、同长异帧 ≈0.79，门槛取 0.62 宁可多留候选。
_SCORE_MIN = 0.62

# 最差元素惩罚权重：`score = 1 - 平均相对误差 - _MAX_W * 最大相对误差`。
# 只用平均误差时，"只差几个 bit"的近邻帧均差只比真帧高 0.006，被抖动淹没，
# 会让近邻帧把真帧挤出 top_n。叠加 max 惩罚后两者拉开一个数量级
# （真帧 max≈0.08，近邻帧 max≈0.67）。权重 0.10~0.25 同为平台最优，取中值 0.20。
_MAX_W = 0.20

# (捕获去头, 库去头, 捕获去尾, 库去尾)
# ⚠️ (0,1,0,0) 是**捕获丢了引导码**那一档，不能少：丢引导码后若不按"库去头"
#    重新对齐，真帧会被错位比对、分数崩到 ~0.75，被碰巧对齐的错帧挤下去。
_ALIGNMENTS = (
    (0, 0, 0, 0),   # 原样
    (1, 0, 0, 0),   # 捕获多 1 个前导元素
    (0, 1, 0, 0),   # 捕获丢 1 个前导元素
    (0, 0, 1, 0),   # 捕获多 1 个尾元素
    (0, 0, 0, 1),   # 库帧多 1 个尾元素
)

# 引导码门限：捕获里 ≥ 此值的正脉冲才算"引导码存在"（NEC 9ms / RCA 4ms 均过）
_LEADER_US = 3000

# 帧签名量化步长（只用于把逐字节相同的库帧归成一组，20µs 足够细）
_FRAME_BUCKET_US = 20

# 特征缓存：{(lib.generated, category): [(device_id, key, count, first, total, sig_hash)]}
_FEATURE_CACHE: dict[tuple[str, int], list[tuple]] = {}

# 代表帧解码缓存：{(lib.generated, category, sig_hash): 带符号 µs}
# 同一帧签名只解码一次，避免同一大类多次匹配时反复解码同一个帧。
_FRAME_CACHE: dict[tuple[str, int, int], list[int]] = {}


def parse_capture(state: str) -> list[int] | None:
    """把学习传感器里的文本解析成带符号时序数组。

    传感器格式（固件 on_raw 生成）："9000,-4500,560,-1690,..."。
    解析失败 / 长度不足 8 返回 None。
    """
    try:
        values = [int(chunk) for chunk in str(state).replace(";", ",").split(",")]
    except (TypeError, ValueError):
        return None
    if len(values) < 8 or any(v == 0 for v in values):
        return None
    return values


def normalize_capture(values) -> list[int] | None:
    """接收端原始时序 → 库约定（偶正奇负）的带符号数组；无效返回 None。

    HA `InfraredReceivedSignal.timings` 有的给**全正**数组、有的给正负交替；
    按下标奇偶强制符号后两种输入结果一致（幂等）。
    出现 0 或长度 < 8 一律判无效 —— 0 会让奇偶错位，宁可不匹配也不给错码。
    """
    if not values:
        return None
    out: list[int] = []
    for index, value in enumerate(values):
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        if number == 0:
            return None
        out.append(abs(number) if index % 2 == 0 else -abs(number))
    return out if len(out) >= 8 else None


def capture_features(timings: list[int]) -> tuple[int, int, int]:
    """(帧长, 首个 mark 绝对值, 总时长绝对值) —— 预筛三特征。"""
    first = 0
    for v in timings:          # 首个正元素（mark）；丢了引导码时就是数据位脉宽
        if v > 0:
            first = v
            break
    total = sum(abs(v) for v in timings)
    return len(timings), first, total


def frame_signature(timings: list[int]) -> tuple:
    """帧指纹：量化后的绝对值元组。库内逐字节相同的帧必然同指纹。"""
    return tuple(abs(int(round(v / _FRAME_BUCKET_US))) for v in timings)


def _similarity(captured: list[int], lib: list[int]) -> float:
    """带符号两帧的**连续**相似度 ∈ [0, 1]。

    `score = 1 - 平均相对误差 - _MAX_W * 最大相对误差`，取 5 种对齐里的最高分。

    ⚠️ 刻意**不用**"容差内匹配占比"：那种算法会饱和在 1.0 —— 大类别里几百个
    **不同**的帧都能对抖动后的捕获打满分，top_n 截断后退化成随机挑 8 个，
    源帧被挤掉。用连续距离后真帧（平均相对误差 ~4%）≈0.94，
    同长异帧（~15%）≈0.79，真帧稳定排第一，同帧设备（逐字节相同）同分共组。
    """
    best = 0.0
    for cap_f, lib_f, cap_b, lib_b in _ALIGNMENTS:
        a = captured[cap_f: len(captured) - cap_b if cap_b else None]
        b = lib[lib_f: len(lib) - lib_b if lib_b else None]
        n = min(len(a), len(b))
        if n < 4:
            continue
        err = 0.0
        worst = 0.0
        for i in range(n):
            va = a[i]
            vb = b[i]
            denom = abs(va) if abs(va) > abs(vb) else abs(vb)
            if denom < 1:
                denom = 1
            e = abs(va - vb) / denom
            err += e
            if e > worst:
                worst = e
        mean_err = err / n
        extra = abs(len(a) - len(b))      # 未对齐的多余元素按满误差计入
        if extra:
            mean_err = (mean_err * n + extra) / (n + extra)
        score = 1.0 - mean_err - _MAX_W * worst
        if score < 0.0:
            score = 0.0
        if score > best:
            best = score
    return best


def _category_features(library, category: int) -> list[tuple]:
    """某大类的全键预筛特征（带缓存）。

    元素：(device_id, key, count, first, total, sig_hash)。
    首次调用逐键解码（最肥的类秒级），之后命中缓存零开销。
    """
    cache_key = (library.generated, category)
    cached = _FEATURE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    features: list[tuple] = []
    for device in library.devices:
        if device["category"] != category:
            continue
        device_id = int(device["id"])
        for key in library.key_names(device):
            timings = library.get_timings(device_id, key)
            if not timings or len(timings) < 4:
                continue
            count_f, first_f, total_f = capture_features(timings)
            sig_hash = hash(frame_signature(timings))
            features.append((device_id, key, count_f, first_f, total_f, sig_hash))

    _FEATURE_CACHE[cache_key] = features
    _LOGGER.debug(
        "IR Hub learn: category %s feature cache built (%d keys)", category, len(features)
    )
    return features


def _prefilter_pass(
    cap_feats_variants: list[tuple[int, int, int]],
    lib_count: int,
    lib_first: int,
    lib_total: int,
) -> bool:
    """双变体预筛：原样 / 丢首元素，任一变体过全部关卡即放行。

    ⚠️ 首脉宽是**条件门**：只有捕获里存在引导码级大脉冲（≥3000µs）才查比例 ——
    引导码被接收端丢掉后，首个正元素只是数据位脉宽（~560µs），拿它对库帧的
    9000 做比例检查必然错杀。引导码缺失时只靠 帧长 + 总时长 约束，
    对齐逻辑在打分侧兜底。
    """
    for count, first, total in cap_feats_variants:
        if abs(lib_count - count) > _COUNT_TOLERANCE:
            continue
        if first >= _LEADER_US and lib_first and not (
            _FIRST_MARK_RATIO[0] <= first / lib_first <= _FIRST_MARK_RATIO[1]
        ):
            continue
        if lib_total and not (
            _TOTAL_RATIO[0] <= total / lib_total <= _TOTAL_RATIO[1]
        ):
            continue
        return True
    return False


def match_timings(
    library,
    category: int,
    captured: list[int],
    top_n: int | None = 8,
    max_devices_per_frame: int = 6,
) -> list[dict]:
    """在大类内逐键比对，返回按**帧**分组的候选（按 score 降序）。

    返回元素（一帧一项）：
        {frame, score, n_devices, device_ids, devices: [
            {device_id, brand, brand_name, model, key}, ... ≤ max_devices_per_frame
        ]}
    frame 是库帧（带符号 µs），可直接喂发射通道做试发确认。
    top_n=None 时返回全部幸存帧（诊断 / "显示全部"用）。
    """
    cap_variants = [capture_features(captured)]
    if len(captured) > 9:
        cap_variants.append(capture_features(captured[1:]))

    features = _category_features(library, category)

    # 幸存者按帧签名分组 —— 同帧设备只解码/打分一次
    groups: dict[int, list[tuple]] = {}
    for row in features:
        device_id, key, lib_count, lib_first, lib_total, sig_hash = row
        if not _prefilter_pass(cap_variants, lib_count, lib_first, lib_total):
            continue
        groups.setdefault(sig_hash, []).append((device_id, key))
    _LOGGER.debug(
        "IR Hub learn: %d/%d keys survived prefilter (%d unique frames)",
        sum(len(v) for v in groups.values()),
        len(features),
        len(groups),
    )

    hits: dict[int, dict] = {}
    for sig_hash, members in groups.items():
        device_id, key = members[0]
        cache_key = (library.generated, category, sig_hash)
        timings = _FRAME_CACHE.get(cache_key)
        if timings is None:
            timings = library.get_timings(device_id, key)
            if not timings:
                continue
            _FRAME_CACHE[cache_key] = timings
        score = _similarity(captured, timings)
        if score < _SCORE_MIN:
            continue
        hits[sig_hash] = {
            "frame": timings,
            "score": score,
            "n_devices": len(members),          # 真正共用该帧的设备总数
            "device_ids": [m[0] for m in members],   # 全成员 id（供交叉/深挖）
            "devices": [],                      # 仅前 max_devices_per_frame 个详情
        }
        for dev_id, dev_key in members[:max_devices_per_frame]:
            device = library.get_device(dev_id)
            hits[sig_hash]["devices"].append(
                {
                    "device_id": dev_id,
                    "brand": device["brand"] if device else 0,
                    "model": device["name"] if device else "",
                    "brand_name": (
                        library.brands.get(device["brand"])
                        or f"品牌 {device['brand']}"
                    )
                    if device
                    else "",
                    "key": dev_key,
                }
            )

    ranked = sorted(hits.values(), key=lambda item: -item["score"])
    return ranked if top_n is None else ranked[:top_n]
