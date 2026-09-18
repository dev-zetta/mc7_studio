"""Isolated, bounded DCU calibration helper with explicit commit/cancel input.

First JSON line: {"command":"start", "device_id":..., "timeout_seconds":90}.
Subsequent lines: {"command":"commit"} or {"command":"cancel"}.
Output lines describe progress and one terminal result. No helper is started
by opening the GUI dialog. All HID handles live only in this process.
"""

import json
import os
import platform
import select
from .pipe_io import pipe_readable
import sys
import threading
import time

from .dcu_commands import DcuCalibrationSession, DcuPhase
from .hardware import read_sensor
from .transport import DeviceError, HidrawTransport

HARD_TIMEOUT_SECONDS = 120
TERMINAL_PHASES = {DcuPhase.COMMITTED, DcuPhase.CANCELLED, DcuPhase.UNCERTAIN}


class CalibrationTransport:
    """Specialized event pump; leaves the ordinary transport.send unchanged."""

    def __init__(self, transport):
        self.transport = transport

    def read_profiles(self):
        return [read_sensor(self.transport, index).raw for index in range(5)]

    def read_event(self, timeout):
        if isinstance(self.transport.events, int):
            if not select.select([self.transport.events], [], [], timeout)[0]:
                return b""
            event = os.read(self.transport.events, 64)
            if not event:
                raise DeviceError("Mouse disconnected during DCU calibration")
            return event
        return bytes(self.transport.events.read(64, max(1, int(timeout * 1000))))

    def drain(self):
        for _ in range(128):
            if not self.read_event(0):
                return
        raise DeviceError("Mouse event queue did not settle before calibration")

    def write(self, report):
        if (not isinstance(report, bytes) or len(report) != 64 or report[:2] != b"\x10\x19"
                or report[2] not in (0x90, 0x92, 0x93) or any(report[3:])):
            raise DeviceError("Unsupported DCU command")
        if isinstance(self.transport.control, int):
            import fcntl
            count = fcntl.ioctl(self.transport.control, self.transport._request(6, 64), bytearray(report), True)
        else:
            count = self.transport.control.send_feature_report(report)
        if count != 64:
            raise DeviceError("DCU feature transfer was incomplete")


class DcuRuntime:
    """One lock-owning helper session; injectable transport/clock for tests."""

    def __init__(self, transport, emit, *, timeout_seconds=90, clock=time.monotonic):
        self.transport, self.emit, self.clock = transport, emit, clock
        self.timeout_seconds = timeout_seconds
        self.session = None
        self.outcome = None
        self.device_may_have_changed = False
        self._last_phase = None

    def _progress(self):
        phase = self.session.phase
        if phase != self._last_phase:
            payload = {"type": "progress", "phase": phase.value}
            if phase == DcuPhase.RESULT_READY:
                payload["completion_event"] = self.session.completion.raw.hex()
            self.emit(payload)
            self._last_phase = phase

    def _send(self, report):
        # Drain before each distinct command, never after writing Start: D1
        # may arrive before the start ACK and must be retained in that case.
        self.transport.drain()
        self.device_may_have_changed = True
        self.transport.write(report)
        deadline = self.clock() + 2
        while self.clock() < deadline:
            event = self.transport.read_event(min(0.05, max(0, deadline - self.clock())))
            if not event:
                continue
            self.session.observe_completion(event, now=self.clock())
            if len(event) != 8 or event[0] != 0x10 or event[2:4] != b"\xf2\x19":
                continue
            if self.session.acknowledge(event):
                self._progress()
                return
        raise DeviceError("DCU command acknowledgement timed out")

    def start(self):
        if self.session is not None or self.outcome is not None:
            raise DeviceError("DCU calibration cannot be restarted in this helper")
        self.emit({"type": "progress", "phase": "reading_baseline"})
        self.session = DcuCalibrationSession(self.transport.read_profiles(), timeout_seconds=self.timeout_seconds)
        self._send(self.session.start(now=self.clock()))

    def tick(self):
        if self.outcome is not None:
            return
        if self.session.check_timeout(now=self.clock()):
            self.cancel(reason="timeout")
            return
        event = self.transport.read_event(0.05)
        if event:
            self.session.observe_completion(event, now=self.clock())
            self._progress()

    def commit(self):
        if self.outcome is not None:
            raise DeviceError("Calibration has already finished")
        self._send(self.session.begin_commit(now=self.clock()))
        self._send(self.session.commit())
        self.session.verify(self.transport.read_profiles())
        self._finish("committed", verified=True)

    def cancel(self, *, reason="cancelled"):
        if self.outcome is not None:
            return
        self.emit({"type": "progress", "phase": "cancelling"})
        self._send(self.session.cancel())
        self.session.verify(self.transport.read_profiles())
        self._finish("cancelled", verified=True, reason=reason)

    def recover(self, error):
        """One best-effort cancel; never replay a failed start/commit/reset."""
        original = str(error)
        if self.session is None or not self.device_may_have_changed:
            self._finish("error", verified=False, error=original)
            return
        try:
            self.session.fail()
            if self.session.cancel_sent:
                raise DeviceError("Cancellation was already attempted")
            self.cancel(reason=original)
        except (OSError, ValueError, DeviceError) as cancel_error:
            self._finish("uncertain", verified=False,
                         error=f"{original}. Cancellation could not be verified: {cancel_error}. Reconnect the mouse and read every profile before continuing.")

    def _finish(self, outcome, **extra):
        self.outcome = {"type": "result", "outcome": outcome,
                        "device_may_have_changed": self.device_may_have_changed, **extra}
        self.emit(self.outcome)


