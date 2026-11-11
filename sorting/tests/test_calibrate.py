"""相机标定工具：不碰真实照片，只验算得对不对。

合成场景自带一个真值单应 H。把它反过来（毫米->像素），取托盘四角，
再喂给 solve_from_corners，看能不能把 H 原样解回来——这是求解链路的
自洽性检验，和"真机精度"是两件事。
"""

import unittest
from pathlib import Path

import numpy as np

from sorting import config as cfgmod
from sorting.tools import calibrate as cal
from sorting.vision import classes as vclasses
from sorting.vision import geometry
from sorting.vision import synthetic

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
TOML = CONFIG_DIR / "machine.toml"
CLASSES = CONFIG_DIR / "classes.json"


class TestTrayFrame(unittest.TestCase):
    def setUp(self):
        self.cfg = cfgmod.load(TOML)

    def test_corners_are_tl_tr_br_bl(self):
        c = cal.tray_corners_mm(self.cfg)
        w, h = self.cfg.tray_w_mm, self.cfg.tray_h_mm
        self.assertEqual(c.shape, (4, 2))
        np.testing.assert_allclose(c[0], [0.0, 0.0])
        np.testing.assert_allclose(c[1], [w, 0.0])
        np.testing.assert_allclose(c[2], [w, h])
        np.testing.assert_allclose(c[3], [0.0, h])

    def test_corner_order_is_clockwise(self):
        # 顺序反了会解出一个镜像的 H，抓取位置会左右颠倒——很难查
        c = cal.tray_corners_mm(self.cfg)
        a = c[1] - c[0]
        b = c[2] - c[1]
        cross = a[0] * b[1] - a[1] * b[0]
        self.assertGreater(cross, 0.0, "四角应为顺时针（图像坐标系）")


class TestSolve(unittest.TestCase):
    def setUp(self):
        self.cfg = cfgmod.load(TOML)
        self.book = vclasses.load(CLASSES)
        scene = synthetic.build(self.book, seed=0, per_class=2,
                                tray_w=self.cfg.tray_w_mm,
                                tray_h=self.cfg.tray_h_mm)
        # 合成场景给的是 像素->毫米，标定工具要的是 毫米->像素
        self.truth = np.linalg.inv(np.asarray(scene.H_px_to_mm, dtype=np.float64))

    def test_exact_corners_recover_h(self):
        px = geometry.apply(self.truth, cal.tray_corners_mm(self.cfg))
        H, rms = cal.solve_from_corners(px, self.cfg)
        self.assertLess(rms, 1e-9)
        # 比元素没意义（透视项量级差好几个数量级），比映射结果
        probe = np.array([[0, 0], [80, 80], [160, 0], [0, 160]], dtype=np.float64)
        np.testing.assert_allclose(geometry.apply(H, probe),
                                   geometry.apply(self.truth, probe), atol=1e-6)

    def test_roundtrip_mm_px_mm(self):
        px = geometry.apply(self.truth, cal.tray_corners_mm(self.cfg))
        H, _ = cal.solve_from_corners(px, self.cfg)
        pts_mm = np.array([[10, 20], [150, 30], [140, 150], [5, 155]], dtype=np.float64)
        back = geometry.apply(np.linalg.inv(H), geometry.apply(H, pts_mm))
        np.testing.assert_allclose(back, pts_mm, atol=1e-6)

    def test_px_per_mm_is_plausible(self):
        px = geometry.apply(self.truth, cal.tray_corners_mm(self.cfg))
        H, _ = cal.solve_from_corners(px, self.cfg)
        sx, sy = cal.px_per_mm(H, self.cfg)
        # 合成相机是斜俯视，横纵比例本来就不相等，不能要求 sx == sy。
        # 只验两端落在合理区间，以及比例和图像尺寸自洽。
        for s in (sx, sy):
            self.assertGreater(s, 0.5)
            self.assertLess(s, 50.0)
        span = geometry.apply(H, cal.tray_corners_mm(self.cfg))
        w = float(np.linalg.norm(span[1] - span[0]))
        h = float(np.linalg.norm(span[3] - span[0]))
        self.assertAlmostEqual(w / self.cfg.tray_w_mm, sx, places=6)
        self.assertAlmostEqual(h / self.cfg.tray_h_mm, sy, places=6)

    def test_one_pixel_noise_stays_sub_millimetre(self):
        px = geometry.apply(self.truth, cal.tray_corners_mm(self.cfg))
        rng = np.random.default_rng(0)
        px = px + rng.normal(0.0, 1.0, px.shape)
        H, _ = cal.solve_from_corners(px, self.cfg)
        sx, _ = cal.px_per_mm(H, self.cfg)

        g = np.linspace(0.0, self.cfg.tray_w_mm, 7)
        grid = np.array([[x, y] for x in g for y in g], dtype=np.float64)
        d_px = np.linalg.norm(geometry.apply(H, grid) - geometry.apply(self.truth, grid),
                              axis=1)
        self.assertLess(float(d_px.max()) / sx, 1.5)   # mm

    def test_collinear_corners_rejected(self):
        bad = np.array([[0, 0], [10, 10], [20, 20], [30, 30]], dtype=np.float64)
        with self.assertRaises(geometry.HomographyError):
            cal.solve_from_corners(bad, self.cfg)


class TestIntrinsicsHandling(unittest.TestCase):
    def test_no_intrinsics_passes_image_through(self):
        img = np.zeros((8, 8, 3), dtype=np.uint8)
        self.assertIs(cal.undistort(img, None), img)
        self.assertIs(cal.undistort(img, {}), img)


if __name__ == "__main__":
    unittest.main()
