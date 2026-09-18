"""Memory-only codecs for the MC7's Realtek CFU reports.

Layouts come from RTKHIDKit 1.0.14.0 and COMMAND_MC7 0.0.0.7.
This module never opens a device or sends a report. See firmware-protocol.md.
"""
from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import Iterator

VERSION_REPORT_ID = 0x2A  # Feature, usage FF0B/0062 (not feature 2B/0065).
VERSION_REPORT_LENGTH = 64  # Shared interface's maximum feature report length.
OFFER_REPORT_ID = 0x2D
CONTENT_REPORT_ID = 0x2A
CONTENT_RESPONSE_ID = 0x2C
OUTPUT_REPORT_LENGTH = 61  # Vendor API envelope; USB offers use only 17 bytes.
INPUT_REPORT_LENGTH = 17
PAYLOAD_RECORD_LENGTH = 57
PAYLOAD_DATA_LENGTH = 52
MAX_PAYLOAD_LENGTH = 64 * 1024 * 1024


class FirmwareProtocolError(ValueError):
    """A firmware file or device report does not match the established layout."""


def decode_realtek_version(version_raw: int) -> tuple[int, int, int, int]:
    """Decode RTK IC part 1's version fields (not a plain four-byte version).

    The MC7's user-facing major/minor are fields 2/3: the native 504 response
    decodes to (1, 7, 5, 4), and official 509 offer to (1, 7, 5, 9).
    """
    if type(version_raw) is not int or not 0 <= version_raw <= 0xFFFFFFFF:
        raise FirmwareProtocolError("Realtek firmware version must be a uint32")
    return (version_raw & 15, (version_raw >> 4) & 255,
            (version_raw >> 12) & 32767, (version_raw >> 27) & 31)


@dataclass(frozen=True)
class FirmwareOffer:
    raw: bytes
    component_id: int
    version_raw: int
    token: int
    bank: int


@dataclass(frozen=True)
class PayloadInfo:
    record_count: int
    data_bytes: int
    base_address: int
    end_address: int


@dataclass(frozen=True)
class FirmwareComponent:
    component_id: int
    version_raw: int
    flags: int
    vendor_data: bytes

    @property
    def bank(self) -> int:
        return self.flags & 3


@dataclass(frozen=True)
class FirmwareVersionResponse:
    raw: bytes
    protocol_version: int
    components: tuple[FirmwareComponent, ...]

    @property
    def component(self) -> FirmwareComponent:
        if len(self.components) != 1:
            raise FirmwareProtocolError("This update path requires exactly one CFU component")
        return self.components[0]

    @property
    def component_id(self) -> int:
        return self.component.component_id

    @property
    def version_raw(self) -> int:
        return self.component.version_raw

    @property
    def bank(self) -> int:
        return self.component.bank


@dataclass(frozen=True)
class OfferResponse:
    status: int
    token: int
    reject_reason: int


@dataclass(frozen=True)
class ContentResponse:
    sequence: int
    status: int


def _bytes(value: bytes, name: str) -> bytes:
    if not isinstance(value, bytes):
        raise FirmwareProtocolError(f"{name} must be immutable bytes")
    return value


def parse_offer(offer: bytes) -> FirmwareOffer:
    _bytes(offer, "Firmware offer")
    if len(offer) != 16:
        raise FirmwareProtocolError("A Realtek firmware offer must contain exactly 16 bytes")
    if offer[2] not in (0x0F, 0x10):
        raise FirmwareProtocolError("Unsupported Realtek firmware component")
    version = int.from_bytes(offer[4:8], "little")
    if version == 0:
        raise FirmwareProtocolError("Firmware offer has no version")
    return FirmwareOffer(offer, offer[2], version, offer[3], offer[10] & 3)


