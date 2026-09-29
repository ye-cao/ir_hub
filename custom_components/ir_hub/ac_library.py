"""AC（空调）状态码库：irext `.bin` 解码器 + 加载器。

与 `library.py`（按键式设备：一个键 = 一帧定长时序）不同，空调是**状态机**：
同一台空调的"制冷 26°C / 高风"和"制热 20°C / 低风"是完全不同的两帧。
irext 把每台空调的编码规则压成一个几百字节的 `.bin`（TAG 表 + 位定义 + 校验
算法），运行时按目标状态**生成**时序 —— 这就是本模块做的事。

为什么用 bin 而不是展开成时序表：525 个 bin 共 ~171 KB，展开成时序后
每型号 5 模式 × 4 风速 × 15 温度 ≈ 300 帧 × 200 值，全库要几百 MB，没法打包。

来源与授权：解码器是 irext（https://site.irext.net，MIT）官方 `ir_decode.c`
（AC 分支）的 Python 移植，参考实现取自 ryanh7/SmartAC（HACS 集成，同源 MIT）；
`ac_library/codes/*.bin` 与 `index.json` 同样来自 irext 数据库导出。
本文件按本项目风格整理，逐行与 SmartAC 保持**可对照**。

⚠️ 符号约定（与 library.py 一致）：irext 解码出的是**全正**的 mark/space 长度，
而 ESPHome / HA 要求"正 = mark、负 = space"。所以 `decode_bin()` 出口处就补好
符号（复用 `library.sign_timings`），调用方拿到即可发送。
"""

from __future__ import annotations

import json
import logging
import os

from .library import sign_timings

_LOGGER = logging.getLogger(__name__)

AC_LIBRARY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ac_library")

# ---------------------------------------------------------------- irext TAG
# .bin 布局：[tag_count][tag_count × 2B 小端 offset][各 tag 的数据段]
# 详见 irext 官方 ir_decode.c。这里只列 AC 分支用到的 tag。
TAG_AC_BOOT_CODE = 1
TAG_AC_ZERO = 2
TAG_AC_ONE = 3
TAG_AC_DELAY_CODE = 4
TAG_AC_FRAME_LENGTH = 5
TAG_AC_ENDIAN = 6
TAG_AC_LAST_BIT = 7

TAG_AC_POWER_1 = 21
TAG_AC_DEFAULT_CODE = 22
TAG_AC_TEMP_1 = 23
TAG_AC_MODE_1 = 24
TAG_AC_SPEED_1 = 25
TAG_AC_SWING_1 = 26
TAG_AC_CHECKSUM_TYPE = 27
TAG_AC_SOLO_FUNCTION = 28
TAG_AC_FUNCTION_1 = 29
TAG_AC_TEMP_2 = 30
TAG_AC_MODE_2 = 31
TAG_AC_SPEED_2 = 32
TAG_AC_SWING_2 = 33
TAG_AC_FUNCTION_2 = 34

TAG_AC_BAN_FUNCTION_IN_COOL_MODE = 41
TAG_AC_BAN_FUNCTION_IN_HEAT_MODE = 42
TAG_AC_BAN_FUNCTION_IN_AUTO_MODE = 43
TAG_AC_BAN_FUNCTION_IN_FAN_MODE = 44
TAG_AC_BAN_FUNCTION_IN_DRY_MODE = 45
TAG_AC_SWING_INFO = 46
TAG_AC_REPEAT_TIMES = 47
TAG_AC_BIT_NUM = 48

_TAGS = [
    TAG_AC_BOOT_CODE, TAG_AC_ZERO, TAG_AC_ONE, TAG_AC_DELAY_CODE,
    TAG_AC_FRAME_LENGTH, TAG_AC_ENDIAN, TAG_AC_LAST_BIT,
    TAG_AC_POWER_1, TAG_AC_DEFAULT_CODE, TAG_AC_TEMP_1, TAG_AC_MODE_1,
    TAG_AC_SPEED_1, TAG_AC_SWING_1, TAG_AC_CHECKSUM_TYPE,
    TAG_AC_SOLO_FUNCTION, TAG_AC_FUNCTION_1, TAG_AC_TEMP_2, TAG_AC_MODE_2,
    TAG_AC_SPEED_2, TAG_AC_SWING_2, TAG_AC_FUNCTION_2,
    TAG_AC_BAN_FUNCTION_IN_COOL_MODE, TAG_AC_BAN_FUNCTION_IN_HEAT_MODE,
    TAG_AC_BAN_FUNCTION_IN_AUTO_MODE, TAG_AC_BAN_FUNCTION_IN_FAN_MODE,
    TAG_AC_BAN_FUNCTION_IN_DRY_MODE, TAG_AC_SWING_INFO, TAG_AC_REPEAT_TIMES,
    TAG_AC_BIT_NUM,
]

