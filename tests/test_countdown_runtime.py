"""Deterministic host countdown state without USB access."""

import unittest

from swarm2.countdown_runtime import CountdownBinding, CountdownRuntime
from swarm2.transport import DeviceError


PRESS_PAGE_1_SLOT_2 = bytes.fromhex("1033114600000100")
PRESS_PAGE_2_SLOT_1 = bytes.fromhex("1033224600000100")


class Transport:
    def __init__(self):
        self.reports = []

    def send(self, report):
        self.reports.append(report)


def records(reports):
    return [report[4 + offset:13 + offset]
            for report in reports
            for offset in range(0, report[3] * 9, 9)]


class CountdownRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.transport = Transport()
        self.messages = []
        self.guards = 0

        def guard():
            self.guards += 1

        self.runtime = CountdownRuntime([
            CountdownBinding("shared", 0, 2, 4),
            CountdownBinding("shared", 1, 1, 4),
        ], self.transport, self.messages.append, guard=guard, clock=lambda: 10.0)

    def test_start_synchronizes_every_position_and_is_not_reentrant(self):
        self.runtime.start()
        self.assertEqual(records(self.transport.reports), [
            bytes.fromhex("01010b000004000000"),
            bytes.fromhex("02020b000004000000"),
        ])
        self.assertEqual(self.guards, 2)
        self.assertEqual(self.messages[-1], {"type": "ready", "timers": 1, "positions": 2})
        with self.assertRaises(DeviceError):
            self.runtime.start()

    def test_press_starts_shared_positions_ticks_without_drift_and_press_stops(self):
        self.runtime.start()
        self.transport.reports.clear()
        self.assertTrue(self.runtime.observe(PRESS_PAGE_1_SLOT_2, now=20.0))
        self.assertEqual(self.runtime.running_timer_ids, ("shared",))
        self.assertEqual([r[3:7] for r in records(self.transport.reports)],
                         [bytes.fromhex("00000400")] * 2)
        self.transport.reports.clear()
        self.assertEqual(self.runtime.advance(now=20.1), 0)
        self.assertEqual(self.runtime.advance(now=21.0), 1)
        self.assertEqual([r[3:7] for r in records(self.transport.reports)],
                         [bytes.fromhex("01000300")] * 2)
        self.transport.reports.clear()
        self.assertTrue(self.runtime.observe(PRESS_PAGE_2_SLOT_1, now=21.1))
        self.assertEqual(self.runtime.running_timer_ids, ())
        self.assertEqual([r[3:7] for r in records(self.transport.reports)],
                         [bytes.fromhex("00000400")] * 2)
        self.assertEqual(self.messages[-1]["event"], "stopped")

    def test_completion_publishes_zero_then_native_reset(self):
        self.runtime.start()
        self.runtime.observe(PRESS_PAGE_1_SLOT_2, now=20.0)
        self.transport.reports.clear()
        self.assertEqual(self.runtime.advance(now=24.0), 1)
        payloads = [r[3:7] for r in records(self.transport.reports)]
        self.assertEqual(payloads[:2], [bytes.fromhex("04000000")] * 2)
        self.assertEqual(payloads[2:], [bytes.fromhex("00000400")] * 2)
        self.assertEqual(self.runtime.running_timer_ids, ())
        self.assertEqual(self.messages[-1]["event"], "completed")

    def test_release_other_widget_and_unbound_countdown_are_ignored(self):
        self.runtime.start()
        before = len(self.transport.reports)
        for event in (bytes.fromhex("1033114600000000"),
                      bytes.fromhex("1033114500000100"),
                      bytes.fromhex("1033134600000100")):
            self.assertFalse(self.runtime.observe(event))
        self.assertEqual(len(self.transport.reports), before)

    def test_explicit_stop_resets_running_timers_once(self):
        self.runtime.start()
        self.runtime.observe(PRESS_PAGE_1_SLOT_2, now=20.0)
        self.transport.reports.clear()
        self.runtime.stop()
        self.assertEqual(len(records(self.transport.reports)), 2)
        self.assertEqual(self.runtime.running_timer_ids, ())
        self.assertEqual(self.messages[-1], {"type": "stopped"})
        self.runtime.stop()
        self.assertEqual(len(records(self.transport.reports)), 2)

    def test_invalid_bindings_and_prestart_calls(self):
        for bindings in ([], [object()],
                         [CountdownBinding("a", 0, 0, 1), CountdownBinding("b", 0, 0, 1)],
                         [CountdownBinding("a", 0, 0, 1), CountdownBinding("a", 0, 1, 2)]):
            with self.subTest(bindings=bindings), self.assertRaises(ValueError):
                CountdownRuntime(bindings, self.transport)
        runtime = CountdownRuntime([CountdownBinding("a", 0, 0, 1)], self.transport)
        with self.assertRaises(DeviceError):
            runtime.observe(PRESS_PAGE_1_SLOT_2)
        with self.assertRaises(DeviceError):
            runtime.advance()


if __name__ == "__main__":
    unittest.main()
