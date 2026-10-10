"""Ways of getting a Modbus RTU frame onto the RS485 bus.

* BridgeTransport: the ESP32 running ../firmware (USB serial, one text line per command).
* DirectTransport: a USB-RS485 adapter, the PC itself is the Modbus master.
* SimTransport:    an in-process simulated meter, to try the GUI without hardware.
"""

from __future__ import annotations

import re
import time
from typing import Callable

from .modbus import ModbusError, frame_complete, strip_echo

LogFn = Callable[[str, str], None]


class TransportError(ModbusError):
    """The PC-side link (COM port or ESP32 bridge) refused or failed a command."""


class LinkLost(TransportError):
    """The COM port or the USB link to the ESP32 is gone; the connection must be reopened."""


def _serial_module():
    try:
        import serial
    except ImportError as exc:
        raise TransportError("Thiếu thư viện pyserial — chạy: python -m pip install pyserial") from exc
    return serial


def bits_per_char(fmt: str) -> int:
    """Start bit + 8 data bits + optional parity bit + stop bits."""
    return 1 + 8 + (0 if fmt[1] == "N" else 1) + int(fmt[2])


def char_time_s(baud: int, fmt: str) -> float:
    return bits_per_char(fmt) / baud


def list_serial_ports() -> list[tuple[str, str]]:
    """(device, description) of every COM port, COM2 sorted before COM10."""
    from serial.tools import list_ports

    def key(port):
        match = re.match(r"(\D*)(\d+)$", port.device)
        return (match.group(1), int(match.group(2))) if match else (port.device, 0)

    return [(port.device, port.description or "") for port in sorted(list_ports.comports(), key=key)]


class Transport:
    """Puts one request frame on the bus and returns the raw bytes that came back."""

    def __init__(self, baud: int = 9600, fmt: str = "8N1", log: LogFn | None = None):
        self.baud = baud
        self.fmt = fmt
        self._log = log or (lambda kind, text: None)
        self.description = ""
        self.last_flags: tuple[str, ...] = ()

    def open(self) -> str:
        return self.description

    def close(self) -> None:
        pass

    def configure(self, baud: int, fmt: str) -> None:
        self.baud = baud
        self.fmt = fmt

    def transact(self, request: bytes, timeout_ms: int, expect_len: int) -> bytes:
        """Send `request` and return what arrived, b"" when nothing did.

        `timeout_ms` counts from the end of our transmission and must already cover the
        time the answer itself spends on the wire. `expect_len` (0 = unknown) lets the
        transport return as soon as a complete answer is in.
        """
        raise NotImplementedError


