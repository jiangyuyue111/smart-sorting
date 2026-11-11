"""单件抓放序列。生成器是纯函数，可以逐条断言。"""

import unittest
from pathlib import Path

from sorting import config as cfgmod
from sorting.config import Target
from sorting.core.sequence import expected_duration_s, run_item, trace

TOML = Path(__file__).resolve().parent.parent / "config" / "machine.toml"


def make_target(cfg, width: float = 20.0) -> Target:
    return Target(1, "红色圆形", "1", (100.0, 60.0), cfg, width_mm=width)


class TestHappyPath(unittest.TestCase):
    def setUp(self):
        self.cfg = cfgmod.load(TOML)
        self.t = make_target(self.cfg)
        self.actions, self.ok = trace(self.t, iter([20.0]))

    def test_result_is_placed(self):
        self.assertTrue(self.ok)

    def test_order(self):
        kinds = [a.kind for a in self.actions]
        self.assertEqual(kinds, ["grip", "goto", "z", "grip", "settle", "check",
                                 "z", "goto", "z", "grip", "settle", "z", "display"])

    def test_opens_before_moving(self):
        """必须先在安全高度张开，否则带件横移会刮到别的工件。"""
        self.assertEqual(self.actions[0].kind, "grip")
        self.assertEqual(self.actions[0].args[0], self.cfg.grip_open_mm)
        self.assertEqual(self.actions[1].kind, "goto")

    def test_goes_up_before_travelling(self):
        """check 之后第一件事必须是抬起，否则会带着件横移到料盒时刮到工件。"""
        kinds = [a.kind for a in self.actions]
        i = kinds.index("check")
        self.assertEqual(kinds[i + 1], "z")
        self.assertEqual(self.actions[i + 1].args[0], self.cfg.z_safe_mm)

    def test_reaches_bin(self):
        gotos = [a.args for a in self.actions if a.kind == "goto"]
        self.assertEqual(gotos[-1], self.t.bin_xy)


class TestSkipPath(unittest.TestCase):
    def setUp(self):
        self.cfg = cfgmod.load(TOML)
        self.t = make_target(self.cfg)
        self.actions, self.ok = trace(self.t, iter([self.cfg.grip_close_mm]))
        self.kinds = [a.kind for a in self.actions]

    def test_result_is_skip(self):
        self.assertFalse(self.ok)

    def test_never_goes_to_bin(self):
        gotos = [a.args for a in self.actions if a.kind == "goto"]
        self.assertEqual(len(gotos), 1)
        self.assertNotIn(self.t.bin_xy, gotos)

    def test_still_refreshes_screen(self):
        self.assertEqual(self.kinds[-1], "display")

    def test_lifts_before_giving_up(self):
        i = self.kinds.index("check")
        self.assertEqual(self.kinds[i + 1], "z")
        self.assertEqual(self.actions[i + 1].args[0], self.cfg.z_safe_mm)


class TestDuration(unittest.TestCase):
    def test_positive(self):
        cfg = cfgmod.load(TOML)
        t = make_target(cfg)
        actions, _ = trace(t, iter([20.0]))
        self.assertGreater(expected_duration_s(actions, t), 0.0)

    def test_matches_axis_speeds(self):
        """解析节拍应该等于各轴行程/速度之和，不含任何魔法常数。"""
        cfg = cfgmod.load(TOML)
        t = make_target(cfg)
        actions, _ = trace(t, iter([20.0]))
        want = 0.0
        want += max(cfg.x.travel_s(t.pick_xy[0]), cfg.y.travel_s(t.pick_xy[1]))
        want += cfg.z.travel_s(cfg.z_pick_mm)                     # 回零位 Z=0 -> 取件高度
        want += cfg.z.travel_s(cfg.z_safe_mm - cfg.z_pick_mm)
        want += cfg.grip.travel_s(cfg.grip_open_mm - cfg.grip_close_mm)
        want += cfg.grip.travel_s(cfg.grip_close_mm - cfg.grip_open_mm)
        want += 2 * cfg.grip_settle_s
        want += max(cfg.x.travel_s(t.bin_xy[0] - t.pick_xy[0]),
                    cfg.y.travel_s(t.bin_xy[1] - t.pick_xy[1]))
        want += 2 * cfg.z.travel_s(cfg.z_safe_mm - cfg.z_drop_mm)
        got = expected_duration_s(actions, t)
        self.assertAlmostEqual(got, want, places=3)


class TestGenerator(unittest.TestCase):
    def test_check_feeds_into_return_value(self):
        cfg = cfgmod.load(TOML)
        t = make_target(cfg)
        gen = run_item(t)
        action = next(gen)
        try:
            while True:
                action = gen.send(30.0 if action.kind == "check" else None)
        except StopIteration as stop:
            self.assertTrue(stop.value)


if __name__ == "__main__":
    unittest.main()
