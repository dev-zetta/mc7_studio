"""Pure codecs for the MC7's per-page screen-key definitions.

Command ``0x29`` stores four eleven-byte trigger records for each of the three
editable LCD pages. Known keyboard records reuse the ordinary button keyboard
codec; known host actions store only a function code and short display label.
Every other nonempty record remains exact so callers cannot accidentally move
or reinterpret it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .button_commands import decode_action, encode_action
from .configuration import Action
from .host_actions import encode_host_action_record, same_host_action_record
from .lcd_commands import (
    LCD_HOST_ACTION_WIDGETS, LcdPage, LcdState, LcdWidget, decode_lcd_response,
)
from .protocol import ProtocolError, REPORT_BYTES


SCREEN_KEY_PAGES = 3
SCREEN_KEYS_PER_PAGE = 4
SCREEN_KEY_RECORD_BYTES = 11
SCREEN_KEY_RESPONSE_BYTES = 54
SCREEN_KEY_RESPONSES_BYTES = SCREEN_KEY_PAGES * SCREEN_KEY_RESPONSE_BYTES
DEVICE_BINDING_PREFIX = "device:"
_EMPTY_RECORD = bytes(SCREEN_KEY_RECORD_BYTES)
_SCREEN_KEY_WIDGETS = frozenset(("remap_key", "hotkey"))
_LCD_MACRO_WIDGET = "macro"
LCD_FINITE_MACRO_PREFIX = bytes.fromhex("0001000700")
LCD_TOGGLE_MACRO_PREFIX = bytes.fromhex("0001020700")


def _integer(name: str, value: int, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProtocolError(f"{name} must be an integer in {minimum}..{maximum}")
    return value


def _record(value: bytes) -> bytes:
    if not isinstance(value, (bytes, bytearray)) or len(value) != SCREEN_KEY_RECORD_BYTES:
        raise ProtocolError("A screen-key record must contain exactly eleven bytes")
    return bytes(value)


@dataclass(frozen=True)
class ScreenKeyPage:
    raw: bytes
    page_index: int
    records: tuple[bytes, ...]

    @property
    def signature(self) -> tuple:
        """Fields persisted by a page write, excluding response metadata."""
        return self.page_index, self.records


@dataclass(frozen=True)
class ScreenKeyState:
    raw: bytes
    profile_index: int
    pages: tuple[ScreenKeyPage, ...]

    @property
    def signature(self) -> tuple:
        return self.profile_index, tuple(page.signature for page in self.pages)


def build_screen_key_read_request(profile_index: int, page_index: int) -> bytes:
    """Build the page selector; public page indices are zero based."""
    profile = _integer("profile_index", profile_index, 0, 4)
    page = _integer("page_index", page_index, 0, SCREEN_KEY_PAGES - 1)
    prefix = bytes((0x10, 0x1C, 0, 0x29, profile, page + 1, 0))
    return prefix + bytes(REPORT_BYTES - len(prefix))


def build_screen_key_get_buffer() -> bytes:
    """Return the initialized 64-byte feature buffer used by the vendor read."""
    prefix = bytes((0x10, 0x29, 0, 0x29, 0x32, 0))
    return prefix + bytes(REPORT_BYTES - len(prefix))


def decode_screen_key_responses(data: bytes, profile_index: int) -> ScreenKeyState:
    """Decode three concatenated, checksum-verified 54-byte page responses."""
    profile = _integer("profile_index", profile_index, 0, 4)
    if not isinstance(data, (bytes, bytearray)) or len(data) != SCREEN_KEY_RESPONSES_BYTES:
        raise ProtocolError("Screen-key responses must contain exactly three 54-byte pages")
    raw = bytes(data)
    pages = []
    for page_index in range(SCREEN_KEY_PAGES):
        start = page_index * SCREEN_KEY_RESPONSE_BYTES
        page_raw = raw[start:start + SCREEN_KEY_RESPONSE_BYTES]
        header = bytes((0x10, 0x29, 0, 0x29, 0x32, 0, profile, page_index + 1))
        if page_raw[:8] != header:
            raise ProtocolError("Screen-key response identity, status, profile, or page does not match")
        if sum(page_raw[2:]) & 0xFF:
            raise ProtocolError("Screen-key response checksum does not match")
        wire_records = tuple(
            page_raw[9 + slot * SCREEN_KEY_RECORD_BYTES:
                     9 + (slot + 1) * SCREEN_KEY_RECORD_BYTES]
            for slot in range(SCREEN_KEYS_PER_PAGE)
        )
        # The response and writer order records right-to-left. Public records
        # follow the LCD editor's logical left-to-right slot order.
        pages.append(ScreenKeyPage(page_raw, page_index, tuple(reversed(wire_records))))
    return ScreenKeyState(raw, profile, tuple(pages))


def opaque_screen_key_binding(record: bytes) -> str:
    """Return the lowercase preset representation of one opaque record."""
    return DEVICE_BINDING_PREFIX + _record(record).hex()


def _ascii_label_valid(label: bytes) -> bool:
    if len(label) != 6 or label[-1] != 0:
        return False
    text, separator, padding = label.partition(b"\0")
    return bool(separator and text and not padding.strip(b"\0")
                and all(0x20 <= byte <= 0x7E for byte in text))


def _macro_label_valid(label: bytes) -> bool:
    """Validate qstrncpy's visible prefix while retaining stale tail bytes."""
    if len(label) != 6 or label[-1] != 0:
        return False
    text = label.split(b"\0", 1)[0]
    return bool(text and all(0x20 <= byte <= 0x7E for byte in text))


