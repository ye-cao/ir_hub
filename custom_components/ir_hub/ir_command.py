"""裸时序红外命令的构造与解析。

HA 用 `infrared_protocols.commands.Command` 表示"要发什么"。它是个抽象基类，
只有 modulation / repeat_count 两个字段和一个 `get_raw_timings()`。
本模块提供一个直接把现成时序数组塞进去的子类，这样码库里的任意波形都能交给
任何 emitter，不必等官方支持该协议。

时序约定（与 ESPHome `remote_transmitter.transmit_raw` 一致，无需换算）：
    正数 = pulse（载波开），负数 = space（载波空闲），单位 µs。
"""

from __future__ import annotations

import logging

from infrared_protocols.commands import Command

from .const import DEFAULT_CARRIER, DEFAULT_REPEATS

_LOGGER = logging.getLogger(__name__)

__all__ = ["RawTimingsCommand", "build_raw_command", "parse_timings"]


class RawTimingsCommand(Command):
    """由显式时序数组定义的命令（正 = pulse，负 = space，单位 µs）。"""

    def __init__(
        self,
        timings: list[int],
        *,
        modulation: int,
        repeat_count: int = 0,
    ) -> None:
        if not timings:
            raise ValueError("raw timings must not be empty")
        if all(t >= 0 for t in timings):
            raise ValueError(
                "raw timings must alternate pulse/space, i.e. contain negatives"
            )
        super().__init__(modulation=modulation, repeat_count=repeat_count)
        self._timings = list(timings)

    def get_raw_timings(self) -> list[int]:
        """返回裸时序（µs，正 = pulse，负 = space）。"""
        return self._timings

    def __repr__(self) -> str:
        return (
            f"RawTimingsCommand(count={len(self._timings)}, "
            f"modulation={self.modulation}, repeat_count={self.repeat_count})"
        )


def parse_timings(raw: str) -> list[int]:
    """把 "1000 -500 1000" / "1000,-500,1000" 解析成带符号 int 列表。"""
    cleaned = raw.replace(",", " ").replace(";", " ").replace("|", " ")
    return [int(chunk) for chunk in cleaned.split() if chunk]


def build_raw_command(
    timings: list[int] | tuple[int, ...],
    *,
    carrier: int = DEFAULT_CARRIER,
    repeats: int = DEFAULT_REPEATS,
) -> RawTimingsCommand:
    """构造要交给 emitter 的裸时序命令。

    ⚠️ `Command.repeat_count` 在本链路上只是装饰：HA 的 esphome emitter 只把
    `timings` 和 `modulation` 透传给设备，repeat_count 走 protobuf 默认值 1，
    ESPHome 侧最终 `set_send_times(1)` ⇒ 只发一遍。

    所以"多送几次"必须在时序层面复制，这里替调用方做掉：
    repeats=1 不复制，repeats=3 就把整帧拼三遍。
    irext 的帧是 mark 开头、space 结尾，首尾相接仍是合法的 mark/space 交替。
    """
    repeats = int(repeats or 1)
    if repeats < 1:
        repeats = 1

    data = list(timings)
    if repeats > 1:
        if data and data[-1] >= 0:
            _LOGGER.warning(
                "raw timings end with a pulse (>=0); concatenating %d frames "
                "without an inter-frame gap may produce a malformed signal",
                repeats,
            )
        data = data * repeats

    return RawTimingsCommand(
        data,
        modulation=int(carrier or DEFAULT_CARRIER),
        repeat_count=repeats,
    )