def validate_payload(payload: bytes) -> PayloadInfo:
    """Validate the contiguous 57-byte record stream before any device write.

    The vendor reader checks divisibility only. We additionally reject zero or
    oversized records, overlaps, gaps and address wrap. Both official packages
    have contiguous 52-byte data records starting at address zero.
    """
    _bytes(payload, "Firmware payload")
    if not payload or len(payload) > MAX_PAYLOAD_LENGTH or len(payload) % PAYLOAD_RECORD_LENGTH:
        raise FirmwareProtocolError("Firmware payload must contain bounded 57-byte records")
    base = int.from_bytes(payload[:4], "little")
    end = base
    total = 0
    for offset in range(0, len(payload), PAYLOAD_RECORD_LENGTH):
        address, size = struct.unpack_from("<IB", payload, offset)
        if not 1 <= size <= PAYLOAD_DATA_LENGTH:
            raise FirmwareProtocolError(f"Invalid firmware record length at record {offset // 57}")
        if address != end or address + size > 0x100000000:
            raise FirmwareProtocolError("Firmware payload addresses are not contiguous or overflow")
        end = address + size
        total += size
    return PayloadInfo(len(payload) // PAYLOAD_RECORD_LENGTH, total, base, end)


def build_offer_report(offer: bytes) -> bytes:
    """Match the original SetUpdateConfig(0x0100): reset, without force-update."""
    parsed = parse_offer(offer)
    wire = bytearray(OUTPUT_REPORT_LENGTH)
    wire[0] = OFFER_REPORT_ID
    wire[1:17] = parsed.raw
    wire[2] = (wire[2] & 0x3F) | 0x40  # ForceReset yes, ForceIgnoreVersion no.
    wire[11] &= 0xFB  # Original configuration disables this Realtek option.
    return bytes(wire)


def iter_content_reports(payload: bytes, info: PayloadInfo | None = None) -> Iterator[bytes]:
    """Yield source-exact content reports, including uint16 sequence wrap.

    Callers must not supply an unvalidated PayloadInfo; it is checked against
    the input here so public use cannot bypass whole-file validation.
    """
    checked = validate_payload(payload)
    if info is not None and info != checked:
        raise FirmwareProtocolError("Payload validation does not match the firmware bytes")
    for index in range(checked.record_count):
        offset = index * PAYLOAD_RECORD_LENGTH
        address, size = struct.unpack_from("<IB", payload, offset)
        # Source uses if/else-if. Real packages contain more than one record.
        flags = 0x80 if index == 0 else 0x40 if index == checked.record_count - 1 else 0
        yield struct.pack("<BBBHI", CONTENT_REPORT_ID, flags, size, index & 0xFFFF,
                          address - checked.base_address) + payload[offset + 5:offset + 57]


def decode_version_response(data: bytes) -> FirmwareVersionResponse:
    _bytes(data, "CFU version response")
    if len(data) not in (61, 64) or data[0] != VERSION_REPORT_ID:
        raise FirmwareProtocolError("Expected the CFU version feature report (2A, 61 or 64 bytes)")
    if len(data) == 64 and any(data[61:]):
        raise FirmwareProtocolError("Unexpected CFU feature padding")
    count = data[1]
    if not 1 <= count <= 7 or data[2:4] != b"\0\0" or data[4] != 4:
        raise FirmwareProtocolError("The mouse did not return a supported CFU version header")
    components = []
    for index in range(count):
        start = 5 + index * 8
        version = int.from_bytes(data[start:start + 4], "little")
        flags, component_id = data[start + 4:start + 6]
        if not version or component_id == 0xFF:
            raise FirmwareProtocolError("The mouse returned an invalid CFU component version")
        components.append(FirmwareComponent(component_id, version, flags, data[start + 6:start + 8]))
    if len({component.component_id for component in components}) != len(components):
        raise FirmwareProtocolError("Duplicate CFU component identifiers")
    if any(data[5 + count * 8:61]):
        raise FirmwareProtocolError("Unexpected data after the CFU component list")
    return FirmwareVersionResponse(data, 4, tuple(components))


def decode_offer_response(data: bytes, *, token: int = 0) -> OfferResponse:
    _bytes(data, "CFU offer response")
    if len(data) != INPUT_REPORT_LENGTH or data[0] != OFFER_REPORT_ID:
        raise FirmwareProtocolError("Expected a 17-byte CFU offer response")
    if data[4] != token:
        raise FirmwareProtocolError("CFU offer response has the wrong transaction token")
    status = data[13]
    if status not in (0, 1, 2, 3, 4, 0xFF):
        raise FirmwareProtocolError(f"Unknown CFU offer status {status:#x}")
    return OfferResponse(status, data[4], data[9])


def decode_content_response(data: bytes, *, sequence: int) -> ContentResponse:
    _bytes(data, "CFU content response")
    if len(data) != INPUT_REPORT_LENGTH or data[0] != CONTENT_RESPONSE_ID:
        raise FirmwareProtocolError("Expected a 17-byte CFU content response")
    actual = int.from_bytes(data[1:3], "little")
    if actual != sequence:
        raise FirmwareProtocolError(f"CFU content acknowledgement sequence {actual} does not match {sequence}")
    return ContentResponse(actual, data[5])
