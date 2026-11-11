"""整机俯视示意图：把托盘、料盒、机械臂行走路径画成一张图。

报告里放这张，看图的人立刻能明白"为什么排序重要"——不排序的路径是一团乱线，
排序后是几条干净的弧。坐标全部来自 machine.toml，改配置图就跟着变。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from ..config import Config
from .renderer import font

MARGIN = 64
# 6 个类别各一色，pick→料盒 的连线用类别色，这样"同色线聚到同一个盒子"一眼可见
CLASS_RGB = {
    "1": (222, 92, 92),
    "2": (232, 152, 74),
    "3": (214, 200, 84),
    "4": (92, 176, 222),
    "5": (132, 130, 226),
    "6": (196, 122, 214),
}
TRAVEL_RGB = (96, 210, 132)         # 料盒 -> 下一个取点 的空走
RAW_RGB = (64, 68, 78)


def _make_map(cfg: Config, w: int, h: int):
    sx = (w - 2 * MARGIN) / cfg.x.max_mm
    sy = (h - 2 * MARGIN) / cfg.y.max_mm
    s = min(sx, sy)

    def to_px(x_mm: float, y_mm: float) -> tuple[float, float]:
        return (MARGIN + x_mm * s, h - MARGIN - y_mm * s)   # Y 轴翻转：机器 Y 向上

    return to_px, s


def draw_layout(cfg: Config, plan, path: str | Path,
                w: int = 960, h: int = 640,
                show_unordered: list | None = None) -> Path:
    img = Image.new("RGB", (w, h), (20, 22, 26))
    d = ImageDraw.Draw(img)
    to_px, s = _make_map(cfg, w, h)

    # -- 行程包络 ---------------------------------------------------------
    x0, y0 = to_px(0, cfg.y.max_mm)
    x1, y1 = to_px(cfg.x.max_mm, 0)
    d.rounded_rectangle((x0, y0, x1, y1), radius=6, outline=(60, 66, 76), width=2)
    d.text((x0 + 8, y0 - 22), f"X 行程 0..{cfg.x.max_mm:.0f} mm",
           font=font(12), fill=(120, 128, 140))
    d.text((x1 - 130, y1 + 6), f"Y 0..{cfg.y.max_mm:.0f} mm",
           font=font(12), fill=(120, 128, 140))

    # -- 托盘 -------------------------------------------------------------
    tx0, ty0 = to_px(cfg.tray_x0_mm, cfg.tray_y0_mm + cfg.tray_h_mm)
    tx1, ty1 = to_px(cfg.tray_x0_mm + cfg.tray_w_mm, cfg.tray_y0_mm)
    d.rectangle((tx0, ty0, tx1, ty1), fill=(34, 38, 46), outline=(90, 98, 112), width=2)
    d.text((tx0 + 6, ty0 + 4), "托盘", font=font(13), fill=(150, 158, 172))

    # -- 料盒 -------------------------------------------------------------
    half = 16 * s
    for cid, (bx, by) in cfg.bins.items():
        px, py = to_px(bx, by)
        col = CLASS_RGB.get(cid, (96, 106, 124))
        d.rectangle((px - half, py - half, px + half, py + half),
                    fill=(40, 46, 58), outline=col, width=2)
        d.text((px - 5, py - 8), cid, font=font(15, bold=True), fill=col)

    # -- 不排序的对照路径（垫在底下）-------------------------------------
    if show_unordered:
        pts = [to_px(0, 0)]
        for t in show_unordered:
            pts.append(to_px(*t.pick_xy))
            pts.append(to_px(*t.bin_xy))
        d.line(pts, fill=RAW_RGB, width=1, joint="curve")

    # -- 排序后的路径 -----------------------------------------------------
    # 空走（料盒 -> 下一个取点）画绿线；搬运段（取点 -> 料盒）用类别色。
    # 这样同色的搬运线会全部收束到同一个盒子，一眼就能看出分组结果。
    if plan.targets:
        cur_px = to_px(0, 0)
        for t in plan.targets:
            pick_px = to_px(*t.pick_xy)
            bin_px = to_px(*t.bin_xy)
            col = CLASS_RGB.get(t.category, (200, 200, 200))

            d.line([cur_px, pick_px], fill=TRAVEL_RGB, width=2)
            d.line([pick_px, bin_px], fill=col, width=2)

            r = 6
            d.ellipse((pick_px[0] - r, pick_px[1] - r, pick_px[0] + r, pick_px[1] + r),
                      fill=col, outline=(240, 245, 250), width=1)
            d.text((pick_px[0] + 8, pick_px[1] - 7), f"#{t.index}",
                   font=font(12), fill=(220, 225, 235))
            cur_px = bin_px

    # -- 图例 -------------------------------------------------------------
    lx, ly = MARGIN, h - 26
    d.line([(lx, ly), (lx + 26, ly)], fill=TRAVEL_RGB, width=2)
    d.text((lx + 32, ly - 8), "空走", font=font(12), fill=(160, 168, 180))
    d.line([(lx + 90, ly), (lx + 116, ly)], fill=(222, 92, 92), width=2)
    d.text((lx + 122, ly - 8), "搬运（颜色 = 类别）", font=font(12), fill=(160, 168, 180))
    if show_unordered:
        d.line([(lx + 300, ly), (lx + 326, ly)], fill=RAW_RGB, width=1)
        d.text((lx + 332, ly - 8), "按检出顺序", font=font(12), fill=(120, 128, 140))

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    img.save(p)
    return p
