"""裸时序红外命令，以及构造/解析辅助。

HA 的红外体系用 `infrared_protocols.commands.Command` 作为"要发什么"的统一载体。
基类是个纯抽象类：

    class Command(abc.ABC):
        repeat_count: int
        modulation: int
        def __init__(self, *, modulation: int, repeat_count: int = 0) -> None: ...
        @abc.abstractmethod
        def get_raw_timings(self) -> list[int]: ...

所以我们只要实现一个"把现成时序数组塞进去"的子类，就能把 irext 码库里的
**任意**波形交给任何 emitter（不用等官方库支持该协议）。
"""

from __future__ import annotations

import logging

from infrared_protocols.commands import Command

from .const import DEFAULT_CARRIER, DEFAULT_REPEATS

_LOGGER = logging.getLogger(__name__)

__all__ = ["RawTimingsCommand", "build_raw_command", "parse_timings"]


class RawTimingsCommand(Command):
    """A command defined by an explicit list of raw timings.

    Timings are in microseconds; positive = pulse (carrier on),
    negative = space (carrier off). This is the same convention ESPHome's
    `remote_transmitter.transmit_raw` uses, so no conversion is needed.
    """

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
        """Return the raw timings (µs, positive = pulse, negative = space)."""
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

    ⚠️ `Command.repeat_count` 在本链路上是**装饰性**的，别指望它生效：
        HA 的 esphome emitter（`components/esphome/infrared.py` →
        `EsphomeInfraredEmitterEntity.async_send_command`）只透传

            timings = command.get_raw_timings()
            carrier_frequency = command.modulation

        两个值给 aioesphomeapi 的 `infrared_rf_transmit_raw_timings()`，而该函数
        的 `repeat_count` 参数**没有被传**，于是走 protobuf 默认值 1；
        ESPHome 侧 `IrRfProxy` 最终 `set_send_times(1)` ⇒ 只发一遍。

    所以"多送几次"必须在**时序层面复制**，这里替调用方做掉：
    repeats=1 视为只发一次（不复制），repeats=3 则把整帧拼三遍。
    irext 存的帧是 mark 开头、space 结尾，首尾相接仍是合法的 mark/space 交替。
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
