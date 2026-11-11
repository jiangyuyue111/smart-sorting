"""从一张俯拍图里找出工件，量出尺寸，判出类别，并换算到托盘毫米坐标。

处理链（每一步都能单独停下来的原因写在注释里）：

    1. V 通道 Otsu 二值化   —— 不能做背景差分：工件是事先摆好的，拍不到空托盘
    2. 形态学开闭           —— 去噪点、补空洞
    3. findContours         —— 只取外轮廓，工件之间不重叠
    4. 轮廓点 -> 毫米       —— 过 H⁻¹，之后的面积/周长/圆度都是物理量
    5. 多边形抽稀           —— eps = 1% 周长，去掉像素锯齿
    6. 特征                 —— 圆度、顶点数、填充率、长宽比、面积、色相
    7. 判定                 —— 交给 ClassBook，低置信度返回 None（走 SKIP）

关键设计一：**面积、周长都在毫米系里算**。像素系里算出来的圆度虽然和尺度无关，
但面积阈值（赛规的最小可夹取尺寸）没法直接比，而且一旦相机高度变了阈值就全废。

关键设计二：**圆度算在抽稀后的多边形上，不算在原始像素轮廓上**。
原始轮廓是锯齿状的阶梯线，周长被高估约 5%，圆度被系统性拉低——
实测一个标准正方形会算出 0.73 而不是理论的 0.785，圆会算出 0.88 而不是 1.0，
三个形状挤在一起就没法分了。用 1% 周长的 Douglas-Peucker 抽稀后，
正方形和三角形直接回到理论值 0.785 / 0.605（见测试 test_features.py）。
这不是调参数凑结果，是把像素离散化带来的偏差去掉。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import cv2
import numpy as np

from . import classes as classbook
from . import geometry


@dataclass
class Detection:
    idx: int
    cx_mm: float                  # 托盘坐标
    cy_mm: float
    area_mm2: float
    perimeter_mm: float
    width_mm: float               # 最小外接矩形的短边：夹爪要跨过的尺寸
    circularity: float
    vertices: int
    fill: float
    aspect: float
    hue: float
    sat: float
    val: float
    verdict: classbook.Verdict
    contour_px: np.ndarray = field(repr=False, default=None)

    @property
    def class_id(self) -> str | None:
        return self.verdict.cls.id if self.verdict.cls else None

    @property
    def name(self) -> str:
        return self.verdict.cls.name if self.verdict.cls else "未识别"

    @property
    def ok(self) -> bool:
        return self.verdict.cls is not None


def segment(image: np.ndarray, open_ksize: int = 3, close_ksize: int = 5) -> np.ndarray:
    """把工件从托盘上分出来。

    用 V（明度）通道做 Otsu：工件是饱和的彩色塑料件，V 高；托盘是深灰哑光板，
    V 低。两峰拉得很开，Otsu 自动找谷底比手调阈值稳，而且换托盘颜色只要重跑
    一次 Otsu 就自适应了。

    不做背景差分的原因是赛规：工件是**事先摆好**的，不允许触碰整理，所以
    根本拿不到"空托盘"这张参考图。
    """
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    v = hsv[:, :, 2]
    _, mask = cv2.threshold(v, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    if open_ksize > 0:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (open_ksize, open_ksize))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)      # 干掉噪点
    if close_ksize > 0:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (close_ksize, close_ksize))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)     # 补上反光造成的小洞
    return mask


EPS_FRAC = 0.01     # 多边形抽稀比例（相对周长），见模块说明


def _poly_mm(contour_px: np.ndarray, H_inv: np.ndarray) -> np.ndarray:
    pts = contour_px.reshape(-1, 2).astype(np.float64)
    return geometry.apply(H_inv, pts)


def find_items(image: np.ndarray, H_px_to_mm: np.ndarray, book: classbook.ClassBook,
               mask: np.ndarray | None = None) -> list[Detection]:
    if mask is None:
        mask = segment(image)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    thr = book.thresholds

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out: list[Detection] = []

    for c in contours:
        if cv2.contourArea(c) < 80:      # 像素级粗筛，省掉后面无谓的单应换算
            continue

        raw_mm = _poly_mm(c, H_px_to_mm).astype(np.float32)
        perim_raw = float(cv2.arcLength(raw_mm, True))
        if perim_raw <= 1e-6:
            continue

        # 抽稀：把像素阶梯去掉，让圆度回到几何真值
        poly = cv2.approxPolyDP(raw_mm, EPS_FRAC * perim_raw, True).astype(np.float32)
        if len(poly) < 3:
            continue

        area = float(cv2.contourArea(poly))
        perim = float(cv2.arcLength(poly, True))
        if perim <= 1e-6 or area <= 0:
            continue
        if not (thr["min_area_mm2"] <= area <= thr["max_area_mm2"]):
            continue

        circ = 4.0 * math.pi * area / (perim * perim)
        n_vert = len(poly)

        rect = cv2.minAreaRect(poly)
        (rw, rh) = rect[1]
        rect_area = rw * rh
        fill = area / rect_area if rect_area > 1e-6 else 0.0
        aspect = (max(rw, rh) / min(rw, rh)) if min(rw, rh) > 1e-6 else 99.0

        m = cv2.moments(poly)
        if abs(m["m00"]) < 1e-9:
            continue
        cx = m["m10"] / m["m00"]
        cy = m["m01"] / m["m00"]

        # 只在轮廓内部统计颜色，避免把托盘底色/阴影算进去
        solid = np.zeros(mask.shape, np.uint8)
        cv2.drawContours(solid, [c], -1, 255, -1)
        hue, sat, val = classbook.hue_mode(hsv, solid)
        verdict = book.judge(hue, sat, val, circ, n_vert, fill)

        out.append(Detection(
            idx=-1, cx_mm=float(cx), cy_mm=float(cy),
            area_mm2=area, perimeter_mm=perim, width_mm=float(min(rw, rh)),
            circularity=circ,
            vertices=n_vert, fill=fill, aspect=aspect,
            hue=hue, sat=sat, val=val, verdict=verdict, contour_px=c,
        ))

    # 按托盘坐标排序，保证同一张图每次输出的编号一致（便于回归比对）
    out.sort(key=lambda d: (round(d.cy_mm / 20), d.cx_mm))
    for i, d in enumerate(out):
        d.idx = i + 1
    return out


def position_error_mm(dets: list[Detection], truth) -> list[tuple[int, float, float]]:
    """把检测结果与真值配对，返回 [(序号, 中心距误差 mm, 角度误差)]。

    配对用最近邻：合成场景里工件最小间距 30mm，检测误差远小于半个间距，
    不会配错。
    """
    pairs = []
    used = set()
    for d in dets:
        best, best_i = None, -1
        for i, it in enumerate(truth):
            if i in used:
                continue
            dist = math.hypot(d.cx_mm - it.cx_mm, d.cy_mm - it.cy_mm)
            if best is None or dist < best:
                best, best_i = dist, i
        if best_i >= 0 and best is not None and best < 30.0:
            used.add(best_i)
            pairs.append((d.idx, best, truth[best_i]))
    return pairs


def draw(image: np.ndarray, dets: list[Detection], H_mm_to_px: np.ndarray) -> np.ndarray:
    """给结果图上画标注，汇报里直接贴这张。"""
    vis = image.copy()
    for d in dets:
        col = (80, 220, 80) if d.ok else (60, 160, 255)      # BGR：绿=识别成功，橙=跳过
        c = d.contour_px
        cv2.drawContours(vis, [c], -1, col, 2)

        cxy = geometry.apply(H_mm_to_px, np.array([[d.cx_mm, d.cy_mm]]))[0]
        p = (int(cxy[0]), int(cxy[1]))
        cv2.drawMarker(vis, p, col, cv2.MARKER_CROSS, 14, 2)

        # 标注放在轮廓上沿外侧，避免压在工件上
        x, y, _, _ = cv2.boundingRect(c)
        label = f"#{d.idx} {d.name}"
        ty = max(14, y - 7)
        cv2.putText(vis, label, (x, ty),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(vis, label, (x, ty),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, col, 1, cv2.LINE_AA)
    return vis
