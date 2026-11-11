"""一键中期演示：没有相机、没有机构，跑完整一轮分拣并出一套汇报材料。

    python -m sorting.app.demo                    # 出报告（瞬时，解析时间轴）
    python -m sorting.app.demo --speed 4          # 4 倍速播放，看屏幕帧
    python -m sorting.app.demo --seed 7 --per-class 3

产出（默认 out/demo/）：
    01_tray.png        合成相机图（"相机看到的东西"）
    02_detect.png      检测标注图
    03_layout.png      整机俯视 + 排序前后路径对比
    frames/*.png       480×320 屏幕帧，每件两张（搬运中 / 已入盒）
    trace.csv          逐动作时序
    timing.csv         逐轴运动记录
    report.md          汇报报告

关于数字的诚实说明（报告里也会写一遍）
--------------------------------------
* 视觉指标来自**合成图**，说明的是"检测代码 -> 毫米坐标"这条链没算错，
  不等于真机精度。真机还会多出光照、反光、镜头畸变的误差。
* 节拍是**按齿轮传动公式解析算出来**的，不含加减速、回程间隙、相机曝光。
* 机器参数目前是设计假定，见 machine.toml 的 [meta] 段。
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

from PIL import Image

from .. import config as cfgmod
from ..core import jobqueue
from ..core.link import MockLink
from ..display import layout, renderer
from ..vision import classes as vclasses
from ..vision import detect as vdetect
from ..vision import synthetic
from ..vision.detect import find_items
from . import runner

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "sorting" / "config"


# --------------------------------------------------------------------------
# 视觉评估
# --------------------------------------------------------------------------

def crop_thumb(image, det, pad: int = 10) -> Image.Image:
    """从原图裁出这件工件的缩略图，给屏幕的"图片"字段用。"""
    pts = det.contour_px.reshape(-1, 2)
    x0, y0 = pts.min(axis=0) - pad
    x1, y1 = pts.max(axis=0) + pad
    h, w = image.shape[:2]
    x0, y0 = max(0, int(x0)), max(0, int(y0))
    x1, y1 = min(w, int(x1)), min(h, int(y1))
    crop = image[y0:y1, x0:x1][:, :, ::-1]          # BGR -> RGB
    return Image.fromarray(crop)


def evaluate(dets, scene) -> dict:
    """把检测结果和合成真值对一遍，算检出率、分类正确率、定位误差。

    配对用最近邻，配对半径 30mm（合成场景工件最小间距也是 30mm，不会配错）。
    """
    pairs = vdetect.position_error_mm(dets, scene.items)
    errors = [e for _, e, _ in pairs]
    shape_ok = color_ok = both_ok = 0
    for idx, _, truth in pairs:
        d = dets[idx - 1]
        s_ok = d.verdict.shape == truth.shape
        c_ok = d.verdict.color == truth.color
        shape_ok += s_ok
        color_ok += c_ok
        both_ok += (s_ok and c_ok)

    n_truth = len(scene.items)
    return {
        "n_truth": n_truth,
        "n_det": len(dets),
        "n_match": len(pairs),
        "n_ok": sum(1 for d in dets if d.ok),
        "shape_ok": shape_ok,
        "color_ok": color_ok,
        "both_ok": both_ok,
        "err_mean": (sum(errors) / len(errors)) if errors else 0.0,
        "err_max": max(errors) if errors else 0.0,
    }


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def run(args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    frames_dir = out / "frames"
    frames_dir.mkdir(exist_ok=True)

    cfg = cfgmod.load(args.config)
    book = vclasses.load(args.classes)

    # -- 1. 造场景 -------------------------------------------------------
    scene = synthetic.build(
        book, seed=args.seed, per_class=args.per_class,
        tray_w=cfg.tray_w_mm, tray_h=cfg.tray_h_mm,
    )
    Image.fromarray(scene.image[:, :, ::-1]).save(out / "01_tray.png")

    # -- 2. 检测 ---------------------------------------------------------
    dets = find_items(scene.image, scene.H_px_to_mm, book)
    Image.fromarray(vdetect.draw(scene.image, dets, scene.H_mm_to_px)[:, :, ::-1]) \
        .save(out / "02_detect.png")
    stats = evaluate(dets, scene)

    # -- 3. 排程 ---------------------------------------------------------
    plan = jobqueue.build(dets, cfg)
    # 对照组：完全不排序，按检出顺序抓 —— 用来量化排序省了多少路
    unordered = [
        cfgmod.Target(index=d.idx, label=d.verdict.cls.name, category=d.verdict.cls.id,
                      pick_xy=cfg.tray_to_machine(d.cx_mm, d.cy_mm), cfg=cfg,
                      width_mm=d.width_mm)
        for d in dets if d.ok
    ]
    cost_sorted = plan.cost_mm
    cost_raw = plan.costs.get("detect", cost_sorted)
    layout.draw_layout(cfg, plan, out / "03_layout.png", show_unordered=unordered)

    if not plan.targets:
        print("没有任何可抓取的工件，检查 classes.json 与检测阈值", file=sys.stderr)
        return 2

    # -- 4. 执行（模拟 MCU，解析时间轴）----------------------------------
    thumbs = {d.idx: crop_thumb(scene.image, d) for d in dets}
    total = len(plan.targets)
    tally: dict[str, int] = {}
    done = 0

    # target.index 是检测编号，这里要换回缩略图
    def thumb_of(target):
        return thumbs.get(target.index)

    first = plan.targets[0]
    renderer.save(renderer.DisplayModel(
        index=1, name=first.label, total=total, done=0,
        thumb=thumb_of(first), status="READY", tally={},
    ), frames_dir / "00_ready.png")

    def on_before(pos, target):
        renderer.save(renderer.DisplayModel(
            index=pos, name=target.label, total=total, done=done,
            thumb=thumb_of(target), status="MOVING", tally=dict(tally),
        ), frames_dir / f"{pos:02d}_moving.png")

    def on_display(pos, rec, target):
        nonlocal done
        done += 1
        if rec.held:
            tally[target.category] = tally.get(target.category, 0) + 1
        renderer.save(renderer.DisplayModel(
            index=pos, name=target.label, total=total, done=done,
            thumb=thumb_of(target),
            status="PLACED" if rec.held else "SKIP", tally=dict(tally),
        ), frames_dir / f"{pos:02d}_{'placed' if rec.held else 'skip'}.png")

    link = MockLink(axes=cfg.axes, speed_factor=args.speed)
    runner.home(link, cfg, args.speed)
    inject = set(args.inject_skip or [])
    res = runner.execute(
        plan, link, cfg, speed_factor=args.speed,
        on_before_item=on_before, on_display=on_display, inject_skip=inject,
    )

    renderer.save(renderer.DisplayModel(
        index=total, name="完成", total=total, done=done,
        thumb=None, status="DONE", tally=dict(tally),
    ), frames_dir / "99_done.png")

    # -- 5. 落盘 ---------------------------------------------------------
    _write_timing_csv(out / "timing.csv", link.log.motions)
    _write_trace_csv(out / "trace.csv", res)
    _write_report(out / "report.md", cfg=cfg, book=book, stats=stats, plan=plan,
                  res=res, link=link, cost_sorted=cost_sorted, cost_raw=cost_raw,
                  args=args, scene=scene)

    # -- 6. 终端摘要 -----------------------------------------------------
    _print_summary(cfg, stats, plan, res, cost_sorted, cost_raw, out)
    return 0


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------

def _write_timing_csv(path: Path, motions) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["t_start_s", "axis", "start_mm", "end_mm", "distance_mm", "duration_s"])
        for m in motions:
            w.writerow([f"{m.t_start_s:.3f}", m.axis, f"{m.start_mm:.2f}",
                        f"{m.end_mm:.2f}", f"{m.distance_mm:.2f}", f"{m.duration_s:.3f}"])


def _write_trace_csv(path: Path, res: runner.RunResult) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["pos", "det_index", "name", "category", "action", "args", "t_s"])
        for rec in res.items:
            for s in rec.steps:
                w.writerow([rec.pos, rec.index, rec.name, rec.category,
                            s.kind, str(s.args), f"{s.t_s:.3f}"])


def _axis_totals(motions) -> dict[str, tuple[float, float]]:
    """每根轴走了多远、花了多久（XY 并行，各自的时间之和会大于总时长）。"""
    acc: dict[str, list[float]] = {}
    for m in motions:
        a = acc.setdefault(m.axis, [0.0, 0.0])
        a[0] += m.distance_mm
        a[1] += m.duration_s
    return {k: (v[0], v[1]) for k, v in acc.items()}


def _segment_time(motions) -> float:
    """把同时发车的轴按 t_start 归并，取最大值，得到真正占用的运动时间。"""
    if not motions:
        return 0.0
    groups: dict[float, float] = {}
    for m in motions:
        key = round(m.t_start_s, 6)
        groups[key] = max(groups.get(key, 0.0), m.duration_s)
    return sum(groups.values())


def _write_report(path: Path, **kw) -> None:
    cfg: cfgmod.Config = kw["cfg"]
    stats = kw["stats"]
    plan = kw["plan"]
    res: runner.RunResult = kw["res"]
    link = kw["link"]
    args = kw["args"]
    q = cfg.meta.qualifier

    motions = link.log.motions
    axis_tot = _axis_totals(motions)
    motion_s = _segment_time(motions)
    other_s = max(0.0, res.total_s - motion_s - res.idle_s)

    L: list[str] = []
    a = L.append

    a("# 智能分拣装置 · 软件层演示报告")
    a("")
    a(f"生成时间：{datetime.now():%Y-%m-%d %H:%M}　场景种子：{args.seed}　"
      f"每类件数：{args.per_class}")
    a("")
    a("## 0. 这份报告里的数字是什么")
    a("")
    a("| 类别 | 来源 | 能不能当实测值 |")
    a("|---|---|---|")
    a("| 检出/分类/定位误差 | **合成托盘图**跑真实检测代码 | ❌ 只能说明链路算得对，不能代替真机精度 |")
    a(f"| 节拍、步数、行程 | 按齿轮传动公式**{q}** | ❌ 不含加减速、回程间隙、相机曝光 |")
    a("| 排序前后的行走距离 | 由上面的坐标直接算 | ✅ 这是几何量，与硬件无关 |")
    a("| 屏幕帧 | 真实渲染出的 PNG | ✅ 与真机同一份渲染代码 |")
    a("")
    a(f"**标定状态：{'已标定' if cfg.meta.calibrated else '未标定（参数为设计假定）'}**。"
      f"实物到位前，上述节拍类数字只用于判断量级和做方案对比，不能写进实测栏。")
    a("")

    # ---- 1 视觉
    a("## 1. 视觉链路")
    a("")
    a(f"- 真值 {stats['n_truth']} 件，检出 {stats['n_det']} 件，成功配对 {stats['n_match']} 件")
    a(f"- 形状判对：{stats['shape_ok']}/{stats['n_match']}　"
      f"颜色判对：{stats['color_ok']}/{stats['n_match']}　"
      f"两者都对：{stats['both_ok']}/{stats['n_match']}")
    a(f"- 进入抓取队列：{stats['n_ok']} 件（阈值过滤后）")
    a(f"- 托盘坐标定位误差：均值 {stats['err_mean']:.2f} mm，最大 {stats['err_max']:.2f} mm")
    a("")
    a("> 误差含义：从图上的像素轮廓 → 单应变换 → 毫米坐标这一整条换算链的闭合误差。")
    a("> 合成图没有镜头畸变和反光，所以这个数字是**下界**，真机只会更大。")
    a("")

    # ---- 2 排程
    a("## 2. 排程")
    a("")
    a(f"- 队列：{len(plan.targets)} 件，类别出现顺序 {' → '.join(plan.class_order)}")
    per = {c: len(v) for c, v in plan.by_class.items()}
    a(f"- 各类件数：{'　'.join(f'{c} 类 {n} 件' for c, n in per.items())}")
    a("")
    a("三种排法都用同一个代价函数算（当前点 → 取点 → 料盒 → 下一个取点）：")
    a("")
    a("| 策略 | 说明 | 行走距离 (mm) |")
    a("|---|---|---|")
    label = {"detect": "按检出顺序", "nn": "全局最近邻（不分同类）",
             "class": "同类一次做完（类别间 6! 穷举）"}
    for key in ("detect", "nn", "class"):
        if key not in plan.costs:
            continue
        mark = " **← 采用**" if key == plan.strategy else ""
        a(f"| `{key}` | {label[key]} | {plan.costs[key]:.0f}{mark} |")
    a("")
    best, worst = plan.cost_mm, plan.costs.get("detect", plan.cost_mm)
    if worst > 0:
        a(f"最终采用 `{plan.strategy}`，相比按检出顺序省 "
          f"**{(1 - best / worst) * 100:.1f}%**。")
    a("")
    a("> 注意：赛规只要求「同形同色进同一个盒子」，**不要求同类连着做完**，")
    a("> 所以排程是自由的旅行商问题。先入为主地按类别分组不一定更省——")
    a("> 这张表就是用来验证这一点的，而不是事先假定的结论。")
    if plan.skipped:
        a("")
        a("未进队列的件：")
        for s in plan.skipped:
            a(f"- 检出 #{s.idx}（托盘 {s.cx_mm:.0f},{s.cy_mm:.0f} mm）：{s.reason}")
    a("")

    # ---- 3 节拍
    a(f"## 3. 节拍（{q}）")
    a("")
    a(f"- 整轮总时长：**{res.total_s:.1f} s**（含开机回零）")
    a(f"- 其中运动 {motion_s:.1f} s，等待稳定 {res.idle_s:.1f} s，"
      f"回零等其它 {other_s:.1f} s")
    a(f"- 已入盒 {res.placed} 件，跳过 {res.skipped} 件")
    a("")
    a("| 轴 | 累计行程 (mm) | 累计运动时间 (s) | 步数 |")
    a("|---|---|---|---|")
    for name in ("x", "y", "z", "grip"):
        if name not in axis_tot:
            continue
        dist, dur = axis_tot[name]
        ax = cfg.axes[name]
        a(f"| {name.upper()} | {dist:.0f} | {dur:.1f} | {ax.mm_to_steps(dist)} |")
    a("")
    if axis_tot and motion_s > 0:
        worst = max(axis_tot.items(), key=lambda kv: kv[1][1])
        share = worst[1][1] / motion_s * 100
        a(f"> 瓶颈：**{worst[0].upper()} 轴占总运动时间的 {share:.0f}%**"
          f"（{worst[1][1]:.1f} s / {motion_s:.1f} s）。"
          f"每件工件 Z 要上下各两次，而 Z 速度只有 "
          f"{cfg.z.speed_mm_s:.0f} mm/s，比 XY 慢。"
          f"当前 z_safe_mm = {cfg.z_safe_mm:.0f} mm 是设计假定值，"
          f"装配后应按夹爪实际长度下调——这是缩短节拍最直接的杠杆。")
        a("")
    a("单件节拍：")
    a("")
    a("| 序号 | 名称 | 料盒 | 用时 (s) | 累计 (s) | 结果 |")
    a("|---|---|---|---|---|---|")
    for rec in res.items:
        tag = "已入盒" if rec.held else "跳过"
        if rec.index in res.injected:
            tag += "（人为注入空夹）"
        a(f"| {rec.pos} | {rec.name} | {rec.category} | {rec.duration_s:.1f} | "
          f"{rec.t_end_s:.1f} | {tag} |")
    a("")
    if res.injected:
        a(f"> ⚠ 本次运行对第 {sorted(res.injected)} 件**人为注入**了空夹，"
          f"目的是演示 SKIP 分支。正常运行时不会出现。")
        a("")

    # ---- 4 待标定
    a("## 4. 待实物标定清单")
    a("")
    a("以下参数目前是设计假定，装配完成后需要实测替换 `sorting/config/machine.toml`：")
    a("")
    for i, item in enumerate(cfg.meta.pending, 1):
        a(f"{i}. {item}")
    a("")
    a("当前的假定值（供装配时对照）：")
    a("")
    a("| 轴 | 模数 m | 齿数 z | 减速比 i | 分度圆 d (mm) | mm/转 | 步/mm | 速度 (mm/s) |")
    a("|---|---|---|---|---|---|---|---|")
    for name in ("x", "y", "z", "grip"):
        ax = cfg.axes[name]
        a(f"| {name.upper()} | {ax.module_mm} | {ax.teeth} | {ax.reduction} | "
          f"{ax.pitch_dia_mm:.1f} | {ax.mm_per_rev:.1f} | {ax.steps_per_mm:.2f} | "
          f"{ax.speed_mm_s:.0f} |")
    a("")

    # ---- 5 产物
    a("## 5. 产物")
    a("")
    a("| 文件 | 内容 |")
    a("|---|---|")
    a("| `01_tray.png` | 合成相机图 |")
    a("| `02_detect.png` | 检测标注（绿=识别成功，橙=跳过） |")
    a("| `03_layout.png` | 整机俯视 + 排序前后路径对比 |")
    a("| `frames/00_ready.png` | 屏幕初始帧 |")
    a("| `frames/NN_moving.png` | 第 N 件搬运中 |")
    a("| `frames/NN_placed.png` | 第 N 件已入盒 |")
    a("| `frames/99_done.png` | 完成帧 |")
    a("| `timing.csv` | 逐轴运动记录 |")
    a("| `trace.csv` | 逐动作时序 |")
    a("")
    a("屏幕帧与真机共用同一份 `sorting/display/renderer.py`，"
      "真机上把 `render()` 返回的图直接送 SPI 屏即可。")
    a("")

    path.write_text("\n".join(L), encoding="utf-8")


def _print_summary(cfg, stats, plan, res, cost_sorted, cost_raw, out: Path) -> None:
    q = cfg.meta.qualifier
    print("=" * 66)
    print("  智能分拣装置 · 软件层演示")
    print("=" * 66)
    print(f"  视觉    检出 {stats['n_det']}/{stats['n_truth']}　"
          f"分类全对 {stats['both_ok']}/{stats['n_match']}　"
          f"定位误差 均 {stats['err_mean']:.2f} / 最大 {stats['err_max']:.2f} mm")
    per = {c: len(v) for c, v in plan.by_class.items()}
    print(f"  排程    {len(plan.targets)} 件 "
          f"{' '.join(f'{c}×{n}' for c, n in per.items())}")
    pct = (1 - cost_sorted / cost_raw) * 100 if cost_raw else 0.0
    print(f"  路径    {cost_raw:.0f} mm -> {cost_sorted:.0f} mm "
          f"（{pct:.1f}% ↓，策略 {plan.strategy}）")
    print(f"  节拍    {res.total_s:.1f} s（{q}，含回零）  "
          f"入盒 {res.placed} / 跳过 {res.skipped}")
    print(f"  产物    {out}")
    print("=" * 66)
    if not cfg.meta.calibrated:
        print("  ⚠ 机器参数未标定：节拍为解析估算，非实测。详见 report.md §4")


def main(argv=None) -> int:
    # Windows 控制台默认 GBK，报告里的 ⚠ / ↑ 之类符号会直接抛 UnicodeEncodeError
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(
        prog="python -m sorting.app.demo",
        description="无硬件跑通一整轮分拣，输出汇报材料",
    )
    ap.add_argument("--out", default="out/demo", help="输出目录")
    ap.add_argument("--config", default=str(CONFIG_DIR / "machine.toml"),
                    help="机器参数。调速度/行程/料盒坐标时换个文件，代码不用动")
    ap.add_argument("--classes", default=str(CONFIG_DIR / "classes.json"),
                    help="识别判据。现场换货时换这个")
    ap.add_argument("--seed", type=int, default=0, help="合成场景随机种子")
    ap.add_argument("--per-class", type=int, default=2, dest="per_class",
                    help="每类工件数（6 类，默认 2 → 共 12 件）")
    ap.add_argument("--speed", type=float, default=0.0,
                    help="播放倍速，0 = 不等待（默认），4 = 4 倍速")
    ap.add_argument("--inject-skip", type=int, nargs="*", default=None,
                    dest="inject_skip",
                    help="演示用：让这些序号的件空夹，展示 SKIP 分支")
    args = ap.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