class JsonCommandReader:
    """Bounded nonblocking JSON lines; EOF means cancel once calibration starts."""

    def __init__(self, fd):
        self.fd, self.buffer = fd, bytearray()
        self.eof = False

    def read(self, timeout=0):
        if b"\n" not in self.buffer and not self.eof and pipe_readable(self.fd, timeout):
            chunk = os.read(self.fd, 4097)
            if not chunk:
                self.eof = True
            self.buffer.extend(chunk)
        if len(self.buffer) > 4096:
            raise DeviceError("Calibration command is too large")
        if b"\n" not in self.buffer:
            if self.eof and self.buffer:
                raise DeviceError("Incomplete calibration command")
            return None
        line, _, rest = self.buffer.partition(b"\n")
        self.buffer = bytearray(rest)
        value = json.loads(line)
        if not isinstance(value, dict):
            raise DeviceError("Calibration command must be an object")
        return value


def _transport_factory(device_id):
    if platform.system() == "Linux":
        return HidrawTransport(device_id)
    if platform.system() == "Darwin":
        from .macos import MacOSTransport
        return MacOSTransport(device_id)
    if platform.system() == "Windows":
        from .windows import WindowsTransport
        return WindowsTransport(device_id)
    raise DeviceError("DCU calibration requires a USB-connected mouse on Linux, macOS or Windows")


def main():
    def emit(value):
        print(json.dumps(value), flush=True)

    # An ioctl may block below Python. The independent watchdog guarantees the
    # child cannot outlive this bound; the GUI also treats a lost child as uncertain.
    watchdog = threading.Timer(HARD_TIMEOUT_SECONDS, lambda: os._exit(3))
    watchdog.daemon = True
    watchdog.start()
    runtime = None
    try:
        reader = JsonCommandReader(sys.stdin.fileno())
        initial = reader.read(5)
        if not initial or initial.get("command") != "start" or set(initial) - {"command", "device_id", "timeout_seconds"}:
            raise DeviceError("Explicit Start is required before calibration")
        device_id = initial.get("device_id")
        if not isinstance(device_id, str) or not device_id or len(device_id) > 1024:
            raise DeviceError("A valid mouse connection is required")
        timeout = initial.get("timeout_seconds", 90)
        if type(timeout) not in (float, int) or not 5 <= timeout <= 90:
            raise DeviceError("Helper calibration timeout must be 5..90 seconds")
        with _transport_factory(device_id) as transport:
            runtime = DcuRuntime(CalibrationTransport(transport), emit, timeout_seconds=timeout)
            try:
                runtime.start()
                while runtime.outcome is None:
                    command = reader.read()
                    if command is not None:
                        if command == {"command": "cancel"}:
                            runtime.cancel()
                        elif command == {"command": "commit"}:
                            runtime.commit()
                        else:
                            raise DeviceError("Only explicit Commit or Cancel is allowed during calibration")
                    elif reader.eof:
                        runtime.cancel(reason="controller_closed")
                    else:
                        runtime.tick()
            except (OSError, ValueError, DeviceError) as error:
                runtime.recover(error)
        return 0 if runtime.outcome["outcome"] in ("committed", "cancelled") else 2
    except (OSError, ValueError, DeviceError) as error:
        emit({"type": "result", "outcome": "uncertain" if runtime and runtime.device_may_have_changed else "error",
              "verified": False, "device_may_have_changed": bool(runtime and runtime.device_may_have_changed), "error": str(error)})
        return 2
    finally:
        watchdog.cancel()


if __name__ == "__main__":
    raise SystemExit(main())
