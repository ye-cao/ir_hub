"""Config flow / Options flow for IR Hub.

添加流程（四步表单）：
    1. user    —— 选一个 infrared emitter（来自 ESPHome 的 ir_rf_proxy 实体）
                  + 选设备大类（空调 / 电视机 / 机顶盒 / 风扇 …）
    2. brand   —— 选品牌
    3. device  —— 选型号
    4. test    —— **发一帧实测码**（设备=power 键；空调=关机帧），用户确认
                  设备有反应才建 entry；没反应退回第 3 步重选，不生成废 entry
                  （对齐 SmartAC 的"测试通过才加入"体验）

建完之后，`remote.<品牌>_<型号>`（或空调的 `climate.*`）就出现了；按键名即
activity_list。载波频率与发送次数在 **选项** 里改（OptionsFlow）—— 因为
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
from .ir_command import build_raw_command
from .library import CodeLibrary

_LOGGER = logging.getLogger(__name__)

# 实测确认步的下拉选项（`vol.In(字典)`：key 是提交值，value 是显示文本）。
# "skip" 留给"人不在设备旁 / 发射器还没上电"的场合 —— 不强制。
CONF_TEST_RESULT = "test_result"
TEST_RETRY = "retry"
TEST_RESULT_OPTIONS = {
    "ok": "有反应，完成添加",
    "retry": "没反应，退回重选型号",
    "skip": "跳过测试，直接添加",
}


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
        self._device_id: int = 0
        self._ac_bin: str = ""
        self._ac_code: dict | None = None

    # ------------------------------------------------------------------ 工具

    async def _async_send_test(self, timings: list[int]) -> None:
        """把一帧测试码直接经所选 emitter 发出去（此刻还没有任何实体）。

        `infrared.async_send_command(hass, emitter_entity_id, command)` 是
        infrared building block 的公开 API —— consumer 实体的 `_send_command()`
        底层走的也是它，config flow 阶段直接调它是合法路径。
        """
        command = build_raw_command(
            timings, carrier=DEFAULT_CARRIER, repeats=DEFAULT_REPEATS
        )
        await infrared.async_send_command(self.hass, self._emitter, command)

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
            # ⭐ 不直接建 entry —— 先去 test 步发一帧实测码，确认设备有反应
            #    再落库，避免"加错了删掉重来"。
            self._device_id = device_id
            return await self.async_step_test()

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

    # ------------------------------------------------------- 发送实测（第 4 步）

    async def async_step_test(self, user_input: dict | None = None):
        """发一帧实测码让用户确认 —— 有反应才建 entry，没反应退回重选。

        对齐 SmartAC 的体验：码库选型只是"候选"，**设备真响应了才算数**。
        测试键取 power（key_names 已把 power 排第一），没有 power 就取
        第一个可用键。
        """
        library = await self._async_library()
        device = library.get_device(self._device_id)
        if device is None:
            return self.async_abort(reason="device_gone")
        brand_name = library.brands.get(self._brand) or f"品牌 {self._brand}"

        if user_input is not None:
            if user_input[CONF_TEST_RESULT] == TEST_RETRY:
                return await self.async_step_device()
            return self._async_finish_device(library, device)

        errors: dict[str, str] = {}
        keys = library.key_names(device)
        key = keys[0] if keys else None
        timings = library.get_timings(self._device_id, key) if key else None
        if timings is None:
            errors["base"] = "no_test_key"
        else:
            try:
                await self._async_send_test(timings)
            except Exception:  # noqa: BLE001 —— emitter 掉线/拒发都必须落到表单
                _LOGGER.exception("IR Hub: 测试码发送失败（emitter=%s）", self._emitter)
                errors["base"] = "send_failed"

        return self.async_show_form(
            step_id="test",
            data_schema=vol.Schema(
                {vol.Required(CONF_TEST_RESULT): vol.In(TEST_RESULT_OPTIONS)}
            ),
            errors=errors or None,
            description_placeholders={"brand": brand_name, "key": key or "—"},
        )

    def _async_finish_device(self, library: CodeLibrary, device: dict):
        """建普通设备的 entry（型号常自带品牌 ⇒ display_name 去重前缀）。"""
        brand_name = library.brands.get(self._brand) or f"品牌 {self._brand}"
        return self.async_create_entry(
            title=library.display_name(brand_name, device["name"]),
            data={
                CONF_EMITTER: self._emitter,
                CONF_CATEGORY: self._category,
                CONF_BRAND: self._brand,
                CONF_DEVICE: int(device["id"]),
                CONF_CARRIER: DEFAULT_CARRIER,
                CONF_REPEATS: DEFAULT_REPEATS,
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
            #    不要等平台 setup 时才静默失败。解码结果留给 ac_test 步发测试码。
            try:
                self._ac_code = await self.hass.async_add_executor_job(
                    ac_library.load_device, bin_name
                )
            except (OSError, ValueError) as err:
                return self.async_abort(reason="ac_decode_failed")
            self._ac_bin = bin_name
            return await self.async_step_ac_test()

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

    async def async_step_ac_test(self, user_input: dict | None = None):
        """空调分支的实测步：发一帧**关机码**，确认空调有反应再建 entry。

        为什么发 off 而不是开机：开机帧需要"模式×风速×温度"全组合才有意义，
        而 off 是独立一帧、对所有空调语义唯一（滴一声/关机）—— 测试信号最干净。
        """
        ac_library = await self._async_ac_library()

        if user_input is not None:
            if user_input[CONF_TEST_RESULT] == TEST_RETRY:
                return await self.async_step_ac_device()
            return self._async_finish_ac(ac_library)

        errors: dict[str, str] = {}
        timings = (self._ac_code or {}).get("off") or []
        try:
            if not timings:
                errors["base"] = "no_test_key"
            else:
                await self._async_send_test(timings)
        except Exception:  # noqa: BLE001 —— emitter 掉线/拒发都必须落到表单
            _LOGGER.exception("IR Hub: 空调测试码发送失败（emitter=%s）", self._emitter)
            errors["base"] = "send_failed"

        model_short = self._ac_bin.removesuffix(".bin").replace("irda_new_ac_", "")
        return self.async_show_form(
            step_id="ac_test",
            data_schema=vol.Schema(
                {vol.Required(CONF_TEST_RESULT): vol.In(TEST_RESULT_OPTIONS)}
            ),
            errors=errors or None,
            description_placeholders={
                "brand": self._ac_brand,
                "model": model_short,
            },
        )

    def _async_finish_ac(self, ac_library: AcLibrary):
        """建空调 entry（建完出 climate 温控面板）。"""
        title = CodeLibrary.display_name(
            self._ac_brand,
            f"空调 {self._ac_bin.removesuffix('.bin').replace('irda_new_ac_', '')}",
        )
        return self.async_create_entry(
            title=title,
            data={
                CONF_EMITTER: self._emitter,
                CONF_CATEGORY: CATEGORY_AC,
                CONF_BRAND: self._ac_brand,
                CONF_DEVICE: self._ac_bin,
                CONF_CARRIER: DEFAULT_CARRIER,
                CONF_REPEATS: DEFAULT_REPEATS,
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
