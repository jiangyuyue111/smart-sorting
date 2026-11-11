"""任务队列：三种排法、跳过分支、代价计算。"""

import unittest
from pathlib import Path

from sorting import config as cfgmod
from sorting.core import jobqueue
from sorting.vision import classes as vclasses
from sorting.vision import detect as vdetect
from sorting.vision import synthetic

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def prepared(seed: int = 0, per_class: int = 2):
    cfg = cfgmod.load(CONFIG_DIR / "machine.toml")
    book = vclasses.load(CONFIG_DIR / "classes.json")
    sc = synthetic.build(book, seed=seed, per_class=per_class,
                         tray_w=cfg.tray_w_mm, tray_h=cfg.tray_h_mm)
    dets = vdetect.find_items(sc.image, sc.H_px_to_mm, book)
    return cfg, dets, jobqueue.build(dets, cfg)


class TestPlan(unittest.TestCase):

    def test_every_item_enters_the_queue(self):
        _, dets, plan = prepared()
        self.assertEqual(len(plan.targets), len(dets))
        self.assertEqual(plan.skipped, [])

    def test_covers_all_six_classes(self):
        _, _, plan = prepared()
        self.assertEqual(set(plan.by_class), {"1", "2", "3", "4", "5", "6"})
        for cat, items in plan.by_class.items():
            self.assertEqual(len(items), 2, cat)

    def test_picks_the_cheapest_strategy(self):
        _, _, plan = prepared()
        self.assertEqual(set(plan.costs), {"detect", "nn", "class"})
        self.assertAlmostEqual(plan.cost_mm, min(plan.costs.values()), places=6)
        self.assertEqual(plan.cost_mm, plan.costs[plan.strategy])

    def test_pick_points_are_in_machine_coords(self):
        cfg, _, plan = prepared()
        for t in plan.targets:
            x, y = t.pick_xy
            self.assertGreaterEqual(x, cfg.tray_x0_mm)
            self.assertLessEqual(x, cfg.tray_x0_mm + cfg.tray_w_mm)
            self.assertGreaterEqual(y, cfg.tray_y0_mm)
            self.assertLessEqual(y, cfg.tray_y0_mm + cfg.tray_h_mm)
            self.assertLessEqual(x, cfg.x.max_mm)
            self.assertLessEqual(y, cfg.y.max_mm)

    def test_bin_comes_from_category(self):
        cfg, _, plan = prepared()
        for t in plan.targets:
            self.assertEqual(t.bin_xy, cfg.bins[t.category])

    def test_unrecognised_items_are_skipped_not_guessed(self):
        """未识别的件绝不能被塞进任何一个盒子。"""
        cfg, dets, _ = prepared()
        dets[0].verdict.cls = None
        dets[0].verdict.reason = "置信度太低"
        plan = jobqueue.build(dets, cfg)
        self.assertEqual(len(plan.targets), len(dets) - 1)
        self.assertEqual(len(plan.skipped), 1)
        self.assertIn("置信度", plan.skipped[0].reason)

    def test_empty_input(self):
        cfg = cfgmod.load(CONFIG_DIR / "machine.toml")
        plan = jobqueue.build([], cfg)
        self.assertEqual(plan.targets, [])
        self.assertEqual(plan.cost_mm, 0.0)
        self.assertEqual(jobqueue.describe(plan), "队列为空")


class TestCost(unittest.TestCase):

    def test_sorted_is_never_worse_than_raw(self):
        for seed in range(4):
            _, _, plan = prepared(seed=seed)
            self.assertLessEqual(plan.cost_mm, plan.costs["detect"] + 1e-6)

    def test_class_grouping_is_contiguous(self):
        """走 class 策略时，同类别必须连在一起——这正是它换来的东西。"""
        _, _, plan = prepared()
        if plan.strategy != "class":
            self.skipTest("本次最优策略不是 class")
        seen, prev = set(), None
        for t in plan.targets:
            if t.category != prev:
                self.assertNotIn(t.category, seen, "同类被拆开了")
                seen.add(t.category)
                prev = t.category


if __name__ == "__main__":
    unittest.main()
