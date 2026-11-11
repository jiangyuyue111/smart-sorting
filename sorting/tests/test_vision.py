"""视觉链路：合成图 -> 分割 -> 特征 -> 分类 -> 毫米坐标。

这些断言用的是**合成图**，证明的是"代码算得对"，不是真机精度。
真机精度要等相机装上去再测，那时只需把 image/H 换掉，断言照用。
"""

import unittest
from pathlib import Path

import numpy as np

from sorting import config as cfgmod
from sorting.vision import classes as vclasses
from sorting.vision import detect as vdetect
from sorting.vision import geometry, synthetic

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def load():
    cfg = cfgmod.load(CONFIG_DIR / "machine.toml")
    book = vclasses.load(CONFIG_DIR / "classes.json")
    return cfg, book


def scene(seed: int = 0, per_class: int = 2):
    cfg, book = load()
    sc = synthetic.build(book, seed=seed, per_class=per_class,
                         tray_w=cfg.tray_w_mm, tray_h=cfg.tray_h_mm)
    return cfg, book, sc


class TestHomography(unittest.TestCase):
    def test_round_trip(self):
        src = np.array([[0, 0], [100, 0], [100, 80], [0, 80]], float)
        dst = np.array([[10, 400], [600, 380], [620, 20], [5, 30]], float)
        H = geometry.solve(src, dst)
        back = geometry.apply(np.linalg.inv(H), dst)
        self.assertTrue(np.allclose(back, src, atol=1e-8))

    def test_apply_maps_corners(self):
        src = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], float)
        dst = src * 10.0
        H = geometry.solve(src, dst)
        self.assertTrue(np.allclose(geometry.apply(H, src), dst, atol=1e-8))

    def test_rejects_too_few_points(self):
        with self.assertRaises(geometry.HomographyError):
            geometry.solve(np.zeros((3, 2)), np.zeros((3, 2)))


class TestDetection(unittest.TestCase):
    def test_finds_every_item(self):
        _, book = load()
        for seed in range(5):
            _, _, sc = scene(seed=seed)
            dets = vdetect.find_items(sc.image, sc.H_px_to_mm, book)
            self.assertEqual(len(dets), len(sc.items), f"seed={seed}")
            self.assertTrue(all(d.ok for d in dets), f"seed={seed}")

    def test_position_error_is_small(self):
        """像素轮廓 -> 单应 -> 毫米 这一整条换算链的闭合误差。"""
        _, book = load()
        for seed in range(3):
            _, _, sc = scene(seed=seed)
            dets = vdetect.find_items(sc.image, sc.H_px_to_mm, book)
            errs = [e for _, e, _ in vdetect.position_error_mm(dets, sc.items)]
            self.assertGreaterEqual(len(errs), len(sc.items) - 1)
            self.assertLess(max(errs), 1.0, f"seed={seed} 最大误差 {max(errs):.2f} mm")

    def test_circularity_recovers_theory(self):
        """抽稀之后的圆度要回到几何真值，这是形状判定能work的前提。"""
        _, book, sc = scene(seed=0)
        dets = vdetect.find_items(sc.image, sc.H_px_to_mm, book)
        for d in dets:
            want = book.shapes[d.verdict.shape].circularity
            self.assertAlmostEqual(d.circularity, want, delta=0.05,
                                   msg=f"检出#{d.idx} 形状 {d.verdict.shape}")

    def test_colour_separation(self):
        """红色跨 0°/180°，用直方图峰值才不会算成青色。"""
        _, book, sc = scene(seed=0)
        dets = vdetect.find_items(sc.image, sc.H_px_to_mm, book)
        reds = [d for d in dets if d.verdict.color == "红"]
        blues = [d for d in dets if d.verdict.color == "蓝"]
        self.assertTrue(reds and blues)
        self.assertTrue(all(d.hue <= 10 or d.hue >= 170 for d in reds))
        self.assertTrue(all(95 <= d.hue <= 135 for d in blues))

    def test_area_is_physical(self):
        """面积是毫米平方，应当和工件实际尺寸对得上。"""
        _, book, sc = scene(seed=0)
        dets = vdetect.find_items(sc.image, sc.H_px_to_mm, book)
        for d in dets:
            size = d.width_mm
            self.assertGreater(size, 15.0)
            self.assertLess(size, 30.0)


class TestTrayCapacity(unittest.TestCase):
    def test_too_many_items_is_rejected(self):
        _, book = load()
        with self.assertRaises(synthetic.TrayOverflow):
            synthetic.build(book, per_class=12, tray_w=160.0, tray_h=160.0)

    def test_capacity_matches(self):
        n = synthetic.tray_capacity(160.0, 160.0, synthetic.ITEM_MAX_MM,
                                    radius=synthetic.ITEM_RADIUS_MM)
        self.assertGreaterEqual(n, 12)      # 赛规至少要放得下 12 件


class TestClassBook(unittest.TestCase):
    def test_six_classes(self):
        _, book = load()
        self.assertEqual(len(book.classes), 6)
        self.assertEqual(len({c.shape for c in book.classes}), 3)
        self.assertEqual(len({c.color for c in book.classes}), 2)

    def test_low_confidence_is_rejected(self):
        _, book = load()
        v = book.judge(hue=0.0, sat=200.0, val=200.0,
                       circularity=0.70, vertices=6, fill=0.9)
        self.assertIsNone(v.cls)
        self.assertNotEqual(v.reason, "ok")

    def test_unknown_colour_is_rejected(self):
        _, book = load()
        v = book.judge(hue=60.0, sat=200.0, val=200.0,
                       circularity=1.0, vertices=8, fill=0.78)
        self.assertIsNone(v.cls)

    def test_grey_is_rejected(self):
        _, book = load()
        v = book.judge(hue=0.0, sat=10.0, val=60.0,
                       circularity=1.0, vertices=8, fill=0.78)
        self.assertIsNone(v.cls)


if __name__ == "__main__":
    unittest.main()