CHECKSUM_TYPE_BYTE = 1
CHECKSUM_TYPE_BYTE_INVERSE = 2
CHECKSUM_TYPE_HALF_BYTE = 3
CHECKSUM_TYPE_HALF_BYTE_INVERSE = 4
CHECKSUM_TYPE_SPEC_HALF_BYTE = 5
CHECKSUM_TYPE_SPEC_HALF_BYTE_INVERSE = 6
CHECKSUM_TYPE_SPEC_HALF_BYTE_ONE_BYTE = 7
CHECKSUM_TYPE_SPEC_HALF_BYTE_INVERSE_ONE_BYTE = 8

AC_FUNCTION_POWER = 1
AC_FUNCTION_MODE = 2
AC_FUNCTION_TEMPERATURE_UP = 3
AC_FUNCTION_TEMPERATURE_DOWN = 4
AC_FUNCTION_WIND_SPEED = 5
AC_FUNCTION_WIND_SWING = 6
AC_FUNCTION_WIND_FIX = 7

# irext 的模式 / 风速枚举（bin 内的取值，不是 HA 的）
MODE_COOL = 0
MODE_HEAT = 1
MODE_AUTO = 2
MODE_FAN = 3
MODE_DRY = 4
_MODES = [MODE_COOL, MODE_HEAT, MODE_AUTO, MODE_FAN, MODE_DRY]

SPEED_AUTO = 0
SPEED_LOW = 1
SPEED_MEDIUM = 2
SPEED_HIGH = 3
_SPEEDS = [SPEED_AUTO, SPEED_LOW, SPEED_MEDIUM, SPEED_HIGH]

POWER_ON = 0
POWER_OFF = 1

# irext 枚举 -> HA 的 HVACMode 值 / 风速字符串
HVAC_MODE_MAP = {
    MODE_COOL: "cool",
    MODE_HEAT: "heat",
    MODE_AUTO: "auto",
    MODE_FAN: "fan_only",
    MODE_DRY: "dry",
}
SPEED_MAP = {
    SPEED_AUTO: "auto",
    SPEED_LOW: "low",
    SPEED_MEDIUM: "medium",
    SPEED_HIGH: "high",
}


def _parse_segments(data: bytes) -> list[bytes]:
    """`[len][bytes]...` 的分段结构 -> [bytes, ...]。"""
    result = []
    index = 0
    while index < len(data):
        seg_len = data[index]
        index += 1
        result.append(data[index : index + seg_len])
        index += seg_len
    return result


def _seg(segments: list[bytes], index: int) -> bytes:
    """取第 index 段；越界视为空段（= 该模式/风速/温度不被支持）。

    ⚠️ 有些 bin 的段数不足枚举数（数据变异）。SmartAC 对这类 bin 会在 setup 时
    静默失败并跳过整台设备；这里改成**缺段 = 不支持该项**，其余模式照常可用。
    """
    return segments[index] if 0 <= index < len(segments) else b""


