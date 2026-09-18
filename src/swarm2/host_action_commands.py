"""Strict decoder for host-owned MC7 LCD touch actions."""

from dataclasses import dataclass

from .lcd_commands import LCD_HOST_ACTION_WIDGETS, LCD_WIDGETS
from .protocol import ProtocolError


_BY_SIGNATURE = {
    LCD_WIDGETS[key].signature: key for key in LCD_HOST_ACTION_WIDGETS
}


@dataclass(frozen=True)
class HostActionTouch:
    widget_key: str
    page_index: int
    slot_index: int
    pressed: bool
    raw: bytes


def decode_host_action_touch(data: bytes) -> HostActionTouch | None:
    """Decode a supported press/release, returning ``None`` for another event."""
    if not isinstance(data, (bytes, bytearray)) or len(data) != 8:
        raise ProtocolError("MC7 input event must contain exactly 8 bytes")
    raw = bytes(data)
    if (raw[0] != 0x10 or raw[1] != 0x33
            or raw[5] != 0x01 or raw[7] != 0x00):
        return None
    widget_key = _BY_SIGNATURE.get((raw[3], raw[4]))
    if widget_key is None or raw[6] not in (0, 1):
        return None
    page_wire, slot_wire = raw[2] >> 4, raw[2] & 0x0F
    if not 1 <= page_wire <= 3 or not 0 <= slot_wire <= 3:
        raise ProtocolError("Host-action touch contains an invalid LCD coordinate")
    return HostActionTouch(
        widget_key, page_wire - 1, 3 - slot_wire, bool(raw[6]), raw)


def decode_host_action_press(data: bytes) -> HostActionTouch | None:
    """Return only a supported press edge; releases and other events are ignored."""
    touch = decode_host_action_touch(data)
    return touch if touch is not None and touch.pressed else None
