"""Config flow / Options flow。

添加流程（多步表单）：

    1. user    —— 选**发射通道**（infrared / esphome / broadlink / mqtt）
                  和配置方式（手动挑码库 / 用遥控器配对）
    2. tx      —— 按通道填发射目标 + 选设备大类（空调 / 电视机 / 机顶盒 …）
    3. brand   —— 选品牌
    4. device  —— 选型号
    5. test    —— **发一帧实测码**（设备 = power 键；空调 = 开机 → 关机两段），
                  用户确认有反应才建 entry；没反应可退回重选或自动试下一个型号

建完之后出现 `remote.<品牌>_<型号>`（空调是 `climate.*`），按键名即 activity_list。
载波频率、发送次数、Broadlink delay 在**选项**里改 —— "38k 还是 56k" 只能靠实测定，
留个不用重加集成的开关很重要。
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
from .learn_match import capture_features, match_timings, normalize_capture
from .learn_match_ac import match_ac
from .library import CodeLibrary
from .transmitter import async_send_timings, async_validate_target

_LOGGER = logging.getLogger(__name__)

# 学习模式依赖 HA infrared 的**接收端** API（2026.4 只引入了发射端，接收端是后续
# 版本才补的）。老版本 HA 下做特性探测，学不了就明确告知，而不是抛 AttributeError。
_HAS_RECEIVER_API = all(
    hasattr(infrared, name)
    for name in ("async_get_receivers", "async_subscribe_receiver")
)

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

# ----------------------------------------------------------- 配置方式（第 1 步选）
# 保留原来的"逐级挑码库"（browse），另加"拿原遥控器按键自动找码"（learn）。
CONF_MODE = "mode"
MODE_BROWSE = "browse"
MODE_LEARN = "learn"
MODE_OPTIONS = {
    MODE_BROWSE: "手动选择码库（大类 → 品牌 → 型号，然后发测试码确认）",
    MODE_LEARN: "用遥控器配对（对准红外接收器按一下电源键，自动在码库里找）",
}

# ----------------------------------------------------------- 学习模式专用
CONF_RECEIVER = "receiver"
CONF_LEARN_ACTION = "learn_action"
LEARN_ACTION_NEXT = "next"
LEARN_ACTION_SKIP = "skip"
LEARN_ACTION_OPTIONS = {
    LEARN_ACTION_NEXT: "按完了，继续（用两键结果收敛型号）",
    LEARN_ACTION_SKIP: "跳过（只用电源键匹配）",
}

# 学习测试步的下拉（学习模式没有"品牌内下一个型号"的概念，去掉 next）
LEARN_TEST_OPTIONS = {
    TEST_OK: "有反应，完成添加",
    TEST_RETRY: "没反应，退回重选候选",
    TEST_SKIP: "跳过测试，直接添加",
}

# 学习匹配：显示多少帧候选、最多给用户多少设备候选。
# 8 帧既够召回又不会让下拉爆炸（双键交集下电视机 ≤108 个设备，其余更少）。
# 空调走状态码库（learn_match_ac），候选是"型号"而不是"帧"，所以不截 top_n，
# 按相似度排序后截到 _LEARN_MAX_CANDIDATES 展示。
_LEARN_TOP_N = 8
_LEARN_MAX_CANDIDATES = 60

# 学习候选里"看起来对"的相似度下限。连续分口径下（见 learn_match._similarity）：
# 真帧（在库里）≈0.90~0.95，同协议族近似型号 ≈0.85~0.93，库外遥控的最近邻
# 通常只有 0.62~0.75（实测：Midea/RCA 族真值 0.945 召回 20 个候选；一只 300 段
# NEC 系的库外遥控只剩 1 个候选、最高 0.67）。用它给用户一句"信不信"的判词。
_LEARN_TRUST_SCORE = 0.85
_LEARN_WEAK_SCORE = 0.75

# 「退回重按」哨兵值。低分/单候选时用户其实无路可走（下拉里全是错的），
# 没有这一项就只能关掉对话框重来 —— 那等于把"配对失败"变成"配对没有出口"。
_LEARN_BACK = "__back__"
# 各通道目标字段的表单提示（description_placeholders 用）
_TX_TARGET_LABELS = {
    TX_INFRARED: "下拉选择 infrared 发射器实体",
    TX_ESPHOME: "如 esphome.ir_control_send_raw_command（或只写动作名）",
    TX_BROADLINK: "下拉选择 Broadlink 的 remote 实体",
    TX_MQTT: "如 tcl_ir/ir_send（SmartAC/tcl-ir 桥，裸数组）；Tasmota 固件也可",
}


class IrHubConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """添加一台遥控（码库设备 + 发射通道的组合）。"""

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
        # 该 bin 被几个品牌共用（手动路径恒为 1 —— 品牌是用户自己选的；配对路径
        # 反查得到的是"其中一个"品牌，多品牌时必须说成共用码，不能冒认一个牌子）
        self._ac_brand_count: int = 1
        # ---- 学习模式状态 ----
        self._receiver: str = ""
        self._learn_unsub = None
        self._learn_captures: list[tuple[list[int], int | None]] = []
        self._learn_capture1: list[int] | None = None
        self._learn_capture2: list[int] | None = None
        self._learn_candidates: list | None = None   # int（按键类 id）或 str（空调 bin）
        self._learn_top_score: float = 0.0
        self._learn_used_two_keys: bool = False
        self._from_learn: bool = False               # 是否由"配对"分支进的空调实测步
        # 订阅接收器失败时的原因（空串 = 订阅正常）。必须留给用户看 —— 订阅失败与
        # "收到 0 帧"在界面上长得一模一样，吞掉就会变成无从下手的"没反应"。
        self._learn_subscribe_error: str = ""

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

    def _category_options(self) -> dict[str, str]:
        """设备大类下拉选项（含空调）—— 手动 / 配对两种流程共用。"""
        library = self._library
        assert library is not None
        ac_library = self._ac_library
        assert ac_library is not None
        return {
            CATEGORY_AC: (
                f"空调 · 温控面板（{ac_library.brand_count} 品牌 / "
                f"{ac_library.device_count} 型号）"
            ),
            **{
                str(cid): f"{name}（{count} 台）"
                for cid, name, count in library.categories_available()
            },
        }

    def _category_label(self) -> str:
        """当前所选大类的显示名（配对流程的占位符用）。"""
        if self._category == CATEGORY_AC:
            return "空调"
        library = self._library
        if library is None:
            return str(self._category)
        return library.categories.get(self._category) or str(self._category)

    def _async_category_schema(self) -> vol.Schema:
        """设备大类下拉（含 AC 特殊项）—— 各通道表单共用。"""
        return vol.Schema(
            {
                vol.Required(CONF_TX_TARGET): self._tx_target_validator(),
                vol.Required(CONF_CATEGORY): vol.In(self._category_options()),
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
        """选发射通道 + 配置方式（手动挑码库 / 用遥控器配对）。"""
        await self._async_library()
        await self._async_ac_library()

        if user_input is not None:
            self._tx_type = user_input[CONF_TX_TYPE]
            # .get 兜底：HA 升级时正开着的旧流程不带这个新字段，
            # 直接取会 KeyError 崩掉用户手上的对话框。
            if user_input.get(CONF_MODE, MODE_BROWSE) == MODE_LEARN:
                return await self.async_step_learn_setup()
            return await self.async_step_tx()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TX_TYPE, default=TX_INFRARED): vol.In(TX_TYPE_OPTIONS),
                    vol.Required(CONF_MODE, default=MODE_BROWSE): vol.In(MODE_OPTIONS),
                }
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
            # 不直接建 entry —— 先去 test 步发一帧实测码确认。同时记下品牌内全部
            # 候选，让 test 步"没反应"时能自动试下一个型号。
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
        """发一帧实测码让用户确认 —— 有反应才建 entry，没反应可退回重选。

        码库选型只是"候选"，**设备真响应了才算数**。测试键取 power
        （key_names 已把 power 排第一），没有 power 就取第一个可用键。
        "next" = 自前进到品牌内下一个型号再发（型号多的品牌不用逐个回下拉重选）。
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
        """建普通设备的 entry（型号常自带品牌 ⇒ display_name 去前缀）。"""
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
        """空调分支：选品牌（来自 irext 状态码库）。"""
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
        """空调分支：选型号 bin；解码通过后进实测步。"""
        ac_library = await self._async_ac_library()

        if user_input is not None:
            bin_name = user_input[CONF_DEVICE]
            # 先解码一遍：坏 bin 在这里带原因 abort，不要等平台 setup 时才静默失败。
            # 解码结果留给 ac_test 步发测试码。
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
        """开机实测帧：mode/fan 优先 auto、温度优先 26（SmartAC 同款选帧逻辑）。"""
        commands = (self._ac_code or {}).get("commands") or {}
        if not commands:
            return []
        mode_key = "auto" if "auto" in commands else next(iter(commands))
        fans = commands[mode_key]
        fan_key = "auto" if "auto" in fans else next(iter(fans))
        temps = fans[fan_key]
        temp_key = "26" if "26" in temps else next(iter(temps))
        return list(temps[temp_key])

    async def _ac_advance(self, ac_library: AcLibrary) -> bool | None:
        """「自动试下一个型号」：切到下一个候选并解码。

        True = 已切好；False = 没有下一个了；None = 新 bin 解码失败。
        """
        nxt = self._ac_index + 1
        if nxt >= len(self._ac_candidates):
            return False
        self._ac_index = nxt
        self._ac_bin = self._ac_candidates[nxt]
        if self._from_learn:
            # 配对路径的候选跨品牌 ⇒ 品牌 / 共用数要跟着换，否则实测文案会张冠李戴
            self._ac_brand, self._ac_brand_count = self._ac_resolve_brand(
                ac_library, self._ac_bin
            )
        try:
            self._ac_code = await self.hass.async_add_executor_job(
                ac_library.load_device, self._ac_bin
            )
        except (OSError, ValueError):
            return None
        return True

    async def async_step_ac_test_on(self, user_input: dict | None = None):
        """空调实测第一段：发**开机帧**，确认空调有反应。

        ⚠️ 为什么要两段：只发关机帧的话，空调本来就关着 ⇒ 毫无可见反应，
        用户无从判断发射链路好坏。所以先开机、再关机，两段都确认。
        """
        ac_library = await self._async_ac_library()

        if user_input is not None:
            result = user_input[CONF_TEST_RESULT]
            if result == TEST_RETRY:
                return await (
                    self.async_step_learn_pick()
                    if self._from_learn
                    else self.async_step_ac_device()
                )
            if result == TEST_NEXT:
                moved = await self._ac_advance(ac_library)
                if moved is None:
                    return self.async_abort(reason="ac_decode_failed")
                if not moved:
                    return self.async_abort(reason="test_exhausted")
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
                "brand": self._ac_brand_label(),
                "model": model_short,
                "index": str(self._ac_index + 1),
                "total": str(max(len(self._ac_candidates), 1)),
            },
        )

    async def async_step_ac_test(self, user_input: dict | None = None):
        """空调实测第二段：发一帧**关机码**，确认后才建 entry。"""
        ac_library = await self._async_ac_library()

        if user_input is not None:
            result = user_input[CONF_TEST_RESULT]
            if result == TEST_RETRY:
                return await (
                    self.async_step_learn_pick()
                    if self._from_learn
                    else self.async_step_ac_device()
                )
            if result == TEST_NEXT:
                moved = await self._ac_advance(ac_library)
                if moved is None:
                    return self.async_abort(reason="ac_decode_failed")
                if not moved:
                    return self.async_abort(reason="test_exhausted")
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
                "brand": self._ac_brand_label(),
                "model": model_short,
                "index": str(self._ac_index + 1),
                "total": str(max(len(self._ac_candidates), 1)),
            },
        )

    def _ac_brand_label(self) -> str:
        """测两步文案里的"向谁发码"。

        一个 bin 常被多个品牌共用（509 个里 149 个，最多一个有 216 个品牌），
        配对路径反查出来的只是其中一个 ⇒ 多品牌时说清是共用码。
        """
        if self._ac_brand_count > 1:
            return f"{self._ac_brand_count} 个品牌共用"
        return self._ac_brand or "未知品牌"

    @staticmethod
    def _ac_resolve_brand(ac_library: AcLibrary, bin_name: str) -> tuple[str, int]:
        """配对路径反查品牌：返回 (品牌名, 共用品牌数)。

        ⚠️ 多品牌共用时品牌名返回**空串** —— 真正的未知量是"这台机器是哪个牌子"。
        拿 `brand_of` 的首个品牌当名字会写出 "Armcor 空调 11272" 这种用户认不出的
        entry / 设备名（11272 实际被 79 个品牌共用，其中有美的）。空串会让 entry
        标题与设备名都退化成 "空调 11272"，这才是诚实的。
        """
        count = len(ac_library.brands_of(bin_name))
        return (ac_library.brand_of(bin_name) if count == 1 else ""), count

    def _async_finish_ac(self, ac_library: AcLibrary):
        """建空调 entry（建完出 climate 温控面板）。"""
        model_short = self._ac_bin.removesuffix(".bin").replace("irda_new_ac_", "")
        # 配对路径下品牌可能是空串（共用码，见 `_ac_resolve_brand`）；
        # 手动路径品牌是用户自己选的，一定非空。
        return self.async_create_entry(
            title=CodeLibrary.display_name(self._ac_brand, f"空调 {model_short}"),
            data=self._entry_data(),
        )

    # ----------------------------------------------- 学习模式：用遥控器配对
    #
    # 流程：learn_setup（发射目标 + 接收器 + 大类）
    #       → learn_press（按电源键，捕获）
    #       → learn_press2（可选：再按一个键，用两键交集收敛型号）
    #       → learn_pick（从候选设备里挑一个）
    #       → learn_test（发该设备 power 码实测）→ 建 entry
    #
    # 匹配引擎见 learn_match.py；效果见 tools/verify_learn_match.py
    # （默认抽 120 例帧命中 100%、双键真值命中 100%）。
    #
    # 空调也走这条路，只是候选与实测步不同：
    #   ac_bin = learn_match_ac.match_ac() 的候选 → learn_pick 列出**型号**
    #   → ac_test_on / ac_test（复用「先开机、再关机」两段式实测）
    #   → 实测"没反应"退回 learn_pick 换型号（`_from_learn` 标志），而不是手动选型号。
    # 匹配引擎见 learn_match_ac.py；效果见 tools/verify_learn_ac.py
    # （120 例真型号 top8 命中 100%、两键交集 100%）。

    @callback
    def _learn_on_signal(self, signal) -> None:
        """接收回调（同步、必须极快）：把一帧原始时序规整后入缓冲。"""
        capture = normalize_capture(getattr(signal, "timings", None))
        if capture is not None:
            self._learn_captures.append((capture, getattr(signal, "modulation", None)))

    def _learn_start_capture(self) -> None:
        """清空缓冲并订阅所选接收器。

        ⚠️ 订阅失败**不能静默**：`infrared.async_subscribe_receiver` 在实体不存在 /
        不是 infrared 接收实体时会抛 `HomeAssistantError`，而"订阅失败"与"用户没按"
        在界面上都表现为「已收到 0 帧」。所以失败原因存进 `_learn_subscribe_error`，
        由 learn_press / learn_press2 报给用户。
        """
        self._learn_captures = []
        self._learn_subscribe_error = ""
        self._learn_stop_capture()
        try:
            self._learn_unsub = infrared.async_subscribe_receiver(
                self.hass, self._receiver, self._learn_on_signal
            )
        except Exception as err:  # noqa: BLE001 —— 接收器消失/实体名失效都要落到表单
            _LOGGER.exception("IR Hub learn: 订阅红外接收器失败：%s", self._receiver)
            self._learn_unsub = None
            self._learn_subscribe_error = f"{type(err).__name__}: {err}"

    def _learn_stop_capture(self) -> None:
        """取消接收订阅（幂等）。"""
        if self._learn_unsub is not None:
            try:
                self._learn_unsub()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("IR Hub learn: 取消接收订阅时出错（忽略）")
            self._learn_unsub = None

    def _learn_best_capture(self) -> tuple[list[int], int | None] | None:
        """一次按压常被接收器拆成多帧（重复帧），取元素最多的那帧最干净。"""
        if not self._learn_captures:
            return None
        best = max(self._learn_captures, key=lambda item: len(item[0]))
        # 打日志是为了排障：用户报"配不上"时，这一行就能判断捕获本身是否正常
        # （段数/首脉冲/全长），不用再猜。
        count, first, total = capture_features(best[0])
        _LOGGER.info(
            "IR Hub learn: 收到 %d 帧，取 %d 段那帧（首脉冲 %d µs / 全长 %d µs）",
            len(self._learn_captures),
            count,
            first,
            total,
        )
        return best

    async def async_step_learn_setup(self, user_input: dict | None = None):
        """学习模式第 1 步：发射目标 + 红外接收器 + 设备大类（含空调）。"""
        library = await self._async_library()
        await self._async_ac_library()

        if not _HAS_RECEIVER_API:
            return self.async_abort(reason="receiver_api_missing")
        receivers = infrared.async_get_receivers(self.hass)
        if not receivers:
            return self.async_abort(reason="no_receivers")

        if user_input is not None:
            self._tx_target = str(user_input[CONF_TX_TARGET]).strip()
            error = await async_validate_target(
                self.hass, self._tx_type, self._tx_target
            )
            if error is not None:
                return self.async_show_form(
                    step_id="learn_setup",
                    data_schema=self._learn_setup_schema(receivers),
                    errors={"base": error},
                    description_placeholders=self._learn_setup_placeholders(library),
                )
            self._receiver = user_input[CONF_RECEIVER]
            picked = str(user_input[CONF_CATEGORY])
            # 空调是特殊项（"ac"），其余大类是数字 id
            self._category = CATEGORY_AC if picked == CATEGORY_AC else int(picked)
            return await self.async_step_learn_press()

        return self.async_show_form(
            step_id="learn_setup",
            data_schema=self._learn_setup_schema(receivers),
            description_placeholders=self._learn_setup_placeholders(library),
        )

    def _receiver_options(self, receivers: list[str]) -> dict[str, str]:
        """接收器下拉：entity_id → 显示名。

        ⚠️ 显示名必须带上 friendly_name。ESPHome 的实体名会被设备名前缀（"HJY IR 红外接收"），
        那是**唯一**能分辨"遥控器该对准哪一台"的线索。只列 entity_id 的话，家里有两台以上
        IR 设备时必然选错 —— 而选错的症状和"按了没收到"完全一样（0 帧），无从察觉。
        """
        options: dict[str, str] = {}
        for entity_id in receivers:
            state = self.hass.states.get(entity_id)
            attrs = getattr(state, "attributes", None) or {}
            name = attrs.get("friendly_name")
            options[entity_id] = (
                f"{name}（{entity_id}）" if name and name != entity_id else entity_id
            )
        return options

    def _receiver_label(self, entity_id: str | None = None) -> str:
        """接收器的展示名：friendly_name（含设备名）+ entity_id。"""
        entity_id = entity_id or self._receiver
        if not entity_id:
            return "未选择"
        return self._receiver_options([entity_id])[entity_id]

    def _receiver_state(self) -> str:
        """所选接收器的 state —— 判断"帧到底有没有进 HA"的探针。

        `infrared` 的接收实体每收到一帧就把自己的 state 写成**时间戳**
        （core：`InfraredReceiverEntity._handle_received_signal` → `async_write_ha_state`），
        所以这个值会随每次按键前进。它的取值本身就把病因说完了：

        - ISO 时间戳 → 帧正在进 HA。此时还报 0 帧，才是本集成的问题。
        - `unknown`  → 这台接收器**从未**收到过任何一帧（选错了设备 / 遥控器对准的是
          另一块板子 —— 家里有多台 IR 设备时最容易踩这个）。
        - `unavailable` → 设备离线。
        - 实体不存在 → 下拉里的名字已失效（设备被删或改名）。

        实测教训：user 的日志里 `hjy-ir` 持续打印 `[I][remote.pronto]`（帧进的是 HJY 板），
        而集成订阅的是 `infrared.tcl_ir_4987d0_ir_receiver`（TCL 86 盒面板，
        `name: tcl_ir` + `name_add_mac_suffix: yes` + `name: IR Receiver`）—— 两块板子，
        症状就是"0 帧"。只看 entity_id 是看不出这层的，所以必须把设备名与 state 都摆出来。
        """
        if not self._receiver:
            return "未选择"
        state = self.hass.states.get(self._receiver)
        if state is None:
            return "实体不存在（下拉里已经没有这台设备）"
        value = str(state.state)
        if value == "unknown":
            return "unknown —— 这台接收器从未收到过任何一帧"
        if value == "unavailable":
            return "unavailable —— 设备离线"
        return f"{value}（最近一次收到帧的时刻）"

    def _receiver_available(self) -> bool:
        """接收器实体存在、且不在 `unavailable`。

        ⚠️ `unknown` 不能算不可用 —— 接收实体在**第一次收到帧之前**就是 unknown，
        把它当成"不可用"会在最需要帮助的时候给出误导性的报错。
        """
        if not self._receiver:
            return False
        state = self.hass.states.get(self._receiver)
        if state is None:
            return False
        return str(state.state) != "unavailable"

    def _learn_setup_schema(self, receivers: list[str]) -> vol.Schema:
        """学习模式第 1 步的表单（大类含空调）。"""
        return vol.Schema(
            {
                vol.Required(CONF_TX_TARGET): self._tx_target_validator(),
                vol.Required(CONF_RECEIVER): vol.In(self._receiver_options(receivers)),
                vol.Required(CONF_CATEGORY): vol.In(self._category_options()),
            }
        )

    def _learn_setup_placeholders(self, library: CodeLibrary) -> dict[str, str]:
        return {
            "tx_type": dict(TX_TYPE_OPTIONS).get(self._tx_type, self._tx_type),
            "tx_target_hint": _TX_TARGET_LABELS.get(self._tx_type, ""),
            "generated": library.generated or "未知",
        }

    async def async_step_learn_press(self, user_input: dict | None = None):
        """学习模式第 2 步：捕获**电源键**。"""
        if user_input is not None:
            best = self._learn_best_capture()
            if best is None:
                # 订阅保持有效，用户可直接再按一次后重新提交
                return self.async_show_form(
                    step_id="learn_press",
                    data_schema=vol.Schema({}),
                    errors={"base": self._learn_capture_error()},
                    description_placeholders=self._learn_press_placeholders("电源键", 1),
                )
            self._learn_stop_capture()
            self._learn_capture1 = best[0]
            return await self.async_step_learn_press2()

        self._learn_start_capture()
        return self.async_show_form(
            step_id="learn_press",
            data_schema=vol.Schema({}),
            description_placeholders=self._learn_press_placeholders("电源键", 1),
        )

    async def async_step_learn_press2(self, user_input: dict | None = None):
        """学习模式第 3 步（可选）：再按一个键，用两键交集收敛型号。"""
        key_hint = (
            "温度+（或风速键）"
            if self._category == CATEGORY_AC
            else "音量+（或方向键 / 频道+）"
        )

        if user_input is not None:
            if user_input[CONF_LEARN_ACTION] == LEARN_ACTION_SKIP:
                self._learn_stop_capture()
                self._learn_capture2 = None
                return await self.async_step_learn_pick()
            best = self._learn_best_capture()
            if best is None:
                return self.async_show_form(
                    step_id="learn_press2",
                    data_schema=self._learn_action_schema(),
                    errors={"base": self._learn_capture_error()},
                    description_placeholders=self._learn_press_placeholders(key_hint, 2),
                )
            self._learn_stop_capture()
            self._learn_capture2 = best[0]
            return await self.async_step_learn_pick()

        self._learn_start_capture()
        return self.async_show_form(
            step_id="learn_press2",
            data_schema=self._learn_action_schema(),
            description_placeholders=self._learn_press_placeholders(key_hint, 2),
        )

    def _learn_action_schema(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_LEARN_ACTION, default=LEARN_ACTION_NEXT): vol.In(
                    LEARN_ACTION_OPTIONS
                )
            }
        )

    def _learn_capture_error(self) -> str:
        """收到 0 帧时报哪个错。

        订阅本身就没成功的话，报 `no_signal`（"请对准接收器"）是**误导** —— 用户会一直
        重按遥控器，而根因在实体上面。所以订阅失败、实体不可用都单独报，把"0 帧"
        拆成三种互不相干的病因：

        - `subscribe_failed`   —— 压根没订阅上（实体不是 infrared 接收实体等）
        - `receiver_unavailable` —— 实体没了 / 设备离线
        - `no_signal`          —— 订阅正常但没收到，这才是"对准 + 再按一次"
        """
        if self._learn_subscribe_error:
            return "subscribe_failed"
        if not self._receiver_available():
            return "receiver_unavailable"
        return "no_signal"

    def _learn_press_placeholders(self, key_hint: str, seq: int) -> dict[str, str]:
        """learn_press / learn_press2 的占位符。

        `receiver_state` 与 `subscribe_error` 是**诊断字段**：它们把"0 帧"拆成
        "帧没进 HA"（接收器选错/设备离线）和"进了 HA 但我们没解码"两种，用户自己就能分。
        """
        subscribe_error = (
            f"\n\n⚠️ **订阅接收器失败**：`{self._learn_subscribe_error}`"
            " —— 该实体可能不是红外接收实体，或已从 HA 移除。"
            if self._learn_subscribe_error
            else ""
        )
        return {
            "key_hint": key_hint,
            "seq": str(seq),
            "receiver": self._receiver_label(),
            "category": self._category_label(),
            "captured": str(len(self._learn_captures)),
            "receiver_state": self._receiver_state(),
            "subscribe_error": subscribe_error,
        }

    def _learn_capture_report(self) -> str:
        """把"这次到底拿什么数据去匹配的"摊开写给人看。

        必要性（实测踩到）：`learn_press` 上那句"已收到 N 帧"是**打开页面那一刻的快照**
        —— 用户永远是先看到页面、再按遥控器，所以那句几乎总显示 0。于是"0 帧却出了
        候选"看起来像**乱选**（用户原话）。其实帧收得好好的。所以匹配页必须把
        「实际用了几帧、每帧多少段、首脉冲多长」原文摆出来，让"有没有真收到码"
        变成可对账的事实，而不是一句会误导人的计数。
        """
        parts: list[str] = []
        for capture, label in (
            (self._learn_capture1, "电源键"),
            (self._learn_capture2, "第 2 键"),
        ):
            if not capture:
                continue
            count, first, total = capture_features(capture)
            parts.append(
                f"{label}：{count} 段 / 首脉冲 {first} µs / 全长 {total} µs"
            )
        if not parts:
            return "（没有可用的捕获）"
        return "；".join(parts)

    def _learn_verdict(self) -> str:
        """候选可信度判词 —— 直接告诉用户"能不能信这个结果"。

        分界值来自 `tools/verify_learn_ac.py` + 库外遥控的实测对照：
        库里真值 ≥0.90、同族近似 ≈0.85、库外遥控最近邻 ≤0.75。
        """
        score = self._learn_top_score
        keys = "两键交集" if self._learn_used_two_keys else "只用电源键"
        if score >= _LEARN_TRUST_SCORE:
            return f"✅ 最高相似度 **{score:.2f}**（{keys}）：**可信**，第一个候选基本就是它。"
        if score >= _LEARN_WEAK_SCORE:
            return (
                f"⚠️ 最高相似度 **{score:.2f}**（{keys}）：**偏低**。可能对、也可能是"
                "同族近似型号 —— 发实测码试一下最直接；没反应就重新配对并按第 2 键。"
            )
        return (
            f"❌ 最高相似度 **{score:.2f}**（{keys}）：**太低，基本可以断定这只遥控**"
            "**不在码库里**（库里真值一般 ≥0.90）。下面列出的只是最近邻，"
            "**不是匹配结果**，别指望它管用。请选「↩ 退回重按」重新配对（务必按"
            "第 2 键）；确认遥控没按错、也对准了接收器之后还是这样，就改用"
            "「手动选择码库」按品牌型号挑，或换用能自学习的方案。"
        )

    def _learn_converge(self, scores1: dict, scores2: dict | None, what: str) -> list:
        """有第二键就取交集，否则（或交集为空）退回单键排序。"""
        if scores2:
            common = set(scores1) & set(scores2)
            if common:
                self._learn_used_two_keys = True
                ranked = sorted(
                    common, key=lambda key: -min(scores1[key], scores2[key])
                )
                return ranked[:_LEARN_MAX_CANDIDATES]
            # 交集为空（第二键没对上/按错了）→ 退回单键结果，别把用户卡死
            _LOGGER.warning("IR Hub learn: 第二键与第一键无交集（%s），退回单键候选", what)
        ranked = sorted(scores1, key=lambda key: -scores1[key])
        return ranked[:_LEARN_MAX_CANDIDATES]

    async def _async_learn_match(self, library: CodeLibrary) -> list:
        """跑匹配引擎（executor 里，别阻塞事件循环），返回候选列表。

        按键类设备的候选是 device_id（int）；空调是 bin 文件名（str）。
        """
        capture1 = self._learn_capture1 or []

        if self._category == CATEGORY_AC:
            return await self._async_learn_match_ac(capture1)

        def _device_scores(frames: list[dict]) -> dict[int, float]:
            scores: dict[int, float] = {}
            for frame in frames:
                for device_id in frame["device_ids"]:
                    scores[device_id] = max(scores.get(device_id, 0.0), frame["score"])
            return scores

        frames1 = await self.hass.async_add_executor_job(
            match_timings, library, self._category, capture1, _LEARN_TOP_N
        )
        self._learn_top_score = frames1[0]["score"] if frames1 else 0.0
        scores1 = _device_scores(frames1)

        scores2 = None
        capture2 = self._learn_capture2
        if capture2:
            frames2 = await self.hass.async_add_executor_job(
                match_timings, library, self._category, capture2, _LEARN_TOP_N
            )
            scores2 = _device_scores(frames2)
        return self._learn_converge(scores1, scores2, "按键类")

    async def _async_learn_match_ac(self, capture1: list[int]) -> list[str]:
        """空调：对状态码库跑匹配（候选是 bin 名），两键取交集收敛型号。"""
        ac_library = await self._async_ac_library()

        def _bin_scores(hits: list[dict]) -> dict[str, float]:
            return {hit["bin"]: hit["score"] for hit in hits}

        # 候选是"型号"不是"帧"，结构预筛后本来就只剩几十个；且实测真值只有 85.8%
        # 排第 1、100% 落在前 8 ⇒ 不能像按键类那样只取 top_n=8，全给出去（最后还是
        # 由 _learn_converge 截到 _LEARN_MAX_CANDIDATES）。
        hits1 = await self.hass.async_add_executor_job(
            match_ac, ac_library, capture1, None
        )
        self._learn_top_score = hits1[0]["score"] if hits1 else 0.0
        scores1 = _bin_scores(hits1)
        # 候选数与最高分是判断"库里有/没有这只遥控"的唯一依据：
        # 库里真值 → 十几个到几十个候选、最高 ~0.94；库外遥控 → 1~2 个、最高 ≤0.75。
        _LOGGER.info(
            "IR Hub learn(AC): 候选 %d 个，top5 %s",
            len(hits1),
            [(hit["bin"], round(hit["score"], 3)) for hit in hits1[:5]],
        )

        scores2 = None
        capture2 = self._learn_capture2
        if capture2:
            hits2 = await self.hass.async_add_executor_job(
                match_ac, ac_library, capture2, None
            )
            scores2 = _bin_scores(hits2)
        return self._learn_converge(scores1, scores2, "空调")

    async def _async_pick_ac(self, bin_name: str):
        """空调：锁定型号（解码一次）后，进既有的「开机 → 关机」两段式实测。"""
        ac_library = await self._async_ac_library()
        try:
            self._ac_code = await self.hass.async_add_executor_job(
                ac_library.load_device, bin_name
            )
        except (OSError, ValueError):
            return self.async_abort(reason="ac_decode_failed")
        self._from_learn = True
        self._ac_bin = bin_name
        self._ac_brand, self._ac_brand_count = self._ac_resolve_brand(ac_library, bin_name)
        # 「自动试下一个」在配对路径下 = 下一个**匹配候选**（按相似度），
        # 而不是同品牌型号 —— 用户是在候选列表里挑的，退回去也得在同一个列表里退。
        self._ac_candidates = list(self._learn_candidates or [bin_name])
        self._ac_index = (
            self._ac_candidates.index(bin_name)
            if bin_name in self._ac_candidates
            else 0
        )
        return await self.async_step_ac_test_on()

    async def _learn_pick_options(self, library: CodeLibrary) -> dict[str, str]:
        """候选下拉：key 是提交值（device_id 或空调 bin），value 是显示名。"""
        options: dict[str, str] = {}
        if self._category == CATEGORY_AC:
            ac_library = await self._async_ac_library()
            for bin_name in self._learn_candidates or []:
                brands = ac_library.brands_of(bin_name)
                model_short = bin_name.removesuffix(".bin").replace("irda_new_ac_", "")
                if len(brands) == 1:
                    label = CodeLibrary.display_name(brands[0], f"空调 {model_short}")
                else:
                    # 共用码：不挑一个品牌来冒充（509 个 bin 里 149 个是多品牌共用的）
                    label = f"空调 {model_short}（{len(brands)} 个品牌共用）"
                options[bin_name] = label
            return options

        for device_id in self._learn_candidates or []:
            device = library.get_device(device_id)
            if device is None:
                continue
            options[str(device_id)] = library.display_name(
                library.brands.get(device["brand"]) or f"品牌 {device['brand']}",
                device["name"],
            )
        return options

    async def _learn_restart(self):
        """退回「按第 1 键」重新配对（清掉上一轮候选与捕获）。"""
        self._learn_candidates = None
        self._learn_capture1 = None
        self._learn_capture2 = None
        self._learn_used_two_keys = False
        self._learn_top_score = 0.0
        return await self.async_step_learn_press()

    async def async_step_learn_pick(self, user_input: dict | None = None):
        """学习模式第 4 步：从匹配到的候选里挑一个型号。

        下拉末尾挂一个「↩ 退回重按」。分数太低时（库外遥控）列表里全是错的，
        没有这一项用户就没有出口 —— 只能关掉对话框从头再来。
        """
        library = await self._async_library()

        if self._learn_candidates is None:
            self._learn_candidates = await self._async_learn_match(library)

        if user_input is not None:
            picked = str(user_input[CONF_DEVICE])
            if picked == _LEARN_BACK:
                return await self._learn_restart()
            if self._category == CATEGORY_AC:
                return await self._async_pick_ac(picked)
            device_id = int(picked)
            device = library.get_device(device_id)
            if device is None:
                return self.async_abort(reason="device_gone")
            self._device_id = device_id
            self._brand = int(device.get("brand") or 0)
            return await self.async_step_learn_test()

        options = await self._learn_pick_options(library)
        if not options:
            return self.async_abort(reason="learn_no_match")
        options[_LEARN_BACK] = "↩ 这些都不是我的型号 —— 退回重按"

        return self.async_show_form(
            step_id="learn_pick",
            data_schema=vol.Schema({vol.Required(CONF_DEVICE): vol.In(options)}),
            description_placeholders={
                "count": str(len(options) - 1),
                "category": self._category_label(),
                "keys": "两键交集" if self._learn_used_two_keys else "仅电源键",
                "score": f"{self._learn_top_score:.2f}",
                "capture": self._learn_capture_report(),
                "verdict": self._learn_verdict(),
            },
        )

    async def async_step_learn_test(self, user_input: dict | None = None):
        """学习模式第 5 步：发该型号的 power 实测码，确认后建 entry。"""
        library = await self._async_library()

        if user_input is not None:
            result = user_input[CONF_TEST_RESULT]
            if result == TEST_RETRY:
                return await self.async_step_learn_pick()
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
            except Exception:  # noqa: BLE001
                _LOGGER.exception(
                    "IR Hub learn: 测试码发送失败（%s/%s）", self._tx_type, self._tx_target
                )
                errors["base"] = "send_failed"

        return self.async_show_form(
            step_id="learn_test",
            data_schema=vol.Schema(
                {vol.Required(CONF_TEST_RESULT): vol.In(LEARN_TEST_OPTIONS)}
            ),
            errors=errors or None,
            description_placeholders={
                "model": device["name"],
                "key": key or "—",
            },
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