class _AcBin:
    """一台空调的 `.bin` 编码规则（对应 SmartAC 的 `AC` 类）。"""

    def __init__(self, data: bytes) -> None:
        tag_count = data[0]
        data = data[1:]

        offsets = [
            int.from_bytes(data[i * 2 : i * 2 + 2], "little") for i in range(tag_count)
        ]
        data = data[tag_count * 2 :]

        tags_data: dict[int, bytes] = {}
        for i in range(tag_count):
            if offsets[i] == 0xFFFF:
                tags_data[_TAGS[i]] = b""
                continue
            for j in range(i + 1, tag_count + 1):
                if j == tag_count:
                    tags_data[_TAGS[i]] = data[offsets[i] :]
                    break
                if offsets[j] != 0xFFFF:
                    tags_data[_TAGS[i]] = data[offsets[i] : offsets[j]]
                    break

        self._tags = tags_data

        self._mode1 = self._parse_data(tags_data.get(TAG_AC_MODE_1, b"").decode())
        self._mode2 = self._parse_data(tags_data.get(TAG_AC_MODE_2, b"").decode())
        self._power1 = self._parse_data(tags_data.get(TAG_AC_POWER_1, b"").decode())

        default_code = bytes.fromhex(tags_data.get(TAG_AC_DEFAULT_CODE, b"").decode())
        self._default_code = default_code[1 : default_code[0] + 1]

        # 每个模式下的禁用项：'NA'=禁用整模式；''=全可用；
        # 'S&0,1'/'T&16,17' = 禁用指定风速 / 温度
        self._n_mode = [
            self._parse_n_mode(tags_data[TAG_AC_BAN_FUNCTION_IN_COOL_MODE].decode()),
            self._parse_n_mode(tags_data[TAG_AC_BAN_FUNCTION_IN_HEAT_MODE].decode()),
            self._parse_n_mode(tags_data[TAG_AC_BAN_FUNCTION_IN_AUTO_MODE].decode()),
            self._parse_n_mode(tags_data[TAG_AC_BAN_FUNCTION_IN_FAN_MODE].decode()),
            self._parse_n_mode(tags_data[TAG_AC_BAN_FUNCTION_IN_DRY_MODE].decode()),
        ]

        self._temp1, self._temp1_dynamic = self._parse_temp(
            tags_data.get(TAG_AC_TEMP_1, b"").decode(), stride=2
        )
        self._temp2, self._temp2_dynamic = self._parse_temp(
            tags_data.get(TAG_AC_TEMP_2, b"").decode(), stride=3
        )

        self._speed1 = self._parse_data(tags_data.get(TAG_AC_SPEED_1, b"").decode())
        self._speed2 = self._parse_data(tags_data.get(TAG_AC_SPEED_2, b"").decode())

        self._function1 = {
            seg[0]: seg[1:]
            for seg in self._parse_data(tags_data.get(TAG_AC_FUNCTION_1, b"").decode())
        }
        self._function2 = {
            seg[0]: seg[1:]
            for seg in self._parse_data(tags_data.get(TAG_AC_FUNCTION_2, b"").decode())
        }

        solo_hex = tags_data.get(TAG_AC_SOLO_FUNCTION, b"").decode()
        self._solo_function = (
            {int(f) for f in bytes.fromhex(solo_hex)[1:]} if len(solo_hex) >= 4 else set()
        )

        self._frame_len = tags_data.get(TAG_AC_FRAME_LENGTH, b"").decode()

        self._zero = _split_ints(tags_data.get(TAG_AC_ZERO, b"").decode())
        self._one = _split_ints(tags_data.get(TAG_AC_ONE, b"").decode())
        self._boot_code = _split_ints(tags_data.get(TAG_AC_BOOT_CODE, b"").decode())

        self._repeat_time = 1
        if tags_data.get(TAG_AC_REPEAT_TIMES):
            self._repeat_time = int(tags_data.get(TAG_AC_REPEAT_TIMES).decode())

        # 每字节有效位数（'pos&bits|...'，pos=-1 = 最后一字节）
        self._bit_num = []
        for sub in tags_data.get(TAG_AC_BIT_NUM, b"").decode().split("|"):
            item = sub.split("&")
            if len(item) < 2:
                continue
            pos = int(item[0])
            if pos == -1:
                pos = len(self._default_code) - 1
            self._bit_num.append({"pos": pos, "bits": int(item[1])})

        self._endian = 0
        if tags_data.get(TAG_AC_ENDIAN):
            self._endian = int(tags_data.get(TAG_AC_ENDIAN).decode())

        # 帧内定点延迟（'pos&t1,t2|...'，pos=-1 = 帧尾）
        self._delay = []
        for sub in tags_data.get(TAG_AC_DELAY_CODE, b"").decode().split("|"):
            delay = sub.split("&")
            if len(delay) < 2:
                continue
            self._delay.append(
                {
                    "pos": int(delay[0]),
                    "time": [int(t) % 65536 for t in delay[1].split(",")],
                }
            )

        self._last_bit = (
            int(tags_data.get(TAG_AC_LAST_BIT).decode())
            if tags_data.get(TAG_AC_LAST_BIT)
            else 0
        )

        self._checksum = []
        for t_hex in tags_data.get(TAG_AC_CHECKSUM_TYPE, b"").decode().split("|"):
            checksum_data = bytes.fromhex(t_hex)
            if len(checksum_data) <= 1:
                continue
            checksum_len = checksum_data[0]
            checksum_type = checksum_data[1]
            checksum: dict = {"type": checksum_type}
            if 1 <= checksum_type <= 4:
                checksum["start_byte_pos"] = checksum_data[2]
                checksum["end_byte_pos"] = checksum_data[3]
                checksum["checksum_byte_pos"] = checksum_data[4]
                checksum["checksum_plus"] = checksum_data[5] if checksum_len > 4 else 0
            elif 5 <= checksum_type <= 8:
                checksum["checksum_byte_pos"] = checksum_data[2]
                checksum["checksum_plus"] = checksum_data[3]
                checksum["spec_pos"] = checksum_data[4:]
            self._checksum.append(checksum)

    # ------------------------------------------------------------- 状态枚举

    def supported_modes(self) -> list[int]:
        """该型号真正可用的模式（irext 枚举值）。"""
        supported = []
        for mode in _MODES:
            if self._n_mode[mode].get("disabled"):
                continue
            if not self._n_mode[mode]:
                continue
            if self._mode1 and not _seg(self._mode1, mode):
                continue
            if self._mode2 and not _seg(self._mode2, mode):
                continue
            supported.append(mode)
        return supported

    def temperature_range(self, mode: int) -> list[int]:
        """该模式支持的温度（16~30°C）。"""
        temps = []
        for t in range(0, 15):
            banned = self._n_mode[mode].get("temperature") or []
            if t + 16 in banned:
                continue
            if self._temp1 and not _seg(self._temp1, t):
                continue
            if self._temp2 and not _seg(self._temp2, t):
                continue
            temps.append(t + 16)
        return temps

    def supported_speeds(self, mode: int) -> list[int]:
        """该模式支持的风速（irext 枚举值）。"""
        speeds = []
        for s in _SPEEDS:
            banned = self._n_mode[mode].get("speed") or []
            if s in banned:
                continue
            if self._speed1 and not _seg(self._speed1, s):
                continue
            if self._speed2 and not _seg(self._speed2, s):
                continue
            speeds.append(s)
        return speeds

    # ------------------------------------------------------------- 解码核心

    def ir_decode(
        self,
        power: int,
        temperature: int = 26,
        mode: int = MODE_COOL,
        speed: int = SPEED_AUTO,
    ) -> list[int]:
        """按目标状态生成**全正**的 mark/space 交替时序（µs）。"""
        ir_hex = bytearray(self._default_code)

        if len(self._power1) > power:
            ir_hex = self._apply_type_1(ir_hex, self._power1[power])

        if power == POWER_ON:
            if AC_FUNCTION_MODE not in self._solo_function:
                if self._mode1:
                    ir_hex = self._apply_type_1(ir_hex, _seg(self._mode1, mode))
                elif self._mode2:
                    ir_hex = self._apply_type_2(ir_hex, _seg(self._mode2, mode))

            if AC_FUNCTION_WIND_SPEED not in self._solo_function:
                if self._speed1:
                    ir_hex = self._apply_type_1(ir_hex, _seg(self._speed1, speed))
                elif self._speed2:
                    ir_hex = self._apply_type_2(ir_hex, _seg(self._speed2, speed))

            if (
                AC_FUNCTION_WIND_SWING not in self._solo_function
                and AC_FUNCTION_WIND_FIX not in self._solo_function
            ):
                # swing 固定 0（连续送风不摆动）—— SmartAC 同样如此
                if self._swing1:
                    ir_hex = self._apply_type_1(ir_hex, _seg(self._swing1, 0))
                elif self._swing2:
                    ir_hex = self._apply_type_2(ir_hex, _seg(self._swing2, 0))

            if (
                AC_FUNCTION_TEMPERATURE_UP not in self._solo_function
                and AC_FUNCTION_TEMPERATURE_DOWN not in self._solo_function
            ):
                if self._temp1:
                    ir_hex = self._apply_type_1(
                        ir_hex,
                        _seg(self._temp1, temperature - 16),
                        self._temp1_dynamic,
                    )
                elif self._temp2:
                    ir_hex = self._apply_type_2(
                        ir_hex,
                        _seg(self._temp2, temperature - 16),
                        self._temp2_dynamic,
                    )

        if 1 in self._function1:
            ir_hex = self._apply_type_1(ir_hex, self._function1[1])
        elif 1 in self._function2:
            ir_hex = self._apply_type_2(ir_hex, self._function2[1])

        for checksum in self._checksum:
            ir_hex = self._apply_checksum(ir_hex, checksum)

        ir_raw: list[int] = []
        ir_raw.extend(self._boot_code)
        for i in range(0, len(ir_hex)):
            bit_num = self._bits_per_byte(i)
            for j in range(0, bit_num):
                if self._endian == 0:
                    mask = (1 << (bit_num - 1)) >> j
                else:
                    mask = 1 << j
                ir_raw.extend(self._one if ir_hex[i] & mask else self._zero)
            for delay in self._delay:
                if delay["pos"] == i:
                    ir_raw.extend(delay["time"])
        if self._last_bit == 0:
            ir_raw.append(self._one[0])
        for delay in self._delay:
            if delay["pos"] == -1:
                ir_raw.extend(delay["time"])

        # repeat（如美的要求连发 3 帧）直接编进时序
        return ir_raw * self._repeat_time

    # ------------------------------------------------------------- 位/校验

    def _apply_checksum(self, ir_hex: bytearray, checksum: dict) -> bytearray:
        """按 bin 里声明的算法写入校验字节（支持 8 种类型）。"""
        ctype = checksum["type"]
        value = 0
        if ctype in (CHECKSUM_TYPE_BYTE, CHECKSUM_TYPE_BYTE_INVERSE):
            for i in range(checksum["start_byte_pos"], checksum["end_byte_pos"]):
                value += ir_hex[i]
            value += checksum["checksum_plus"]
            value %= 256
            if ctype == CHECKSUM_TYPE_BYTE_INVERSE:
                value = ~value + 256
            ir_hex[checksum["checksum_byte_pos"]] = value % 256
        elif ctype in (CHECKSUM_TYPE_HALF_BYTE, CHECKSUM_TYPE_HALF_BYTE_INVERSE):
            for i in range(checksum["start_byte_pos"], checksum["end_byte_pos"]):
                value += (ir_hex[i] >> 4) + (ir_hex[i] & 0x0F)
            value += checksum["checksum_plus"]
            value %= 256
            if ctype == CHECKSUM_TYPE_HALF_BYTE_INVERSE:
                value = ~value + 256
            ir_hex[checksum["checksum_byte_pos"]] = value % 256
        elif ctype in (
            CHECKSUM_TYPE_SPEC_HALF_BYTE,
            CHECKSUM_TYPE_SPEC_HALF_BYTE_INVERSE,
            CHECKSUM_TYPE_SPEC_HALF_BYTE_ONE_BYTE,
            CHECKSUM_TYPE_SPEC_HALF_BYTE_INVERSE_ONE_BYTE,
        ):
            if "spec_pos" not in checksum:
                return ir_hex
            for pos in checksum["spec_pos"]:
                byte_pos = pos >> 1
                value += (
                    ir_hex[byte_pos] >> 4 if pos & 0x01 == 0 else ir_hex[byte_pos] & 0x0F
                )
            value += checksum["checksum_plus"]
            value %= 256
            inverse = ctype in (
                CHECKSUM_TYPE_SPEC_HALF_BYTE_INVERSE,
                CHECKSUM_TYPE_SPEC_HALF_BYTE_INVERSE_ONE_BYTE,
            )
            if inverse:
                value = ~value + 256
            apply_byte_pos = checksum["checksum_byte_pos"] >> 1
            if ctype in (
                CHECKSUM_TYPE_SPEC_HALF_BYTE,
                CHECKSUM_TYPE_SPEC_HALF_BYTE_INVERSE,
            ):
                # 半字节写入
                if checksum["checksum_byte_pos"] & 0x01 == 0:
                    ir_hex[apply_byte_pos] = (
                        (ir_hex[apply_byte_pos] & 0x0F) | (value << 4)
                    ) % 256
                else:
                    ir_hex[apply_byte_pos] = (
                        (ir_hex[apply_byte_pos] & 0xF0) | (value & 0x0F)
                    ) % 256
            else:
                ir_hex[apply_byte_pos] = value % 256
        return ir_hex

    def _apply_type_1(
        self, ir_hex: bytearray, data: bytes, is_temp: bool = False
    ) -> bytearray:
        """按字节写：[pos, value, ...]（温度模式为加法）。"""
        for i in range(0, len(data), 2):
            if is_temp:
                ir_hex[data[i]] = (ir_hex[data[i]] + data[i + 1]) % 256
            else:
                ir_hex[data[i]] = data[i + 1]
        return ir_hex

    def _apply_type_2(
        self, ir_hex: bytearray, data: bytes, is_temp: bool = False
    ) -> bytearray:
        """按位段写：[start_bit, end_bit, value, ...]（可跨字节）。"""
        for i in range(0, len(data), 3):
            start_bit = data[i]
            end_bit = data[i + 1]
            bit_range = end_bit - start_bit
            raw_value = data[i + 2]
            cover_hi = start_bit >> 3
            cover_lo = (end_bit - 1) >> 3
            int_start = start_bit - (cover_hi << 3)
            int_end = end_bit - (cover_lo << 3)
            if cover_hi == cover_lo:
                mask = ((0xFF << (8 - int_start)) | (0xFF >> int_end)) % 256
                origin = ir_hex[cover_lo]
                if is_temp:
                    move_bit = 8 - int_end
                    value = (origin & mask) | (
                        ((((origin & ~mask) >> move_bit) + raw_value) << move_bit)
                        & ~mask
                    )
                else:
                    value = (origin & mask) | (
                        (raw_value << (8 - int_start - bit_range)) & ~mask
                    )
                ir_hex[cover_lo] = value % 256
            else:
                origin_hi = ir_hex[cover_hi]
                origin_lo = ir_hex[cover_lo]
                mask_hi = 0xFF << (8 - int_start)
                mask_lo = 0xFF >> int_end
                value = ((origin_hi & ~mask_hi) << int_end) | (
                    (origin_lo & ~mask_lo) >> (8 - int_end)
                )
                if is_temp:
                    raw_value += value
                ir_hex[cover_hi] = (
                    (origin_hi & mask_hi)
                    | ((0xFF >> (8 - bit_range)) & raw_value) >> int_end
                ) % 256
                ir_hex[cover_lo] = (
                    (origin_lo & mask_lo)
                    | ((0xFF >> (8 - bit_range)) & raw_value) << (8 - int_end)
                ) % 256
        return ir_hex

    def _bits_per_byte(self, index: int) -> int:
        """该字节实际写几位（默认 8）。"""
        if not self._bit_num:
            return 8
        for bit_num in self._bit_num:
            if bit_num["pos"] == index:
                return bit_num["bits"]
            if bit_num["pos"] > index:
                return 8
        return 8

    # ------------------------------------------------------------- 解析工具

    @staticmethod
    def _parse_data(hex_data: str) -> list[bytes]:
        """hex 字符串 -> 分段列表。"""
        return _parse_segments(bytes.fromhex(hex_data)) if hex_data else []

    @staticmethod
    def _parse_n_mode(data: str) -> dict:
        """解析某模式的禁用项声明。"""
        if data == "NA":
            return {"disabled": True}
        result: dict = {}
        if data == "":
            result["speed"] = []
            result["temperature"] = []
            return result
        for sub in data.split("|"):
            if sub in ("S", "s"):
                result["speed"] = list(range(0, 4))
            elif sub in ("T", "t"):
                result["temperature"] = list(range(16, 31))
            elif sub.startswith(("S&", "s&")):
                result["speed"] = [int(s) for s in sub[2:].split(",") if s]
            elif sub.startswith(("T&", "t&")):
                result["temperature"] = [int(t) for t in sub[2:].split(",") if t]
        return result

    @staticmethod
    def _parse_temp(hex_data: str, stride: int) -> tuple[list, bool]:
        """TAG_AC_TEMP_1/2：static = [pos, value, ...]；dynamic = 15 组加法段。

        返回 (parsed, is_dynamic)。动态温度的段结构随 stride（2 或 3）不同。
        """
        result: list = []
        if hex_data == "":
            return result, False
        temp_data = bytes.fromhex(hex_data)
        seg_len = temp_data[0]
        if seg_len != len(temp_data) - 1:
            return _parse_segments(temp_data), False
        # dynamic：15 组（16~30°C），组内 [pos, value, index*value, ...]
        if stride == 2:
            for index in range(0, 15):
                segment: list[int] = []
                for i in range(1, seg_len, 2):
                    segment.append(temp_data[i])
                    segment.append(temp_data[i + 1] * index)
                result.append(segment)
        else:
            for index in range(0, 15):
                segment = []
                for i in range(2, seg_len, 3):
                    segment.append(temp_data[i - 1])
                    segment.append(temp_data[i])
                    segment.append(temp_data[i + 1] * index)
                result.append(segment)
        return result, True

    # 兼容旧命名（SmartAC 用 _swing1/_swing2；本文件在 ir_decode 里引用）
    @property
    def _swing1(self) -> list[bytes]:
        if not hasattr(self, "__swing1"):
            self.__swing1 = self._parse_data(
                self._tags.get(TAG_AC_SWING_1, b"").decode()
            )
        return self.__swing1

    @property
    def _swing2(self) -> list[bytes]:
        if not hasattr(self, "__swing2"):
            self.__swing2 = self._parse_data(
                self._tags.get(TAG_AC_SWING_2, b"").decode()
            )
        return self.__swing2


