# 诊断工具：同一个 AC bin 分别用 IR Hub 与 SmartAC 的 irext 解码器展开，逐项对比。
# 用途：空调按键无反应时，先确认两边生成的帧到底一致不一致（定位根因方向）。
# 跑法：<venv>/python tools/diff_ac_decode.py
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

# ---- 复用 selfcheck 的 HA stub（irext/climate 依赖 homeassistant）----
import selfcheck as sc

sc._install_ha_stubs()

smartac = sc.load_module(
    "_smartac_irext", os.path.join(ROOT, "re", "smartac", "irext.py")
)
sc.make_shim_package()
ac_lib_mod = sc.load_module(
    "_ir_hub_shim.ac_library",
    os.path.join(ROOT, "custom_components", "ir_hub", "ac_library.py"),
)

BIN = "irda_new_ac_3387.bin"
path = os.path.join(ROOT, "custom_components", "ir_hub", "ac_library", "codes", BIN)
data = open(path, "rb").read()
print(f"bin: {BIN} ({len(data)} bytes)\n")

# ---- IR Hub ----
hub = ac_lib_mod.decode_bin(data)
hub_off = [abs(t) for t in hub["off"]]
hub_cool26 = [abs(t) for t in hub["commands"]["cool"]["auto"]["26"]]

# ---- SmartAC ----
sa_ac = smartac.AC(data)
sa_off = sa_ac.ir_decode(smartac.POWER_OFF, 26, smartac.MODE_AUTO, smartac.SPEED_AUTO)
sa_cool26 = sa_ac.ir_decode(smartac.POWER_ON, 26, smartac.MODE_COOL, smartac.SPEED_AUTO)
print(f"SmartAC _repeat_time = {sa_ac._repeat_time}")
print(f"IR Hub off len={len(hub_off)}  SmartAC off len={len(sa_off)}")
print(f"IR Hub cool26 len={len(hub_cool26)}  SmartAC cool26 len={len(sa_cool26)}")


def cmp(name, a, b):
    """逐项比较，不一致时打印首个差异点附近的上下文。"""
    if a == b:
        print(f"[SAME] {name} ({len(a)} 项)")
        return True
    print(f"[DIFF] {name}: len {len(a)} vs {len(b)}")
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            print(f"  首个差异 @ {i}: ir_hub={x} smartac={y}")
            print(f"  ir_hub  [{max(0,i-3)}:{i+4}] = {a[max(0,i-3):i+4]}")
            print(f"  smartac [{max(0,i-3)}:{i+4}] = {b[max(0,i-3):i+4]}")
            break
    else:
        print("  前缀一致，仅长度不同（尾部多/少）：")
        longer, tag = (a, "ir_hub") if len(a) > len(b) else (b, "smartac")
        shorter_len = min(len(a), len(b))
        print(f"  {tag} 多出: {longer[shorter_len:shorter_len+12]} ...")
    return False


same_off = cmp("off 帧", hub_off, sa_off)
same_cool = cmp("cool/26/auto 帧", hub_cool26, sa_cool26)

# SmartAC 真正发布的是 repeat_time 倍数后的帧
print()
print(f"SmartAC 实发 off = off × {sa_ac._repeat_time}（{len(sa_off)} 项）")
print(f"IR Hub 实发 off = 1 帧（{len(hub_off)} 项，repeats 选项默认 1）")

if not (same_off and same_cool):
    print("\n==> 帧内容不同：这就是空调无反应的根因方向")
else:
    print("\n==> 帧内容逐项一致：差异在别处（repeat 次数 / 发送链路）")
