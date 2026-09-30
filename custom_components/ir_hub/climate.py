"""climate 平台 —— AC 码库设备 = 一个真正的空调温控面板。

用户视角：添加集成时选「空调 · 温控面板」→ 品牌 → 型号，得到一个标准
`climate.*` 实体，温度滑条 / 模式 / 风速全原生可调，dashboard 上直接用恒温器
卡片，不需要 SmartIR / SmartAC。

实现要点：
  · `InfraredEmitterConsumerEntity` 提供 `_send_command()` 与 emitter 可用性跟随。
  · 每次状态变更 = 从 AC 码库查 `[模式][风速][温度]`（+ 摆风档）的一帧带符号时序发出。
    空调是"全状态帧"协议：改温度就重发整帧，不是"温度+/-"增量键。
  · **能力按"当前模式"动态声明**（`fan_modes` / `min_temp` / `max_temp` /
    `supported_features` 都是 property）—— 库码里每个模式的可用风速与温度范围
    并不相同（实测 525 个 bin 文件中 353 个至少有一个模式没有温度维度、338 个
    各模式的风速集合不同），声明成全模式并集就会让面板给出不存在的组合。
  · **取帧永不因"组合不存在"失败**：一律走 `ac_library.frame_for()`，不支持的分量
    按该模式/风速的能力替换并记日志。只有"整个模式×风速下一帧都没有"才报错。
  · `RestoreEntity`：重启后恢复模式/风速/温度/摆风。物理遥控器改的状态看不到
    （红外单向，没有状态回读）—— 例外见下一条。
  · **可选外部传感器**（对齐 SmartAC，选项里填实体 id，留空不用）：
    温度 / 湿度只用于**显示**室温湿度；功率（智能插座）传感器 ON/OFF 用来同步
    "物理遥控器把它开了 / 关了" —— 功率 ON 且当前关机 ⇒ 状态改成开机（默认回到
    上次开的模式），**不发红外**（红外侧其实什么都没做）；OFF ⇒ 状态改成关机。
  · off 有专用帧（`commands["off"]`）；开机/调温/调风/摆风共用"状态帧"。
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.components.infrared import InfraredEmitterConsumerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_TEMPERATURE,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity

from .ac_library import AcLibrary, frame_for
from .const import (
    CONF_BRAND,
    CONF_CARRIER,
    CONF_CATEGORY,
    CONF_DEVICE,
    CONF_EMITTER,
    CONF_HUMIDITY_SENSOR,
    CONF_MQTT_FORMAT,
    CONF_POWER_SENSOR,
    CONF_REPEATS,
    CONF_TEMPERATURE_SENSOR,
    CONF_TX_DELAY,
    CONF_TX_TARGET,
    CONF_TX_TYPE,
    CATEGORY_AC,
    DEFAULT_CARRIER,
    DEFAULT_MQTT_FORMAT,
    DEFAULT_REPEATS,
    DEFAULT_TX_DELAY,
    DOMAIN,
    TX_INFRARED,
)
from .ir_command import build_raw_command
from .library import CodeLibrary
from .transmitter import async_send_timings

_LOGGER = logging.getLogger(__name__)

# 红外单向、无可读状态 —— 不轮询
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """只处理空调条目：解码 AC 码库后建 climate 实体。"""
    data = {**entry.data, **entry.options}
    if data.get(CONF_CATEGORY) != CATEGORY_AC:
        return

    ac_lib = AcLibrary()
    try:
        code = await hass.async_add_executor_job(ac_lib.load_device, data[CONF_DEVICE])
    except (OSError, ValueError) as err:
        _LOGGER.error("IR Hub: AC 码库 %s 解码失败：%s", data[CONF_DEVICE], err)
        return

    async_add_entities([IrHubClimate(hass, entry, data, code)])


class IrHubClimate(InfraredEmitterConsumerEntity, ClimateEntity, RestoreEntity):
    """AC 码库里的一个型号，映射成 climate 实体。"""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_icon = "mdi:air-conditioner"
    # irext 的温度都是整数度（16~30）
    _attr_target_temperature_step = 1.0
    _attr_precision = 1.0

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        data: dict,
        code: dict,
    ) -> None:
        super().__init__()

        brand: str = data[CONF_BRAND]
        model: str = data[CONF_DEVICE]
        # bin 文件名是 `irda_new_ac_11272.bin` —— 展示型号取编号
        model_short = model.removesuffix(".bin").replace("irda_new_ac_", "")

        self._code = code
        # 发射通道（旧条目只有 CONF_EMITTER ⇒ 回退为 infrared）
        self._tx_type: str = data.get(CONF_TX_TYPE) or TX_INFRARED
        self._tx_target: str = (
            data.get(CONF_TX_TARGET) or data.get(CONF_EMITTER) or ""
        )
        self._tx_delay: float = float(data.get(CONF_TX_DELAY) or DEFAULT_TX_DELAY)
        self._mqtt_format: str = data.get(CONF_MQTT_FORMAT) or DEFAULT_MQTT_FORMAT
        # infrared 通道下基类靠它跟踪 emitter 可用性；其它通道不用
        self._infrared_emitter_entity_id: str = self._tx_target
        self._carrier = int(data.get(CONF_CARRIER) or DEFAULT_CARRIER)
        self._repeats = max(1, int(data.get(CONF_REPEATS) or DEFAULT_REPEATS))

        # ---- 可选外部传感器（对齐 SmartAC；留空 = 不用）----
        # 温度/湿度只管显示；功率 ON/OFF 用来同步"物理遥控器开了/关了"。
        self._temperature_sensor: str = data.get(CONF_TEMPERATURE_SENSOR) or ""
        self._humidity_sensor: str = data.get(CONF_HUMIDITY_SENSOR) or ""
        self._power_sensor: str = data.get(CONF_POWER_SENSOR) or ""
        self._current_temperature: float | None = None
        self._current_humidity: float | None = None
        # 功率传感器判定出的"被遥控器开了"标记（与 SmartAC 同名状态对齐）。
        self._on_by_remote = False

        # ---- 按模式的能力表（见 ac_library.decode_bin）----
        # 为什么不能只用 code["fan_modes"] / min_temp / max_temp：那三个是**全模式并集**。
        self._caps_fans: dict[str, list[str]] = code.get("fans_by_mode") or {}
        self._caps_temps: dict[str, list[int]] = code.get("temps_by_mode") or {}
        self._swing_modes: list[str] = list(code.get("swing_modes") or [])

        # ---- 状态（随后被 RestoreEntity 覆盖）----
        self._attr_hvac_mode = HVACMode.OFF
        self._last_on_operation: HVACMode | None = None
        self._attr_hvac_modes = [HVACMode.OFF] + [
            HVACMode(m) for m in code["modes"]
        ]
        # 初始风速/温度取"第一个开机的模式"的能力，而不是全库并集 ——
        # 否则一上来就有一个该模式不支持的风速，面板显示的就是个无效值。
        first_mode = (
            self._attr_hvac_modes[1].value
            if len(self._attr_hvac_modes) > 1
            else HVACMode.COOL.value
        )
        init_fans = self._fans_of(first_mode)
        self._attr_fan_mode = init_fans[0] if init_fans else "auto"
        init_temps = self._temps_of(first_mode)
        self._attr_target_temperature = float(
            init_temps[0] if init_temps else code["min_temp"]
        )
        self._attr_swing_mode: str | None = (
            self._swing_modes[0] if self._swing_modes else None
        )

        self._attr_unique_id = entry.entry_id
        self._attr_name = None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=CodeLibrary.display_name(brand, f"空调 {model_short}"),
            manufacturer=brand,
            model=f"空调 {model_short}",
            model_id=model,
        )

        _LOGGER.debug(
            "IR Hub climate created: %s %s (modes=%s, fans=全模式并集%s, "
            "按模式风速=%s, 按模式温度=%s, 摆风=%s) via %s",
            brand,
            model_short,
            code["modes"],
            code["fan_modes"],
            self._caps_fans,
            {k: (v[:1] + [".."] + v[-1:] if len(v) > 2 else v) for k, v in self._caps_temps.items()},
            self._swing_modes or "无",
            self._infrared_emitter_entity_id,
        )

    # ------------------------------------------------------- 按模式的能力（核心）

    def _fans_of(self, mode: str) -> list[str]:
        """该模式**真正可用**的风速；不知道就退回全模式并集（保守）。"""
        return self._caps_fans.get(mode) or self._code["fan_modes"]

    def _temps_of(self, mode: str) -> list[int]:
        """该模式**真正可用**的温度；空列表 = 这个模式没有温度维度（如 fan_only）。"""
        return list(self._caps_temps.get(mode) or [])

    @property
    def _active_mode(self) -> str:
        """声明能力时用哪个模式：关机时用"下次开机会用的那个"。

        关机状态下也要能正确地显示风速/温度选项 —— 用来开机的模式就是
        `_last_on_operation`（没有则第一个可用模式）。
        """
        if self._attr_hvac_mode != HVACMode.OFF:
            return self._attr_hvac_mode.value
        if self._last_on_operation is not None:
            return self._last_on_operation.value
        modes = [m for m in self._attr_hvac_modes if m != HVACMode.OFF]
        return modes[0].value if modes else HVACMode.COOL.value

    def _normalize_for_mode(self, mode: str) -> list[str]:
        """把当前风速/温度**收敛到目标模式支持的范围**，返回需要记日志的说明。

        ⚠️ 这一步是"按了必执行"的关键：风机与温度是**模式相关**的（实测 525 个
        bin 文件中 338 个各模式风速集合不同、353 个至少一个模式没温度）。
        切模式时如果不收敛，面板上留着的旧风速就会落进"该模式不存在"的位置。
        """
        notes: list[str] = []

        fans = self._fans_of(mode)
        if fans and self._attr_fan_mode not in fans:
            notes.append(f"风速 {self._attr_fan_mode} 在 {mode} 下不可用 → {fans[0]}")
            self._attr_fan_mode = fans[0]

        temps = self._temps_of(mode)
        if temps:
            current = float(self._attr_target_temperature)
            if not temps[0] <= current <= temps[-1]:
                nearest = min(temps, key=lambda t: (abs(t - current), t))
                notes.append(f"温度 {current:g}°C 不在 {mode} 的范围 → {nearest}°C")
                self._attr_target_temperature = float(nearest)
            elif int(current) not in temps:
                # 范围对但该值被禁用（BAN 里点名禁掉的那几个）
                nearest = min(temps, key=lambda t: (abs(t - current), t))
                notes.append(f"温度 {current:g}°C 被 {mode} 禁用 → {nearest}°C")
                self._attr_target_temperature = float(nearest)
        return notes

    # ------------------------------------------------------------------ 恢复

    async def async_added_to_hass(self) -> None:
        """infrared 通道先跟随 emitter 可用性，然后恢复上次状态、挂传感器订阅。

        ⚠️ RestoreEntity 的恢复走 `async_get_last_state()` 直接调用（不经过
        super() 链）⇒ 非 infrared 通道跳过 super() 不会跳过恢复。
        ⚠️ 订阅必须在"没有历史状态"时也执行 —— 旧实现 `last is None` 直接 return，
        传感器就永远不会挂上。
        """
        if self._tx_type == TX_INFRARED:
            await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            if last.state in {mode.value for mode in HVACMode}:
                self._attr_hvac_mode = HVACMode(last.state)
                if last.state != HVACMode.OFF.value:
                    self._last_on_operation = HVACMode(last.state)
            if (fan := last.attributes.get("fan_mode")) in self._fans_of(
                self._active_mode
            ):
                self._attr_fan_mode = fan
            if (swing := last.attributes.get("swing_mode")) in self._swing_modes:
                self._attr_swing_mode = swing
            if (temp := last.attributes.get("temperature")) is not None:
                try:
                    value = float(temp)
                    if self._code["min_temp"] <= value <= self._code["max_temp"]:
                        self._attr_target_temperature = value
                except (TypeError, ValueError):
                    pass
            # 恢复出来的组合可能不属于当前模式（码库换过/固件升级）—— 收敛一次
            notes = self._normalize_for_mode(self._active_mode)
            if notes:
                _LOGGER.info("IR Hub: 恢复状态时按当前模式调整：%s", "；".join(notes))

        # ---- 可选外部传感器（温度/湿度只显示；功率同步物理遥控器的开/关）----
        if self._temperature_sensor:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self._temperature_sensor],
                    self._async_temp_sensor_changed_event,
                )
            )
            state = self.hass.states.get(self._temperature_sensor)
            if state is not None and state.state not in (
                STATE_UNKNOWN, STATE_UNAVAILABLE
            ):
                self._async_update_temp(state)

        if self._humidity_sensor:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self._humidity_sensor],
                    self._async_humidity_sensor_changed_event,
                )
            )
            state = self.hass.states.get(self._humidity_sensor)
            if state is not None and state.state not in (
                STATE_UNKNOWN, STATE_UNAVAILABLE
            ):
                self._async_update_humidity(state)

        if self._power_sensor:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self._power_sensor],
                    self._async_power_sensor_changed_event,
                )
            )

    # ------------------------------------------------------------------ 能力声明
    # ⚠️ 这四个都是 **property**（不是 `_attr_*` 常量）：HA 每次写状态都会重新读，
    #    所以面板在切模式后会自动换成该模式真实可用的选项。

    @property
    def fan_modes(self) -> list[str]:
        """当前模式下可用的风速（不是全模式并集）。"""
        return list(self._fans_of(self._active_mode))

    @property
    def min_temp(self) -> float:
        """当前模式的温度下限；该模式没有温度维度时退回全库并集（前端兜底用）。"""
        temps = self._temps_of(self._active_mode)
        return float(temps[0]) if temps else float(self._code["min_temp"])

    @property
    def max_temp(self) -> float:
        temps = self._temps_of(self._active_mode)
        return float(temps[-1]) if temps else float(self._code["max_temp"])

    @property
    def supported_features(self) -> ClimateEntityFeature:
        """按当前模式给能力位。

        `fan_only` / 某些 `dry` 没有温度维度 ⇒ **不声明 TARGET_TEMPERATURE**，
        面板就不显示温度滑条（而不是显示一个按了报错的滑条）。
        """
        features = (
            ClimateEntityFeature.FAN_MODE
            | ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
        )
        if self._temps_of(self._active_mode):
            features |= ClimateEntityFeature.TARGET_TEMPERATURE
        if self._swing_modes:
            features |= ClimateEntityFeature.SWING_MODE
        return features

    @property
    def swing_modes(self) -> list[str] | None:
        """这台空调能选的摆风档；空 = 库码里没有摆风（则不声明 SWING_MODE）。"""
        return list(self._swing_modes) or None

    @property
    def swing_mode(self) -> str | None:
        return self._attr_swing_mode

    # ------------------------------------------------------------------ 属性

    @property
    def temperature_unit(self) -> str:
        return self.hass.config.units.temperature_unit

    @property
    def current_temperature(self) -> float | None:
        """室温 —— 只做**显示**，来自选项里配的温度传感器（没配 = 不显示）。"""
        return self._current_temperature

    @property
    def current_humidity(self) -> float | None:
        """湿度 —— 同上，来自选项里配的湿度传感器（没配 = 不显示）。"""
        return self._current_humidity

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "ir_hub_tx_type": self._tx_type,
            "ir_hub_tx_target": self._tx_target,
            "ir_hub_emitter": self._tx_target if self._tx_type == TX_INFRARED else None,
            "ir_hub_carrier": self._carrier,
            "ir_hub_repeats": self._repeats,
            "ir_hub_model": self._attr_device_info["model_id"],
            "last_on_operation": (
                self._last_on_operation.value if self._last_on_operation else None
            ),
            # 能力表原样摊出来 —— 面板给出的选项不对劲时，照这个对就知道
            # 是库码声明如此还是集成算错了。
            "ir_hub_fans_by_mode": self._caps_fans,
            "ir_hub_temps_by_mode": self._caps_temps,
            "ir_hub_swing_modes": self._swing_modes,
            # 可选传感器（选项里配的实体 id，空 = 没配）与功率判定标记
            "ir_hub_temperature_sensor": self._temperature_sensor or None,
            "ir_hub_humidity_sensor": self._humidity_sensor or None,
            "ir_hub_power_sensor": self._power_sensor or None,
            "ir_hub_on_by_remote": self._on_by_remote,
        }

    # ------------------------------------------------- 外部传感器（可选，对齐 SmartAC）

    async def _async_temp_sensor_changed_event(self, event) -> None:
        """温度传感器状态变化 → 刷新显示。"""
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        self._async_update_temp(new_state)
        self.async_write_ha_state()

    async def _async_humidity_sensor_changed_event(self, event) -> None:
        """湿度传感器状态变化 → 刷新显示。"""
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        self._async_update_humidity(new_state)
        self.async_write_ha_state()

    async def _async_power_sensor_changed_event(self, event) -> None:
        """功率（智能插座）传感器 ON/OFF → 同步"物理遥控器开了/关了空调"。

        口径与 SmartAC 一致：**只看 ON/OFF，不看瓦数阈值**（用户插座上接什么
        电器都有可能，阈值反而不可移植）。
        ⚠️ 这里**只改状态、不发红外** —— 红外侧其实什么都没做，发状态帧反而
        可能与物理遥控器的模式打架。
        """
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        if new_state is None:
            return
        if old_state is not None and new_state.state == old_state.state:
            return

        if new_state.state == STATE_ON and self._attr_hvac_mode == HVACMode.OFF:
            # 物理遥控器开机了：面板同步成开机状态，模式回到上次开的那个
            #（没有记录就取第一个可用模式 —— 恒温器卡片上总得显示一个合法模式）。
            self._on_by_remote = True
            mode = self._last_on_operation or self._attr_hvac_modes[1]
            self._attr_hvac_mode = mode
            notes = self._normalize_for_mode(mode.value)
            if notes:
                _LOGGER.info(
                    "IR Hub: %s 被遥控器打开（功率传感器 ON），按库码能力调整：%s",
                    self.entity_id,
                    "；".join(notes),
                )
            self.async_write_ha_state()
            return

        if new_state.state == STATE_OFF and self._attr_hvac_mode != HVACMode.OFF:
            self._on_by_remote = False
            self._attr_hvac_mode = HVACMode.OFF
            self.async_write_ha_state()

    @callback
    def _async_update_temp(self, state) -> None:
        """把温度传感器状态解析成数字（解析不了就保持旧值并记日志）。"""
        try:
            if state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE):
                self._current_temperature = float(state.state)
        except (TypeError, ValueError) as ex:
            _LOGGER.error(
                "IR Hub: %s 温度传感器 %s 的值读不懂：%s",
                self.entity_id, state.entity_id, ex,
            )

    @callback
    def _async_update_humidity(self, state) -> None:
        """把湿度传感器状态解析成数字（同上）。"""
        try:
            if state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE):
                self._current_humidity = float(state.state)
        except (TypeError, ValueError) as ex:
            _LOGGER.error(
                "IR Hub: %s 湿度传感器 %s 的值读不懂：%s",
                self.entity_id, state.entity_id, ex,
            )

    # ------------------------------------------------------------------ 发射通道

    async def _send_command(self, command) -> None:
        """按通道路由：infrared 走 consumer 基类；其余交给 transmitter 打包发服务。"""
        if self._tx_type == TX_INFRARED:
            await super()._send_command(command)
            return
        await async_send_timings(
            self.hass,
            self._tx_type,
            self._tx_target,
            command.get_raw_timings(),
            carrier=command.modulation,
            delay=self._tx_delay,
            mqtt_format=self._mqtt_format,
        )

    # ------------------------------------------------------------------ 设置

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """切模式；先把风速/温度收敛到该模式支持的范围，再发状态帧。"""
        self._attr_hvac_mode = hvac_mode
        if hvac_mode != HVACMode.OFF:
            self._last_on_operation = hvac_mode
            notes = self._normalize_for_mode(hvac_mode.value)
            if notes:
                _LOGGER.info(
                    "IR Hub: %s 切到 %s，按库码能力调整：%s",
                    self.entity_id,
                    hvac_mode.value,
                    "；".join(notes),
                )
        await self._send_state_frame()
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """改温度。

        ⚠️ 不再"越界就忽略"：先按当前模式的能力把值收敛到最近的可用温度
        （模式没温度维度的则原样保留，发帧时用哨兵温度），保证按了必执行。
        """
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        self._attr_target_temperature = float(temperature)
        notes = self._normalize_for_mode(self._active_mode)
        if notes:
            _LOGGER.info(
                "IR Hub: %s 温度按库码能力调整：%s", self.entity_id, "；".join(notes)
            )
        if self._attr_hvac_mode != HVACMode.OFF:
            await self._send_state_frame()
        self.async_write_ha_state()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """改风速（该模式不支持的会被换成该模式第一个可用风速，并记日志）。"""
        self._attr_fan_mode = fan_mode
        notes = self._normalize_for_mode(self._active_mode)
        if notes:
            _LOGGER.info(
                "IR Hub: %s 风速按库码能力调整：%s", self.entity_id, "；".join(notes)
            )
        if self._attr_hvac_mode != HVACMode.OFF:
            await self._send_state_frame()
        self.async_write_ha_state()

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        """改摆风。

        库码里有两种摆风（见 `ac_library._swing_options`）：状态帧里的一个 bit
        （直接重发状态帧即生效），或是独立功能帧（`function_code=6` 叠在**当前状态**
        上发出 —— 与遥控器按那颗摆风键等价）。两者在这里是同一条路径。
        """
        self._attr_swing_mode = swing_mode
        if self._attr_hvac_mode != HVACMode.OFF:
            await self._send_state_frame()
        self.async_write_ha_state()

    async def async_turn_on(self) -> None:
        """开机 = 回到上次开的模式；没记录就制冷（行业惯例）。"""
        if self._last_on_operation is not None:
            await self.async_set_hvac_mode(self._last_on_operation)
        else:
            fallback = (
                HVACMode.COOL if HVACMode.COOL in self._attr_hvac_modes
                else self._attr_hvac_modes[1]
            )
            await self.async_set_hvac_mode(fallback)

    async def async_turn_off(self) -> None:
        """关机。"""
        await self.async_set_hvac_mode(HVACMode.OFF)

    # ------------------------------------------------------------------ 发送

    async def _send_state_frame(self) -> None:
        """把当前 (模式, 风速, 温度, 摆风) 对应的一帧状态码发给 emitter。

        ⚠️ 取帧一律走 `ac_library.frame_for()` —— 它会把该模式不支持的
        风速/温度/摆风档替换成可用的值并返回说明。这样面板/自动化给出的任何组合
        都能发出**某一帧**，不会出现"按了不执行"。
        """
        if self._attr_hvac_mode == HVACMode.OFF:
            timings = self._code["off"]
        else:
            timings, notes = frame_for(
                self._code,
                self._attr_hvac_mode.value,
                self._attr_fan_mode,
                self._attr_target_temperature,
                self._attr_swing_mode,
            )
            if notes:
                _LOGGER.info(
                    "IR Hub: %s 按库码能力取帧时做了替换：%s",
                    self.entity_id,
                    "；".join(notes),
                )
            if timings is None:
                # 组合不存在的情况已经在 frame_for 里替换掉了，走到这里只剩
                # "这台型号在这个模式×风速下一条帧都没有"（码库数据异常）。
                raise HomeAssistantError(
                    f"IR Hub: {self._attr_device_info['name']} 的模式 "
                    f"{self._attr_hvac_mode.value} 在库码里没有任何可用帧"
                    f"（数据异常，请重加集成或换型号）"
                )

        await self._send_command(
            build_raw_command(timings, carrier=self._carrier, repeats=self._repeats)
        )
        _LOGGER.debug(
            "IR Hub: %s -> mode=%s fan=%s temp=%s swing=%s (%d timings, carrier=%d)",
            self.entity_id,
            self._attr_hvac_mode,
            self._attr_fan_mode,
            self._attr_target_temperature,
            self._attr_swing_mode,
            len(timings),
            self._carrier,
        )
