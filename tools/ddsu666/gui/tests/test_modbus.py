import unittest

from ddsu666.modbus import (ExceptionResponse, FrameError, NoResponse, crc16, crc_ok, expected_length,
                            float_to_words, frame_complete, parse_int, parse_read_response, parse_write_response,
                            read_request, strip_echo, to_int16, to_word, trim_frame, with_crc, words_to_float,
                            write_request)


def h(text: str) -> bytes:
    return bytes.fromhex(text)


class ManualExamples(unittest.TestCase):
    """Frames printed in appendix A of the DDSU666 manual (tables A.3 to A.6)."""

    def test_read_request_a3(self):
        self.assertEqual(read_request(1, 0x000C, 2), h("01 03 00 0C 00 02 04 08"))

    def test_read_response_a4(self):
        response = h("01 03 04 00 00 13 88 F7 65")
        self.assertTrue(crc_ok(response))
        self.assertEqual(parse_read_response(read_request(1, 0x000C, 2), response), [0x0000, 0x1388])

    def test_write_request_a5(self):
        self.assertEqual(write_request(1, 0x0000, [0x0002, 0x1388, 0x000A]),
                         h("01 10 00 00 00 03 06 00 02 13 88 00 0A 9B E9"))

    def test_write_response_a6(self):
        request = write_request(1, 0x0000, [0x0002, 0x1388, 0x000A])
        parse_write_response(request, h("01 10 00 00 00 03 80 08"))

    def test_crc_byte_order_is_low_first(self):
        self.assertEqual(crc16(h("01 03 00 0C 00 02")), 0x0804)


class Framing(unittest.TestCase):
    def setUp(self):
        self.request = read_request(1, 0x2000, 2)

    def test_exception_reply(self):
        reply = with_crc(h("01 83 02"))
        with self.assertRaises(ExceptionResponse) as caught:
            parse_read_response(self.request, reply)
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("02H", str(caught.exception))

    def test_bad_crc(self):
        reply = bytearray(with_crc(h("01 03 04 43 66 00 00")))
        reply[-1] ^= 0xFF
        with self.assertRaises(FrameError):
            parse_read_response(self.request, bytes(reply))

    def test_wrong_slave(self):
        with self.assertRaises(FrameError):
            parse_read_response(self.request, with_crc(h("02 03 04 43 66 00 00")))

    def test_wrong_byte_count(self):
        with self.assertRaises(FrameError):
            parse_read_response(self.request, with_crc(h("01 03 02 43 66")))

    def test_empty_is_no_response(self):
        with self.assertRaises(NoResponse):
            parse_read_response(self.request, b"")

    def test_write_echo_mismatch(self):
        request = write_request(1, 0x0006, [5])
        with self.assertRaises(FrameError):
            parse_write_response(request, with_crc(h("01 10 00 07 00 01")))

    def test_expected_length(self):
        self.assertEqual(expected_length(read_request(1, 0x2000, 18)), 41)
        self.assertEqual(expected_length(write_request(1, 6, [3])), 8)
        self.assertEqual(expected_length(with_crc(h("01 08 00 00"))), 0)

    def test_frame_complete(self):
        self.assertTrue(frame_complete(with_crc(h("01 83 02")), 41))
        self.assertFalse(frame_complete(h("01 03 04 43"), 9))
        self.assertTrue(frame_complete(bytes(9), 9))
        self.assertFalse(frame_complete(bytes(9), 0))

    def test_strip_echo_and_trim(self):
        reply = with_crc(h("01 03 04 43 66 00 00"))
        self.assertEqual(strip_echo(self.request, self.request + reply), reply)
        self.assertEqual(strip_echo(self.request, reply), reply)
        self.assertEqual(trim_frame(self.request, reply + b"\x00\xff"), reply)
        exception = with_crc(h("01 83 02"))
        self.assertEqual(trim_frame(self.request, exception + b"\x00"), exception)
        # A trailing 00H makes the longer buffer pass the CRC test too; length must win.
        self.assertTrue(crc_ok(reply + b"\x00"))
        self.assertEqual(trim_frame(self.request, reply + b"\x00"), reply)

    def test_argument_checks(self):
        for bad in ((0, 0, 1), (248, 0, 1), (1, 0, 0), (1, 0, 126), (1, 0xFFFF, 2)):
            with self.assertRaises(ValueError):
                read_request(*bad)
        with self.assertRaises(ValueError):
            write_request(1, 0, [])
        with self.assertRaises(ValueError):
            write_request(1, 0, [70000])


class Values(unittest.TestCase):
    def test_float_word_orders(self):
        self.assertEqual(float_to_words(230.0), (0x4366, 0x0000))
        self.assertEqual(words_to_float((0x4366, 0x0000)), 230.0)
        self.assertEqual(words_to_float((0x0000, 0x4366), "CDAB"), 230.0)
        self.assertEqual(words_to_float(float_to_words(-1.5, "CDAB"), "CDAB"), -1.5)

    def test_int16(self):
        self.assertEqual(to_int16(0xFFFF), -1)
        self.assertEqual(to_int16(0x7FFF), 32767)
        self.assertEqual(to_word(-1), 0xFFFF)
        self.assertEqual(to_word(65535), 0xFFFF)

    def test_parse_int(self):
        self.assertEqual(parse_int("8192"), 8192)
        self.assertEqual(parse_int("0x2000"), 0x2000)
        self.assertEqual(parse_int("2000H"), 0x2000)
        self.assertEqual(parse_int(" 400ah "), 0x400A)
        self.assertEqual(parse_int("-5"), -5)
        with self.assertRaises(ValueError):
            parse_int("")
        with self.assertRaises(ValueError):
            parse_int("12x")


if __name__ == "__main__":
    unittest.main()