def _split_ints(text: str) -> list[int]:
    return [int(t) for t in text.split(",") if t]


def decode_bin(data: bytes) -> dict:
    """把一台空调的 `.bin` 解码成 HA 侧可查表的结构（帧已补符号）。

    返回::

        {
            "modes":     ["cool", "heat", ...],          # HVACMode 值
            "fan_modes": ["auto", "low", ...],           # 全模式并集
            "commands":  {mode: {fan: {"16".."30": [timings]}}},
            "off":       [timings],                      # 关机帧
            "min_temp": int, "max_temp": int,
        }
    """
    ac = _AcBin(data)

    modes = ac.supported_modes()
    if not modes:
        raise ValueError("该 bin 没有任何可用模式（数据无效？）")

    commands: dict[str, dict[str, dict[str, list[int]]]] = {}
    speeds_seen: set[int] = set()
    temps_seen: set[int] = set()

    for m in modes:
        mode_key = HVAC_MODE_MAP[m]
        commands[mode_key] = {}
        speeds = ac.supported_speeds(m) or [SPEED_AUTO]
        for s in speeds:
            speeds_seen.add(s)
            speed_key = SPEED_MAP[s]
            commands[mode_key][speed_key] = {}
            temps = ac.temperature_range(m) or [26]
            for t in temps:
                temps_seen.add(t)
                raw = ac.ir_decode(POWER_ON, t, m, s)
                commands[mode_key][speed_key][str(t)] = sign_timings(raw)

    off_raw = ac.ir_decode(POWER_OFF, 26, MODE_AUTO, SPEED_AUTO)

    return {
        "modes": [HVAC_MODE_MAP[m] for m in modes],
        # 风速并集按固定顺序输出（SmartAC 用 set，顺序不定 —— 这里改进）
        "fan_modes": [SPEED_MAP[s] for s in _SPEEDS if s in speeds_seen],
        "commands": commands,
        "off": sign_timings(off_raw),
        "min_temp": min(temps_seen),
        "max_temp": max(temps_seen),
    }


