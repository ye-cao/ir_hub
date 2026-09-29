"""button 平台 —— 每个可用按键一个 button 实体。

为什么要有（而不只是 `remote` 实体）：

  · `remote` 的用法是"先选 activity、再调服务"，做面板得写一串 perform-action YAML。
  · button 在面板上就是一排按钮，点一下即发 —— 遥控器该有的样子，
    自动化里 `button.press` 也最直接。
  · 只为**有有效码**的键生成（`CodeLibrary.key_names()` 已滤掉占位键）。

发码路径与 `remote.py` 完全一致（同一个 emitter、同一套 carrier / repeats），
差别只在触发方式。

⚠️ entity_id（本平台唯一需要小心的地方）：

按键名里的 `+` / `-` 经 HA 的 slugify 会整个消失 —— `vol+` 和 `vol-` 都变成
`vol`。一台设备同时有这两组键是常态，交给 HA 自动生成 id 的话后者会被加后缀
成 `_2`，用户分不清哪个是加哪个是减。

所以本平台**显式指定 entity_id**，把符号映射成词：

    vol+  -> button.<设备>_vol_plus
    vol-  -> button.<设备>_vol_minus

显示名则保持原始按键名（`vol+`），好看好认，两者互不干扰。
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.components.infrared import InfraredEmitterConsumerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import slugify as ha_slugify

from .const import (
    CONF_BRAND,
    CONF_CARRIER,
    CONF_CATEGORY,
    CONF_DEVICE,
    CONF_EMITTER,
    CONF_MQTT_FORMAT,
    CONF_REPEATS,
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

# 红外是单向的，没有可读状态 —— 不轮询
PARALLEL_UPDATES = 0

# entity_id 里的符号映射（见模块 docstring）
_ID_SYMBOLS = (("+", "_plus"), ("-", "_minus"))

# 常用按键的图标（认不出的不设，HA 用 button 默认图标）
_ICONS: dict[str, str] = {
    "power": "mdi:power",
    "mute": "mdi:volume-off",
    "vol+": "mdi:volume-plus",
    "vol-": "mdi:volume-minus",
    "up": "mdi:chevron-up",
    "down": "mdi:chevron-down",
    "left": "mdi:chevron-left",
    "right": "mdi:chevron-right",
    "ok": "mdi:checkbox-blank-circle-outline",
    "back": "mdi:arrow-u-left-top",
    "menu": "mdi:menu",
    "home": "mdi:home",
    "input": "mdi:import",
    "page+": "mdi:chevron-double-up",
    "page-": "mdi:chevron-double-down",
    "set": "mdi:cog",
    "wind_speed": "mdi:fan",
}


def key_object_id(key: str) -> str:
    """把按键名转成可安全用于 entity_id 的一段（符号 -> 单词）。"""
    out = key
    for symbol, word in _ID_SYMBOLS:
        out = out.replace(symbol, word)
    return ha_slugify(out)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """为该设备的每个可用按键建一个 button 实体。"""
    library: CodeLibrary = hass.data[DOMAIN][entry.entry_id]
    data = {**entry.data, **entry.options}

    if data.get(CONF_CATEGORY) == CATEGORY_AC:
        # 空调条目走 climate 平台（状态码库没有"按键"可言）
        _LOGGER.debug("IR Hub: AC entry %s -> climate platform, button skipped", entry.entry_id)
        return

    device = library.get_device(int(data[CONF_DEVICE]))
    if device is None:
        _LOGGER.warning(
            "IR Hub: 码库里找不到设备 id=%s，button 平台不生成实体", data[CONF_DEVICE]
        )
        return

    keys = library.key_names(device)
    if not keys:
        _LOGGER.warning(
            "IR Hub: 设备 %s 在码库里没有可用按键，button 平台不生成实体", device["name"]
        )
        return

    brand = library.brands.get(device["brand"]) or f"品牌 {device['brand']}"
    # 与 remote 平台用同一个显示名 ⇒ 两组实体归到同一个 HA 设备、前缀一致
    display = library.display_name(brand, device["name"])
    device_domain_slug = ha_slugify(display)

    # 与 remote.py 相同的 DeviceInfo —— identifiers 一致 ⇒ HA 认为是同一台设备
    device_info = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=display,
        manufacturer=brand,
        model=device["name"],
        model_id=str(int(device["id"])),
    )

    carrier = int(data.get(CONF_CARRIER) or DEFAULT_CARRIER)
    repeats = max(1, int(data.get(CONF_REPEATS) or DEFAULT_REPEATS))
    # 发射通道（旧条目只有 CONF_EMITTER ⇒ 回退为 infrared）
    tx_type = data.get(CONF_TX_TYPE) or TX_INFRARED
    tx_target = data.get(CONF_TX_TARGET) or data.get(CONF_EMITTER) or ""
    tx_delay = float(data.get(CONF_TX_DELAY) or DEFAULT_TX_DELAY)
    mqtt_format = data.get(CONF_MQTT_FORMAT) or DEFAULT_MQTT_FORMAT

    entities = [
        IrHubButton(
            entry=entry,
            library=library,
            device=device,
            key=key,
            carrier=carrier,
            repeats=repeats,
            tx_type=tx_type,
            tx_target=tx_target,
            tx_delay=tx_delay,
            mqtt_format=mqtt_format,
            device_info=device_info,
            entity_id=f"button.{device_domain_slug}_{key_object_id(key)}",
        )
        for key in keys
    ]
    _LOGGER.debug("IR Hub: 为 %s 生成了 %d 个 button", display, len(entities))
    async_add_entities(entities)


class IrHubButton(InfraredEmitterConsumerEntity, ButtonEntity):
    """irext 设备的一个按键，做成可按的按钮。

    多重继承说明（与 `remote.py` 同构）：
      · `InfraredEmitterConsumerEntity` 提供 `_send_command()` 与**自动跟随
        emitter 可用性**（emitter 掉线 → 实体 unavailable，上线自动恢复），
        满足官方对 consumer 的要求（不得直接调 `InfraredEmitterEntity.async_send_command`）。
      · `ButtonEntity` 提供 `button.press` 契约（实现 `async_press`）。
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        *,
        entry: ConfigEntry,
        library: CodeLibrary,
        device: dict,
        key: str,
        carrier: int,
        repeats: int,
        tx_type: str,
        tx_target: str,
        tx_delay: float,
        mqtt_format: str,
        device_info: DeviceInfo,
        entity_id: str,
    ) -> None:
        # 与 remote.py 同理：HA 当前的 Entity 没有 __init__，但调了不亏，
        # 将来 HA 若给 Entity.__init__ 加东西也不会被静默跳过。
        super().__init__()

        self._library = library
        self._device = device
        self._device_id = int(device["id"])
        self._key = key

        # 发射通道：infrared 通道下基类靠 _infrared_emitter_entity_id 跟踪可用性；
        # 其它通道跳过跟踪（见 async_added_to_hass），发码走 transmitter。
        self._tx_type = tx_type
        self._tx_target = tx_target
        self._tx_delay = tx_delay
        self._mqtt_format = mqtt_format
        self._infrared_emitter_entity_id = tx_target

        self._carrier = carrier
        self._repeats = repeats

        self._attr_unique_id = f"{entry.entry_id}_{key_object_id(key)}"
        # 显示名 = 原始按键名（'vol+' 而不是 'vol_plus'）
        self._attr_name = key
        self._attr_device_info = device_info
        self._attr_icon = _ICONS.get(key)

        # 显式 entity_id：符号已变成词，保证同一设备内唯一，
        # 不会出现 vol+ / vol- 都被 slugify 成 `vol` 而让 HA 加 `_2`。
        self.entity_id = entity_id

    async def async_added_to_hass(self) -> None:
        """infrared 通道跟随 emitter 可用性；其它通道无状态可跟，恒可用。"""
        if self._tx_type == TX_INFRARED:
            await super().async_added_to_hass()

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

    async def async_press(self) -> None:
        """按一下 = 把这个键的时序发出去。"""
        timings = self._library.get_timings(self._device_id, self._key)
        if timings is None:
            # 理论上不会发生（key_names() 只返回有有效码的键），
            # 但码库被换掉就可能 —— 报清楚，别静默。
            raise HomeAssistantError(
                f"IR Hub: 设备 {self._device['name']} 的按键 '{self._key}' 现在读不到有效码"
                f"（码库更新过？请重加集成）"
            )

        await self._send_command(
            build_raw_command(timings, carrier=self._carrier, repeats=self._repeats)
        )
        _LOGGER.debug(
            "IR Hub: %s pressed -> %s (%d timings, carrier=%d, repeats=%d)",
            self.entity_id,
            self._key,
            len(timings),
            self._carrier,
            self._repeats,
        )
