"""IR Hub —— 基于 irext 码库的通用红外遥控（HA infrared 架构里的 consumer 集成）。

本集成不碰硬件，只把码库里的码交给 emitter（如 ESPHome 的 ir_rf_proxy）发出去。
数据通路：

    本集成的 remote 实体
      --infrared.async_send_command--> HA 的 infrared.<emitter> 实体
      --> aioesphomeapi.infrared_rf_transmit_raw_timings(...)
      --> ESPHome infrared 组件 --> remote_transmitter（GPIO4）--> 红外 LED

两个容易踩的点（源码依据见 ir_command.py）：

  1. 载波频率走 `Command.modulation`，与 ESPHome 的 `api.actions`（send_raw 等）
     无关；那两条动作是留给脚本 / SmartIR 的旁路，本集成不碰。
  2. `Command.repeat_count` 会被丢弃 ⇒ "多送几次"靠复制时序实现，
     已封装在 `ir_command.build_raw_command()`。
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
    """加载一个配置条目。"""
    hass.data.setdefault(DOMAIN, {})

    # 码库是只读静态资源，全实例共用一份，避免每个条目各解压一遍。
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
    """卸载一个配置条目。"""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """选项（载波 / 发送次数）变化时重载条目。"""
    await hass.config_entries.async_reload(entry.entry_id)


def _register_send_raw(hass: HomeAssistant) -> None:
    """注册 send_raw 服务：把任意裸时序直接交给指定的 infrared emitter。

    这是"码库里没有、或要临时调试"时的逃生口 —— 完全绕过码库。
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