def iter_frames(data: bytes):
    """逐帧产出 `(state, timings)` —— 供学习匹配**流式**比对。

    state 形如 `("on", 模式, 风速, 温度)` 或 `("off",)`；timings 带符号
    （与 `decode_bin` 出口一致）。顺序与 `decode_bin` 生成 commands 的顺序相同，
    最后是关机帧。

    为什么单独提供：一台空调最多 300 帧，`decode_bin` 会把它们全建出来；
    而学习匹配只要求"一次一帧地过一遍" —— 用生成器就不必把全库 15 万帧塞进内存。
    """
    ac = _AcBin(data)
    modes = ac.supported_modes()
    if not modes:
        raise ValueError("该 bin 没有任何可用模式（数据无效？）")
    for mode in modes:
        mode_key = HVAC_MODE_MAP[mode]
        for speed in ac.supported_speeds(mode) or [SPEED_AUTO]:
            fan_key = SPEED_MAP[speed]
            for temp in ac.temperature_range(mode) or [26]:
                yield ("on", mode_key, fan_key, temp), sign_timings(
                    ac.ir_decode(POWER_ON, temp, mode, speed)
                )
    yield ("off",), sign_timings(ac.ir_decode(POWER_OFF, 26, MODE_AUTO, SPEED_AUTO))


