"""配置驱动：机器参数全部来自 machine.toml，代码里不写死任何坐标和传动比。

为什么这么做——现场要换货（≥12 件 6 类，随机摆放），托盘和盒位的实际位置
每场都会动；齿轮参数在装配前也定不下来。把坐标、传动、行程、速度、超时全
放进配置文件，换场/换机构只改一个文件、不重新编译。

四个轴统一用一种模型描述：
    linear  直线轴（X / Y / Z），坐标单位 mm
    grip    夹爪张合轴，坐标也是 mm（0 = 闭合，max = 全张），
            这样步数换算、行程校验、序列代码都能复用同一套逻辑

传动一律按齿轮算，见 machine.toml 顶部的公式。
"""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(ValueError):
    pass


# --------------------------------------------------------------------------
# 轴
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Axis:
    """一根轴：齿轮参数 + 行程 + 速度。"""

    name: str
    kind: str                  # "linear" | "grip"
    module_mm: float           # 齿轮模数
    teeth: int                 # 齿数
    reduction: float           # 减速比（电机:输出）
    steps_per_rev: int
    microstep: int
    max_mm: float
    speed_mm_s: float

    @property
    def pitch_dia_mm(self) -> float:
        """分度圆直径 d = m·z"""
        return self.module_mm * self.teeth

    @property
    def mm_per_rev(self) -> float:
        """输出轴转一圈，负载走多少 mm。齿条按分度圆周长展开。"""
        return math.pi * self.pitch_dia_mm / self.reduction

    @property
    def steps_per_mm(self) -> float:
        if self.mm_per_rev <= 0:
            raise ConfigError(f"{self.name}: mm_per_rev 必须为正")
        return self.steps_per_rev * self.microstep / self.mm_per_rev

    def mm_to_steps(self, mm: float) -> int:
        return round(mm * self.steps_per_mm)

    def travel_s(self, distance_mm: float) -> float:
        return abs(distance_mm) / self.speed_mm_s if self.speed_mm_s > 0 else 0.0


# --------------------------------------------------------------------------
# 整机配置
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Meta:
    """标定状态。只影响报告怎么措辞，不影响任何计算。"""

    calibrated: bool = False
    pending: tuple[str, ...] = ()

    @property
    def qualifier(self) -> str:
        """放在数字后面的话。没标定就不许把解析值说成实测值。"""
        return "实测" if self.calibrated else "解析估算"


@dataclass(frozen=True)
class Config:
    meta: Meta
    axes: dict[str, Axis]

    # -- 承载（把轴拿出来做快捷属性，下面那些老代码不用到处 self.axes["x"]）
    x: Axis
    y: Axis
    z: Axis
    grip: Axis

    # -- 工艺 ------------------------------------------------------------
    z_pick_mm: float
    z_drop_mm: float
    z_safe_mm: float

    grip_open_mm: float
    grip_close_mm: float
    grip_hold_min_mm: float
    grip_settle_s: float

    backlash_mm: float

    # -- 时序 ------------------------------------------------------------
    ack_timeout_s: float
    done_timeout_s: float
    heartbeat_s: float

    # -- 托盘 ------------------------------------------------------------
    tray_w_mm: float
    tray_h_mm: float
    tray_x0_mm: float
    tray_y0_mm: float

    # -- 料盒 ------------------------------------------------------------
    bins: dict[str, tuple[float, float]] = field(default_factory=dict)

    # -- 便捷换算 ---------------------------------------------------------
    def bin_xy(self, category: str) -> tuple[float, float]:
        try:
            return self.bins[str(category)]
        except KeyError as exc:
            raise ConfigError(f"没有编号为 {category!r} 的料盒") from exc

    @property
    def bin_count(self) -> int:
        return len(self.bins)

    def tray_to_machine(self, tx: float, ty: float) -> tuple[float, float]:
        """托盘坐标 -> 整机坐标。视觉输出用托盘系，运动用整机系。"""
        return (self.tray_x0_mm + tx, self.tray_y0_mm + ty)


@dataclass(frozen=True)
class Target:
    """一件待分拣工件。index 是屏上显示的序号（赛规要求）。"""

    index: int
    label: str                 # 屏上"名称"字段
    category: str              # 料盒编号 "1".."6"
    pick_xy: tuple[float, float]   # 整机坐标 mm
    cfg: Config = field(compare=False)
    thumb: object = field(default=None, compare=False)  # 可选缩略图 (PIL.Image)
    width_mm: float = field(default=0.0, compare=False)  # 夹爪要跨过的宽度

    @property
    def bin_xy(self) -> tuple[float, float]:
        return self.cfg.bin_xy(self.category)


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------

def _need(d: dict, key: str, kind, where: str):
    if key not in d:
        raise ConfigError(f"[{where}] 缺少配置项: {key}")
    v = d[key]
    if kind is float:
        if not isinstance(v, (int, float)):
            raise ConfigError(f"[{where}] {key} 应为数字")
        return float(v)
    if kind is int:
        if not isinstance(v, int) or isinstance(v, bool):
            raise ConfigError(f"[{where}] {key} 应为整数")
        return v
    if not isinstance(v, kind):
        raise ConfigError(f"[{where}] {key} 类型不对")
    return v


