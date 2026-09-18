"""Bridge local keyboard/mouse macros to MC7 images and button records.

All operations are pure. The helper owns transport locking, stale-baseline
checks, upload sequencing, readback, and recovery after partial writes.
"""

from collections.abc import Mapping
from dataclasses import replace

from .button_commands import BUTTON_SLOTS, KEY_CODES, LAYERS, ButtonState, replace_actions
from .configuration import Action, Configuration, Macro, MacroEvent
from .macro_commands import (
    InputEvent, KeyboardEvent, MouseEvent, MOUSE_CODES, MacroUploadPlan, build_keyboard_macro_image,
    build_vendor_macro_packets, decode_keyboard_macro, macro_slot,
    plan_keyboard_macro_upload, decode_macro_playback, NORMAL_MACRO_ASSIGNMENT,
    HELD_MACRO_ASSIGNMENT,
)
from .protocol import ProtocolError


_KEYS = {name.lower(): code for name, code in KEY_CODES.items()}
_KEY_NAMES = {code: name for name, code in KEY_CODES.items()}
_ALIASES = {"control": "ctrl", "return": "enter", "esc": "escape", "cmd": "meta",
            "command": "meta", "super": "meta", "option": "alt", "plus": "equal"}


def _validate_local_macro(macro: Macro) -> None:
    if not isinstance(macro, Macro):
        raise ProtocolError("Choose a local macro before uploading it")
    Configuration(macros=[macro]).validate()


def macro_to_keyboard_events(macro: Macro) -> tuple[InputEvent, ...]:
    """Interpret explicit delay events as additional delay after the prior key.

    Leading or trailing nonzero delays cannot currently be represented without
    changing the sequence. The historical function name includes supported
    left/right/middle/back/forward mouse clicks. Other mouse actions remain
    local drafts and fail before an upload plan is produced.
    """
    _validate_local_macro(macro)
    events: list[InputEvent] = []
    for event in macro.events:
        if event.kind == "delay":
            if not events:
                if event.delay_ms:
                    raise ProtocolError("A leading delay is unsupported in onboard macros")
            else:
                previous = events[-1]
                events[-1] = replace(previous, delay_after_ticks=previous.delay_after_ticks + event.delay_ms)
            continue
        if event.kind in ("mouse_down", "mouse_up"):
            if event.value not in MOUSE_CODES:
                raise ProtocolError(
                    "Onboard mouse macros support left, right, middle, back, and forward click only"
                )
            events.append(MouseEvent(event.value, event.kind == "mouse_down", event.delay_ms))
            continue
        if event.kind not in ("key_down", "key_up"):
            raise ProtocolError("Only supported key/mouse presses, releases, and delays can be uploaded")
        key = event.value.lower()
        code = _KEYS.get(_ALIASES.get(key, key))
        if code is None:
            raise ProtocolError(f"Unsupported onboard macro key: {event.value}")
        events.append(KeyboardEvent(code, event.kind == "key_down", event.delay_ms))
    return tuple(events)


def plan_macro_upload(macro: Macro, *, profile_index: int, logical_slot: int,
                      layer: str = "primary", group: str = "MC7 Studio") -> MacroUploadPlan:
    """Plan a local macro, retaining an existing supported group when supplied.

    The group must contain 1..39 printable ASCII characters, matching the
    encoder's terminated forty-byte field. For existing slots, the caller
    should pass the group from the validated device baseline on every edit.

    Compiler-generated delays re-encode identically after normalization. An
    imported alternate delay encoding can normalize differently, so callers
    must detect semantic no-ops before replanning an unchanged device macro.
    """
    if layer == "lcd" and macro.playback == "while_held":
        raise ProtocolError("LCD touch macros support once, repeat, and toggle playback")
    events = macro_to_keyboard_events(macro)
    plan = plan_keyboard_macro_upload(events, profile_index=profile_index,
        logical_slot=logical_slot, layer=layer, name=macro.name, group=group,
        repeat_count=macro.repeat, playback=macro.playback)
    # Normalization can add a tick, including at the editor's per-event or
    # total-duration boundary. Every accepted local upload must also be
    # representable when its verified device state is imported again.
    decode_device_macro(plan.expected_read_payload, profile_index=profile_index,
                        logical_slot=logical_slot, layer=layer,
                        assignment=plan.assignment)
    return plan


def _text_field(raw: bytes, label: str) -> str:
    text, separator, padding = raw.partition(b"\0")
    if (not separator or any(padding) or not text or not text.strip()
            or any(not 32 <= value <= 126 for value in text)):
        raise ProtocolError(f"Unsupported or unterminated onboard macro {label}")
    return text.decode("ascii")


def _selection(profile_index: int, logical_slot: int, layer: str) -> None:
    if type(profile_index) is not int or not 0 <= profile_index <= 4:
        raise ProtocolError("Macro profile index must be an integer in 0..4")
    macro_slot(logical_slot, layer)


