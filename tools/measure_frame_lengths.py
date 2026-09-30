"""量真码库里"一帧有多少段"——判定 ESP8266 接收缓冲够不够的依据。

背景（ESPHome 2026.9.0 源码）：`remote_receiver` 的 `buffer_size` 默认
esp8266 = 1000、esp32 = 10000，而 ESP8266 走软件计时路径
（`remote_receiver.cpp`：`new int32_t[buffer_size_]`）⇒ **1000 个脉冲**，
且**不会自动扩容**；ESP32 走 RMT（`remote_receiver_rmt.cpp`）会扩到需要的两倍。
所以"一帧最长多少段"就是"ESP8266 会不会溢出"的分水岭。

跑法：`python tools/measure_frame_lengths.py`
"""

from __future__ import annotations

import importlib.util
import os
import statistics
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
COMPONENT = os.path.join(ROOT, "custom_components", "ir_hub")


def _install_shim() -> None:
    """给组件的相对 import 建包外壳（__init__ 依赖 homeassistant，不能走真包）。"""
    shim = types.ModuleType("_ir_hub_shim")
    shim.__path__ = [COMPONENT]
    sys.modules["_ir_hub_shim"] = shim


def _load(name: str, filename: str):
    path = os.path.join(COMPONENT, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _dist(label: str, lengths: list[int]) -> None:
    if not lengths:
        print(f"{label}: 空")
        return
    lengths = sorted(lengths)
    n = len(lengths)
    q = lambda p: lengths[min(n - 1, int(n * p))]  # noqa: E731
    over = sum(1 for v in lengths if v > 1000)
    print(
        f"{label}: n={n}  min={lengths[0]}  p50={q(.50)}  p90={q(.90)}  "
        f"p99={q(.99)}  max={lengths[-1]}  均值={statistics.mean(lengths):.1f}  "
        f">1000 段的比例={over / n:.4%}"
    )


def main() -> int:
    _install_shim()
    ac_library_mod = _load("_ir_hub_shim.ac_library", "ac_library.py")
    learn_match = _load("_ir_hub_shim.learn_match", "learn_match.py")
    _load("_ir_hub_shim.learn_match_ac", "learn_match_ac.py")
    library_mod = _load("_ir_hub_shim.library", "library.py")

    ac_lib = ac_library_mod.AcLibrary()
    ac_lengths: list[int] = []
    bad = 0
    for bin_name in ac_lib.bin_names():
        try:
            _state, frame = next(ac_library_mod.iter_frames(ac_lib.read_raw(bin_name)))
        except (StopIteration, ValueError, KeyError, IndexError, OSError):
            bad += 1
            continue
        ac_lengths.append(len(frame))
    print(f"--- 空调状态码库（{len(ac_lib.bin_names())} bin，解析失败 {bad}）---")
    _dist("AC 单帧段数", ac_lengths)

    lib = library_mod.CodeLibrary()
    print(f"--- 按键类码库（{len(lib.devices)} 设备 / {len(lib.categories)} 大类）---")
    key_lengths: list[int] = []
    for category in sorted(lib.categories):
        rows = learn_match._category_features(lib, category)
        key_lengths.extend(row[2] for row in rows)
    _dist("按键类单帧段数（全库）", key_lengths)

    print("\n判读：ESP8266 的 remote_receiver buffer_size 默认 1000 个脉冲。")
    print("      若 max 明显 < 1000，则『缓冲太小』**不是**丢帧主因，别再调 buffer_size。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
