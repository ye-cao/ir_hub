"""remote 平台 —— 每个 config entry = 一个 remote 实体（对应一台家电）。

一个 remote 实体代表"码库里的某台设备 + 一个 infrared emitter"：
  · 它的 activity_list 就是这台设备的全部可用按键
  · 调 `remote.send_command` 时，把按键名映射成码库里的时序发出去

为什么用 remote 实体：HA 的 `remote` 域本身就是"通用遥控器"的表达，
自带 send_command / turn_on / turn_off 服务，dashboard 侧做按钮面板最省事。
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.infrared import InfraredEmitterConsumerEntity
from homeassistant.components.remote import (
    ATTR_ACTIVITY,
    ATTR_NUM_REPEATS,
    RemoteEntity,
    RemoteEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

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
from .ir_command import build_raw_command, parse_timings
from .library import CodeLibrary
from .transmitter import async_send_timings

_LOGGER = logging.getLogger(__name__)

# 本平台不轮询设备（红外是单向的，也没有可读状态）
PARALLEL_UPDATES = 0

# 逃生口前缀：command: ["raw:1000 -500 1000"] 可直接指定裸时序
RAW_PREFIX = "raw:"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the remote platform."""
    data = {**entry.data, **entry.options}
    if data.get(CONF_CATEGORY) == CATEGORY_AC:
        # 空调条目走 climate 平台（状态机码库没有"按键"可言）
        _LOGGER.debug("IR Hub: AC entry %s -> climate platform, remote skipped", entry.entry_id)
        return

    library: CodeLibrary = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([IrHubRemote(hass, entry, library)])