def _widget_key(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, LcdWidget):
        return value.key
    if isinstance(value, str) and value:
        return value
    raise ProtocolError("LCD page slots must contain widget names, widgets, or None")


def _page_matrix(value, *, name: str) -> tuple[tuple[str | None, ...], ...]:
    if isinstance(value, LcdState):
        parsed = decode_lcd_response(value.raw, value.profile_index)
        if parsed != value:
            raise ProtocolError("LCD state was modified without a matching raw response")
        value = value.pages[:SCREEN_KEY_PAGES]
    if (isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence)
            or len(value) != SCREEN_KEY_PAGES):
        raise ProtocolError(f"{name} must contain exactly three LCD pages")
    result = []
    for page in value:
        if isinstance(page, LcdPage):
            page = page.slots
        if (page is None or isinstance(page, (str, bytes, bytearray))
                or not isinstance(page, Sequence) or len(page) != SCREEN_KEYS_PER_PAGE):
            raise ProtocolError(f"Each {name} page must contain exactly four logical slots")
        result.append(tuple(_widget_key(slot) for slot in page))
    return tuple(result)


def _known_binding(record: bytes, widget_key: str | None) -> str | None:
    if record == _EMPTY_RECORD:
        return None
    if widget_key not in _SCREEN_KEY_WIDGETS or not _ascii_label_valid(record[5:11]):
        return None
    action = decode_action(record[:4])
    if action is None or action.kind != "keyboard":
        return None
    if widget_key == "remap_key" and record[3] != 0x0C:
        return None
    if widget_key == "hotkey" and record[3] != 0x06:
        return None
    return action.value


def decode_screen_key_record(record: bytes, widget_key: str) -> str | None:
    """Decode a supported record, returning an exact device marker otherwise."""
    raw = _record(record)
    if not isinstance(widget_key, str) or widget_key not in _SCREEN_KEY_WIDGETS:
        raise ProtocolError("Screen-key widget must be remap_key or hotkey")
    known = _known_binding(raw, widget_key)
    if known is not None or raw == _EMPTY_RECORD:
        return known
    return opaque_screen_key_binding(raw)


