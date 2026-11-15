"""识别实况：把"分割 → 轮廓 → 抽稀 → 特征 → 判定"这一条链摊开给人看。

这个模块只服务一个问题：**你怎么知道识别是对的。**

所以里面有两条硬规矩：

1. **中间图不是照着结果补画的。** 七张图分别调 `detect.segment()`（开/闭运算
   传 0 就是未做形态学的原始 Otsu 结果）、`cv2.findContours`、`approxPolyDP`
   和 `detect.draw()` 现算，参数用的都是 `detect` 自己的常量
   （如 `EPS_FRAC`）。图变成什么样，代码就是什么样。
2. **源代码从磁盘现读。** 界面里展示的 Python 源码由 `inspect.getsourcelines`
   从当前进程正在执行的函数对象取，不是抄一份放进 HTML。抄一份就会漂移，
   漂移过的"代码展示"比没有更糟。

真值只有合成场景才有（`synthetic.build` 生图时一并给出）。真机上这一页会少
"真值"一列——那时正确性靠标定和抽检，不靠这一页。
"""

from __future__ import annotations

import inspect
import io
import math
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from ..vision import classes as vclasses
from ..vision import detect as vdetect
from ..vision import geometry
from .demo import evaluate

ROOT = Path(__file__).resolve().parents[2]

GREEN = (80, 220, 80)       # BGR，和 detect.draw 里"识别成功"同色
DROP = (70, 90, 230)        # 被面积粗筛丢掉的轮廓
FAINT = (120, 125, 130)
KEPT_MIN_AREA_PX = 80       # 与 detect.find_items 里的粗筛一致


# --------------------------------------------------------------------------
# 七个环节：标题 / 一句话说明 / 界面上该高亮哪一行代码
# --------------------------------------------------------------------------

STAGES = (
    {"k": "01", "short": "原图", "title": "原图 640×480",
     "note": "合成托盘图，12 件随机摆。真机上这一格是相机的一帧——下游一行代码都不用改。",
     "code": None},
    {"k": "02", "short": "V 通道", "title": "V 通道（明度）",
     "note": "工件是饱和彩色件，V 高；托盘是深灰哑光板，V 低。两座峰拉得很开。",
     "code": ("segment", "cv2.cvtColor")},
    {"k": "03", "short": "Otsu", "title": "Otsu 自动阈值",
     "note": "自动找两峰之间的谷底，阈值不用手填，换托盘颜色会自己适应。",
     "code": ("segment", "THRESH_OTSU")},
    {"k": "04", "short": "形态学", "title": "形态学 开→闭",
     "note": "开运算干掉噪点，闭运算补上反光造成的小洞。",
     "code": ("segment", "MORPH_CLOSE")},
    {"k": "05", "short": "轮廓", "title": "外轮廓",
     "note": "只取外轮廓（工件互不重叠）。像素面积小于 80 的直接丢掉，省掉后面的单应换算。",
     "code": ("find_items", "RETR_EXTERNAL")},
    {"k": "06", "short": "抽稀", "title": "抽稀到 1% 周长",
     "note": "把像素阶梯去掉。不抽稀的话正方形圆度算成 0.73 而不是理论 0.785，三种形状会挤在一起分不开。",
     "code": ("find_items", "approxPolyDP")},
    {"k": "07", "short": "判定", "title": "判定结果",
     "note": "圆度定形状、色相峰值定颜色，组合成类别。绿=判定成功，橙=跳过不去抓。",
     "code": ("judge", "min_confidence")},
)

STAGE_KEYS = tuple(s["k"] for s in STAGES)
STAGE_BY_KEY = {s["k"]: s for s in STAGES}


# --------------------------------------------------------------------------
# 中间图
# --------------------------------------------------------------------------

def stage_png(scene, dets, key: str) -> bytes:
    """渲染某一环的中间图，返回 PNG 字节。"""
    if key not in STAGE_BY_KEY:
        raise KeyError(f"没有这一环 {key!r}")
    img = scene.image

    if key == "01":
        return _png(img[:, :, ::-1])

    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    if key == "02":
        return _png(cv2.cvtColor(hsv[:, :, 2], cv2.COLOR_GRAY2RGB))

    # 同一个 segment()，把两个半径传 0 就是"还没做形态学"的那一步
    raw = vdetect.segment(img, open_ksize=0, close_ksize=0)
    if key == "03":
        return _png(cv2.cvtColor(raw, cv2.COLOR_GRAY2RGB))

    mask = vdetect.segment(img)
    if key == "04":
        return _png(cv2.cvtColor(mask, cv2.COLOR_GRAY2RGB))

    if key == "05":
        return _png(_draw_contours(img, mask))
    if key == "06":
        return _png(_draw_polys(img, mask, scene))
    return _png(vdetect.draw(img, dets, scene.H_mm_to_px)[:, :, ::-1])


