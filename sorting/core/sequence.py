"""单件工件抓放序列（夹爪版）。

用生成器写：`yield Action(...)` 把一步交给执行器，执行器把这一步的结果
`send()` 回来。这样"动作顺序"是纯函数（可以断言、可以单测），
"怎么执行"留在 app 层，两边互不污染。

一件工件的标准序列（docs/软件方案.md §6）：
    GRIP_OPEN -> NAV_PICK -> Z_DOWN -> GRIP_CLOSE -> HOLD_CHK -> Z_UP
              -> NAV_BIN -> Z_DOWN -> GRIP_OPEN -> Z_UP -> DISPLAY -> NEXT

HOLD_CHK 用的是**夹爪实际停止位置**：空夹时爪口会合到底（grip_close_mm），
夹到工件时会在工件宽度处堵转停下，停止位置明显更大。所以

    actual >= grip_hold_min_mm  ->  夹到了
    actual <  grip_hold_min_mm  ->  空夹，走 SKIP

夹不住时抬 Z、直接 DISPLAY（标注 NG），不往料盒跑——宁可跳过丢一件的分，
也不要把不对的东西放进错的盒子。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generator, Iterator

from ..config import Target


@dataclass(frozen=True)
class Action:
    """执行器需要做的一件事。kind 决定 args 怎么解释。

    kind      args                    说明
    --------  ----------------------  --------------------------------
    home      ()                      四轴回零
    goto      (x_mm, y_mm)            绝对 XY
    z         (mm,)                   绝对 Z
    grip      (mm,)                   夹爪张合轴绝对位置
    settle    (seconds,)              等待稳定
    check     ()   -> 回填 float      夹爪实际停止位置（mm）
    display   (target,)               刷新屏上当前件信息
    """

    kind: str
    args: tuple = ()

    def __str__(self) -> str:
        return f"{self.kind}{self.args}"


def run_item(target: Target) -> Generator[Action, Any, bool]:
    """驱动一件工件。返回值：True = 已入盒，False = 空夹跳过。"""
    cfg = target.cfg

    # 先张开，否则带件去下一个取点会刮到别的工件
    yield Action("grip", (cfg.grip_open_mm,))
    yield Action("goto", (target.pick_xy[0], target.pick_xy[1]))
    yield Action("z", (cfg.z_pick_mm,))
    yield Action("grip", (cfg.grip_close_mm,))
    yield Action("settle", (cfg.grip_settle_s,))
    actual = yield Action("check", ())

    held = actual is not None and actual >= cfg.grip_hold_min_mm

    yield Action("z", (cfg.z_safe_mm,))

    if not held:
        yield Action("display", (target,))
        return False

    yield Action("goto", (target.bin_xy[0], target.bin_xy[1]))
    yield Action("z", (cfg.z_drop_mm,))
    yield Action("grip", (cfg.grip_open_mm,))
    yield Action("settle", (cfg.grip_settle_s,))
    yield Action("z", (cfg.z_safe_mm,))
    yield Action("display", (target,))
    return True


def trace(target: Target, check_results: Iterator[float] | None = None) -> tuple[list[Action], bool]:
    """不连硬件，只把动作序列走一遍。测试和"预演"都用它。"""
    results = check_results if check_results is not None else iter(lambda: 99.0, None)
    gen = run_item(target)
    actions: list[Action] = []
    try:
        action = next(gen)
        while True:
            actions.append(action)
            fed = next(results, 99.0) if action.kind == "check" else None
            action = gen.send(fed)
    except StopIteration as stop:
        return actions, bool(stop.value)


def expected_duration_s(actions: list[Action], target: Target) -> float:
    """按齿轮传动算的解析节拍，用来配超时和做节拍估算。

    不算梯形加减速和齿轮回程间隙——那两项要等实测参数，先按匀速估。
    """
    cfg = target.cfg
    t = 0.0
    pos = {"x": 0.0, "y": 0.0, "z": 0.0, "grip": cfg.grip_open_mm}

    for a in actions:
        if a.kind == "goto":
            nx, ny = a.args
            t += max(cfg.x.travel_s(nx - pos["x"]), cfg.y.travel_s(ny - pos["y"]))
            pos["x"], pos["y"] = nx, ny
        elif a.kind == "z":
            (nz,) = a.args
            t += cfg.z.travel_s(nz - pos["z"])
            pos["z"] = nz
        elif a.kind == "grip":
            (ng,) = a.args
            t += cfg.grip.travel_s(ng - pos["grip"])
            pos["grip"] = ng
        elif a.kind == "settle":
            t += a.args[0]
        elif a.kind == "home":
            t += 6.0
    return t