def _canonical_action(binding: str) -> tuple[Action, bytes]:
    if not isinstance(binding, str) or not binding or binding.startswith(DEVICE_BINDING_PREFIX):
        raise ProtocolError("A screen-key binding must name one supported keyboard key or shortcut")
    encoded = encode_action(Action("keyboard", binding))
    action = decode_action(encoded)
    if action is None:
        raise ProtocolError("A screen-key binding must use the supported keyboard encoding")
    return action, encoded


def encode_screen_key_record(binding: str, widget_key: str) -> bytes:
    """Encode a key/shortcut and its six-byte, NUL-padded ASCII display label."""
    if widget_key not in _SCREEN_KEY_WIDGETS:
        raise ProtocolError("Screen-key widget must be remap_key or hotkey")
    action, encoded = _canonical_action(binding)
    if widget_key == "remap_key":
        if encoded[3] != 0x0C:
            raise ProtocolError("A remap_key widget accepts one key without shortcut modifiers")
    elif encoded[3] == 0x0C:
        usage = encoded[2]
        if usage >= 0xE0:
            raise ProtocolError("A hotkey must end with a non-modifier key")
        encoded = bytes((0, usage, 0, 0x06))
    key_name = action.value.rsplit("+", 1)[-1]
    try:
        label = key_name.encode("ascii")
    except UnicodeEncodeError as error:
        raise ProtocolError("Screen-key labels must use ASCII characters") from error
    label = label[:5].ljust(6, b"\0")
    return encoded + b"\0" + label


def encode_lcd_macro_record(name: str, playback: str = "once") -> bytes:
    """Encode the command-0x29 trigger record for a finite LCD macro.

    The touch callback writes function record ``00 01 00 07``, reserves byte
    four, and copies at most five Latin-1 label bytes plus a terminator. Local
    macro names are already bounded to printable ASCII, so encoding is exact.
    """
    if playback not in ("once", "repeat", "toggle"):
        raise ProtocolError("LCD touch macros support once, repeat, and toggle playback")
    if (not isinstance(name, str) or not name or len(name) >= 32
            or any(not 0x20 <= ord(character) <= 0x7E for character in name)):
        raise ProtocolError("An LCD macro name must contain 1..31 printable ASCII characters")
    prefix = LCD_TOGGLE_MACRO_PREFIX if playback == "toggle" else LCD_FINITE_MACRO_PREFIX
    return prefix + name.encode("ascii")[:5].ljust(6, b"\0")


def lcd_macro_record_mode(record: bytes) -> str:
    """Return ``finite`` or ``toggle`` for a supported touch record."""
    raw = _record(record)
    if not _macro_label_valid(raw[5:11]):
        raise ProtocolError("The LCD macro trigger uses an unsupported or malformed mode record")
    if raw[:5] == LCD_FINITE_MACRO_PREFIX:
        return "finite"
    if raw[:5] == LCD_TOGGLE_MACRO_PREFIX:
        return "toggle"
    raise ProtocolError("The LCD macro trigger uses an unsupported or malformed mode record")


def decode_lcd_macro_record(record: bytes) -> str:
    """Return the finite touch record's display label, rejecting other modes."""
    raw = _record(record)
    lcd_macro_record_mode(raw)
    return raw[5:11].partition(b"\0")[0].decode("ascii")


def _same_lcd_macro_record(existing: bytes, requested: bytes) -> bool:
    try:
        return (lcd_macro_record_mode(existing) == lcd_macro_record_mode(requested)
                and decode_lcd_macro_record(existing) == decode_lcd_macro_record(requested))
    except ProtocolError:
        return False


def _validated_state(state: ScreenKeyState) -> ScreenKeyState:
    if not isinstance(state, ScreenKeyState):
        raise ProtocolError("Read all three screen-key pages before editing them")
    parsed = decode_screen_key_responses(state.raw, state.profile_index)
    if parsed != state:
        raise ProtocolError("Screen-key state was modified without matching raw responses")
    return parsed