def _png(rgb: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(rgb)).save(buf, format="PNG")
    return buf.getvalue()


def _dim(img: np.ndarray) -> np.ndarray:
    """压暗原图当底，轮廓才看得清。"""
    return (img.astype(np.float32) * 0.32).astype(np.uint8)


def _draw_contours(img: np.ndarray, mask: np.ndarray) -> np.ndarray:
    vis = _dim(img)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    kept = small = 0
    for c in contours:
        if cv2.contourArea(c) < KEPT_MIN_AREA_PX:
            small += 1
            cv2.drawContours(vis, [c], -1, DROP, 1)
        else:
            kept += 1
            cv2.drawContours(vis, [c], -1, GREEN, 2)
    _label(vis, f"外轮廓 {len(contours)} 条：留下 {kept}，面积过小丢掉 {small}")
    return vis[:, :, ::-1]


def _draw_polys(img: np.ndarray, mask: np.ndarray, scene) -> np.ndarray:
    vis = _dim(img)
    h_inv = scene.H_px_to_mm
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    raw_total = poly_total = 0
    for c in contours:
        raw_mm = geometry.apply(h_inv, c.reshape(-1, 2)).astype(np.float32)
        perim = float(cv2.arcLength(raw_mm, True))
        if perim <= 1e-6:
            continue
        poly = cv2.approxPolyDP(raw_mm, vdetect.EPS_FRAC * perim, True)
        if len(poly) < 3:
            continue
        raw_total += len(c)
        poly_total += len(poly)
        px = geometry.apply(scene.H_mm_to_px, poly.reshape(-1, 2))
        cv2.polylines(vis, [np.round(px).astype(np.int32)], True, GREEN, 2)
        for x, y in px:
            cv2.circle(vis, (int(round(x)), int(round(y))), 3, (255, 220, 90), -1)
    _label(vis, f"轮廓点 {raw_total} → 抽稀后 {poly_total}（eps = 1% 周长）")
    return vis[:, :, ::-1]


