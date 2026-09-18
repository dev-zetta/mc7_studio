"""Profile/application activation codecs traced from MC7 startup."""

from dataclasses import dataclass

from .protocol import ProtocolError


@dataclass(frozen=True)
class ProfileState:
    raw: bytes
    current_profile: int
    profile_count: int
    energy_saving: bool


def decode_profile_response(data: bytes) -> ProfileState:
    if len(data) not in (6, 64) or data[:3] != b"\x10\x12\0":
        raise ProtocolError("Invalid MC7 profile response")
    if sum(data[2:6]) & 255:
        raise ProtocolError("MC7 profile response checksum does not match")
    if any(data[6:]):
        raise ProtocolError("Unknown extended MC7 profile response")
    count, flags = data[4] & 15, data[4] & 0xF0
    if not 1 <= count <= 5 or data[3] >= count or flags not in (0, 0x10, 0xF0):
        raise ProtocolError("Unknown MC7 profile or application state")
    return ProfileState(bytes(data[:6]), data[3], count, bool(flags))


def build_profile_read_request() -> bytes:
    return bytes.fromhex("101c0012000000") + bytes(57)


def build_application_report(state: ProfileState, active: bool, *, profile_index: int | None = None,
                             energy_saving: bool | None = None) -> bytes:
    if type(active) is not bool:
        raise ProtocolError("Application activity must be a boolean")
    verified = decode_profile_response(state.raw)
    selected = verified.current_profile if profile_index is None else profile_index
    if type(selected) is not int or not 0 <= selected < verified.profile_count:
        raise ProtocolError("Invalid onboard profile")
    eco = verified.energy_saving if energy_saving is None else energy_saving
    if type(eco) is not bool:
        raise ProtocolError("Energy saving must be a boolean")
    profile = selected | (int(active) << 7)
    count = verified.profile_count | (0x10 if eco else 0)
    return bytes((0x10, 0x12, profile, count, (-0x12-profile-count) & 255)) + bytes(59)
