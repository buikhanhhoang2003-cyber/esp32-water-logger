import threading
import unittest

from ddsu666 import registers as R
from ddsu666.client import DDSU666Client, combined_energy
from ddsu666.modbus import ExceptionResponse, ModbusError, NoResponse, with_crc
from ddsu666.simulator import SimulatedDDSU666
from ddsu666.transport import SimTransport


def connect(meter=None, baud=9600, fmt="8N1", **client_args):
    meter = meter or SimulatedDDSU666(seed=1)
    transport = SimTransport(meter, baud, fmt, realtime=False)
    transport.open()
    client_args.setdefault("retries", 1)
    log = []
    client = DDSU666Client(transport, log=lambda kind, text: log.append((kind, text)), **client_args)
    return meter, transport, client, log


class CountingTransport(SimTransport):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.requests = []

    def transact(self, request, timeout_ms, expect_len):
        self.requests.append(request)
        return super().transact(request, timeout_ms, expect_len)


class EchoTransport(SimTransport):
    """A transceiver that hands our own frame back in front of the answer."""

    def transact(self, request, timeout_ms, expect_len):
        reply = super().transact(request, timeout_ms, expect_len)
        return request + reply if reply else request


class ReadTests(unittest.TestCase):
    def test_measurements(self):
        meter, _, client, _ = connect()
        readings = {reading.register.address: reading for reading in client.read_measurements()}
        self.assertEqual(len(readings), len(R.MEASUREMENTS) + len(R.ENERGY))
        self.assertTrue(all(reading.ok for reading in readings.values()))
        self.assertAlmostEqual(readings[R.VOLTAGE.address].value, 230, delta=3)
        self.assertAlmostEqual(readings[R.FREQUENCY.address].value, 50, delta=0.1)
        self.assertAlmostEqual(readings[R.EXPORT_ENERGY.address].value, 0.37, places=5)
        voltage = readings[R.VOLTAGE.address]
        self.assertEqual(len(voltage.words), 2)
        total = combined_energy(readings.values())
        self.assertAlmostEqual(total, readings[R.IMPORT_ENERGY.address].value + 0.37, places=4)

    def test_block_fallback_is_remembered(self):
        meter = SimulatedDDSU666(seed=1)
        transport = CountingTransport(meter, realtime=False)
        transport.open()
        client = DDSU666Client(transport)
        client.read_measurements()
        # 2000H block, refused 4000H block, then Ep and -Ep one by one.
        self.assertEqual(len(transport.requests), 4)
        transport.requests.clear()
        client.read_measurements()
        self.assertEqual(len(transport.requests), 3)

    def test_parameters(self):
        _, _, client, _ = connect()
        readings = {reading.register.code: reading for reading in client.read_parameters()
                    if not reading.register.reserved}
        self.assertEqual(readings["Addr"].value, 1)
        self.assertEqual(readings["BAud"].value, 3)
        self.assertEqual(readings["ChangeProtocol"].value, 2)
        self.assertEqual(readings["ClrE"].value, 0)

    def test_parameters_when_reserved_rows_are_unreadable(self):
        meter = SimulatedDDSU666(readable_reserved=False)
        _, _, client, log = connect(meter)
        readings = client.read_parameters()
        for reading in readings:
            if reading.register.reserved:
                self.assertIsNotNone(reading.error)
            else:
                self.assertTrue(reading.ok, reading.register.code)
        self.assertTrue(any("từng thông số" in text for kind, text in log if kind == "info"))

    def test_word_order(self):
        meter = SimulatedDDSU666(word_order="CDAB", seed=1)
        _, _, client, _ = connect(meter, word_order="CDAB")
        readings = {reading.register.address: reading for reading in client.read_measurements()}
        self.assertAlmostEqual(readings[R.VOLTAGE.address].value, 230, delta=3)
        client.word_order = "ABCD"
        readings = {reading.register.address: reading for reading in client.read_measurements()}
        self.assertNotAlmostEqual(readings[R.VOLTAGE.address].value, 230, delta=3)

    def test_wrong_line_settings_time_out(self):
        _, _, client, log = connect(baud=4800)
        with self.assertRaises(NoResponse) as caught:
            client.read_registers(0x0006, 1)
        self.assertIn("4800", str(caught.exception))
        self.assertEqual(sum(1 for kind, _ in log if kind == "tx"), 2)  # one try + one retry

    def test_undocumented_address(self):
        _, _, client, _ = connect()
        with self.assertRaises(ExceptionResponse) as caught:
            client.read_registers(0x3000, 1)
        self.assertEqual(caught.exception.code, 2)

    def test_echoing_transceiver(self):
        meter = SimulatedDDSU666(seed=1)
        transport = EchoTransport(meter, realtime=False)
        transport.open()
        client = DDSU666Client(transport)
        self.assertEqual(client.read_registers(0x0006, 1), [1])

    def test_raw_exchange(self):
        _, _, client, _ = connect()
        request, answer = client.raw_exchange(bytes.fromhex("01 03 00 0C 00 01"))
        self.assertEqual(request[-2:], with_crc(request[:-2])[-2:])
        self.assertEqual(answer, with_crc(bytes.fromhex("01 03 02 00 03")))


