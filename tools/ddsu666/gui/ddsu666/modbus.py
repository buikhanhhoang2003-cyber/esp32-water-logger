"""Modbus RTU framing as described in appendix A of the DDSU666 manual.

The meter implements only function 03H (read registers) and 10H (write
multiple registers). Function 06H is not listed, so every write uses 10H.
"""

from __future__ import annotations

import struct
from typing import Sequence

READ_REGISTERS = 0x03
WRITE_REGISTERS = 0x10
MAX_READ_COUNT = 125
MAX_WRITE_COUNT = 123

# Table A.8 of the manual.
EXCEPTION_TEXT = {
    0x01: "mã chức năng không hợp lệ (01H)",
    0x02: "địa chỉ thanh ghi không hợp lệ (02H)",
    0x03: "giá trị dữ liệu không hợp lệ (03H)",
}


class ModbusError(Exception):
    """A request did not produce a usable answer."""


class NoResponse(ModbusError):
    """Nothing came back before the timeout."""


class FrameError(ModbusError):
    """Bytes came back but did not form a valid answer (CRC, length, address)."""


class ExceptionResponse(ModbusError):
    """The meter answered with an exception code (table A.8)."""

    def __init__(self, function: int, code: int):
        self.function = function
        self.code = code
        text = EXCEPTION_TEXT.get(code, f"mã lỗi {code:02X}H")
        super().__init__(f"Đồng hồ từ chối lệnh {function:02X}H: {text}")


def crc16(data: bytes) -> int:
    """CRC-16/MODBUS: preset FFFFH, polynomial A001H (section A.4)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def with_crc(body: bytes) -> bytes:
    """Append the CRC, low byte first."""
    crc = crc16(body)
    return bytes(body) + bytes((crc & 0xFF, crc >> 8))


def crc_ok(frame: bytes) -> bool:
    return len(frame) >= 4 and crc16(frame[:-2]) == frame[-2] | (frame[-1] << 8)


def to_int16(word: int) -> int:
    """Registers of table 9 are "16-bit with symbols", i.e. signed."""
    return word - 0x10000 if word & 0x8000 else word


def to_word(value: int) -> int:
    """Accept a signed or unsigned 16-bit value and return the register word."""
    if not -0x8000 <= value <= 0xFFFF:
        raise ValueError(f"{value} không nằm trong 16 bit (-32768..65535)")
    return value & 0xFFFF


def words_to_float(words: Sequence[int], word_order: str = "ABCD") -> float:
    """Decode an IEEE-754 single from two registers.

    ABCD sends the high word first (the usual Modbus order); CDAB swaps the words.
    """
    high, low = words[0], words[1]
    if word_order == "CDAB":
        high, low = low, high
    return struct.unpack(">f", struct.pack(">HH", high, low))[0]


def float_to_words(value: float, word_order: str = "ABCD") -> tuple[int, int]:
    high, low = struct.unpack(">HH", struct.pack(">f", value))
    return (low, high) if word_order == "CDAB" else (high, low)


def _check_slave(slave: int) -> None:
    if not 1 <= slave <= 247:
        raise ValueError(f"Địa chỉ đồng hồ phải trong 1..247 (đang là {slave})")


def _check_span(address: int, count: int) -> None:
    if not 0 <= address <= 0xFFFF or address + count > 0x10000:
        raise ValueError("Địa chỉ thanh ghi vượt quá 0000H..FFFFH")


def read_request(slave: int, address: int, count: int) -> bytes:
    _check_slave(slave)
    if not 1 <= count <= MAX_READ_COUNT:
        raise ValueError(f"Số thanh ghi đọc phải trong 1..{MAX_READ_COUNT}")
    _check_span(address, count)
    return with_crc(struct.pack(">BBHH", slave, READ_REGISTERS, address, count))


def write_request(slave: int, address: int, values: Sequence[int]) -> bytes:
    _check_slave(slave)
    count = len(values)
    if not 1 <= count <= MAX_WRITE_COUNT:
        raise ValueError(f"Số thanh ghi ghi phải trong 1..{MAX_WRITE_COUNT}")
    _check_span(address, count)
    words = [to_word(value) for value in values]
    body = struct.pack(">BBHHB", slave, WRITE_REGISTERS, address, count, 2 * count)
    return with_crc(body + struct.pack(f">{count}H", *words))


def expected_length(request: bytes) -> int:
    """Length of a normal answer to `request`, or 0 when it cannot be known."""
    if len(request) >= 6 and request[1] == READ_REGISTERS:
        return 5 + 2 * struct.unpack(">H", request[4:6])[0]
    if len(request) >= 6 and request[1] == WRITE_REGISTERS:
        return 8
    return 0


def frame_complete(data: bytes, expect_len: int) -> bool:
    """True once `data` holds a whole answer (normal or exception)."""
    if len(data) >= 5 and data[1] & 0x80:
        return True
    return expect_len > 0 and len(data) >= expect_len


def strip_echo(request: bytes, response: bytes) -> bytes:
    """Drop our own frame if the RS485 transceiver echoed it back."""
    if len(response) > len(request) and response.startswith(request):
        return response[len(request):]
    return response


def trim_frame(request: bytes, response: bytes) -> bytes:
    """Cut trailing line noise after a complete answer.

    The expected length decides, not a CRC match on the whole buffer: this CRC has no
    final XOR, so a valid frame followed by a 00H byte also "checks out" as a longer frame.
    """
    if len(response) >= 5 and response[1] & 0x80 and crc_ok(response[:5]):
        return response[:5]
    expect = expected_length(request)
    if expect and len(response) >= expect and crc_ok(response[:expect]):
        return response[:expect]
    return response


def _validate(request: bytes, response: bytes) -> None:
    if not response:
        raise NoResponse("Không có phản hồi")
    if len(response) < 5:
        raise FrameError(f"Phản hồi quá ngắn ({len(response)} byte): {response.hex(' ').upper()}")
    if not crc_ok(response):
        raise FrameError(f"Sai CRC: {response.hex(' ').upper()}")
    if response[0] != request[0]:
        raise FrameError(f"Phản hồi đến từ địa chỉ {response[0]}, đang chờ địa chỉ {request[0]}")
    if response[1] == request[1] | 0x80:
        raise ExceptionResponse(request[1], response[2])
    if response[1] != request[1]:
        raise FrameError(f"Phản hồi sai mã chức năng {response[1]:02X}H")


def parse_read_response(request: bytes, response: bytes) -> list[int]:
    _validate(request, response)
    count = struct.unpack(">H", request[4:6])[0]
    if response[2] != 2 * count or len(response) != 5 + 2 * count:
        raise FrameError(f"Phản hồi đọc sai độ dài: chờ {2 * count} byte dữ liệu, nhận {response[2]}")
    return list(struct.unpack(f">{count}H", response[3:3 + 2 * count]))


def parse_write_response(request: bytes, response: bytes) -> None:
    _validate(request, response)
    if len(response) != 8 or response[2:6] != request[2:6]:
        raise FrameError("Phản hồi ghi không khớp địa chỉ/số lượng thanh ghi")


def parse_int(text: str) -> int:
    """Parse "8192", "0x2000" or "2000H" (the manual writes addresses as 2000H)."""
    value = text.strip().replace("_", "")
    if not value:
        raise ValueError("Chưa nhập giá trị")
    lowered = value.lower()
    if lowered.endswith("h"):
        return int(lowered[:-1], 16)
    if lowered.startswith(("0x", "-0x")):
        return int(lowered, 16)
    return int(lowered, 10)
