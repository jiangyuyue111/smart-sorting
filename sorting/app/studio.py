"""本地网页工作台：把整个软件层做成一个能看、能点的产品界面。

    python -m sorting.app.studio                # 仿真，浏览器自动打开
    python -m sorting.app.studio --port COM3    # 接实物（STM32 串口）

为什么做这个东西
----------------
中期汇报时，"跑一条命令打印一堆数字"对听众是不友好的；实物到位后，
调试时又需要看见机器人在哪、这一步在干什么。两者要的是同一个东西：
**一个把内部状态摊开给人看的界面**。所以做一个，而不是做两个。

黑色盒子是被明确拒绝的，所以界面上有一块"原理 · 内部实况"：
当前发出的协议行、四个轴的位置与步数换算、夹爪判据的实测值与阈值、
像素→毫米→整机坐标这条换算链的中间结果、三种排程策略的代价对比。
看起来在动的东西，每一处都能对上代码里的一行。

一条时间轴，两种驱动
--------------------
动画不需要知道底下是模拟还是真板子。`RecordingLink` 包在任意 Link 外面，
把"发了哪条指令、从哪走到哪、花了多久"记成一条时间轴：

    预演    MockLink speed=0，整条时间轴瞬间算完，一次性交给浏览器，可随意拖
    实机    SerialLink，边跑边把新产生的段落推给浏览器（SSE），跟着真机走

两边是**同一段动画代码**，所以"预演里看到的"和"实物上看到的"不会变成两回事。
录下来的耗时同时被用来校验：仿真时它必须和 MockLink 的解析时间轴一致，
`--selftest` 会断言这一点。

产物
----
    GET  /                 工作台页面（单文件）
    GET  /recognition      识别实况页面：七张中间图 + 逐件判定链 + 真实源码
    GET  /api/scene        当前场景 + 完整时间轴（JSON）
    GET  /api/reload       改参数重算（seed / per_class / 各轴速度 / z_safe / inject）
    GET  /api/recognition  逐件判定链（可带 min_confidence / min_margin / min_area_mm2）
    GET  /api/stage.png    某一环的中间图（?k=01..07）
    GET  /api/screen.png   由 renderer.py 真实渲染的 480×320 屏幕帧
    GET  /api/tray.png     合成相机原图
    GET  /api/thumb.png    单件缩略图
    GET  /api/live/events  实跑事件流（SSE）
"""

from __future__ import annotations

import argparse
import copy
import json
import queue
import sys
import threading
import time
import webbrowser
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from PIL import Image

from .. import config as cfgmod
from ..core import jobqueue
from ..core import protocol as proto
from ..core.link import Link, MockLink, SerialLink
from ..display import renderer
from ..vision import classes as vclasses
from ..vision import detect as vdetect
from ..vision import geometry
from ..vision import synthetic
from . import recog
from . import runner
from .demo import crop_thumb, evaluate

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "sorting" / "config"
WEB_DIR = ROOT / "sorting" / "web"
DEFAULT_CONFIG = CONFIG_DIR / "machine.toml"
DEFAULT_CLASSES = CONFIG_DIR / "classes.json"
PAGE = WEB_DIR / "studio.html"
RECOG_PAGE = WEB_DIR / "recognition.html"

# 页面配色用。只影响画法，不参与任何计算。
COLOR_HEX = {"红": "#e2594c", "蓝": "#4a86e0"}
FALLBACK_HEX = "#8a93a6"

MAX_POLY_POINTS = 18          # 轮廓抽稀到这么多点就够画了，别把 JSON 撑大
LINGER_S = 0.8                # "已入盒"结果帧在屏上停留多久，纯观感


# ==========================================================================
# 录制层：任意 Link -> 一条时间轴
# ==========================================================================