class WriteTests(unittest.TestCase):
    def test_ucode(self):
        meter, _, client, _ = connect()
        client.write_parameter(R.UCODE, 1234)
        self.assertEqual(meter.ucode, 1234)
        with self.assertRaises(ValueError):
            client.write_parameter(R.REV, 1)

    def test_clear_energy(self):
        meter, _, client, _ = connect()
        client.clear_energy()
        values = [reading.value for reading in client.read_block(R.ENERGY_BLOCK)]
        self.assertLess(values[0], 0.01)
        self.assertEqual(values[1], 0.0)

    def test_change_address(self):
        meter, _, client, _ = connect()
        message = client.change_address(42)
        self.assertEqual((meter.address, client.slave), (42, 42))
        self.assertIn("42", message)

    def test_change_address_meter_answers_from_new_address(self):
        class EagerMeter(SimulatedDDSU666):
            def handle(self, frame):
                reply = super().handle(frame)
                # Some firmware switches before answering: the reply carries the new address.
                return reply if reply is None else with_crc(bytes((self.address,)) + reply[1:-2])

        meter = EagerMeter(seed=1)
        _, _, client, log = connect(meter)
        message = client.change_address(9)
        self.assertEqual((meter.address, client.slave), (9, 9))
        self.assertIn("không nhận được xác nhận", message)

    def test_change_address_refused(self):
        class StubbornMeter(SimulatedDDSU666):
            @staticmethod
            def _check_write(address, value):
                return 0x03 if address == R.ADDR.address else SimulatedDDSU666._check_write(address, value)

        meter, _, client, _ = connect(StubbornMeter(seed=1))
        with self.assertRaises(ExceptionResponse):
            client.change_address(9)
        self.assertEqual((meter.address, client.slave), (1, 1))

    def test_change_baud(self):
        meter, transport, client, _ = connect()
        message = client.change_baud(2)
        self.assertEqual((meter.baud, transport.baud), (4800, 4800))
        self.assertIn("4800", message)
        self.assertEqual(client.read_registers(R.BAUD.address, 1), [2])

    def test_change_baud_ignored_by_meter(self):
        class DeafMeter(SimulatedDDSU666):
            def _write(self, address, value):
                if address != R.BAUD.address:
                    super()._write(address, value)

        meter, transport, client, _ = connect(DeafMeter(seed=1))
        with self.assertRaises(ModbusError) as caught:
            client.change_baud(1)
        self.assertIn("9600", str(caught.exception))
        self.assertEqual(transport.baud, 9600)

    def test_settings_writes_need_a_reachable_meter(self):
        meter, transport, client, _ = connect(baud=4800)  # meter is at 9600: nothing answers
        for action in (lambda: client.change_address(5), lambda: client.change_baud(1),
                       lambda: client.set_protocol(1)):
            with self.assertRaises(NoResponse):
                action()
        self.assertEqual((meter.address, meter.baud, meter.protocol), (1, 9600, 2))
        self.assertEqual((client.slave, transport.baud), (1, 4800))

    def test_protocol_switch(self):
        meter, _, client, _ = connect()
        self.assertIn("Modbus RTU", client.set_protocol(2))
        message = client.set_protocol(1)
        self.assertEqual(meter.protocol, 1)
        self.assertIn("DL/T 645", message)
        with self.assertRaises(NoResponse):
            client.read_registers(0x0006, 1)


class ScanTests(unittest.TestCase):
    def test_finds_meter(self):
        meter = SimulatedDDSU666(address=37, baud_code=2, fmt="8E1", seed=1)
        _, transport, client, _ = connect(meter)
        seen = []
        hits = client.scan([9600, 4800], ["8N1", "8E1"], range(30, 40), 50, threading.Event(),
                           progress=lambda *state: seen.append(state))
        self.assertEqual(len(hits), 1)
        hit = hits[0]
        self.assertEqual((hit.baud, hit.fmt, hit.address, hit.outcome), (4800, "8E1", 37, "ok"))
        self.assertEqual(hit.rev, meter.rev)
        self.assertEqual((transport.baud, transport.fmt), (9600, "8N1"))  # restored
        self.assertEqual(seen[-1], (37, 4800, "8E1", 37))

    def test_cancel(self):
        _, _, client, _ = connect(SimulatedDDSU666(address=200))
        cancel = threading.Event()
        cancel.set()
        self.assertEqual(client.scan([9600], ["8N1"], range(1, 248), 50, cancel), [])


if __name__ == "__main__":
    unittest.main()
