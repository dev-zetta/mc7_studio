"""MC7 button assignment codecs derived from vendor module 0.0.0.7.

This module performs no device or host input I/O. Unknown assignments remain
opaque and untouched; assigning a macro requires a separate macro upload.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .configuration import (
    Action, CUSTOM_PRECISION_PREFIX, DPI_MAX, DPI_MIN, DPI_STEP,
    parse_custom_precision_dpi,
)
from .protocol import ProtocolError


LAYERS = {"primary": 0x15, "easy_shift": 0x16}
# The vendor SVG has eleven KEY groups. Logical slot 9 is not a visible key.
BUTTON_SLOTS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 11)
BUTTON_LABELS = (
    "Left click", "Right click", "Wheel click", "Wheel up", "Wheel down",
    "Front thumb button", "Rear thumb button", "Top button", "Thumb paddle",
    "Wheel tilt right", "Wheel tilt left",
)

# Values are FunctionType << 8 | FunctionID, from FUN_1801e1380 and its
# immutable 16-bit catalog keys in .rdata at 0x1802f7b68 onward.
ACTION_CODES = {
    ("disabled", ""): 0x0000,
    ("mouse", "left"): 0x0101,
    ("mouse", "right"): 0x0102,
    ("mouse", "middle"): 0x0103,
    ("mouse", "double_click"): 0x0104,
    ("mouse", "forward"): 0x0105,
    ("mouse", "back"): 0x0106,
    ("mouse", "tilt_left"): 0x0107,
    ("mouse", "tilt_right"): 0x0108,
    ("mouse", "scroll_up"): 0x0109,
    ("mouse", "scroll_down"): 0x010A,
    ("dpi", "cycle"): 0x0201,
    ("dpi", "up"): 0x0202,
    ("dpi", "down"): 0x0203,
    ("dpi", "precision"): 0x020D,  # Vendor's fixed Easy-Aim 200 DPI.
    ("media", "previous"): 0x0302,
    ("media", "next"): 0x0303,
    ("media", "play_pause"): 0x0304,
    ("media", "stop"): 0x0305,
    ("media", "mute"): 0x0306,
    ("media", "volume_up"): 0x0307,
    ("media", "volume_down"): 0x0308,
    ("launch", "browser"): 0x0309,
    ("launch", "calculator"): 0x0311,
    ("profile", "cycle"): 0x0801,
    ("profile", "next"): 0x0802,
    ("profile", "previous"): 0x0803,
    ("easy_wheel", "dpi"): 0x0903,
    ("easy_wheel", "volume"): 0x0904,
    ("easy_wheel", "alt_tab"): 0x0905,
    ("easy_wheel", "desktop"): 0x0906,
    ("easy_shift", "hold"): 0x0A01,
    ("easy_shift", "toggle"): 0x0A03,
}
for _slot in range(1, 6):
    ACTION_CODES[("profile", str(_slot))] = 0x0803 + _slot
    ACTION_CODES[("dpi", f"precision_stage_{_slot}")] = 0x0206 + _slot
_ACTIONS_BY_CODE = {code: action for action, code in ACTION_CODES.items()}

# HID keyboard-page usages. The vendor remap table at 0x1803009b0 maps these
# usages to Windows scan codes; FUN_18020f860 uses it to display key names.
KEY_CODES = {chr(ord("A") + index): index + 4 for index in range(26)}
KEY_CODES.update({str(index): index + 29 for index in range(1, 10)})
KEY_CODES.update({"0": 0x27})
KEY_CODES.update({f"F{index}": 0x39 + index for index in range(1, 13)})
KEY_CODES.update({f"F{index}": 0x5B + index for index in range(13, 25)})
KEY_CODES.update({
    "Enter": 0x28, "Escape": 0x29, "Backspace": 0x2A, "Tab": 0x2B,
    "Space": 0x2C, "Minus": 0x2D, "Equal": 0x2E, "BracketLeft": 0x2F,
    "BracketRight": 0x30, "Backslash": 0x31, "Semicolon": 0x33,
    "Quote": 0x34, "Grave": 0x35, "Comma": 0x36, "Period": 0x37,
    "Slash": 0x38, "CapsLock": 0x39, "PrintScreen": 0x46,
    "ScrollLock": 0x47, "Pause": 0x48, "Insert": 0x49, "Home": 0x4A,
    "PageUp": 0x4B, "Delete": 0x4C, "End": 0x4D, "PageDown": 0x4E,
    "Right": 0x4F, "Left": 0x50, "Down": 0x51, "Up": 0x52,
    "NumLock": 0x53, "Num0": 0x62, "Menu": 0x65,
    "Ctrl": 0xE0, "Shift": 0xE1, "Alt": 0xE2, "Meta": 0xE3,
})
KEY_CODES.update({f"Num{index}": 0x58 + index for index in range(1, 10)})
_KEYS_BY_CODE = {code: key for key, code in KEY_CODES.items()}
_KEYS_LOWER = {key.lower(): code for key, code in KEY_CODES.items()}
_KEY_ALIASES = {"esc": "escape", "return": "enter", "control": "ctrl",
                "super": "meta", "cmd": "meta", "command": "meta", "option": "alt",
                "plus": "equal"}
_MODIFIER_BITS = {"ctrl": 1, "shift": 2, "alt": 4, "meta": 8}
_MODIFIER_NAMES = ((1, "Ctrl"), (2, "Shift"), (4, "Alt"), (8, "Meta"))


def _profile(value: int) -> int:
    if type(value) is not int or not 0 <= value <= 4:
        raise ProtocolError("Profile index must be an integer from 0 to 4")
    return value


def _layer(value: str) -> int:
    if not isinstance(value, str) or value not in LAYERS:
        raise ProtocolError("Button layer must be primary or easy_shift")
    return LAYERS[value]


def _record(value: bytes) -> bytes:
    if not isinstance(value, (bytes, bytearray)) or len(value) != 4:
        raise ProtocolError("A button assignment must contain exactly four bytes")
    return bytes(value)


@dataclass(frozen=True)
class ButtonState:
    raw: bytes
    profile_index: int
    layer: str
    records: tuple[bytes, ...]

    @property
    def signature(self) -> tuple:
        """Established assignment fields, excluding response-only padding."""
        return self.profile_index, self.layer, self.records


def build_button_read_request(profile_index: int, layer: str = "primary") -> bytes:
    """Vendor FUN_180266720's seven-byte selector, padded to a HID report."""
    return bytes((0x10, 0x1C, 0, _layer(layer), _profile(profile_index), 0, 0)) + bytes(57)


