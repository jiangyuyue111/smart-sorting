"""工作台：界面背后的那几条换算不许出错。

这套测试只碰纯逻辑——不起服务、不开浏览器。
    RecordingLink   任意链路 -> 时间轴，是"预演"和"实机"共用动画的基础
    apply_overrides 界面旋钮 -> Config，改坏了必须立刻报错而不是默默算错
    各类 payload    界面拿到的数是能从代码里复算出来的

最要紧的一条是 `test_timeline_matches_report_clock`：动画时间轴和报告里的
节拍必须**是同一个数**。如果它俩能分开，那"看着在动"就变成了另一套故事，
汇报时说的一切都失去意义。
"""

import unittest
from pathlib import Path

from sorting import config as cfgmod
from sorting.app import studio
from sorting.core import jobqueue
from sorting.core import protocol as proto
from sorting.core.link import Link, MockLink
from sorting.vision import classes as vclasses
from sorting.vision import detect as vdetect
from sorting.vision import synthetic

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
TOML = CONFIG_DIR / "machine.toml"
CLASSES = CONFIG_DIR / "classes.json"


def _scene(cfg, book, seed=0, per_class=2):
    sc = synthetic.build(book, seed=seed, per_class=per_class,
                         tray_w=cfg.tray_w_mm, tray_h=cfg.tray_h_mm)
    dets = vdetect.find_items(sc.image, sc.H_px_to_mm, book)
    return sc, dets, jobqueue.build(dets, cfg)


class TestRecordingLink(unittest.TestCase):
    def setUp(self):
        self.cfg = cfgmod.load(TOML)

    def _link(self):
        return studio.RecordingLink(MockLink(axes=self.cfg.axes), self.cfg)

    def test_goto_records_two_parallel_motions(self):
        """XY 必须记成同一时刻出发的两段，否则动画会以为要串行走。"""
        link = self._link()
        link.send(proto.Goto(100.0, 50.0).to_line())
        self.assertEqual(len(link.motions), 2)
        x, y = link.motions
        self.assertEqual((x[0], y[0]), ("x", "y"))
        self.assertEqual(x[3], y[3])                       # 同一个 t0
        self.assertAlmostEqual(x[4], 100.0 / self.cfg.x.speed_mm_s)
        self.assertAlmostEqual(y[4], 50.0 / self.cfg.y.speed_mm_s)

    def test_duration_matches_gear_speed(self):
        link = self._link()
        link.send(proto.ZMove(40.0).to_line())
        (m,) = link.motions
        self.assertAlmostEqual(m[4], 40.0 / self.cfg.z.speed_mm_s, places=6)

    def test_grip_predicts_stall_from_blocker(self):
        """夹爪停在工件宽度处，不是指令位置——这条错了界面会显示夹空也夹到。"""
        link = self._link()
        link.send(proto.Grip(self.cfg.grip_open_mm).to_line())
        link.blocker_mm = 18.0
        link.send(proto.Grip(self.cfg.grip_close_mm).to_line())
        grip_motions = [m for m in link.motions if m[0] == "grip"]
        self.assertAlmostEqual(grip_motions[-1][2], 18.0)
        self.assertAlmostEqual(link.pos["grip"], 18.0)

    def test_no_blocker_closes_fully(self):
        link = self._link()
        link.send(proto.Grip(self.cfg.grip_open_mm).to_line())
        link.blocker_mm = None
        link.send(proto.Grip(self.cfg.grip_close_mm).to_line())
        grip_motions = [m for m in link.motions if m[0] == "grip"]
        self.assertAlmostEqual(grip_motions[-1][2], self.cfg.grip_close_mm)

    def test_home_resets_all_axes(self):
        link = self._link()
        link.send(proto.Goto(100.0, 80.0).to_line())
        link.send(proto.Home().to_line())
        self.assertEqual(set(link.pos.values()), {0.0})

    def test_bad_line_records_no_motion(self):
        link = self._link()
        link.send("NOTACOMMAND 1 2")
        self.assertEqual(link.motions, [])
        self.assertEqual(len(link.commands), 1)      # 但指令本身要留痕

    def test_does_not_fake_blocker_on_plain_serial(self):
        """真串口没有 blocker_mm，包装层不许凭空造一个——runner 靠 hasattr 分流。"""
        class Bare(Link):
            def send(self, line):
                pass

            def poll(self, timeout):
                return None

        bare = Bare()
        link = studio.RecordingLink(bare, self.cfg)
        self.assertFalse(hasattr(bare, "blocker_mm"))
        self.assertFalse(hasattr(link, "blocker_mm"),
                         "包装层伪造了 blocker_mm，真机上会误走模拟堵转分支")