class IrHubRemote(InfraredEmitterConsumerEntity, RemoteEntity):
    """A remote entity backed by an irext device from the bundled code library.

    多重继承说明：
      · `InfraredEmitterConsumerEntity` 提供 `_send_command()` 与
        **自动跟随 emitter 可用性**（emitter 掉线 → 本实体 unavailable，
        emitter 上线 → 自动恢复），符合官方对 consumer 的要求
        （consumer 不得直接调 `InfraredEmitterEntity.async_send_command`）。
      · `RemoteEntity` 提供 remote 域的服务契约（send_command / turn_on / turn_off）
        与 activity_list 状态属性。
      MRO：IrHubRemote → InfraredEmitterConsumerEntity → InfraredConsumerEntity
           → RemoteEntity → ToggleEntity → Entity
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_icon = "mdi:remote"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        library: CodeLibrary,
    ) -> None:
        # 惯例上必须调（已核实：HA 当前的 `Entity` **没有** `__init__`，初始化职责在
        # `add_to_platform_start`，而 `_context` / `_attr_available` /
        # `_attr_should_poll` 都是带默认值的**类属性** ⇒ 不调也能跑）。
        # 仍然调，是为了将来 HA 真给 `Entity.__init__` 加东西时不会静默跳过。
        super().__init__()

        data = {**entry.data, **entry.options}

        device = library.get_device(int(data[CONF_DEVICE]))
        if device is None:
            raise HomeAssistantError(
                f"IR Hub: 码库里找不到设备 id={data[CONF_DEVICE]}（码库已更新？请重加集成）"
            )

        self._library = library
        self._device = device
        self._device_id = int(device["id"])
        self._keys = library.key_names(device)

        # ---- 发射通道（infrared / esphome / broadlink / mqtt）----
        # 旧条目只有 CONF_EMITTER ⇒ 兼容回退为 infrared + emitter 实体
        self._tx_type: str = data.get(CONF_TX_TYPE) or TX_INFRARED
        self._tx_target: str = (
            data.get(CONF_TX_TARGET) or data.get(CONF_EMITTER) or ""
        )
        self._tx_delay: float = float(data.get(CONF_TX_DELAY) or DEFAULT_TX_DELAY)

        # 这是给基类用的：infrared 通道下基类靠它跟踪 emitter 可用性 / 发命令；
        # 其它通道基类跟踪被 async_added_to_hass 跳过，发码也走 transmitter。
        self._infrared_emitter_entity_id: str = self._tx_target

        self._carrier = int(data.get(CONF_CARRIER) or DEFAULT_CARRIER)
        self._repeats = max(1, int(data.get(CONF_REPEATS) or DEFAULT_REPEATS))

        brand = library.brands.get(device["brand"]) or f"品牌 {device['brand']}"
        self._brand_name = brand

        self._attr_unique_id = entry.entry_id
        # has_entity_name=True + name=None ⇒ 实体名取设备名
        self._attr_name = None

        # activity_list 让 remote 卡片/自动化能发现"这台设备有哪些键"
        self._attr_activity_list = self._keys
        self._attr_supported_features = RemoteEntityFeature.ACTIVITY

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            # 型号常自带品牌（「TCL电视-1」）⇒ 别前缀出「TCL TCL电视-1」，
            # 那会把 entity_id 变成 `remote.tcl_tcl电视_1`。
            name=library.display_name(brand, device["name"]),
            manufacturer=brand,
            model=device["name"],
            model_id=str(self._device_id),
        )

        _LOGGER.debug(
            "IR Hub remote created: %s %s (%d keys) via emitter %s",
            brand,
            device["name"],
            len(self._keys),
            self._infrared_emitter_entity_id,
        )

    # ------------------------------------------------------------------ 属性

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose what this remote can do (helps building dashboards)."""
        return {
            "ir_hub_tx_type": self._tx_type,
            "ir_hub_tx_target": self._tx_target,
            "ir_hub_emitter": self._tx_target if self._tx_type == TX_INFRARED else None,
            "ir_hub_carrier": self._carrier,
            "ir_hub_repeats": self._repeats,
            "ir_hub_category": self._library.categories.get(self._device["category"]),
            "ir_hub_protocol": self._device.get("protocol"),
            "ir_hub_key_count": len(self._keys),
        }

    # ------------------------------------------------------------------ 发射通道

    async def async_added_to_hass(self) -> None:
        """infrared 通道跟随 emitter 可用性；其它通道无实体状态可跟，恒可用。"""
        if self._tx_type == TX_INFRARED:
            await super().async_added_to_hass()

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

    # ------------------------------------------------------------------ 发送

    async def async_send_command(self, command, **kwargs: Any) -> None:
        """Send one or more library keys to the emitter.

        `remote.send_command` 的 schema 是
        `probatio.All(cv.ensure_list, [cv.string])`（`remote/services.py`）
        ⇒ 传裸字符串 `"vol+"` 会被包成 `["vol+"]`，不会拆成字符。
        `command: ["vol+", "vol+"]` 就是按两次音量加。
        也支持 `raw:<时序>` 直接发裸时序（不受码库限制）。
        """
        # 本方法也可能被其它集成 / 脚本**直接调用**（那时服务 schema 不生效），
        # 所以自己再挡一道裸字符串 —— 否则 `for k in "vol+"` 会静默拆成 4 个
        # 单字符键。
        if isinstance(command, str):
            command = [command]

        keys = [str(k) for k in command]
        if not keys:
            return

        # remote.send_command 的 num_repeats 默认值就是 1（DEFAULT_NUM_REPEATS，
        # 且 schema 给了默认值 ⇒ 这个 kwarg 一定存在）⇒ 不能直接拿它覆盖本集成的
        # 配置，只有用户**显式给了 >1** 时，才让服务参数胜出。
        requested = int(kwargs.get(ATTR_NUM_REPEATS, 1) or 1)
        repeats = requested if requested > 1 else self._repeats

        for key in keys:
            timings = self._timings_for(key)
            await self._send_command(
                build_raw_command(timings, carrier=self._carrier, repeats=repeats)
            )
            _LOGGER.debug(
                "IR Hub: %s -> key=%s (%d timings, carrier=%d, repeats=%d)",
                self.entity_id,
                key,
                len(timings),
                self._carrier,
                repeats,
            )

        # 记下最近一次发送的按键（作为 current_activity 展示）
        self._attr_current_activity = keys[-1]
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """`remote.turn_on` —— 默认按一次电源键。

        HA 给 remote 的 turn_on/turn_off 注册的 schema（`REMOTE_SERVICE_ACTIVITY_SCHEMA`）
        带一个可选 `activity` ⇒ 传了就用它当按键名（我们的 `activity_list` 本来
        就是一串按键名），没传才退回 `power`。
        """
        await self.async_send_command([str(kwargs[ATTR_ACTIVITY])] if kwargs.get(ATTR_ACTIVITY) else ["power"])

    async def async_turn_off(self, **kwargs: Any) -> None:
        """同上 —— 红外电源一般就一个键（toggle），所以默认也是按 `power`。"""
        await self.async_send_command([str(kwargs[ATTR_ACTIVITY])] if kwargs.get(ATTR_ACTIVITY) else ["power"])

    # ------------------------------------------------------------------ 内部

    def _timings_for(self, key: str) -> list[int]:
        """把按键名（或 raw: 前缀）解析成带符号时序数组。"""
        if key.startswith(RAW_PREFIX):
            return parse_timings(key[len(RAW_PREFIX) :])

        timings = self._library.get_timings(self._device_id, key)
        if timings is None:
            available = "、".join(self._keys) or "（无）"
            raise HomeAssistantError(
                f"IR Hub: 「{self._brand_name} {self._device['name']}」的码库里"
                f"没有按键 '{key}' 的有效码（也可能是该键只有占位数据）。"
                f"可用按键：{available}"
            )
        return timings