class RecordingLink(Link):
    """把任意链路上发生的动作录成 `[axis, from, to, t0, dur]` 四元组。

    为什么不直接用 MockLink 自带的 SessionLog：**真串口没有解析时间轴**，
    只有墙上时间。把记录做在 Link 外面，预演和实机就能共用同一段动画，
    也就不用维护两套"看起来像在动"的代码——那种代码一定会讲两种故事。

    转发规则
    --------
    `__getattr__` 只把 inner **确实有**的属性暴露出去，不伪造。这一点很关键：
    `runner` 里用 `hasattr(link, "blocker_mm")` 判断"这支链路能不能模拟堵转"，
    如果这里无脑给一个 blocker_mm，真串口上也会走模拟分支，夹爪判据就假了。
    """

    _FIELDS = frozenset({
        "inner", "cfg", "motions", "commands", "lock", "version",
        "t_wall", "pos", "grip_idx",
    })

    def __init__(self, inner: Link, cfg: cfgmod.Config) -> None:
        object.__setattr__(self, "inner", inner)
        object.__setattr__(self, "cfg", cfg)
        object.__setattr__(self, "motions", [])
        object.__setattr__(self, "commands", [])
        object.__setattr__(self, "lock", threading.Lock())
        object.__setattr__(self, "version", 0)
        object.__setattr__(self, "t_wall", time.monotonic())
        object.__setattr__(self, "pos", {"x": 0.0, "y": 0.0, "z": 0.0, "grip": 0.0})
        object.__setattr__(self, "grip_idx", None)

    # -- 属性转发 --------------------------------------------------------
    def __getattr__(self, name: str):
        if name == "inner":
            raise AttributeError(name)
        return getattr(self.inner, name)

    def __setattr__(self, name: str, value) -> None:
        if name in self._FIELDS:
            object.__setattr__(self, name, value)
        else:
            setattr(self.inner, name, value)

    # -- 时间 ------------------------------------------------------------
    def _now(self) -> float:
        el = getattr(self.inner, "elapsed_s", None)
        if el is not None:
            return float(el)
        return time.monotonic() - self.t_wall

    # -- Link 接口 -------------------------------------------------------
    def send(self, line: str) -> None:
        t = self._now()
        with self.lock:
            n_m, n_c = len(self.motions), len(self.commands)
            self.commands.append({"t": round(t, 4), "line": line})
            self._record(line, t)
            self.version += 1
        try:
            self.inner.send(line)
        except Exception:
            with self.lock:
                del self.motions[n_m:]
                del self.commands[n_c:]
                self._reflow()
            raise

    def poll(self, timeout: float):
        msg = self.inner.poll(timeout)
        if isinstance(msg, proto.GripReport):
            self._correct_grip(float(msg.actual_mm))
        return msg

    def close(self) -> None:
        self.inner.close()

    # -- 录制 ------------------------------------------------------------
    def _add(self, axis: str, target: float, t: float, dur: float | None = None) -> int:
        ax = self.cfg.axes[axis]
        start = self.pos[axis]
        if dur is None:
            dur = abs(target - start) / ax.speed_mm_s if ax.speed_mm_s > 0 else 0.0
        self.motions.append([axis, round(start, 3), round(target, 3),
                             round(t, 4), round(dur, 4)])
        self.pos[axis] = target
        return len(self.motions) - 1

    def _record(self, line: str, t: float) -> None:
        try:
            msg = proto.parse(line)
        except proto.ProtocolError:
            return                       # 非法行不产生运动，MCU 会回 ERR

        if isinstance(msg, proto.Goto):
            self._add("x", msg.x_mm, t)      # XY 并行，两段 t0 相同
            self._add("y", msg.y_mm, t)
        elif isinstance(msg, proto.ZMove):
            self._add("z", msg.mm, t)
        elif isinstance(msg, proto.Grip):
            actual = msg.mm
            blocker = getattr(self.inner, "blocker_mm", None)
            if blocker is not None and msg.mm < blocker < self.pos["grip"]:
                actual = float(blocker)      # 堵转：夹到工件，停在工件宽度处
            self.grip_idx = self._add("grip", actual, t)
        elif isinstance(msg, proto.Home):
            dur = float(getattr(self.inner, "homing_s", 6.0))
            for a in ("x", "y", "z", "grip"):
                self._add(a, 0.0, t, dur)

    def _correct_grip(self, actual: float) -> None:
        """GRIPD 到了之后用真实停位修正那一段——真串口上这是唯一的信息来源。"""
        with self.lock:
            i = self.grip_idx
            if i is None or i >= len(self.motions):
                return
            m = self.motions[i]
            if abs(m[2] - actual) < 1e-6:
                return
            ax = self.cfg.axes["grip"]
            dur = abs(actual - m[1]) / ax.speed_mm_s if ax.speed_mm_s > 0 else 0.0
            self.motions[i] = [m[0], m[1], round(actual, 3), m[3], round(dur, 4)]
            self._reflow()
            self.version += 1

    def _reflow(self) -> None:
        self.pos = {"x": 0.0, "y": 0.0, "z": 0.0, "grip": 0.0}
        for m in self.motions:
            self.pos[m[0]] = m[2]

    # -- 给界面用 --------------------------------------------------------
    def snapshot(self, since_motion: int = 0, since_cmd: int = 0) -> dict:
        with self.lock:
            return {
                "motions": [list(m) for m in self.motions[since_motion:]],
                "commands": [dict(c) for c in self.commands[since_cmd:]],
                "n_motions": len(self.motions),
                "n_commands": len(self.commands),
                "t": round(self._now(), 3),
                "pos": dict(self.pos),
                "version": self.version,
            }


