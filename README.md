# IR Hub

基于 **irext 码库**（8,565 台设备 / 1,259 个品牌 / 153,025 条按键）的通用红外遥控，
是 Home Assistant `infrared` 域里的一个 **consumer** 集成。

**⭐ 空调是真恒温器面板**：内置 irext **状态码库**（399 个编码 bin / 344 KB，覆盖
**233 品牌 / 1053 型号** —— 美的、格力、TCL、海尔、奥克斯、海信全在）。添加集成时选
「空调 · 温控面板」→ 品牌 → 型号，得到标准 `climate.*` 实体：模式 / 温度滑条 / 风速
原生可调，重启后自动恢复状态，**无需 SmartIR / SmartAC**。

> **品牌数口径**：索引里 `brands` 表有 1,818 条，但其中 **559 个品牌**在该品牌下的设备
> 被"占位键 / 重复键"过滤清空后**已无任何可用设备**，用户在下拉框里实际能选到的是
> **1,259 个**。本文一律用后者（实测：`{d['brand'] for d in index['devices']}` 的势）。

它自己**不碰硬件**：只把码库里的时序交给一个 infrared emitter（如 ESPHome 的
`ir_rf_proxy` 实体）发出去。你选不同的"遥控器"（remote 实体）就控不同的设备。

```
IR Hub 的 remote 实体
  └─ infrared.async_send_command()          ← HA 官方 helper
       └─ HA 的 infrared.<emitter> 实体       ← ESPHome 集成提供
            └─ aioesphomeapi.infrared_rf_transmit_raw_timings()
                 └─ ESPHome API (protobuf)
                      └─ infrared::Infrared::control() → IrRfProxy::control()
                           └─ remote_transmitter (GPIO4) → 红外 LED
```

---

## 依赖

| 项 | 要求 |
|---|---|
| Home Assistant | **≥ 2026.6.0**（consumer 基类 `InfraredEmitterConsumerEntity` 是 2026-05-16 随 PR #170854 加入的，落在 2026.6） |
| ESPHome 固件 | 带 `infrared:` 实体（`platform: ir_rf_proxy`，挂一个 `remote_transmitter`）。已在 **ESPHome 2026.9.0** 上验证 |
| 库 | `infrared-protocols`（HA 自带） |

ESPHome 侧最小配置：

```yaml
remote_transmitter:
  id: ir_tx
  pin: GPIO4
  carrier_duty_percent: 50%     # ⚠️ ir_rf_proxy 要求"中间值"，0 或 100 会报错
  non_blocking: true

infrared:
  - platform: ir_rf_proxy
    name: IR Transmitter        # ⇒ HA 里出现红外发射器实体
    remote_transmitter_id: ir_tx
```

> ⚠️ 这一条有个容易踩的坑：`ir_rf_proxy` 的 schema 是
> `cv.has_exactly_one_key(remote_receiver_id, remote_transmitter_id)`，
> 且 **`carrier_duty_percent` 必须取中间值**（`0`/`100` 会被 FINAL_VALIDATE 拒掉）。
> 走完 `esphome config` 不报错才是地基稳了。

---

## 安装

1. 把 `custom_components/ir_hub/` 整个目录放进 HA 配置目录：
   `config/custom_components/ir_hub/`
   > 嫌逐个传文件麻烦：`ir_hub-for-config.zip` 的内容就是 `custom_components/ir_hub/…`
   > （426 个文件 / 3.66 MB，含 AC 状态码库）。**解压到 HA 的 `config/` 根目录**即可就位，无需再挪。
2. 重启 Home Assistant。
3. 设置 → 设备与服务 → 添加集成 → 搜 **IR Hub**。

（作为 HACS 自定义仓库安装时，本目录就是仓库根，`hacs.json` 已在根上。）

添加流程是三张表单：

| 步骤 | 选什么 |
|---|---|
| 1 | 红外发射器（列出现有全部 `infrared.*` 实体）+ 设备大类 |
| 2 | 品牌（按型号数从多到少排列） |
| 3 | 型号（附 irext 协议名作参考） |

建完就会多出一个 `remote` 实体。

> ⚠️ **实体 id 不是你看到的中文** —— HA 用 `slugify()`（底层是 `python-slugify`
> + Unidecode）把中文**音译成拼音**：
>
> | 界面上的设备名 | 实际 entity_id |
> |---|---|
> | `TCL电视-1` | **`remote.tcldian_shi_1`** |
> | `格力空调-2` | **`remote.ge_li_kong_diao_2`** |
>
> （`unidecode("电视")` = `Dian Shi`，且 `TCL` 与 `Dian` 之间不会补分隔符
> ⇒ `tcldian_shi_1`。**拿不准就去「开发者工具 → 状态」按 `remote.` 筛一下**，
> 别照着中文猜。）

---

## 用法