class BridgeTransport(Transport):
    """ESP32 running the RS485 bridge firmware; every reply line starts with '@'."""

    HOST_BAUD = 115200

    def __init__(self, port: str, baud: int = 9600, fmt: str = "8N1", log: LogFn | None = None,
                 serial_factory=None):
        super().__init__(baud, fmt, log)
        self.port = port
        self._serial_factory = serial_factory
        self._ser = None
        self._buffer = bytearray()
        self.info: dict[str, str] = {}

    def open(self) -> str:
        serial = _serial_module()
        ser = (self._serial_factory or serial.Serial)()
        ser.port = self.port
        ser.baudrate = self.HOST_BAUD
        ser.timeout = 0.02
        ser.write_timeout = 2
        # DTR/RTS drive EN/IO0 on ESP32 dev boards: keep them released so that
        # opening the port does not reset the chip.
        ser.dtr = False
        ser.rts = False
        try:
            ser.open()
        except (serial.SerialException, OSError) as exc:
            raise TransportError(f"Không mở được {self.port}: {exc}") from exc
        self._ser = ser
        self._buffer.clear()
        try:
            self._handshake()
            self._apply_config()
        except BaseException:
            self.close()
            raise
        return self.description

    def close(self) -> None:
        ser, self._ser = self._ser, None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass

    def configure(self, baud: int, fmt: str) -> None:
        previous = (self.baud, self.fmt)
        super().configure(baud, fmt)
        if self._ser is not None:
            try:
                self._apply_config()
            except ModbusError:
                super().configure(*previous)
                raise

    def transact(self, request: bytes, timeout_ms: int, expect_len: int) -> bytes:
        timeout_ms = max(1, min(int(timeout_ms), 10000))
        wire_s = len(request) * char_time_s(self.baud, self.fmt)
        reply = self._command(f"TX {request.hex().upper()} {timeout_ms} {int(expect_len)}",
                              ("@RX", "@TIMEOUT"), wire_s + timeout_ms / 1000 + 1.5)
        fields = reply.split()
        if fields[0] == "@TIMEOUT":
            self.last_flags = tuple(fields[1:])
            return b""
        self.last_flags = tuple(fields[2:])
        try:
            return bytes.fromhex(fields[1])
        except (IndexError, ValueError) as exc:
            raise TransportError(f"ESP32 trả về dữ liệu hỏng: {reply[:80]}") from exc

    # -- bridge protocol -------------------------------------------------

    def _handshake(self) -> None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            self._send("PING")
            reply = self._wait_reply(("@PONG",), 0.8)
            if reply and reply.startswith("@PONG"):
                self._parse_pong(reply)
                # Answers to PINGs sent while the board was still booting may follow.
                self._drain(0.15)
                return
        raise TransportError(
            f"Không thấy firmware cầu nối trả lời trên {self.port}.\n"
            "• Đã nạp firmware cầu nối (tools/ddsu666/firmware) vào ESP32 chưa? Lệnh: dev.bat bridge\n"
            "• Đúng cổng COM của ESP32 chưa?\n"
            "• Đã đóng idf.py monitor / Arduino Serial Monitor đang giữ cổng chưa?")

    def _parse_pong(self, reply: str) -> None:
        fields = reply.split()
        version = fields[2] if len(fields) > 2 else "?"
        self.info = dict(field.split("=", 1) for field in fields[3:] if "=" in field)
        bus = self.info.get("bus", "ok")
        if bus != "ok":
            raise TransportError(f"ESP32 không khởi tạo được UART RS485 ({bus}); kiểm tra chân trong menuconfig.")
        info = self.info
        self.description = (f"ESP32 cầu nối v{version} · UART{info.get('port', '?')} "
                            f"TX{info.get('tx', '?')} RX{info.get('rx', '?')}")
        if info.get("de", "-1") != "-1":
            self.description += f" DE{info['de']}"

    def _apply_config(self) -> None:
        self._command(f"CFG {self.baud} {self.fmt}", ("@OK",), 2.0, resend_after_reboot=False)

    def _command(self, line: str, prefixes: tuple[str, ...], timeout_s: float,
                 resend_after_reboot: bool = True) -> str:
        self._send(line)
        reply = self._wait_reply(prefixes, timeout_s)
        if reply is not None and reply.startswith("@READY"):
            if not resend_after_reboot:
                raise LinkLost("ESP32 liên tục khởi động lại (kiểm tra nguồn/cáp USB)")
            # A reboot puts the bus back to its default line settings.
            self._log("warn", "ESP32 vừa khởi động lại — gửi lại cấu hình RS485 và lặp lại lệnh")
            self._drain(0.3)
            self._apply_config()
            return self._command(line, prefixes, timeout_s, resend_after_reboot=False)
        if reply is None:
            raise LinkLost("ESP32 không trả lời qua USB (mất kết nối hoặc board đang reset)")
        if reply.startswith("@ERR"):
            raise TransportError(f"ESP32 báo lỗi: {reply[4:].strip()}")
        return reply

    def _wait_reply(self, prefixes: tuple[str, ...], timeout_s: float) -> str | None:
        deadline = time.monotonic() + timeout_s
        while True:
            line = self._readline(deadline)
            if line is None:
                return None
            if not line.startswith("@"):
                if line:
                    self._log("dev", f"ESP32: {line}")
                continue
            if line.startswith(prefixes) or line.startswith(("@ERR", "@READY")):
                return line
            # Commands are answered in order, so this belongs to an earlier, abandoned one.
            self._log("info", f"Bỏ qua phản hồi cũ của ESP32: {line[:60]}")

    def _readline(self, deadline: float) -> str | None:
        serial = _serial_module()
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                raw = bytes(self._buffer[:newline])
                del self._buffer[:newline + 1]
                return raw.decode("ascii", "replace").strip()
            if time.monotonic() >= deadline:
                return None
            if self._ser is None:
                raise LinkLost("Chưa kết nối")
            try:
                waiting = self._ser.in_waiting
                chunk = self._ser.read(waiting if waiting else 1)
            except (serial.SerialException, OSError) as exc:
                raise LinkLost(f"Mất kết nối với {self.port}: {exc}") from exc
            if chunk:
                self._buffer.extend(chunk)
                if len(self._buffer) > 8192 and b"\n" not in self._buffer:
                    self._buffer.clear()

    def _drain(self, quiet_s: float) -> None:
        deadline = time.monotonic() + quiet_s
        while True:
            line = self._readline(deadline)
            if line is None:
                return
            if line and not line.startswith("@"):
                self._log("dev", f"ESP32: {line}")

    def _send(self, line: str) -> None:
        serial = _serial_module()
        if self._ser is None:
            raise LinkLost("Chưa kết nối")
        try:
            self._ser.write((line + "\n").encode("ascii"))
        except (serial.SerialException, OSError) as exc:
            raise LinkLost(f"Không gửi được tới {self.port}: {exc}") from exc


