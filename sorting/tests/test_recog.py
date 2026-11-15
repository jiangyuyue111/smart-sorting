"""识别实况页背后的那几件事，尤其是"图是现算的、代码是现读的"这条。

这一页是拿去回答"你怎么知道识别是对的"的，所以它自己不能是演出来的：

    stage_png        七张中间图必须来自 detect 的真函数，不是照着结果补画
    build_payload    圆度对比必须真的能证明"抽稀修了测量，不是调了阈值"
    code_index       界面上的源码必须和磁盘上的源码逐字相同

最后一条用 `inspect.getsource` 对拍——如果哪天有人把源码抄进 HTML，
这条测试会红。
"""

import inspect
import unittest
from pathlib import Path

from sorting import config as cfgmod
from sorting.app import recog
from sorting.app import studio
from sorting.vision import classes as vclasses
from sorting.vision import detect as vdetect
from sorting.vision import synthetic

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
TOML = CONFIG_DIR / "machine.toml"
CLASSES = CONFIG_DIR / "classes.json"


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = cfgmod.load(TOML)
        cls.book = vclasses.load(CLASSES)
        cls.scene = synthetic.build(cls.book, seed=0, per_class=2,
                                    tray_w=cls.cfg.tray_w_mm,
                                    tray_h=cls.cfg.tray_h_mm)
        cls.dets = vdetect.find_items(cls.scene.image, cls.scene.H_px_to_mm, cls.book)
        cls.payload = recog.build_payload(cls.cfg, cls.scene, cls.dets, cls.book)


class TestStages(_Base):
    def test_every_stage_renders_a_png(self):
        for key in recog.STAGE_KEYS:
            png = recog.stage_png(self.scene, self.dets, key)
            self.assertTrue(png.startswith(b"\x89PNG"), key)
            self.assertGreater(len(png), 2000, key)

    def test_unknown_stage_rejected(self):
        with self.assertRaises(KeyError):
            recog.stage_png(self.scene, self.dets, "99")

    def test_payload_lists_the_same_stages(self):
        self.assertEqual([s["k"] for s in self.payload["stages"]], list(recog.STAGE_KEYS))

    def test_stage_code_anchors_exist_in_source(self):
        """每个"该高亮哪一行"的关键字，必须真的能在对应函数里找到。

        找不到就只是不亮，不会报错——但那样舞台上指的东西就成了空话，
        所以在这里钉死。
        """
        code = self.payload["code"]
        for st in recog.STAGES:
            if not st["code"]:
                continue
            key, needle = st["code"]
            self.assertIn(key, code)
            src = "\n".join(code[key]["lines"])
            self.assertIn(needle, src, f"{st['k']} 环高亮关键字 {needle!r} 找不到")


class TestCircularityStory(_Base):
    """页面最主要的一张论证：抽稀把圆度拉回理论值。"""

    def test_every_item_moves_closer_to_its_theory(self):
        theory = {k: v.circularity for k, v in self.book.shapes.items()}
        for row in self.payload["rows"]:
            t = theory[row["shape"]]
            self.assertLess(abs(row["circ_poly"] - t), abs(row["circ_raw"] - t),
                            f"#{row['idx']} {row['label']}：抽稀后反而更远了")

    def test_simplification_actually_removes_points(self):
        for row in self.payload["rows"]:
            self.assertLessEqual(row["poly_pts"], row["raw_pts"])
            self.assertGreaterEqual(row["poly_pts"], 3)

    def test_square_raw_circularity_is_systematically_low(self):
        """像素阶梯让正方形算成 ~0.73 而不是 0.785——这就是抽稀的理由。"""
        squares = [r for r in self.payload["rows"] if r["shape"] == "square"]
        self.assertTrue(squares)
        for r in squares:
            self.assertLess(r["circ_raw"], 0.78)
            self.assertAlmostEqual(r["circ_poly"], 0.785, places=2)


class TestShapeScores(_Base):
    def test_chosen_is_unique_and_matches_judge_shape(self):
        for row in self.payload["rows"]:
            chosen = [s["key"] for s in row["shape_scores"] if s["chosen"]]
            self.assertEqual(len(chosen), 1)
            best, _, _ = self.book.judge_shape(
                row["circ_poly"], row["poly_pts"], row["fill"])
            self.assertEqual(chosen[0], best, f"#{row['idx']} 打分与 judge_shape 不一致")

    def test_scores_are_sorted(self):
        for row in self.payload["rows"]:
            dists = [s["dist"] for s in row["shape_scores"]]
            self.assertEqual(dists, sorted(dists))

    def test_shape_scores_does_not_mutate_book(self):
        before = dict(self.book.thresholds)
        self.book.shape_scores(0.7, 4)
        self.assertEqual(self.book.thresholds, before)