### 服务调用

```yaml
service: remote.send_command
target:
  entity_id: remote.tcldian_shi_1
data:
  command:
    - vol+            # 按键名，取自该设备的 activity_list
    - vol+
```

一次可以给多个键，按顺序发。也支持逃生口直接发裸时序：

```yaml
service: remote.send_command
target:
  entity_id: remote.tcldian_shi_1
data:
  command: ["raw:3963 -3985 491 -1990 491 -1992"]
```

`remote.turn_on` / `remote.turn_off` 默认按一次电源键；给了 `activity` 就按那个键
（例：`remote.turn_on(activity="vol+")`）。

### 做"面板"

本集成**已经给每个可用按键自动生成了 `button` 实体**（`button.<设备>_<按键>`），
所以 dashboard 上不用写 `perform-action` 了 —— 直接放按钮卡片，点一下即发：

```yaml
type: grid
columns: 4
square: true
cards:
  - type: button
    entity: button.tcldian_shi_1_power
    name: 电源
    icon: mdi:power
  - type: button
    entity: button.tcldian_shi_1_vol_plus
  - type: button
    entity: button.tcldian_shi_1_vol_minus
  - type: button
    entity: button.tcldian_shi_1_up
  # …照抄，把 entity 换成其它键：down / left / right / ok / back / menu / 数字 …
```

> ⚠️ **entity_id 里 `+` / `-` 被映射成了 `plus` / `minus`**：
> `vol+` → `button.<设备>_vol_plus`、`vol-` → `button.<设备>_vol_minus`。
> 原因是 HA 的 slugify 会把这两个符号**整个吃掉**（`vol+` 和 `vol-` 都变成 `vol`），
> 同设备内必然撞名、HA 会给后者加 `_2`，就分不清加减了 —— 所以本集成显式做了映射。
> **显示名仍然是 `vol+` / `vol-`**（好看好认）。
> 拿不准就去「开发者工具 → 状态」按 `button.` 筛一下实际 id。
> 同理还有 `page+/-`、`brightness+/-`、`fanspeed+/-`。

如果**只想用一个实体**（不想生成几十个按钮），那条 `remote` 实体的路仍然可用 ——
它的 `activity_list` 属性就是这台设备的全部可用按键：

```yaml
type: grid
columns: 3
square: true
cards:
  - type: button
    name: 电源
    icon: mdi:power
    tap_action: &send
      action: perform-action
      perform_action: remote.send_command
      target: {entity_id: remote.tcldian_shi_1}
      data: {command: ["power"]}
  - type: button
    name: 音量+
    icon: mdi:volume-plus
    tap_action:
      <<: *send
      data: {command: ["vol+"]}
  # …照抄，把 command 换成 mute / up / down / left / right / ok / back / menu …
```

低层逃生口服务（绕过码库，直接发时序，调试用）：

```yaml
service: ir_hub.send_raw
data:
  emitter: infrared.ir_control_ir_transmitter
  timings: "3963 -3985 491 -1990"      # 也接受列表 [3963, -3985, ...]
  carrier: 38000
  repeats: 1
```

> ⚠️ **`emitter` 这个 id 是本文档里唯一「靠规则推导、未经真机确认」的值**。
> 它按 HA 的 `slugify(设备名 + "_" + 实体名)` 算得：固件 `name: ir-control` + 红外实体
> `IR Transmitter` → `ir_control_ir_transmitter`（已用 `python-slugify` 实算验证过算式本身）。
> 若你改过固件的 `name:`，去「开发者工具 → 状态」按 **`infrared.`** 筛一下真实 id 再填。
> （`carrier` / `repeats` / `timings` 三项的字段名与取值范围已与 `__init__.py` 的
> `SEND_RAW_SCHEMA` 逐项核对一致。）

---

## 选项（载波频率 / 发送次数）

集成卡片 → 配置 → 两项：

| 选项 | 默认 | 说明 |
|---|---|---|
| 载波频率 (Hz) | `38000` | 改完立即重载，不用删掉重加 |
| 发送次数 | `1` | 一按发几遍 |

### ⚠️ 别信码库里的协议名

本机 TCL 电视在 irext 里标 **"RCA (56K)"**，但**实测 56 kHz 完全没反应，
38 kHz 才管用**。协议名只是采集时的标注，**以设备实际反应为准**。
设备不理你就来这儿换 38000 / 40000 / 56000 试。

### 为什么要单独给"发送次数"

HA 的 esphome emitter 只把 `timings` 和 `carrier_frequency=command.modulation`
两部分透传给设备，**`Command.repeat_count` 会被丢弃**（protobuf 的
`repeat_count` 走默认值 1，ESPHome 侧最终 `set_send_times(1)`）。

