"""顶层状态机：BOOT -> SELF_TEST -> STANDBY -> ARMED -> RUNNING -> FINISH。

刻意做得很小：只描述"整机处于哪个阶段"，单件工件的动作编排放在 sequence.py。
两层分开的好处是——现场掉电重启、急停、换货重扫这些异常，改顶层就够，
不会牵动已经调好的抓放序列。
"""

from __future__ import annotations

from enum import Enum


class State(str, Enum):
    BOOT = "BOOT"
    SELF_TEST = "SELF_TEST"
    STANDBY = "STANDBY"
    ARMED = "ARMED"
    RUNNING = "RUNNING"
    FINISH = "FINISH"
    FAULT = "FAULT"


class Event(str, Enum):
    SELF_TEST_OK = "SELF_TEST_OK"
    SELF_TEST_FAIL = "SELF_TEST_FAIL"
    ARM = "ARM"            # 操作员按下"开始"
    PLAN_READY = "PLAN_READY"   # 视觉给出本轮分拣计划
    PLAN_DONE = "PLAN_DONE"
    FAULT = "FAULT"
    RESET = "RESET"


# 状态转移表： (状态, 事件) -> 新状态
_TABLE: dict[tuple[State, Event], State] = {
    (State.BOOT, Event.SELF_TEST_OK): State.STANDBY,
    (State.BOOT, Event.SELF_TEST_FAIL): State.FAULT,

    (State.SELF_TEST, Event.SELF_TEST_OK): State.STANDBY,
    (State.SELF_TEST, Event.SELF_TEST_FAIL): State.FAULT,

    (State.STANDBY, Event.ARM): State.ARMED,
    (State.ARMED, Event.PLAN_READY): State.RUNNING,
    (State.RUNNING, Event.PLAN_DONE): State.FINISH,
    (State.FINISH, Event.RESET): State.STANDBY,

    (State.FAULT, Event.RESET): State.STANDBY,
}

# FAULT 可以从任何状态进入（急停、超行程、通信超时……）
_FAULT_FROM = frozenset(State) - {State.BOOT}


class IllegalTransition(RuntimeError):
    def __init__(self, state: State, event: Event) -> None:
        super().__init__(f"状态 {state.value} 不接受事件 {event.value}")
        self.state = state
        self.event = event


class Machine:
    def __init__(self, state: State = State.BOOT) -> None:
        self._state = state
        self._history: list[tuple[State, Event, State]] = []

    @property
    def state(self) -> State:
        return self._state

    @property
    def history(self) -> list[tuple[State, Event, State]]:
        return list(self._history)

    def can(self, event: Event) -> bool:
        if event is Event.FAULT:
            return self._state in _FAULT_FROM
        return (self._state, event) in _TABLE

    def fire(self, event: Event) -> State:
        if event is Event.FAULT:
            if self._state not in _FAULT_FROM:
                raise IllegalTransition(self._state, event)
            nxt = State.FAULT
        else:
            nxt = _TABLE.get((self._state, event))
            if nxt is None:
                raise IllegalTransition(self._state, event)
        self._history.append((self._state, event, nxt))
        self._state = nxt
        return nxt
