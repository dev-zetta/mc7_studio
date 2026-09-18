"""Pure touch and live-state codecs for the MC7 General Media LCD tile."""

from dataclasses import dataclass
from enum import Enum

from .protocol import ProtocolError, REPORT_BYTES


GENERAL_MEDIA_WIRE_TYPE = 0x45
GENERAL_MEDIA_EVENT_SELECTOR = 0x01
GENERAL_MEDIA_RECORD_COMMAND = 0x0F


class GeneralMediaAction(str, Enum):
    """Actions exposed by the original app-1024 General Media panel."""

    SHUFFLE = "shuffle"
    NEXT = "next"
    PLAY_PAUSE = "play_pause"
    PREVIOUS = "previous"
    REPEAT = "repeat"


class GeneralMediaSlotForm(str, Enum):
    """Original caller conventions for byte 5 of an A3/0F update.

    Swarm II's initial and plugin-event scans pass their zero-based slot-array
    index directly.  Its immediate touch refresh passes the touch report's low
    nibble, which is the reversed slot coordinate.  Firmware 5.09 ignores this
    field for record command 0F, but both source-observed forms stay explicit.
    """

    SCAN_INDEX = "scan_index"
    TOUCH_COORDINATE = "touch_coordinate"


_ACTIONS_BY_SUBTYPE = {
    0: GeneralMediaAction.SHUFFLE,
    1: GeneralMediaAction.NEXT,
    2: GeneralMediaAction.PLAY_PAUSE,
    3: GeneralMediaAction.PREVIOUS,
    4: GeneralMediaAction.REPEAT,
}


@dataclass(frozen=True)
class GeneralMediaPress:
    """One validated app-1024 press in logical LCD coordinates."""

    page_index: int
    logical_slot: int
    action: GeneralMediaAction
    raw: bytes


@dataclass(frozen=True)
class GeneralMediaDisplayState:
    """The three boolean flags rendered by firmware record command 0F."""

    repeat_active: bool
    playing: bool
    shuffle_active: bool

    def __post_init__(self) -> None:
        for name, value in (
            ("repeat_active", self.repeat_active),
            ("playing", self.playing),
            ("shuffle_active", self.shuffle_active),
        ):
            if type(value) is not bool:
                raise ProtocolError(f"{name} must be a boolean")


def _lcd_coordinate(name: str, value: int, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ProtocolError(f"{name} must be an integer in 0..{maximum}")
    return value


def build_general_media_report(
    state: GeneralMediaDisplayState,
    *,
    page_index: int,
    logical_slot: int,
    slot_form: GeneralMediaSlotForm = GeneralMediaSlotForm.TOUCH_COORDINATE,
) -> bytes:
    """Build the original zero-padded 64-byte ``A3/0F`` live-state report.

    ``TOUCH_COORDINATE`` encodes byte 5 as ``3 - logical_slot`` and matches the
    immediate-touch caller as well as other A3 widgets. ``SCAN_INDEX`` encodes
    byte 5 as ``logical_slot`` and reproduces the original activation/event
    scan callers. The recovered firmware consumes the flags at offsets 7, 9
    and 12 and ignores page/slot for record command 0F.
    """
    if not isinstance(state, GeneralMediaDisplayState):
        raise ProtocolError("state must be a GeneralMediaDisplayState")
    state.__post_init__()
    page = _lcd_coordinate("page_index", page_index, 2)
    slot = _lcd_coordinate("logical_slot", logical_slot, 3)
    if not isinstance(slot_form, GeneralMediaSlotForm):
        raise ProtocolError("slot_form must be a GeneralMediaSlotForm")
    slot_byte = slot if slot_form is GeneralMediaSlotForm.SCAN_INDEX else 3 - slot
    record = bytes((
        page + 1,
        slot_byte,
        GENERAL_MEDIA_RECORD_COMMAND,
        int(state.repeat_active),
        0,
        int(state.playing),
        0,
        0,
        int(state.shuffle_active),
    ))
    packet = bytes((0x10, 0xA3, 0, 1)) + record
    return packet + bytes(REPORT_BYTES - len(packet))


def decode_general_media_press(data: bytes) -> GeneralMediaPress | None:
    """Decode one exact app-1024 press; ignore releases and other events.

    The event envelope is ``10 33 PP 45 SS 01 ST 00``. ``PP`` contains a
    one-based page in its high nibble and a reversed LCD slot in its low
    nibble. ``ST`` is one for a press and zero for its release.
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) != 8:
        raise ProtocolError("MC7 input event must contain exactly 8 bytes")
    raw = bytes(data)
    if (raw[0] != 0x10 or raw[1] != 0x33
            or raw[3] != GENERAL_MEDIA_WIRE_TYPE
            or raw[5] != GENERAL_MEDIA_EVENT_SELECTOR
            or raw[6] != 1 or raw[7] != 0):
        return None
    action = _ACTIONS_BY_SUBTYPE.get(raw[4])
    if action is None:
        return None
    page_wire, slot_wire = raw[2] >> 4, raw[2] & 0x0F
    if not 1 <= page_wire <= 3 or not 0 <= slot_wire <= 3:
        raise ProtocolError("General Media press contains an invalid LCD coordinate")
    return GeneralMediaPress(page_wire - 1, 3 - slot_wire, action, raw)