def build_button_get_buffer(layer: str = "primary") -> bytes:
    return bytes((0x10, _layer(layer))) + bytes(62)


def decode_button_response(data: bytes, profile_index: int, layer: str = "primary") -> ButtonState:
    """Decode the full 64-byte response observed on the connected MC7.

    The vendor retains only 53 bytes of this response internally. Accepting a
    truncated 53-byte native transfer would hide the actual transfer length.
    """
    profile = _profile(profile_index)
    command = _layer(layer)
    if not isinstance(data, (bytes, bytearray)) or len(data) != 64:
        raise ProtocolError("Button response must contain exactly 64 bytes")
    if data[:4] != bytes((0x10, command, 0, profile)):
        raise ProtocolError("Button response report, layer, status, or profile does not match")
    raw = bytes(data)
    return ButtonState(raw, profile, layer, tuple(raw[4 + index * 4:8 + index * 4] for index in range(12)))


def _key_code(name: str) -> int:
    lowered = _KEY_ALIASES.get(name.lower(), name.lower())
    try:
        return _KEYS_LOWER[lowered]
    except KeyError as error:
        raise ProtocolError(f"Unsupported keyboard key: {name}") from error


def encode_action(action: Action) -> bytes:
    """Compile a supported action to Data0, Data1, FunctionID, FunctionType."""
    if not isinstance(action, Action) or not isinstance(action.kind, str) or not isinstance(action.value, str):
        raise ProtocolError("Choose a supported button action")
    code = ACTION_CODES.get((action.kind, action.value))
    if code is not None:
        return bytes((0, 0, code & 0xFF, code >> 8))
    if action.kind == "dpi" and action.value.startswith(CUSTOM_PRECISION_PREFIX):
        dpi = parse_custom_precision_dpi(action.value)
        if dpi is None:
            raise ProtocolError(
                f"Custom Easy-Aim DPI must be from {DPI_MIN} to {DPI_MAX} "
                f"in steps of {DPI_STEP}")
        # FUN_180211d30 writes the zero-based 50-DPI step index high byte
        # first, followed by the 020c action code in little-endian order.
        encoded = dpi // DPI_STEP - 1
        return bytes((encoded >> 8, encoded & 0xFF, 0x0C, 0x02))
    if action.kind != "keyboard":
        raise ProtocolError(f"The {action.kind} action {action.value!r} cannot yet be written to the mouse")
    if len(action.value) > 128:
        raise ProtocolError("Keyboard shortcut is too long")
    tokens = action.value.split("+")
    if not 1 <= len(tokens) <= 5 or any(not token for token in tokens):
        raise ProtocolError("Use a key or a shortcut such as Ctrl+Shift+S")
    key = _key_code(tokens[-1])
    if len(tokens) == 1:
        return bytes((0, 0, key, 0x0C))
    modifier_mask = 0
    for token in tokens[:-1]:
        modifier = _KEY_ALIASES.get(token.lower(), token.lower())
        bit = _MODIFIER_BITS.get(modifier)
        if bit is None or modifier_mask & bit:
            raise ProtocolError("Use each shortcut modifier at most once, followed by one key")
        modifier_mask |= bit
    if key >= 0xE0:
        raise ProtocolError("A shortcut must end with a non-modifier key")
    return bytes((0, key, modifier_mask, 6))


