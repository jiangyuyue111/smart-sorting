"""屏幕渲染：把"当前这件工件"画成一张 480×320 的图。

赛规硬性要求屏幕显示四样东西，缺一项该件不得分：
    序号 / 名称 / 图片 / 总数
而且每分拣完一件都要刷新。

这个模块只负责"画"，不负责"什么时候画"。之所以能在没有屏、没有板的情况下
出图，是因为它直接输出 PNG——真机上同一份 render() 返回的 PIL Image 只要
`img.tobytes()` 喂给 SPI 屏驱动即可，布局和字体问题在 PC 上就能调完。
屏是 480×320 横屏（ST7796S 3.5"），这里按同一分辨率出图，所见即所得。

字体查找按"本机有什么用什么"的顺序试，Windows 开发机和树莓派都能跑。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 480, 320

# 配色（RGB）。深底浅字，赛场灯下比浅底更抗反光。
BG = (18, 20, 24)
CARD = (30, 34, 41)
CARD_HI = (38, 78, 52)          # 抓到并已入盒
CARD_WARN = (78, 52, 26)        # 跳过 / 未识别
LINE = (58, 64, 74)
TEXT = (236, 240, 245)
DIM = (140, 148, 160)
ACCENT = (86, 200, 120)
WARN = (240, 170, 70)

_FONT_CANDIDATES_REG = [
    ("C:/Windows/Fonts/msyh.ttc", 0),
    ("C:/Windows/Fonts/simhei.ttf", 0),
    ("C:/Windows/Fonts/Deng.ttf", 0),
    ("C:/Windows/Fonts/simsun.ttc", 0),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 0),
    ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 0),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0),
    ("/System/Library/Fonts/PingFang.ttc", 0),
]
_FONT_CANDIDATES_BOLD = [
    ("C:/Windows/Fonts/msyhbd.ttc", 0),
    ("C:/Windows/Fonts/simhei.ttf", 0),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 0),
] + _FONT_CANDIDATES_REG

_cache: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    if key in _cache:
        return _cache[key]
    for path, idx in (_FONT_CANDIDATES_BOLD if bold else _FONT_CANDIDATES_REG):
        if not os.path.exists(path):
            continue
        try:
            f = ImageFont.truetype(path, size, index=idx)
            _cache[key] = f
            return f
        except Exception:
            continue
    raise RuntimeError(
        "找不到可用的中文字体。Windows 上装微软雅黑即可；"
        "树莓派执行 sudo apt install fonts-noto-cjk"
    )


@dataclass
class DisplayModel:
    """屏上这一刻应该显示的内容。"""

    index: int                     # 当前是第几件（1 起，按处理顺序）
    name: str                      # 类别名称
    total: int                     # 本轮总件数
    thumb: Image.Image | None = None
    status: str = "READY"          # READY / MOVING / PLACED / SKIP / DONE
    done: int = 0                  # 已处理件数（含跳过）
    tally: dict[str, int] = field(default_factory=dict)   # 类别 -> 已入盒件数

    @property
    def status_text(self) -> str:
        return {
            "READY": "就绪",
            "MOVING": "搬运中",
            "PLACED": "已入盒",
            "SKIP": "跳过",
            "DONE": "完成",
        }.get(self.status, self.status)


def _card(d: ImageDraw.ImageDraw, box, fill) -> None:
    d.rounded_rectangle(box, radius=8, fill=fill, outline=LINE, width=1)


def _center_text(d, box, text, f, fill):
    x0, y0, x1, y1 = box
    l, t, r, b = d.textbbox((0, 0), text, font=f)
    d.text(((x0 + x1 - (r - l)) / 2 - l, (y0 + y1 - (b - t)) / 2 - t),
           text, font=f, fill=fill)


def _fit_thumb(thumb: Image.Image, side: int) -> Image.Image:
    img = thumb.convert("RGB")
    img.thumbnail((side, side), Image.LANCZOS)
    return img


def render(m: DisplayModel) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # -- 顶栏 -------------------------------------------------------------
    d.text((14, 8), "智能分拣装置", font=font(18, bold=True), fill=TEXT)
    st = m.status_text
    st_box = (W - 110, 6, W - 12, 30)
    st_fill = {"PLACED": CARD_HI, "SKIP": CARD_WARN, "DONE": CARD_HI}.get(m.status, CARD)
    st_col = {"PLACED": ACCENT, "SKIP": WARN, "DONE": ACCENT}.get(m.status, DIM)
    d.rounded_rectangle(st_box, radius=6, fill=st_fill, outline=LINE, width=1)
    _center_text(d, st_box, st, font(15, bold=True), st_col)
    d.line([(10, 36), (W - 10, 36)], fill=LINE, width=1)

    # -- 四个字段：左上序号 / 右上名称 / 左下图片 / 右下总数 ----------------
    LX, LW = 10, 150
    RX, RW = 168, W - 168 - 10
    R1Y, RH = 44, 130
    R2Y = 182
    R2H = 96

    ok = m.status in ("PLACED", "DONE")
    warn = m.status == "SKIP"

    # 序号
    b = (LX, R1Y, LX + LW, R1Y + RH)
    _card(d, b, CARD_HI if ok else CARD_WARN if warn else CARD)
    d.text((b[0] + 10, b[1] + 8), "序号", font=font(14), fill=DIM)
    _center_text(d, (b[0], b[1] + 26, b[2], b[3] - 6), f"{m.index:02d}",
                 font(66, bold=True), TEXT)

    # 名称
    b = (RX, R1Y, RX + RW, R1Y + RH)
    _card(d, b, CARD_HI if ok else CARD_WARN if warn else CARD)
    d.text((b[0] + 12, b[1] + 8), "名称", font=font(14), fill=DIM)
    name_f = font(34, bold=True) if len(m.name) <= 5 else font(26, bold=True)
    _center_text(d, (b[0] + 6, b[1] + 24, b[2] - 6, b[3] - 6), m.name, name_f, TEXT)

    # 图片
    b = (LX, R2Y, LX + LW, R2Y + R2H)
    _card(d, b, CARD)
    if m.thumb is not None:
        th = _fit_thumb(m.thumb, R2H - 12)
        img.paste(th, (b[0] + (LW - th.width) // 2, b[1] + (R2H - th.height) // 2))
    else:
        _center_text(d, b, "无图", font(16), DIM)

    # 总数
    b = (RX, R2Y, RX + RW, R2Y + R2H)
    _card(d, b, CARD)
    d.text((b[0] + 12, b[1] + 8), "总数", font=font(14), fill=DIM)
    _center_text(d, (b[0] + 10, b[1] + 22, b[2] - 100, b[3] - 6),
                 f"{m.total}", font(40, bold=True), TEXT)
    d.text((b[0] + 12, b[3] - 26), f"进度 {m.done}/{m.total}", font=font(13), fill=DIM)

    # 右下角：本类累计（不占用"总数"字段的位置，只是补充信息）
    if m.tally:
        d.text((b[2] - 84, b[1] + 8), "本类", font=font(12), fill=DIM)
        yy = b[1] + 24
        for c, n in sorted(m.tally.items(), key=lambda kv: kv[0]):
            if yy > b[3] - 16:
                break
            d.text((b[2] - 84, yy), f"{c}号{n}件", font=font(14), fill=TEXT)
            yy += 17

    # -- 底栏 -------------------------------------------------------------
    d.line([(10, H - 22), (W - 10, H - 22)], fill=LINE, width=1)
    d.text((14, H - 18), "形状相同且颜色相同 → 同一料盒",
           font=font(12), fill=DIM)
    return img


def save(m: DisplayModel, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    render(m).save(p)
    return p
