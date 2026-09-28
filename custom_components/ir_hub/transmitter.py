"""发射通道抽象 —— 一个码库，四种发射器。

对齐 SmartAC 的 controller 抽象（re/smartac/controller.py，MIT），让没有
自制 ESPHome 硬件的用户也能用现成发射器：

=================  ============================  ==============================
通道               目标（CONF_TX_TARGET）         发送方式
=================  ============================  ==============================
infrared（默认）   infrared emitter 实体          `infrared.async_send_command`
esphome            esphome 动作名（可带前缀）      `esphome.<动作>` 服务，
                                                  data {"command": [带符号时序]}
                                                  —— 与 SmartAC 契约逐字段一致
broadlink          remote.<实体>                  µs→tick(÷30.45) → 0x26 包 →
                                                  b64 → `remote.send_command`
                                                  （与 SmartAC raw2broadlink
                                                  逐字节一致，selfcheck 有对拍）
mqtt               topic                          `mqtt.publish`，载荷 smartac 裸数组
                                                  （默认）或 Tasmota RAW JSON
=================  ============================  ==============================

⚠️ 三个从 SmartAC 源码核实的细节：
  1. SmartAC 交给 controller 的是**全正** µs 数组，符号在 ESPHomeController
     里补（偶正奇负）。我们的码库出口**已带符号** ⇒ 传给 esphome 通道直接用；
     传给 broadlink/mqtt 通道取绝对值（它们只认交替的正值序列）。
  2. Broadlink tick = µs × 269 / 8192（≈ 30.45 µs/tick，**不是** 32.84），
     `0x00` 是转义符（后跟 2 字节 big-endian），包尾 `0x0d 0x05`，整包补零到
     16 字节倍数（AES 块）。
  3. MQTT 有两种载荷（`mqtt_format`）：**smartac**（默认）= 裸全正 µs 数组
     `json.dumps([4450,4450,560,...])` —— tcl-ir 等桥接固件解析的就是它（09-28
     实测：发 Tasmota JSON 设备无反应）；**tasmota** = IRMQTTServer RAW JSON
     （`Raw` 是逗号分隔的全正 µs 序列，首个值 = mark），刷 Tasmota 固件即用。
"""

from __future__ import annotations

import base64
import json
import logging
import struct

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import (
    CONF_TX_DELAY,
    DEFAULT_MQTT_FORMAT,
    DEFAULT_TX_DELAY,
    MQTT_FORMAT_SMARTAC,
    MQTT_FORMAT_TASMOTA,
    TX_BROADLINK,
    TX_ESPHOME,
    TX_INFRARED,
    TX_MQTT,
)

_LOGGER = logging.getLogger(__name__)

__all__ = ["raw_to_broadlink_packet", "async_send_timings", "async_validate_target"]


# ---------------------------------------------------------------- Broadlink

def raw_to_broadlink_packet(pulses_us: list[int]) -> bytes:
    """全正 µs 序列 → Broadlink 0x26 包（与 SmartAC raw2broadlink 逐字节一致）。

    `pulses_us` 必须是**全正**的交替 mark/space 值（调用方负责取绝对值）。
    """
    array = bytearray()
    for pulse in pulses_us:
        tick = int(pulse * 269 / 8192)
        if tick < 256:
            array += bytearray(struct.pack(">B", tick))
        else:
            array += bytearray([0x00])
            array += bytearray(struct.pack(">H", tick))

    packet = bytearray([0x26, 0x00])
    packet += bytearray(struct.pack("<H", len(array)))
    packet += array
    packet += bytearray([0x0D, 0x05])

    # 补零到 16 字节倍数（128-bit AES 加密块）
    remainder = (len(packet) + 4) % 16
    if remainder:
        packet += bytearray(16 - remainder)
    return packet


# ---------------------------------------------------------------- 发送

async def async_send_timings(
    hass: HomeAssistant,
    tx_type: str,
    tx_target: str,
    timings: list[int],
    *,
    carrier: int,
    delay: float = DEFAULT_TX_DELAY,
    mqtt_format: str = DEFAULT_MQTT_FORMAT,
) -> None:
    """把**带符号**时序（正=mark/负=space，µs）经指定通道发出去。

    repeats 的"时序层面复制"由调用方（build_raw_command 或手工拼接）完成
    —— 本函数拿到什么就发什么，不重复。
    """
    if tx_type == TX_INFRARED:
        # 红外 building block 通道（见 ir_command.build_raw_command 的调用方）
        from homeassistant.components import infrared

        from .ir_command import RawTimingsCommand

        command = RawTimingsCommand(list(timings), modulation=int(carrier))
        await infrared.async_send_command(hass, tx_target, command)
        return

    if tx_type == TX_ESPHOME:
        # SmartAC 契约：偶正奇负的带符号数组，直接交服务
        service = tx_target.split(".", 1)[-1] if "." in tx_target else tx_target
        await hass.services.async_call(
            "esphome", service, {"command": list(timings)}, blocking=True
        )
        return

    if tx_type == TX_BROADLINK:
        pulses = [abs(t) for t in timings]
        packet = raw_to_broadlink_packet(pulses)
        b64 = base64.b64encode(packet).decode("utf-8")
        await hass.services.async_call(
            "remote",
            "send_command",
            {
                "entity_id": tx_target,
                "command": [f"b64:{b64}"],
                "delay_secs": float(delay),
            },
            blocking=True,
        )
        return

    if tx_type == TX_MQTT:
        if mqtt_format == MQTT_FORMAT_TASMOTA:
            raw = ",".join(str(abs(t)) for t in timings)
            payload = json.dumps(
                {"Protocol": "RAW", "Bits": 0, "Raw": raw, "Frequency": int(carrier)}
            )
        else:
            # SmartAC 契约：裸全正 µs 数组（tcl-ir 等桥接固件解析的就是它）
            payload = json.dumps([abs(t) for t in timings])
        await hass.services.async_call(
            "mqtt", "publish", {"topic": tx_target, "payload": payload}, blocking=True
        )
        return

    raise HomeAssistantError(f"IR Hub: 未知发射通道 '{tx_type}'")


# ---------------------------------------------------------------- 校验

async def async_validate_target(
    hass: HomeAssistant, tx_type: str, tx_target: str
) -> str | None:
    """校验发射目标当前是否可用；返回错误 key（None = 通过）。

    只在 config flow 里用 —— 能拦住"手滑填错"就够了，不做运行时强校验。
    """
    if tx_type == TX_INFRARED:
        from homeassistant.components import infrared

        if tx_target not in infrared.async_get_emitters(hass):
            return "target_missing"
        return None

    if tx_type == TX_ESPHOME:
        service = tx_target.split(".", 1)[-1] if "." in tx_target else tx_target
        if not service or not hass.services.has_service("esphome", service):
            return "target_missing"
        return None

    if tx_type == TX_BROADLINK:
        state = hass.states.get(tx_target)
        if state is None or state.domain != "remote":
            return "target_missing"
        return None

    if tx_type == TX_MQTT:
        if not hass.services.has_service("mqtt", "publish"):
            return "mqtt_missing"
        if not tx_target:
            return "target_missing"
        return None

    return "target_missing"
