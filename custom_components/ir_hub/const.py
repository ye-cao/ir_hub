"""Constants for the IR Hub integration."""

DOMAIN = "ir_hub"

CONF_EMITTER = "emitter"
CONF_CATEGORY = "category"
CONF_BRAND = "brand"
CONF_DEVICE = "device"
CONF_CARRIER = "carrier"
CONF_REPEATS = "repeats"

# 绝大多数家电是 38 kHz。注意：**以实测为准**，不要相信 irext 的协议名
# （本机 TCL 电视协议标 "RCA (56K)"，但实测 38 kHz 才管用）。
DEFAULT_CARRIER = 38000

# 发送次数（1 = 只发一次）。
# ⚠️ 语义提醒：HA 的 esphome emitter（components/esphome/infrared.py）只把
#    timings 和 modulation 透传给设备，**Command.repeat_count 会被丢弃**
#    （protobuf 的 repeat_count 走默认值 1）⇒ "多送几次"只能靠复制时序实现，
#    见 ir_command.build_raw_command()。
DEFAULT_REPEATS = 1

SERVICE_SEND_RAW = "send_raw"

# library/data/<category_id>.bin.gz
CATEGORY_DATA_FILE = "data/%s.bin.gz"

# 空调条目的特殊"大类"标记：走 ac_library/（irext 状态码 bin）而非按键式码库，
# 平台路由也不同（climate，而不是 remote/button）。存进 entry.data[CONF_CATEGORY]。
CATEGORY_AC = "ac"

# 库里时序值里出现的"长 gap"（4 万~20 万 µs）由 varint 自然承载，无需特殊处理。

# --------------------------------------------------------------- 发射通道
# 对齐 SmartAC 的发射器抽象：不只 ESPHome infrared，还能走 Broadlink /
# ESPHome 动作 / MQTT，让没有自制硬件的用户也能用现成发射器。
CONF_TX_TYPE = "tx_type"
CONF_TX_TARGET = "tx_target"
CONF_TX_DELAY = "tx_delay"

TX_INFRARED = "infrared"    # HA infrared emitter 实体（ESPHome ir_rf_proxy 等）
TX_ESPHOME = "esphome"      # esphome.<动作> 服务，data {"command": [带符号时序]}（SmartAC 契约）
TX_BROADLINK = "broadlink"  # remote.<实体>，b64 包（与 SmartAC raw2broadlink 逐字节一致）
TX_MQTT = "mqtt"            # mqtt.publish，载荷格式见 CONF_MQTT_FORMAT

TX_TYPES = (TX_INFRARED, TX_ESPHOME, TX_BROADLINK, TX_MQTT)

# Broadlink `remote.send_command` 的 delay_secs（SmartAC 默认值，兼容沿用）
DEFAULT_TX_DELAY = 0.5

# MQTT 载荷格式。⚠️ 实测（09-28）：tcl-ir 桥接固件（tcl-ir-xxxx/ir_send）解析的是
# SmartAC 的**裸全正 µs 数组**（json.dumps([4450,4450,560,...])），发 Tasmota JSON
# 设备无反应 ⇒ 默认 smartac；Tasmota IRMQTTServer 固件用户在选项里切 tasmota。
CONF_MQTT_FORMAT = "mqtt_format"
MQTT_FORMAT_SMARTAC = "smartac"  # json.dumps([全正 µs 数组])——SmartAC 契约
MQTT_FORMAT_TASMOTA = "tasmota"  # {"Protocol":"RAW","Bits":0,"Raw":"...","Frequency":Hz}
MQTT_FORMATS = (MQTT_FORMAT_SMARTAC, MQTT_FORMAT_TASMOTA)
DEFAULT_MQTT_FORMAT = MQTT_FORMAT_SMARTAC
