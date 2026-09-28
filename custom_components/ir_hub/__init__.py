"""IR Hub —— 基于 irext 码库的通用红外遥控（HA infrared 的 consumer 集成）。

定位：本集成是 HA infrared 架构里的 **consumer**（设备级）角色 ——
      它不碰硬件，只把码库里的码交给 emitter（如 ESPHome 的 ir_rf_proxy）发出去。

数据通路（已逐段对照源码核对，不是推测）：

    本集成的 remote 实体
      --infrared.async_send_command--> HA 的 `infrared.<emitter>` 实体
      --aioesphomeapi.infrared_rf_transmit_raw_timings(
            key, carrier_frequency=command.modulation, timings=... )-->
      ESPHome API --> infrared::Infrared::control() --> IrRfProxy::control()
      --> remote_transmitter（GPIO4）--> 红外 LED

⚠️ 两个必须知道的点（都有源码依据，详见 ir_command.py / const.py 的注释）：

  1. **载波频率走 `Command.modulation`**，跟 ESPHome 的 `api.actions`（send_raw /
     send_raw_command）**完全无关**。那两条动作是留给脚本 / SmartIR 的旁路；
     本集成这条通路一个字都不碰它们（HA 的 esphome emitter 直接走 protobuf
     原生红外接口）。
  2. **`Command.repeat_count` 会被丢弃**（HA 的 esphome emitter 不传它）⇒
     "多送几次"只能靠复制时序实现，已封装在 `ir_command.build_raw_command()`。
"""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.components import infrared
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
import homeassistant.helpers.config_validation as cv

from .const import (
    CONF_CARRIER,
    CONF_EMITTER,
    CONF_REPEATS,
    DEFAULT_CARRIER,
    DEFAULT_REPEATS,
    DOMAIN,
    SERVICE_SEND_RAW,
)
from .ir_command import build_raw_command, parse_timings
from .library import CodeLibrary

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.REMOTE, Platform.BUTTON, Platform.CLIMATE]

SEND_RAW_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMITTER): cv.entity_id,
        # 既接受列表 [1000, -500, ...]，也接受字符串 "1000 -500 ..."（方便模板拼）
        vol.Required("timings"): vol.Any(str, [int]),
        vol.Optional(CONF_CARRIER, default=DEFAULT_CARRIER): cv.positive_int,
        vol.Optional(CONF_REPEATS, default=DEFAULT_REPEATS): vol.All(
            int, vol.Range(min=1, max=50)
        ),
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up IR Hub from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # 码库是只读静态资源，整个 HA 实例共用一份，避免每个 entry 各解压一遍。
    library = hass.data[DOMAIN].get("library")
    if library is None:
        library = await hass.async_add_executor_job(CodeLibrary)
        hass.data[DOMAIN]["library"] = library

    hass.data[DOMAIN][entry.entry_id] = library

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if not hass.services.has_service(DOMAIN, SERVICE_SEND_RAW):
        _register_send_raw(hass)

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options change (载波 / 发送次数)."""
    await hass.config_entries.async_reload(entry.entry_id)


def _register_send_raw(hass: HomeAssistant) -> None:
    """Register a low-level service to blast arbitrary raw timings.

    这是给"码库里没有、或要临时调试"的场合用的逃生口 —— 它完全绕过码库，
    直接把时序交给指到的 infrared emitter。
    """

    async def _handle(call: ServiceCall) -> None:
        raw = call.data["timings"]
        timings = parse_timings(raw) if isinstance(raw, str) else list(raw)
        command = build_raw_command(
            timings,
            carrier=call.data[CONF_CARRIER],
            repeats=call.data[CONF_REPEATS],
        )
        await infrared.async_send_command(
            hass, call.data[CONF_EMITTER], command, context=call.context
        )

    hass.services.async_register(
        DOMAIN, SERVICE_SEND_RAW, _handle, schema=SEND_RAW_SCHEMA
    )
    _LOGGER.debug("IR Hub: registered service %s.%s", DOMAIN, SERVICE_SEND_RAW)