def screen_key_bindings(state: ScreenKeyState, lcd_pages) -> tuple[tuple[str | None, ...], ...]:
    """Return a 3x4 matrix of keyboard strings, None, or ``device:<22hex>``."""
    state = _validated_state(state)
    page_keys = _page_matrix(lcd_pages, name="lcd_pages")
    result = []
    for page_index, page in enumerate(state.pages):
        values = []
        for slot, record in enumerate(page.records):
            widget_key = page_keys[page_index][slot]
            if widget_key not in _SCREEN_KEY_WIDGETS:
                # Command 0x29 can retain stale or vendor-private data behind
                # ordinary LCD widgets. It is not an editable binding there.
                values.append(None)
                continue
            known = _known_binding(record, widget_key)
            if known is not None:
                values.append(known)
            elif record == _EMPTY_RECORD:
                values.append(None)
            else:
                values.append(opaque_screen_key_binding(record))
        result.append(tuple(values))
    return tuple(result)


def _tile_identity(page: tuple[str | None, ...], slot: int) -> tuple:
    """Identify the LCD tile occupying a slot, including wide continuations."""
    key = page[slot]
    if key is not None:
        return slot, key, 0
    anchor = slot - 1
    while anchor >= 0 and page[anchor] is None:
        anchor -= 1
    if anchor < 0:
        return slot, None, 0
    return anchor, page[anchor], slot - anchor


def _binding_matrix(value) -> tuple[tuple[str | None, ...], ...]:
    if (isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence)
            or len(value) != SCREEN_KEY_PAGES):
        raise ProtocolError("desired_bindings must contain exactly three pages")
    result = []
    for page in value:
        if (isinstance(page, (str, bytes, bytearray)) or not isinstance(page, Sequence)
                or len(page) != SCREEN_KEYS_PER_PAGE):
            raise ProtocolError("Each desired binding page must contain exactly four slots")
        if any(binding is not None and not isinstance(binding, str) for binding in page):
            raise ProtocolError("A screen-key binding must be a string, device marker, or None")
        result.append(tuple(page))
    return tuple(result)


def _macro_record_matrix(value) -> tuple[tuple[bytes | None, ...], ...]:
    if value is None:
        return tuple((None,) * SCREEN_KEYS_PER_PAGE for _ in range(SCREEN_KEY_PAGES))
    if (isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence)
            or len(value) != SCREEN_KEY_PAGES):
        raise ProtocolError("desired_macro_records must contain exactly three pages")
    result = []
    for page in value:
        if (isinstance(page, (str, bytes, bytearray)) or not isinstance(page, Sequence)
                or len(page) != SCREEN_KEYS_PER_PAGE):
            raise ProtocolError("Each desired macro-record page must contain exactly four slots")
        result.append(tuple(None if record is None else _record(record) for record in page))
    return tuple(result)


def _host_action_target_matrix(value) -> tuple[tuple[str | None, ...], ...]:
    if value is None:
        return tuple((None,) * SCREEN_KEYS_PER_PAGE for _ in range(SCREEN_KEY_PAGES))
    if (isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence)
            or len(value) != SCREEN_KEY_PAGES):
        raise ProtocolError("desired_host_action_targets must contain exactly three pages")
    result = []
    for page in value:
        if (isinstance(page, (str, bytes, bytearray)) or not isinstance(page, Sequence)
                or len(page) != SCREEN_KEYS_PER_PAGE):
            raise ProtocolError(
                "Each desired host-action target page must contain exactly four slots")
        if any(target is not None and not isinstance(target, str) for target in page):
            raise ProtocolError("A host-action target must be a string or None")
        result.append(tuple(page))
    return tuple(result)


