"""Offline, guarded custom lift-off calibration commands and session state.

No device I/O is performed here. The caller must retain the same exclusive
control/event connection for the whole session, drain old events before start,
and send each returned command at most once.
"""

from dataclasses import dataclass
from enum import Enum
import math
from typing import Sequence

from .protocol import AckStatus, ProtocolError, decode_acknowledgement, decode_sensor_response


def _report(value: int) -> bytes:
    return bytes((0x10, 0x19, value, 0)) + bytes(60)


def build_start_report() -> bytes:
    return _report(0x90)


def build_cancel_report() -> bytes:
    return _report(0x93)


def build_commit_reports() -> tuple[bytes, bytes]:
    """Reset followed by commit; require a separate accepted ACK for each."""
    return _report(0x93), _report(0x92)


@dataclass(frozen=True)
class DcuCompletionEvent:
    """The vendor's D1 completion indication, without guessed result fields."""

    raw: bytes


def decode_completion_event(data: bytes) -> DcuCompletionEvent:
    if not isinstance(data, (bytes, bytearray)) or len(data) != 8:
        raise ProtocolError("DCU completion must be an eight-byte input report")
    if data[0] != 0x10 or data[2] != 0xD1 or data[1] == 0x33:
        raise ProtocolError("Input report is not a DCU D1 completion event")
    return DcuCompletionEvent(bytes(data))


class DcuPhase(str, Enum):
    READY = "ready"
    STARTING = "starting"
    RUNNING = "running"
    RESULT_READY = "result_ready"
    COMMIT_RESETTING = "commit_resetting"
    COMMIT_READY = "commit_ready"
    COMMITTING = "committing"
    VERIFYING_COMMIT = "verifying_commit"
    CANCELLING = "cancelling"
    VERIFYING_CANCEL = "verifying_cancel"
    COMMITTED = "committed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    UNCERTAIN = "uncertain"


def _timestamp(value: float) -> float:
    if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
        raise ProtocolError("Calibration time must be a finite nonnegative monotonic value")
    return float(value)


def _profiles(records: Sequence[bytes]) -> tuple[bytes, ...]:
    if not isinstance(records, (list, tuple)) or len(records) != 5:
        raise ProtocolError("DCU calibration requires sensor reads for all five profiles")
    if any(not isinstance(record, (bytes, bytearray)) for record in records):
        raise ProtocolError("DCU sensor reads must be byte strings")
    return tuple(decode_sensor_response(record, index).raw[:48]
                 for index, record in enumerate(records))