所以本集成把"多送几次"实现在**时序层面**：`repeats=3` 就是把整帧拼三遍。
（`ir_command.build_raw_command()` 里做的，附带一个"帧尾是 mark 时无法直接拼接"
的告警。）

---

## 码库

来自 irext 索引库，已做三类清理（判据与等价性由 `tools/selfcheck.py` 守着）：

| 处理 | 数量 | 原因 |
|---|---|---|
| 剔除占位键 | 29,028 | 键名进了 `key_mapping` 但码就是单个 `0`，发出去必然没反应 |
| 剔除重复键 | 1,073 | 同一设备同一键名重复出现，保留首个（顺带消掉数据块里的孤立字节） |
| 剔除空设备 | 0 | 键被剔光的设备 |

`library/` 合计 **3.66 MB**（`index.json.gz` 846 KB + `data/*.bin.gz` 2.83 MB），
按类别分块，运行时只解压用到的那一块（机顶盒 16 MB 原始）。

| 类别 | 设备 | 按键 |
|---|---|---|
| 机顶盒 | 4,269 | 95,876 |
| 电视机 | 2,076 | 43,004 |
| 风扇 | 590 | 1,953 |
| 音响 | 588 | 3,813 |
| 投影仪 | 413 | 4,082 |
| 网络盒子 | 251 | 2,635 |
| 灯 | 147 | 329 |
| DVD | 87 | 887 |
| 热水器 | 71 | 250 |
| 空气净化器 | 52 | 175 |
| 相机 | 18 | 18 |
| **空调** | **3** | **3** |

> ⚠️ **空调在 irext 索引库里几乎没有码**（3 台设备、3 条按键）。空调遥控是
> 状态机式的（温度/模式/风速整帧下发），本来就不适合"按键码库"这套模型。
> 空调请走 **ESPHome 原生 `climate_ir`**：本机 ESPHome 2026.9.0 实测有 **21 个**平台 ——
> `gree` / `midea_ir` / **`tcl112`** / `daikin`(+`_arc` / `_brc`) / `mitsubishi` /
> `toshiba` / `hitachi_ac344` / `hitachi_ac424` / `coolix` / `delonghi` / `fujitsu_general` /
> `whirlpool` / `whynter` / `noblex` / `ballu` / `emmeti` / `heatpumpir` / `zhlt01` /
> `climate_ir_lg`。写法 `climate: - platform: tcl112`（**TCL 空调就是 `tcl112`**，
> 且不用再加硬件 —— 同一个 GPIO4 发射管就能发）。这样会得到一个带
> 温度 / 模式 / 风速的 `climate` 实体，比按键码库贴合得多。

### 存储格式

`data/<cat>.bin.gz` 是**键序拼接**的 `varint(zigzag)` 流，索引里记
`(chunk, offset, count)` 可随机取。

两个关键事实：

1. **存的是无符号长度**。irext 的 `key_value` 是全正的 mark/space 交替长度
   （全库 12,554,748 个值实测负值 **0 个**）。"正 = pulse / 负 = space" 的符号
   由 `library.sign_timings()` 在**读取时**补 —— 这样 3.66 MB 数据一个字节都
   不用为符号改写。

   漏了这一步的后果不是"声音小一点"，而是**一次都发不出去**：
   全正数组会被 `RawTimingsCommand` 直接 `ValueError` 拒收。
   （这个坑就是被 `tools/selfcheck.py` 抓出来的。）

2. 帧长**奇数很正常**：NEC 标准帧就是"引导码 + 32×2 位 + stop_mark"，
   以 mark 收尾。所以 `repeats > 1` 时帧尾是 mark，拼接会粘出双 mark，
   `build_raw_command()` 会就此告警。

### 重新打包

```bash
PY="C:/Users/ye_ca/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
$PY tools/pack_irext.py --stats                       # 只看统计
$PY tools/pack_irext.py <outdir> [--categories 2,3]   # 只打电视机+机顶盒
```

---

## 自检

不依赖 HA，可在开发机直接跑（会把全库每一个键都过一遍，约 30 秒）：

```bash
$PY tools/selfcheck.py
```

100 项检查，覆盖：

- 所有 `.py` 可解析（`ast`）
- `manifest.json` / `hacs.json` / `translations/*.json` 合法，
  且翻译的 `step` / `abort` / `data` 键与 `config_flow.py` 实际用到的**一一对应**
  （拼错会让表单显示原始键名，很难发现）
- **全库 153,025 个键的逐字节往返**：用 `library.decode_varints()` 解出的值，
  再用打包器 `pack_irext.py` **自己的** `zigzag()`/`put_varint()` 重编，
  必须与 `data/*.bin.gz` 原始字节逐字节相等 —— 不是抽样