def _host_action_icon_index_matrix(value) -> tuple[tuple[int | None, ...], ...]:
    if value is None:
        return tuple((None,) * SCREEN_KEYS_PER_PAGE for _ in range(SCREEN_KEY_PAGES))
    if (isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence)
            or len(value) != SCREEN_KEY_PAGES):
        raise ProtocolError(
            "desired_host_action_icon_indices must contain exactly three pages")
    result = []
    for page in value:
        if (isinstance(page, (str, bytes, bytearray)) or not isinstance(page, Sequence)
                or len(page) != SCREEN_KEYS_PER_PAGE):
            raise ProtocolError(
                "Each desired host-action icon-index page must contain exactly four slots")
        if any(index is not None
               and (type(index) is not int or not 0 <= index <= 19)
               for index in page):
            raise ProtocolError(
                "A host-action icon index must be an integer from 0 to 19 or None")
        result.append(tuple(page))
    return tuple(result)


def _planned_records(state: ScreenKeyState, lcd_state: LcdState, desired_pages,
                     desired_bindings, desired_macro_records=None,
                     desired_host_action_targets=None,
                     desired_host_action_icon_indices=None) -> tuple[tuple[bytes, ...], ...]:
    state = _validated_state(state)
    if not isinstance(lcd_state, LcdState):
        raise ProtocolError("A fresh LCD state is required before editing screen keys")
    verified_lcd = decode_lcd_response(lcd_state.raw, lcd_state.profile_index)
    if verified_lcd != lcd_state:
        raise ProtocolError("LCD state was modified without a matching raw response")
    if state.profile_index != lcd_state.profile_index:
        raise ProtocolError("LCD and screen-key states must belong to the same profile")
    current_pages = _page_matrix(lcd_state, name="lcd_state")
    target_pages = _page_matrix(desired_pages, name="desired_pages")
    bindings = _binding_matrix(desired_bindings)
    macro_records = _macro_record_matrix(desired_macro_records)
    host_targets = _host_action_target_matrix(desired_host_action_targets)
    host_icon_indices = _host_action_icon_index_matrix(
        desired_host_action_icon_indices)
    current_bindings = screen_key_bindings(state, current_pages)
    planned = []
    for page_index, page in enumerate(state.pages):
        records = []
        for slot, original in enumerate(page.records):
            widget_key = target_pages[page_index][slot]
            binding = bindings[page_index][slot]
            host_target = host_targets[page_index][slot]
            host_icon_index = host_icon_indices[page_index][slot]
            same_tile = (_tile_identity(current_pages[page_index], slot)
                         == _tile_identity(target_pages[page_index], slot))
            if (host_icon_index is not None
                    and widget_key != "open_application"):
                raise ProtocolError(
                    "Only Open Application LCD tiles accept custom-icon indices")
            if widget_key == _LCD_MACRO_WIDGET:
                if host_target is not None:
                    raise ProtocolError("LCD macro tiles do not accept host-action targets")
                if binding is not None:
                    raise ProtocolError("LCD macro tiles use macro records, not keyboard bindings")
                requested = macro_records[page_index][slot]
                if requested is None:
                    replacement = original if same_tile else _EMPTY_RECORD
                else:
                    decode_lcd_macro_record(requested)
                    replacement = (original if same_tile
                                   and _same_lcd_macro_record(original, requested)
                                   else requested)
            elif widget_key in LCD_HOST_ACTION_WIDGETS:
                if binding is not None:
                    raise ProtocolError(
                        "Host-action LCD tiles do not accept keyboard bindings")
                if macro_records[page_index][slot] is not None:
                    raise ProtocolError("Host-action LCD tiles do not accept macro records")
                if host_target is None:
                    if host_icon_index is not None:
                        raise ProtocolError(
                            "An Open Application icon index requires an explicit local target")
                    if not same_tile:
                        raise ProtocolError(
                            "A new host-action LCD tile requires an explicit local target")
                    replacement = original
                else:
                    requested = encode_host_action_record(
                        widget_key, host_target, icon_index=host_icon_index)
                    replacement = (original if same_tile and same_host_action_record(
                        original, requested, widget_key) else requested)
            elif widget_key not in _SCREEN_KEY_WIDGETS:
                if host_target is not None:
                    raise ProtocolError(
                        "Only host-action LCD tiles accept host-action targets")
                if binding is not None:
                    raise ProtocolError(
                        "Only remap_key and hotkey LCD tiles accept screen-key bindings")
                # Hidden command-0x29 data belongs to its current LCD tile. Keep
                # it across unrelated edits, but do not carry it into a new tile.
                replacement = original if same_tile else _EMPTY_RECORD
            elif binding is None:
                if host_target is not None:
                    raise ProtocolError(
                        "Keyboard LCD tiles do not accept host-action targets")
                replacement = _EMPTY_RECORD
            elif binding.startswith(DEVICE_BINDING_PREFIX):
                if host_target is not None:
                    raise ProtocolError(
                        "Keyboard LCD tiles do not accept host-action targets")
                marker = opaque_screen_key_binding(original)
                if (binding != marker
                        or not same_tile
                        or current_bindings[page_index][slot] != marker):
                    raise ProtocolError(
                        "Opaque screen-key records may only be preserved in their unchanged logical tile")
                replacement = original
            else:
                if host_target is not None:
                    raise ProtocolError(
                        "Keyboard LCD tiles do not accept host-action targets")
                if widget_key not in _SCREEN_KEY_WIDGETS:
                    raise ProtocolError("Keyboard bindings require a remap_key or hotkey LCD tile")
                canonical = decode_action(encode_screen_key_record(binding, widget_key)[:4])
                existing = _known_binding(original, widget_key)
                replacement = (original if (same_tile and canonical is not None
                                            and existing == canonical.value)
                               else encode_screen_key_record(binding, widget_key))
            records.append(replacement)
        planned.append(tuple(records))
    return tuple(planned)