class DcuCalibrationSession:
    """Source-derived state ordering with additional conservative host guards.

    Start from a fresh, uniform Very Low/Low preset. Old custom coefficients
    cannot currently be backed up, so they are rejected. Time is supplied by
    the caller for deterministic tests and an explicit host deadline.
    """

    def __init__(self, baseline: Sequence[bytes], *, timeout_seconds: float = 90):
        self.baseline = _profiles(baseline)
        values = {record[45] for record in self.baseline}
        if len(values) != 1 or next(iter(values)) not in (0, 0x85):
            raise ProtocolError("Start DCU calibration from the same Very Low or Low preset in all five profiles; existing custom calibration cannot be backed up")
        timeout = _timestamp(timeout_seconds)
        if not 5 <= timeout <= 300:
            raise ProtocolError("Calibration timeout must be between 5 and 300 seconds")
        self.timeout_seconds = timeout
        self.phase = DcuPhase.READY
        self.deadline: float | None = None
        self.completion: DcuCompletionEvent | None = None
        self.cancel_sent = False

    def _require(self, *phases):
        if self.phase not in phases:
            raise ProtocolError(f"DCU operation is unavailable while {self.phase.value}")

    def start(self, *, now: float) -> bytes:
        self._require(DcuPhase.READY)
        self.deadline = _timestamp(now) + self.timeout_seconds
        self.phase = DcuPhase.STARTING
        return build_start_report()

    def check_timeout(self, *, now: float) -> bool:
        now = _timestamp(now)
        if (self.phase in (DcuPhase.STARTING, DcuPhase.RUNNING, DcuPhase.RESULT_READY)
                and self.deadline is not None and now >= self.deadline):
            self.phase = DcuPhase.TIMED_OUT
        return self.phase == DcuPhase.TIMED_OUT

    def observe_completion(self, data: bytes, *, now: float) -> bool:
        """Only an event received during this start attempt can finish it.

        D1 can precede the start ACK; remember it, but do not enable commit
        until the ACK is also accepted. Unrelated/malformed events are ignored.
        """
        if self.check_timeout(now=now) or self.phase not in (DcuPhase.STARTING, DcuPhase.RUNNING):
            return False
        try:
            completion = decode_completion_event(data)
        except ProtocolError:
            return False
        self.completion = completion
        if self.phase == DcuPhase.RUNNING:
            self.phase = DcuPhase.RESULT_READY
        return True

    def acknowledge(self, data: bytes) -> bool:
        """Consume the current command's matching F2 ACK; busy is not success.

        No ACK, including F2/19/0, establishes calibration completion. All DCU
        subcommands share command ID19, so the transport must remove stale ACKs
        and serialize writes; this class cannot invent a subcommand correlation.
        """
        self._require(DcuPhase.STARTING, DcuPhase.COMMIT_RESETTING,
                      DcuPhase.COMMITTING, DcuPhase.CANCELLING)
        ack = decode_acknowledgement(data, target="mouse")
        if ack.raw[0] != 0x10 or ack.raw[3] != 0x19:
            raise ProtocolError("ACK does not match the DCU command")
        if ack.status == AckStatus.BUSY:
            return False
        if ack.status != AckStatus.ACCEPTED:
            self.phase = DcuPhase.UNCERTAIN
            raise ProtocolError(f"DCU command returned unknown/rejected status {ack.status_code}")
        self.phase = {
            DcuPhase.STARTING: DcuPhase.RESULT_READY if self.completion else DcuPhase.RUNNING,
            DcuPhase.COMMIT_RESETTING: DcuPhase.COMMIT_READY,
            DcuPhase.COMMITTING: DcuPhase.VERIFYING_COMMIT,
            DcuPhase.CANCELLING: DcuPhase.VERIFYING_CANCEL,
        }[self.phase]
        return True

    def begin_commit(self, *, now: float) -> bytes:
        """Called only after explicit acceptance of the completed calibration."""
        self.check_timeout(now=now)
        self._require(DcuPhase.RESULT_READY)
        self.phase = DcuPhase.COMMIT_RESETTING
        return build_commit_reports()[0]

    def commit(self) -> bytes:
        self._require(DcuPhase.COMMIT_READY)
        self.phase = DcuPhase.COMMITTING
        return build_commit_reports()[1]

    def cancel(self) -> bytes:
        """One best-effort reset, including after a failed/ambiguous write.

        This does not claim to restore any calibration matrix or connection.
        A missing ACK must remain uncertain; this method never auto-replays.
        """
        if self.cancel_sent:
            raise ProtocolError("DCU cancellation has already been attempted; reconnect and read settings before recovery")
        self._require(DcuPhase.STARTING, DcuPhase.RUNNING, DcuPhase.RESULT_READY,
                      DcuPhase.COMMIT_RESETTING, DcuPhase.COMMIT_READY,
                      DcuPhase.COMMITTING, DcuPhase.VERIFYING_COMMIT,
                      DcuPhase.TIMED_OUT, DcuPhase.UNCERTAIN)
        self.cancel_sent = True
        self.phase = DcuPhase.CANCELLING
        return build_cancel_report()

    def fail(self) -> None:
        """The transport could not establish the outcome. Do not retry writes."""
        self._require(DcuPhase.STARTING, DcuPhase.RUNNING, DcuPhase.RESULT_READY,
                      DcuPhase.COMMIT_RESETTING, DcuPhase.COMMIT_READY,
                      DcuPhase.COMMITTING, DcuPhase.VERIFYING_COMMIT,
                      DcuPhase.CANCELLING, DcuPhase.VERIFYING_CANCEL,
                      DcuPhase.TIMED_OUT, DcuPhase.UNCERTAIN)
        self.phase = DcuPhase.UNCERTAIN

    def verify(self, records: Sequence[bytes]) -> None:
        """Verify all profile LOD values and preserve other sensor settings.

        Sensor checksum byte47 is allowed to change with LOD byte45. Firmware
        readback, not the source's cache update, must establish the custom1 value.
        """
        self._require(DcuPhase.VERIFYING_COMMIT, DcuPhase.VERIFYING_CANCEL)
        committing = self.phase == DcuPhase.VERIFYING_COMMIT
        try:
            current = _profiles(records)
            for before, after in zip(self.baseline, current):
                expected_lod = 1 if committing else before[45]
                if after[45] != expected_lod:
                    raise ProtocolError("DCU lift-off readback differs from the expected result; recovery remains uncertain")
                if before[:45] + before[46:47] != after[:45] + after[46:47]:
                    raise ProtocolError("Other mouse sensor settings changed during calibration; read all profiles before continuing")
        except ProtocolError:
            self.phase = DcuPhase.UNCERTAIN
            raise
        self.phase = DcuPhase.COMMITTED if committing else DcuPhase.CANCELLED
