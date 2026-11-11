"""视觉层：标定 -> 分割 -> 特征 -> 分类 -> 毫米坐标。

对外的门面（注意 `detect` 是模块，函数叫 `find_items`，避免同名遮蔽）：

    from sorting.vision import classes, detect, synthetic

    book  = classes.load("sorting/config/classes.json")
    scene = synthetic.build(book, seed=0)              # 没有相机时的替代品
    dets  = detect.find_items(scene.image, scene.H_px_to_mm, book)

真机上把 `scene.image` 换成 `cv2.VideoCapture(0).read()` 拿到的那一帧，
`scene.H_px_to_mm` 换成离线标定得到的矩阵，下游代码一个字都不用改。
"""

from . import classes, detect, geometry, synthetic
from .classes import ClassBook, ClassBookError, ClassDef, ShapeDef, Verdict, load
from .detect import Detection, draw, find_items, position_error_mm, segment

__all__ = [
    # 模块
    "classes", "detect", "geometry", "synthetic",
    # 常用类
    "ClassBook", "ClassBookError", "ClassDef", "ShapeDef", "Verdict", "load",
    "Detection", "find_items", "draw", "position_error_mm", "segment",
]
