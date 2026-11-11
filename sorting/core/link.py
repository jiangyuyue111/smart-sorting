"""链路层：把"发一行 / 收一行"抽象出来，让上层代码在没板子时也能跑。

三个实现：
  SerialLink  真实串口（pyserial），后台读线程 -> 队列，避免阻塞主循环
  MockLink    模拟 MCU：按齿轮传动算出每个轴的实际耗时，无板也能跑完整工序

Session 在 Link 之上提供"发一条指令、等 OK、再等 DONE"的同步语义，
并负责把异步上报（GRIPD / ERR）接住，不让它们串到指令应答里。

时间是怎么算的
--------------
MockLink 记录的是**解析耗时**（距离 ÷ 该轴速度），不是墙上时间。
所以 `--speed 8` 只是让演示播得快一点，报告里的节拍数字完全不受影响——
这一点很重要：中期汇报的节拍表必须是真数字，不能跟着播放速度变。
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field

from . import protocol as proto


class LinkTimeout(TimeoutError):
    """在超时内没有等到期望的消息。"""


class McuFault(RuntimeError):
    """MCU 上报了 ERR。"""

    def __init__(self, code: proto.ErrorCode, text: str) -> None:
        super().__init__(f"MCU 故障 {code.value}: {text}")
        self.code = code
        self.text = text


@dataclass
class Motion:
    """一次轴运动的解析记录，用来做节拍表 / 甘特图。"""

    axis: str
    start_mm: float
    end_mm: float
    duration_s: float
    t_start_s: float        # 相对本次运行起点的解析时刻

    @property
    def distance_mm(self) -> float:
        return abs(self.end_mm - self.start_mm)


@dataclass
class SessionLog:
    """一次运行里所有动作的账。报告直接从它生成。"""

    motions: list[Motion] = field(default_factory=list)
    grips: list[dict] = field(default_factory=list)
    commands: int = 0
    bad_lines: int = 0

    @property
    def total_motion_s(self) -> float:
        return sum(m.duration_s for m in self.motions)


class Link:
    def send(self, line: str) -> None:
        raise NotImplementedError

    def poll(self, timeout: float) -> proto.Message | None:
        """取一条消息，超时返回 None。"""
        raise NotImplementedError

    def close(self) -> None:
        pass

    @property
    def log(self) -> SessionLog:
        return SessionLog()


# --------------------------------------------------------------------------
# 真实串口
# --------------------------------------------------------------------------

class SerialLink(Link):
    def __init__(self, port: str, baudrate: int = 115200) -> None:
        import serial  # 只在真机上才需要

        self._ser = serial.Serial(port, baudrate, timeout=0.05, write_timeout=1.0)
        self._lock = threading.Lock()
        self._rx: queue.Queue[proto.Message] = queue.Queue()
        self._stop = threading.Event()
        self._bad_lines = 0
        self._reader = threading.Thread(target=self._read_loop, name="serial-rx", daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        buf = bytearray()
        while not self._stop.is_set():
            try:
                chunk = self._ser.read(256)
            except Exception:
                break
            if not chunk:
                continue
            buf.extend(chunk)
            while b"\n" in buf:
                raw, _, rest = buf.partition(b"\n")
                buf = bytearray(rest)
                line = raw.decode("ascii", errors="replace").strip()
                if not line:
                    continue
                try:
                    self._rx.put(proto.parse(line))
                except proto.ProtocolError:
                    # 解析不出的行直接丢，不能让 MCU 的一行噪声打死主循环
                    self._bad_lines += 1

    def send(self, line: str) -> None:
        with self._lock:
            self._ser.write((line + "\n").encode("ascii"))
            self._ser.flush()

    def poll(self, timeout: float) -> proto.Message | None:
        try:
            return self._rx.get(timeout=timeout)
        except queue.Empty:
            return None

    @property
    def bad_lines(self) -> int:
        return self._bad_lines

    def close(self) -> None:
        self._stop.set()
        try:
            self._ser.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# 模拟 MCU（四个轴）
# --------------------------------------------------------------------------

class MockLink(Link):
    """四个轴 + 夹爪的 MCU 替身。

    speed_factor = 0   立即完成，只算解析时间（出报告用，最快）
    speed_factor = 1   按真实速度播
    speed_factor = 8   8 倍速播放

    夹爪堵转：把 `blocker_mm` 设成工件宽度，GRIP 指令往更小位置走时就会
    停在工件宽度处并回 GRIPD——这正是真机上 StallGuard 的行为。
    """

    def __init__(
        self,
        axes: dict | None = None,
        speed_factor: float = 0.0,
        homing_s: float = 6.0,
        grip_settle_s: float = 0.15,
    ) -> None:
        # axes: {"x": Axis, ...}；给 None 时按默认速度近似（单元测试用）
        self.axes = axes or {}
        self.speed_factor = speed_factor
        self.homing_s = homing_s
        self.grip_settle_s = grip_settle_s

        self.blocker_mm: float | None = None   # 工件挡在夹爪里的位置

        self._due: queue.Queue[tuple[float, proto.Message]] = queue.Queue()
        self._pos = {"x": 0.0, "y": 0.0, "z": 0.0, "grip": 0.0}
        self._elapsed = 0.0                    # 解析时间轴
        self._log = SessionLog()
        self._bad = 0

    # -- 内部 ------------------------------------------------------------
    def _schedule(self, delay_s: float, msg: proto.Message) -> None:
        if self.speed_factor <= 0:
            wall = time.monotonic()
        else:
            wall = time.monotonic() + delay_s / self.speed_factor
        self._due.put((wall, msg))

    def _speed(self, axis: str) -> float:
        ax = self.axes.get(axis)
        return ax.speed_mm_s if ax else 200.0

    def _move(self, axis: str, target: float, advance_elapsed: bool = True) -> float:
        """执行一次轴运动，记录解析耗时，返回耗时。

        advance_elapsed=False 用于 XY 联动：两根轴同时在走，时间轴只能推进一步
        （取两者的较大值），否则解析出来的节拍会是两轴时间之和，凭空变慢一倍。
        """
        start = self._pos[axis]
        dist = abs(target - start)
        dur = dist / self._speed(axis) if self._speed(axis) > 0 else 0.0
        self._log.motions.append(
            Motion(axis=axis, start_mm=start, end_mm=target,
                   duration_s=dur, t_start_s=self._elapsed)
        )
        if advance_elapsed:
            self._elapsed += dur
        self._pos[axis] = target
        return dur

    # -- Link 接口 --------------------------------------------------------
    def send(self, line: str) -> None:
        self._log.commands += 1
        try:
            msg = proto.parse(line)
        except proto.ProtocolError:
            self._bad += 1
            self._schedule(0.0, proto.Fault(proto.ErrorCode.UNKNOWN_COMMAND, line))
            return

        if isinstance(msg, proto.Goto):
            # XY 同时走：两根轴并行，这一段的耗时取两者的较大值
            d1 = self._move("x", msg.x_mm, advance_elapsed=False)
            d2 = self._move("y", msg.y_mm, advance_elapsed=False)
            self._elapsed += max(d1, d2)
            self._schedule(0.0, proto.OK)
            self._schedule(max(d1, d2), proto.DONE)

        elif isinstance(msg, proto.ZMove):
            d = self._move("z", msg.mm)
            self._schedule(0.0, proto.OK)
            self._schedule(d, proto.DONE)

        elif isinstance(msg, proto.Grip):
            target = msg.mm
            actual = target
            stalled = False
            if self.blocker_mm is not None and target < self.blocker_mm < self._pos["grip"]:
                actual = self.blocker_mm
                stalled = True
            d = self._move("grip", actual)
            self._log.grips.append({
                "commanded_mm": target, "actual_mm": actual, "stalled": stalled,
                "t_start_s": self._elapsed - d, "duration_s": d,
            })
            self._schedule(0.0, proto.OK)
            self._schedule(d, proto.DONE)
            self._schedule(d + self.grip_settle_s, proto.GripReport(actual))

        elif isinstance(msg, proto.Home):
            self._elapsed += self.homing_s
            for a in self._pos:
                self._pos[a] = 0.0
            self._schedule(0.0, proto.OK)
            self._schedule(self.homing_s, proto.DONE)

        elif isinstance(msg, proto.Stop):
            self._schedule(0.0, proto.OK)

        elif isinstance(msg, proto.Ping):
            self._schedule(0.0, proto.PONG)

    def poll(self, timeout: float) -> proto.Message | None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                due, msg = self._due.queue[0]  # 只看队首
            except IndexError:
                msg = None
            if msg is not None and due <= time.monotonic():
                self._due.get_nowait()
                return msg
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.002)

    @property
    def log(self) -> SessionLog:
        self._log.bad_lines = self._bad
        return self._log

    def advance(self, dt: float) -> None:
        """把解析时间轴往前推一段（等待稳定、相机曝光这类不产生运动的时间）。"""
        self._elapsed += max(dt, 0.0)

    @property
    def elapsed_s(self) -> float:
        return self._elapsed

    @property
    def position(self) -> dict[str, float]:
        return dict(self._pos)


# --------------------------------------------------------------------------
# 会话：指令 -> OK -> DONE
# --------------------------------------------------------------------------

class Session:
    def __init__(
        self,
        link: Link,
        ack_timeout_s: float = 0.5,
        done_timeout_s: float = 15.0,
        item_timeout_s: float = 0.3,
    ) -> None:
        self.link = link
        self.ack_timeout_s = ack_timeout_s
        self.done_timeout_s = done_timeout_s
        self.item_timeout_s = item_timeout_s
        self.last_grip_mm: float | None = None

    def _wait_for(self, kinds: set[str], timeout_s: float) -> proto.Ack:
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LinkTimeout(f"等待 {kinds} 超时")
            msg = self.link.poll(remaining)
            if msg is None:
                raise LinkTimeout(f"等待 {kinds} 超时")
            if isinstance(msg, proto.Fault):
                raise McuFault(msg.code, msg.text)
            if isinstance(msg, proto.GripReport):
                self.last_grip_mm = msg.actual_mm
                continue
            if isinstance(msg, proto.Ack) and msg.kind in kinds:
                return msg

    def command(self, cmd: proto.Command, expect_done: bool = True) -> None:
        self.link.send(cmd.to_line())
        self._wait_for({"OK"}, self.ack_timeout_s)
        if expect_done:
            self._wait_for({"DONE"}, self.done_timeout_s)

    def grip(self, mm: float) -> float:
        """发 GRIP，等 OK/DONE，再等 GRIPD，返回夹爪实际停止位置。

        GRIPD 是异步上报，可能落在 OK 和 DONE 之间，也可能在 DONE 之后，
        所以不能假设顺序——单独开一段窗口专门等它。
        """
        self.last_grip_mm = None
        self.link.send(proto.Grip(mm).to_line())
        self._wait_for({"OK"}, self.ack_timeout_s)
        self._wait_for({"DONE"}, self.done_timeout_s)

        deadline = time.monotonic() + self.item_timeout_s
        while self.last_grip_mm is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LinkTimeout("夹爪没有回报实际停止位置（GRIPD）")
            msg = self.link.poll(remaining)
            if msg is None:
                raise LinkTimeout("夹爪没有回报实际停止位置（GRIPD）")
            if isinstance(msg, proto.Fault):
                raise McuFault(msg.code, msg.text)
            if isinstance(msg, proto.GripReport):
                self.last_grip_mm = msg.actual_mm
        return self.last_grip_mm

    def ping(self) -> None:
        self.link.send(proto.Ping().to_line())
        self._wait_for({"PONG"}, self.ack_timeout_s)

    def stop(self) -> None:
        self.link.send(proto.Stop().to_line())
        self._wait_for({"OK"}, self.ack_timeout_s)

    def close(self) -> None:
        self.link.close()