def decode_action(record: bytes) -> Action | None:
    """Return None for opaque assignments; callers must preserve those bytes."""
    data0, data1, function_id, function_type = _record(record)
    code = function_type << 8 | function_id
    if code == 0x020C:
        # FUN_1802103d0 reconstructs this as (big_endian_index + 1) * 50.
        dpi = (((data0 << 8) | data1) + 1) * DPI_STEP
        if DPI_MIN <= dpi <= DPI_MAX:
            return Action("dpi", f"{CUSTOM_PRECISION_PREFIX}{dpi}")
        return None
    # Data bytes may carry semantics not represented by the ordinary action.
    if data0:
        return None
    if not data1 and code in _ACTIONS_BY_CODE:
        return Action(*_ACTIONS_BY_CODE[code])
    if function_type == 0x0C and not data1 and function_id in _KEYS_BY_CODE:
        return Action("keyboard", _KEYS_BY_CODE[function_id])
    standard_keys = ("Insert", "Delete", "Home", "End", "PageUp", "PageDown", "Ctrl", "Shift", "Alt", "Meta", "CapsLock")
    if function_type == 4 and not data1 and 1 <= function_id <= len(standard_keys):
        return Action("keyboard", standard_keys[function_id - 1])
    if function_type == 6 and not function_id & 0xF0 and data1 in _KEYS_BY_CODE and data1 < 0xE0:
        modifiers = [name for bit, name in _MODIFIER_NAMES if function_id & bit]
        return Action("keyboard", "+".join([*modifiers, _KEYS_BY_CODE[data1]]))
    return None


def replace_actions(state: ButtonState, assignments: Mapping[int, Action]) -> tuple[bytes, ...]:
    """Change explicitly selected visible logical slots, preserving all others."""
    if not isinstance(state, ButtonState):
        raise ProtocolError("Read the mouse's button assignments before editing them")
    verified = decode_button_response(state.raw, state.profile_index, state.layer)
    if verified.records != state.records:
        raise ProtocolError("Button baseline records do not match their original response")
    if not isinstance(assignments, Mapping) or len(assignments) > len(BUTTON_SLOTS):
        raise ProtocolError("Provide at most eleven explicit button changes")
    records = list(state.records)
    for logical_slot, action in assignments.items():
        if type(logical_slot) is not int or logical_slot not in BUTTON_SLOTS:
            raise ProtocolError("That logical button slot is not present in the MC7 button diagram")
        # A decoded action can have an alternate wire representation. Preserve
        # it exactly when the editor did not change the semantic assignment.
        if decode_action(records[logical_slot]) != action:
            records[logical_slot] = encode_action(action)
    if state.layer == "primary":
        for needed in (Action("mouse", "left"), Action("mouse", "right")):
            if not any(decode_action(records[slot]) == needed for slot in BUTTON_SLOTS):
                raise ProtocolError("Keep reachable left and right click assignments in the primary layer")
    return tuple(records)


def build_button_report(state: ButtonState, assignments: Mapping[int, Action]) -> bytes:
    """Build vendor 0x15/0x16 write, preserving twelve four-byte records.

    FUN_180277830 sends 52 bytes (three-byte write header, 48 record bytes,
    trailing zero). FUN_180276e50 pads to 64 without changing the payload.
    Response padding/check bytes do not belong in this different write layout.
    """
    records = replace_actions(state, assignments)
    return bytes((0x10, _layer(state.layer), state.profile_index)) + b"".join(records) + bytes(13)