# ==========================================================================
# 单轮仿真
# ==========================================================================

def _hold_window(rec: runner.ItemRecord, cfg: cfgmod.Config
                 ) -> tuple[float | None, float | None]:
    """这件工件"在夹爪里"的时间窗，用来画被搬走的东西。

    取"夹紧那一步做完"到"到料盒松开那一步做完"。中间隔着抬 Z、平移、下探，
    这段时间夹爪里确实有件——把它画出来，看的人才知道东西去哪了。
    """
    closed = None
    opened = None
    for i, s in enumerate(rec.steps):
        if s.kind != "grip":
            continue
        nxt = rec.steps[i + 1].t_s if i + 1 < len(rec.steps) else rec.t_end_s
        if abs(s.args[0] - cfg.grip_close_mm) < 1e-6:
            closed = nxt
        elif abs(s.args[0] - cfg.grip_open_mm) < 1e-6 and closed is not None:
            opened = nxt
    return closed, opened


def _steps_out(rec: runner.ItemRecord) -> list[dict]:
    """把这一件的动作序列摊平给界面。

    工艺顺序写在 sequence.py 里，界面上要能一步步对上——否则"机器在动"
    就成了一团看不懂的东西。args 里可能有 Target 对象，只留数字。
    """
    out = []
    for s in rec.steps:
        nums = [round(float(a), 2) for a in s.args if isinstance(a, (int, float))]
        out.append({"k": s.kind, "a": nums, "t": round(s.t_s, 3)})
    return out


def _poly_machine(det, cfg: cfgmod.Config, H_px_to_mm, limit: int = MAX_POLY_POINTS):
    """把像素轮廓映射成整机毫米坐标下的多边形，给界面直接画。"""
    if getattr(det, "contour_px", None) is None:
        return []
    pts = det.contour_px.reshape(-1, 2).astype(float)
    if len(pts) > limit:
        step = len(pts) / limit
        pts = pts[[int(i * step) for i in range(limit)]]
    mm = geometry.apply(H_px_to_mm, pts)
    return [[round(cfg.tray_x0_mm + float(p[0]), 2),
             round(cfg.tray_y0_mm + float(p[1]), 2)] for p in mm]


def simulate(cfg: cfgmod.Config, plan, inject: set[int], speed: float = 0.0):
    """用模拟 MCU 跑完整轮，返回录制链路和运行结果。"""
    link = RecordingLink(MockLink(axes=cfg.axes, speed_factor=speed), cfg)
    runner.home(link, cfg, speed)
    res = runner.execute(plan, link, cfg, speed_factor=speed,
                         inject_skip=set(inject))
    return link, res


# ==========================================================================
# 工作台状态
# ==========================================================================

def _num(q: dict, key: str, default):
    v = q.get(key)
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _id_list(q: dict, key: str) -> set[int]:
    raw = q.get(key) or ""
    if isinstance(raw, (list, tuple)):
        raw = ",".join(str(x) for x in raw)
    out: set[int] = set()
    for piece in str(raw).split(","):
        piece = piece.strip()
        if piece:
            try:
                out.add(int(piece))
            except ValueError:
                pass
    return out


def apply_overrides(cfg: cfgmod.Config, q: dict) -> cfgmod.Config:
    """把界面上的几个旋钮翻译成 Config。改不动的参数这里不提供。"""
    axes = dict(cfg.axes)
    for name in ("x", "y", "z", "grip"):
        v = _num(q, f"{name}_speed", None)
        if v is not None and v > 0:
            axes[name] = replace(axes[name], speed_mm_s=float(v))
    out = replace(cfg, axes=axes,
                  x=axes["x"], y=axes["y"], z=axes["z"], grip=axes["grip"])
    zs = _num(q, "z_safe", None)
    if zs is not None:
        out = replace(out, z_safe_mm=float(zs))
    cfgmod.validate(out)
    return out


THRESHOLD_KEYS = ("min_confidence", "min_margin", "min_area_mm2")


