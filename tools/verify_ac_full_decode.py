"""全量解码验收：index.json 引用的每个 bin 都真实展开一次（decode_bin）。

顺便打印美的 / 格力 / TCL 的型号数作为抽样锚点。
跑法：<venv>/python tools/verify_ac_full_decode.py
"""
import json
import os
import sys
import time

REPO = r"C:/Users/ye_ca/Documents/AIwork/ESPHOME/ir-hub-integration"
COMP = os.path.join(REPO, "custom_components", "ir_hub")
sys.path.insert(0, os.path.join(REPO, "custom_components"))

# ir_hub/__init__ 依赖 voluptuous/HA，这里只 stub 掉包的 __init__
import types
pkg = types.ModuleType("ir_hub")
pkg.__path__ = [COMP]
sys.modules["ir_hub"] = pkg

from ir_hub.ac_library import AcLibrary  # noqa: E402

lib = AcLibrary(os.path.join(COMP, "ac_library"))
print("brands:", lib.brand_count, "devices:", lib.device_count)

bins = sorted({d["bin"] for devs in lib._brands.values() for d in devs})
print("distinct bins referenced:", len(bins))

t0 = time.time()
fail = []
for i, b in enumerate(bins, 1):
    try:
        code = lib.load_device(b)
        assert code["modes"] and code["off"], b
    except Exception as e:  # noqa: BLE001
        fail.append((b, str(e)[:60]))
    if i % 100 == 0:
        print("  %d/%d  %.1fs" % (i, len(bins), time.time() - t0))
print("解码失败:", len(fail), fail[:10])
print("用时 %.1fs" % (time.time() - t0))

m = sorted(int(d["device_name"]) for d in lib.devices_in("美的"))
print("美的:", len(m), m)
g = len(lib.devices_in("格力"))
t = len(lib.devices_in("TCL"))
print("格力:", g, " TCL:", t)
