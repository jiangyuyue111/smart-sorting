"""UART 行协议：上位机（树莓派 4B）<-> STM32F407 运动控制 MCU。

帧格式：一行一条消息，ASCII，以 "\\n" 结束，字段用单个空格分隔。
**上位机是链路上唯一的写入方**；MCU 只回包，或异步上报状态。

四个电机 = 四个轴，统一用"到哪、多少毫米"来描述：

上位机 -> MCU
    GOTO <x_mm> <y_mm>   绝对 XY 移动（mm，一位小数）
    Z <mm>               绝对 Z 移动
    GRIP <mm>            夹爪张合轴绝对位置（0 = 全闭，40 = 全张）
    HOME                 四轴回零
    STOP                 立即中止当前动作
    PING                 存活探测

MCU -> 上位机
    OK                   指令已接受
    DONE                 动作完成
    PONG                 对 PING 的回复
    GRIPD <mm>           夹爪**实际停止位置**（异步上报，见下）
    ERR <code> <text>    故障

夹爪怎么判断"夹到了东西"
--------------------------
夹爪电机用 TMC2209 的 StallGuard 做堵转检测。空夹时爪口会一直合到
grip_close_mm（全闭）；夹到工件时会在工件宽度处堵转停下，停止位置明显
更大。于是：

    GRIPD 的值 ≥ grip_hold_min_mm  ->  夹到了
    GRIPD 的值 <  grip_hold_min_mm  ->  空夹，该件走 SKIP 分支

这比加一个压力/光电开关省事，而且不需要额外 IO。注意这是**位置判据**，
不是力度判据：夹太紧会压坏件，力矩上限在 MCU 侧限制。

设计约束（来自 docs/软件方案.md §5）：
  * 只传"到哪、干什么"，不传插补参数——插补由 MCU 自己做。
  * 每条指令都必须有 OK 应答，运动类还要有 DONE，上位机据此做超时判定。
  * 解析不出的行直接丢弃并计数，不抛到主循环。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ProtocolError(ValueError):
    """收到语法不合法的行。"""


class ErrorCode(str, Enum):
    OUT_OF_RANGE = "E01"       # 超行程
    LIMIT_NOT_FOUND = "E02"    # 回零时找不到限位
    UNKNOWN_COMMAND = "E03"    # MCU 不认识这条指令
    GRIP_TIMEOUT = "E04"       # 夹爪动作超时（没走到目标也没堵转）


# --------------------------------------------------------------------------
# 上位机 -> MCU
# --------------------------------------------------------------------------

class Command:
    """所有下行指令的基类。"""

    def to_line(self) -> str:
        raise NotImplementedError


@dataclass(frozen=True)
class Goto(Command):
    x_mm: float
    y_mm: float

    def to_line(self) -> str:
        return f"GOTO {self.x_mm:.1f} {self.y_mm:.1f}"


@dataclass(frozen=True)
class ZMove(Command):
    mm: float

    def to_line(self) -> str:
        return f"Z {self.mm:.1f}"


@dataclass(frozen=True)
class Grip(Command):
    """夹爪张合轴绝对位置。"""

    mm: float

    def to_line(self) -> str:
        return f"GRIP {self.mm:.1f}"


@dataclass(frozen=True)
class Home(Command):
    def to_line(self) -> str:
        return "HOME"


@dataclass(frozen=True)
class Stop(Command):
    def to_line(self) -> str:
        return "STOP"


@dataclass(frozen=True)
class Ping(Command):
    def to_line(self) -> str:
        return "PING"


# --------------------------------------------------------------------------
# MCU -> 上位机
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Ack:
    """OK / DONE / PONG —— 无载荷的确认。"""

    kind: str

    def __post_init__(self) -> None:
        if self.kind not in _ACK_KINDS:
            raise ProtocolError(f"未知应答: {self.kind}")


_ACK_KINDS = frozenset({"OK", "DONE", "PONG"})

OK = Ack("OK")
DONE = Ack("DONE")
PONG = Ack("PONG")


@dataclass(frozen=True)
class GripReport:
    """夹爪实际停止位置。堵转即停，所以这个值说明了夹到了多厚的件。"""

    actual_mm: float


@dataclass(frozen=True)
class Fault:
    """MCU 报警。收到即应中止当前工序并进 FAULT。"""

    code: ErrorCode
    text: str


Message = Command | Ack | GripReport | Fault


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------

def _as_float(token: str, what: str) -> float:
    try:
        return float(token)
    except ValueError as exc:
        raise ProtocolError(f"{what} 不是数字: {token!r}") from exc


def parse(line: str) -> Message:
    """把一行文本解析成消息对象。语法错误抛 ProtocolError。"""
    text = line.strip()
    if not text:
        raise ProtocolError("空行")
    parts = text.split()
    head = parts[0].upper()

    if head in _ACK_KINDS:
        if len(parts) != 1:
            raise ProtocolError(f"{head} 不应带参数: {text!r}")
        return Ack(head)

    if head == "GRIPD":
        if len(parts) != 2:
            raise ProtocolError(f"GRIPD 需要 1 个数值: {text!r}")
        return GripReport(_as_float(parts[1], "grip"))

    if head == "ERR":
        if len(parts) < 2:
            raise ProtocolError(f"ERR 缺错误码: {text!r}")
        try:
            code = ErrorCode(parts[1].upper())
        except ValueError as exc:
            raise ProtocolError(f"未知错误码: {parts[1]!r}") from exc
        return Fault(code=code, text=" ".join(parts[2:]))

    if head == "GOTO":
        if len(parts) != 3:
            raise ProtocolError(f"GOTO 需要 2 个坐标: {text!r}")
        return Goto(_as_float(parts[1], "x"), _as_float(parts[2], "y"))

    if head == "Z":
        if len(parts) != 2:
            raise ProtocolError(f"Z 需要 1 个坐标: {text!r}")
        return ZMove(_as_float(parts[1], "z"))

    if head == "GRIP":
        if len(parts) != 2:
            raise ProtocolError(f"GRIP 需要 1 个位置: {text!r}")
        return Grip(_as_float(parts[1], "grip"))

    if head == "HOME":
        return Home()

    if head == "STOP":
        return Stop()

    if head == "PING":
        return Ping()

    raise ProtocolError(f"未知指令: {head!r}")


def encode(msg: Message) -> str:
    """把消息编码成一行（不含换行符）。"""
    if isinstance(msg, Command):
        return msg.to_line()
    if isinstance(msg, Ack):
        return msg.kind
    if isinstance(msg, GripReport):
        return f"GRIPD {msg.actual_mm:.1f}"
    if isinstance(msg, Fault):
        return f"ERR {msg.code.value} {msg.text}".rstrip()
    raise TypeError(f"无法编码: {msg!r}")
