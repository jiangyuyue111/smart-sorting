"""屏幕渲染：尺寸、四个字段是否都在、能否落盘。"""

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from sorting.display import renderer


def model(**kw):
    base = dict(index=3, name="红色三角", total=12, done=2,
                thumb=Image.new("RGB", (40, 30), (200, 50, 50)))
    base.update(kw)
    return renderer.DisplayModel(**base)


class TestRender(unittest.TestCase):

    def test_size_and_mode(self):
        img = renderer.render(model())
        self.assertEqual(img.size, (renderer.W, renderer.H))
        self.assertEqual(img.mode, "RGB")

    def test_works_without_thumbnail(self):
        img = renderer.render(model(thumb=None))
        self.assertEqual(img.size, (renderer.W, renderer.H))

    def test_long_name_still_renders(self):
        img = renderer.render(model(name="蓝色三角形"))
        self.assertEqual(img.size, (renderer.W, renderer.H))

    def test_every_status(self):
        for st in ("READY", "MOVING", "PLACED", "SKIP", "DONE"):
            img = renderer.render(model(status=st))
            self.assertEqual(img.size, (renderer.W, renderer.H))

    def test_save_creates_parent_dir(self):
        with tempfile.TemporaryDirectory() as td:
            p = renderer.save(model(), Path(td) / "a" / "b" / "f.png")
            self.assertTrue(p.exists())
            with Image.open(p) as im:
                self.assertEqual(im.size, (renderer.W, renderer.H))

    def test_content_changes_with_fields(self):
        """换个序号必须画出来不一样，否则屏上根本没刷新。"""
        a = renderer.render(model(index=1)).tobytes()
        b = renderer.render(model(index=2)).tobytes()
        self.assertNotEqual(a, b)

    def test_cjk_font_is_available(self):
        f = renderer.font(20)
        self.assertIsNotNone(f)


if __name__ == "__main__":
    unittest.main()
