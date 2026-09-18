"""Pure touch decoders for source-mapped MC7 OBS tiles."""

from dataclasses import dataclass

from .protocol import ProtocolError


OBS_LAUNCH_APP_ID = 3400
OBS_LAUNCH_WIRE_TYPE = 0x4B
OBS_LAUNCH_WIRE_SUBTYPE = 0x00
OBS_LAUNCH_EVENT_SELECTOR = 0x01
OBS_SCREENSHOT_APP_ID = 3407
OBS_SCREENSHOT_WIRE_TYPE = 0x43
OBS_SCREENSHOT_WIRE_SUBTYPE = 0x07
OBS_SCREENSHOT_EVENT_SELECTOR = 0x01
OBS_STUDIO_MODE_APP_ID = 3406
OBS_STUDIO_MODE_WIRE_TYPE = 0x43
OBS_STUDIO_MODE_WIRE_SUBTYPE = 0x09
OBS_STUDIO_MODE_EVENT_SELECTOR = 0x01


@dataclass(frozen=True)
class ObsLaunchTouch:
    """One validated press or release in logical LCD coordinates."""

    page_index: int
    slot_index: int
    pressed: bool
    raw: bytes


@dataclass(frozen=True)
class ObsScreenshotTouch:
    """One validated app-3407 press or release in logical coordinates."""

    page_index: int
    slot_index: int
    pressed: bool
    raw: bytes


@dataclass(frozen=True)
class ObsStudioModeTouch:
    """One validated app-3406 press or release in logical coordinates."""

    page_index: int
    slot_index: int
    pressed: bool
    raw: bytes


def decode_obs_launch_touch(data: bytes) -> ObsLaunchTouch | None:
    """Decode exact ``4B/00`` edges and ignore unrelated input reports."""

    if not isinstance(data, (bytes, bytearray)) or len(data) != 8:
        raise ProtocolError("MC7 input event must contain exactly 8 bytes")
    raw = bytes(data)
    if (
        raw[0] != 0x10
        or raw[1] != 0x33
        or raw[3] != OBS_LAUNCH_WIRE_TYPE
        or raw[4] != OBS_LAUNCH_WIRE_SUBTYPE
        or raw[5] != OBS_LAUNCH_EVENT_SELECTOR
        or raw[6] not in (0, 1)
        or raw[7] != 0
    ):
        return None
    page_wire, slot_wire = raw[2] >> 4, raw[2] & 0x0F
    if not 1 <= page_wire <= 3 or not 0 <= slot_wire <= 3:
        raise ProtocolError("Launch OBS touch contains an invalid LCD coordinate")
    return ObsLaunchTouch(
        page_index=page_wire - 1,
        slot_index=3 - slot_wire,
        pressed=bool(raw[6]),
        raw=raw,
    )


def decode_obs_screenshot_touch(data: bytes) -> ObsScreenshotTouch | None:
    """Decode exact app-3407 ``43/07`` edges and ignore other reports."""

    if not isinstance(data, (bytes, bytearray)) or len(data) != 8:
        raise ProtocolError("MC7 input event must contain exactly 8 bytes")
    raw = bytes(data)
    if (
        raw[0] != 0x10
        or raw[1] != 0x33
        or raw[3] != OBS_SCREENSHOT_WIRE_TYPE
        or raw[4] != OBS_SCREENSHOT_WIRE_SUBTYPE
        or raw[5] != OBS_SCREENSHOT_EVENT_SELECTOR
        or raw[6] not in (0, 1)
        or raw[7] != 0
    ):
        return None
    page_wire, slot_wire = raw[2] >> 4, raw[2] & 0x0F
    if not 1 <= page_wire <= 3 or not 0 <= slot_wire <= 3:
        raise ProtocolError(
            "OBS Screenshot touch contains an invalid LCD coordinate"
        )
    return ObsScreenshotTouch(
        page_index=page_wire - 1,
        slot_index=3 - slot_wire,
        pressed=bool(raw[6]),
        raw=raw,
    )


def decode_obs_studio_mode_touch(data: bytes) -> ObsStudioModeTouch | None:
    """Decode exact app-3406 ``43/09`` edges and ignore other reports."""

    if not isinstance(data, (bytes, bytearray)) or len(data) != 8:
        raise ProtocolError("MC7 input event must contain exactly 8 bytes")
    raw = bytes(data)
    if (
        raw[0] != 0x10
        or raw[1] != 0x33
        or raw[3] != OBS_STUDIO_MODE_WIRE_TYPE
        or raw[4] != OBS_STUDIO_MODE_WIRE_SUBTYPE
        or raw[5] != OBS_STUDIO_MODE_EVENT_SELECTOR
        or raw[6] not in (0, 1)
        or raw[7] != 0
    ):
        return None
    page_wire, slot_wire = raw[2] >> 4, raw[2] & 0x0F
    if not 1 <= page_wire <= 3 or not 0 <= slot_wire <= 3:
        raise ProtocolError(
            "OBS Studio Mode touch contains an invalid LCD coordinate"
        )
    return ObsStudioModeTouch(
        page_index=page_wire - 1,
        slot_index=3 - slot_wire,
        pressed=bool(raw[6]),
        raw=raw,
    )