def _parse_axis(name: str, raw: dict) -> Axis:
    return Axis(
        name=name,
        kind=_need(raw, "kind", str, name),
        module_mm=_need(raw, "module_mm", float, name),
        teeth=_need(raw, "teeth", int, name),
        reduction=_need(raw, "reduction", float, name),
        steps_per_rev=_need(raw, "steps_per_rev", int, name),
        microstep=_need(raw, "microstep", int, name),
        max_mm=_need(raw, "max_mm", float, name),
        speed_mm_s=_need(raw, "speed_mm_s", float, name),
    )


def load(path: str | Path) -> Config:
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    axes_raw = raw.get("axis", {})
    missing = {"x", "y", "z", "grip"} - set(axes_raw)
    if missing:
        raise ConfigError(f"缺少轴定义: {sorted(missing)}")
    axes = {n: _parse_axis(n, axes_raw[n]) for n in ("x", "y", "z", "grip")}

    p = raw.get("process", {})
    t = raw.get("timing", {})
    tray = raw.get("tray", {})
    bins_raw = raw.get("bins", {})
    meta_raw = raw.get("meta", {})

    meta = Meta(
        calibrated=bool(meta_raw.get("calibrated", False)),
        pending=tuple(meta_raw.get("pending", [])),
    )

    cfg = Config(
        meta=meta,
        axes=axes,
        x=axes["x"], y=axes["y"], z=axes["z"], grip=axes["grip"],
        z_pick_mm=_need(p, "z_pick_mm", float, "process"),
        z_drop_mm=_need(p, "z_drop_mm", float, "process"),
        z_safe_mm=_need(p, "z_safe_mm", float, "process"),
        grip_open_mm=_need(p, "grip_open_mm", float, "process"),
        grip_close_mm=_need(p, "grip_close_mm", float, "process"),
        grip_hold_min_mm=_need(p, "grip_hold_min_mm", float, "process"),
        grip_settle_s=_need(p, "grip_settle_s", float, "process"),
        backlash_mm=float(p.get("backlash_mm", 0.0)),
        ack_timeout_s=_need(t, "ack_timeout_s", float, "timing"),
        done_timeout_s=_need(t, "done_timeout_s", float, "timing"),
        heartbeat_s=_need(t, "heartbeat_s", float, "timing"),
        tray_w_mm=_need(tray, "width_mm", float, "tray"),
        tray_h_mm=_need(tray, "height_mm", float, "tray"),
        tray_x0_mm=_need(tray, "x0_mm", float, "tray"),
        tray_y0_mm=_need(tray, "y0_mm", float, "tray"),
        bins={str(k): (float(v[0]), float(v[1])) for k, v in bins_raw.items()},
    )
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    if not cfg.bins:
        raise ConfigError("至少要配一个料盒")

    for ax in cfg.axes.values():
        if ax.max_mm <= 0:
            raise ConfigError(f"{ax.name}: max_mm 必须为正")
        if ax.speed_mm_s <= 0:
            raise ConfigError(f"{ax.name}: speed_mm_s 必须为正")
        if ax.teeth <= 0 or ax.module_mm <= 0 or ax.reduction <= 0:
            raise ConfigError(f"{ax.name}: 齿轮参数必须为正")

    if not 0 <= cfg.z_pick_mm <= cfg.z.max_mm:
        raise ConfigError(f"z_pick_mm 超出行程 0..{cfg.z.max_mm}")
    if not 0 <= cfg.z_drop_mm <= cfg.z.max_mm:
        raise ConfigError(f"z_drop_mm 超出行程 0..{cfg.z.max_mm}")
    if cfg.z_safe_mm <= cfg.z_pick_mm or cfg.z_safe_mm <= cfg.z_drop_mm:
        raise ConfigError("z_safe_mm 必须高于 z_pick_mm 和 z_drop_mm")

    if not 0 <= cfg.grip_close_mm < cfg.grip_hold_min_mm <= cfg.grip_open_mm <= cfg.grip.max_mm:
        raise ConfigError(
            "夹爪参数应满足 0 ≤ grip_close_mm < grip_hold_min_mm ≤ grip_open_mm ≤ max_mm"
        )

    for name, (bx, by) in cfg.bins.items():
        if not (0 <= bx <= cfg.x.max_mm and 0 <= by <= cfg.y.max_mm):
            raise ConfigError(f"料盒 {name} 的坐标 {bx},{by} 超出 X/Y 行程")

    if cfg.tray_x0_mm + cfg.tray_w_mm > cfg.x.max_mm:
        raise ConfigError("托盘超出 X 行程")
    if cfg.tray_y0_mm + cfg.tray_h_mm > cfg.y.max_mm:
        raise ConfigError("托盘超出 Y 行程")


def check_in_range(cfg: Config, x_mm: float, y_mm: float) -> None:
    """移动前先在上位机侧拦一道，别指望 MCU 的 E01 兜底。"""
    if not (0 <= x_mm <= cfg.x.max_mm) or not (0 <= y_mm <= cfg.y.max_mm):
        raise ConfigError(f"目标 ({x_mm}, {y_mm}) 超出行程")
