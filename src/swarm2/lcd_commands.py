"""MC7 onboard LCD page codecs, derived from the vendor's read/write paths.

No device access. A fresh response is required for every write so reserved
fields and the two additional, unexposed page records survive an edit.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from .protocol import ProtocolError, REPORT_BYTES

LCD_RESPONSE_BYTES = 61
LCD_EDITABLE_PAGES = 3
LCD_SLOTS_PER_PAGE = 4
LCD_GENERAL_MEDIA_APP_ID = 1024
LCD_LAUNCH_OBS_APP_ID = 3400
_WIDE_TYPES = frozenset((0x01, 0x45, 0x65))
LCD_KEY_WIDGETS = frozenset(("remap_key", "hotkey"))
LCD_MACRO_WIDGETS = frozenset(("macro",))
LCD_TIMER_WIDGETS = frozenset(("countdown",))
LCD_HOST_ACTION_WIDGETS = frozenset((
    "open_application", "open_website", "open_file", "open_folder",
))

# Swarm's onboard menu catalog IDs are not the two-byte records stored in an
# LCD layout.  These six one-cell shortcuts need no separate key/macro record.
LCD_ONBOARD_SHORTCUT_APP_IDS = MappingProxyType({
    "emoji": 31,
    "launch_browser": 34,
    "browser_back": 35,
    "browser_forward": 36,
    "calculator": 37,
    "screenshot": 26,
})
LCD_ONBOARD_SHORTCUT_WIDGETS = frozenset(LCD_ONBOARD_SHORTCUT_APP_IDS)


@dataclass(frozen=True)
class LcdWidget:
    key: str
    label: str
    wire_type: int
    wire_subtype: int = 0

    @property
    def width(self) -> int:
        return 3 if self.wire_type in _WIDE_TYPES else 1

    @property
    def signature(self) -> tuple[int, int]:
        return self.wire_type, self.wire_subtype


# App IDs and wire types are distinct. These mappings are from 0x180253870;
# labels come from touch_menu_onboard.json. No plugin/image upload is encoded.
LCD_WIDGETS = MappingProxyType({widget.key: widget for widget in (
    LcdWidget("empty", "Empty", 0xFE),
    LcdWidget("download_swarm", "Download Swarm II", 0x01),
    LcdWidget("remap_key", "Remap key", 0x05, 0),
    LcdWidget("hotkey", "Keyboard shortcut", 0x05, 1),
    LcdWidget("macro", "Macro", 0x05, 2),
    # These one-cell tiles keep their target on the host. Their command-0x29
    # trigger records are encoded separately in screen_key_commands.py.
    LcdWidget("open_application", "Open application", 0x05, 3),
    LcdWidget("open_folder", "Open folder", 0x05, 4),
    LcdWidget("open_website", "Open website", 0x05, 5),
    LcdWidget("open_file", "Open file", 0x05, 6),
    LcdWidget("countdown", "Count down timer", 0x46, 0),
    LcdWidget("dpi", "DPI", 0x64),
    LcdWidget("gpu_temperature", "GPU temperature (live)", 0x37),
    LcdWidget("gpu_load", "GPU usage (live)", 0x38),
    LcdWidget("cpu_temperature", "CPU temperature (live)", 0x39),
    LcdWidget("cpu_load", "CPU usage (live)", 0x3A),
    LcdWidget("ram_usage", "RAM usage (live)", 0x41),
    LcdWidget("led_brightness", "LED brightness", 0x63),
    LcdWidget("polling_rate", "Polling rate", 0x66),
    # Original Swarm II app 1024. Its persisted mapping and five touch
    # subactions are known; live track/state rendering is a separate path.
    LcdWidget("general_media", "General media controls", 0x45),
    # Original third-party app 3400. It is a static command-0x25 tile; the
    # guarded host listener launches OBS and no command-0x29 record is stored.
    LcdWidget("launch_obs", "Launch OBS", 0x4B, 0),
    # Original OBS plugin app 3407. Its host listener sends the source-mapped
    # OBSBasic.Screenshot hotkey over the local OBS WebSocket server.
    LcdWidget("obs_screenshot", "OBS screenshot", 0x43, 0x07),
    # Original OBS plugin app 3406. The host listener toggles and verifies the
    # OBS state; transient state rendering is intentionally a separate path.
    LcdWidget("obs_studio_mode", "OBS Studio mode", 0x43, 0x09),
    LcdWidget("system_media", "System media controls", 0x65),
    LcdWidget("play_pause", "Play / pause", 0x60, 0),
    LcdWidget("next_track", "Next track", 0x60, 1),
    LcdWidget("previous_track", "Previous track", 0x60, 2),
    LcdWidget("stop", "Stop", 0x60, 3),
    LcdWidget("speaker_mute", "Speaker mute", 0x61),
    LcdWidget("shuffle", "Shuffle", 0x4F),
    LcdWidget("repeat", "Repeat", 0x51),
    LcdWidget("volume_mute", "Volume mute", 0x48),
    LcdWidget("cut", "Cut", 0x47),
    LcdWidget("copy", "Copy", 0x19),
    LcdWidget("paste", "Paste", 0x1A),
    LcdWidget("undo", "Undo", 0x1F),
    LcdWidget("redo", "Redo", 0x20),
    LcdWidget("game_bar", "Game Bar (Windows)", 0x15),
    LcdWidget("record_last_30_seconds", "Record last 30 seconds (Windows)", 0x16),
    LcdWidget("game_bar_screenshot", "Game Bar screenshot (Windows)", 0x17),
    LcdWidget("emoji", "Emoji", 0x18),
    LcdWidget("launch_browser", "Launch browser", 0x1B),
    LcdWidget("browser_back", "Browser back", 0x1C),
    LcdWidget("browser_forward", "Browser forward", 0x1D),
    LcdWidget("calculator", "Calculator", 0x1E),
    LcdWidget("lock_pc", "Lock PC", 0x3B),
    LcdWidget("shutdown", "Shutdown", 0x3C),
    LcdWidget("screenshot", "Screenshot", 0x3E),
)})
_BY_WIRE = {widget.signature: widget for widget in LCD_WIDGETS.values()}


def _integer(name: str, value: int, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProtocolError(f"{name} must be an integer in {minimum}..{maximum}")
    return value


def _widget(wire_type: int, subtype: int) -> LcdWidget:
    subtype = 0 if subtype == 0xFF else subtype
    return _BY_WIRE.get((wire_type, subtype), LcdWidget(
        f"unknown_{wire_type:02x}_{subtype:02x}",
        f"Unknown widget {wire_type:02X}/{subtype:02X}", wire_type, subtype))


@dataclass(frozen=True)
class LcdPage:
    raw: bytes
    page_id: int
    # None denotes a continuation of a preceding three-slot widget.
    # The unexposed fourth/fifth records have slots=None and remain opaque.
    slots: tuple[LcdWidget | None, ...] | None

    @property
    def signature(self) -> tuple:
        if self.slots is None:
            return (self.raw,)
        return (self.page_id, self.raw[1:3], tuple(
            None if widget is None else widget.signature for widget in self.slots))


@dataclass(frozen=True)
class LcdState:
    raw: bytes
    profile_index: int
    page_count: int
    pages: tuple[LcdPage, ...]

    @property
    def signature(self) -> tuple:
        """Logical state for conflict/readback checks; excludes response trailer."""
        return self.profile_index, self.page_count, tuple(page.signature for page in self.pages)


def build_lcd_read_request(profile_index: int) -> bytes:
    profile = _integer("profile_index", profile_index, 0, 4)
    return bytes((0x10, 0x1C, 0, 0x25, profile, 0, 0)) + bytes(REPORT_BYTES - 7)


def build_lcd_get_buffer() -> bytes:
    return bytes((0x10, 0x25)) + bytes(REPORT_BYTES - 2)


def _decode_slots(record: bytes) -> tuple[LcdWidget | None, ...]:
    # Firmware compacts nonzero widget pairs; the UI displays them reversed.
    pairs = [(record[i], record[i + 1]) for i in range(3, 11, 2) if record[i]]
    slots: list[LcdWidget | None] = []
    for wire_type, subtype in reversed(pairs):
        widget = _widget(wire_type, subtype)
        if len(slots) + widget.width > LCD_SLOTS_PER_PAGE:
            raise ProtocolError("LCD page has overlapping or overflowing widget widths")
        slots.append(widget)
        slots.extend([None] * (widget.width - 1))
    slots.extend([LCD_WIDGETS["empty"]] * (LCD_SLOTS_PER_PAGE - len(slots)))
    return tuple(slots)


def decode_lcd_response(data: bytes, profile_index: int) -> LcdState:
    profile = _integer("profile_index", profile_index, 0, 4)
    if not isinstance(data, (bytes, bytearray)) or len(data) != LCD_RESPONSE_BYTES:
        raise ProtocolError("LCD response must contain exactly 61 bytes")
    raw = bytes(data)
    if raw[:4] != bytes((0x10, 0x25, 0, profile)):
        raise ProtocolError("LCD response report, command, status, or profile does not match")
    # There are five records on the wire. The vendor editor exposes only three.
    _integer("LCD page count", raw[4], 1, 5)
    pages = []
    for index in range(5):
        record = raw[5 + index * 11:16 + index * 11]
        if record[0] != index + 1:
            raise ProtocolError("LCD response contains an unexpected page identifier")
        slots = _decode_slots(record) if index < LCD_EDITABLE_PAGES else None
        pages.append(LcdPage(record, index + 1, slots))
    return LcdState(raw, profile, raw[4], tuple(pages))


def _validated_state(state: LcdState) -> LcdState:
    if not isinstance(state, LcdState):
        raise ProtocolError("An LCD read response is required before editing")
    parsed = decode_lcd_response(state.raw, state.profile_index)
    if parsed != state:
        raise ProtocolError("LCD state was modified without a matching raw response")
    return parsed


def _edited_slots(values: Sequence, original: LcdPage) -> tuple[LcdWidget | None, ...]:
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Sequence) or len(values) != 4:
        raise ProtocolError("An LCD page must have exactly four logical slots")
    result: list[LcdWidget | None] = []
    continuation = 0
    for value in values:
        if continuation:
            if value is not None:
                raise ProtocolError("A wide LCD widget requires None in its two continuation slots")
            result.append(None)
            continuation -= 1
            continue
        if isinstance(value, str):
            if value not in LCD_WIDGETS:
                raise ProtocolError(f"Unsupported LCD widget: {value}")
            widget = LCD_WIDGETS[value]
        elif isinstance(value, LcdWidget):
            if value not in LCD_WIDGETS.values() and value not in (original.slots or ()):
                raise ProtocolError("Unknown LCD widgets may only be preserved from the current page")
            widget = value
        else:
            raise ProtocolError("LCD slot must name a widget; None is only a wide-widget continuation")
        result.append(widget)
        continuation = widget.width - 1
    if continuation:
        raise ProtocolError("A wide LCD widget extends beyond the end of its page")
    return tuple(result)


def edited_lcd_signature(state: LcdState, *, pages: Mapping[int, Sequence]) -> tuple:
    """Expected semantic readback for precisely the same edits as the builder."""
    state = _validated_state(state)
    edits = _validate_edits(state, pages)
    signatures = []
    for index, page in enumerate(state.pages):
        slots = edits.get(index, page.slots)
        signatures.append(page.signature if slots is None else (
            page.page_id, page.raw[1:3], tuple(
                None if widget is None else widget.signature for widget in slots)))
    return state.profile_index, state.page_count, tuple(signatures)


def _validate_edits(state: LcdState, pages: Mapping[int, Sequence]) -> dict:
    if not isinstance(pages, Mapping):
        raise ProtocolError("LCD page edits must map zero-based page indices to slots")
    result = {}
    for index, values in pages.items():
        _integer("editable LCD page", index, 0, LCD_EDITABLE_PAGES - 1)
        if index >= state.page_count:
            raise ProtocolError("Enabling additional LCD pages has not been implemented")
        result[index] = _edited_slots(values, state.pages[index])
    return result


def build_lcd_report(state: LcdState, *, pages: Mapping[int, Sequence]) -> bytes:
    """Replace selected page slots; preserve profile/count/reserved/opaque data.

    Pages use zero-based indices 0..2. A three-slot widget requires two None
    continuation entries, e.g. ["system_media", None, None, "dpi"].
    This is only command 0x25: it does not replace separate key/macro definitions.
    """
    state = _validated_state(state)
    edits = _validate_edits(state, pages)
    report = bytearray(state.raw + bytes(REPORT_BYTES - len(state.raw)))
    report[2] = 0x3B
    for index in range(LCD_EDITABLE_PAGES):
        slots = edits.get(index, state.pages[index].slots)
        assert slots is not None
        pairs = [b"\0\0" if widget is None else bytes(widget.signature) for widget in slots]
        offset = 8 + index * 11
        report[offset:offset + 8] = b"".join(reversed(pairs))
    return bytes(report)
