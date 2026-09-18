"""Bounded, injected-transport Realtek CFU transfer for the traced MC7 path.

The caller owns identity binding, exclusive locking, trusted package download,
backup, reconnect, version verification and any required settings migration.
There is deliberately no abort/retry operation after a possibly accepted write:
RTKCFUStopUpdate in the vendor DLL is a no-op, not a device recovery command.
"""
from __future__ import annotations

import time
from typing import Callable, Protocol

from .firmware_commands import (
    CONTENT_RESPONSE_ID, OFFER_REPORT_ID, VERSION_REPORT_ID, VERSION_REPORT_LENGTH,
    FirmwareProtocolError, FirmwareVersionResponse, build_offer_report,
    decode_content_response, decode_offer_response, decode_version_response,
    iter_content_reports, parse_offer, validate_payload,
)


class FirmwareTransport(Protocol):
    def begin_update(self) -> None: ...
    def get_feature(self, report_id: int, length: int) -> bytes: ...
    def set_output(self, data: bytes) -> None: ...
    def read_input(self, timeout: float) -> bytes: ...


class UpdatePackage(Protocol):
    offer: bytes
    payload: bytes


class FirmwareUpdateError(RuntimeError):
    def __init__(self, message: str, *, device_may_have_changed: bool = False,
                 records_acknowledged: int = 0, content_records_sent: int = 0,
                 phase: str = "preflight") -> None:
        super().__init__(message)
        self.device_may_have_changed = device_may_have_changed
        self.records_acknowledged = records_acknowledged
        self.content_records_sent = content_records_sent
        self.phase = phase


def inspect_update(version: FirmwareVersionResponse, package: UpdatePackage) -> dict:
    """Reject unsupported paths without sending an offer or any payload."""
    offer = parse_offer(package.offer)
    payload = validate_payload(package.payload)
    component = version.component
    # Marker 00 is compatible with IC part 1 in RTK's static table, and selects
    # UpdateFlowControl (one image). Other branches need separate acceptance.
    if version.protocol_version != 4 or component.component_id != 0 or component.bank != 2:
        raise FirmwareProtocolError("This device does not use the supported MC7 CFU update path")
    if offer.component_id != 0x0F or (offer.raw[12] >> 4) & 3 != 0:
        raise FirmwareProtocolError("The firmware offer does not match the supported MC7 update path")
    if payload.record_count < 2:
        raise FirmwareProtocolError("Firmware payload must contain both first and last records")
    if component.version_raw == offer.version_raw:
        raise FirmwareProtocolError("This firmware version is already installed")
    return {
        "supported": True, "path": "realtek_single_image", "component_id": offer.component_id,
        "device_component_marker": component.component_id, "bank": component.bank,
        "installed_version_raw": component.version_raw, "target_version_raw": offer.version_raw,
        "record_count": payload.record_count, "data_bytes": payload.data_bytes,
        "requires_reconnect_verification": True, "cancellation_supported": False,
    }


def run_update(transport: FirmwareTransport, package: UpdatePackage,
               progress_callback: Callable[[dict], None] | None = None, *,
               timeout_seconds: float = 1800.0, response_timeout: float = 2.0) -> dict:
    """Transfer one verified offer/payload and require each exact acknowledgement.

    A successful return means every packet was acknowledged. It does not mean
    the new image booted or that settings migration succeeded. Unknown or lost
    acknowledgements stop the transfer; no content packet is replayed.
    """
    if not 0 < timeout_seconds <= 3600 or not 0 < response_timeout <= 5:
        raise FirmwareUpdateError("Invalid firmware transfer time limits")
    started = time.monotonic()
    deadline = started + timeout_seconds
    acknowledged = 0
    content_sent = 0
    changed = False
    phase = "preflight"

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("Firmware transfer reached its time limit")
        return value

    def progress(**values) -> None:
        if progress_callback is not None:
            try:
                progress_callback({"phase": phase, "records_acknowledged": acknowledged, **values})
            except Exception:
                # Losing a window/progress pipe is not a device abort command.
                # Finish the acknowledged transfer even if presentation fails.
                pass

    def wait_report(report_id: int) -> bytes:
        response_deadline = time.monotonic() + min(response_timeout, remaining())
        ignored = 0
        while True:
            wait = min(response_deadline - time.monotonic(), remaining())
            if wait <= 0:
                raise TimeoutError(f"No CFU acknowledgement for report {report_id:02X}")
            reply = transport.read_input(wait)
            if not reply:
                raise TimeoutError(f"No CFU acknowledgement for report {report_id:02X}")
            if reply[0] == report_id:
                return reply
            # An out-of-order CFU reply can be stale or from another sender.
            if reply[0] in (OFFER_REPORT_ID, CONTENT_RESPONSE_ID):
                raise FirmwareProtocolError("Unexpected CFU acknowledgement order")
            ignored += 1
            if ignored >= 64:
                raise FirmwareProtocolError("Too many unrelated reports during firmware transfer")

    try:
        remaining()
        transport.begin_update()
        version = decode_version_response(transport.get_feature(VERSION_REPORT_ID, VERSION_REPORT_LENGTH))
        plan = inspect_update(version, package)
        report = build_offer_report(package.offer)
        info = validate_payload(package.payload)
        progress(total_records=info.record_count, percent=0)
        # Drain old events before any write; cap a continuously active queue.
        for _ in range(64):
            remaining()
            if not transport.read_input(0.0):
                break
        else:
            raise FirmwareProtocolError("The CFU event queue did not become idle")
        phase = "offer"
        progress(total_records=info.record_count, percent=0)
        remaining()
        changed = True  # Set before I/O: a failing syscall may still deliver it.
        transport.set_output(report)
        response = decode_offer_response(wait_report(OFFER_REPORT_ID), token=package.offer[3])
        if response.status not in (1, 4):
            names = {0: "skipped", 2: "rejected", 3: "busy", 0xFF: "unsupported"}
            name = names.get(response.status, f"status {response.status}")
            raise FirmwareProtocolError(f"The mouse {name} the firmware offer (reason {response.reject_reason:#x})")
        phase = "transfer"
        previous_percent = -1
        previous_progress = time.monotonic()
        for index, report in enumerate(iter_content_reports(package.payload, info)):
            remaining()
            # A failed syscall may still deliver its packet; count attempts.
            content_sent += 1
            transport.set_output(report)
            response = decode_content_response(wait_report(CONTENT_RESPONSE_ID), sequence=index & 0xFFFF)
            if response.status != 0:
                raise FirmwareProtocolError(f"Firmware record {index} failed with device status {response.status:#x}")
            acknowledged += 1
            percent = acknowledged * 100 // info.record_count
            now = time.monotonic()
            if percent != previous_percent or now - previous_progress >= 1:
                progress(total_records=info.record_count, percent=percent)
                previous_percent, previous_progress = percent, now
        phase = "transferred"
        progress(total_records=info.record_count, percent=100)
        return {
            "outcome": "transferred", "version_verified": False,
            "device_may_have_changed": True, "records_acknowledged": acknowledged,
            "target_version_raw": plan["target_version_raw"],
            "installed_version_raw": plan["installed_version_raw"],
            "elapsed_seconds": time.monotonic() - started,
        }
    except Exception as error:
        if isinstance(error, FirmwareUpdateError):
            raise
        detail = " No firmware data was sent." if content_sent == 0 else ""
        message = f"Firmware {phase} stopped: {error}".rstrip(".") + "." + detail
        raise FirmwareUpdateError(message,
                                  device_may_have_changed=changed,
                                  records_acknowledged=acknowledged,
                                  content_records_sent=content_sent, phase=phase) from error
