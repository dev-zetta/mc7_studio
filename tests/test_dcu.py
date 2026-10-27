"""Test the isolated DCU workflow with a bounded synthetic event pump."""

from contextlib import contextmanager, nullcontext
import io
import os
import json
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from swarm2 import dcu
from swarm2.dcu import CalibrationTransport, DcuRuntime, JsonCommandReader
from swarm2.transport import DeviceError
from tests.test_dcu_commands import ACK, D1, profiles


class FakeCalibrationTransport:
    def command_session(self):
        return nullcontext()

    def __init__(self):
        self.now = 10.0
        self.records = profiles()
        self.commands = []
        self.events = []
        self.early_completion = False
        self.drop_ack = set()
        self.disconnect = False
        self.read_count = 0
        self.prepared = False

    def prepare_calibration(self):
        self.prepared = True

    def read_profiles(self):
        self.read_count += 1
        if self.disconnect:
            raise DeviceError("Disconnected")
        return self.records

    def drain(self):
        self.events.clear()

    def write(self, report):
        self.commands.append(report[2])
        if self.disconnect:
            raise DeviceError("Disconnected")
        if report[2] == 0x90 and self.early_completion:
            self.events.append(D1)
        if report[2] not in self.drop_ack:
            self.events.append(ACK)
        if report[2] == 0x92:
            self.records = profiles(1)

    def read_event(self, timeout):
        self.now += max(timeout, .001)
        if self.disconnect:
            raise DeviceError("Disconnected")
        return self.events.pop(0) if self.events else b""


class DcuRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.transport = FakeCalibrationTransport()
        self.messages = []
        self.runtime = DcuRuntime(self.transport, self.messages.append, timeout_seconds=5,
                                  clock=lambda: self.transport.now)

    def test_start_checks_all_profiles_and_generic_ack_never_finishes_calibration(self):
        self.assertEqual(self.transport.commands, [])
        self.runtime.start()
        self.assertEqual(self.transport.read_count, 1)
        self.assertTrue(self.transport.prepared)
        self.assertEqual(self.transport.commands, [0x90])
        self.transport.events.append(ACK)
        self.runtime.tick()
        self.assertEqual(self.runtime.session.phase, "running")
        self.assertIsNone(self.runtime.outcome)

    def test_early_completion_waits_for_ack_then_explicit_commit(self):
        self.transport.early_completion = True
        self.runtime.start()
        self.assertEqual(self.runtime.session.phase, "result_ready")
        self.assertEqual(self.transport.commands, [0x90])
        self.runtime.commit()
        self.assertEqual(self.transport.commands, [0x90, 0x93, 0x92])
        self.assertEqual(self.runtime.outcome["outcome"], "committed")
        self.assertTrue(self.runtime.outcome["verified"])
        self.assertEqual(self.transport.read_count, 2)

    def test_stale_completion_before_start_is_drained(self):
        self.transport.events.append(D1)
        self.runtime.start()
        self.assertEqual(self.runtime.session.phase, "running")

    def test_timeout_sends_one_cancel_and_verifies_every_profile(self):
        self.runtime.start()
        self.transport.now += 6
        self.runtime.tick()
        self.assertEqual(self.transport.commands, [0x90, 0x93])
        self.assertEqual(self.runtime.outcome["outcome"], "cancelled")
        self.assertEqual(self.runtime.outcome["reason"], "timeout")
        self.runtime.cancel()
        self.assertEqual(self.transport.commands, [0x90, 0x93])

    def test_missing_start_ack_is_cancelled_without_start_replay(self):
        self.transport.drop_ack.add(0x90)
        with self.assertRaises(DeviceError) as error:
            self.runtime.start()
        self.runtime.recover(error.exception)
        self.assertEqual(self.transport.commands, [0x90, 0x93])
        self.assertEqual(self.runtime.outcome["outcome"], "cancelled")

    def test_missing_cancel_ack_is_uncertain_without_cancel_replay(self):
        self.runtime.start()
        self.transport.drop_ack.add(0x93)
        with self.assertRaises(DeviceError) as error:
            self.runtime.cancel()
        self.runtime.recover(error.exception)
        self.assertEqual(self.transport.commands, [0x90, 0x93])
        self.assertEqual(self.runtime.outcome["outcome"], "uncertain")

    def test_disconnect_attempts_cancel_once_without_claiming_recovery(self):
        self.runtime.start()
        self.transport.disconnect = True
        with self.assertRaises(DeviceError) as error:
            self.runtime.tick()
        self.runtime.recover(error.exception)
        self.assertEqual(self.transport.commands, [0x90, 0x93])
        self.assertEqual(self.runtime.outcome["outcome"], "uncertain")

    def test_existing_custom_baseline_never_sends_start(self):
        self.transport.records = profiles(1)
        with self.assertRaises(ValueError) as error:
            self.runtime.start()
        self.runtime.recover(error.exception)
        self.assertEqual(self.transport.commands, [])
        self.assertFalse(self.transport.prepared)
        self.assertFalse(self.runtime.outcome["device_may_have_changed"])
        self.assertEqual(self.runtime.outcome["outcome"], "error")

    def test_wrong_commit_readback_does_not_claim_success(self):
        self.transport.early_completion = True
        self.runtime.start()
        original = self.transport.read_profiles
        self.transport.read_profiles = lambda: profiles(0)
        with self.assertRaises(ValueError) as error:
            self.runtime.commit()
        self.runtime.recover(error.exception)
        self.assertEqual(self.runtime.outcome["outcome"], "uncertain")
        self.assertEqual(self.transport.commands, [0x90, 0x93, 0x92, 0x93])


class CalibrationReceiverTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.commands = []
        self.sessions = []
        self.actions = []
        self.resume_event = None

        @contextmanager
        def configuration_session(*, on_event):
            self.sessions.append("pause")
            self.actions.append("pause")
            # A notification before Start must not count as its completion.
            on_event(D1)
            try:
                yield
            finally:
                self.sessions.append("resume")
                self.actions.append("resume")
                if self.resume_event is not None:
                    on_event(self.resume_event)

        def write(report):
            self.commands.append(report[2])
            self.actions.append(f"write:{report[2]:02x}")
            self.events.append(ACK)
            return 64

        raw = SimpleNamespace(
            configuration_session=configuration_session,
            control=SimpleNamespace(send_feature_report=write),
            events=SimpleNamespace(read=lambda *_: self.events.pop(0) if self.events else b""),
            send=Mock(),
        )
        self.adapter = CalibrationTransport(raw)
        self.runtime = DcuRuntime(self.adapter, lambda _: None)

    def start(self):
        with patch("swarm2.dcu.read_sensor", side_effect=[SimpleNamespace(raw=r) for r in profiles()]), \
             patch("swarm2.dcu.read_raw", return_value=bytes.fromhex("1012000005fb")):
            self.runtime.start()

    def test_profile_reads_and_start_each_pair_reporting_and_discard_stale_completion(self):
        self.start()
        self.assertEqual(self.sessions, ["pause", "resume"] * 3)
        self.assertEqual(self.commands, [0x90])
        self.assertEqual(self.runtime.session.phase, "running")
        self.adapter.transport.send.assert_called_once_with(bytes.fromhex("1012800569") + bytes(59))

    def test_activation_preserves_current_profile_count_and_energy_saving(self):
        # Active slot 3, four enabled slots, energy saving enabled.
        before = bytes.fromhex("1012000214ea")
        with patch("swarm2.dcu.read_raw", return_value=before):
            self.adapter.prepare_calibration()
        self.adapter.transport.send.assert_called_once_with(bytes.fromhex("1012821458") + bytes(59))

    def test_changed_profile_after_activation_prevents_calibration_start(self):
        with patch("swarm2.dcu.read_sensor", side_effect=[SimpleNamespace(raw=r) for r in profiles()]), \
             patch("swarm2.dcu.read_raw", side_effect=[bytes.fromhex("1012000005fb"), bytes.fromhex("1012000105fa")]):
            with self.assertRaisesRegex(DeviceError, "profile changed"):
                self.runtime.start()
        self.assertEqual(self.commands, [])

    def test_completion_during_resume_reaches_running_session(self):
        self.resume_event = D1
        self.start()
        self.assertEqual(self.runtime.session.phase, "running")
        self.runtime.tick()
        self.assertEqual(self.runtime.session.phase, "result_ready")
        self.assertEqual(self.runtime.session.completion.raw, D1)

    def test_resume_ack_and_touch_namespace_never_count_as_completion(self):
        for event in (ACK, D1[:1] + b"\x33" + D1[2:]):
            with self.subTest(event=event):
                self.resume_event = event
                self.adapter._pending_completion = None
                self.adapter._preserve_completion(event)
                self.assertEqual(self.adapter.read_event(0), b"")

    def test_save_keeps_reporting_paused_across_reset_and_commit(self):
        self.resume_event = D1
        self.start()
        self.runtime.tick()
        self.actions.clear()
        with patch("swarm2.dcu.read_sensor", side_effect=[SimpleNamespace(raw=r) for r in profiles(1)]):
            self.runtime.commit()
        self.assertEqual(self.actions[:4], ["pause", "write:93", "write:92", "resume"])
        self.assertEqual(self.commands, [0x90, 0x93, 0x92])
        self.assertEqual(self.runtime.outcome["outcome"], "committed")

    def test_failed_commit_still_resumes_without_replaying_either_save_packet(self):
        self.resume_event = D1
        self.start()
        self.runtime.tick()
        self.actions.clear()
        original = self.adapter.write

        def failing_write(report):
            if report[2] == 0x92:
                self.actions.append("failed:92")
                raise DeviceError("Incomplete commit transfer")
            return original(report)

        with patch.object(self.adapter, "write", side_effect=failing_write):
            with self.assertRaisesRegex(DeviceError, "Incomplete commit"):
                self.runtime.commit()
        self.assertEqual(self.actions, ["pause", "write:93", "failed:92", "resume"])

    def test_write_failure_still_resumes_reporting_without_replaying_start(self):
        with patch.object(self.adapter, "write", side_effect=DeviceError("Disconnected")) as write:
            with self.assertRaisesRegex(DeviceError, "Disconnected"):
                self.start()
        write.assert_called_once()
        self.assertEqual(self.sessions, ["pause", "resume"] * 3)