class TestOverrides(unittest.TestCase):
    def setUp(self):
        self.cfg = cfgmod.load(TOML)

    def test_empty_keeps_everything(self):
        out = studio.apply_overrides(self.cfg, {})
        self.assertEqual(out.z.speed_mm_s, self.cfg.z.speed_mm_s)
        self.assertEqual(out.z_safe_mm, self.cfg.z_safe_mm)

    def test_speed_override(self):
        out = studio.apply_overrides(self.cfg, {"z_speed": "200"})
        self.assertEqual(out.z.speed_mm_s, 200.0)
        self.assertEqual(out.x.speed_mm_s, self.cfg.x.speed_mm_s)   # 别的轴不动
        self.assertIs(out.z, out.axes["z"])                         # 快捷属性要跟着换

    def test_z_safe_override(self):
        out = studio.apply_overrides(self.cfg, {"z_safe": "25"})
        self.assertEqual(out.z_safe_mm, 25.0)

    def test_illegal_z_safe_rejected(self):
        """z_safe 低于取件高度是配置错误，必须当场报，不能带病往下算。"""
        with self.assertRaises(cfgmod.ConfigError):
            studio.apply_overrides(self.cfg, {"z_safe": "1"})

    def test_garbage_falls_back_to_default(self):
        out = studio.apply_overrides(self.cfg, {"z_speed": "abc"})
        self.assertEqual(out.z.speed_mm_s, self.cfg.z.speed_mm_s)


class TestTimeline(unittest.TestCase):
    def setUp(self):
        self.cfg = cfgmod.load(TOML)
        self.book = vclasses.load(CLASSES)
        self.sc, self.dets, self.plan = _scene(self.cfg, self.book)

    def test_timeline_matches_report_clock(self):
        """★ 动画时间轴的末尾 == 报告里的节拍。这是整个界面可信的前提。"""
        link, res = studio.simulate(self.cfg, self.plan, set())
        end = max(m[3] + m[4] for m in link.motions)
        self.assertAlmostEqual(end, res.total_s, places=3)

    def test_every_item_has_motions(self):
        link, res = studio.simulate(self.cfg, self.plan, set())
        for rec in res.items:
            inside = [m for m in link.motions
                      if rec.t_start_s - 1e-6 <= m[3] <= rec.t_end_s + 1e-6]
            self.assertTrue(inside, f"第 {rec.pos} 件没有录到任何动作")

    def test_hold_window_brackets_transport(self):
        """夹住的时间窗必须夹在"这件开始"和"这件结束"之间。"""
        _, res = studio.simulate(self.cfg, self.plan, set())
        for rec in res.items:
            hf, ht = studio._hold_window(rec, self.cfg)
            self.assertIsNotNone(hf)
            self.assertLessEqual(rec.t_start_s, hf)
            if rec.held:
                self.assertIsNotNone(ht)
                self.assertLessEqual(ht, rec.t_end_s + 1e-6)

    def test_skipped_item_never_reaches_bin(self):
        """空夹的件不许跑去料盒——那是最容易写错、也最丢分的一条。"""
        link, res = studio.simulate(self.cfg, self.plan, {1})
        rec = res.items[0]
        self.assertFalse(rec.held)
        bx, by = self.cfg.bin_xy(rec.category)
        moved = [m for m in link.motions
                 if m[0] in ("x", "y") and rec.t_start_s <= m[3] <= rec.t_end_s]
        self.assertTrue(moved)
        hit_x = any(abs(m[2] - bx) < 1e-6 for m in moved if m[0] == "x")
        hit_y = any(abs(m[2] - by) < 1e-6 for m in moved if m[0] == "y")
        self.assertFalse(hit_x and hit_y, "空夹的件跑去了料盒")


