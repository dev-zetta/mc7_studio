"""MC7 lighting records and source-traced effect IDs, without device I/O.

Firmware 5.04 returns a compact 12-byte record. Firmware 5.09 returns the
13-byte layout used by COMMAND_MC7.dll 0.0.0.7. Both layouts are retained
exactly so a write cannot shift an opaque or version-specific field.
"""

from dataclasses import dataclass
from collections.abc import Sequence
from types import MappingProxyType

from .protocol import ProtocolError, REPORT_BYTES


LIGHTING_COMMAND = 0x2A
LIGHTING_COMPACT_RESPONSE_BYTES = 12
LIGHTING_EXTENDED_RESPONSE_BYTES = 13
LIGHTING_EFFECT_IDS = MappingProxyType({
    "off": 0,
    "static": 1,
    "blink": 2,
    "breathing": 3,
    "heartbeat": 4,
    "aimo": 5,
    "wave": 6,
})
LIGHTING_EFFECT_NAMES = MappingProxyType({value: name for name, value in LIGHTING_EFFECT_IDS.items()})


def _integer(name: str, value: int, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProtocolError(f"{name} must be an integer in {minimum}..{maximum}")
    return value


@dataclass(frozen=True)
class LightingState:
    """An exact, checksum-validated lighting record from either firmware."""

    raw: bytes
    profile_index: int
    enabled_raw: int | None
    led_timeout_raw: int
    brightness_raw: int
    effect_id: int
    speed: int
    color: tuple[int, int, int]

    @property
    def effect(self) -> str | None:
        if self.enabled_raw == 0:
            return "off"
        return LIGHTING_EFFECT_NAMES.get(self.effect_id)

    @property
    def signature(self) -> tuple:
        """Stable setting values for stale-baseline and readback comparison."""
        return (self.profile_index, self.enabled_raw, self.led_timeout_raw, self.brightness_raw,
                self.effect_id, self.speed, self.color)

    @property
    def extended_layout(self) -> bool:
        return self.enabled_raw is not None

    @property
    def brightness_percent(self) -> int:
        return (self.brightness_raw * 100 + 127) // 255


def build_lighting_read_request(profile_index: int) -> bytes:
    """Build the observed seven-byte read selector, padded to HID length."""
    profile = _integer("profile_index", profile_index, 0, 4)
    return bytes((0x10, 0x1C, 0, LIGHTING_COMMAND, profile, 0, 0)) + bytes(REPORT_BYTES - 7)


def build_lighting_get_buffer() -> bytes:
    """Return the source-observed initial GET buffer, not a SET command."""
    return bytes((0x10, LIGHTING_COMMAND)) + bytes(REPORT_BYTES - 2)


def decode_lighting_response(data: bytes, profile_index: int) -> LightingState:
    """Decode a firmware 5.04 compact or firmware 5.09 extended record.

    A 64-byte transport buffer is accepted only when its unused tail is zero.
    An extended record whose checksum is zero is indistinguishable from a
    compact record in a padded buffer, so exact transport lengths are preferred;
    the native transports retain the HID result length.
    Unknown effects and speed values remain visible as their original bytes.
    """
    profile = _integer("profile_index", profile_index, 0, 4)
    if not isinstance(data, (bytes, bytearray)) or len(data) not in (
            LIGHTING_COMPACT_RESPONSE_BYTES, LIGHTING_EXTENDED_RESPONSE_BYTES, REPORT_BYTES):
        raise ProtocolError("lighting response must contain 12, 13 or 64 bytes")
    length = len(data)
    if length == REPORT_BYTES:
        if any(data[LIGHTING_EXTENDED_RESPONSE_BYTES:]):
            raise ProtocolError("lighting response contains data beyond the supported layouts")
        if data[12]:
            length = LIGHTING_EXTENDED_RESPONSE_BYTES
        elif data[4] not in (0, 1):
            length = LIGHTING_COMPACT_RESPONSE_BYTES
        else:
            raise ProtocolError(
                "padded lighting response is ambiguous; preserve the exact transport length")
    raw = bytes(data[:length])
    if raw[:4] != bytes((0x10, LIGHTING_COMMAND, 0, profile)):
        raise ProtocolError("lighting response report, command, status, or profile does not match")
    if sum(raw[2:]) & 0xFF:
        raise ProtocolError("lighting response checksum does not match")
    if length == LIGHTING_EXTENDED_RESPONSE_BYTES:
        if raw[4] not in (0, 1):
            raise ProtocolError("lighting response contains an unknown enable value")
        return LightingState(raw, profile, raw[4], raw[5], raw[6], raw[7], raw[8], tuple(raw[9:12]))
    return LightingState(raw, profile, None, raw[4], raw[5], raw[6], raw[7], tuple(raw[8:11]))


def brightness_from_percent(value: int, *, baseline: LightingState | None = None) -> int:
    """Map editor percent to raw brightness without changing a rounded baseline.

    For example, raw brightness 128 displays as 50%. Leaving 50% untouched
    must preserve 128 rather than treating a rounded UI value as a new setting.
    """
    percent = _integer("brightness percent", value, 0, 100)
    if baseline is not None and baseline.brightness_percent == percent:
        return baseline.brightness_raw
    return (percent * 255 + 50) // 100


def build_lighting_report(state: LightingState, *, effect: str | None = None,
                          brightness_raw: int | None = None, speed: int | None = None,
                          color: Sequence[int] | None = None,
                          led_timeout_raw: int | None = None,
                          enabled: bool | None = None) -> bytes:
    """Encode either supported layout using a fresh, validated baseline.

    The root transaction layer must verify that this baseline still matches
    the mouse, send the packet, correlate the acknowledgement, and read back
    the result. This pure codec never opens a device. The validated write uses
    no outbound checksum. The compact payload ends at byte 10 and the extended
    payload ends at byte 11; every following byte is zero.

    Fields not supplied are preserved, including the 5.09 enable byte and the
    per-profile LED timeout byte.
    Unknown existing effect/speed values can be retained unchanged; selecting
    a new value is restricted to the source-traced MC7 catalog and 1..10 range.
    """
    if not isinstance(state, LightingState):
        raise ProtocolError("a lighting response baseline is required")
    validated = decode_lighting_response(state.raw, state.profile_index)
    if state != validated:
        raise ProtocolError("lighting baseline fields do not match its response bytes")
    shift = int(state.extended_layout)
    payload_end = 11 + shift
    report = bytearray(REPORT_BYTES)
    report[:payload_end] = state.raw[:payload_end]
    report[2:4] = bytes((state.profile_index, 1))
    if enabled is not None:
        if type(enabled) is not bool or not state.extended_layout:
            raise ProtocolError("enabled must be a boolean for an extended lighting record")
        report[4] = int(enabled)
    if effect is not None:
        if not isinstance(effect, str) or effect not in LIGHTING_EFFECT_IDS:
            raise ProtocolError("effect must be one of: " + ", ".join(LIGHTING_EFFECT_IDS))
        report[6 + shift] = LIGHTING_EFFECT_IDS[effect]
    if brightness_raw is not None:
        report[5 + shift] = _integer("brightness_raw", brightness_raw, 0, 255)
    if speed is not None:
        report[7 + shift] = _integer("speed", speed, 1, 10)
    if led_timeout_raw is not None:
        report[4 + shift] = _integer("led_timeout_raw", led_timeout_raw, 0, 30)
    if color is not None:
        if not isinstance(color, Sequence) or isinstance(color, (str, bytes, bytearray)) or len(color) != 3:
            raise ProtocolError("color must contain exactly three RGB channel integers")
        report[8 + shift:11 + shift] = bytes(
            _integer("RGB channel", channel, 0, 255) for channel in color)
    return bytes(report)


def expected_lighting_state(report: bytes, baseline: LightingState) -> LightingState:
    """Produce the expected readback of a report made by this codec.

    This is a prediction for comparison, never evidence of a completed write.
    The baseline supplies the record layout because a zero blue channel makes
    the two padded outbound formats ambiguous from the report alone.
    """
    if not isinstance(report, bytes) or len(report) != REPORT_BYTES:
        raise ProtocolError("lighting write report must contain exactly 64 bytes")
    if not isinstance(baseline, LightingState):
        raise ProtocolError("a lighting response baseline is required")
    checked = decode_lighting_response(baseline.raw, baseline.profile_index)
    if baseline != checked:
        raise ProtocolError("lighting baseline fields do not match its response bytes")
    payload_end = len(baseline.raw) - 1
    if (report[:2] != bytes((0x10, LIGHTING_COMMAND)) or report[2] != baseline.profile_index
            or report[3] != 1 or any(report[payload_end:])):
        raise ProtocolError("unrecognized lighting write report")
    raw = bytearray(report[:payload_end]) + bytearray(1)
    raw[2:4] = bytes((0, baseline.profile_index))
    raw[-1] = (-sum(raw[2:-1])) & 255
    return decode_lighting_response(bytes(raw), baseline.profile_index)
