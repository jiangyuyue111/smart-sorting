"""任务队列：把检出的工件排成一个"走路最少"的抓放顺序。

为什么需要排序
--------------
每件工件的分是独立的，但**行走距离不是**。12 件 × (取点→料盒) 的路径如果不排，
XY 轴会来回横穿整机。180 秒一轮的预算下这不是优化项。

三种策略，都真算一遍，取最短的
------------------------------
赛规只要求"同形同色进同一个盒子"，**不要求同类连着做完**。所以排程是自由的
旅行商问题，不该先入为主地按类别分组——那样反而可能多走路。于是：

    detect   按检出顺序（大致是从上到下、从左到右），作为对照基线
    nn       全局最近邻：每步都去离当前最近的取点，不管类别
    class    同类一次做完：类别间 6! 全排列穷举，类别内最近邻
             唯一的好处是少换盒子，但会多走取点之间的路

三者的代价都用同一个函数算（当前点 → 取点 → 料盒 → 下一个取点），
所以数字可比。build() 直接返回最省的那个，并把三家都记下来供报告展示。

6! = 720 全排列 × 约 20 次距离计算 ≈ 1.4 万次浮点运算，微秒级，
没有任何理由用启发式近似。
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field

from ..config import Config, Target

Cost = tuple[float, ...]


@dataclass
class Skipped:
    """没进队列的件，报告里要列出来（为什么跳过要能说清）。"""

    idx: int
    reason: str
    cx_mm: float
    cy_mm: float


@dataclass
class Plan:
    targets: list[Target]
    cost_mm: float
    strategy: str
    costs: dict[str, float] = field(default_factory=dict)
    skipped: list[Skipped] = field(default_factory=list)

    @property
    def class_order(self) -> tuple[str, ...]:
        """按实际出现顺序去重，报告里画成 1 → 3 → 2 这样。"""
        seen: list[str] = []
        for t in self.targets:
            if t.category not in seen:
                seen.append(t.category)
        return tuple(seen)

    @property
    def by_class(self) -> dict[str, list[Target]]:
        out: dict[str, list[Target]] = {}
        for t in self.targets:
            out.setdefault(t.category, []).append(t)
        return out


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _walk_cost(seq: list[Target], start: tuple[float, float]) -> float:
    """当前点 → 取点 → 料盒 → 下一个取点 …… 逐段累加。"""
    cur, cost = start, 0.0
    for t in seq:
        cost += _dist(cur, t.pick_xy)
        cost += _dist(t.pick_xy, t.bin_xy)
        cur = t.bin_xy
    return cost


def _nearest_neighbour(items: list[Target], start: tuple[float, float]
                       ) -> tuple[list[Target], float]:
    remaining = list(items)
    cur = start
    order: list[Target] = []
    cost = 0.0
    while remaining:
        i = min(range(len(remaining)), key=lambda k: _dist(cur, remaining[k].pick_xy))
        t = remaining.pop(i)
        cost += _dist(cur, t.pick_xy) + _dist(t.pick_xy, t.bin_xy)
        cur = t.bin_xy
        order.append(t)
    return order, cost


def _grouped(items: list[Target], start: tuple[float, float],
             max_permutations: int = 5040) -> tuple[list[Target], float]:
    """同类一次做完 + 类别间穷举。类别太多时退化成按料盒 X 排序。"""
    groups: dict[str, list[Target]] = {}
    for t in items:
        groups.setdefault(t.category, []).append(t)

    cats = sorted(groups)
    if math.factorial(len(cats)) > max_permutations:
        orders = [tuple(sorted(cats, key=lambda c: groups[c][0].bin_xy[0]))]
    else:
        orders = list(itertools.permutations(cats))

    best: list[Target] = []
    best_cost = float("inf")
    for order in orders:
        cur = start
        seq: list[Target] = []
        cost = 0.0
        for cat in order:
            sub, c = _nearest_neighbour(groups[cat], cur)
            if sub:
                cur = sub[-1].bin_xy
            cost += c
            seq.extend(sub)
        if cost < best_cost:
            best, best_cost = seq, cost
    return best, best_cost


def build(dets, cfg: Config, start_xy: tuple[float, float] = (0.0, 0.0)) -> Plan:
    """把检测结果排成抓放队列。

    dets 是 vision.detect.Detection 列表；未识别（verdict.cls is None）的
    一律不进队列，记到 skipped 里——宁可不抓，也不能抓错的扔进对的盒子。
    """
    target_list: list[Target] = []
    skipped: list[Skipped] = []

    for d in dets:
        if not d.ok:
            skipped.append(Skipped(d.idx, d.verdict.reason, d.cx_mm, d.cy_mm))
            continue
        cls = d.verdict.cls
        if cls.id not in cfg.bins:
            skipped.append(Skipped(d.idx, f"料盒 {cls.id} 未配置", d.cx_mm, d.cy_mm))
            continue
        target_list.append(Target(
            index=d.idx,
            label=cls.name,
            category=cls.id,
            pick_xy=cfg.tray_to_machine(d.cx_mm, d.cy_mm),
            cfg=cfg,
            width_mm=float(d.width_mm),
        ))

    if not target_list:
        return Plan([], 0.0, "empty", {}, skipped)

    candidates: dict[str, tuple[list[Target], float]] = {
        "detect": (list(target_list), _walk_cost(target_list, start_xy)),
        "nn": _nearest_neighbour(target_list, start_xy),
        "class": _grouped(target_list, start_xy),
    }
    strategy = min(candidates, key=lambda k: candidates[k][1])
    seq, cost = candidates[strategy]

    return Plan(
        targets=seq,
        cost_mm=cost,
        strategy=strategy,
        costs={k: v[1] for k, v in candidates.items()},
        skipped=skipped,
    )


def describe(plan: Plan) -> str:
    """一行摘要，日志和报告都用它。"""
    if not plan.targets:
        return "队列为空"
    per = {c: len(v) for c, v in plan.by_class.items()}
    detail = " ".join(f"{c}×{per[c]}" for c in plan.class_order)
    return (f"{len(plan.targets)} 件 / {len(plan.class_order)} 类 "
            f"[{detail}] 策略 {plan.strategy} 行走 {plan.cost_mm:.0f} mm")