def _book_with(base, q: dict):
    """复制一份类别簿，只换判据阈值。原件不许被界面改动。"""
    book = copy.copy(base)
    book.thresholds = dict(base.thresholds)
    for key in THRESHOLD_KEYS:
        v = _num(q, key, None)
        if v is not None:
            book.thresholds[key] = float(v)
    return book


class Studio:
    """一份场景 + 一份时间轴。单用户本地服务，没必要上并发保护之外的复杂度。"""

    def __init__(self, config_path, classes_path, mode: str = "sim",
                 serial_port: str | None = None, baud: int = 115200) -> None:
        self.config_path = Path(config_path)
        self.classes_path = Path(classes_path)
        self.base_cfg = cfgmod.load(self.config_path)
        self.book = vclasses.load(self.classes_path)
        self.mode = mode
        self.serial_port = serial_port
        self.baud = baud
        self.live: LiveRunner | None = None
        self.lock = threading.Lock()
        self.state: dict = {}
        self._thumbs: dict[int, Image.Image] = {}
        self._tray: Image.Image | None = None
        self._screen_cache: dict[tuple, bytes] = {}
        self._cfg: cfgmod.Config | None = None
        self._scene = None
        self._stage_cache: dict[str, bytes] = {}
        self._recog_ctx = None          # (scene, dets, book) 最近一次识别所用的上下文
        self.reload({})

    # -- 重建 ------------------------------------------------------------
    def reload(self, q: dict) -> dict:
        cfg = apply_overrides(self.base_cfg, q)
        seed = int(_num(q, "seed", 0))
        per = int(_num(q, "per_class", 2))
        inject = _id_list(q, "inject")

        scene = synthetic.build(self.book, seed=seed, per_class=per,
                                tray_w=cfg.tray_w_mm, tray_h=cfg.tray_h_mm)
        dets = vdetect.find_items(scene.image, scene.H_px_to_mm, self.book)
        plan = jobqueue.build(dets, cfg)

        link = res = None
        if plan.targets:
            link, res = simulate(cfg, plan, inject)

        state = self._payload(cfg, scene, dets, plan, link, res, inject, seed, per)
        with self.lock:
            self.state = state
            self._cfg = cfg
            self._scene = scene
            self._thumbs = {d.idx: crop_thumb(scene.image, d) for d in dets}
            self._tray = Image.fromarray(scene.image[:, :, ::-1])
            self._screen_cache = {}
            self._stage_cache = {}
        return state

    def _payload(self, cfg, scene, dets, plan, link, res, inject, seed, per) -> dict:
        items: list[dict] = []
        motions: list[list] = []
        commands: list[dict] = []

        if res is not None:
            motions = [list(m) for m in link.motions]
            commands = [dict(c) for c in link.commands]
            for rec in res.items:
                tgt = plan.targets[rec.pos - 1]
                hf, ht = _hold_window(rec, cfg)
                items.append({
                    "pos": rec.pos,
                    "index": rec.index,
                    "name": rec.name,
                    "category": rec.category,
                    "held": rec.held,
                    "t_start": round(rec.t_start_s, 3),
                    "t_end": round(rec.t_end_s, 3),
                    "hold_from": None if hf is None else round(hf, 3),
                    "hold_to": None if ht is None else round(ht, 3),
                    "active_from": None if not rec.held else round(hf or rec.t_start_s, 3),
                    "active_to": None if not rec.held else round(ht or rec.t_end_s, 3),
                    "pick": [round(tgt.pick_xy[0], 2), round(tgt.pick_xy[1], 2)],
                    "bin": [round(tgt.bin_xy[0], 2), round(tgt.bin_xy[1], 2)],
                    "width_mm": round(tgt.width_mm, 2),
                    "injected": rec.index in inject,
                    "steps": _steps_out(rec),
                })

        # -- 检测明细（含像素坐标和多边形，界面要展示换算链）------------
        det_rows = []
        for d in dets:
            pts = d.contour_px.reshape(-1, 2) if getattr(d, "contour_px", None) is not None else None
            cx_px = float(pts[:, 0].mean()) if pts is not None else 0.0
            cy_px = float(pts[:, 1].mean()) if pts is not None else 0.0
            cls = d.verdict.cls
            det_rows.append({
                "idx": d.idx,
                "ok": bool(d.ok),
                "label": cls.name if cls else "未识别",
                "category": cls.id if cls else None,
                "shape": d.verdict.shape,
                "color": d.verdict.color,
                "reason": d.verdict.reason,
                "cx_px": round(cx_px, 1),
                "cy_px": round(cy_px, 1),
                "cx_mm": round(float(d.cx_mm), 2),
                "cy_mm": round(float(d.cy_mm), 2),
                "machine_xy": [round(cfg.tray_x0_mm + float(d.cx_mm), 2),
                               round(cfg.tray_y0_mm + float(d.cy_mm), 2)],
                "width_mm": round(float(d.width_mm), 2),
                "hex": COLOR_HEX.get(d.verdict.color or "", FALLBACK_HEX),
                "poly": _poly_machine(d, cfg, scene.H_px_to_mm),
            })

        try:
            stats = evaluate(dets, scene)
        except Exception:                      # 合成真值配对失败不该影响界面
            stats = {}

        return {
            "mode": self.mode,
            "seed": seed,
            "per_class": per,
            "inject": sorted(inject),
            "calibrated": cfg.meta.calibrated,
            "qualifier": cfg.meta.qualifier,
            "machine": {"x_max": cfg.x.max_mm, "y_max": cfg.y.max_mm},
            "tray": {"x0": cfg.tray_x0_mm, "y0": cfg.tray_y0_mm,
                     "w": cfg.tray_w_mm, "h": cfg.tray_h_mm},
            "bins": {k: list(v) for k, v in cfg.bins.items()},
            "axes": {n: {"speed": ax.speed_mm_s, "steps_per_mm": round(ax.steps_per_mm, 3),
                         "max_mm": ax.max_mm, "module": ax.module_mm, "teeth": ax.teeth,
                         "reduction": ax.reduction, "steps_per_rev": ax.steps_per_rev,
                         "microstep": ax.microstep, "mm_per_rev": round(ax.mm_per_rev, 3)}
                     for n, ax in cfg.axes.items()},
            "process": {
                "z_pick": cfg.z_pick_mm, "z_drop": cfg.z_drop_mm, "z_safe": cfg.z_safe_mm,
                "grip_open": cfg.grip_open_mm, "grip_close": cfg.grip_close_mm,
                "grip_hold_min": cfg.grip_hold_min_mm, "grip_settle": cfg.grip_settle_s,
            },
            "detections": det_rows,
            "items": items,
            "skipped": [{"idx": s.idx, "reason": s.reason,
                         "cx_mm": round(s.cx_mm, 2), "cy_mm": round(s.cy_mm, 2)}
                        for s in plan.skipped],
            "plan": {"strategy": plan.strategy, "cost_mm": round(plan.cost_mm, 1),
                     "costs": {k: round(v, 1) for k, v in plan.costs.items()}},
            "motions": motions,
            "commands": commands,
            "total_s": round(res.total_s, 3) if res else 0.0,
            "idle_s": round(res.idle_s, 3) if res else 0.0,
            "placed": res.placed if res else 0,
            "skipped_n": res.skipped if res else 0,
            "stats": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in stats.items()},
            "source": ("合成托盘图 + 模拟 MCU" if self.mode == "sim"
                       else f"真机串口 {self.serial_port}"),
        }

    # -- 屏幕帧 ----------------------------------------------------------
    def screen_png(self, pos: int, phase: str) -> bytes:
        with self.lock:
            state = self.state
            items = state.get("items", [])
            key = (pos, phase)
            if key in self._screen_cache:
                return self._screen_cache[key]

            total = len(items)
            model = self._display_model(items, total, pos, phase)
            buf = BytesIO()
            renderer.render(model).save(buf, format="PNG")
            data = buf.getvalue()
            self._screen_cache[key] = data
            return data

    def _display_model(self, items, total, pos, phase) -> renderer.DisplayModel:
        def tally_upto(n: int) -> dict[str, int]:
            out: dict[str, int] = {}
            for it in items[:n]:
                if it["held"]:
                    out[it["category"]] = out.get(it["category"], 0) + 1
            return out

        if not items:
            return renderer.DisplayModel(index=1, name="-", total=0, status="READY")

        if phase == "done":
            return renderer.DisplayModel(index=total, name="完成", total=total,
                                         done=total, status="DONE",
                                         tally=tally_upto(total))
        pos = max(1, min(pos, total))
        it = items[pos - 1]
        thumb = self._thumbs.get(it["index"])
        if phase == "ready":
            return renderer.DisplayModel(index=1, name=items[0]["name"], total=total,
                                         done=0, thumb=self._thumbs.get(items[0]["index"]),
                                         status="READY", tally={})
        if phase in ("placed", "skip"):
            return renderer.DisplayModel(index=pos, name=it["name"], total=total,
                                         done=pos, thumb=thumb,
                                         status="PLACED" if phase == "placed" else "SKIP",
                                         tally=tally_upto(pos))
        return renderer.DisplayModel(index=pos, name=it["name"], total=total,
                                     done=pos - 1, thumb=thumb, status="MOVING",
                                     tally=tally_upto(pos - 1))

    def tray_png(self) -> bytes:
        with self.lock:
            img = self._tray
        if img is None:
            return b""
        buf = BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def thumb_png(self, det_idx: int) -> bytes:
        with self.lock:
            img = self._thumbs.get(det_idx)
        if img is None:
            return b""
        buf = BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    # -- 识别实况 --------------------------------------------------------
    def recognition(self, q: dict | None = None) -> dict:
        """重跑一遍识别并给出逐件判定链。

        允许临时改判据阈值（min_confidence / min_margin / min_area_mm2），
        用来现场演示"阈值一动，结论就变"——这也是答辩时"现场可调"那句话的
        证据，而不是一句口号。
        """
        q = q or {}
        with self.lock:
            scene = self._scene
            cfg = self._cfg
            base_book = self.book
        if scene is None or cfg is None:
            return {"rows": [], "stages": [], "code": {}, "summary": {}}

        book = _book_with(base_book, q)
        dets = vdetect.find_items(scene.image, scene.H_px_to_mm, book)
        payload = recog.build_payload(cfg, scene, dets, book)
        with self.lock:
            self._recog_ctx = (scene, dets, book)
            self._stage_cache = {}          # 判定变了，第 07 张图也得重画
        return payload

    def stage_png(self, key: str, q: dict | None = None) -> bytes:
        with self.lock:
            cached = self._stage_cache.get(key)
            ctx = self._recog_ctx
            scene = self._scene
        if cached is not None:
            return cached
        if ctx is None:                     # 还没人调过 /api/recognition，先算一遍
            self.recognition(q)
            with self.lock:
                ctx = self._recog_ctx
        if ctx is None or scene is None:
            return b""
        _scene, dets, _book = ctx
        png = recog.stage_png(_scene, dets, key)
        with self.lock:
            self._stage_cache[key] = png
        return png

    # -- 实跑 ------------------------------------------------------------
    def start_live(self, q: dict) -> dict:
        if self.live is not None and self.live.alive:
            return {"ok": False, "error": "已经有一轮在跑"}
        with self.lock:
            plan_state = dict(self.state)
        inject = _id_list(q, "inject") or set(plan_state.get("inject", []))
        speed = _num(q, "speed", 1.0) or 1.0
        self.live = LiveRunner(self, inject=inject, speed=speed)
        self.live.start()
        return {"ok": True}