class TestTruth(_Base):
    def test_all_items_paired_on_seed_zero(self):
        rows = self.payload["rows"]
        self.assertEqual(len(rows), len(self.scene.items))
        for row in rows:
            self.assertIsNotNone(row["truth"], f"#{row['idx']} 没配上真值")
            self.assertIsNotNone(row["err_mm"])
            self.assertLess(row["err_mm"], 5.0)

    def test_nothing_missed(self):
        self.assertEqual(self.payload["missed"], [])

    def test_counts_agree_with_summary(self):
        s = self.payload["summary"]
        self.assertEqual(s["n_det"], s["n_match"])
        self.assertEqual(s["both_ok"], s["n_match"])

    def test_coordinate_chain_is_consistent(self):
        """整机 mm = 托盘 mm + 托盘原点。三个数必须对得上。"""
        for row in self.payload["rows"]:
            self.assertAlmostEqual(row["machine_xy"][0],
                                   self.cfg.tray_x0_mm + row["cx_mm"], places=1)
            self.assertAlmostEqual(row["machine_xy"][1],
                                   self.cfg.tray_y0_mm + row["cy_mm"], places=1)


class TestThresholds(_Base):
    def test_high_confidence_skips_the_uncertain_ones(self):
        """界面把置信度下限拉高，判得不够准的件必须变成"不抓"。

        注意满分件（圆度与理论值几乎重合、置信度 1.00）不会被筛掉——
        判据是 `conf < thr`，严格小于。所以这里断言的是"有一部分被跳过，
        且跳过的原因写清楚了"，不是"全都跳过"。这条边界本身值得留着。
        """
        book = studio._book_with(self.book, {"min_confidence": 0.99})
        dets = vdetect.find_items(self.scene.image, self.scene.H_px_to_mm, book)
        payload = recog.build_payload(self.cfg, self.scene, dets, book)

        rows = payload["rows"]
        skipped = [r for r in rows if not r["ok"]]
        self.assertTrue(rows)
        self.assertTrue(skipped, "拉高置信度下限后一件都没被跳过，说明阈值没接上")
        for row in skipped:
            self.assertIn("置信度", row["reason"])
            self.assertIsNotNone(row["truth"])      # 真值配对还在，只是不去抓
        for row in rows:
            if row["confidence"] < 0.99:
                self.assertFalse(row["ok"], f"#{row['idx']} 置信度不够却还是去抓")

    def test_override_does_not_touch_the_base_book(self):
        before = dict(self.book.thresholds)
        studio._book_with(self.book, {"min_confidence": 0.99, "min_area_mm2": 999})
        self.assertEqual(self.book.thresholds, before)

    def test_zero_confidence_keeps_everything(self):
        book = studio._book_with(self.book, {"min_confidence": 0.0, "min_margin": 0.0})
        dets = vdetect.find_items(self.scene.image, self.scene.H_px_to_mm, book)
        self.assertEqual(sum(1 for d in dets if d.ok), len(dets))


class TestCodeIndex(unittest.TestCase):
    def setUp(self):
        self.code = recog.code_index()

    def test_source_matches_inspect_verbatim(self):
        """界面上的代码必须和进程里在跑的函数逐字相同——抄一份就会漂移。"""
        for key, (fn, _title) in recog.CODE_TARGETS.items():
            want = inspect.getsource(fn).rstrip("\n").split("\n")
            got = [ln.rstrip() for ln in self.code[key]["lines"]]
            self.assertEqual(got, [ln.rstrip() for ln in want], key)

    def test_path_exists_and_start_line_matches(self):
        for key, entry in self.code.items():
            path = recog.ROOT / entry["path"]
            self.assertTrue(path.exists(), f"{key} 的路径不存在：{entry['path']}")
            lines = path.read_text(encoding="utf-8").split("\n")
            first = entry["lines"][0].strip()
            self.assertEqual(lines[entry["start"] - 1].strip(), first, key)

    def test_no_absolute_paths_leak(self):
        for entry in self.code.values():
            self.assertNotIn(":\\", entry["path"])
            self.assertFalse(entry["path"].startswith("/"))


class TestStudioIntegration(unittest.TestCase):
    def test_recognition_route_payload(self):
        st = studio.Studio(TOML, CLASSES)
        payload = st.recognition({})
        self.assertEqual(len(payload["rows"]), 12)
        self.assertEqual(len(payload["stages"]), 7)
        self.assertIn("segment", payload["code"])
        # 只算一次，图也拿得到
        self.assertTrue(st.stage_png("06").startswith(b"\x89PNG"))

    def test_stage_cache_cleared_when_thresholds_change(self):
        st = studio.Studio(TOML, CLASSES)
        st.recognition({})
        first = st.stage_png("07")
        st.recognition({"min_confidence": 0.99})
        second = st.stage_png("07")
        self.assertNotEqual(first, second, "判定变了，第 07 张图却没重画")


if __name__ == "__main__":
    unittest.main()
