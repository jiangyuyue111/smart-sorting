"""执行器：把 sequence 产出的 Action 变成真正发给 MCU 的指令。

这一层是唯一知道"动作 -> 指令"对应关系的地方。sequence 里只写工艺顺序，
这里写协议，换通信方式（串口/模拟）不用碰工艺代码。

时间戳的来源分两种，靠 Link 自己报告：
    真串口  -> 用墙上时间
    MockLink -> 用 link.elapsed_s，也就是按齿轮传动算出的解析时间轴
这样 `--speed 8` 只影响演示播多快，报告里的节拍数字不变。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..config import Config
from ..core import protocol as proto
from ..core import sequence
from ..core.link import Link, Session


@dataclass
class StepRecord:
    kind: str
    args: tuple
    t_s: float


@dataclass
class ItemRecord:
    index: int
    name: str
    category: str
    held: bool
    actual_mm: float | None
    t_start_s: float
    t_end_s: float
    pos: int = 0
    steps: list[StepRecord] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return self.t_end_s - self.t_start_s


@dataclass
class RunResult:
    items: list[ItemRecord] = field(default_factory=list)
    total_s: float = 0.0
    idle_s: float = 0.0
    injected: set[int] = field(default_factory=set)

    @property
    def placed(self) -> int:
        return sum(1 for i in self.items if i.held)

    @property
    def skipped(self) -> int:
        return sum(1 for i in self.items if not i.held)


def _clock(link: Link, t0_wall: float) -> float:
    el = getattr(link, "elapsed_s", None)
    if el is not None:
        return float(el)
    return time.monotonic() - t0_wall


def _idle(link: Link, dt: float, speed_factor: float) -> None:
    """等待。解析时间轴要跟上，墙上时间只在播放模式下等。"""
    if hasattr(link, "advance"):
        link.advance(dt)
    if speed_factor > 0:
        time.sleep(dt / speed_factor)


def execute(plan, link: Link, cfg: Config, *,
            speed_factor: float = 0.0,
            on_before_item=None,
            on_display=None,
            inject_skip: set[int] | None = None) -> RunResult:
    """按计划逐件执行。

    on_before_item(pos, target)  开始一件之前（pos 从 1 起，用来刷新屏幕为"搬运中"）
    on_display(pos, record, target)  一件处理完（用来刷新屏幕为"已入盒/跳过"）
    inject_skip                  **仅供演示**：人为让这些序号空夹，
                                 用来展示 SKIP 分支。默认空，报告里会标注。
    """
    inject_skip = inject_skip or set()
    session = Session(link, cfg.ack_timeout_s, cfg.done_timeout_s, item_timeout_s=2.0)
    t0_wall = time.monotonic()
    result = RunResult()
    result.injected = set(inject_skip)

    for pos, target in enumerate(plan.targets, start=1):
        if on_before_item:
            on_before_item(pos, target)

        t_start = _clock(link, t0_wall)
        rec = ItemRecord(
            index=target.index, name=target.label, category=target.category,
            held=False, actual_mm=None, t_start_s=t_start, t_end_s=t_start,
        )

        gen = sequence.run_item(target)
        last_grip: float | None = None
        action = next(gen)
        try:
            while True:
                rec.steps.append(StepRecord(action.kind, action.args, _clock(link, t0_wall)))
                feed = None

                if action.kind == "goto":
                    x, y = action.args
                    session.command(proto.Goto(x, y))
                elif action.kind == "z":
                    session.command(proto.ZMove(action.args[0]))
                elif action.kind == "grip":
                    # 演示用：让夹爪在这件上空夹，其余正常
                    if hasattr(link, "blocker_mm"):
                        link.blocker_mm = (None if target.index in inject_skip
                                           else max(target.width_mm, 0.0) or None)
                    last_grip = session.grip(action.args[0])
                elif action.kind == "settle":
                    _idle(link, action.args[0], speed_factor)
                    result.idle_s += action.args[0]
                elif action.kind == "check":
                    feed = last_grip
                elif action.kind == "home":
                    session.command(proto.Home())

                action = gen.send(feed)
        except StopIteration as stop:
            rec.held = bool(stop.value)
            rec.actual_mm = last_grip

        rec.t_end_s = _clock(link, t0_wall)
        rec.pos = pos
        result.items.append(rec)
        if on_display:
            on_display(pos, rec, target)

    result.total_s = _clock(link, t0_wall)
    return result


def home(link: Link, cfg: Config, speed_factor: float = 0.0) -> None:
    session = Session(link, cfg.ack_timeout_s, cfg.done_timeout_s, item_timeout_s=2.0)
    session.command(proto.Home())
    if speed_factor > 0:
        time.sleep(6.0 / speed_factor)
