"""链路层：MockLink 的解析时间轴和夹爪堵转。不需要板子。"""

import unittest
from pathlib import Path

from sorting import config as cfgmod
from sorting.core import protocol as proto
from sorting.core.link import LinkTimeout, McuFault, MockLink, Session

TOML = Path(__file__).resolve().parent.parent / "config" / "machine.toml"


def mock(cfg=None) -> MockLink:
    return MockLink(axes=cfg.axes if cfg else None)


class TestEnvelopes(unittest.TestCase):
    def setUp(self):
        self.link = mock()
        self.s = Session(self.link, ack_timeout_s=0.5, done_timeout_s=1.0)

    def test_ping(self):
        self.s.ping()

    def test_goto_ok_then_done(self):
        self.s.command(proto.Goto(30.0, 40.0))

    def test_home(self):
        self.s.command(proto.Home())

    def test_bad_line_becomes_fault_message(self):
        self.link.send("THIS IS NOT A COMMAND")
        msg = self.link.poll(0.5)
        self.assertIsInstance(msg, proto.Fault)
        self.assertEqual(msg.code, proto.ErrorCode.UNKNOWN_COMMAND)

    def test_timeout_on_silent_link(self):
        class Silent(MockLink):
            def send(self, line):  # 装作没插线
                pass

        s = Session(Silent(), ack_timeout_s=0.05, done_timeout_s=0.1)
        with self.assertRaises(LinkTimeout):
            s.command(proto.Goto(1.0, 1.0))

    def test_fault_raises_mcu_fault(self):
        self.link.send("garbage!!")
        with self.assertRaises(McuFault):
            self.s.command(proto.Ping())


class TestAnalyticClock(unittest.TestCase):
    """MockLink 报告的是解析时间，不是墙上时间——报告里的节拍数字靠它。"""

    def setUp(self):
        self.cfg = cfgmod.load(TOML)
        self.link = mock(self.cfg)

    def test_goto_takes_max_of_xy_not_sum(self):
        # X 走 200mm / 200mm·s⁻¹ = 1.0s，Y 走 40mm = 0.2s，联动应取 1.0s
        self.link.send(proto.Goto(200.0, 40.0).to_line())
        self.assertAlmostEqual(self.link.elapsed_s, 1.0, places=3)

    def test_z_uses_its_own_speed(self):
        self.link.send(proto.ZMove(10.0).to_line())
        self.assertAlmostEqual(self.link.elapsed_s, 10.0 / self.cfg.z.speed_mm_s, places=3)

    def test_homing_counts(self):
        self.link.send(proto.Home().to_line())
        self.assertAlmostEqual(self.link.elapsed_s, self.link.homing_s, places=3)

    def test_advance_adds_idle(self):
        self.link.advance(0.5)
        self.assertAlmostEqual(self.link.elapsed_s, 0.5, places=3)

    def test_motions_recorded(self):
        self.link.send(proto.Goto(100.0, 50.0).to_line())
        acts = {m.axis for m in self.link.log.motions}
        self.assertEqual(acts, {"x", "y"})
        for m in self.link.log.motions:
            self.assertGreaterEqual(m.duration_s, 0.0)


class TestGripStall(unittest.TestCase):
    """空夹合到底，夹到件就堵转在工件宽度处——这是判断有没有夹到的唯一依据。"""

    def setUp(self):
        self.cfg = cfgmod.load(TOML)
        self.link = mock(self.cfg)
        self.s = Session(self.link, ack_timeout_s=0.5, done_timeout_s=1.0,
                         item_timeout_s=1.0)

    def _open(self):
        self.assertAlmostEqual(self.s.grip(self.cfg.grip_open_mm),
                               self.cfg.grip_open_mm, places=3)

    def test_empty_close_reaches_target(self):
        self._open()
        self.assertAlmostEqual(self.s.grip(self.cfg.grip_close_mm),
                               self.cfg.grip_close_mm, places=3)

    def test_workpiece_stops_the_jaw(self):
        self._open()
        self.link.blocker_mm = 18.0
        self.assertAlmostEqual(self.s.grip(self.cfg.grip_close_mm), 18.0, places=3)

    def test_stall_position_decides_held(self):
        self._open()
        self.link.blocker_mm = 18.0
        held = self.s.grip(self.cfg.grip_close_mm) >= self.cfg.grip_hold_min_mm
        self.assertTrue(held)

        self.link.blocker_mm = None
        held = self.s.grip(self.cfg.grip_close_mm) >= self.cfg.grip_hold_min_mm
        self.assertFalse(held)

    def test_grip_report_is_logged(self):
        self._open()
        self.link.blocker_mm = 18.0
        self.s.grip(self.cfg.grip_close_mm)
        self.assertEqual(len(self.link.log.grips), 2)
        self.assertTrue(self.link.log.grips[-1]["stalled"])


if __name__ == "__main__":
    unittest.main()