# ==========================================================================
# 实跑驱动
# ==========================================================================

class LiveRunner:
    """在后台线程里真跑一轮，把新发生的动作推给 SSE 客户端。

    和预演走的是同一个 `runner.execute`、同一个 `RecordingLink`，
    差别只在速度系数和"边跑边推"。
    """

    def __init__(self, studio: Studio, inject: set[int], speed: float) -> None:
        self.studio = studio
        self.inject = set(inject)
        self.speed = float(speed) if speed > 0 else 1.0
        self.link: RecordingLink | None = None
        self.error: str | None = None
        self.items: list[dict] = []
        self.done = threading.Event()
        self._thread = threading.Thread(target=self._run, name="studio-live", daemon=True)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def start(self) -> None:
        self._thread.start()

    def _make_link(self, cfg) -> Link:
        if self.studio.mode == "serial" and self.studio.serial_port:
            return SerialLink(self.studio.serial_port, self.studio.baud)
        return MockLink(axes=cfg.axes, speed_factor=self.speed)

    def _run(self) -> None:
        st = self.studio
        try:
            cfg = st.base_cfg
            state = st.state
            # 用当前场景的排程重跑：重新构造 Target 需要 Config，直接从 payload 还原
            plan = _plan_from_state(state, cfg, st.book)
            if not plan.targets:
                self.error = "队列为空"
                return
            link = RecordingLink(self._make_link(cfg), cfg)
            self.link = link
            runner.home(link, cfg, self.speed)

            def on_display(pos, rec, target):
                hf, ht = _hold_window(rec, cfg)
                self.items.append({
                    "pos": pos, "index": rec.index, "name": rec.name,
                    "category": rec.category, "held": rec.held,
                    "t_start": round(rec.t_start_s, 3), "t_end": round(rec.t_end_s, 3),
                    "hold_from": hf, "hold_to": ht,
                    "pick": [round(target.pick_xy[0], 2), round(target.pick_xy[1], 2)],
                    "bin": [round(target.bin_xy[0], 2), round(target.bin_xy[1], 2)],
                    "injected": rec.index in self.inject,
                    "steps": _steps_out(rec),
                })

            res = runner.execute(plan, link, cfg, speed_factor=self.speed,
                                 inject_skip=self.inject, on_display=on_display)
            self.result_s = res.total_s
        except Exception as exc:                      # noqa: BLE001 —— 界面要看到原因
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.done.set()

    def stream(self):
        """SSE 生成器：新动作、新工件、心跳、结束。"""
        sent_m = sent_c = 0
        sent_items = 0
        while True:
            link = self.link
            payload: dict = {}
            if link is not None:
                snap = link.snapshot(sent_m, sent_c)
                sent_m, sent_c = snap["n_motions"], snap["n_commands"]
                if snap["motions"]:
                    payload["motions"] = snap["motions"]
                if snap["commands"]:
                    payload["commands"] = snap["commands"]
                payload["t"] = snap["t"]
                payload["pos"] = snap["pos"]
            if len(self.items) > sent_items:
                payload["items"] = self.items[sent_items:]
                sent_items = len(self.items)
            if payload:
                yield payload
            if self.done.is_set():
                yield {"end": True, "error": self.error,
                       "total_s": round(getattr(self, "result_s", 0.0), 3)}
                return
            time.sleep(0.05)


