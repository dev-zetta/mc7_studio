"""MC7 advanced setting codecs. No device access or implicit defaults.

Layouts follow COMMAND_MC7.dll 0.0.0.7; see advanced-sensor-protocol.md for
addresses, paired fields, and the separate status of hardware validation.
"""

from dataclasses import dataclass

from .protocol import ProtocolError, REPORT_BYTES, decode_sensor_response

POLLING_RATES = (1000, 2000, 4000, 8000, 125, 250, 500)
SCREEN_BRIGHTNESS_LEVELS = (20, 40, 60, 80, 100)
SCREEN_TIMEOUT_VALUES = (0, 1, 2, 3, 4, 5, 10, 15, 20, 25, 30)
SETTING_RESPONSE_LENGTHS = {0x05: 5, 0x1A: 6, 0x24: 5, 0x26: 5, 0x2B: 6}


def _integer(name: str, value: int, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProtocolError(f"{name} must be an integer in {minimum}..{maximum}")
    return value


def _boolean(name: str, value: bool) -> int:
    if type(value) is not bool:
        raise ProtocolError(f"{name} must be a boolean")
    return int(value)


def _profile(value: int) -> int:
    return _integer("profile_index", value, 0, 4)


def _report(*values: int) -> bytes:
    return bytes(values) + bytes(REPORT_BYTES - len(values))


def _selector(value: int) -> int:
    _integer("selector", value, 0, 255)
    if value not in SETTING_RESPONSE_LENGTHS:
        raise ProtocolError("Unsupported global setting selector")
    return value


def build_setting_read_request(selector: int) -> bytes:
    """Select a global setting, then await its 0x1c acknowledgement before GET."""
    return _report(0x10, 0x1C, 0, _selector(selector), 0, 0, 0)


def build_setting_get_buffer(selector: int) -> bytes:
    """Initial feature-GET buffer, not another feature SET message."""
    return _report(0x10, _selector(selector))


def _response(data: bytes, selector: int) -> bytes:
    expected = SETTING_RESPONSE_LENGTHS[selector]
    if not isinstance(data, (bytes, bytearray)) or len(data) != expected:
        raise ProtocolError(f"Setting 0x{selector:02x} response must contain {expected} bytes")
    if data[:3] != bytes((0x10, selector, 0)):
        raise ProtocolError("Setting response report, selector, or status does not match")
    return bytes(data)


@dataclass(frozen=True)
class AdvancedSensorState:
    raw: bytes
    profile_index: int
    polling_rate_usb: int
    polling_rate_wireless: int
    angle_snapping: bool
    angle_tuning: int
    angle_tuning_enabled: bool
    motion_sync: bool
    lift_off_raw: int

    @property
    def polling_signature(self) -> tuple:
        """USB command 0x10 fields; wireless polling is a separate read value."""
        return self.profile_index, self.polling_rate_usb, self.motion_sync

    @property
    def angle_signature(self) -> tuple:
        return (self.profile_index, self.angle_snapping, self.angle_tuning,
                self.angle_tuning_enabled)


def decode_advanced_sensor_response(data: bytes, profile_index: int) -> AdvancedSensorState:
    state = decode_sensor_response(data, profile_index)
    raw = state.raw
    if raw[4] >= len(POLLING_RATES) or raw[5] >= len(POLLING_RATES):
        raise ProtocolError("Sensor response contains an unknown polling-rate enum")
    if any(raw[offset] not in (0, 1) for offset in (6, 8, 46)):
        raise ProtocolError("Sensor response contains unknown angle or Motion Sync flags")
    angle = int.from_bytes(raw[7:8], "little", signed=True)
    _integer("angle_tuning", angle, -30, 30)
    # Calibration states are deliberately preserved; never relabel them Low.
    return AdvancedSensorState(raw, state.profile_index, POLLING_RATES[raw[4]],
                               POLLING_RATES[raw[5]], bool(raw[6]), angle,
                               bool(raw[8]), bool(raw[46]), raw[45])


def build_polling_report(*, profile_index: int, polling_rate: int, motion_sync: bool) -> bytes:
    """Polling and Motion Sync share one write; supply both intended values."""
    _integer("polling_rate", polling_rate, 125, 8000)
    if polling_rate not in POLLING_RATES:
        raise ProtocolError("Unsupported MC7 polling rate")
    packed = POLLING_RATES.index(polling_rate) | (_boolean("motion_sync", motion_sync) << 4)
    return _report(0x10, 0x10, _profile(profile_index), packed, 0)


def build_angle_report(*, profile_index: int, angle_snapping: bool,
                       angle_tuning: int, angle_tuning_enabled: bool) -> bytes:
    profile = _profile(profile_index)
    if not _boolean("angle_tuning_enabled", angle_tuning_enabled):
        profile |= 0xA0
    angle = _integer("angle_tuning", angle_tuning, -30, 30) & 0xFF
    return _report(0x10, 0x11, profile, angle, _boolean("angle_snapping", angle_snapping), 0)


def build_lift_off_reports(level: str) -> tuple[bytes, bytes]:
    """Global DCU reset then preset; acknowledge both separately, then reread.

    Custom calibration has additional states and is intentionally not encoded.
    """
    choices = {"very_low": 0, "low": 0x85}
    if not isinstance(level, str) or level not in choices:
        raise ProtocolError("Lift-off preset must be 'very_low' or 'low'")
    return _report(0x10, 0x19, 0x93, 0), _report(0x10, 0x19, choices[level], 0)


@dataclass(frozen=True)
class DebounceState:
    raw: bytes
    debounce_ms: int
    secondary_debounce_ms: int

    @property
    def signature(self) -> tuple[int, int]:
        return self.debounce_ms, self.secondary_debounce_ms


def decode_debounce_response(data: bytes) -> DebounceState:
    raw = _response(data, 0x1A)
    return DebounceState(raw, _integer("debounce", raw[3], 0, 10),
                         _integer("secondary debounce", raw[4], 0, 10))


def build_debounce_report(*, debounce_ms: int) -> bytes:
    """Vendor UI setter updates both debounce fields to the same value."""
    value = _integer("debounce", debounce_ms, 0, 10)
    return _report(0x10, 0x1A, value, value, 0)


@dataclass(frozen=True)
class HapticState:
    raw: bytes
    intensity: int


def decode_haptic_response(data: bytes) -> HapticState:
    raw = _response(data, 0x24)
    return HapticState(raw, _integer("haptic intensity", raw[3], 0, 3))


def build_haptic_report(*, intensity: int) -> bytes:
    return _report(0x10, 0x24, _integer("haptic intensity", intensity, 0, 3), 0)


@dataclass(frozen=True)
class StandbyState:
    raw: bytes
    standby_value: int


def decode_standby_response(data: bytes) -> StandbyState:
    raw = _response(data, 0x05)
    return StandbyState(raw, _integer("standby value", raw[3], 1, 30))


def build_standby_report(*, standby_value: int) -> bytes:
    """Raw vendor menu value 1..30; no unverified time-unit conversion."""
    return _report(0x10, 0x05, _integer("standby value", standby_value, 1, 30), 0)


@dataclass(frozen=True)
class EcoState:
    raw: bytes
    enabled: bool


def decode_eco_response(data: bytes) -> EcoState:
    raw = _response(data, 0x26)
    if raw[3] not in (0, 1):
        raise ProtocolError("Unknown ECO enable value")
    return EcoState(raw, bool(raw[3]))


def build_eco_report(*, enabled: bool) -> bytes:
    return _report(0x10, 0x26, _boolean("enabled", enabled), 0)


@dataclass(frozen=True)
class ScreenState:
    raw: bytes
    brightness: int
    timeout_value: int

    @property
    def signature(self) -> tuple[int, int]:
        return self.brightness, self.timeout_value


def decode_screen_response(data: bytes) -> ScreenState:
    raw = _response(data, 0x2B)
    return ScreenState(raw, _integer("brightness", raw[3], 0, 100),
                       _integer("timeout value", raw[4], 0, 30))


def build_screen_report(*, brightness: int, timeout_value: int) -> bytes:
    """Global screen settings; MC7 brightness uses five levels, 20..100."""
    _integer("brightness", brightness, 20, 100)
    if brightness not in SCREEN_BRIGHTNESS_LEVELS:
        raise ProtocolError("Screen brightness must be 20, 40, 60, 80 or 100")
    _integer("timeout value", timeout_value, 0, 30)
    if timeout_value not in SCREEN_TIMEOUT_VALUES:
        raise ProtocolError("Unsupported MC7 screen timeout value")
    return _report(0x10, 0x2B, brightness, timeout_value, 0)
