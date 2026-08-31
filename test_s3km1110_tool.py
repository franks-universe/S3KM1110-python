import unittest

from s3km1110_tool import BinaryReportParser, S3KM1110Parser
from s3km1110_debug_plot import DebugParser, mirror_bin


class ParserTests(unittest.TestCase):
    def test_real_capture_sample(self):
        data = b"ON\r\nRange 148\r\nON\r\nRange 26\r\n"
        reports = S3KM1110Parser().feed(data, timestamp=100.25)
        self.assertEqual([r.range_value for r in reports], [148, 26])
        self.assertTrue(all(r.presence for r in reports))

    def test_fragmented_lines(self):
        parser = S3KM1110Parser()
        self.assertEqual(parser.feed(b"ON\r\nRan"), [])
        report = parser.feed(b"ge 15\r\n", timestamp=2.0)[0]
        self.assertEqual(report.range_value, 15)

    def test_off_report(self):
        report = S3KM1110Parser().feed(b"OFF\r\n", timestamp=3.0)[0]
        self.assertFalse(report.presence)
        self.assertIsNone(report.range_value)

    def test_unknown_line_is_counted(self):
        parser = S3KM1110Parser()
        self.assertEqual(parser.feed(b"unexpected\r\n"), [])
        self.assertEqual(parser.invalid_lines, 1)

    def test_real_binary_report(self):
        frame = bytes.fromhex(
            "f4 f3 f2 f1 23 00 01 4f 00 "
            "d1 83 35 44 f4 5a 3a 01 0d 01 05 01 65 00 59 00 "
            "35 00 28 00 1a 00 24 00 14 00 20 00 25 00 1a 00 "
            "f8 f7 f6 f5"
        )
        report = BinaryReportParser().feed(frame, timestamp=4.0)[0]
        self.assertTrue(report.presence)
        self.assertEqual(report.range_value, 79)
        self.assertEqual(
            report.gate_energies,
            (33745, 17461, 23284, 314, 269, 261, 101, 89,
             53, 40, 26, 36, 20, 32, 37, 26),
        )

    def test_debug_frame(self):
        values = tuple(range(320))
        import struct
        raw = bytes.fromhex("AA BF 10 14") + struct.pack("<320I", *values) + bytes.fromhex("FD FC FB FA")
        parser = DebugParser()
        self.assertEqual(parser.feed(raw[:517]), [])
        frame = parser.feed(raw[517:])[0]
        self.assertEqual(frame.values, values)
        self.assertEqual(mirror_bin(176), 144)


if __name__ == "__main__":
    unittest.main()