def _plan_from_state(state: dict, cfg: cfgmod.Config, book) -> jobqueue.Plan:
    """把 payload 里的队列还原成 Plan，供实跑复用同一份排程。"""
    targets = []
    for it in state.get("items", []):
        targets.append(cfgmod.Target(
            index=it["index"], label=it["name"], category=it["category"],
            pick_xy=tuple(it["pick"]), cfg=cfg, width_mm=float(it.get("width_mm", 0.0)),
        ))
    return jobqueue.Plan(targets=targets, cost_mm=state["plan"]["cost_mm"],
                         strategy=state["plan"]["strategy"],
                         costs=state["plan"]["costs"])


# ==========================================================================
# HTTP
# ==========================================================================

class Handler(BaseHTTPRequestHandler):
    studio: Studio = None            # 启动时注入
    base: str = ""                   # 挂在子路径下时（如 /sort）先把这个前缀剥掉
    server_version = "SortingStudio/1.0"

    def log_message(self, fmt, *args):       # 别把控制台刷满
        pass

    # -- 响应工具 --------------------------------------------------------
    def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(body, "application/json; charset=utf-8", code)

    def _error(self, text: str, code: int = 400) -> None:
        self._json({"ok": False, "error": text}, code)

    # -- 路由 ------------------------------------------------------------
    def do_GET(self) -> None:
        u = urlparse(self.path)
        q = {k: v[-1] for k, v in parse_qs(u.query).items()}
        path = u.path

        # 挂在子路径下（隧道/反代转发 /sort/* 时用）。页面里的链接一律写成
        # 相对路径，所以剥掉前缀就能照常工作——不需要知道自己在哪个前缀下。
        if self.base:
            if path == self.base:
                self.send_response(301)
                self.send_header("Location", self.base + "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path.startswith(self.base + "/"):
                path = path[len(self.base):]

        try:
            if path in ("/", "/index.html"):
                self._send(PAGE.read_bytes(), "text/html; charset=utf-8")
            elif path in ("/recognition", "/recognition.html"):
                self._send(RECOG_PAGE.read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/scene":
                self._json(self.studio.state)
            elif path == "/api/recognition":
                self._json(self.studio.recognition(q))
            elif path == "/api/stage.png":
                self._send(self.studio.stage_png(q.get("k", "01"), q), "image/png")
            elif path == "/api/reload":
                self._json(self.studio.reload(q))
            elif path == "/api/tray.png":
                self._send(self.studio.tray_png(), "image/png")
            elif path == "/api/thumb.png":
                self._send(self.studio.thumb_png(int(_num(q, "i", -1))), "image/png")
            elif path == "/api/screen.png":
                pos = int(_num(q, "pos", 1))
                phase = q.get("phase", "ready")
                self._send(self.studio.screen_png(pos, phase), "image/png")
            elif path == "/api/live/start":
                self._json(self.studio.start_live(q))
            elif path == "/api/live/events":
                self._sse()
            elif path == "/favicon.ico":
                self._send(b"", "image/x-icon")
            else:
                self._error("没有这个路径", 404)
        except cfgmod.ConfigError as exc:
            self._error(str(exc), 400)
        except Exception as exc:                      # noqa: BLE001
            self._error(f"{type(exc).__name__}: {exc}", 500)

    def do_POST(self) -> None:
        self.do_GET()

    def _sse(self) -> None:
        runner_ = self.studio.live
        if runner_ is None:
            self._error("没有正在跑的轮次", 409)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            for ev in runner_.stream():
                chunk = f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8")
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


# ==========================================================================
# 自检 + 入口
# ==========================================================================

def selftest(config_path, classes_path) -> int:
    """验证录制链路复现出的时间轴与 MockLink 的解析时间轴一致。

    这条断言是整个界面的信任基础：如果动画时间轴和报告里的节拍不是同一个数，
    那么"看着在动"就成了另一套故事。
    """
    cfg = cfgmod.load(config_path)
    book = vclasses.load(classes_path)
    scene = synthetic.build(book, seed=0, per_class=2,
                            tray_w=cfg.tray_w_mm, tray_h=cfg.tray_h_mm)
    dets = vdetect.find_items(scene.image, scene.H_px_to_mm, book)
    plan = jobqueue.build(dets, cfg)
    link, res = simulate(cfg, plan, set())

    if not link.motions:
        print("✗ 没录到任何动作")
        return 1

    # 每段运动的结束时刻取最大值，就是时间轴的总长（不含末端等待）
    # 容差 1 ms：时间轴落盘时按 4 位小数取整，不是模型误差
    end = max(m[3] + m[4] for m in link.motions)
    diff = abs(end - res.total_s)
    print(f"  录制时间轴 {end:.3f} s　运行结果 {res.total_s:.3f} s　差 {diff * 1000:.2f} ms")
    if diff > 1e-3:
        print("✗ 时间轴和运行结果对不上")
        return 1

    # 四轴终点必须等于最后一帧的指令位置
    for axis in ("x", "y", "z", "grip"):
        last = [m for m in link.motions if m[0] == axis]
        if last and abs(link.pos[axis] - last[-1][2]) > 1e-6:
            print(f"✗ {axis} 轴位置回放不一致")
            return 1

    # 屏幕帧必须真能渲染出来，且四要素齐全
    studio = Studio(config_path, classes_path)
    png = studio.screen_png(1, "moving")
    if not png.startswith(b"\x89PNG"):
        print("✗ 屏幕帧不是合法 PNG")
        return 1

    print(f"✓ 自检通过（{len(link.motions)} 段运动 / {len(plan.targets)} 件）")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(
        prog="python -m sorting.app.studio",
        description="智能分拣装置 · 本地网页工作台（仿真 / 实机）")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--classes", default=str(DEFAULT_CLASSES))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--http-port", type=int, default=8765, dest="http_port")
    ap.add_argument("--serial", metavar="PORT",
                    help="接实物：STM32 串口，如 COM3。不给就是仿真")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--base", default="",
                    help="挂在子路径下，如 --base /sort（隧道或反代转发到子路径时用）")
    ap.add_argument("--selftest", action="store_true", help="自检后退出")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest(args.config, args.classes)

    if not PAGE.exists():
        print(f"缺少页面文件: {PAGE}", file=sys.stderr)
        return 2

    mode = "serial" if args.serial else "sim"
    studio = Studio(args.config, args.classes, mode=mode,
                    serial_port=args.serial, baud=args.baud)

    Handler.studio = studio
    Handler.base = args.base.rstrip("/")
    httpd = ThreadingHTTPServer((args.host, args.http_port), Handler)
    httpd.daemon_threads = True

    url = f"http://{args.host}:{args.http_port}{Handler.base}/"
    print("=" * 62)
    print("  智能分拣装置 · 工作台")
    print("=" * 62)
    print(f"  地址    {url}")
    print(f"  链路    {'串口 ' + args.serial if args.serial else '模拟 MCU（仿真）'}")
    print(f"  场景    {len(studio.state.get('items', []))} 件　"
          f"节拍 {studio.state.get('total_s', 0):.1f} s（{studio.base_cfg.meta.qualifier}）")
    print("  退出    Ctrl+C")
    print("=" * 62)

    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
