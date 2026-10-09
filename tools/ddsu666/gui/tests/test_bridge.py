"""BridgeTransport against a fake COM port that behaves like the ESP32 firmware."""

import unittest
from unittest import mock

import serial

from ddsu666 import registers as R
from ddsu666.client import DDSU666Client
from ddsu666.modbus import read_request
from ddsu666.simulator import SimulatedDDSU666
from ddsu666.transport import BridgeTransport, LinkLost, TransportError


class FakeBridge:
    """pyserial stand-in running the firmware's command loop (see firmware/main/bridge_proto.h)."""

    def __init__(self, meter, boot_noise=True, deaf_pings=0):
        self.meter = meter
        self.port = None
        self.baudrate = None
        self.timeout = None
        self.write_timeout = None
        self.dtr = True
        self.rts = True
        self.lines_at_open = None
        self.is_open = False
        self.baud, self.fmt = 9600, "8N1"
        self.commands = []
        self.reboot_before = None   # command prefix that triggers a reboot instead of an answer
        self.unplug = False
        self.error_next = None
        self.deaf_pings = deaf_pings
        self._out = bytearray(b"ets Jun  8 2016 00:22:57\r\nI (31) boot: ESP-IDF v5.3.3\r\n"
                              if boot_noise else b"")
        self._in = bytearray()
        if boot_noise:
            self._out += b"@READY RS485-BRIDGE 1.0\n"

    # pyserial surface used by BridgeTransport
    def open(self):
        self.lines_at_open = (self.dtr, self.rts)
        self.is_open = True

    def close(self):
        self.is_open = False

    @property
    def in_waiting(self):
        if self.unplug:
            raise serial.SerialException("device disconnected")
        return len(self._out)

    def read(self, size=1):
        if self.unplug:
            raise serial.SerialException("device disconnected")
        chunk = bytes(self._out[:size])
        del self._out[:size]
        return chunk

    def write(self, data):
        self._in.extend(data)
        while b"\n" in self._in:
            line, _, rest = bytes(self._in).partition(b"\n")
            self._in = bytearray(rest)
            self._handle(line.decode().strip())
        return len(data)

    def _reply(self, text):
        self._out.extend((text + "\n").encode())

    def _handle(self, line):
        self.commands.append(line)
        if self.reboot_before and line.startswith(self.reboot_before):
            self.reboot_before = None
            self.baud, self.fmt = 9600, "8N1"
            self._out.extend(b"rst:0xc (SW_CPU_RESET)\r\n")
            self._reply("@READY RS485-BRIDGE 1.0")
            return
        if self.error_next:
            self._reply(f"@ERR {self.error_next}")
            self.error_next = None
            return
        fields = line.split()
        name = fields[0].upper() if fields else ""
        if name == "PING":
            if self.deaf_pings:
                self.deaf_pings -= 1
                return
            self._reply(f"@PONG RS485-BRIDGE 1.0 port=2 tx=16 rx=17 de=-1 baud={self.baud} fmt={self.fmt} bus=ok")
        elif name == "CFG":
            self.baud, self.fmt = int(fields[1]), fields[2]
            self._reply(f"@OK baud={self.baud} fmt={self.fmt}")
        elif name == "TX":
            try:
                frame = bytes.fromhex(fields[1])
            except ValueError:
                self._reply("@ERR BAD_HEX")
                return
            answer = None
            if (self.baud, self.fmt) == (self.meter.baud, self.meter.fmt):
                answer = self.meter.handle(frame)
            self._reply(f"@RX {answer.hex().upper()}" if answer else "@TIMEOUT")
        else:
            self._reply("@ERR UNKNOWN_COMMAND")


def make(meter=None, **bridge_args):
    meter = meter or SimulatedDDSU666(seed=1)
    fake = FakeBridge(meter, **bridge_args)
    log = []
    transport = BridgeTransport("COM99", log=lambda kind, text: log.append((kind, text)),
                                serial_factory=lambda: fake)
    return meter, fake, transport, log


class BridgeTests(unittest.TestCase):
    def test_open_does_not_reset_and_skips_boot_noise(self):
        _, fake, transport, log = make()
        description = transport.open()
        self.assertEqual(fake.lines_at_open, (False, False))
        self.assertIn("TX16 RX17", description)
        self.assertEqual(fake.commands[-1], "CFG 9600 8N1")
        self.assertTrue(any(kind == "dev" and "boot" in text for kind, text in log))

    def test_slow_boot_is_retried(self):
        _, fake, transport, _ = make(deaf_pings=2)
        transport.open()
        self.assertEqual(sum(1 for command in fake.commands if command == "PING"), 3)

    def test_no_firmware(self):
        _, fake, transport, _ = make(boot_noise=False, deaf_pings=99)
        clock = [0.0]

        def fast_clock():  # skip the 5 s handshake window
            clock[0] += 0.5
            return clock[0]

        with mock.patch("time.monotonic", fast_clock):
            with self.assertRaises(TransportError) as caught:
                transport.open()
        self.assertIn("firmware", str(caught.exception))
        self.assertFalse(fake.is_open)

    def test_transact_and_timeout(self):
        meter, fake, transport, _ = make()
        transport.open()
        request = read_request(1, 0x0006, 1)
        answer = transport.transact(request, 300, 7)
        self.assertEqual(answer[:5], bytes.fromhex("01 03 02 00 01"))
        self.assertTrue(fake.commands[-1].startswith(f"TX {request.hex().upper()} 300 7"))
        transport.configure(4800, "8N1")
        self.assertEqual((fake.baud, fake.fmt), (4800, "8N1"))
        self.assertEqual(transport.transact(request, 300, 7), b"")

    def test_reboot_mid_session_restores_line_settings(self):
        meter = SimulatedDDSU666(baud_code=2, fmt="8E1", seed=1)
        _, fake, transport, log = make(meter)
        transport.configure(4800, "8E1")
        transport.open()
        fake.reboot_before = "TX"
        answer = transport.transact(read_request(1, 0x0006, 1), 300, 7)
        self.assertEqual(answer[:5], bytes.fromhex("01 03 02 00 01"))
        self.assertEqual((fake.baud, fake.fmt), (4800, "8E1"))
        self.assertTrue(any("khởi động lại" in text for kind, text in log if kind == "warn"))

    def test_error_reply(self):
        _, fake, transport, _ = make()
        transport.open()
        fake.error_next = "BAD_HEX"
        with self.assertRaises(TransportError) as caught:
            transport.transact(read_request(1, 0x0006, 1), 300, 7)
        self.assertNotIsInstance(caught.exception, LinkLost)
        self.assertIn("BAD_HEX", str(caught.exception))
        self.assertEqual(len(transport.transact(read_request(1, 0x0006, 1), 300, 7)), 7)

    def test_unplugged(self):
        _, fake, transport, _ = make()
        transport.open()
        fake.unplug = True
        with self.assertRaises(LinkLost):
            transport.transact(read_request(1, 0x0006, 1), 300, 7)

    def test_full_client_over_bridge(self):
        meter, fake, transport, _ = make()
        transport.open()
        client = DDSU666Client(transport)
        readings = client.read_measurements()
        self.assertTrue(all(reading.ok for reading in readings))
        client.change_baud(1)
        self.assertEqual((meter.baud, fake.baud), (2400, 2400))
        client.change_address(17)
        self.assertEqual(client.read_registers(R.ADDR.address, 1), [17])


if __name__ == "__main__":
    unittest.main()
