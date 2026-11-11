"""配置与齿轮传动的换算。所有数字都从 machine.toml 推，测试不写死实例值。"""

import math
import unittest
from pathlib import Path

from sorting import config as cfgmod
from sorting.config import Config

TOML = Path(__file__).resolve().parent.parent / "config" / "machine.toml"


def load() -> Config:
    return cfgmod.load(TOML)


class TestGearMath(unittest.TestCase):
    """d = m·z，L = π·d/i，S = 整步数 × 细分 / L。"""

    def test_pitch_diameter(self):
        for ax in load().axes.values():
            self.assertAlmostEqual(ax.pitch_dia_mm, ax.module_mm * ax.teeth)

    def test_mm_per_rev_follows_pitch_circumference(self):
        for ax in load().axes.values():
            self.assertAlmostEqual(
                ax.mm_per_rev, math.pi * ax.pitch_dia_mm / ax.reduction
            )

    def test_steps_per_mm(self):
        cfg = load()
        x = cfg.x
        want = x.steps_per_rev * x.microstep / x.mm_per_rev
        self.assertAlmostEqual(x.steps_per_mm, want)
        # 200 步/圈 × 16 细分，m=1 z=20 i=1 -> 3200 / 62.83 = 50.93 步/mm
        self.assertAlmostEqual(x.steps_per_mm, 3200 / (math.pi * 20.0))

    def test_mm_to_steps_rounds(self):
        cfg = load()
        self.assertEqual(cfg.x.mm_to_steps(10.0), round(10.0 * cfg.x.steps_per_mm))
        self.assertEqual(cfg.x.mm_to_steps(-10.0), -round(10.0 * cfg.x.steps_per_mm))

    def test_travel_time(self):
        cfg = load()
        self.assertAlmostEqual(cfg.x.travel_s(100.0), 100.0 / cfg.x.speed_mm_s)
        self.assertAlmostEqual(cfg.x.travel_s(-100.0), 100.0 / cfg.x.speed_mm_s)

    def test_grip_reduction_makes_steps_denser(self):
        """夹爪多一级减速，同样行程下每毫米步数应该更多。"""
        cfg = load()
        if cfg.grip.reduction > 1.0 and cfg.grip.teeth == cfg.z.teeth:
            self.assertGreater(cfg.grip.steps_per_mm, cfg.z.steps_per_mm)


class TestBinsAndTray(unittest.TestCase):

    def test_six_bins(self):
        cfg = load()
        self.assertEqual(sorted(cfg.bins), ["1", "2", "3", "4", "5", "6"])

    def test_unknown_bin(self):
        with self.assertRaises(cfgmod.ConfigError):
            load().bin_xy("9")

    def test_tray_to_machine_offset(self):
        cfg = load()
        self.assertEqual(cfg.tray_to_machine(0.0, 0.0), (cfg.tray_x0_mm, cfg.tray_y0_mm))
        self.assertEqual(cfg.tray_to_machine(10.0, 20.0),
                         (cfg.tray_x0_mm + 10.0, cfg.tray_y0_mm + 20.0))


class TestValidate(unittest.TestCase):

    def _bad(self, **kw) -> Config:
        return Config(**{**load().__dict__, **kw})

    def test_z_safe_must_be_above_pick(self):
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.validate(self._bad(z_safe_mm=1.0))

    def test_z_pick_inside_travel(self):
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.validate(self._bad(z_pick_mm=-5.0))

    def test_grip_ordering(self):
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.validate(self._bad(grip_close_mm=20.0, grip_hold_min_mm=12.0))

    def test_bin_out_of_range(self):
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.validate(self._bad(bins={"1": (999.0, 10.0)}))

    def test_tray_must_fit_travel(self):
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.validate(self._bad(tray_x0_mm=0.0, tray_w_mm=999.0))

    def test_no_bins(self):
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.validate(self._bad(bins={}))


class TestCheckInRange(unittest.TestCase):

    def test_ok(self):
        cfg = load()
        cfgmod.check_in_range(cfg, 0.0, 0.0)
        cfgmod.check_in_range(cfg, cfg.x.max_mm, cfg.y.max_mm)

    def test_out(self):
        cfg = load()
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.check_in_range(cfg, cfg.x.max_mm + 1.0, 0.0)


class TestMeta(unittest.TestCase):

    def test_pending_list_is_declared(self):
        """未标定时必须给出待标定清单，否则报告会静默地把假定值当实测值。"""
        cfg = load()
        if not cfg.meta.calibrated:
            self.assertTrue(cfg.meta.pending)
            self.assertEqual(cfg.meta.qualifier, "解析估算")

    def test_qualifier_switches(self):
        self.assertEqual(cfgmod.Meta(calibrated=True).qualifier, "实测")
        self.assertEqual(cfgmod.Meta(calibrated=False).qualifier, "解析估算")


if __name__ == "__main__":
    unittest.main()
