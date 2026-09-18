"""DCU dialog controls and subprocess failure states, without USB."""

import json
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtCore import QObject, QProcess, Signal
    from PySide6.QtWidgets import QApplication
    from swarm2.gui.dcu import DcuCalibrationDialog
except ModuleNotFoundError as error:
    if error.name and error.name.startswith("PySide6"):
        DcuCalibrationDialog = None
    else:
        raise


if DcuCalibrationDialog is not None:
    class FakeProcess(QObject):
        started = Signal()
        finished = Signal(int, object)
        errorOccurred = Signal(object)
        readyReadStandardOutput = Signal()
        readyReadStandardError = Signal()

        def __init__(self, parent=None):
            super().__init__(parent)
            self._state = QProcess.ProcessState.NotRunning
            self.commands = []
            self.output = b""

        def state(self): return self._state
        def setProcessChannelMode(self, mode): pass
        def readAllStandardError(self): return b""

        def start(self, program, arguments):
            self.program, self.arguments = program, arguments
            self._state = QProcess.ProcessState.Running
            self.started.emit()

        def write(self, raw):
            self.commands.append(json.loads(raw))
            return len(raw)

        def readAllStandardOutput(self):
            output, self.output = self.output, b""
            return output

        def deliver(self, value):
            self.output += (json.dumps(value) + "\n").encode()
            self.readyReadStandardOutput.emit()

        def finish(self, result=None, *, code=0):
            if result:
                self.deliver(result)
            self._state = QProcess.ProcessState.NotRunning
            self.finished.emit(code, QProcess.ExitStatus.NormalExit)

        def kill(self):
            self._state = QProcess.ProcessState.NotRunning
            self.finished.emit(-9, QProcess.ExitStatus.CrashExit)


@unittest.skipIf(DcuCalibrationDialog is None, "Install the gui extra to exercise Qt")
class DcuDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dialog = DcuCalibrationDialog("fake-mouse", process_factory=FakeProcess)

    def tearDown(self):
        if self.dialog._active():
            self.dialog.process.kill()
        self.dialog.close()
        self.dialog.deleteLater()
        self.app.processEvents()

    def test_opening_never_starts_helper_or_enables_commit(self):
        self.assertIsNone(self.dialog.process)
        self.assertIsNone(self.dialog.outcome)
        self.assertFalse(self.dialog.commit_button.isEnabled())
        self.dialog.commit_calibration()
        self.assertIsNone(self.dialog.process)

    def test_explicit_start_sends_one_request_and_completion_enables_commit(self):
        self.dialog.start_calibration()
        self.dialog.start_calibration()
        process = self.dialog.process
        self.assertEqual(process.commands, [{"command": "start", "device_id": "fake-mouse", "timeout_seconds": 90}])
        self.dialog.commit_calibration()
        self.assertEqual(len(process.commands), 1)
        process.deliver({"type": "progress", "phase": "result_ready"})
        self.assertTrue(self.dialog.commit_button.isEnabled())
        self.dialog.commit_calibration()
        self.dialog.commit_calibration()
        self.assertEqual(process.commands[-1], {"command": "commit"})
        self.assertEqual(len(process.commands), 2)

    def test_close_requests_cancel_once_and_waits_for_helper(self):
        self.dialog.show()
        self.dialog.start_calibration()
        self.dialog.reject()
        self.dialog.reject()
        self.assertTrue(self.dialog.isVisible())
        self.assertEqual(self.dialog.process.commands[-1], {"command": "cancel"})
        self.assertEqual(len(self.dialog.process.commands), 2)
        self.dialog.process.finish({"type": "result", "outcome": "cancelled", "verified": True,
                                    "device_may_have_changed": True})
        self.assertFalse(self.dialog.isVisible())
        self.assertEqual(self.dialog.outcome["outcome"], "cancelled")

    def test_late_completion_does_not_enable_save_after_cancel(self):
        self.dialog.start_calibration()
        self.dialog.reject()
        self.dialog.process.deliver({"type": "progress", "phase": "result_ready"})
        self.assertFalse(self.dialog.commit_button.isEnabled())

    def test_only_verified_result_reports_success(self):
        self.dialog.start_calibration()
        self.dialog.process.finish({"type": "result", "outcome": "committed", "verified": True,
                                    "device_may_have_changed": True})
        self.assertIn("read back", self.dialog.status_label.text())
        self.assertTrue(self.dialog.device_may_have_changed)
        self.assertFalse(self.dialog.deadline_timer.isActive())

    def test_claimed_success_with_nonzero_exit_is_uncertain(self):
        self.dialog.start_calibration()
        self.dialog.process.finish({"type": "result", "outcome": "committed", "verified": True}, code=2)
        self.assertEqual(self.dialog.outcome["outcome"], "uncertain")

    def test_missing_or_malformed_reply_never_claims_success(self):
        self.dialog.start_calibration()
        self.dialog.process.output = b"bad-json\n"
        self.dialog.process.finish()
        self.assertEqual(self.dialog.outcome["outcome"], "uncertain")
        self.assertIn("invalid response", self.dialog.detail_label.text())

    def test_cancel_deadline_kills_child_and_marks_uncertainty(self):
        self.dialog.start_calibration()
        self.dialog.reject()
        self.dialog.cancel_timer.timeout.emit()
        self.assertFalse(self.dialog._active())
        self.assertEqual(self.dialog.outcome["outcome"], "uncertain")
        self.assertIn("Cancellation did not finish", self.dialog.outcome["error"])

    def test_partial_trailing_output_invalidates_prior_success(self):
        self.dialog.start_calibration()
        self.dialog.process.deliver({"type": "result", "outcome": "committed", "verified": True})
        self.dialog.process.output = b'{"unfinished":'
        self.dialog.process.finish()
        self.assertEqual(self.dialog.outcome["outcome"], "uncertain")
        self.assertIn("incomplete response", self.dialog.detail_label.text())

    def test_messages_after_terminal_result_are_rejected(self):
        self.dialog.start_calibration()
        self.dialog.process.deliver({"type": "result", "outcome": "committed", "verified": True})
        self.dialog.process.deliver({"type": "progress", "phase": "running"})
        self.assertEqual(self.dialog.outcome["outcome"], "uncertain")

    def test_close_after_accepting_commit_waits_without_claiming_cancellation(self):
        self.dialog.start_calibration()
        self.dialog.process.deliver({"type": "progress", "phase": "result_ready"})
        self.dialog.commit_calibration()
        self.dialog.reject()
        self.assertEqual([message["command"] for message in self.dialog.process.commands], ["start", "commit"])
        self.assertIn("accepted save", self.dialog.status_label.text())


if __name__ == "__main__":
    unittest.main()
