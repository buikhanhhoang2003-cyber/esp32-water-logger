"""A simulated DDSU666 for trying the GUI and for tests.

It answers like the manual describes: only functions 03H and 10H, the registers of
table 9, exception codes of table A.8, and new communication settings take effect
after the reply has been sent. The default values (UCode, REV., Meter type) are
made up for the simulation; they are not documented values.

Reads touching 4002H..4009H (absent from the manual) fail with exception 02H, and so
do RESERVED rows of table 9 when `readable_reserved` is False; both exercise the
client's per-register fallback.
"""

from __future__ import annotations

import math
import random
import struct
import threading
import time

from .modbus import crc_ok, float_to_words, to_int16, with_crc
from .registers import BAUD_RATES


class SimulatedDDSU666:
    def __init__(self, address: int = 1, baud_code: int = 3, fmt: str = "8N1", word_order: str = "ABCD",
                 time_scale: float = 60.0, seed: int | None = None, readable_reserved: bool = True):
        self.address = address
        self.baud_code = baud_code
        self.fmt = fmt
        self.word_order = word_order
        self.protocol = 2
        self.ucode = 701
        self.rev = 0x0102
        self.meter_type = 0x0001
        self.import_kwh = 1234.56
        self.export_kwh = 0.37
        self.time_scale = time_scale  # energy accumulates this many times faster than real time
        self.readable_reserved = readable_reserved
        self.requests = 0
        self._random = random.Random(seed)
        self._t0 = time.monotonic()
        self._last = self._t0
        self._pending: dict[str, int] = {}
        self._lock = threading.Lock()

    @property
    def baud(self) -> int:
        return BAUD_RATES[self.baud_code]

    def handle(self, frame: bytes) -> bytes | None:
        """Process one request frame; return the reply, or None when the meter stays silent."""
        with self._lock:
            if self.protocol != 2:            # switched to DL/T 645-2007: Modbus is ignored
                return None
            if len(frame) < 4 or not crc_ok(frame):
                return None
            slave, function = frame[0], frame[1]
            if slave not in (self.address, 0):
                return None
            self.requests += 1
            reply = self._dispatch(function, frame[:-2])
            if slave == 0:                    # broadcast: execute, never answer
                reply = None
            self._apply_pending()
            return reply

    def _dispatch(self, function: int, body: bytes) -> bytes | None:
        if function == 0x03:
            if len(body) != 6:
                return None
            address, count = struct.unpack(">HH", body[2:6])
            if not 1 <= count <= 125:
                return self._exception(function, 0x03)
            words = self._registers()
            span = range(address, address + count)
            if any(item not in words for item in span):
                return self._exception(function, 0x02)
            data = struct.pack(f">{count}H", *(words[item] for item in span))
            return with_crc(bytes((self.address, function, 2 * count)) + data)
        if function == 0x10:
            if len(body) < 7:
                return None
            address, count, nbytes = struct.unpack(">HHB", body[2:7])
            if not 1 <= count <= 123 or nbytes != 2 * count or len(body) != 7 + nbytes:
                return self._exception(function, 0x03)
            values = struct.unpack(f">{count}H", body[7:7 + nbytes])
            for offset, word in enumerate(values):
                code = self._check_write(address + offset, to_int16(word))
                if code:
                    return self._exception(function, code)
            for offset, word in enumerate(values):
                self._write(address + offset, to_int16(word))
            return with_crc(bytes((self.address,)) + body[1:6])
        return self._exception(function, 0x01)

    def _exception(self, function: int, code: int) -> bytes:
        return with_crc(bytes((self.address, function | 0x80, code)))

    @staticmethod
    def _check_write(address: int, value: int) -> int:
        """Exception code for writing `value` to `address`, 0 when allowed."""
        allowed = {
            0x0000: lambda v: True,
            0x0002: lambda v: v in (0, 1),
            0x0005: lambda v: v in (1, 2),
            0x0006: lambda v: 1 <= v <= 247,
            0x000C: lambda v: v in BAUD_RATES,
        }
        if address not in allowed:
            return 0x02
        return 0 if allowed[address](value) else 0x03

    def _write(self, address: int, value: int) -> None:
        if address == 0x0000:
            self.ucode = value
        elif address == 0x0002 and value == 1:
            self.import_kwh = 0.0
            self.export_kwh = 0.0
        elif address == 0x0005:
            self._pending["protocol"] = value
        elif address == 0x0006:
            self._pending["address"] = value
        elif address == 0x000C:
            self._pending["baud_code"] = value

    def _apply_pending(self) -> None:
        for name, value in self._pending.items():
            setattr(self, name, value)
        self._pending.clear()

    def _registers(self) -> dict[int, int]:
        documented = (0x0000, 0x0001, 0x0002, 0x0005, 0x0006, 0x000B, 0x000C)
        span = range(0x0000, 0x0011) if self.readable_reserved else documented
        words = {address: 0 for address in span}
        words.update({
            0x0000: self.ucode & 0xFFFF,
            0x0001: self.rev,
            0x0005: self.protocol,
            0x0006: self.address,
            0x000B: self.meter_type,
            0x000C: self.baud_code,
        })
        voltage, current, power, reactive, factor, frequency = self._measure()
        floats = {
            0x2000: voltage, 0x2002: current, 0x2004: power, 0x2006: reactive, 0x2008: 0.0,
            0x200A: factor, 0x200C: 0.0, 0x200E: frequency, 0x2010: 0.0,
            0x4000: self.import_kwh, 0x400A: self.export_kwh,
        }
        for address, value in floats.items():
            words[address], words[address + 1] = float_to_words(value, self.word_order)
        return words

    def _measure(self) -> tuple[float, float, float, float, float, float]:
        now = time.monotonic()
        t = now - self._t0
        noise = self._random.uniform
        voltage = 229.6 + 1.8 * math.sin(t / 9.0) + noise(-0.15, 0.15)
        current = max(0.0, 2.2 + 1.5 * math.sin(t / 13.0) + noise(-0.03, 0.03))
        factor = min(1.0, 0.93 + 0.05 * math.sin(t / 17.0))
        power = voltage * current * factor / 1000
        reactive = voltage * current * math.sqrt(max(0.0, 1 - factor * factor)) / 1000
        frequency = 50.0 + 0.03 * math.sin(t / 5.0)
        self.import_kwh += power * (now - self._last) * self.time_scale / 3600
        self._last = now
        return voltage, current, power, reactive, factor, frequency