class TestPayload(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.studio = studio.Studio(TOML, CLASSES)

    def test_required_keys(self):
        sc = self.studio.state
        for key in ("machine", "tray", "bins", "axes", "process", "detections",
                    "items", "plan", "motions", "commands", "total_s"):
            self.assertIn(key, sc)

    def test_items_match_plan(self):
        sc = self.studio.state
        self.assertEqual(len(sc["items"]), 12)              # 6 类 × 2 件
        self.assertEqual(sc["placed"] + sc["skipped_n"], len(sc["items"]))

    def test_polygons_in_machine_frame(self):
        sc = self.studio.state
        cfg = self.studio.base_cfg
        for d in sc["detections"]:
            for x, y in d["poly"]:
                self.assertGreaterEqual(x, cfg.tray_x0_mm - 1e-6)
                self.assertLessEqual(x, cfg.tray_x0_mm + cfg.tray_w_mm + 1e-6)
                self.assertGreaterEqual(y, cfg.tray_y0_mm - 1e-6)
                self.assertLessEqual(y, cfg.tray_y0_mm + cfg.tray_h_mm + 1e-6)

    def test_steps_carry_numbers_only(self):
        """sequence 的 args 里有 Target 对象，界面只能拿到数字。"""
        sc = self.studio.state
        for it in sc["items"]:
            for s in it["steps"]:
                for a in s["a"]:
                    self.assertIsInstance(a, (int, float))
                self.assertIsInstance(s["k"], str)

    def test_screen_frame_is_png_with_all_phases(self):
        for phase in ("ready", "moving", "placed", "skip", "done"):
            png = self.studio.screen_png(1, phase)
            self.assertTrue(png.startswith(b"\x89PNG"), phase)

    def test_axes_expose_gear_chain(self):
        """界面要显示 S = 整步×细分/(πd/i)，所以这三样都得给出去。"""
        ax = self.studio.state["axes"]["x"]
        expect = ax["steps_per_rev"] * ax["microstep"] / ax["mm_per_rev"]
        # 三个字段各自取过 3 位小数，重组回来只能要求到这个精度
        self.assertLess(abs(ax["steps_per_mm"] - expect), 0.01)

    def test_reload_changes_cycle(self):
        quick = self.studio.reload({"z_safe": "25"})
        slow = self.studio.reload({"z_safe": "45"})
        self.assertLess(quick["total_s"], slow["total_s"])

    def test_reload_resets_screen_cache(self):
        self.studio.reload({"seed": "3"})
        png = self.studio.screen_png(1, "moving")
        self.assertTrue(png.startswith(b"\x89PNG"))


class TestPlanRoundTrip(unittest.TestCase):
    def test_live_reuses_payload_plan(self):
        """实跑要从同一份 payload 还原队列，不能自己再排一次。"""
        st = studio.Studio(TOML, CLASSES)
        cfg = st.base_cfg
        plan = studio._plan_from_state(st.state, cfg, st.book)
        self.assertEqual(len(plan.targets), len(st.state["items"]))
        for got, want in zip(plan.targets, st.state["items"]):
            self.assertEqual(got.index, want["index"])
            self.assertEqual(got.category, want["category"])
            self.assertAlmostEqual(got.pick_xy[0], want["pick"][0])


if __name__ == "__main__":
    unittest.main()
