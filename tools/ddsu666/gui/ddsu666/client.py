"""DDSU666 operations on top of a Transport: block reads, verified writes, bus scan."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from . import registers as R
from .modbus import (ExceptionResponse, FrameError, ModbusError, NoResponse, crc_ok, expected_length,
                     parse_read_response, parse_write_response, read_request, strip_echo, to_int16,
                     trim_frame, with_crc, words_to_float, write_request)
from .transport import Transport, char_time_s

# Pause after a write that changes address/baud/protocol before talking to the meter again.
SETTLE_S = 0.4


@dataclass
class Reading:
    register: R.Register
    words: tuple[int, ...] = ()
    value: float | int | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.value is not None

    @property
    def raw_hex(self) -> str:
        return " ".join(f"{word:04X}" for word in self.words)


@dataclass
class ScanHit:
    baud: int
    fmt: str
    address: int
    outcome: str          # "ok", "exception" or "crc"
    detail: str = ""
    rev: int | None = None
    meter_type: int | None = None


def combined_energy(readings: Iterable[Reading]) -> float | None:
    """ComEp as on the LCD: table 8 shows EImp 1.20 + EExp 1.00 = ComEp 2.20 kWh."""
    values = {reading.register.address: reading for reading in readings}
    imported = values.get(R.IMPORT_ENERGY.address)
    exported = values.get(R.EXPORT_ENERGY.address)
    if imported is None or exported is None or not (imported.ok and exported.ok):
        return None
    return float(imported.value) + float(exported.value)


def _hex(data: bytes) -> str:
    return data.hex(" ").upper()


def _flags(flags: Sequence[str]) -> str:
    return f"  [{' '.join(flags)}]" if flags else ""


def _flag_hint(flags: Sequence[str]) -> str:
    if "PE" in flags or "FE" in flags:
        return " (UART báo lỗi parity/khung bit: có thể sai baud hoặc định dạng 8N1/8E1/8O1)"
    return ""


class DDSU666Client:
    """Every method blocks; call them from one worker thread only."""

    def __init__(self, transport: Transport, slave: int = 1, timeout_ms: int = 300, retries: int = 2,
                 word_order: str = "ABCD", log: Callable[[str, str], None] | None = None):
        self._block_ok: dict[int, bool] = {}
        self._unreadable: set[int] = set()
        self._slave = slave
        self.transport = transport
        self.timeout_ms = timeout_ms
        self.retries = retries
        self.word_order = word_order
        self._log = log or (lambda kind, text: None)

    @property
    def slave(self) -> int:
        return self._slave

    @slave.setter
    def slave(self, value: int) -> None:
        if value != self._slave:
            self._slave = value
            # What one meter refuses to read says nothing about another one.
            self._block_ok.clear()
            self._unreadable.clear()

    # -- single requests -----------------------------------------------------

    def _answer_timeout(self, expect_len: int, timeout_ms: int | None = None) -> int:
        """Reply timeout plus the time the answer itself needs on the wire."""
        base = self.timeout_ms if timeout_ms is None else timeout_ms
        wire_ms = (expect_len or 64) * char_time_s(self.transport.baud, self.transport.fmt) * 1000
        return int(base + wire_ms * 1.2) + 5

    def _silence_text(self) -> str:
        return (f"Đồng hồ không phản hồi (địa chỉ {self.slave}, {self.transport.baud} bps {self.transport.fmt}). "
                "Kiểm tra dây A/B, địa chỉ, tốc độ baud, định dạng khung — hoặc dùng tab Quét thiết bị.")

    def exchange(self, request: bytes, retries: int | None = None, timeout_ms: int | None = None) -> bytes:
        """Send `request`, retrying on silence or CRC errors; return a CRC-valid frame."""
        expect = expected_length(request)
        attempts = 1 + (self.retries if retries is None else retries)
        error: ModbusError = NoResponse(self._silence_text())
        for attempt in range(attempts):
            if attempt:
                time.sleep(0.05)
            self._log("tx", _hex(request))
            raw = self.transport.transact(request, self._answer_timeout(expect, timeout_ms), expect)
            flags = self.transport.last_flags
            if not raw:
                error = NoResponse(self._silence_text())
                self._log("warn", f"Không có phản hồi (lần {attempt + 1}/{attempts})")
                continue
            self._log("rx", _hex(raw) + _flags(flags))
            if raw == request:
                error = NoResponse("Chỉ nhận lại đúng khung vừa gửi (tiếng vọng) — đồng hồ không trả lời")
                continue
            response = trim_frame(request, strip_echo(request, raw))
            if not crc_ok(response):
                error = FrameError(f"Sai CRC / khung hỏng{_flag_hint(flags)}: {_hex(raw)}")
                self._log("warn", str(error))
                continue
            return response
        raise error

    def read_registers(self, address: int, count: int, retries: int | None = None) -> list[int]:
        request = read_request(self.slave, address, count)
        return parse_read_response(request, self.exchange(request, retries))

    def write_registers(self, address: int, values: Sequence[int], retries: int | None = None) -> None:
        request = write_request(self.slave, address, list(values))
        parse_write_response(request, self.exchange(request, retries))

    def raw_exchange(self, frame: bytes, add_crc: bool = True) -> tuple[bytes, bytes]:
        """Send an arbitrary frame once; return (request actually sent, answer without echo)."""
        request = with_crc(frame) if add_crc else bytes(frame)
        if not request:
            raise ValueError("Khung rỗng")
        expect = expected_length(request) if crc_ok(request) else 0
        self._log("tx", _hex(request))
        raw = self.transport.transact(request, self._answer_timeout(expect), expect)
        if raw:
            self._log("rx", _hex(raw) + _flags(self.transport.last_flags))
        else:
            self._log("warn", "Không có phản hồi")
        return request, strip_echo(request, raw)

    # -- table reads ---------------------------------------------------------

    def _decode(self, register: R.Register, words: Sequence[int]) -> float | int:
        if register.kind == R.FLOAT32:
            return words_to_float(words, self.word_order)
        return to_int16(words[0])

    def _reading(self, register: R.Register, words: Sequence[int]) -> Reading:
        words = tuple(words)
        return Reading(register, words, self._decode(register, words))

    def read_block(self, block: R.Block) -> list[Reading]:
        """One request for the whole span; per-register reads if the meter refuses the span."""
        if self._block_ok.get(block.start, True):
            try:
                words = self.read_registers(block.start, block.count)
            except ExceptionResponse as exc:
                if exc.code not in (0x02, 0x03):
                    raise
                self._block_ok[block.start] = False
                self._log("info", f"Đồng hồ không cho đọc liền {block.count} thanh ghi từ {block.start:04X}H "
                                  f"(lỗi {exc.code:02X}H) — chuyển sang đọc từng thông số")
            else:
                self._block_ok[block.start] = True
                return [self._reading(register, words[register.address - block.start:
                                                      register.address - block.start + register.words])
                        for register in block.registers]
        readings = []
        for register in block.registers:
            if register.address in self._unreadable:
                readings.append(Reading(register, error="Đồng hồ không cho đọc (02H)"))
                continue
            try:
                readings.append(self._reading(register, self.read_registers(register.address, register.words)))
            except ExceptionResponse as exc:
                if exc.code == 0x02:
                    self._unreadable.add(register.address)
                readings.append(Reading(register, error=str(exc)))
        return readings

    def read_measurements(self) -> list[Reading]:
        return self.read_block(R.MEASUREMENT_BLOCK) + self.read_block(R.ENERGY_BLOCK)

    def read_parameters(self) -> list[Reading]:
        return self.read_block(R.PARAMETER_BLOCK)

    # -- writes ----------------------------------------------------------------

    def write_parameter(self, register: R.Register, value: int) -> None:
        if not register.writable:
            raise ValueError(f"{register.code} ({register.address:04X}H) là thanh ghi chỉ đọc")
        self.write_registers(register.address, [value])

    def clear_energy(self) -> None:
        """ClrE = 1 zeroes the accumulated energy."""
        self.write_registers(R.CLR_E.address, [1])

    def _write_setting(self, register: R.Register, value: int) -> bool:
        """Write a register that changes how the meter talks; True if the meter acknowledged.

        The meter may already use the new setting when it answers, so silence or a
        garbled reply is not a failure here: the caller verifies with the new setting.
        An exception reply (the meter refused) is raised.
        """
        request = write_request(self.slave, register.address, [value])
        try:
            parse_write_response(request, self.exchange(request, retries=0))
            return True
        except (NoResponse, FrameError) as exc:
            self._log("warn", f"Chưa nhận được xác nhận ghi {register.code}: {exc}")
            return False

    def _require_meter(self) -> None:
        """Fail early if the meter is not answering: after a settings write, silence would
        otherwise be indistinguishable from a meter that already switched over."""
        self.read_registers(R.ADDR.address, 1)

    def _responds(self) -> bool:
        try:
            self.read_registers(R.ADDR.address, 1, retries=1)
            return True
        except ModbusError:
            return False

    def change_address(self, new: int) -> str:
        if not 1 <= new <= 247:
            raise ValueError("Địa chỉ phải trong 1..247")
        old = self.slave
        if new == old:
            self.write_registers(R.ADDR.address, [new])
            return f"Địa chỉ giữ nguyên {new}"
        self._require_meter()
        acked = self._write_setting(R.ADDR, new)
        time.sleep(SETTLE_S)
        self.slave = new
        try:
            confirmed = to_int16(self.read_registers(R.ADDR.address, 1)[0])
        except ModbusError:
            self.slave = old
            if self._responds():
                raise ModbusError(f"Đồng hồ vẫn trả lời ở địa chỉ cũ {old}: địa chỉ chưa được đổi.") from None
            raise ModbusError(f"Không thấy đồng hồ ở địa chỉ mới {new} lẫn địa chỉ cũ {old}. "
                              "Dùng tab Quét thiết bị để tìm lại.") from None
        note = "" if acked else " (không nhận được xác nhận ghi, nhưng đọc lại ở địa chỉ mới thành công)"
        return f"Đã đổi địa chỉ {old} → {confirmed}{note}"

    def change_baud(self, code: int) -> str:
        if code not in R.BAUD_RATES:
            raise ValueError("BAud chỉ nhận 1 (2400), 2 (4800) hoặc 3 (9600)")
        rate = R.BAUD_RATES[code]
        old_rate, fmt = self.transport.baud, self.transport.fmt
        self._require_meter()
        acked = self._write_setting(R.BAUD, code)
        time.sleep(SETTLE_S)
        self.transport.configure(rate, fmt)
        try:
            confirmed = to_int16(self.read_registers(R.BAUD.address, 1)[0])
        except ModbusError:
            self.transport.configure(old_rate, fmt)
            if self._responds():
                raise ModbusError(f"Đồng hồ vẫn trả lời ở {old_rate} bps: tốc độ chưa được đổi.") from None
            raise ModbusError(f"Không thấy đồng hồ ở {rate} bps lẫn {old_rate} bps. "
                              "Dùng tab Quét thiết bị để tìm lại.") from None
        note = "" if acked else " (không nhận được xác nhận ghi, nhưng đọc lại ở tốc độ mới thành công)"
        return f"Đã đổi tốc độ {old_rate} → {rate} bps (BAud = {confirmed}){note}"

    def set_protocol(self, value: int) -> str:
        if value not in (1, 2):
            raise ValueError("ChangeProtocol chỉ nhận 2 (Modbus RTU) hoặc 1 (DL/T 645-2007)")
        if value == 2:
            self.write_registers(R.CHANGE_PROTOCOL.address, [2])
            return "Đã ghi ChangeProtocol = 2 (Modbus RTU)"
        self._require_meter()
        self._write_setting(R.CHANGE_PROTOCOL, 1)
        time.sleep(SETTLE_S)
        if self._responds():
            return "Đã ghi ChangeProtocol = 1 nhưng đồng hồ vẫn trả lời Modbus (có thể cần khởi động lại đồng hồ)."
        return "Đồng hồ đã chuyển sang DL/T 645-2007 và không còn trả lời Modbus."

    # -- bus scan --------------------------------------------------------------

    def probe(self, slave: int, timeout_ms: int) -> tuple[str, str] | None:
        """One short read of Addr; None when nothing answered."""
        request = read_request(slave, R.ADDR.address, 1)
        expect = expected_length(request)
        raw = self.transport.transact(request, self._answer_timeout(expect, timeout_ms), expect)
        if not raw or raw == request:
            return None
        response = trim_frame(request, strip_echo(request, raw))
        if not crc_ok(response):
            return "crc", f"có tín hiệu nhưng sai CRC{_flag_hint(self.transport.last_flags)}: {_hex(raw)[:47]}"
        if response[0] != slave:
            return "crc", f"trả lời mang địa chỉ {response[0]}"
        if response[1] & 0x80:
            return "exception", f"trả lời mã lỗi {response[2]:02X}H"
        return "ok", ""

    def _identify(self, hit: ScanHit) -> None:
        saved = self._slave
        self._slave = hit.address
        try:
            hit.rev = to_int16(self.read_registers(R.REV.address, 1, retries=1)[0])
            hit.meter_type = to_int16(self.read_registers(R.METER_TYPE.address, 1, retries=1)[0])
        except ModbusError:
            pass
        finally:
            self._slave = saved

    def scan(self, bauds: Sequence[int], fmts: Sequence[str], addresses: Iterable[int], probe_timeout_ms: int,
             cancel: threading.Event, stop_on_first: bool = True,
             progress: Callable[[int, int, str, int], None] | None = None,
             found: Callable[[ScanHit], None] | None = None) -> list[ScanHit]:
        """Try every baud × format × address; line settings are restored afterwards."""
        addresses = list(addresses)
        original = (self.transport.baud, self.transport.fmt)
        hits: list[ScanHit] = []
        done = 0
        try:
            for baud in bauds:
                for fmt in fmts:
                    self.transport.configure(baud, fmt)
                    for address in addresses:
                        if cancel.is_set():
                            return hits
                        if progress:
                            progress(done, baud, fmt, address)
                        outcome = self.probe(address, probe_timeout_ms)
                        done += 1
                        if outcome is None:
                            continue
                        hit = ScanHit(baud, fmt, address, *outcome)
                        if hit.outcome == "ok":
                            self._identify(hit)
                        hits.append(hit)
                        self._log("info", f"Quét: {baud} bps {fmt}, địa chỉ {address} → {hit.outcome} {hit.detail}")
                        if found:
                            found(hit)
                        if stop_on_first and hit.outcome == "ok":
                            return hits
        finally:
            self.transport.configure(*original)
        return hits
