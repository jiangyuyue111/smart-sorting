"""上机入口：视觉 -> 排程 -> 动作 -> 屏幕，跑真正的分拣流程。

和 demo 的区别
--------------
    demo.py  一次性把报告材料全生成出来（合成场景、报告、CSV、图）
    main.py  反复跑同一套流程，接真串口/真相机，带看门狗和状态机

两者共用同一批模块（vision / jobqueue / sequence / runner / renderer），
不存在"演示一套、真机另一套"的问题。

线程模型——单进程三线程：
    main      唯一的串口写入方。执行动作序列，绝不做渲染。
    display   只读当前显示模型，自己节流刷屏。主线程不等它。
    watchdog  空闲时 ping MCU；连续失败 -> 置 FAULT 事件。

为什么主线程必须是唯一写入方：串口是半双工的一问一答，两个线程同时写
就会把 GOTO 和 GRIP 拼成一条非法指令。这条约束不要破。

用法：
    python -m sorting.app.main --demo                 # 无板无相机，合成场景
    python -m sorting.app.main --demo --frames out/f  # 顺带把屏幕帧存下来
    python -m sorting.app.main --port COM3 --image tray.png --calib calib.json
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path

from ..config import Config, load as load_config
from ..core import jobqueue
from ..core import protocol as proto
from ..core.link import Link, MockLink, SerialLink, Session
from ..core.machine import Event, Machine, State
from ..display import renderer
from ..vision import classes as vclasses
from ..vision import detect as vdetect
from ..vision import synthetic
from . import runner

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
DEFAULT_CONFIG = CONFIG_DIR / "machine.toml"
DEFAULT_CLASSES = CONFIG_DIR / "classes.json"


# --------------------------------------------------------------------------
# 取图 + 标定
# --------------------------------------------------------------------------

def acquire(cfg: Config, book, args):
    """取一帧图并给出 像素->毫米 的单应矩阵。返回 (image, H_px_to_mm, 真值或None)。"""
    if args.demo:
        scene = synthetic.build(book, seed=args.seed, per_class=args.per_class,
                                tray_w=cfg.tray_w_mm, tray_h=cfg.tray_h_mm)
        return scene.image, scene.H_px_to_mm, scene.items

    import cv2

    if args.image:
        img = cv2.imread(args.image)
        if img is None:
            raise SystemExit(f"读不到图片: {args.image}")
    else:
        cap = cv2.VideoCapture(int(args.camera))
        ok, img = cap.read()
        cap.release()
        if not ok:
            raise SystemExit(f"相机 {args.camera} 取图失败")

    if not args.calib:
        raise SystemExit(
            "真机必须给 --calib（托盘四角标定得到的单应矩阵）。\n"
            "先用 python -m sorting.tools.calibrate 标一次：\n"
            "  intrinsics  -> intrinsics.json（棋盘格，标内参去畸变）\n"
            "  tray        -> calib.json（点托盘四角，解单应）\n"
            "  check       -> check.png（把网格画回图上，肉眼确认）"
        )
    cal = json.loads(Path(args.calib).read_text(encoding="utf-8"))
    import numpy as np

    intr = cal.get("intrinsics")
    if cal.get("undistorted") and intr:
        # 标定是在**去畸变之后**的图上做的，所以取图也必须走同一步。
        # 漏掉这一步，H 会带着镜头畸变去映射，边角能差好几个毫米。
        img = cv2.undistort(img, np.asarray(intr["K"], dtype=float),
                            np.asarray(intr["dist"], dtype=float))

    H = np.asarray(cal["H"], dtype=float)      # 托盘毫米 -> 像素
    return img, np.linalg.inv(H), None         # 下游要的是 像素 -> 毫米


# --------------------------------------------------------------------------
# 看门狗
# --------------------------------------------------------------------------

class Watchdog:
    def __init__(self, session: Session, busy: threading.Lock, period_s: float) -> None:
        self.session = session
        self.busy = busy
        self.period_s = period_s
        self.faulted = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="watchdog", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)

    def _loop(self) -> None:
        misses = 0
        while not self._stop.wait(self.period_s):
            if not self.busy.acquire(blocking=False):
                continue          # 主线程正在等 MCU，这一拍不插话
            try:
                self.session.ping()
                misses = 0
            except Exception:
                misses += 1
                if misses >= 3:
                    self.faulted.set()
                    return
            finally:
                self.busy.release()


# --------------------------------------------------------------------------
# 屏幕：主线程写模型，显示线程自己刷
# --------------------------------------------------------------------------

class Screen:
    """把 DisplayModel 渲染出去。--frames 给了就存 PNG，否则打印一行。"""

    def __init__(self, out_dir: Path | None = None) -> None:
        self.out_dir = out_dir
        self.model = renderer.DisplayModel(index=1, name="-", total=0)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="display", daemon=True)
        self._seq = 0
        if out_dir:
            out_dir.mkdir(parents=True, exist_ok=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        self._flush()

    def update(self, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self.model, k, v)

    def _loop(self) -> None:
        while not self._stop.wait(0.2):
            self._flush()

    def _flush(self) -> None:
        with self._lock:
            m = self.model
            model = renderer.DisplayModel(
                index=m.index, name=m.name, total=m.total, thumb=m.thumb,
                status=m.status, done=m.done, tally=dict(m.tally),
            )
        if self.out_dir:
            self._seq += 1
            renderer.save(model, self.out_dir / f"{self._seq:03d}_{model.status.lower()}.png")
        else:
            print(f"[{model.status_text}] {model.index}/{model.total} "
                  f"{model.name}  进度 {model.done}/{model.total}", flush=True)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def run(cfg: Config, book, link: Link, args) -> int:
    session = Session(link, cfg.ack_timeout_s, cfg.done_timeout_s, item_timeout_s=2.0)
    busy = threading.Lock()
    wd = Watchdog(session, busy, cfg.heartbeat_s * 5)
    screen = Screen(Path(args.frames) if args.frames else None)
    machine = Machine(State.BOOT)

    try:
        # -- 自检 --------------------------------------------------------
        try:
            session.ping()
            ok = True
        except Exception:
            ok = False
        machine.fire(Event.SELF_TEST_OK if ok else Event.SELF_TEST_FAIL)
        if machine.state is State.FAULT:
            print("自检失败：MCU 无应答", flush=True)
            return 2

        # -- 取图 -> 识别 -> 排程 ----------------------------------------
        image, H_inv, truth = acquire(cfg, book, args)
        dets = vdetect.find_items(image, H_inv, book)
        plan = jobqueue.build(dets, cfg)
        print(f"检出 {len(dets)} 件，队列 {len(plan.targets)} 件，"
              f"策略 {plan.strategy}，估算行走 {plan.cost_mm:.0f} mm", flush=True)

        thumbs = {d.idx: _thumb(image, d) for d in dets}
        total = len(plan.targets)
        state = {"done": 0, "tally": {}}

        screen.update(total=total, index=1, status="READY",
                      name=plan.targets[0].label if plan.targets else "-",
                      thumb=thumbs.get(plan.targets[0].index) if plan.targets else None)
        screen.start()
        wd.start()

        def on_before(pos, target):
            screen.update(index=pos, name=target.label, status="MOVING",
                          thumb=thumbs.get(target.index))

        def on_display(pos, rec, target):
            state["done"] += 1
            if rec.held:
                state["tally"][target.category] = state["tally"].get(target.category, 0) + 1
            screen.update(index=pos, name=target.label, done=state["done"],
                          tally=dict(state["tally"]),
                          status="PLACED" if rec.held else "SKIP")
            print(f"  #{pos:02d} {target.label} -> "
                  f"{target.category if rec.held else 'NG（空夹，跳过）'}"
                  f"  {rec.duration_s:.1f}s", flush=True)

        # -- 上料 -> 运行 -------------------------------------------------
        machine.fire(Event.ARM)
        machine.fire(Event.PLAN_READY)

        with busy:
            session.command(proto.Home())
        res = runner.execute(plan, link, cfg, speed_factor=args.speed,
                             on_before_item=on_before, on_display=on_display)

        machine.fire(Event.PLAN_DONE)
        screen.update(status="DONE", done=state["done"])
        print(f"完成：{res.placed} 件入盒，{res.skipped} 件跳过，"
              f"总时长 {res.total_s:.1f} s", flush=True)
        return 0

    except Exception as exc:  # noqa: BLE001 —— 顶层兜底，任何异常都要停机
        try:
            session.stop()
        except Exception:
            pass
        screen.update(status="SKIP")
        print(f"故障停机: {type(exc).__name__}: {exc}", flush=True)
        return 1
    finally:
        wd.stop()
        screen.stop()
        session.close()


def _thumb(image, det, pad: int = 10):
    from PIL import Image

    pts = det.contour_px.reshape(-1, 2)
    x0, y0 = pts.min(axis=0) - pad
    x1, y1 = pts.max(axis=0) + pad
    h, w = image.shape[:2]
    crop = image[max(0, int(y0)):min(h, int(y1)), max(0, int(x0)):min(w, int(x1))]
    return Image.fromarray(crop[:, :, ::-1])


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(prog="python -m sorting.app.main",
                                 description="智能分拣装置上位机")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--classes", default=str(DEFAULT_CLASSES))
    ap.add_argument("--port", help="串口，如 COM3；不给则用模拟 MCU")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--demo", action="store_true", help="合成场景，不需要相机")
    ap.add_argument("--image", help="用这张图代替相机")
    ap.add_argument("--camera", default="0", help="相机编号")
    ap.add_argument("--calib", help="标定文件（{\"H\": 3x3}）")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--per-class", type=int, default=2, dest="per_class")
    ap.add_argument("--speed", type=float, default=0.0, help="播放倍速，0 = 不等待")
    ap.add_argument("--frames", help="把屏幕帧存到这个目录")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    book = vclasses.load(args.classes)
    link: Link = SerialLink(args.port, args.baud) if args.port else MockLink(axes=cfg.axes)

    print(f"配置 {args.config}")
    print(f"链路 {'串口 ' + args.port if args.port else '模拟 MCU'}")
    print(f"标定 {'已标定' if cfg.meta.calibrated else '未标定（参数为设计假定）'}")
    return run(cfg, book, link, args)


if __name__ == "__main__":
    raise SystemExit(main())
