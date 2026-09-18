"""Pure touch and value logic for Swarm II media-volume app 1025.

The recovered ``COMMAND_MC7.dll`` callback ``FUN_18028ac80`` is registered
separately for app IDs 1024 and 1025.  Its app-1025 branch uses touch subtypes
0, 1 and 2 for minus two, plus two and reset-to-one, then clamps the selected
audio session's volume to 0..100.  The setter key at ``DAT_180d88e90`` is
initialized from ``AUDIO_SESSION_VOL_``.

The app-1025 LCD storage signature has not been recovered.  Consequently this
module requires the dispatcher's app ID, preserves the event's main-type byte
without interpreting it, and intentionally provides no layout or report
builder.  Its strict outer event envelope follows the shared MC7 touch router;
it is a safety constraint rather than an observed app-1025 report.  A future
device integration must establish the missing mapping separately.
"""

from dataclasses import dataclass
from enum import Enum

from .protocol import ProtocolError


MEDIA_VOLUME_APP_ID = 1025
MEDIA_VOLUME_EVENT_SELECTOR = 0x01
MEDIA_VOLUME_MINIMUM = 0
MEDIA_VOLUME_MAXIMUM = 100
MEDIA_VOLUME_STEP = 2


class MediaVolumeAction(str, Enum):
    """Actions implemented by the original app-1025 touch callback."""

    DECREASE = "decrease"
    INCREASE = "increase"
    SET_ONE = "set_one"


_ACTIONS_BY_SUBTYPE = {
    0: MediaVolumeAction.DECREASE,
    1: MediaVolumeAction.INCREASE,
    2: MediaVolumeAction.SET_ONE,
}


@dataclass(frozen=True)
class MediaVolumePress:
    """One app-1025 press with unresolved wire identity kept explicit.

    ``wire_slot`` is the event's low coordinate nibble.  It is not converted
    to a logical slot because no app-1025 layout mapping has been established.
    """

    page_index: int
    wire_slot: int
    main_type: int
    event_type: int
    action: MediaVolumeAction
    raw: bytes


def _volume(value: int) -> int:
    if (type(value) is not int
            or not MEDIA_VOLUME_MINIMUM <= value <= MEDIA_VOLUME_MAXIMUM):
        raise ProtocolError("Media volume must be an integer in 0..100")
    return value


def apply_media_volume_action(
    current_volume: int, action: MediaVolumeAction
) -> int:
    """Return the source-exact app-1025 volume transition.

    At ``COMMAND_MC7.dll`` address ``0x18028b5cd`` the callback reads the
    selected audio-session volume.  Subtypes 0/1 add -2/+2 and subtype 2 sets
    one; the instructions at ``0x18028b607`` then clamp the result to 0..100.
    """
    current = _volume(current_volume)
    if not isinstance(action, MediaVolumeAction):
        raise ProtocolError("Media volume action must be a MediaVolumeAction")
    if action is MediaVolumeAction.DECREASE:
        candidate = current - MEDIA_VOLUME_STEP
    elif action is MediaVolumeAction.INCREASE:
        candidate = current + MEDIA_VOLUME_STEP
    else:
        candidate = 1
    return max(MEDIA_VOLUME_MINIMUM, min(MEDIA_VOLUME_MAXIMUM, candidate))


def decode_media_volume_press(
    data: bytes, *, app_id: int
) -> MediaVolumePress | None:
    """Decode an app-1025 press and ignore releases or unrelated events.

    The original dispatcher supplies the app ID separately from the event.
    Requiring it here prevents this decoder from claiming a subtype belonging
    to another tile while app 1025's main-type layout byte remains unknown.
    The ``10 33`` prefix, selector 1 and zero trailer are strict constraints
    from the shared MC7 touch router; no app-1025 event has been observed.
    Bytes 3 and 5 are preserved as ``main_type`` and ``event_type``; the
    recovered callback reads both but branches only on subtype byte 4 and
    press-status byte 6.
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) != 8:
        raise ProtocolError("MC7 input event must contain exactly 8 bytes")
    if type(app_id) is not int or not 0 <= app_id <= 0xFFFF:
        raise ProtocolError("MC7 application ID must be a 16-bit integer")
    raw = bytes(data)
    if app_id != MEDIA_VOLUME_APP_ID:
        return None
    if (raw[0] != 0x10 or raw[1] != 0x33
            or raw[5] != MEDIA_VOLUME_EVENT_SELECTOR
            or raw[6] != 1 or raw[7] != 0):
        return None
    action = _ACTIONS_BY_SUBTYPE.get(raw[4])
    if action is None:
        return None
    page_wire, wire_slot = raw[2] >> 4, raw[2] & 0x0F
    if not 1 <= page_wire <= 3 or not 0 <= wire_slot <= 3:
        raise ProtocolError("Media-volume press contains an invalid LCD coordinate")
    return MediaVolumePress(
        page_index=page_wire - 1,
        wire_slot=wire_slot,
        main_type=raw[3],
        event_type=raw[5],
        action=action,
        raw=raw,
    )
