"""UART 行协议的编解码。"""

import unittest

from sorting.core import protocol as proto


class TestEncode(unittest.TestCase):
    def test_commands(self):
        self.assertEqual(proto.Goto(30.0, 40.5).to_line(), "GOTO 30.0 40.5")
        self.assertEqual(proto.ZMove(5.0).to_line(), "Z 5.0")
        self.assertEqual(proto.Grip(6.0).to_line(), "GRIP 6.0")
        self.assertEqual(proto.Home().to_line(), "HOME")
        self.assertEqual(proto.Stop().to_line(), "STOP")
        self.assertEqual(proto.Ping().to_line(), "PING")

    def test_replies(self):
        self.assertEqual(proto.encode(proto.OK), "OK")
        self.assertEqual(proto.encode(proto.DONE), "DONE")
        self.assertEqual(proto.encode(proto.PONG), "PONG")
        self.assertEqual(proto.encode(proto.GripReport(12.5)), "GRIPD 12.5")
        self.assertEqual(
            proto.encode(proto.Fault(proto.ErrorCode.OUT_OF_RANGE, "x=300")),
            "ERR E01 x=300",
        )


class TestParse(unittest.TestCase):
    def test_round_trip(self):
        for msg in (proto.Goto(12.5, 7.0), proto.ZMove(40.0), proto.Grip(6.0),
                    proto.Home(), proto.Stop(), proto.Ping()):
            self.assertEqual(proto.parse(msg.to_line()), msg)

    def test_acks(self):
        for line, kind in (("OK", "OK"), ("done", "DONE"), ("Pong", "PONG")):
            self.assertEqual(proto.parse(line), proto.Ack(kind))

    def test_grip_report(self):
        self.assertEqual(proto.parse("GRIPD 12.5"), proto.GripReport(12.5))
        self.assertEqual(proto.parse("gripd 0"), proto.GripReport(0.0))

    def test_fault(self):
        f = proto.parse("ERR E02 limit sw not found")
        self.assertEqual(f.code, proto.ErrorCode.LIMIT_NOT_FOUND)
        self.assertEqual(f.text, "limit sw not found")

    def test_fault_without_text(self):
        f = proto.parse("ERR E04")
        self.assertEqual(f.code, proto.ErrorCode.GRIP_TIMEOUT)
        self.assertEqual(f.text, "")

    def test_tolerates_noise(self):
        # 旧协议的 VAC / PSW 已经不存在了，必须当作未知指令拒掉
        for bad in ("", "   ", "OK extra", "REBOOT", "GOTO 1.0", "VAC 1", "PSW 1",
                    "GRIP", "GRIPD", "ERR E99 x", "GOTO a b"):
            with self.assertRaises(proto.ProtocolError, msg=bad):
                proto.parse(bad)

    def test_whitespace_and_case(self):
        self.assertEqual(proto.parse("  goto 1.0 2.0 \r\n"), proto.Goto(1.0, 2.0))


class TestErrorCodes(unittest.TestCase):
    def test_all_four(self):
        self.assertEqual({c.value for c in proto.ErrorCode},
                         {"E01", "E02", "E03", "E04"})


if __name__ == "__main__":
    unittest.main()
