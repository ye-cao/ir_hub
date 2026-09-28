"""Config flow / Options flow for IR Hub.

添加流程（三步表单）：
    1. user    —— 选一个 infrared emitter（来自 ESPHome 的 ir_rf_proxy 实体）
                  + 选设备大类（电视机 / 机顶盒 / 风扇 …）
    2. brand   —— 选品牌
    3. device  —— 选型号 → 建 entry

建完之后，`remote.<品牌>_<型号>` 就出现了；按键名即 activity_list。
载波频率与发送次数在 **选项** 里改（OptionsFlow）—— 因为
"38k 还是 56k" 只能靠实测定，留个不用重加集成的开关很重要。
"""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import infrared
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback

from .ac_library import AcLibrary
from .const import (
    CONF_BRAND,
    CONF_CARRIER,
    CONF_CATEGORY,
    CONF_DEVICE,
    CONF_EMITTER,
    CONF_REPEATS,
    CATEGORY_AC,
    DEFAULT_CARRIER,
    DEFAULT_REPEATS,
    DOMAIN,
)
from .library import CodeLibrary

_LOGGER = logging.getLogger(__name__)


class IrHubConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Add one remote (码库设备 + emitter 的组合)。"""

    VERSION = 1

    def __init__(self) -> None:
        self._emitter: str = ""
        self._category: int | str = 0
        self._brand: int = 0
        self._library: CodeLibrary | None = None
        self._ac_library: AcLibrary | None = None
        self._ac_brand: str = ""

    # ------------------------------------------------------------------ 工具

    async def _async_library(self) -> CodeLibrary:
        """码库是只读静态资源，config flow 里自己解一份即可（不会常驻）。"""
        if self._library is None:
            self._library = await self.hass.async_add_executor_job(CodeLibrary)
        return self._library

    async def _async_ac_library(self) -> AcLibrary:
        """AC 状态码库（只读 index.json，bin 在建 entry 后才解码）。"""
        if self._ac_library is None:
            self._ac_library = await self.hass.async_add_executor_job(AcLibrary)
        return self._ac_library

    # ------------------------------------------------------------- 第 1 步

    async def async_step_user(self, user_input: dict | None = None):
        """选 emitter + 设备大类。"""
        library = await self._async_library()
        ac_library = await self._async_ac_library()

        emitters = infrared.async_get_emitters(self.hass)
        if not emitters:
            # 没有可用的红外发射器就没必要往下走 —— 引导用户先把 ESPHome 配好
            return self.async_abort(reason="no_emitters")

        if user_input is not None:
            self._emitter = user_input[CONF_EMITTER]
            if user_input[CONF_CATEGORY] == CATEGORY_AC:
                return await self.async_step_ac_brand()
            self._category = int(user_input[CONF_CATEGORY])
            return await self.async_step_brand()

        categories = {
            str(cid): f"{name}（{count} 台）"
            for cid, name, count in library.categories_available()
        }
        if not categories:
            return self.async_abort(reason="empty_library")

        # ⭐ 空调走**状态码库**（irext bin，233 品牌），产生真正的 climate 温控
        #    面板 —— 放在第一位，因为这是家用场景里最常用的。
        categories = {
            CATEGORY_AC: (
                f"空调 · 温控面板（{ac_library.brand_count} 品牌 / "
                f"{ac_library.device_count} 型号）"
            ),
            **categories,
        }

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMITTER): vol.In(emitters),
                    vol.Required(CONF_CATEGORY): vol.In(categories),
                }
            ),
            description_placeholders={
                "devices": str(len(library.devices)),
                "generated": library.generated or "未知",
            },
        )

    # ------------------------------------------------------------- 第 2 步

    async def async_step_brand(self, user_input: dict | None = None):
        """选品牌。"""
        library = await self._async_library()

        if user_input is not None:
            self._brand = int(user_input[CONF_BRAND])
            return await self.async_step_device()

        brands = {
            str(bid): f"{name}（{count}）"
            for bid, name, count in library.brands_in(self._category)
        }
        if not brands:
            return self.async_abort(reason="no_brands")

        return self.async_show_form(
            step_id="brand",
            data_schema=vol.Schema({vol.Required(CONF_BRAND): vol.In(brands)}),
            description_placeholders={
                "category": library.categories.get(self._category) or str(self._category),
                "count": str(len(brands)),
            },
        )

    # ------------------------------------------------------------- 第 3 步

    async def async_step_device(self, user_input: dict | None = None):
        """选型号并建 entry。"""
        library = await self._async_library()
        brand_name = library.brands.get(self._brand) or f"品牌 {self._brand}"

        if user_input is not None:
            device_id = int(user_input[CONF_DEVICE])
            device = library.get_device(device_id)
            if device is None:
                return self.async_abort(reason="device_gone")
            return self.async_create_entry(
                # 型号常自带品牌（如「TCL电视-1」）⇒ 交给 display_name 判断要不要前缀，
                # 否则实体 id 会变成 `remote.tcl_tcl电视_1`。
                title=library.display_name(brand_name, device["name"]),
                data={
                    CONF_EMITTER: self._emitter,
                    CONF_CATEGORY: self._category,
                    CONF_BRAND: self._brand,
                    CONF_DEVICE: device_id,
                    CONF_CARRIER: DEFAULT_CARRIER,
                    CONF_REPEATS: DEFAULT_REPEATS,
                },
            )

        devices: dict[str, str] = {}
        for device in library.devices_in(self._category, self._brand):
            label = device["name"]
            protocol = device.get("protocol")
            if protocol:
                # irext 的协议名会写 "RCA (56K)" 这种 —— 仅供选型参考，
                # 真载波以实测为准（本机 TCL 电视标 56K，实际 38K 才管用）。
                label = f"{label} · {protocol}"
            devices[str(device["id"])] = label

        if not devices:
            return self.async_abort(reason="no_devices")

        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema({vol.Required(CONF_DEVICE): vol.In(devices)}),
            description_placeholders={
                "brand": brand_name,
                "count": str(len(devices)),
            },
        )

    # ------------------------------------------------------- AC 专用步骤

    async def async_step_ac_brand(self, user_input: dict | None = None):
        """空调分支：选品牌（来自 irext 状态码库，233 家）。"""
        ac_library = await self._async_ac_library()

        if user_input is not None:
            self._ac_brand = user_input[CONF_BRAND]
            return await self.async_step_ac_device()

        brands = {
            name: f"{name}（{len(devs)} 型号）"
            for name, devs in sorted(
                ((b, ac_library.devices_in(b)) for b in ac_library.brands),
                key=lambda kv: (-len(kv[1]), kv[0]),
            )
        }
        if not brands:
            return self.async_abort(reason="no_brands")

        return self.async_show_form(
            step_id="ac_brand",
            data_schema=vol.Schema({vol.Required(CONF_BRAND): vol.In(brands)}),
            description_placeholders={
                "count": str(ac_library.brand_count),
                "devices": str(ac_library.device_count),
            },
        )

    async def async_step_ac_device(self, user_input: dict | None = None):
        """空调分支：选型号 bin 并建 entry（建完出 climate 实体）。"""
        ac_library = await self._async_ac_library()

        if user_input is not None:
            bin_name = user_input[CONF_DEVICE]
            # ⭐ 建 entry 前先解码一遍：坏 bin 在这里挡住（带原因 abort），
            #    不要等平台 setup 时才静默失败。
            try:
                await self.hass.async_add_executor_job(
                    ac_library.load_device, bin_name
                )
            except (OSError, ValueError) as err:
                return self.async_abort(reason="ac_decode_failed")
            title = CodeLibrary.display_name(
                self._ac_brand,
                f"空调 {bin_name.removesuffix('.bin').replace('irda_new_ac_', '')}",
            )
            return self.async_create_entry(
                title=title,
                data={
                    CONF_EMITTER: self._emitter,
                    CONF_CATEGORY: CATEGORY_AC,
                    CONF_BRAND: self._ac_brand,
                    CONF_DEVICE: bin_name,
                    CONF_CARRIER: DEFAULT_CARRIER,
                    CONF_REPEATS: DEFAULT_REPEATS,
                },
            )

        devices = {
            dev["bin"]: f"型号 {dev['device_name']}"
            for dev in ac_library.devices_in(self._ac_brand)
        }
        if not devices:
            return self.async_abort(reason="no_devices")

        return self.async_show_form(
            step_id="ac_device",
            data_schema=vol.Schema({vol.Required(CONF_DEVICE): vol.In(devices)}),
            description_placeholders={
                "brand": self._ac_brand,
                "count": str(len(devices)),
            },
        )

    # ------------------------------------------------------------- 选项

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> IrHubOptionsFlow:
        return IrHubOptionsFlow()


class IrHubOptionsFlow(config_entries.OptionsFlow):
    """只暴露两个真正需要调的旋钮：载波频率、发送次数。"""

    async def async_step_init(self, user_input: dict | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        entry = self.config_entry
        current = {**entry.data, **entry.options}

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_CARRIER,
                        default=int(current.get(CONF_CARRIER) or DEFAULT_CARRIER),
                    ): vol.All(vol.Coerce(int), vol.Range(min=20000, max=60000)),
                    vol.Required(
                        CONF_REPEATS,
                        default=int(current.get(CONF_REPEATS) or DEFAULT_REPEATS),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=50)),
                }
            ),
            description_placeholders={"title": entry.title},
        )
