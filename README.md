# IR Hub

基于 **irext 码库** 的 Home Assistant 红外 `consumer` 集成 —— 自己不碰硬件，
把码库里的码交给 infrared emitter（如 ESPHome `ir_rf_proxy`）发出去。

```text
IR Hub 实体 ─ infrared.async_send_command ─ infrared.<emitter> 实体
  ─ aioesphomeapi (protobuf) ─ ESPHome ir_rf_proxy ─ remote_transmitter ─ IR LED
```

## 两个平台

| | 码库 | 实体 |
|---|---|---|
| **电视 / 机顶盒 / 风扇…** | irext 按键码库：8,565 设备 / 1,259 品牌 / 153,025 键 | 每设备 1 个 `remote.*` + 每键 1 个 `button.*` |
| **⭐ 空调** | irext 状态码库：399 bin（344 KB）/ **233 品牌 / 1053 型号**（美的/格力/TCL/海尔…全在） | 每型号 1 个 `climate.*` 恒温器面板 |

空调是真恒温器：模式 / 温度滑条 / 风速原生可调，重启自动恢复状态，
**不需要 SmartIR / SmartAC**。载波 / 发送次数在集成「选项」里改，改完立即生效。

[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
**一键添加到 HACS**：装好 HACS 的 HA 点这里 →
[HACS 安装 ir_hub](https://my.home-assistant.io/redirect/hacs_repository/?owner=ye-cao&repository=ir_hub&category=integration)

## 依赖

| 项 | 要求 |
|---|---|
| Home Assistant | **≥ 2026.6.0**（`InfraredEmitterConsumerEntity` 落在 2026.6） |
| 发射器 | 四选一，见下表；自制方案示例为 ESPHome（已验证至 2026.9.0） |

**发射通道**（添加集成时选，对齐 SmartAC 的发射器抽象——没有自制硬件也能用现成的）：

| 通道 | 目标 | 说明 |
|---|---|---|
| `infrared`（推荐） | infrared emitter 实体 | HA 官方红外体系，配 ESPHome `ir_rf_proxy` |
| `broadlink` | `remote.*` 实体 | 现成遥控宝；µs→b64 包与 SmartAC 逐字节一致 |
| `esphome` | `esphome.<动作>` | SmartAC 兼容契约（`{"command": [带符号时序]}`） |
| `mqtt` | topic | **默认发 SmartAC 裸时序数组**（tcl-ir 等桥接固件即插即用）；选项里可切 Tasmota IRMQTTServer RAW JSON |

ESPHome 侧最小配置（仅 `infrared` 通道需要）：

```yaml
remote_transmitter:
  id: ir_tx
  pin: GPIO4
  carrier_duty_percent: 50%     # ⚠️ 必须是中间值，0/100 会被拒
  non_blocking: true

infrared:
  - platform: ir_rf_proxy
    name: IR Transmitter        # ⇒ HA 里出现 infrared 发射器实体
    remote_transmitter_id: ir_tx
```

## 安装

### 方式一：HACS（推荐）

1. HA 里先装好 [HACS](https://hacs.xyz) 本身（设置 → 设备与服务 → HACS，首次会要求授权 GitHub）。
2. 添加本仓库，二选一：
   - **点这个链接直达**（需已配置 My Home Assistant）：
     [HACS 添加 ir_hub](https://my.home-assistant.io/redirect/hacs_repository/?owner=ye-cao&repository=ir_hub&category=integration)
   - 或手动：**HACS → 右下角「自定义存储库」** → 仓库填 `ye-cao/ir_hub`、类别选 **Integration** → 添加 → 点 **下载**。
3. 重启 Home Assistant。
4. 设置 → 设备与服务 → 添加集成 → 搜 **IR Hub**。

> 之后有新版本，HACS 页面会出现更新提示，点更新 + 重启即可。

### 方式二：离线安装

1. 下载仓库（Code → Download ZIP，或 `git clone https://github.com/ye-cao/ir_hub.git`）。
2. 把其中的 `custom_components/ir_hub/` **整个目录**拷到 HA 的
   `config/custom_components/ir_hub/`（最终结构：`config/custom_components/ir_hub/manifest.json` 存在）。
3. 重启 Home Assistant → 添加集成 → 搜 **IR Hub**。

> HA 在容器/虚拟机里跑的，注意把目录拷进**容器内**的 `/config`（Samba / File editor 插件均可）。

添加流程：选发射通道 + 目标 → 大类 → 品牌 → 型号 → **实测确认**（自动发一帧测试码：设备=电源键，空调=关机帧；有反应才建 entry，没反应可「自动试下一个型号」——机顶盒这类多型号品牌不用逐个回下拉重选，试完全部自动停，人不在旁边可跳过）。**所有设备大类都有实测环节**。

> 中文设备名会被 slugify 成拼音：`TCL电视-1` → `remote.tcldian_shi_1`。
> 拿不准就去「开发者工具 → 状态」按前缀筛，别照中文猜。

## 用法

**空调**：得到 `climate.*` 后直接用恒温器卡片；每次调温度/模式/风速发一帧
全状态码（美的类协议自动连发 3 帧）。两次操作间隔 ≥1 秒。

**电视/机顶盒** —— 每个可用键已自动生成 button，dashboard 点一下即发：

```yaml
type: grid
columns: 4
square: true
cards:
  - type: button
    entity: button.tcldian_shi_1_power
    name: 电源
  - type: button
    entity: button.tcldian_shi_1_vol_plus
  - type: button
    entity: button.tcldian_shi_1_vol_minus
```

> `+`/`-` 在 entity_id 里映射为 `plus`/`minus`（裸 slugify 会把 `vol+`/`vol-`
> 都压成 `vol` 而撞名）；显示名仍保留原始 `vol+`。同见 `page+/-`、`brightness+/-`。

或者只用 remote 实体调服务（可一次多个键）：

```yaml
service: remote.send_command
target: {entity_id: remote.tcldian_shi_1}
data:
  command: ["vol+", "vol+"]     # 键名取自实体的 activity_list 属性
```

调试逃生口 —— 绕过码库直接发裸时序：

```yaml
service: ir_hub.send_raw
data:
  emitter: infrared.<你的发射器实体>
  timings: "3963 -3985 491 -1990"   # 也接受列表；正=载波 负=空闲，µs
  carrier: 38000
  repeats: 1
```

## 选项与排障

| 选项 | 默认 | 说明 |
|---|---|---|
| 载波频率 (Hz) | 38000 | **别信码库协议名**——实测 TCL 电视标"RCA (56K)"却是 38 kHz 才响。设备没反应就换 38000 / 40000 / 56000 试 |
| 发送次数 | 1 | HA 的 esphome emitter 会丢弃 `repeat_count`，故在时序层复制实现 |
| Broadlink 延迟 | 0.5 秒 | 仅 Broadlink 通道有效（`remote.send_command` 的 `delay_secs`） |

空调排障顺序：① 换载波试 → ② 换同品牌其它型号 bin → ③ 检查发射强度
（直驱 LED 建议三极管驱动，`drive_strength: 40mA` 可拉满 GPIO 能力）。

## 码库说明

- 按键库已清理：剔除占位键 29,028 / 重复键 1,073（判据由 `tools/selfcheck.py` 守着）。
  存储 3.66 MB 按类别分块，运行时只解压用到的一块。
- 空调状态码库 399 bin 在 `ac_library/`，解码器移植自
  [SmartAC](https://github.com/ryanh7/SmartAC)（irext 官方 `ir_decode.c` 的 Python 移植，MIT）。
- irext 按键码存的是无符号长度，符号在读取时补——漏了这步会被
  `RawTimingsCommand` 以"全正数组"拒收，一次都发不出去。

## 自检

开发机直跑（不依赖 HA，约 30 秒）：

```bash
python tools/selfcheck.py
```

**140 项**：全库每键 varint 逐字节往返（不抽样）、符号交替不变式、AC 全库
399 bin / 62,403 帧解码回归、remote/button/climate/config_flow 真逻辑单测
（stub homeassistant）、翻译键与 flow 的一致性。末尾 `expected_total` 护栏
保证没有任何检查段被静默跳过。发布形态下 2 项需要仓库外文件的检查自动跳过。

## 已知边界

- **红外是单向的**：没有状态回读，remote 的 state 恒为 unknown；
  climate 的状态是"最后发送的状态"，物理遥控器的操作不会同步进来。
- 码库只有 irext 收录的设备；没有的用 `ir_hub.send_raw` 或
  `remote.send_command` 的 `raw:` 前缀手发时序。
- emitter 掉线自动跟随（一起 unavailable，上线自愈），无需重加集成。
