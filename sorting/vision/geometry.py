"""单应变换：托盘毫米坐标 <-> 图像像素坐标。

托盘是平面，相机固定俯拍，所以两者之间是一个 3×3 单应矩阵 H。
一张图求一次，之后正反变换都只做一次矩阵乘。

给定 4 组对应点就能解 H（8 个未知量、8 个方程）。真机上这 4 个点是
托盘四角的标记；演示时由合成相机直接给出，用来验证"检测 -> 毫米"这一
段没有算错。
"""

from __future__ import annotations

import numpy as np


class HomographyError(ValueError):
    pass


def solve(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """求把 src 映射到 dst 的单应矩阵。src/dst 都是 N×2（N ≥ 4）。

    用 DLT：每对点贡献 2 行，凑成 A·h = 0，取 A 的最小奇异向量。
    """
    src = np.asarray(src, dtype=np.float64).reshape(-1, 2)
    dst = np.asarray(dst, dtype=np.float64).reshape(-1, 2)
    if src.shape[0] < 4 or src.shape != dst.shape:
        raise HomographyError("至少需要 4 组对应点，且两组点数相同")

    rows = []
    for (x, y), (u, v) in zip(src, dst):
        rows.append([-x, -y, -1, 0, 0, 0, u * x, u * y, u])
        rows.append([0, 0, 0, -x, -y, -1, v * x, v * y, v])
    A = np.asarray(rows, dtype=np.float64)

    _, _, vt = np.linalg.svd(A)
    H = vt[-1].reshape(3, 3)
    if abs(H[2, 2]) < 1e-12:
        raise HomographyError("解出的单应矩阵退化")
    return H / H[2, 2]


def apply(H: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """把一组点过一遍单应变换。pts 是 N×2，返回 N×2。"""
    pts = np.asarray(pts, dtype=np.float64).reshape(-1, 2)
    ones = np.ones((pts.shape[0], 1))
    homo = np.hstack([pts, ones]) @ H.T
    w = homo[:, 2:3]
    # 透视变换后 w 可能接近 0（点在无穷远）；托盘点不会，兜一下防止除零
    w = np.where(np.abs(w) < 1e-12, 1e-12, w)
    return homo[:, :2] / w
