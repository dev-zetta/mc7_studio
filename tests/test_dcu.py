"""Test the isolated DCU workflow with a bounded synthetic event pump."""

import os
import json
import subprocess
import sys
import unittest

from swarm2.dcu import DcuRuntime, JsonCommandReader
from swarm2.transport import DeviceError
from tests.test_dcu_commands import ACK, D1, profiles


class FakeCalibrationTransport:
    def __init__(self):
        self.now = 10.0
        self.records = profiles()
        self.commands = []
        self.events = []
        self.early_completion = False
        self.drop_ack = set()
        self.disconnect = False
        self.read_count = 0

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


if __name__ == "__main__":
    unittest.main()
