"""Explicit custom lift-off workflow backed by an isolated bounded HID helper."""

import json

from PySide6.QtCore import QProcess, QTimer, Signal
from PySide6.QtWidgets import QDialog, QHBoxLayout, QProgressBar, QPushButton, QVBoxLayout

from ..dcu import HARD_TIMEOUT_SECONDS
from ..runtime import helper_command
from .widgets import STYLE, label


class DcuCalibrationDialog(QDialog):
    """Opening is inert. Only Start can open USB handles and begin calibration.

    Inspect outcome and device_may_have_changed after exec(), then reread the
    main-window snapshot before applying anything else. The parent must block
    other device jobs for the lifetime of this modal dialog.
    """

    operation_active = Signal(bool)

    def __init__(self, device_id, parent=None, *, process_factory=QProcess, timeout_seconds=90):
        super().__init__(parent)
        self.device_id = device_id
        self.timeout_seconds = timeout_seconds
        self.outcome = None
        self.device_may_have_changed = False
        self.process = None
        self._process_factory = process_factory
        self._buffer = bytearray()
        self._phase = "ready"
        self._cancel_requested = False
        self._close_requested = False
        self._fatal_message = None
        self._result_received = False
        self._finishing = False
        self._finished_once = False
        self.setWindowTitle("Custom lift-off calibration")
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(600)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 26, 28, 24)
        layout.setSpacing(16)
        layout.addWidget(label("Calibrate for your surface", "pageTitle", True))
        layout.addWidget(label("Place the mouse on the surface you use. After Start, keep it on the surface and move it in circles until the mouse reports completion.", "muted", True))
        layout.addWidget(label("This changes a global setting shared by all five mouse profiles. Start is available only when every profile uses the same Very Low or Low preset. Existing custom surface data cannot be backed up.", "notice", True))
        self.status_label = label("Ready. No calibration command has been sent.", "sectionTitle", True)
        layout.addWidget(self.status_label)
        self.detail_label = label("Start checks the current settings. When the mouse finishes, choose Save to apply its result.", "muted", True)
        layout.addWidget(self.detail_label)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)
        row = QHBoxLayout()
        self.start_button = QPushButton("Start calibration")
        self.start_button.clicked.connect(self.start_calibration)
        self.commit_button = QPushButton("Save calibration to mouse")
        self.commit_button.setEnabled(False)
        self.commit_button.clicked.connect(self.commit_calibration)
        self.cancel_button = QPushButton("Close")
        self.cancel_button.clicked.connect(self.reject)
        row.addWidget(self.start_button)
        row.addWidget(self.commit_button)
        row.addStretch()
        row.addWidget(self.cancel_button)
        layout.addLayout(row)
        self.deadline_timer = QTimer(self)
        self.deadline_timer.setSingleShot(True)
        self.deadline_timer.timeout.connect(lambda: self._abort("The calibration helper exceeded its deadline. Reconnect the mouse and read every profile before continuing."))
        self.cancel_timer = QTimer(self)
        self.cancel_timer.setSingleShot(True)
        self.cancel_timer.timeout.connect(lambda: self._abort("Cancellation did not finish in time. Reconnect the mouse and read every profile before continuing."))

    def _active(self):
        return self.process is not None and self.process.state() != QProcess.ProcessState.NotRunning

    def start_calibration(self):
        if self.process is not None:
            return
        if not isinstance(self.device_id, str) or not self.device_id:
            self.status_label.setText("Select a USB mouse before calibrating.")
            return
        self.process = self._process_factory(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self.process.started.connect(self._started)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.readyReadStandardError.connect(self._discard_stderr)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._process_error)
        self.start_button.setEnabled(False)
        self.cancel_button.setText("Cancel calibration")
        self.status_label.setText("Reading the mouse before calibration…")
        self.progress.setRange(0, 0)
        self.operation_active.emit(True)
        self.deadline_timer.start((HARD_TIMEOUT_SECONDS + 5) * 1000)
        command = helper_command("swarm2.dcu")
        self.process.start(command[0], list(command[1:]))

    def _started(self):
        self.device_may_have_changed = True
        self._send({"command": "start", "device_id": self.device_id,
                    "timeout_seconds": self.timeout_seconds})
        if self._cancel_requested:
            self._send({"command": "cancel"})

    def _send(self, command):
        if self.process is None or self.process.state() == QProcess.ProcessState.NotRunning:
            return
        raw = (json.dumps(command) + "\n").encode()
        if self.process.write(raw) != len(raw):
            self._abort("The calibration command could not reach its helper. The mouse state is uncertain; reconnect and read it again.")

    def commit_calibration(self):
        if self._phase != "result_ready" or not self._active() or self._cancel_requested:
            return
        self.commit_button.setEnabled(False)
        self._phase = "committing"
        self.status_label.setText("Saving and checking all five profiles…")
        self._send({"command": "commit"})

    def _read_output(self):
        self._buffer.extend(bytes(self.process.readAllStandardOutput()))
        if len(self._buffer) > 65536:
            self._abort("The calibration helper returned too much data. Reconnect and read the mouse again.")
            return
        while b"\n" in self._buffer:
            line, _, rest = self._buffer.partition(b"\n")
            self._buffer = bytearray(rest)
            try:
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("Invalid helper message")
                self._message(message)
            except (ValueError, TypeError):
                self._abort("The calibration helper returned an invalid response. Reconnect and read the mouse again.")
                return

    def _discard_stderr(self):
        # Runtime failures are structured on stdout; never grow an unbounded log.
        self.process.readAllStandardError()

    def _message(self, message):
        if self._result_received:
            raise ValueError("Calibration helper sent data after its terminal result")
        if message.get("type") == "progress":
            phase = message.get("phase")
            captions = {
                "reading_baseline": "Checking all five mouse profiles…",
                "starting": "Starting calibration…",
                "running": "Move the mouse in circles on your surface.",
                "result_ready": "The mouse reported calibration completion.",
                "commit_resetting": "Preparing to save calibration…",
                "commit_ready": "Preparing to save calibration…",
                "committing": "Saving calibration…",
                "verifying_commit": "Checking the saved setting in all five profiles…",
                "cancelling": "Cancelling calibration…",
                "verifying_cancel": "Checking cancellation in all five profiles…",
            }
            if phase not in captions:
                raise ValueError("Unknown calibration progress")
            self._phase = phase
            if phase != "reading_baseline":
                self.device_may_have_changed = True
            self.status_label.setText(captions[phase])
            self.commit_button.setEnabled(phase == "result_ready" and not self._cancel_requested)
            if phase == "result_ready":
                self.detail_label.setText("Choose Save calibration to mouse to accept it, or Cancel. The app checks the stored setting; surface tracking quality needs your physical check.")
        elif message.get("type") == "result":
            if message.get("outcome") not in ("committed", "cancelled", "uncertain", "error"):
                raise ValueError("Unknown calibration result")
            if message.get("outcome") in ("committed", "cancelled") and message.get("verified") is not True:
                raise ValueError("Calibration result lacks verification")
            self.outcome = message
            self._result_received = True
            self.device_may_have_changed |= message.get("device_may_have_changed") is True
            self.commit_button.setEnabled(False)
        else:
            raise ValueError("Unknown calibration message")

    def _abort(self, message):
        if self._fatal_message is None:
            self._fatal_message = message
        self.commit_button.setEnabled(False)
        self.device_may_have_changed = True
        if self._active():
            self.process.kill()
        elif not self._finishing:
            self._finished(-1, QProcess.ExitStatus.CrashExit)

    def _process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self._fatal_message = "The calibration helper could not start. No device command was sent."
            self._finished(-1, QProcess.ExitStatus.CrashExit)

    def _finished(self, exit_code, exit_status):
        if self._finished_once or self._finishing:
            return
        self._finishing = True
        self.deadline_timer.stop()
        self.cancel_timer.stop()
        if self.process is not None:
            self._read_output()
        if self._buffer:
            self._fatal_message = "The calibration helper returned an incomplete response. Reconnect and read the mouse again."
        if (self._fatal_message or not self._result_received or exit_status != QProcess.ExitStatus.NormalExit
                or (exit_code != 0 and self.outcome and self.outcome["outcome"] in ("committed", "cancelled"))):
            self.outcome = {"type": "result", "outcome": "uncertain" if self.device_may_have_changed or not self._fatal_message else "error",
                            "verified": False, "device_may_have_changed": self.device_may_have_changed,
                            "error": self._fatal_message or "The calibration helper stopped unexpectedly. Reconnect the mouse and read every profile before continuing."}
        self._finished_once = True
        self._finishing = False
        self.operation_active.emit(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.cancel_button.setEnabled(True)
        self.cancel_button.setText("Close")
        if self.outcome["outcome"] == "committed":
            self.status_label.setText("Custom lift-off setting saved and read back in all five profiles.")
            self.detail_label.setText("Check tracking on your surface. Read the mouse again in the main window before making further changes.")
        elif self.outcome["outcome"] == "cancelled":
            self.status_label.setText("Calibration cancelled; the previous preset was read back in every profile.")
            reason = self.outcome.get("reason")
            self.detail_label.setText("The calibration timed out and was cancelled." if reason == "timeout" else str(reason or "No custom result was accepted."))
        else:
            self.status_label.setText("Calibration could not be verified." if self.outcome["outcome"] == "uncertain" else "Calibration did not start.")
            self.detail_label.setText(self.outcome.get("error", "Read the mouse before continuing."))
        if self._close_requested:
            super().reject()

    def reject(self):
        if self._active():
            self._close_requested = True
            if self._phase in ("committing", "commit_resetting", "commit_ready", "verifying_commit"):
                self.cancel_button.setEnabled(False)
                self.status_label.setText("Finishing the accepted save before closing…")
                return
            if not self._cancel_requested:
                self._cancel_requested = True
                self.commit_button.setEnabled(False)
                self.cancel_button.setEnabled(False)
                self.status_label.setText("Cancelling and checking the mouse before closing…")
                if self.process.state() == QProcess.ProcessState.Running:
                    self._send({"command": "cancel"})
                self.cancel_timer.start(15_000)
            return
        super().reject()

    def closeEvent(self, event):
        if self._active():
            self.reject()
            event.ignore()
        else:
            event.accept()
