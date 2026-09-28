"""climate 平台 —— AC 码库设备 = 一个真正的空调温控面板。

用户视角：添加集成时选「空调 · 温控面板」→ 品牌 → 型号，
得到一个标准 `climate.*` 实体 —— 温度滑条 / 模式 / 风速全原生可调，
dashboard 上直接用恒温器卡片，**不需要** SmartIR/SmartAC。

实现要点（与 remote/button 平台同构的部分不赘述）：
  · `InfraredEmitterConsumerEntity` 提供 `_send_command()` 与 emitter 可用性跟随。
  · 每次状态变更 = 从 AC 码库查 `[模式][风速][温度]` 的一帧带符号时序发出。
    空调是"全状态帧"协议：改温度就重发整帧，不是"温度+/-"增量键。
  · `RestoreEntity`：重启后恢复模式/风速/温度（物理遥控器改的状态我们
    看不到 —— 红外单向，这点与 SmartAC 一致，功率传感器联动留待后续）。
  · off 有专用帧（`commands["off"]`）；开机/调温/调风共用"状态帧"。
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
from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .ac_library import AcLibrary
from .const import (
    CONF_BRAND,
    CONF_CARRIER,
    CONF_CATEGORY,
    CONF_DEVICE,
    CONF_EMITTER,
    CONF_REPEATS,
    CONF_TX_DELAY,
    CONF_TX_TARGET,
    CONF_TX_TYPE,
    CATEGORY_AC,
    DEFAULT_CARRIER,
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
    """Set up the climate platform (AC entries only)."""
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
    """One AC model from the irext state-code library, as a climate entity.

    MRO：IrHubClimate → InfraredEmitterConsumerEntity → InfraredConsumerEntity
         → ClimateEntity → RestoreEntity → ToggleEntity → Entity
    """

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
        # 发射通道（旧条目只有 CONF_EMITTER ⇒ 兼容回退为 infrared）
        self._tx_type: str = data.get(CONF_TX_TYPE) or TX_INFRARED
        self._tx_target: str = (
            data.get(CONF_TX_TARGET) or data.get(CONF_EMITTER) or ""
        )
        self._tx_delay: float = float(data.get(CONF_TX_DELAY) or DEFAULT_TX_DELAY)
        # infrared 通道下基类靠它跟踪 emitter 可用性；其它通道不用
        self._infrared_emitter_entity_id: str = self._tx_target
        self._carrier = int(data.get(CONF_CARRIER) or DEFAULT_CARRIER)
        self._repeats = max(1, int(data.get(CONF_REPEATS) or DEFAULT_REPEATS))

        # ---- 状态（随后被 RestoreEntity 覆盖）----
        self._attr_hvac_mode = HVACMode.OFF
        self._attr_fan_mode = code["fan_modes"][0]
        self._attr_target_temperature = float(code["min_temp"])
        self._last_on_operation: HVACMode | None = None

        # ---- 能力声明 ----
        self._attr_hvac_modes = [HVACMode.OFF] + [
            HVACMode(m) for m in code["modes"]
        ]
        self._attr_fan_modes = code["fan_modes"]
        self._attr_min_temp = float(code["min_temp"])
        self._attr_max_temp = float(code["max_temp"])
        self._attr_supported_features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.FAN_MODE
            | ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
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
            "IR Hub climate created: %s %s (modes=%s, fans=%s, temp=%d-%d) via %s",
            brand,
            model_short,
            code["modes"],
            code["fan_modes"],
            code["min_temp"],
            code["max_temp"],
            self._infrared_emitter_entity_id,
        )

    # ------------------------------------------------------------------ 恢复

    async def async_added_to_hass(self) -> None:
        """infrared 通道先跟随 emitter 可用性，然后恢复状态。

        ⚠️ RestoreEntity 的恢复走 `async_get_last_state()` 直接调用（不经过
        super() 链）⇒ 非 infrared 通道跳过 super() 不会跳过恢复。
        """
        if self._tx_type == TX_INFRARED:
            await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None:
            return
        if last.state in {mode.value for mode in HVACMode}:
            self._attr_hvac_mode = HVACMode(last.state)
            if last.state != HVACMode.OFF.value:
                self._last_on_operation = HVACMode(last.state)
        if (fan := last.attributes.get("fan_mode")) in (
            self._attr_fan_modes or []
        ):
            self._attr_fan_mode = fan
        if (temp := last.attributes.get("temperature")) is not None:
            try:
                value = float(temp)
                if self._attr_min_temp <= value <= self._attr_max_temp:
                    self._attr_target_temperature = value
            except (TypeError, ValueError):
                pass

    # ------------------------------------------------------------------ 属性

    @property
    def temperature_unit(self) -> str:
        return self.hass.config.units.temperature_unit

    @property
    def current_temperature(self) -> None:
        # 红外空调拿不到室温；不伪造（不设 = 卡片不显示当前温度）
        return None

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
        }

    # ------------------------------------------------------------------ 发射通道

    async def _send_command(self, command) -> None:
        """按通道路由：infrared 走 consumer 基类；其余由 transmitter 打包发服务。"""
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
        )

    # ------------------------------------------------------------------ 设置

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self._attr_hvac_mode = hvac_mode
        if hvac_mode != HVACMode.OFF:
            self._last_on_operation = hvac_mode
        await self._send_state_frame()
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        if not self._attr_min_temp <= float(temperature) <= self._attr_max_temp:
            _LOGGER.warning(
                "IR Hub: 温度 %s 超出 %d~%d，忽略",
                temperature,
                self._attr_min_temp,
                self._attr_max_temp,
            )
            return
        self._attr_target_temperature = float(temperature)
        if self._attr_hvac_mode != HVACMode.OFF:
            await self._send_state_frame()
        self.async_write_ha_state()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        self._attr_fan_mode = fan_mode
        if self._attr_hvac_mode != HVACMode.OFF:
            await self._send_state_frame()
        self.async_write_ha_state()

    async def async_turn_on(self) -> None:
        """开机 = 回到上次开的模式；没记录就制冷 26°C（行业惯例）。"""
        if self._last_on_operation is not None:
            await self.async_set_hvac_mode(self._last_on_operation)
        else:
            fallback = (
                HVACMode.COOL if HVACMode.COOL in self._attr_hvac_modes
                else self._attr_hvac_modes[1]
            )
            await self.async_set_hvac_mode(fallback)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    # ------------------------------------------------------------------ 发送

    async def _send_state_frame(self) -> None:
        """把当前 (模式, 风速, 温度) 对应的一帧状态码发给 emitter。"""
        if self._attr_hvac_mode == HVACMode.OFF:
            timings = self._code["off"]
        else:
            fans = self._code["commands"].get(self._attr_hvac_mode.value) or {}
            temps = fans.get(self._attr_fan_mode) or {}
            timings = temps.get(str(int(self._attr_target_temperature)))
            if timings is None:
                # 该模式×风速×温度组合在这台空调的码库里不存在
                # （如制热无低风）—— 报清楚，别静默发错帧
                raise HomeAssistantError(
                    f"IR Hub: {self._attr_device_info['name']} 不支持组合 "
                    f"模式={self._attr_hvac_mode.value} / 风速={self._attr_fan_mode} / "
                    f"温度={int(self._attr_target_temperature)}°C。"
                    f"该模式可用风速：{sorted(fans) or '无'}"
                )

        await self._send_command(
            build_raw_command(timings, carrier=self._carrier, repeats=self._repeats)
        )
        _LOGGER.debug(
            "IR Hub: %s -> mode=%s fan=%s temp=%s (%d timings, carrier=%d)",
            self.entity_id,
            self._attr_hvac_mode,
            self._attr_fan_mode,
            self._attr_target_temperature,
            len(timings),
            self._carrier,
        )