def decode_device_macro(payload: bytes, *, profile_index: int, logical_slot: int,
                        layer: str = "primary", assignment: bytes) -> Macro:
    """Promote a supported, fully read macro to a local draft.

    This is semantic decoding of all seventeen payload chunks. The caller must
    establish the transfer's profile/slot/layer and retain its raw baseline.
    Other macro function IDs remain opaque. No source checksum is claimed: the
    vendor-derived transfer zero-fills that region on the connected firmware.
    """
    _selection(profile_index, logical_slot, layer)
    if not isinstance(assignment, (bytes, bytearray)) or assignment not in (NORMAL_MACRO_ASSIGNMENT, HELD_MACRO_ASSIGNMENT):
        raise ProtocolError("The onboard macro assignment uses an unsupported function record")
    if layer == "lcd" and assignment != NORMAL_MACRO_ASSIGNMENT:
        raise ProtocolError("LCD touch macros support once, repeat, and toggle playback")
    if not isinstance(payload, (bytes, bytearray)) or len(payload) != 1037:
        raise ProtocolError("Read all seventeen native macro chunks before importing the macro")
    raw = bytes(payload)
    if raw in (bytes(1037), b"\xff" * 1037):
        raise ProtocolError("The onboard macro slot is empty or erased")
    _text_field(raw[:40], "group")
    name = _text_field(raw[40:72], "name")
    if raw[72:74] != b"\x01\0":
        raise ProtocolError("Only onboard macros with time base 1 can be imported")
    count = int.from_bytes(raw[74:76], "little")
    playback = decode_macro_playback(assignment, count)
    if layer == "lcd" and playback == "while_held":
        raise ProtocolError("LCD touch macros support once, repeat, and toggle playback")
    decoded = decode_keyboard_macro(raw[76:1036])
    if not decoded.events or any(decoded.trailing_bytes) or any(raw[1022:]):
        raise ProtocolError("Unknown data follows the onboard macro's stop marker")
    if decoded.events[-1].delay_after_ticks:
        raise ProtocolError("Onboard macro trailing delays are not supported")
    events = []
    for event in decoded.events:
        if isinstance(event, KeyboardEvent) and event.key_code not in _KEY_NAMES:
            raise ProtocolError("The onboard macro uses a keyboard key unsupported by the local editor")
        if event.delay_after_ticks > 60000:
            raise ProtocolError("An onboard macro delay exceeds the local editor's per-event limit")
        category, value = (("mouse", event.button) if isinstance(event, MouseEvent)
                           else ("key", _KEY_NAMES[event.key_code]))
        events.append(MacroEvent(f"{category}_{'down' if event.pressed else 'up'}",
                                 value, event.delay_after_ticks))
    macro = Macro(id=f"device_p{profile_index+1}_{layer}_{logical_slot}", name=name,
                  events=events, repeat=max(1, count), playback=playback)
    _validate_local_macro(macro)
    return macro


def _validate_plan(plan: MacroUploadPlan) -> None:
    if not isinstance(plan, MacroUploadPlan):
        raise ProtocolError("A verified macro upload plan is required")
    _selection(plan.profile_index, plan.logical_slot, plan.layer)
    raw = plan.source_image
    if not isinstance(raw, bytes) or len(raw) != 1038:
        raise ProtocolError("Macro plan source image is malformed")
    reconstructed = build_keyboard_macro_image(plan.keyboard_macro,
        name=_text_field(raw[40:72], "name"), group=_text_field(raw[:40], "group"),
        repeat_count=max(1, int.from_bytes(raw[74:76], "little")), playback=plan.playback)
    packets = build_vendor_macro_packets(raw, plan.profile_index, plan.logical_slot, plan.layer)
    if reconstructed != raw or plan.packets != packets:
        raise ProtocolError("Macro upload plan does not match its validated source image")


def build_macro_button_report(state: ButtonState, plans: Mapping[int, MacroUploadPlan],
                               verified_readbacks: Mapping[int, bytes]) -> bytes:
    """Assign supported macros only after exact planned readback matches.

    Data0=0, Data1=1, ID=2, Type=7 is the vendor's normalized ordinary macro
    record. A repeat count of1 means once; counts2..999 live in the macro image.
    Count0 plus ID2 is Toggle; count0 plus ID1 is While held.
    Every other button, hidden slot, and opaque record is preserved exactly.
    """
    if not isinstance(plans, Mapping) or len(plans) > len(BUTTON_SLOTS):
        raise ProtocolError("Provide at most eleven macro button assignments")
    if not isinstance(verified_readbacks, Mapping):
        raise ProtocolError("Macro readbacks must map logical slots to exact payloads")
    if not isinstance(state, ButtonState):
        raise ProtocolError("Read the button layer before assigning macros")
    for logical_slot, plan in plans.items():
        if type(logical_slot) is not int or logical_slot not in BUTTON_SLOTS:
            raise ProtocolError("Choose a visible MC7 logical button slot")
        _validate_plan(plan)
        if (plan.profile_index, plan.logical_slot, plan.layer) != (state.profile_index, logical_slot, state.layer):
            raise ProtocolError("Macro upload plan and button baseline target different profile, layer, or slot")
        readback = verified_readbacks.get(logical_slot)
        if not isinstance(readback, (bytes, bytearray)) or readback != plan.expected_read_payload:
            raise ProtocolError("Read back the complete uploaded macro before assigning its button")
    # A macro is not an essential mouse click. Validate this replacement using
    # the ordinary button codec's existing baseline and click-preservation guard.
    records = list(replace_actions(state, {slot: Action("disabled", "") for slot in plans}))
    for logical_slot in plans:
        records[logical_slot] = plans[logical_slot].assignment
    return bytes((0x10, LAYERS[state.layer], state.profile_index)) + b"".join(records) + bytes(13)
