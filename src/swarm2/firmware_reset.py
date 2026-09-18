"""Single-attempt, explicitly invoked post-update MC7 settings migration.

No device is discovered or opened here. The caller owns the exclusive normal
transport and must back up settings before authorizing a firmware update that
requires this reset. An ACK does not establish reset completion or settings
readback.
"""

from dataclasses import dataclass
import math
import time

from .dcu import CalibrationTransport
from .protocol import AckStatus, ProtocolError, decode_acknowledgement
from .transport import DeviceError

RESET_REPORT = bytes.fromhex("10 13 5a 00") + bytes(60)
MAX_RESET_SECONDS = 30
ACK_TIMEOUT_SECONDS = 2


@dataclass(frozen=True)
class ResetCompletion:
    raw: bytes


def decode_reset_completion(data: bytes) -> ResetCompletion:
    """Decode only the source's distinct B1/F5 success events.

    The vendor routes byte1=33 elsewhere before checking byte2; that report
    cannot complete this operation. Unclassified trailing event bytes are kept.
    """
    if (not isinstance(data, (bytes, bytearray)) or len(data) != 8
            or data[0] != 0x10 or data[1] == 0x33
            or data[2] not in (0xB1, 0xF5) or data[3] != 0):
        raise ProtocolError("Input report is not an MC7 reset-completion event")
    return ResetCompletion(bytes(data))


class FirmwareResetTransport(CalibrationTransport):
    """Specialized direct write/event pump over a caller-owned normal handle."""

    def write(self, report):
        if type(report) is not bytes or report != RESET_REPORT:
            raise DeviceError("Unsupported firmware migration reset command")
        if isinstance(self.transport.control, int):
            import fcntl
            count = fcntl.ioctl(self.transport.control, self.transport._request(6, 64),
                                bytearray(report), True)
        else:
            count = self.transport.control.send_feature_report(report)
        if count != len(RESET_REPORT):
            raise DeviceError("Firmware migration reset feature transfer was incomplete")


def perform_factory_reset(transport, *, timeout_seconds=MAX_RESET_SECONDS,
                          clock=time.monotonic):
    """Send once, then require both accepted F2/13 and fresh B1/F5 completion.

    A pre-send drain removes stale reports. A completion arriving before the
    ACK remains valid for this attempt; no reports are discarded after sending.
    Failures never replay the reset. Read every profile afterward to establish
    the resulting configuration; this function does not claim to preserve it.
    """
    if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
            or not ACK_TIMEOUT_SECONDS <= timeout_seconds <= MAX_RESET_SECONDS):
        raise DeviceError("Firmware migration reset timeout must be 2..30 seconds")
    # A failed drain is known to precede any reset command in this attempt.
    transport.drain()
    started = clock()
    deadline, ack_deadline = started + timeout_seconds, started + ACK_TIMEOUT_SECONDS
    acknowledgement, completion = None, None
    try:
        transport.write(RESET_REPORT)
        while acknowledgement is None or completion is None:
            now = clock()
            if now >= deadline:
                raise DeviceError("The mouse did not report reset completion before the deadline")
            if acknowledgement is None and now >= ack_deadline:
                raise DeviceError("The mouse did not acknowledge the reset command")
            wait_until = deadline if acknowledgement is not None else min(deadline, ack_deadline)
            event = transport.read_event(min(0.05, max(0, wait_until - now)))
            if not event:
                continue
            if (len(event) == 8 and event[0] == 0x10 and event[2:4] == b"\xf2\x13"):
                ack = decode_acknowledgement(event, target="mouse")
                if ack.status == AckStatus.ACCEPTED:
                    acknowledgement = ack.raw
                elif ack.status != AckStatus.BUSY:
                    raise DeviceError(f"The mouse rejected reset with status {ack.status_code}")
                continue
            try:
                completion = decode_reset_completion(event).raw
            except ProtocolError:
                pass
        return {"acknowledged": True, "reset_completed": True,
                "acknowledgement": acknowledgement.hex(), "completion_event": completion.hex()}
    except (OSError, ValueError, DeviceError) as error:
        raise DeviceError(f"Firmware migration reset could not be verified: {error}. "
                          "The mouse may have reset; reconnect and read every profile before continuing.") from error
