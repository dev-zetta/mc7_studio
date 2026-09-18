"""Bounded MC7 macro transfer and input-event codecs, with no device I/O.

Selectors and response headers follow vendor FUN_180267e70. Reassembly retains
the native transfer lengths; it never invents the missing byte in the vendor's
1038-byte image allocation. A completed transfer is research evidence, not a
checksum-verified macro. Vendor write packets are reproduced from a capture
made under Wine with device I/O blocked. No function opens a device, assigns
a button, or executes macro events.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from .button_commands import BUTTON_SLOTS
from .protocol import ProtocolError, REPORT_BYTES


MACRO_READ_CHUNKS = 17
MACRO_RESPONSE_HEADER = b"\x10\x1d\0"
MACRO_MAX_CHUNK_PAYLOAD = REPORT_BYTES - len(MACRO_RESPONSE_HEADER)
VENDOR_MACRO_IMAGE_BYTES = 0x40E
MACRO_METADATA_BYTES = 0x4C
MACRO_EVENT_BYTES = 0x3C0
MAX_KEYBOARD_EVENTS = (MACRO_EVENT_BYTES - 2) // 2
MAX_EVENT_DELAY_TICKS = 60_000
MAX_MACRO_DELAY_TICKS = 3_600_000
MOUSE_CODES = {
    "left": 0xF0,
    "right": 0xF1,
    "middle": 0xF2,
    "forward": 0xF3,
    "back": 0xF4,
}
_MOUSE_NAMES = {code: button for button, code in MOUSE_CODES.items()}
NORMAL_MACRO_ASSIGNMENT = bytes.fromhex("00010207")
HELD_MACRO_ASSIGNMENT = bytes.fromhex("00010107")
# FUN_18027a470 stores LCD macro tiles after the two eleven-button namespaces.
# The vendor walks cells from left to right while its raw slot selector counts
# down. Keep the public coordinate flattened as page * 4 + cell.
LCD_MACRO_CELLS = tuple(range(12))
LCD_MACRO_RAW_SLOTS = (33, 32, 31, 30, 37, 36, 35, 34, 41, 40, 39, 38)


def macro_playback_metadata(playback: str, repeat_count: int = 1) -> tuple[bytes, int]:
    """Return the source-established button record and image loop count.

    The local repeat preference remains 1..999 in every mode. Once ignores it;
    While held and Toggle encode zero instead. This describes stored metadata,
    not tested physical cancellation behavior or a host-side stop command.
    """
    count = _integer("Macro repeat count", repeat_count, 1, 999)
    if not isinstance(playback, str) or playback not in ("once", "repeat", "while_held", "toggle"):
        raise ProtocolError("Macro playback must be once, repeat, while_held, or toggle")
    if playback == "while_held":
        return HELD_MACRO_ASSIGNMENT, 0
    return NORMAL_MACRO_ASSIGNMENT, (0 if playback == "toggle" else 1 if playback == "once" else count)


def decode_macro_playback(assignment: bytes, repeat_count: int) -> str:
    """Validate a wire assignment/count pair and recover its playback mode."""
    count = _integer("Onboard macro loop count", repeat_count, 0, 999)
    if not isinstance(assignment, (bytes, bytearray)):
        raise ProtocolError("An onboard macro requires a supported four-byte assignment")
    if assignment == HELD_MACRO_ASSIGNMENT:
        if count:
            raise ProtocolError("While-held macro assignments require a zero image loop count")
        return "while_held"
    if assignment != NORMAL_MACRO_ASSIGNMENT:
        raise ProtocolError("The onboard macro assignment uses an unsupported function record")
    return "toggle" if count == 0 else "once" if count == 1 else "repeat"


def _integer(name: str, value: int, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProtocolError(f"{name} must be an integer in {minimum}..{maximum}")
    return value


def macro_slot(logical_slot: int, layer: str = "primary") -> int:
    """Resolve a button or flattened LCD cell to its raw macro slot."""
    if not isinstance(layer, str) or layer not in ("primary", "easy_shift", "lcd"):
        raise ProtocolError("Macro layer must be primary, easy_shift, or lcd")
    if layer == "lcd":
        if type(logical_slot) is not int or logical_slot not in LCD_MACRO_CELLS:
            raise ProtocolError("Choose an LCD macro cell in 0..11")
        return LCD_MACRO_RAW_SLOTS[logical_slot]
    if type(logical_slot) is not int or logical_slot not in BUTTON_SLOTS:
        raise ProtocolError("Choose a visible MC7 logical button slot")
    return logical_slot + (15 if layer == "easy_shift" else 0)


def build_macro_read_request(profile_index: int, logical_slot: int, chunk_index: int,
                             layer: str = "primary") -> bytes:
    """Select one read chunk; await the matching F2 ACK before feature GET.

    The source sums all six selector bytes, including report ID 0x10, and
    stores the low eight bits. It does not use a negative checksum here.
    """
    profile = _integer("Profile index", profile_index, 0, 4)
    slot = macro_slot(logical_slot, layer)
    chunk = _integer("Macro read chunk", chunk_index, 0, MACRO_READ_CHUNKS - 1)
    prefix = bytes((0x10, 0x1C, 3, profile, slot, chunk))
    return prefix + bytes((sum(prefix) & 255,)) + bytes(REPORT_BYTES - 7)


def build_macro_get_buffer() -> bytes:
    return b"\x10\x1d" + bytes(REPORT_BYTES - 2)


@dataclass(frozen=True)
class MacroChunk:
    """One native GET result with its caller-correlated chunk index.

    The response does not echo profile, slot, or chunk index. The transaction
    owner must serialize selector/ACK/GET and retain the same opened device.
    """

    chunk_index: int
    raw: bytes

    @property
    def payload(self) -> bytes:
        return self.raw[len(MACRO_RESPONSE_HEADER):]


def decode_macro_chunk(data: bytes, chunk_index: int) -> MacroChunk:
    """Validate a native header without padding or guessing a trailer layout.

    Four through 64 native bytes are retained for format discovery. The
    assembler separately enforces consistent ordinary chunk lengths and a
    bounded final chunk; short transfers never silently become zero padding.
    """
    index = _integer("Macro read chunk", chunk_index, 0, MACRO_READ_CHUNKS - 1)
    if not isinstance(data, (bytes, bytearray)) or not 4 <= len(data) <= REPORT_BYTES:
        raise ProtocolError("Macro GET must contain a three-byte header and 1..61 payload bytes")
    if data[:2] != MACRO_RESPONSE_HEADER[:2]:
        raise ProtocolError("Macro response report or command does not match")
    if data[2] != 0:
        raise ProtocolError(f"Macro chunk {index} returned status 0x{data[2]:02x}")
    return MacroChunk(index, bytes(data))


@dataclass(frozen=True)
class MacroReadResult:
    """All seventeen source-observed chunks, with the exact bytes received.

    This result intentionally makes no checksum or playback-validity claim.
    A source-format parser must account for native firmware's actual size.
    """

    profile_index: int
    logical_slot: int
    layer: str
    chunks: tuple[MacroChunk, ...]

    @property
    def macro_slot(self) -> int:
        return macro_slot(self.logical_slot, self.layer)

    @property
    def raw(self) -> bytes:
        return b"".join(chunk.payload for chunk in self.chunks)

    @property
    def native_lengths(self) -> tuple[int, ...]:
        return tuple(len(chunk.raw) for chunk in self.chunks)


class MacroReadAssembler:
    """Incremental read reassembly that never fills a failed or missing chunk.

    Read exactly seventeen chunks on one serialized transport session. Supply
    an explicit chunk index to append() when correlating an external capture.
    The first sixteen payload sizes must match; the final size may be shorter
    because the firmware's effective image length is still being established.
    """

    def __init__(self, profile_index: int, logical_slot: int, layer: str = "primary"):
        self._profile_index = _integer("Profile index", profile_index, 0, 4)
        macro_slot(logical_slot, layer)
        self._logical_slot = logical_slot
        self._layer = layer
        self._chunks: list[MacroChunk] = []

    @property
    def next_chunk_index(self) -> int:
        return len(self._chunks)

    @property
    def received_bytes(self) -> int:
        return sum(len(chunk.payload) for chunk in self._chunks)

    @property
    def transfer_complete(self) -> bool:
        return len(self._chunks) == MACRO_READ_CHUNKS

    def read_request(self) -> bytes:
        if self.transfer_complete:
            raise ProtocolError("All seventeen macro chunks have already been read")
        return build_macro_read_request(self._profile_index, self._logical_slot,
                                        self.next_chunk_index, self._layer)

    def append(self, data: bytes, chunk_index: int | None = None) -> MacroChunk:
        if self.transfer_complete:
            raise ProtocolError("All seventeen macro chunks have already been read")
        expected = self.next_chunk_index
        supplied = expected if chunk_index is None else chunk_index
        _integer("Macro read chunk", supplied, 0, MACRO_READ_CHUNKS - 1)
        if supplied != expected:
            raise ProtocolError(f"Expected macro chunk {expected}; duplicate or out-of-order chunk received")
        chunk = decode_macro_chunk(data, supplied)
        if self._chunks:
            ordinary_size = len(self._chunks[0].payload)
            if (expected < MACRO_READ_CHUNKS - 1 and len(chunk.payload) != ordinary_size
                    or len(chunk.payload) > ordinary_size):
                raise ProtocolError("Macro chunk length changed before the final chunk")
        self._chunks.append(chunk)
        return chunk

    def finish(self) -> MacroReadResult:
        if not self.transfer_complete:
            raise ProtocolError(f"Macro read is incomplete: {len(self._chunks)} of seventeen chunks received")
        return MacroReadResult(self._profile_index, self._logical_slot, self._layer,
                               tuple(self._chunks))


@dataclass(frozen=True)
class KeyboardEvent:
    """A physical HID keyboard key transition and delay after it.

    Delay values use the compact stream's ticks. This module does not claim a
    physical duration without the image's time base and hardware validation.
    """

    key_code: int
    pressed: bool
    delay_after_ticks: int = 0


@dataclass(frozen=True)
class MouseEvent:
    """A source-established mouse button transition, using MC7 private codes.

    Swarm's mouse recording hook maps left/right/middle to f0/f1/f2. The MC7
    5.09 macro executor and ordinary-action handlers establish forward/back as
    f3/f4. These are not HID keyboard usages. Wheel scrolling remains
    unsupported.
    """

    button: str
    pressed: bool
    delay_after_ticks: int = 0

    @property
    def key_code(self) -> int:
        if not isinstance(self.button, str) or self.button not in MOUSE_CODES:
            raise ProtocolError(
                "Onboard mouse macros support left, right, middle, back, and forward click only"
            )
        return MOUSE_CODES[self.button]


InputEvent = KeyboardEvent | MouseEvent


@dataclass(frozen=True)
class TimingAdjustment:
    event_index: int
    requested_ticks: int
    encoded_ticks: int


@dataclass(frozen=True)
class KeyboardMacro:
    """Decoded input events and compact bytes; name retained for compatibility."""

    events: tuple[InputEvent, ...]
    encoded: bytes
    trailing_bytes: bytes = b""
    timing_adjustments: tuple[TimingAdjustment, ...] = ()


def _keyboard_key(value: int) -> int:
    _integer("HID keyboard usage", value, 0, 255)
    # Established keyboard/keypad usages through F24, plus both modifier sides.
    # Private mouse identifiers require a separate MouseEvent record.
    if not (4 <= value <= 0x73 or 0xE0 <= value <= 0xE7):
        raise ProtocolError("Unsupported HID keyboard usage; use MouseEvent for supported mouse buttons")
    return value


def _validate_events(events: Sequence[InputEvent], *, permit_empty: bool,
                     encoded_timing: bool = False) -> tuple[InputEvent, ...]:
    if (not isinstance(events, Sequence) or isinstance(events, (str, bytes, bytearray))
            or len(events) > MAX_KEYBOARD_EVENTS or not events and not permit_empty):
        raise ProtocolError(f"A macro needs 1..{MAX_KEYBOARD_EVENTS} input events")
    held: set[int] = set()
    duration = 0
    result = []
    for index, event in enumerate(events):
        if not isinstance(event, (KeyboardEvent, MouseEvent)):
            raise ProtocolError("Macros require KeyboardEvent or MouseEvent records")
        key = event.key_code if isinstance(event, MouseEvent) else _keyboard_key(event.key_code)
        if type(event.pressed) is not bool:
            raise ProtocolError("An input event's pressed flag must be a boolean")
        duration += _integer("Delay after input event", event.delay_after_ticks, 0,
                             MAX_EVENT_DELAY_TICKS + int(encoded_timing))
        if duration > MAX_MACRO_DELAY_TICKS + (MAX_KEYBOARD_EVENTS if encoded_timing else 0):
            raise ProtocolError("A macro exceeds its total delay limit")
        if event.pressed:
            if key in held:
                raise ProtocolError(f"Event {index} presses a key or mouse button that is already held")
            held.add(key)
        else:
            if key not in held:
                raise ProtocolError(f"Event {index} releases a key or mouse button that is not held")
            held.remove(key)
        result.append(event)
    if held:
        raise ProtocolError("A macro must release every key and mouse button before its stop marker")
    return tuple(result)


def decode_keyboard_macro(data: bytes) -> KeyboardMacro:
    """Decode the source-established compact grammar, requiring a stop marker.

    The byte cap includes the stop marker. Bytes after it remain separate
    evidence, rather than being treated as executable events or discarded.
    The historical function name also covers source-established mouse clicks.
    Unbalanced keys/buttons and unknown event codes fail closed.
    """
    if not isinstance(data, (bytes, bytearray)) or not 2 <= len(data) <= MACRO_EVENT_BYTES:
        raise ProtocolError("A compact macro stream must contain 2..960 bytes")
    raw = bytes(data)
    events: list[InputEvent] = []
    offset = 0
    while offset + 2 <= len(raw):
        first, second = raw[offset:offset + 2]
        offset += 2
        if first == 0:
            if second == 0:
                checked = _validate_events(events, permit_empty=True, encoded_timing=True)
                return KeyboardMacro(checked, raw[:offset], raw[offset:])
            if second not in (1, 2, 3):
                raise ProtocolError("Unknown compact macro delay-extension type")
            if not events:
                raise ProtocolError("A macro delay extension must follow an input event")
            if offset + 2 > len(raw):
                raise ProtocolError("Truncated compact macro delay extension")
            count = int.from_bytes(raw[offset:offset + 2], "little")
            offset += 2
            added = count * {1: 20, 2: 50, 3: 100}[second]
            events[-1] = replace(events[-1], delay_after_ticks=events[-1].delay_after_ticks + added)
        else:
            event = (MouseEvent(_MOUSE_NAMES[second], not bool(first & 0x80))
                     if second in _MOUSE_NAMES else KeyboardEvent(_keyboard_key(second), not bool(first & 0x80)))
            if events:
                events[-1] = replace(events[-1], delay_after_ticks=events[-1].delay_after_ticks + (first & 0x7F))
            events.append(event)
            if len(events) > MAX_KEYBOARD_EVENTS:
                raise ProtocolError("A compact macro contains too many input events")
    raise ProtocolError("Compact macro stop marker is missing or truncated")


def compile_keyboard_macro(events: Sequence[InputEvent]) -> KeyboardMacro:
    """Compile bounded balanced keyboard/mouse events using the vendor converter.

    This returns compact event bytes only, never upload or assignment packets.
    Zero gaps become one tick. Multiples of 20 requiring an extension gain one
    tick because the next ordinary event must have a nonzero short delay. All
    such differences appear in timing_adjustments and in the effective events.
    A final nonzero delay is rejected, since the source can silently lose it.
    """
    requested = _validate_events(events, permit_empty=False)
    if requested[-1].delay_after_ticks:
        raise ProtocolError("Trailing delays are unsupported; end with a release and zero delay")
    encoded = bytearray()
    pending = 1
    for event in requested:
        encoded.extend((max(1, pending) | (0 if event.pressed else 0x80), event.key_code))
        pending = event.delay_after_ticks
        if pending >= 128:
            count, pending = divmod(pending, 20)
            encoded.extend((0, 1))
            encoded.extend(count.to_bytes(2, "little"))
        if len(encoded) + 2 > MACRO_EVENT_BYTES:
            raise ProtocolError("Macro events and stop marker exceed the 960-byte device area")
    encoded.extend((0, 0))
    decoded = decode_keyboard_macro(encoded)
    adjustments = tuple(TimingAdjustment(index, wanted.delay_after_ticks, actual.delay_after_ticks)
                        for index, (wanted, actual) in enumerate(zip(requested, decoded.events))
                        if wanted.delay_after_ticks != actual.delay_after_ticks)
    return replace(decoded, timing_adjustments=adjustments)


def _name_bytes(value: str, label: str, width: int) -> bytes:
    if (not isinstance(value, str) or not value.strip() or len(value) >= width
            or any(not 32 <= ord(character) <= 126 for character in value)):
        raise ProtocolError(f"{label} must contain 1..{width-1} printable ASCII characters")
    return value.encode("ascii").ljust(width, b"\0")


def build_keyboard_macro_image(macro: KeyboardMacro, *, name: str = "Swarm2 macro",
                               group: str = "Swarm2", repeat_count: int = 1,
                               playback: str | None = None) -> bytes:
    """Build a new source image with fixed time base 1 and bounded metadata.

    Its source checksum is valid, but the traced vendor sender does not
    transmit that field. Validating this image never establishes device state.
    The stricter event limit prevents the vendor's last-chunk zero fill from
    replacing meaningful encoded events or their stop marker.
    Omitting playback preserves finite repeat_count behavior. Zero image counts
    require an explicit while_held or toggle mode; repeat_count itself stays a
    bounded local preference and is ignored by those two modes.
    """
    if not isinstance(macro, KeyboardMacro):
        raise ProtocolError("Compile a macro before creating its image")
    decoded = decode_keyboard_macro(macro.encoded)
    if decoded.events != macro.events or decoded.trailing_bytes or not decoded.events:
        raise ProtocolError("Macro events do not match their compact encoding")
    # Writer copies source[:1022], then zero-fills its remaining 32 payload bytes.
    if MACRO_METADATA_BYTES + len(macro.encoded) > 1022:
        raise ProtocolError("This macro would be truncated by the vendor's final upload chunk")
    mode = ("once" if repeat_count == 1 else "repeat") if playback is None else playback
    _, count = macro_playback_metadata(mode, repeat_count)
    image = bytearray(VENDOR_MACRO_IMAGE_BYTES)
    image[:40] = _name_bytes(group, "Macro group", 40)
    image[40:72] = _name_bytes(name, "Macro name", 32)
    image[72:76] = b"\x01\0" + count.to_bytes(2, "little")
    image[76:76 + len(macro.encoded)] = macro.encoded
    image[-2:] = (sum(image[:-2]) & 0xFFFF).to_bytes(2, "big")
    return bytes(image)


def build_vendor_macro_packets(source_image: bytes, profile_index: int,
                                logical_slot: int, layer: str = "primary") -> tuple[bytes, ...]:
    """Clone the vendor writer's 34 packets, without interpreting source data.

    This low-level research codec deliberately reproduces the captured writer,
    including its zero-filled final payload. It copies source bytes 0..1021;
    source bytes 1022..1037 are omitted. Use plan_keyboard_macro_upload() for
    validated new keyboard/mouse macros that fit this limitation.

    Keep the profile and visible-slot validation here. MC7 5.09 stores raw
    macro selectors before validating them and can otherwise erase a different
    flash slot when the final chunk commits.
    """
    if not isinstance(source_image, (bytes, bytearray)) or len(source_image) != VENDOR_MACRO_IMAGE_BYTES:
        raise ProtocolError("Vendor macro source image must contain exactly 1038 bytes")
    profile = _integer("Profile index", profile_index, 0, 4)
    slot = macro_slot(logical_slot, layer)
    image = bytes(source_image)
    packets = []
    for index in range(MACRO_READ_CHUNKS):
        prefix = bytes((0x10, 0x1C, 2, profile, slot, index))
        packets.append(prefix + bytes((sum(prefix) & 255,)) + bytes(57))
        payload = image[index * 62:(index + 1) * 62] if index < 16 else image[992:1022] + bytes(32)
        packets.append(b"\x10\x1d" + payload)
    return tuple(packets)


@dataclass(frozen=True)
class MacroUploadPlan:
    """An offline plan; the transaction owner must preserve/read back the slot."""

    profile_index: int
    logical_slot: int
    layer: str
    keyboard_macro: KeyboardMacro
    source_image: bytes
    packets: tuple[bytes, ...]
    assignment: bytes = NORMAL_MACRO_ASSIGNMENT

    @property
    def playback(self) -> str:
        return decode_macro_playback(self.assignment, int.from_bytes(self.source_image[74:76], "little"))

    @property
    def transmitted_payload(self) -> bytes:
        return b"".join(packet[2:] for packet in self.packets[1::2])

    @property
    def expected_read_payload(self) -> bytes:
        # Source reader returns 17×61 bytes, from the outgoing 1054 payload bytes.
        return self.transmitted_payload[:MACRO_READ_CHUNKS * MACRO_MAX_CHUNK_PAYLOAD]

    @property
    def omitted_source_bytes(self) -> bytes:
        return self.source_image[1022:]


def plan_keyboard_macro_upload(events: Sequence[InputEvent], *, profile_index: int,
                                logical_slot: int, layer: str = "primary",
                                name: str = "Swarm2 macro", group: str = "Swarm2",
                                repeat_count: int = 1, playback: str | None = None) -> MacroUploadPlan:
    """Plan a validated input macro upload, leaving every button untouched.

    Readback prediction is derived from the source transfer layout and must be
    checked on hardware. In particular, the source checksum is not transmitted.
    This API does not claim that a macro is stored, assigned, or playable.
    """
    macro = compile_keyboard_macro(events)
    mode = ("once" if repeat_count == 1 else "repeat") if playback is None else playback
    assignment, _ = macro_playback_metadata(mode, repeat_count)
    image = build_keyboard_macro_image(macro, name=name, group=group, repeat_count=repeat_count, playback=mode)
    packets = build_vendor_macro_packets(image, profile_index, logical_slot, layer)
    return MacroUploadPlan(profile_index, logical_slot, layer, macro, image, packets, assignment)