class CommandPipeTests(unittest.TestCase):
    def setUp(self):
        self.read_fd, self.write_fd = os.pipe()
        self.reader = JsonCommandReader(self.read_fd)

    def tearDown(self):
        os.close(self.read_fd)
        if self.write_fd is not None:
            os.close(self.write_fd)

    def test_buffered_commands_and_eof(self):
        os.write(self.write_fd, b'{"command":"commit"}\n{"command":"cancel"}\n')
        self.assertEqual(self.reader.read(), {"command": "commit"})
        self.assertEqual(self.reader.read(), {"command": "cancel"})
        os.close(self.write_fd)
        self.write_fd = None
        self.assertIsNone(self.reader.read())
        self.assertTrue(self.reader.eof)

    def test_partial_line_never_blocks_and_oversized_input_is_rejected(self):
        os.write(self.write_fd, b'{"command":')
        self.assertIsNone(self.reader.read())
        os.write(self.write_fd, b'"cancel"}\n')
        self.assertEqual(self.reader.read(), {"command": "cancel"})
        # Exercise overflow incrementally; Windows pipes hold only 4 KiB.
        os.write(self.write_fd, b"x" * 2048)
        self.assertIsNone(self.reader.read())
        os.write(self.write_fd, b"x" * 2049)
        with self.assertRaises(DeviceError):
            self.reader.read()

    def test_real_helper_rejects_non_start_before_opening_any_transport(self):
        completed = subprocess.run([sys.executable, "-m", "swarm2.dcu"],
                                   input='{"command":"cancel"}\n', text=True,
                                   capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 2)
        result = json.loads(completed.stdout)
        self.assertEqual(result["outcome"], "error")
        self.assertFalse(result["device_may_have_changed"])
        self.assertIn("Explicit Start", result["error"])


class HelperTimeoutTests(unittest.TestCase):
    def test_extended_window_reaches_runtime_and_explicit_cancel_still_verifies(self):
        for timeout in (180, 300, None):
            command = {"command": "start", "device_id": "fixture"}
            if timeout is not None:
                command["timeout_seconds"] = timeout
            reader = Mock(eof=False)
            reader.read.side_effect = [command, {"command": "cancel"}]
            output = io.StringIO()
            with self.subTest(timeout=timeout), \
                 patch.object(dcu, "JsonCommandReader", return_value=reader), \
                 patch.object(dcu, "_transport_factory", return_value=nullcontext(object())), \
                 patch.object(dcu, "CalibrationTransport", return_value=FakeCalibrationTransport()), \
                 patch.object(dcu, "DcuRuntime", wraps=DcuRuntime) as runtime, \
                 patch.object(dcu.threading, "Timer"), \
                 patch.object(dcu.sys, "stdin", Mock()), patch.object(dcu.sys, "stdout", output):
                self.assertEqual(dcu.main(), 0)
            self.assertEqual(runtime.call_args.kwargs["timeout_seconds"], timeout or 300)
            result = json.loads(output.getvalue().splitlines()[-1])
            self.assertEqual(result["outcome"], "cancelled")
            self.assertTrue(result["verified"])

    def test_timeout_above_bound_is_rejected_before_opening_device(self):
        reader = Mock()
        reader.read.return_value = {"command": "start", "device_id": "fixture", "timeout_seconds": 301}
        with patch.object(dcu, "JsonCommandReader", return_value=reader), \
             patch.object(dcu, "_transport_factory") as factory, \
             patch.object(dcu.threading, "Timer"), \
             patch.object(dcu.sys, "stdin", Mock()), patch.object(dcu.sys, "stdout", io.StringIO()):
            self.assertEqual(dcu.main(), 2)
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
