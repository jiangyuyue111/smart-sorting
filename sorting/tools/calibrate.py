"""相机标定：把"托盘毫米坐标"和"图像像素坐标"对上。

为什么必须两步，不能只做单应
----------------------------
单应矩阵只在**无畸变**的平面图像上成立。我们这套是 250 mm 工作距离 + 小焦距
镜头，画面边角的桶形畸变会让托盘四角外扩几个百分点——而那恰好是工件的位置。
先标内参去畸变、再解单应，四角才准；反过来做，误差会直接进抓取坐标。

标出来的 H 存的是 **托盘毫米 → 像素**（和 machine.toml 里 tray_w_mm/tray_h_mm
同一个局部坐标系：左上 (0,0)，右下 (w,h)）。上位机读进来取逆，就是
像素 → 毫米，和 vision.detect 要的 H_px_to_mm 一致。

三个子命令
----------
    intrinsics  棋盘格标内参 + 畸变系数 -> intrinsics.json
    tray        托盘四角标单应 -> calib.json（有内参就先自动去畸变）
    check       把标定结果画回图上，肉眼看对不对 + 报重投影误差

    # 1. 拍 15~20 张棋盘格，覆盖画面四角和倾斜
    python -m sorting.tools.calibrate intrinsics --images calib/*.jpg --cols 9 --rows 6 -o intrinsics.json

    # 2. 拍一张空托盘，点它的四个内角
    python -m sorting.tools.calibrate tray --image tray.jpg --intrinsics intrinsics.json -o calib.json

    # 3. 验证：看 check.png，转角点是否落在托盘角上
    python -m sorting.tools.calibrate check --calib calib.json --image tray.jpg -o check.png

    # 没有相机也能验证求解器本身对不对
    python -m sorting.tools.calibrate selftest

点四角时顺序必须是 **左上 → 右上 → 右下 → 左下**，按托盘的物理朝向，
不要按图像里的"最上/最下"乱点。点错了按 R 重来。
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

import numpy as np

from ..config import Config, load as load_config
from ..vision import geometry

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
DEFAULT_CONFIG = CONFIG_DIR / "machine.toml"

WIN = "calibrate"

# 四角共线/重合时解出来的 H 会把位置映射偏，但不会报错——用重投影 RMS 兜住。
# 正常四边形（哪怕手点歪几个像素）RMS 必然接近 0，所以这个阈值很宽。
DEGENERATE_RMS_PX = 0.5


# --------------------------------------------------------------------------
# 内参
# --------------------------------------------------------------------------

def load_intrinsics(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def undistort(image, intr: dict):
    """按内参去畸变。没给内参就原样返回。"""
    if not intr:
        return image
    import cv2

    K = np.asarray(intr["K"], dtype=np.float64)
    dist = np.asarray(intr["dist"], dtype=np.float64)
    return cv2.undistort(image, K, dist)


def cmd_intrinsics(args) -> int:
    import cv2

    files = sorted(f for pat in args.images for f in glob.glob(pat))
    if len(files) < 6:
        print(f"只有 {len(files)} 张图，至少 6 张才能标定（建议 15 张以上）", file=sys.stderr)
        return 2

    pattern = (args.cols, args.rows)          # 内角点个数，不是棋盘格数
    objp = np.zeros((pattern[0] * pattern[1], 3), np.float32)
    objp[:, :2] = np.mgrid[0:pattern[0], 0:pattern[1]].T.reshape(-1, 2) * args.square_mm

    obj_pts, img_pts, used, size = [], [], [], None
    for f in files:
        img = cv2.imread(f)
        if img is None:
            print(f"  跳过（读不到）{f}")
            continue
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        size = gray.shape[::-1]
        ok, corners = cv2.findChessboardCornersSB(gray, pattern)
        if not ok:
            ok, corners = cv2.findChessboardCorners(
                gray, pattern,
                cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE,
            )
            if ok:
                corners = cv2.cornerSubPix(
                    gray, corners, (11, 11), (-1, -1),
                    (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001),
                )
        if not ok:
            print(f"  没找到棋盘格 {f}")
            continue
        obj_pts.append(objp)
        img_pts.append(corners.astype(np.float32))
        used.append(f)

    if len(obj_pts) < 6:
        print(f"只有 {len(obj_pts)} 张能识别，标定无法进行", file=sys.stderr)
        return 2

    rms, K, dist, _, _ = cv2.calibrateCamera(obj_pts, img_pts, size, None, None)
    out = {
        "K": K.tolist(),
        "dist": dist.ravel().tolist(),
        "image_size": [int(size[0]), int(size[1])],
        "rms_px": float(rms),
        "n_images": len(obj_pts),
        "pattern": [int(args.cols), int(args.rows)],
        "square_mm": float(args.square_mm),
    }
    Path(args.output).write_text(json.dumps(out, indent=2), encoding="utf-8")

    print(f"用了 {len(obj_pts)}/{len(files)} 张图")
    print(f"重投影 RMS = {rms:.3f} px  "
          f"{'✓ 可用' if rms < 0.5 else '⚠ 偏大，多拍几张覆盖边角' if rms < 1.0 else '✗ 太大，重拍'}")
    print(f"fx={K[0,0]:.1f} fy={K[1,1]:.1f} cx={K[0,2]:.1f} cy={K[1,2]:.1f}")
    print(f"-> {args.output}")
    return 0


# --------------------------------------------------------------------------
# 托盘四角
# --------------------------------------------------------------------------

def tray_corners_mm(cfg: Config) -> np.ndarray:
    """托盘局部坐标系下的四角：左上、右上、右下、左下。"""
    w, h = cfg.tray_w_mm, cfg.tray_h_mm
    return np.array([[0.0, 0.0], [w, 0.0], [w, h], [0.0, h]], dtype=np.float64)


def solve_from_corners(px: np.ndarray, cfg: Config) -> tuple[np.ndarray, float]:
    """托盘四角像素坐标 -> (毫米->像素 的单应, 重投影 RMS[px])。

    注意：四组点正好定出 8 个未知量，所以**只要四角构成正常四边形，RMS 必然
    接近 0**。它不能用来衡量"点得准不准"——点偏了照样精确通过。它的用处只有一个：
    发现退化的四边形（四点共线、两点重合），那种情况解出来的 H 会把人抓偏几十毫米
    却毫无提示。要判断点得对不对，跑 `check` 看图。
    """
    mm = tray_corners_mm(cfg)
    px = np.asarray(px, dtype=np.float64).reshape(4, 2)
    H = geometry.solve(mm, px)               # 毫米 -> 像素，和 calib.json 的约定一致

    # 四角共线时 DLT 照样能解出一个"完美"的 H（重投影误差 0），但它是**不可逆**的：
    # 前两行成比例，秩只有 2。下游要取逆才能把像素换成毫米，取逆就炸。
    # 所以这里判秩，判的不是误差。
    sv = np.linalg.svd(H, compute_uv=False)
    if sv[-1] <= 1e-9 * sv[0]:
        raise geometry.HomographyError(
            "四角接近共线或重合，解出的单应不可逆。重新点一遍：左上 右上 右下 左下"
        )

    back = geometry.apply(H, mm)
    rms = float(np.sqrt(np.mean(np.sum((back - px) ** 2, axis=1))))
    if rms > DEGENERATE_RMS_PX:
        raise geometry.HomographyError(
            f"四角对不上（RMS {rms:.2f} px）—— 重新点一遍：左上 右上 右下 左下")
    return H, rms


def px_per_mm(H: np.ndarray, cfg: Config) -> tuple[float, float]:
    """横向、纵向各多少个像素对应 1mm——判断焦距和安装高度合不合理。"""
    mm = tray_corners_mm(cfg)
    px = geometry.apply(H, mm)
    sx = np.linalg.norm(px[1] - px[0]) / cfg.tray_w_mm
    sy = np.linalg.norm(px[3] - px[0]) / cfg.tray_h_mm
    return float(sx), float(sy)


def _pick_corners(image, scale: float | None = None) -> np.ndarray:
    """在图上点四个角。原图太大就按比例缩到屏幕上点，坐标自动换算回去。"""
    import cv2

    h, w = image.shape[:2]
    if scale is None:
        scale = min(1.0, 1400.0 / w, 900.0 / h)
    view = (cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            if scale != 1.0 else image.copy())

    pts: list[tuple[float, float]] = []

    def on_mouse(event, x, y, _flags, _param):
        if event == cv2.EVENT_LBUTTONDOWN and len(pts) < 4:
            pts.append((x / scale, y / scale))

    cv2.namedWindow(WIN, cv2.WINDOW_AUTOSIZE)
    cv2.setMouseCallback(WIN, on_mouse)
    try:
        while True:
            canvas = view.copy()
            sp = [(int(p[0] * scale), int(p[1] * scale)) for p in pts]
            for i, p in enumerate(sp):
                cv2.circle(canvas, p, 6, (0, 255, 0), -1)
                cv2.putText(canvas, str(i + 1), (p[0] + 8, p[1] - 8),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
            if len(sp) > 1:
                for i in range(len(sp) - 1):
                    cv2.line(canvas, sp[i], sp[i + 1], (0, 255, 255), 2)
            if len(sp) == 4:
                cv2.line(canvas, sp[3], sp[0], (0, 255, 255), 2)

            names = ["TL (top-left)", "TR", "BR", "BL (bottom-left)"]
            hint = (f"click {len(pts) + 1}/4: {names[len(pts)]}"
                    if len(pts) < 4 else "ENTER=ok   R=redo   ESC=cancel")
            cv2.putText(canvas, hint, (10, 28), cv2.FONT_HERSHEY_SIMPLEX,
                        0.75, (0, 255, 255), 2)
            cv2.imshow(WIN, canvas)

            k = cv2.waitKey(20) & 0xFF
            if k == 27:
                raise SystemExit("已取消")
            if k in (ord("r"), ord("R")):
                pts.clear()
            if k in (10, 13) and len(pts) == 4:
                break
    finally:
        cv2.destroyWindow(WIN)

    return np.array(pts, dtype=np.float64)


def cmd_tray(args) -> int:
    import cv2

    cfg = load_config(args.config)
    intr = load_intrinsics(args.intrinsics) if args.intrinsics else None

    raw = cv2.imread(args.image)
    if raw is None:
        print(f"读不到图片: {args.image}", file=sys.stderr)
        return 2
    img = undistort(raw, intr)

    if args.corners:
        vals = [float(v) for pair in args.corners for v in pair.split(",")]
        if len(vals) != 8:
            print("--corners 需要 4 组 x,y", file=sys.stderr)
            return 2
        px = np.array(vals, dtype=np.float64).reshape(4, 2)
    else:
        px = _pick_corners(img)

    H, rms = solve_from_corners(px, cfg)
    sx, sy = px_per_mm(H, cfg)

    out = {
        "H": H.tolist(),
        "tray_w_mm": cfg.tray_w_mm,
        "tray_h_mm": cfg.tray_h_mm,
        "undistorted": intr is not None,
        "image_size": [int(img.shape[1]), int(img.shape[0])],
        "corners_px": px.tolist(),
        "rms_px": rms,
        "px_per_mm": [sx, sy],
        "intrinsics": {"K": intr["K"], "dist": intr["dist"]} if intr else None,
    }
    Path(args.output).write_text(json.dumps(out, indent=2), encoding="utf-8")

    print(f"托盘 {cfg.tray_w_mm:.0f}×{cfg.tray_h_mm:.0f} mm")
    print(f"分辨率 {sx:.2f} / {sy:.2f} px·mm⁻¹  "
          f"（相当于 {1000.0 / max(sx, 1e-9) / 1000:.3f} mm/px）")
    print(f"重投影 RMS = {rms:.3f} px" + ("  （四角精确通过，正常）" if rms < 1e-6 else ""))
    print(f"去畸变 {'已应用' if intr else '未应用 —— 建议先跑 intrinsics'}")
    print(f"-> {args.output}")
    return 0


# --------------------------------------------------------------------------
# 检查
# --------------------------------------------------------------------------

def cmd_check(args) -> int:
    """把毫米网格画回原图，肉眼确认 H 没接反、没镜像。"""
    import cv2

    cfg = load_config(args.config)
    cal = load_intrinsics(args.calib)
    H = np.asarray(cal["H"], dtype=np.float64)
    intr = cal.get("intrinsics")

    raw = cv2.imread(args.image)
    if raw is None:
        print(f"读不到图片: {args.image}", file=sys.stderr)
        return 2
    img = undistort(raw, intr)

    step = args.grid_mm
    gx = np.arange(0.0, cfg.tray_w_mm + 1e-6, step)
    gy = np.arange(0.0, cfg.tray_h_mm + 1e-6, step)
    lines_mm = []
    for x in gx:
        lines_mm.append([[x, 0.0], [x, cfg.tray_h_mm]])
    for y in gy:
        lines_mm.append([[0.0, y], [cfg.tray_w_mm, y]])

    for seg in lines_mm:
        p = geometry.apply(H, np.asarray(seg, dtype=np.float64))
        cv2.line(img, tuple(np.int32(p[0])), tuple(np.int32(p[1])), (0, 255, 0), 1)

    corners = geometry.apply(H, tray_corners_mm(cfg))
    for i, p in enumerate(np.int32(corners)):
        cv2.circle(img, tuple(p), 8, (0, 0, 255), -1)
        cv2.putText(img, str(i + 1), (p[0] + 10, p[1] - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 255), 2)

    cv2.imwrite(args.output, img)

    sx, sy = px_per_mm(H, cfg)
    print(f"重投影 RMS = {cal.get('rms_px', float('nan')):.3f} px")
    print(f"比例 {sx:.2f} / {sy:.2f} px·mm⁻¹  （横纵差 {abs(sx - sy) / max(sx, sy) * 100:.1f}%）")
    if abs(sx - sy) / max(sx, sy) > 0.05:
        print("⚠ 横纵比例差超过 5%，四角可能点错了，或者托盘不是矩形被拍歪了")
    print(f"-> {args.output}   看绿网格是否贴合托盘边缘")


# --------------------------------------------------------------------------
# 自检：没有相机也能证明求解器是对的
# --------------------------------------------------------------------------

def cmd_selftest(args) -> int:
    """用合成场景的真值单应反过来验：喂 4 个角进去，能不能把 H 原样解回来。"""
    from ..vision import classes as vclasses
    from ..vision import synthetic

    cfg = load_config(args.config)
    book = vclasses.load(CONFIG_DIR / "classes.json")

    # 直接比 H 的元素没意义：透视项和线性项量级差几个数量级。
    # 工程上关心的是"按这个 H 去抓，点会偏多少毫米"，所以就量这个。
    gx = np.linspace(0.0, cfg.tray_w_mm, 7)
    gy = np.linspace(0.0, cfg.tray_h_mm, 7)
    grid = np.array([[x, y] for x in gx for y in gy], dtype=np.float64)

    print(f"{'seed':>4}  {'角点RMS(px)':>12}  {'网格最大偏移(mm)':>16}  {'px/mm':>6}")
    worst_px = worst_mm = 0.0
    for seed in range(args.n):
        scene = synthetic.build(book, seed=seed, per_class=2,
                                tray_w=cfg.tray_w_mm, tray_h=cfg.tray_h_mm)
        truth = np.linalg.inv(np.asarray(scene.H_px_to_mm, dtype=np.float64))  # 毫米->像素
        px = geometry.apply(truth, tray_corners_mm(cfg))
        px = px + np.random.default_rng(seed).normal(0.0, args.noise_px, px.shape)

        H, rms = solve_from_corners(px, cfg)
        sx, _ = px_per_mm(H, cfg)

        d_px = np.linalg.norm(geometry.apply(H, grid) - geometry.apply(truth, grid), axis=1)
        err_px = float(d_px.max())
        err_mm = err_px / max(sx, 1e-9)
        worst_px = max(worst_px, err_px)
        worst_mm = max(worst_mm, err_mm)
        print(f"{seed:>4}  {rms:>12.4f}  {err_mm:>16.3f}  {sx:>6.2f}")

    print()
    if args.noise_px <= 0:
        print(f"托盘内最大偏移 {worst_mm:.2e} mm（无噪声，应接近 0）")
        print("✓ 求解器正确" if worst_mm < 1e-6 else "✗ 求解器有问题")
        return 0 if worst_mm < 1e-6 else 1
    print(f"四角各加 {args.noise_px:.1f} px 随机误差 -> 托盘内最大偏离 {worst_mm:.2f} mm")
    print(f"（约等于夹爪半开的一半；3mm 以内都能夹住，但比像素级差一个数量级）")
    return 0


# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(prog="python -m sorting.tools.calibrate",
                                 description="相机标定（内参 + 托盘单应）")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("intrinsics", help="棋盘格标内参")
    p.add_argument("--images", nargs="+", required=True, help="图片路径或通配符")
    p.add_argument("--cols", type=int, default=9, help="内角点列数")
    p.add_argument("--rows", type=int, default=6, help="内角点行数")
    p.add_argument("--square-mm", type=float, default=20.0, dest="square_mm")
    p.add_argument("-o", "--output", default="intrinsics.json")
    p.set_defaults(func=cmd_intrinsics)

    p = sub.add_parser("tray", help="托盘四角标单应")
    p.add_argument("--image", required=True)
    p.add_argument("--intrinsics", help="intrinsics.json；给了就先去畸变")
    p.add_argument("--corners", nargs=4, metavar="X,Y",
                   help="不给就开窗口手点：左上 右上 右下 左下")
    p.add_argument("-o", "--output", default="calib.json")
    p.set_defaults(func=cmd_tray)

    p = sub.add_parser("check", help="把标定网格画回图上")
    p.add_argument("--calib", required=True)
    p.add_argument("--image", required=True)
    p.add_argument("--grid-mm", type=float, default=20.0, dest="grid_mm")
    p.add_argument("-o", "--output", default="check.png")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("selftest", help="用合成场景验证求解器")
    p.add_argument("-n", type=int, default=5)
    p.add_argument("--noise-px", type=float, default=0.0, dest="noise_px",
                   help="给四角加多少像素的随机误差（模拟手点不准）")
    p.set_defaults(func=cmd_selftest)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