class AcLibrary:
    """`ac_library/` 下 bin 码库的只读视图。"""

    def __init__(self, path: str = AC_LIBRARY_DIR) -> None:
        self._path = path
        with open(os.path.join(path, "index.json"), encoding="utf-8") as handle:
            # [{"brand_name": "格力", "devices": [{"device_name", "bin"}, ...]}, ...]
            self._index: list[dict] = json.load(handle)
        self._brands: dict[str, list[dict]] = {
            brand["brand_name"]: brand["devices"] for brand in self._index
        }
        self._decoded: dict[str, dict] = {}

    @property
    def brands(self) -> list[str]:
        """全部品牌名。"""
        return list(self._brands)

    @property
    def brand_count(self) -> int:
        """品牌总数。"""
        return len(self._brands)

    @property
    def device_count(self) -> int:
        """型号总数。"""
        return sum(len(devs) for devs in self._brands.values())

    def devices_in(self, brand: str) -> list[dict]:
        """某品牌下的全部型号条目。"""
        return self._brands.get(brand, [])

    # ------------------------------------------------------------ 反查（学习用）

    @property
    def index_path(self) -> str:
        """index.json 所在目录（做缓存键用）。"""
        return self._path

    def bin_names(self) -> list[str]:
        """索引引用到的全部 bin（去重、排序）。"""
        seen: dict[str, None] = {}
        for devices in self._brands.values():
            for device in devices:
                seen.setdefault(device["bin"], None)
        return sorted(seen)

    def devices_by_bin(self) -> dict[str, list[tuple[str, str]]]:
        """bin -> [(品牌, 型号名), ...]（同一 bin 可能被多个品牌共用）。"""
        out: dict[str, list[tuple[str, str]]] = {}
        for brand, devices in self._brands.items():
            for device in devices:
                out.setdefault(device["bin"], []).append((brand, device["device_name"]))
        return out

    def brands_of(self, bin_name: str) -> list[str]:
        """该 bin 被哪些品牌共用（去重、按索引顺序）。

        一个 bin 常被多个品牌共用（509 个里 149 个；最多的一个有 216 个品牌），
        所以"这台空调是什么牌子"从 bin 反查不出来，只能拿到一份候选品牌名单。
        """
        out: dict[str, None] = {}
        for brand, devices in self._brands.items():
            for device in devices:
                if device["bin"] == bin_name:
                    out.setdefault(brand, None)
        return list(out)

    def brand_of(self, bin_name: str) -> str:
        """反查 bin 所属品牌（多个品牌共用时取第一个；想要全名单用 `brands_of`）。"""
        return next(iter(self.brands_of(bin_name)), "")

    def read_raw(self, bin_name: str) -> bytes:
        """直接读 bin 原始字节。

        不走 `load_device` 的解码缓存 —— 学习匹配是"读一次、用完就扔"，
        缓存整台空调的 300 帧会把内存吃光。
        """
        with open(os.path.join(self._path, "codes", bin_name), "rb") as handle:
            return handle.read()

    def load_device(self, bin_name: str) -> dict:
        """解码一个型号（带缓存 —— 同一 bin 可能在多个品牌下重复出现）。"""
        cached = self._decoded.get(bin_name)
        if cached is not None:
            return cached
        path = os.path.join(self._path, "codes", bin_name)
        with open(path, "rb") as handle:
            code = decode_bin(handle.read())
        self._decoded[bin_name] = code
        return code