def expected_screen_key_state(state: ScreenKeyState, lcd_state: LcdState,
                              desired_pages, desired_bindings,
                              desired_macro_records=None,
                              desired_host_action_targets=None,
                              desired_host_action_icon_indices=None) -> ScreenKeyState:
    """Predict the exact checksum-valid response for a successful reread."""
    state = _validated_state(state)
    planned = _planned_records(state, lcd_state, desired_pages, desired_bindings,
                               desired_macro_records, desired_host_action_targets,
                               desired_host_action_icon_indices)
    responses = []
    for page, records in zip(state.pages, planned):
        if records == page.records:
            responses.append(page.raw)
            continue
        response = bytearray(page.raw)
        response[9:53] = b"".join(reversed(records))
        response[53] = (-sum(response[2:53])) & 0xFF
        responses.append(bytes(response))
    return decode_screen_key_responses(b"".join(responses), state.profile_index)


def build_screen_key_reports(state: ScreenKeyState, lcd_state: LcdState,
                             desired_pages, desired_bindings,
                             desired_macro_records=None,
                             desired_host_action_targets=None,
                             desired_host_action_icon_indices=None) -> tuple[tuple[bytes, ...], ScreenKeyState]:
    """Build only changed page writes and return their predicted reread state."""
    state = _validated_state(state)
    planned = _planned_records(state, lcd_state, desired_pages, desired_bindings,
                               desired_macro_records, desired_host_action_targets,
                               desired_host_action_icon_indices)
    reports = []
    for page_index, (page, records) in enumerate(zip(state.pages, planned)):
        if records == page.records:
            continue
        # The embedded length is 0x32 bytes after report ID 0x10: the remaining
        # six header bytes plus four 11-byte records.  Byte 6 is reserved.
        prefix = bytes((0x10, 0x29, 0x32, 0, state.profile_index, page_index + 1, 0))
        report = prefix + b"".join(reversed(records))
        reports.append(report + bytes(REPORT_BYTES - len(report)))
    expected = expected_screen_key_state(state, lcd_state, desired_pages, desired_bindings,
                                         desired_macro_records,
                                         desired_host_action_targets,
                                         desired_host_action_icon_indices)
    return tuple(reports), expected