- 存储全无符号 / 补符号后含 space / 严格 mark/space 交替 / 无空数组 / 单段时长上限
- 回归锚点：device 47（TCL 电视）`power` = 156 项、首值 `3963`、次值 `-3985`
- ⭐ **回归锚点（更强）**：把本集成将发出的 `power` 码，与
  `../ir-remote/ha/tcl-tv.yaml` 里**已实测能开关真机电视**的那份码**逐项比对**，
  必须完全相同（当前 156 项、差异 0）。这一条把"本集成会发出什么"钉死在
  "已验证对真机有效的字节"上 —— 部署后电视若不响应，可直接排除"码取错了"，
  只剩传输路径一个变量
- `ir_command` 单元测试（用 stub 顶掉 `infrared_protocols`）：
  `parse_timings` / `build_raw_command` 的复制与回落 / `RawTimingsCommand` 的拒收分支
- ⭐ **`remote.py` 实体逻辑单测**（用一整套最小 `homeassistant.*` stub 顶掉 HA）——
  跑的是**上线后会跑的那段代码**：构造（emitter id / 载波 / 发送次数 / `activity_list`
  取自码库 / `device_info`）、发 1 个键 → 1 次发送（156 项 / 38000）、两个键 → 两次发送、
  **裸字符串不被拆成单字符**（本方法被直接调用时服务 schema 不生效，这是防护）、
  `num_repeats=1` 不覆盖配置而显式 `>1` 才胜出、`raw:` 前缀、`turn_on`/`turn_off`
  与 `activity`、未知按键抛清晰错误并列出可用按键
- ⭐ **`config_flow.py` 逻辑单测**（**真 voluptuous** + stub homeassistant）——
  这是**你第一步就会走的路**，坏掉等于集成根本加不上：无 emitter → `abort(no_emitters)`；
  三步表单的 step 流转；下拉框内容（类别名 + 设备数、443 个品牌、型号附协议提示）；
  **`vol.In(字典)` 提交回来确实是 key 字符串**（`config_flow` 紧接着 `int()`，假设错了全盘崩）；
  建 entry 的 title/data 完整性；`async_get_options_flow`；OptionsFlow 的默认值、
  `Coerce(int)` 与保存
- ⭐ **`button.py` 平台单测**（同一套 stub）—— 每个可用按键一个按钮：
  `key_object_id()` 的符号映射（`vol+` → `vol_plus`、`vol-` → `vol_minus`，
  以及 `page+/-` / `brightness+/-` / `fanspeed+/-`）；
  ⭐ **不变量：全库 8,565 台设备内按键 object_id 互不撞名**
  （做法是先给 61 种键名建映射表、再逐设备查表 —— 逐键调 slugify 会卡死）；
  实体构造（显示名保留原始键名 / `entity_id` / `unique_id` / 图标 / 把 emitter id 交给基类）；
  `async_press` 真的发出（载波透传 + 时序项数与码库一致）；该设备 24 个键逐个 press 全通过；
  `async_setup_entry` 生成 24 个 button 且 id 全不重复；设备 id 不存在时不生成、不抛异常

> **自检自身的护栏**：脚本末尾有一条 `expected_total` 常量，断言
> `实际执行 + 跳过` 恒等于它。这样一旦某一段 check 因为异常 / 条件分支 / 脚本被换回
> 旧版本而**整段没执行**，报告会直接判失败 —— 否则脚本只会照旧打印"全部通过"，
> 只是数字悄悄变小，不看数字根本发现不了。**新增或删除 check 时必须同步这个数字。**
>
> 逐字节往返与"与实测码比对"两项，各需要打包器 `../ir-remote/tools/pack_irext.py`
> （依赖 108 MB 的 irext 源库）和 `../ir-remote/ha/tcl-tv.yaml`。**本集成独立发布时不带
> 它们**，脚本会自动降级为 `跳过 2 项（不影响其余结论）`，其余照查 ——
> 已实测两种模式都通过（开发树 100/100；模拟独立发布 98/98 + 跳过 2）。

---

## 已知边界

- **状态是单向的**：红外没有回读，"开/关"无从确认，所以 remote 实体的 state 是
  `unknown`，`current_activity` 只表示"最近发过哪个键"。不要拿它当真实状态用。
- **码库只有 irext 有的东西**：小众品牌可能搜不到；搜不到就用 `ir_hub.send_raw`
  或 `remote.send_command` 的 `raw:` 前缀手发时序。
- **emitter 掉线会自愈**：本集成通过 `InfraredEmitterConsumerEntity` 自动跟随
  emitter 可用性（一起 unavailable、上线自动恢复），所以 HA 重启时即使 ESPHome
  的设备还没连上，也不需要重加集成。
- **固件里的 `api.actions`（`send_raw` 等）与本集成无关**：本集成走的是原生
  protobuf 红外接口，一个字都不碰 `esphome.<device>_send_raw`。那两条动作是留给
  脚本 / SmartIR 的旁路，保留无害。
