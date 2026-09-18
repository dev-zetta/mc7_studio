"""Read-only MC7 mouse firmware and battery feature-report codecs."""

from dataclasses import dataclass
import re

from .protocol import ProtocolError, REPORT_BYTES

STATUS_SELECTOR = 0x09
STATUS_RESPONSE_BYTES = 12


@dataclass(frozen=True)
class DeviceStatus:
    raw: bytes
    firmware_major: int
    firmware_minor: int
    battery_percent: int | None
    charging: bool | None

    @property
    def firmware_version(self) -> str:
        return f"{self.firmware_major}.{self.firmware_minor:02d}"

    @property
    def firmware_numeric(self) -> int:
        """Vendor role-specific fw_version integer, distinct from CFU versions."""
        return self.firmware_major * 100 + self.firmware_minor

    @property
    def firmware_catalog_version(self) -> str:
        """The manifest's four-component spelling of this mouse version."""
        return f"{self.firmware_major}.{self.firmware_minor}.0.0"

    @property
    def role(self) -> str:
        # This decoder only accepts selector09, the source's mouse key0xB7.
        return "mouse"

    @property
    def charging_raw(self) -> int:
        return self.raw[10]


def build_status_read_request() -> bytes:
    """Select wired mouse info; this does not enter any firmware-update mode."""
    return bytes((0x10, 0x1C, 0, STATUS_SELECTOR, 0, 0, 0)) + bytes(REPORT_BYTES - 7)


def build_status_get_buffer() -> bytes:
    return bytes((0x10, STATUS_SELECTOR)) + bytes(REPORT_BYTES - 2)


def _bcd(value: int) -> int:
    high, low = value >> 4, value & 15
    if high > 9 or low > 9:
        raise ProtocolError("Mouse firmware version contains invalid BCD digits")
    return high * 10 + low


def decode_status_response(data: bytes) -> DeviceStatus:
    """Decode the 12-byte USB response; preserve unknown fields and sentinels.

    Version components are packed decimal digits (BCD), minor first. Unknown
    battery/charging values remain available in raw and are displayed as unknown.
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) != STATUS_RESPONSE_BYTES:
        raise ProtocolError("Mouse status response must contain exactly 12 bytes")
    raw = bytes(data)
    if raw[:3] != bytes((0x10, STATUS_SELECTOR, 0)):
        raise ProtocolError("Mouse status report, selector or status does not match")
    if sum(raw[2:]) & 255:
        raise ProtocolError("Mouse status response checksum does not match")
    battery = raw[9] if raw[9] <= 100 else None
    charging = bool(raw[10]) if raw[10] in (0, 1) else None
    return DeviceStatus(raw, _bcd(raw[4]), _bcd(raw[3]), battery, charging)


def compare_catalog_version(status: DeviceStatus, version: str, *, role: str,
                            product_id: int) -> int:
    """Return -1/0/1 when installed firmware is older/equal/newer.

    Only the source-established mouse manifest mapping is accepted. Receiver
    packages and nonzero trailing components must not be compared to selector09
    status. CFU offer versions have their own format and are not accepted here.
    """
    if (not isinstance(status, DeviceStatus)
            or decode_status_response(status.raw) != status):
        raise ProtocolError("A verified mouse status response is required for version comparison")
    if role != "mouse" or type(product_id) is not int or product_id != 0x502C:
        raise ProtocolError("Mouse firmware can only be compared with an MC7 mouse package")
    if (not isinstance(version, str)
            or not re.fullmatch(r"(?:0|[1-9][0-9]?)\.(?:0|[1-9][0-9]?)\.0\.0", version)):
        raise ProtocolError("Unsupported mouse firmware catalog version; expected major.minor.0.0")
    major, minor, _, _ = map(int, version.split("."))
    candidate = major * 100 + minor
    return (status.firmware_numeric > candidate) - (status.firmware_numeric < candidate)