def _label(img: np.ndarray, text: str) -> None:
    cv2.putText(img, text, (12, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                (0, 0, 0), 4, cv2.LINE_AA)
    cv2.putText(img, text, (12, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                (235, 240, 245), 1, cv2.LINE_AA)


# --------------------------------------------------------------------------
# 逐件判定链
# --------------------------------------------------------------------------

def build_payload(cfg, scene, dets, book, thresholds: dict | None = None) -> dict:
    h_inv = scene.H_px_to_mm
    pairs = {idx: (err, truth)
             for idx, err, truth in vdetect.position_error_mm(dets, scene.items)}
    # position_error_mm 只回传真值对象，没回传它的下标；按对象身份反查
    truth_index = {id(it): i for i, it in enumerate(scene.items)}
    matched = {truth_index[id(t)] for _, t in pairs.values()}

    rows = []
    for d in dets:
        err, truth = pairs.get(d.idx, (None, None))
        raw_px = d.contour_px.reshape(-1, 2)
        raw_mm = geometry.apply(h_inv, raw_px).astype(np.float32)
        perim_raw = float(cv2.arcLength(raw_mm, True))
        area_raw = float(cv2.contourArea(raw_mm))
        circ_raw = (4.0 * math.pi * area_raw / (perim_raw * perim_raw)
                    if perim_raw > 1e-9 else 0.0)

        cx_px, cy_px = geometry.apply(scene.H_mm_to_px, [[d.cx_mm, d.cy_mm]])[0]
        rows.append({
            "idx": d.idx,
            "label": d.name,
            "ok": bool(d.ok),
            "shape": d.verdict.shape,
            "color": d.verdict.color,
            "confidence": round(float(d.verdict.confidence), 3),
            "margin": round(float(d.verdict.shape_margin), 3),
            "reason": d.verdict.reason,
            # -- 轮廓与抽稀 --
            "raw_pts": int(len(raw_px)),
            "poly_pts": int(d.vertices),
            "circ_raw": round(circ_raw, 3),
            "circ_poly": round(float(d.circularity), 3),
            # -- 形状打分（与 judge_shape 同一个函数）--
            "shape_scores": [
                {"key": k, "label": book.shapes[k].label,
                 "theory": book.shapes[k].circularity,
                 "range": list(book.shapes[k].vertices),
                 "dist": round(float(dd), 3), "in_range": bool(ir),
                 "chosen": k == d.verdict.shape}
                for dd, k, ir in book.shape_scores(d.circularity, d.vertices)
            ],
            # -- 颜色 --
            "hue": round(float(d.hue), 1),
            "sat": round(float(d.sat), 1),
            "val": round(float(d.val), 1),
            "color_hits": [
                {"name": c.name, "hue": list(c.hue),
                 "wrap": list(c.hue_wrap) if c.hue_wrap else None,
                 "min_s": c.min_s, "min_v": c.min_v,
                 "hit": bool(c.contains(d.hue) and d.sat >= c.min_s and d.val >= c.min_v)}
                for c in book.colors
            ],
            # -- 尺寸 --
            "area_mm2": round(float(d.area_mm2), 1),
            "perim_mm": round(float(d.perimeter_mm), 1),
            "width_mm": round(float(d.width_mm), 2),
            "fill": round(float(d.fill), 3),
            "aspect": round(float(d.aspect), 3),
            # -- 坐标链 --
            "cx_px": round(float(cx_px), 1),
            "cy_px": round(float(cy_px), 1),
            "cx_mm": round(float(d.cx_mm), 2),
            "cy_mm": round(float(d.cy_mm), 2),
            "machine_xy": [round(cfg.tray_x0_mm + float(d.cx_mm), 2),
                           round(cfg.tray_y0_mm + float(d.cy_mm), 2)],
            # -- 真值比对 --
            "truth": None if truth is None else {
                "name": truth.name, "shape": truth.shape,
                "color": truth.color, "class_id": truth.class_id},
            "err_mm": None if err is None else round(float(err), 3),
            "match_shape": None if truth is None else d.verdict.shape == truth.shape,
            "match_color": None if truth is None else d.verdict.color == truth.color,
        })

    try:
        summary = evaluate(dets, scene)
        summary = {k: (round(v, 3) if isinstance(v, float) else v)
                   for k, v in summary.items()}
    except Exception:                       # 真机上没有真值，这一块自然为空
        summary = {}

    return {
        "rows": rows,
        "stages": [dict(s) for s in STAGES],
        "code": code_index(),
        "truth_total": len(scene.items),
        "missed": [{"name": it.name, "shape": it.shape, "color": it.color,
                    "cx_mm": round(it.cx_mm, 2), "cy_mm": round(it.cy_mm, 2)}
                   for i, it in enumerate(scene.items) if i not in matched],
        "summary": summary,
        "thresholds": dict(thresholds or book.thresholds),
        "source": "合成托盘图（真值由 synthetic.build 生成）",
    }


# --------------------------------------------------------------------------
# 真实源代码（从磁盘现读）
# --------------------------------------------------------------------------

CODE_TARGETS = {
    "segment": (vdetect.segment, "detect.py · segment()"),
    "find_items": (vdetect.find_items, "detect.py · find_items()"),
    "judge": (vclasses.ClassBook.judge, "classes.py · judge()"),
    "judge_shape": (vclasses.ClassBook.judge_shape, "classes.py · judge_shape()"),
    "shape_scores": (vclasses.ClassBook.shape_scores, "classes.py · shape_scores()"),
    "hue_mode": (vclasses.hue_mode, "classes.py · hue_mode()"),
}


def code_index() -> dict:
    """把界面上要展示的源码从磁盘现读一份。

    用 `inspect.getsourcelines` 而不是 `open(文件)`：拿到的是**当前进程里真正
    在跑的那个函数对象**对应的源码，路径和行号都跟着走，抄错/漏改不可能发生。
    """
    out = {}
    for key, (fn, title) in CODE_TARGETS.items():
        lines, start = inspect.getsourcelines(fn)
        path = Path(inspect.getsourcefile(fn)).resolve()
        try:
            rel = path.relative_to(ROOT).as_posix()
        except ValueError:
            rel = path.name
        out[key] = {"title": title, "path": rel, "start": start,
                    "lines": [ln.rstrip("\n") for ln in lines]}
    return out
