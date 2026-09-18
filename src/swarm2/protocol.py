"""Offline MC7 packet codecs derived from static Swarm II module analysis.

Sensor reads have been validated on a USB-connected MC7. Physical DPI and RGB
semantics were independently traced through the vendor UI setters and getters.
No checksum was observed in these paths; other commands may require one.
This module performs no device I/O.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Literal

REPORT_BYTES = 64
SUPPORTED_COMMAND_IDS = frozenset((0x05, 0x10, 0x11, 0x12, 0x14, 0x15, 0x16,
                                   0x19, 0x1A, 0x1C, 0x1D, 0x24, 0x25, 0x26, 0x29,
                                   0x2A, 0x2B, 0x2C, 0xA3))


class ProtocolError(ValueError):
    """A value cannot be represented in the statically observed wire format."""


def _uint(name: str, value: int, bits: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(f"{name} must be an integer, not {type(value).__name__}")
    if not 0 <= value < (1 << bits):
        raise ProtocolError(f"{name} must be in 0..{(1 << bits) - 1}")
    return value


def _sequence(name: str, value: Sequence, length: int) -> Sequence:
    if not isinstance(value, Sequence) or isinstance(value, str) or len(value) != length:
        raise ProtocolError(f"{name} must contain exactly {length} items")
    return value


def build_dpi_report(*, profile_index: int, current_dpi_index: int,
                     packed_flags: int, raw_sensor_values: Sequence[int],
                     raw_color_triplets: Sequence[Sequence[int]]) -> bytes:
    """Encode the DPI packet assembled by MC7 function FUN_180278260.

    This low-level codec permits raw four-bit indices and 16-bit sensor fields.
    Use build_physical_dpi_report for confirmed device ranges and DPI units.
    Flags and RGB groups must be explicit because a codec cannot infer device
    state. Stage and profile numbering on the wire is zero based.
    """
    profile = _uint("profile_index", profile_index, 4)
    current = _uint("current_dpi_index", current_dpi_index, 4)
    flags = _uint("packed_flags", packed_flags, 8)
    values = _sequence("raw_sensor_values", raw_sensor_values, 5)
    colors = _sequence("raw_color_triplets", raw_color_triplets, 5)
    report = bytearray(REPORT_BYTES)
    report[:4] = bytes((0x10, 0x14, (profile << 4) | current, flags))
    for index, value in enumerate(values):
        encoded = _uint(f"raw_sensor_values[{index}]", value, 16)
        report[4 + 2 * index:6 + 2 * index] = encoded.to_bytes(2, "little")
    for index, color in enumerate(colors):
        channels = _sequence(f"raw_color_triplets[{index}]", color, 3)
        report[14 + 3 * index:17 + 3 * index] = bytes(
            _uint(f"raw_color_triplets[{index}][{channel}]", value, 8)
            for channel, value in enumerate(channels)
        )
    return bytes(report)


def build_sensor_read_request(profile_index: int) -> bytes:
    """Encode the observed sensor-read request, without issuing it.

    This request carries a full raw profile byte (0..255), unlike the DPI
    packet's nibble. That format range does not imply 256 supported profiles.
    """
    profile = _uint("profile_index", profile_index, 8)
    report = bytearray(REPORT_BYTES)
    report[:7] = bytes((0x10, 0x1C, 0x00, 0x10, profile, 0x00, 0x00))
    return bytes(report)


def build_sensor_get_buffer() -> bytes:
    """Return the observed initial buffer for a subsequent feature GET.

    This is the vendor call's in-memory buffer, not a separate write request.
    The platform transport's GET API determines which bytes reach the device.
    """
    return bytes((0x10, 0x10)) + bytes(REPORT_BYTES - 2)


class AckStatus(str, Enum):
    ACCEPTED = "accepted"
    BUSY = "busy"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Acknowledgement:
    target: Literal["mouse", "transmitter"]
    discriminator: int
    status_code: int
    status: AckStatus
    raw: bytes


def decode_acknowledgement(data: bytes, *, target: Literal["mouse", "transmitter"]) -> Acknowledgement:
    """Summarize only the discriminator/status checks observed in the module.

    Exactly eight bytes are required. Byte 2 must match the explicitly selected
    target (mouse F2, transmitter 06). Byte 4 maps to accepted (0) for either
    target. Only the mouse wrapper establishes busy (1/2); nonzero transmitter
    statuses and other mouse statuses remain unknown. Other fields are retained
    without interpretation: this does not correlate an acknowledgement with a
    command or establish hardware success.
    """
    targets = {"mouse": 0xF2, "transmitter": 0x06}
    if not isinstance(target, str) or target not in targets:
        raise ProtocolError("target must be 'mouse' or 'transmitter'")
    if not isinstance(data, (bytes, bytearray)) or len(data) != 8:
        raise ProtocolError("acknowledgement must contain exactly 8 bytes")
    if data[2] != targets[target]:
        raise ProtocolError(f"acknowledgement discriminator does not match {target}")
    code = data[4]
    if code == 0:
        status = AckStatus.ACCEPTED
    elif target == "mouse" and code in (1, 2):
        status = AckStatus.BUSY
    else:
        status = AckStatus.UNKNOWN
    return Acknowledgement(target, data[2], code, status, bytes(data))


@dataclass(frozen=True)
class SensorState:
    """Validated sensor response. Unknown fields are preserved without names."""

    raw: bytes
    profile_index: int
    current_stage: int
    dpi: tuple[int, ...]
    colors: tuple[tuple[int, int, int], ...]
    enabled: tuple[bool, ...]
    indicator_enabled: bool

    @property
    def dpi_signature(self) -> tuple:
        """Only fields touched by command 0x14, for conflict/readback checks."""
        return (self.profile_index, self.current_stage, self.dpi, self.colors,
                self.enabled, self.indicator_enabled)


def decode_sensor_response(data: bytes, profile_index: int) -> SensorState:
    profile = _uint("profile_index", profile_index, 8)
    if profile > 4:
        raise ProtocolError("MC7 has five profiles, indexed 0..4")
    if len(data) not in (48, 64):
        raise ProtocolError("sensor response must contain 48 or 64 bytes")
    if data[:4] != bytes((0x10, 0x10, 0, profile)):
        raise ProtocolError("sensor response report, command, status, or profile does not match")
    if data[9] > 4:
        raise ProtocolError("device returned an invalid current DPI stage")
    values = tuple((int.from_bytes(data[11+7*i:13+7*i], "little") + 1) * 50
                   for i in range(5))
    if any(value > 30_000 for value in values):
        raise ProtocolError("device returned DPI outside the supported 50..30000 range")
    flags = tuple(data[10+7*i] for i in range(5))
    indicators = tuple(data[16+7*i] for i in range(5))
    if any(value not in (0, 1) for value in (*flags, *indicators)):
        raise ProtocolError("device returned unknown DPI flags; refusing to reinterpret them")
    if len(set(indicators)) != 1:
        raise ProtocolError("device returned inconsistent shared DPI indicator flags")
    return SensorState(bytes(data[:48]), profile, data[9], values,
                       tuple(tuple(data[13+7*i:16+7*i]) for i in range(5)),
                       tuple(bool(value) for value in flags), bool(indicators[0]))


def build_physical_dpi_report(*, profile_index: int, current_stage: int,
                              dpi: Sequence[int], colors: Sequence[Sequence[int]],
                              enabled: Sequence[bool], indicator_enabled: bool) -> bytes:
    """Encode the source-confirmed 50-DPI units, RGB channels and enable bits."""
    if _uint("profile_index", profile_index, 4) > 4:
        raise ProtocolError("profile_index must be in 0..4")
    if _uint("current_stage", current_stage, 4) > 4:
        raise ProtocolError("current_stage must be in 0..4")
    values = _sequence("dpi", dpi, 5)
    flags = _sequence("enabled", enabled, 5)
    for value in values:
        _uint("dpi", value, 16)
        if not 50 <= value <= 30_000 or value % 50:
            raise ProtocolError("DPI must be 50..30000 in steps of 50")
    if any(type(flag) is not bool for flag in (*flags, indicator_enabled)):
        raise ProtocolError("DPI enable flags must be booleans")
    if not any(flags) or not flags[current_stage]:
        raise ProtocolError("The current DPI stage must be enabled")
    packed = sum(int(flag) << index for index, flag in enumerate(flags))
    packed |= int(indicator_enabled) << 7
    return build_dpi_report(profile_index=profile_index, current_dpi_index=current_stage,
                            packed_flags=packed, raw_sensor_values=[v//50-1 for v in values],
                            raw_color_triplets=colors)