class DirectTransport(Transport):
    """USB-RS485 adapter: the PC drives Modbus RTU on the COM port itself."""

    def __init__(self, port: str, baud: int = 9600, fmt: str = "8N1", log: LogFn | None = None):
        super().__init__(baud, fmt, log)
        self.port = port
        self._ser = None

    def open(self) -> str:
        serial = _serial_module()
        ser = serial.Serial()
        ser.port = self.port
        ser.timeout = 0
        ser.write_timeout = 2
        self._apply(ser)
        try:
            ser.open()
        except (serial.SerialException, OSError) as exc:
            raise TransportError(f"Không mở được {self.port}: {exc}") from exc
        self._ser = ser
        self.description = f"USB-RS485 trực tiếp · {self.port}"
        return self.description

    def close(self) -> None:
        ser, self._ser = self._ser, None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass

    def configure(self, baud: int, fmt: str) -> None:
        serial = _serial_module()
        super().configure(baud, fmt)
        if self._ser is not None:
            try:
                self._apply(self._ser)
            except (serial.SerialException, ValueError, OSError) as exc:
                raise TransportError(f"Không đặt được {baud} {fmt} cho {self.port}: {exc}") from exc

    def _apply(self, ser) -> None:
        serial = _serial_module()
        ser.baudrate = self.baud
        ser.bytesize = serial.EIGHTBITS
        ser.parity = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN, "O": serial.PARITY_ODD}[self.fmt[1]]
        ser.stopbits = serial.STOPBITS_TWO if self.fmt[2] == "2" else serial.STOPBITS_ONE

    def transact(self, request: bytes, timeout_ms: int, expect_len: int) -> bytes:
        serial = _serial_module()
        if self._ser is None:
            raise LinkLost("Chưa kết nối")
        ser = self._ser
        char_s = char_time_s(self.baud, self.fmt)
        # USB adapters hand data over in bursts, so 3.5 characters of silence is too tight.
        idle_s = max(0.03, 4 * char_s)
        data = bytearray()
        try:
            ser.reset_input_buffer()
            ser.write(request)
            ser.flush()
            deadline = time.monotonic() + len(request) * char_s + timeout_ms / 1000
            last_rx = None
            while True:
                waiting = ser.in_waiting
                if waiting:
                    data.extend(ser.read(waiting))
                    last_rx = time.monotonic()
                    frame = bytes(data)
                    echo_so_far = len(frame) <= len(request) and request.startswith(frame)
                    if not echo_so_far and frame_complete(strip_echo(request, frame), expect_len):
                        break
                    continue
                now = time.monotonic()
                if last_rx is None:
                    if now >= deadline:
                        break
                elif now - last_rx >= idle_s and (expect_len == 0 or now >= deadline):
                    break
                time.sleep(0.002)
        except (serial.SerialException, OSError) as exc:
            raise LinkLost(f"Lỗi cổng {self.port}: {exc}") from exc
        return bytes(data)


class SimTransport(Transport):
    """Talks to a SimulatedDDSU666 in-process; wrong line settings behave like silence."""

    def __init__(self, meter, baud: int = 9600, fmt: str = "8N1", log: LogFn | None = None,
                 realtime: bool = True):
        super().__init__(baud, fmt, log)
        self.meter = meter
        self.realtime = realtime
        self.description = "Mô phỏng DDSU666 (không cần phần cứng)"
        self._open = False

    def open(self) -> str:
        self._open = True
        return self.description

    def close(self) -> None:
        self._open = False

    def transact(self, request: bytes, timeout_ms: int, expect_len: int) -> bytes:
        if not self._open:
            raise LinkLost("Chưa kết nối")
        reply = b""
        if self.baud == self.meter.baud and self.fmt == self.meter.fmt:
            reply = self.meter.handle(request) or b""
        if self.realtime:
            if reply:
                time.sleep(0.015 + (len(request) + len(reply)) * char_time_s(self.baud, self.fmt))
            else:
                # Shortened: a real silent bus costs the whole timeout.
                time.sleep(min(timeout_ms, 150) / 1000)
        return reply
