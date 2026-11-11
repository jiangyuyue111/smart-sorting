import unittest

from sorting.core.machine import Event, IllegalTransition, Machine, State


class TestMachine(unittest.TestCase):
    def test_happy_path(self):
        m = Machine()
        for ev, want in [
            (Event.SELF_TEST_OK, State.STANDBY),
            (Event.ARM, State.ARMED),
            (Event.PLAN_READY, State.RUNNING),
            (Event.PLAN_DONE, State.FINISH),
            (Event.RESET, State.STANDBY),
        ]:
            self.assertEqual(m.fire(ev), want)
        self.assertEqual(len(m.history), 5)

    def test_self_test_fail_goes_fault(self):
        m = Machine()
        self.assertEqual(m.fire(Event.SELF_TEST_FAIL), State.FAULT)

    def test_fault_from_any_running_state(self):
        for start in (State.STANDBY, State.ARMED, State.RUNNING, State.FINISH, State.FAULT):
            m = Machine(start)
            self.assertEqual(m.fire(Event.FAULT), State.FAULT)

    def test_illegal_transitions(self):
        m = Machine(State.STANDBY)
        for ev in (Event.PLAN_READY, Event.PLAN_DONE, Event.SELF_TEST_OK):
            with self.assertRaises(IllegalTransition):
                m.fire(ev)
        self.assertEqual(m.state, State.STANDBY)

    def test_can(self):
        m = Machine(State.STANDBY)
        self.assertTrue(m.can(Event.ARM))
        self.assertFalse(m.can(Event.PLAN_DONE))
        self.assertTrue(m.can(Event.FAULT))


if __name__ == "__main__":
    unittest.main()
