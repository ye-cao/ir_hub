"""IR Hub 用到的常量与配置键。"""

DOMAIN = "ir_hub"

# 配置项 / 服务参数名
CONF_EMITTER = "emitter"
CONF_CATEGORY = "category"
CONF_BRAND = "brand"
CONF_DEVICE = "device"
CONF_CARRIER = "carrier"
CONF_REPEATS = "repeats"

# 载波频率（Hz）。绝大多数家电是 38 kHz。
# 以实测为准，不要相信 irext 的协议名（本机 TCL 电视标 "RCA (56K)"，实测 38 kHz 才有效）。
DEFAULT_CARRIER = 38000

# 发送次数。⚠️ 这个值在链路上不生效：HA 的 esphome emitter 不透传 repeat_count，
# 设备侧恒为 1 次。"多发几遍"靠复制时序实现，见 ir_command.build_raw_command()。
DEFAULT_REPEATS = 1

SERVICE_SEND_RAW = "send_raw"

# 时序数据块路径：library/data/<category_id>.bin.gz
CATEGORY_DATA_FILE = "data/%s.bin.gz"

# 空调条目的特殊"大类"标记。它不走按键式码库，而是走 ac_library/ 里的状态码 bin，
# 平台也路由到 climate 而不是 remote/button。存进 entry.data[CONF_CATEGORY]。
CATEGORY_AC = "ac"

# ------------------------------------------------------------------ 发射通道
# 对齐 SmartAC 的发射器抽象：除了 ESPHome infrared，还能走 Broadlink /
# ESPHome 动作 / MQTT，让没有自制硬件的用户也能用现成发射器。
CONF_TX_TYPE = "tx_type"
CONF_TX_TARGET = "tx_target"
CONF_TX_DELAY = "tx_delay"

TX_INFRARED = "infrared"    # HA infrared emitter 实体（ESPHome ir_rf_proxy 等）
TX_ESPHOME = "esphome"      # esphome.<动作> 服务，data {"command": [带符号时序]}（SmartAC 契约）
TX_BROADLINK = "broadlink"  # remote.<实体>，b64 包（与 SmartAC raw2broadlink 逐字节一致）
TX_MQTT = "mqtt"            # mqtt.publish，载荷格式见 CONF_MQTT_FORMAT

TX_TYPES = (TX_INFRARED, TX_ESPHOME, TX_BROADLINK, TX_MQTT)

# Broadlink `remote.send_command` 的 delay_secs（沿用 SmartAC 默认值）
DEFAULT_TX_DELAY = 0.5

# ------------------------------------------------------------------ 可选传感器
# 对齐 SmartAC（port 自 re/smartac）：红外是单向的拿不到回读，这三个是**可选**的
# 外部传感器实体 id，选项里填（留空 = 不用）：
#   · 温度 / 湿度 —— 只是**显示**在恒温器卡片上（current_temperature / humidity）；
#   · 功率 —— 接空调的智能插座：ON ⇒ 物理遥控器把它开了（同步成开机状态），
#     OFF ⇒ 关了。判断口径与 SmartAC 一致：只看 ON/OFF，不看瓦数阈值。
CONF_TEMPERATURE_SENSOR = "temperature_sensor"
CONF_HUMIDITY_SENSOR = "humidity_sensor"
CONF_POWER_SENSOR = "power_sensor"

# MQTT 载荷格式。
# 默认 smartac：json.dumps([全正 µs 数组])。本机 tcl-ir 桥接固件就是这么解析的，
# 发 Tasmota JSON 格式设备无反应。Tasmota IRMQTTServer 固件用户在选项里切 tasmota。
CONF_MQTT_FORMAT = "mqtt_format"
MQTT_FORMAT_SMARTAC = "smartac"  # json.dumps([全正 µs 数组])
MQTT_FORMAT_TASMOTA = "tasmota"  # {"Protocol":"RAW","Bits":0,"Raw":"...","Frequency":Hz}
MQTT_FORMATS = (MQTT_FORMAT_SMARTAC, MQTT_FORMAT_TASMOTA)
DEFAULT_MQTT_FORMAT = MQTT_FORMAT_SMARTAC
