"""Config flow / Options flow for IR Hub.

添加流程（四步表单）：
    1. user    —— 选**发射通道**（infrared / esphome / broadlink / mqtt，
                  对齐 SmartAC 的发射器抽象，让没有自制硬件的用户也能用）
    2. tx      —— 按通道填发射目标 + 选设备大类（空调 / 电视机 / 机顶盒 …）
    3. brand   —— 选品牌
    4. device  —— 选型号
    5. test    —— **发一帧实测码**（设备=power 键；空调=关机帧），用户确认
                  设备有反应才建 entry；没反应退回第 4 步重选，不生成废 entry
                  （对齐 SmartAC 的"测试通过才加入"体验）

建完之后，`remote.<品牌>_<型号>`（或空调的 `climate.*`）就出现了；按键名即
activity_list。载波频率、发送次数、Broadlink delay 在 **选项** 里改
（OptionsFlow）—— "38k 还是 56k" 只能靠实测定，留个不用重加集成的开关很重要。
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
    MQTT_FORMATS,
    TX_BROADLINK,
    TX_ESPHOME,
    TX_INFRARED,
    TX_MQTT,
    TX_TYPES,
)
from .ir_command import build_raw_command
from .library import CodeLibrary
from .transmitter import async_send_timings, async_validate_target

_LOGGER = logging.getLogger(__name__)

# 实测确认步的下拉选项（`vol.In(字典)`：key 是提交值，value 是显示文本）。
# "skip" 留给"人不在设备旁 / 发射器还没上电"的场合 —— 不强制。
CONF_TEST_RESULT = "test_result"
TEST_OK = "ok"
TEST_RETRY = "retry"
TEST_NEXT = "next"
TEST_SKIP = "skip"
TEST_RESULT_OPTIONS = {
    TEST_OK: "有反应，完成添加",
    TEST_NEXT: "没反应，自动试下一个型号",
    TEST_RETRY: "没反应，退回重选型号",
    TEST_SKIP: "跳过测试，直接添加",
}
# 空调两段式实测的第一段（开机帧）：ok 只是"继续"，还没建 entry
AC_TEST_ON_OPTIONS = {
    TEST_OK: "有反应（空调开机了），继续关机确认",
    TEST_NEXT: "没反应，自动试下一个型号",
    TEST_RETRY: "没反应，退回重选型号",
    TEST_SKIP: "跳过这一步",
}

# 发射通道下拉（value → 显示文本）
TX_TYPE_OPTIONS = {
    TX_INFRARED: "infrared 发射器（ESPHome ir_rf_proxy 等，推荐）",
    TX_BROADLINK: "Broadlink（现成遥控宝，走 remote.send_command）",
    TX_ESPHOME: "ESPHome 动作（SmartAC 兼容，如 esphome.xxx_send_raw_command）",
    TX_MQTT: "MQTT（默认发 SmartAC 裸时序数组，填 topic）",
}

# 各通道目标字段的表单提示（description_placeholders 用）
_TX_TARGET_LABELS = {
    TX_INFRARED: "下拉选择 infrared 发射器实体",
    TX_ESPHOME: "如 esphome.ir_control_send_raw_command（或只写动作名）",
    TX_BROADLINK: "下拉选择 Broadlink 的 remote 实体",
    TX_MQTT: "如 tcl_ir/ir_send（SmartAC/tcl-ir 桥，裸数组）；Tasmota 固件也可",
}


class IrHubConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Add one remote (码库设备 + 发射通道的组合)。"""

    VERSION = 1

    def __init__(self) -> None:
        self._tx_type: str = TX_INFRARED
        self._tx_target: str = ""
        self._category: int | str = 0
        self._brand: int = 0
        self._library: CodeLibrary | None = None
        self._ac_library: AcLibrary | None = None
        self._ac_brand: str = ""
        self._device_id: int = 0
        self._key_candidates: list[int] = []   # 品牌内全部候选（"自动试下一个"用）
        self._key_index: int = 0
        self._ac_bin: str = ""
        self._ac_candidates: list[str] = []
        self._ac_index: int = 0
        self._ac_code: dict | None = None

    # ------------------------------------------------------------------ 工具

    async def _async_send_test(self, timings: list[int]) -> None:
        """把一帧测试码经**所选通道**发出去（此刻还没有任何实体）。"""
        await async_send_timings(
            self.hass,
            self._tx_type,
            self._tx_target,
            timings,
            carrier=DEFAULT_CARRIER,
            delay=DEFAULT_TX_DELAY,
        )

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

    def _async_category_schema(self) -> vol.Schema:
        """设备大类下拉（含 AC 特殊项）—— 各通道表单共用。"""
        library = self._library
        assert library is not None
        categories = {
            str(cid): f"{name}（{count} 台）"
            for cid, name, count in library.categories_available()
        }
        ac_library = self._ac_library
        assert ac_library is not None
        categories = {
            CATEGORY_AC: (
                f"空调 · 温控面板（{ac_library.brand_count} 品牌 / "
                f"{ac_library.device_count} 型号）"
            ),
            **categories,
        }
        return vol.Schema(
            {
                vol.Required(CONF_TX_TARGET): self._tx_target_validator(),
                vol.Required(CONF_CATEGORY): vol.In(categories),
            }
        )

    def _tx_target_validator(self):
        """按通道生成目标字段的校验器。"""
        if self._tx_type == TX_INFRARED:
            emitters = infrared.async_get_emitters(self.hass)
            return vol.In(emitters)
        if self._tx_type == TX_BROADLINK:
            remotes = sorted(
                state.entity_id
                for state in self.hass.states.async_all("remote")
            )
            return vol.In(remotes)
        # esphome / mqtt：自由文本
        return str

    # ------------------------------------------------------------- 第 1 步

    async def async_step_user(self, user_input: dict | None = None):
        """选发射通道。"""
        await self._async_library()
        await self._async_ac_library()

        if user_input is not None:
            self._tx_type = user_input[CONF_TX_TYPE]
            return await self.async_step_tx()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_TX_TYPE, default=TX_INFRARED): vol.In(TX_TYPE_OPTIONS)}
            ),
            description_placeholders={
                "devices": str(len((await self._async_library()).devices))
            },
        )

    async def async_step_tx(self, user_input: dict | None = None):
        """按通道填发射目标 + 选设备大类。"""
        library = await self._async_library()

        # infrared 通道且没有 emitter ⇒ 提前引导（别的通道不依赖 infrared）
        if self._tx_type == TX_INFRARED and user_input is None:
            if not infrared.async_get_emitters(self.hass):
                return self.async_abort(reason="no_emitters")

        if user_input is not None:
            self._tx_target = str(user_input[CONF_TX_TARGET]).strip()
            error = await async_validate_target(
                self.hass, self._tx_type, self._tx_target
            )
            if error is not None:
                return self.async_show_form(
                    step_id="tx",
                    data_schema=self._async_category_schema(),
                    errors={"base": error},
                    description_placeholders=self._tx_placeholders(library),
                )
            if user_input[CONF_CATEGORY] == CATEGORY_AC:
                self._category = CATEGORY_AC
                return await self.async_step_ac_brand()
            self._category = int(user_input[CONF_CATEGORY])
            return await self.async_step_brand()

        return self.async_show_form(
            step_id="tx",
            data_schema=self._async_category_schema(),
            description_placeholders=self._tx_placeholders(library),
        )

    def _tx_placeholders(self, library: CodeLibrary) -> dict[str, str]:
        return {
            "tx_type": dict(TX_TYPE_OPTIONS).get(self._tx_type, self._tx_type),
            "tx_target_hint": _TX_TARGET_LABELS.get(self._tx_type, ""),
            "devices": str(len(library.devices)),
            "generated": library.generated or "未知",
        }

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
        """选型号。"""
        library = await self._async_library()
        brand_name = library.brands.get(self._brand) or f"品牌 {self._brand}"

        if user_input is not None:
            device_id = int(user_input[CONF_DEVICE])
            device = library.get_device(device_id)
            if device is None:
                return self.async_abort(reason="device_gone")
            # ⭐ 不直接建 entry —— 先去 test 步发一帧实测码，确认设备有反应
            #    再落库，避免"加错了删掉重来"。
            #    同时记下品牌内全部候选：test 步"没反应"可以自动试下一个型号。
            self._device_id = device_id
            self._key_candidates = [int(d["id"]) for d in library.devices_in(self._category, self._brand)]
            self._key_index = (
                self._key_candidates.index(device_id)
                if device_id in self._key_candidates
                else 0
            )
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
        第一个可用键。"next" = 自动前进到品牌内下一个型号再发（机顶盒这类
        型号多的品牌不用逐个回下拉重选）。
        """
        library = await self._async_library()
        brand_name = library.brands.get(self._brand) or f"品牌 {self._brand}"

        if user_input is not None:
            result = user_input[CONF_TEST_RESULT]
            if result == TEST_RETRY:
                return await self.async_step_device()
            if result == TEST_NEXT:
                nxt = self._key_index + 1
                if nxt >= len(self._key_candidates):
                    return self.async_abort(reason="test_exhausted")
                self._key_index = nxt
                self._device_id = self._key_candidates[nxt]
            else:  # ok / skip
                device = library.get_device(self._device_id)
                if device is None:
                    return self.async_abort(reason="device_gone")
                return self._async_finish_device(library, device)

        device = library.get_device(self._device_id)
        if device is None:
            return self.async_abort(reason="device_gone")

        errors: dict[str, str] = {}
        keys = library.key_names(device)
        key = keys[0] if keys else None
        timings = library.get_timings(self._device_id, key) if key else None
        if timings is None:
            errors["base"] = "no_test_key"
        else:
            try:
                await self._async_send_test(timings)
            except Exception:  # noqa: BLE001 —— 发射器掉线/拒发都必须落到表单
                _LOGGER.exception("IR Hub: 测试码发送失败（%s/%s）", self._tx_type, self._tx_target)
                errors["base"] = "send_failed"

        return self.async_show_form(
            step_id="test",
            data_schema=vol.Schema(
                {vol.Required(CONF_TEST_RESULT): vol.In(TEST_RESULT_OPTIONS)}
            ),
            errors=errors or None,
            description_placeholders={
                "brand": brand_name,
                "model": device["name"],
                "index": str(self._key_index + 1),
                "total": str(max(len(self._key_candidates), 1)),
                "key": key or "—",
            },
        )

    def _async_finish_device(self, library: CodeLibrary, device: dict):
        """建普通设备的 entry（型号常自带品牌 ⇒ display_name 去重前缀）。"""
        brand_name = library.brands.get(self._brand) or f"品牌 {self._brand}"
        return self.async_create_entry(
            title=library.display_name(brand_name, device["name"]),
            data=self._entry_data(),
        )

    def _entry_data(self) -> dict:
        """entry.data 公共部分：发射通道 + 载波 + 次数。

        infrared 通道额外写一份 CONF_EMITTER（兼容旧字段/旧脚本）。
        """
        data = {
            CONF_TX_TYPE: self._tx_type,
            CONF_TX_TARGET: self._tx_target,
            CONF_CARRIER: DEFAULT_CARRIER,
            CONF_REPEATS: DEFAULT_REPEATS,
        }
        if self._category == CATEGORY_AC:
            data[CONF_CATEGORY] = CATEGORY_AC
            data[CONF_BRAND] = self._ac_brand
            data[CONF_DEVICE] = self._ac_bin
        else:
            data[CONF_CATEGORY] = self._category
            data[CONF_BRAND] = self._brand
            data[CONF_DEVICE] = self._device_id
        if self._tx_type == TX_INFRARED:
            data[CONF_EMITTER] = self._tx_target
        return data

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
            except (OSError, ValueError):
                return self.async_abort(reason="ac_decode_failed")
            self._ac_bin = bin_name
            # 品牌内全部候选（"自动试下一个"用），顺序与下拉一致
            self._ac_candidates = [
                d["bin"] for d in ac_library.devices_in(self._ac_brand)
            ]
            self._ac_index = (
                self._ac_candidates.index(bin_name)
                if bin_name in self._ac_candidates
                else 0
            )
            return await self.async_step_ac_test_on()

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

    def _ac_on_frame(self) -> list[int]:
        """开机实测帧：mode/fan 优先 auto、温度优先 26（SmartAC async_test 同款选择）。"""
        commands = (self._ac_code or {}).get("commands") or {}
        if not commands:
            return []
        mode_key = "auto" if "auto" in commands else next(iter(commands))
        fans = commands[mode_key]
        fan_key = "auto" if "auto" in fans else next(iter(fans))
        temps = fans[fan_key]
        temp_key = "26" if "26" in temps else next(iter(temps))
        return list(temps[temp_key])

    async def async_step_ac_test_on(self, user_input: dict | None = None):
        """空调实测第一段：发**开机帧**，确认空调有反应。

        ⚠️ 为什么要两段（09-28 用户实测教训）：只发关机帧时，空调**本来就关着**
        ⇒ 毫无可见反应，用户无从判断发射链路好坏。SmartAC 是开机→关机两段确认，
        这里对齐。第二段（关机帧）在 async_step_ac_test。
        """
        ac_library = await self._async_ac_library()

        if user_input is not None:
            result = user_input[CONF_TEST_RESULT]
            if result == TEST_RETRY:
                return await self.async_step_ac_device()
            if result == TEST_NEXT:
                nxt = self._ac_index + 1
                if nxt >= len(self._ac_candidates):
                    return self.async_abort(reason="test_exhausted")
                self._ac_index = nxt
                self._ac_bin = self._ac_candidates[nxt]
                try:
                    self._ac_code = await self.hass.async_add_executor_job(
                        ac_library.load_device, self._ac_bin
                    )
                except (OSError, ValueError):
                    return self.async_abort(reason="ac_decode_failed")
            elif result == TEST_OK:
                return await self.async_step_ac_test()
            else:  # skip —— 直接进第二段（关机确认）
                return await self.async_step_ac_test()
            # next：落到下面用新 bin 重发开机帧

        errors: dict[str, str] = {}
        timings = self._ac_on_frame()
        try:
            if not timings:
                errors["base"] = "no_test_key"
            else:
                await self._async_send_test(timings)
        except Exception:  # noqa: BLE001 —— 发射器掉线/拒发都必须落到表单
            _LOGGER.exception(
                "IR Hub: 空调开机帧发送失败（%s/%s）", self._tx_type, self._tx_target
            )
            errors["base"] = "send_failed"

        model_short = self._ac_bin.removesuffix(".bin").replace("irda_new_ac_", "")
        return self.async_show_form(
            step_id="ac_test_on",
            data_schema=vol.Schema(
                {vol.Required(CONF_TEST_RESULT): vol.In(AC_TEST_ON_OPTIONS)}
            ),
            errors=errors or None,
            description_placeholders={
                "brand": self._ac_brand,
                "model": model_short,
                "index": str(self._ac_index + 1),
                "total": str(max(len(self._ac_candidates), 1)),
            },
        )

    async def async_step_ac_test(self, user_input: dict | None = None):
        """空调实测第二段：发一帧**关机码**，确认后才建 entry。

        第一段（开机帧）见 async_step_ac_test_on —— 两段都确认，对齐 SmartAC。
        """
        ac_library = await self._async_ac_library()

        if user_input is not None:
            result = user_input[CONF_TEST_RESULT]
            if result == TEST_RETRY:
                return await self.async_step_ac_device()
            if result == TEST_NEXT:
                nxt = self._ac_index + 1
                if nxt >= len(self._ac_candidates):
                    return self.async_abort(reason="test_exhausted")
                self._ac_index = nxt
                self._ac_bin = self._ac_candidates[nxt]
                try:
                    self._ac_code = await self.hass.async_add_executor_job(
                        ac_library.load_device, self._ac_bin
                    )
                except (OSError, ValueError):
                    return self.async_abort(reason="ac_decode_failed")
            else:  # ok / skip
                return self._async_finish_ac(ac_library)

        errors: dict[str, str] = {}
        timings = (self._ac_code or {}).get("off") or []
        try:
            if not timings:
                errors["base"] = "no_test_key"
            else:
                await self._async_send_test(timings)
        except Exception:  # noqa: BLE001 —— 发射器掉线/拒发都必须落到表单
            _LOGGER.exception("IR Hub: 空调测试码发送失败（%s/%s）", self._tx_type, self._tx_target)
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
                "index": str(self._ac_index + 1),
                "total": str(max(len(self._ac_candidates), 1)),
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
            data=self._entry_data(),
        )

    # ------------------------------------------------------------- 选项

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> IrHubOptionsFlow:
        return IrHubOptionsFlow()


class IrHubOptionsFlow(config_entries.OptionsFlow):
    """载波频率、发送次数、Broadlink delay —— 真正需要调的旋钮。"""

    async def async_step_init(self, user_input: dict | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        entry = self.config_entry
        current = {**entry.data, **entry.options}

        schema = {
            vol.Required(
                CONF_CARRIER,
                default=int(current.get(CONF_CARRIER) or DEFAULT_CARRIER),
            ): vol.All(vol.Coerce(int), vol.Range(min=20000, max=60000)),
            vol.Required(
                CONF_REPEATS,
                default=int(current.get(CONF_REPEATS) or DEFAULT_REPEATS),
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=50)),
            vol.Required(
                CONF_TX_DELAY,
                default=float(current.get(CONF_TX_DELAY) or DEFAULT_TX_DELAY),
            ): vol.All(vol.Coerce(float), vol.Range(min=0, max=10)),
        }
        # MQTT 载荷格式只有 mqtt 通道有意义（smartac 裸数组 / tasmota RAW JSON）
        if (current.get(CONF_TX_TYPE) or TX_INFRARED) == TX_MQTT:
            schema[vol.Required(
                CONF_MQTT_FORMAT,
                default=current.get(CONF_MQTT_FORMAT) or DEFAULT_MQTT_FORMAT,
            )] = vol.In(MQTT_FORMATS)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema),
            description_placeholders={"title": entry.title},
        )
