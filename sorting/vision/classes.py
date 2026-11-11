"""类别簿：把 classes.json 读成可查的结构，并给出"一条轮廓属于哪一类"的判定。

判据来自赛规：**形状相同且颜色相同**才算同一类。所以分类被拆成两件独立的
事——先定形状，再定颜色，最后组合成类别。拆开的好处是现场调阈值时能一眼
看出是哪一半错了（形状判对但颜色判错，和整个都错，处理方式完全不同）。

颜色用色相直方图的**峰值**而不是均值：红色跨 0°/180° 两端，直接求均值会
算成青色；峰值只需要对直方图做一次循环平滑就能正确处理跨越。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np


class ClassBookError(ValueError):
    pass


@dataclass(frozen=True)
class ColorDef:
    name: str
    hue: tuple[int, int]
    hue_wrap: tuple[int, int] | None
    min_s: float
    min_v: float

    def contains(self, h: float) -> bool:
        if self.hue[0] <= h <= self.hue[1]:
            return True
        if self.hue_wrap and self.hue_wrap[0] <= h <= self.hue_wrap[1]:
            return True
        return False


@dataclass(frozen=True)
class ShapeDef:
    key: str
    label: str
    circularity: float          # 理论圆度 4πA/P²
    vertices: tuple[int, int]
    fill: tuple[float, float]


@dataclass(frozen=True)
class ClassDef:
    id: str
    name: str
    shape: str
    color: str


@dataclass
class Verdict:
    """一次判定的完整过程，报告里要展示"为什么这么判"。"""

    cls: ClassDef | None
    confidence: float
    shape: str | None
    color: str | None
    shape_margin: float
    reason: str


class ClassBook:
    def __init__(self, colors, shapes, classes, thresholds) -> None:
        self.colors: list[ColorDef] = colors
        self.shapes: dict[str, ShapeDef] = shapes
        self.classes: list[ClassDef] = classes
        self.thresholds = thresholds
        self._by_id = {c.id: c for c in classes}
        self._by_pair = {(c.shape, c.color): c for c in classes}

    # -- 查询 -------------------------------------------------------------
    def get(self, cid: str) -> ClassDef:
        try:
            return self._by_id[str(cid)]
        except KeyError as exc:
            raise ClassBookError(f"没有类别 {cid!r}") from exc

    def names(self) -> list[str]:
        return [c.name for c in self.classes]

    # -- 判定 -------------------------------------------------------------
    def judge_shape(self, circularity: float, vertices: int, fill: float) -> tuple[str | None, float, float]:
        """返回 (形状键, 置信度, 与次优的距离余量)。

        打分只用圆度：三个形状的理论圆度分别是 1.000 / 0.785 / 0.605，
        间隔足够大，比多特征加权更稳也更好解释。
        """
        scored = []
        for key, sd in self.shapes.items():
            d = abs(circularity - sd.circularity)
            lo, hi = sd.vertices
            if not (lo <= vertices <= hi):
                d += 0.05          # 顶点数不符的轻惩罚，不直接否决（抗噪）
            scored.append((d, key))
        scored.sort()
        best_d, best = scored[0]
        second_d = scored[1][0] if len(scored) > 1 else 1.0
        conf = max(0.0, 1.0 - best_d / 0.20)
        return best, conf, second_d - best_d

    def judge_color(self, hue: float, sat: float, val: float) -> str | None:
        if sat < 60:
            return None            # 无彩色，本方案只处理有彩件
        for c in self.colors:
            if c.contains(hue) and sat >= c.min_s and val >= c.min_v:
                return c.name
        return None

    def judge(self, hue: float, sat: float, val: float,
              circularity: float, vertices: int, fill: float) -> Verdict:
        shape, conf_s, margin = self.judge_shape(circularity, vertices, fill)
        color = self.judge_color(hue, sat, val)

        if shape is None or color is None:
            why = "形状无法判定" if shape is None else "颜色不在任何已知区间"
            return Verdict(None, 0.0, shape, color, margin, why)

        cls = self._by_pair.get((shape, color))
        if cls is None:
            return Verdict(None, 0.0, shape, color, margin,
                           f"形状 {shape} + 颜色 {color} 没有登记成类别")

        conf = min(conf_s, 1.0)
        thr = self.thresholds
        if conf < thr["min_confidence"]:
            return Verdict(None, conf, shape, color, margin,
                           f"置信度 {conf:.2f} < {thr['min_confidence']}")
        if margin < thr["min_margin"]:
            return Verdict(None, conf, shape, color, margin,
                           f"与第二候选太接近（余量 {margin:.3f}）")

        return Verdict(cls, conf, shape, color, margin, "ok")


def load(path: str | Path) -> ClassBook:
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)

    colors = [
        ColorDef(
            name=c["name"],
            hue=tuple(c["hue"]),
            hue_wrap=tuple(c["hue_wrap"]) if c.get("hue_wrap") else None,
            min_s=float(c.get("min_s", 0)),
            min_v=float(c.get("min_v", 0)),
        )
        for c in raw["colors"]
    ]
    shapes = {
        k: ShapeDef(
            key=k,
            label=v["label"],
            circularity=float(v["circularity"]),
            vertices=tuple(v["vertices"]),
            fill=tuple(v["fill"]),
        )
        for k, v in raw["shapes"].items()
    }
    classes = [ClassDef(**c) for c in raw["classes"]]
    for c in classes:
        if c.shape not in shapes:
            raise ClassBookError(f"类别 {c.id} 引用了未定义的形状 {c.shape}")
        if c.color not in {x.name for x in colors}:
            raise ClassBookError(f"类别 {c.id} 引用了未定义的颜色 {c.color}")

    book = ClassBook(colors, shapes, classes, raw.get("thresholds", {}))
    # 阈值缺失时给一组保守默认，别让它静默变成 0
    for k, v in {"min_confidence": 0.55, "min_margin": 0.08,
                 "min_area_mm2": 120.0, "max_area_mm2": 4000.0}.items():
        book.thresholds.setdefault(k, v)
    return book


def hue_mode(hsv: np.ndarray, mask: np.ndarray) -> tuple[float, float, float]:
    """在掩膜内求色相峰值、平均饱和度、平均明度（OpenCV：H∈[0,180]）。

    峰值而不是均值——红色分布在 0 和 180 两侧，均值会跑到青色去。
    """
    h = hsv[:, :, 0][mask > 0]
    s = hsv[:, :, 1][mask > 0]
    v = hsv[:, :, 2][mask > 0]
    if h.size == 0:
        return 0.0, 0.0, 0.0

    hist = np.bincount(h.astype(np.int32).ravel(), minlength=180).astype(np.float64)
    # 循环三点平滑，把 179/0 当作相邻
    smooth = (
        np.roll(hist, 1) + hist + np.roll(hist, -1)
    )
    peak = int(np.argmax(smooth))
    return float(peak), float(s.mean()), float(v.mean())
