"""Synthetic DCU events plus retained sensor fixture; never access USB."""

import unittest

from swarm2.dcu_commands import (DcuCalibrationSession, DcuPhase, build_cancel_report,
                                build_commit_reports, build_start_report,
                                decode_completion_event)
from swarm2.protocol import ProtocolError


SENSOR = bytes.fromhex("10100000020200000004010700ff000001010f0000ff000101170000646401011f000000ff01013f00e320ca0185014b")
ACK = bytes.fromhex("1000f21900000000")
D1 = bytes.fromhex("1000d10000000000")  # Synthetic identity, not a native capture.


def profiles(lod=0x85):
    records = []
    for index in range(5):
        record = bytearray(SENSOR)
        record[3] = index
        record[45] = lod
        record[47] = -sum(record[2:47]) & 0xFF
        records.append(bytes(record))
    return records


class DcuCommandTests(unittest.TestCase):
    def test_exact_source_commands_and_zero_padding(self):
        reports = (build_start_report(), build_cancel_report(), *build_commit_reports())
        for report, prefix in zip(reports, ("10199000", "10199300", "10199300", "10199200")):
            self.assertEqual(report, bytes.fromhex(prefix) + bytes(60))

    def test_completion_identity_excludes_generic_ack_and_special_route(self):
        self.assertEqual(decode_completion_event(D1).raw, D1)
        for value in (b"", D1[:7], D1 + b"\0", ACK, b"\x00" + D1[1:],
                      bytes.fromhex("1033d10000000000"), "1000d100"):
            with self.subTest(value=value), self.assertRaises(ProtocolError):
                decode_completion_event(value)

    def test_unknown_event_tail_is_preserved_without_success_interpretation(self):
        event = bytes.fromhex("1001d17f010203ff")
        self.assertEqual(decode_completion_event(event).raw, event)

    def test_baseline_must_cover_uniform_known_preset_in_five_profiles(self):
        for values in ([], profiles()[:4], profiles()[::-1], profiles(1), profiles(0x92),
                       profiles()[:4] + profiles(0)[4:], [None] * 5):
            with self.subTest(values=values), self.assertRaises(ProtocolError):
                DcuCalibrationSession(values)
        DcuCalibrationSession(profiles(0))
        DcuCalibrationSession(profiles(0x85))

    def test_no_start_on_construction_and_no_start_replay(self):
        session = DcuCalibrationSession(profiles())
        self.assertEqual(session.phase, DcuPhase.READY)
        self.assertFalse(session.observe_completion(D1, now=1))
        self.assertEqual(session.start(now=1), build_start_report())
        with self.assertRaises(ProtocolError):
            session.start(now=2)

    def ready_session(self, *, early=False):
        session = DcuCalibrationSession(profiles())
        session.start(now=10)
        if early:
            self.assertTrue(session.observe_completion(D1, now=11))
            self.assertEqual(session.phase, DcuPhase.STARTING)
        session.acknowledge(ACK)
        if not early:
            self.assertEqual(session.phase, DcuPhase.RUNNING)
            self.assertTrue(session.observe_completion(D1, now=11))
        self.assertEqual(session.phase, DcuPhase.RESULT_READY)
        return session

    def test_start_ack_cannot_enable_commit_without_fresh_completion(self):
        session = DcuCalibrationSession(profiles())
        session.start(now=10)
        self.assertFalse(session.observe_completion(ACK, now=11))
        session.acknowledge(ACK)
        with self.assertRaises(ProtocolError):
            session.begin_commit(now=11)
        self.assertEqual(session.phase, DcuPhase.RUNNING)

    def test_completion_before_start_ack_is_retained(self):
        self.ready_session(early=True)

    def test_commit_requires_explicit_acceptance_ordered_acks_and_all_reads(self):
        session = self.ready_session()
        with self.assertRaises(ProtocolError):
            session.commit()
        self.assertEqual(session.begin_commit(now=12), build_cancel_report())
        with self.assertRaises(ProtocolError):
            session.commit()
        session.acknowledge(ACK)
        self.assertEqual(session.commit(), build_commit_reports()[1])
        with self.assertRaises(ProtocolError):
            session.verify(profiles(1))
        session.acknowledge(ACK)
        session.verify(profiles(1))
        self.assertEqual(session.phase, DcuPhase.COMMITTED)
        for operation in (session.commit, session.cancel, lambda: session.start(now=20)):
            with self.assertRaises(ProtocolError):
                operation()

    def test_busy_ack_does_not_advance_and_wrong_ack_is_rejected(self):
        session = DcuCalibrationSession(profiles())
        session.start(now=1)
        for status in (1, 2):
            raw = bytearray(ACK)
            raw[4] = status
            self.assertFalse(session.acknowledge(raw))
            self.assertEqual(session.phase, DcuPhase.STARTING)
        for raw in (D1, bytes.fromhex("1000f21400000000"), bytes.fromhex("0000f21900000000")):
            with self.assertRaises(ProtocolError):
                session.acknowledge(raw)
        self.assertEqual(session.phase, DcuPhase.STARTING)

    def test_cancel_verifies_baseline_and_rejects_late_completion(self):
        session = self.ready_session()
        self.assertEqual(session.cancel(), build_cancel_report())
        self.assertFalse(session.observe_completion(D1, now=12))
        session.acknowledge(ACK)
        session.verify(profiles())
        self.assertEqual(session.phase, DcuPhase.CANCELLED)

    def test_timeout_requires_cancellation_and_never_commit(self):
        session = DcuCalibrationSession(profiles(), timeout_seconds=5)
        session.start(now=10)
        session.acknowledge(ACK)
        self.assertFalse(session.observe_completion(D1, now=15))
        self.assertEqual(session.phase, DcuPhase.TIMED_OUT)
        with self.assertRaises(ProtocolError):
            session.begin_commit(now=15)
        session.cancel()
        session.acknowledge(ACK)
        session.verify(profiles())
        self.assertEqual(session.phase, DcuPhase.CANCELLED)

    def test_timeout_also_applies_to_unaccepted_result(self):
        session = self.ready_session()
        self.assertTrue(session.check_timeout(now=101))
        self.assertEqual(session.phase, DcuPhase.TIMED_OUT)

    def test_disconnect_cancel_failure_stays_uncertain_without_replay(self):
        session = self.ready_session()
        session.fail()
        self.assertEqual(session.cancel(), build_cancel_report())
        session.fail()
        self.assertEqual(session.phase, DcuPhase.UNCERTAIN)
        with self.assertRaises(ProtocolError):
            session.cancel()

    def test_rejected_command_is_uncertain(self):
        session = DcuCalibrationSession(profiles())
        session.start(now=1)
        with self.assertRaises(ProtocolError):
            session.acknowledge(bytes.fromhex("1000f219ff000000"))
        self.assertEqual(session.phase, DcuPhase.UNCERTAIN)

    def test_readback_disagreement_or_unrelated_sensor_edit_is_uncertain(self):
        for records in (profiles(), profiles(1)[:4], profiles(1)[::-1]):
            session = self.ready_session()
            session.begin_commit(now=12)
            session.acknowledge(ACK)
            session.commit()
            session.acknowledge(ACK)
            with self.assertRaises(ProtocolError):
                session.verify(records)
            self.assertEqual(session.phase, DcuPhase.UNCERTAIN)
        session = self.ready_session()
        session.cancel()
        session.acknowledge(ACK)
        changed = profiles()
        raw = bytearray(changed[3])
        raw[4] = 0  # Calibration must not silently alter polling rate.
        changed[3] = bytes(raw)
        with self.assertRaises(ProtocolError):
            session.verify(changed)
        self.assertEqual(session.phase, DcuPhase.UNCERTAIN)

    def test_time_bounds_are_host_validation(self):
        for timeout in (0, 4, 301, float("nan"), float("inf"), True, "90"):
            with self.assertRaises(ProtocolError):
                DcuCalibrationSession(profiles(), timeout_seconds=timeout)
        for now in (-1, float("nan"), True, "10"):
            with self.assertRaises(ProtocolError):
                DcuCalibrationSession(profiles()).start(now=now)


if __name__ == "__main__":
    unittest.main()
