"""合成托盘场景：在没有相机、没有机器的前提下造一张"看起来像真的"俯拍图。

为什么要有这个模块
------------------
中期汇报时硬件还没装好，但视觉链路是必须能演示的部分。做法是：

    按托盘毫米坐标摆工件  ->  单应投影到 640×480 像素  ->  当成本地图像
    ->  走和真机完全相同的分割/特征/分类代码  ->  反算回毫米
    ->  和真值比较，得到定位误差与分类正确率

关键点：**生成侧和检测侧不共享任何中间量**，检测侧只拿到一张图和单应矩阵，
和真机拿到的信息一样多。所以这里测出来的误差是有意义的下界参考，不是自己
骗自己。真机上还会多出光照不均、镜头畸变、反光等误差，那些要等硬件到位再测。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

from . import geometry

# BGR
BG_COLOR = (48, 46, 44)
COLOR_BGR = {
    "红": (45, 45, 205),
    "蓝": (200, 95, 35),
}

# 工件尺寸（mm）
SIZE_MM = {
    "circle": 22.0,     # 直径
    "square": 20.0,     # 边长
    "triangle": 24.0,   # 边长
}
ITEM_MAX_MM = max(SIZE_MM.values())     # 摆位时按最大件留空
ITEM_RADIUS_MM = 14.0                   # 三角形的外接圆半径最大，按它兜住整个轮廓


@dataclass
class SceneItem:
    class_id: str
    name: str
    shape: str
    color: str
    cx_mm: float        # 托盘坐标
    cy_mm: float
    angle_deg: float
    size_mm: float


@dataclass
class Scene:
    image: np.ndarray       # BGR 640×480
    H_mm_to_px: np.ndarray  # 托盘 mm -> 像素
    items: list[SceneItem]

    @property
    def H_px_to_mm(self) -> np.ndarray:
        return np.linalg.inv(self.H_mm_to_px)


def camera_homography(pad_px: int = 40) -> np.ndarray:
    """托盘 0..160mm 映射到 640×480 的画面里，带一点透视和旋转。

    pad 留出画面边缘的一圈余量：工件可能贴着托盘边走，如果托盘正好铺满
    整个画面，边上的工件就会被画面裁掉半个，检测直接失效。真机取景时
    也必须留这圈余量。
    """
    H, W = 480, 640
    s = (H - 2 * pad_px) / 160.0
    tray = np.array([[0, 0], [160, 0], [160, 160], [0, 160]], dtype=np.float64)
    px = np.array([
        [pad_px + 18, H - pad_px],
        [W - pad_px - 6, H - pad_px - 4],
        [W - pad_px, pad_px - 6],
        [pad_px, pad_px + 10],
    ], dtype=np.float64)
    return geometry.solve(tray, px)


def _shape_points(shape: str, size_mm: float, angle_deg: float) -> np.ndarray:
    """在原点生成多边形顶点（托盘 mm 系，逆时针）。"""
    a = math.radians(angle_deg)
    ca, sa = math.cos(a), math.sin(a)

    if shape == "circle":
        n, r = 28, size_mm / 2
        pts = [(r * math.cos(2 * math.pi * i / n), r * math.sin(2 * math.pi * i / n))
               for i in range(n)]
    elif shape == "square":
        h = size_mm / 2
        pts = [(-h, -h), (h, -h), (h, h), (-h, h)]
    elif shape == "triangle":
        r = size_mm / math.sqrt(3)      # 外接圆半径
        pts = [(r * math.cos(math.radians(90 + 120 * i)),
                r * math.sin(math.radians(90 + 120 * i))) for i in range(3)]
    else:
        raise ValueError(f"未知形状 {shape}")

    out = np.array([(x * ca - y * sa, x * sa + y * ca) for x, y in pts], dtype=np.float64)
    return out


class TrayOverflow(ValueError):
    """要求摆放的件数超过托盘物理容量。"""


def tray_capacity(w: float, h: float, item: float, gap: float = 4.0,
                  radius: float | None = None) -> int:
    """托盘最多能摆几件这种尺寸的工件（按最紧的网格算）。

    `item` 用最长边，`gap` 是两件之间至少留的净空——留净空不是为了好看，
    而是夹爪要能插下去，且两件不能碰到一起（碰到就粘成一个轮廓，检测必失败）。
    """
    rad = radius if radius is not None else item / 2.0
    pitch = item + gap
    cols = int((w - 2 * rad) // pitch) + 1
    rows = int((h - 2 * rad) // pitch) + 1
    return max(0, cols * rows)


def _place(rng: np.random.Generator, n: int, w: float, h: float,
           item: float, gap: float = 4.0,
           radius: float | None = None) -> list[tuple[float, float]]:
    """把 n 个点撒在托盘上：抖动网格，而不是纯拒绝采样。

    拒绝采样在件数多时会直接失败，而且失败前已经跑了两万次。网格能保证
    "一定放得下"，抖动保证"不呆板"。抖动幅度按工件尺寸夹住——抖过头的
    后果是两件叠在一起，而现实中工件不会互相穿透。

    位置一律夹在 [radius, 边-radius] 之间：三角形外接圆比边长还大，
    按边长的一半留空会让尖端顶到画面外，检测出来就是缺角的轮廓。
    """
    rad = radius if radius is not None else item / 2.0
    pitch_min = item + gap
    cols_max = max(1, int((w - 2 * rad) // pitch_min) + 1)
    rows_max = max(1, int((h - 2 * rad) // pitch_min) + 1)
    if n > cols_max * rows_max:
        raise TrayOverflow(
            f"{w:.0f}×{h:.0f} mm 的托盘最多摆 {cols_max * rows_max} 件"
            f"（工件 {item:.0f} mm，净空 {gap:.0f} mm），要求 {n} 件"
        )

    # 在能放下的列数里挑一个间距最大的
    best = None
    for cols in range(1, cols_max + 1):
        rows = math.ceil(n / cols)
        if rows > rows_max:
            continue
        pw = (w - 2 * rad) / (cols - 1) if cols > 1 else w
        ph = (h - 2 * rad) / (rows - 1) if rows > 1 else h
        score = min(pw, ph)
        if best is None or score > best[0]:
            best = (score, cols, rows)
    if best is None:
        raise TrayOverflow("托盘放不下这么多工件")
    _, cols, rows = best

    cw = (w - 2 * rad) / (cols - 1) if cols > 1 else 0.0
    ch = (h - 2 * rad) / (rows - 1) if rows > 1 else 0.0
    jx = max(0.0, (cw - pitch_min) / 2.0)
    jy = max(0.0, (ch - pitch_min) / 2.0)

    cells = [(r, c) for r in range(rows) for c in range(cols)]
    rng.shuffle(cells)
    pts: list[tuple[float, float]] = []
    for r, c in cells[:n]:
        x = rad + cw * c + float(rng.uniform(-jx, jx))
        y = rad + ch * r + float(rng.uniform(-jy, jy))
        pts.append((min(max(x, rad), w - rad), min(max(y, rad), h - rad)))
    return pts


def build(classes, seed: int = 0, per_class: int = 2,
          tray_w: float = 160.0, tray_h: float = 160.0,
          blur: int = 3, noise: float = 4.0) -> Scene:
    """按类别簿里的每一类生成 per_class 件，随机撒在托盘上。"""
    rng = np.random.default_rng(seed)
    H = camera_homography()
    img = np.zeros((480, 640, 3), np.uint8)
    img[:] = BG_COLOR

    # 托盘底色稍亮一点，模拟哑光板 + 环形光的边缘衰减
    yy, xx = np.mgrid[0:480, 0:640]
    vign = 1.0 - 0.22 * (((xx - 320) / 320.0) ** 2 + ((yy - 240) / 240.0) ** 2)
    vign = np.clip(vign, 0.55, 1.0)[:, :, None]
    img = np.clip(img.astype(np.float32) * vign, 0, 255).astype(np.uint8)

    # 先把所有位置撒好再随机配对类别：否则同类会挤在同一片，和现场不符
    kinds = [cls for cls in classes.classes for _ in range(per_class)]
    coords = _place(rng, len(kinds), tray_w, tray_h, ITEM_MAX_MM,
                    radius=ITEM_RADIUS_MM)
    rng.shuffle(coords)

    items: list[SceneItem] = []
    for cls, (cx, cy) in zip(kinds, coords):
        ang = float(rng.uniform(0, 360))
        size = SIZE_MM[cls.shape] * float(rng.uniform(0.95, 1.05))
        local = _shape_points(cls.shape, size, ang) + np.array([cx, cy])
        px = geometry.apply(H, local).astype(np.int32)
        cv2.fillPoly(img, [px.reshape(-1, 1, 2)], COLOR_BGR[cls.color])
        items.append(SceneItem(cls.id, cls.name, cls.shape, cls.color, cx, cy, ang, size))

    if blur > 0:
        k = blur if blur % 2 == 1 else blur + 1
        img = cv2.GaussianBlur(img, (k, k), 0)
    if noise > 0:
        img = np.clip(
            img.astype(np.float32) + rng.normal(0, noise, img.shape), 0, 255
        ).astype(np.uint8)

    return Scene(image=img, H_mm_to_px=H, items=items)
