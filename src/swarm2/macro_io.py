"""Bounded macro reads and upload-before-assignment transactions."""

from dataclasses import dataclass

from .button_commands import BUTTON_SLOTS, decode_button_response
from .macro_commands import (MacroReadAssembler, TimingAdjustment, NORMAL_MACRO_ASSIGNMENT,
                             decode_macro_playback, macro_playback_metadata)
from .protocol import ProtocolError
from .transport import DeviceError

MACRO_ASSIGNMENT = NORMAL_MACRO_ASSIGNMENT


@dataclass(frozen=True)
class MacroTarget:
    """Exact data to retain or upload, including noncanonical stored encodings."""

    expected_read_payload: bytes
    timing_adjustments: tuple[TimingAdjustment, ...] = ()
    assignment: bytes = NORMAL_MACRO_ASSIGNMENT

    def __post_init__(self):
        if not isinstance(self.expected_read_payload, bytes) or len(self.expected_read_payload) != 1037:
            raise ProtocolError('A macro target requires the complete native payload')
        decode_macro_playback(self.assignment, int.from_bytes(self.expected_read_payload[74:76], 'little'))


def read_macro(transport, profile, logical_slot, layer='primary'):
    reader = MacroReadAssembler(profile, logical_slot, layer)
    while not reader.transfer_complete:
        transport.send(reader.read_request())
        reader.append(transport.get_feature(0x1D))
    result = reader.finish()
    if result.native_lengths != (64,) * 17:
        raise DeviceError('Macro response length differs from the verified native format')
    return result.raw


def prepare_macro_updates(transport, configuration, raw, macro_data, baseline_data):
    """Compile and validate all requested macros, then read their current slots.

    This function makes no writes. The caller must validate ordinary button
    edits and every predicted response before executing the returned updates.
    """
    from .macro_profiles import decode_device_macro, macro_to_keyboard_events, plan_macro_upload
    library = {macro.id: macro for macro in configuration.macros}
    plans, targets, current_slots = {}, {}, {}
    for binding in configuration.buttons:
        logical = BUTTON_SLOTS[binding.button_id - 1]
        for layer in ('primary', 'easy_shift'):
            action = getattr(binding, layer)
            if action.kind != 'macro':
                continue
            macro = library[action.value]
            assignment, count = macro_playback_metadata(macro.playback, macro.repeat)
            key = f'{layer}:{logical}'
            state = decode_button_response(raw[layer], configuration.profile_slot-1, layer)
            group = 'MC7 Studio'
            if state.records[logical][3] == 7:
                current, previous = macro_data.get(key), baseline_data.get(key)
                if not current or not previous:
                    raise DeviceError('Read the assigned macro successfully before replacing it')
                if current != previous:
                    raise DeviceError('The assigned macro changed since the last read; read the mouse again')
                before = current_slots[(layer, logical)] = bytes.fromhex(current)
                try:
                    stored = decode_device_macro(before, profile_index=configuration.profile_slot-1,
                        logical_slot=logical, layer=layer, assignment=state.records[logical])
                except ValueError:
                    # Replacing an unsupported assignment is explicit; preserve
                    # its raw baseline but do not reuse unrecognized metadata.
                    stored = None
                if stored is not None:
                    group = before[:40].partition(b'\0')[0].decode('ascii')
                    _, stored_count = macro_playback_metadata(stored.playback, stored.repeat)
                    if (macro.name == stored.name and count == stored_count
                            and macro_to_keyboard_events(macro) == macro_to_keyboard_events(stored)):
                        # Mode-only held/toggle changes use the same image and
                        # retain any valid noncanonical delay representation.
                        targets[(layer, logical)] = MacroTarget(before, assignment=assignment)
                        continue
            plan = plan_macro_upload(macro, profile_index=configuration.profile_slot-1,
                                     logical_slot=logical, layer=layer, group=group)
            plans[(layer, logical)] = plan
            targets[(layer, logical)] = MacroTarget(plan.expected_read_payload,
                                                    plan.keyboard_macro.timing_adjustments, plan.assignment)
    updates, disabling = {}, {}
    for (layer, logical), plan in plans.items():
        state = decode_button_response(raw[layer], configuration.profile_slot-1, layer)
        bound = state.records[logical][3] == 7
        if bound:
            before = current_slots[(layer, logical)]
        else:
            before = read_macro(transport, configuration.profile_slot-1, logical, layer)
        if before != plan.expected_read_payload:
            updates[(layer, logical)] = plan
            if bound:
                # Prevent a partially replaced sequence from remaining callable.
                if layer not in disabling:
                    disabling[layer] = bytearray(bytes((0x10, 0x15 if layer == 'primary' else 0x16,
                                                       configuration.profile_slot-1)) + b''.join(state.records) + bytes(13))
                disabling[layer][3+logical*4:7+logical*4] = bytes(4)
    return targets, updates, {layer: bytes(packet) for layer, packet in disabling.items()}


def prepare_lcd_macro_updates(transport, configuration, raw_lcd, macro_data, baseline_data):
    """Plan macro images for the twelve coordinate-bound LCD touch slots.

    An existing active tile supplies a stale-checked raw baseline. A new tile
    reads its currently hidden slot before any write. If active macro data must
    change, the returned LCD report temporarily replaces those tiles with
    Empty; the caller must send the final requested layout after the upload.
    """
    from .lcd_commands import build_lcd_report, decode_lcd_response
    from .macro_profiles import decode_device_macro, macro_to_keyboard_events, plan_macro_upload

    profile = configuration.profile_slot - 1
    state = decode_lcd_response(raw_lcd, profile)
    library = {macro.id: macro for macro in configuration.macros}
    bindings = configuration.display.macro_bindings
    active = {
        page * 4 + cell
        for page, current in enumerate(state.pages[:min(3, state.page_count)])
        for cell, widget in enumerate(current.slots)
        if widget is not None and widget.key == "macro"
    }
    plans, targets, current_slots = {}, {}, {}
    for page, row in enumerate(configuration.display.pages):
        for cell, widget in enumerate(row):
            if widget != "macro":
                continue
            logical = page * 4 + cell
            macro_id = (bindings[page][cell]
                        if page < len(bindings) and cell < len(bindings[page]) else None)
            if macro_id is None:
                if logical not in active:
                    raise DeviceError("Choose a macro for every new LCD Macro tile")
                # A hardware read may retain an unsupported or unreadable
                # active image as an opaque tile. Leaving it in the same
                # coordinate performs no selector write.
                continue
            macro = library.get(macro_id)
            if macro is None:
                raise DeviceError("Every LCD Macro tile must reference a saved local macro")
            if macro.playback == "while_held":
                raise DeviceError("LCD touch macros support once, repeat, and toggle playback")
            key = f"lcd:{logical}"
            group = "MC7 Studio"
            if logical in active:
                current, previous = macro_data.get(key), baseline_data.get(key)
                if not current or not previous:
                    raise DeviceError("Read the active LCD macro successfully before replacing it")
                if current != previous:
                    raise DeviceError("The LCD macro changed since the last read; read the mouse again")
                before = current_slots[logical] = bytes.fromhex(current)
                try:
                    stored = decode_device_macro(
                        before, profile_index=profile, logical_slot=logical,
                        layer="lcd", assignment=NORMAL_MACRO_ASSIGNMENT)
                except ValueError:
                    stored = None
                if stored is not None:
                    group = before[:40].partition(b"\0")[0].decode("ascii")
                    _, requested_count = macro_playback_metadata(macro.playback, macro.repeat)
                    _, stored_count = macro_playback_metadata(stored.playback, stored.repeat)
                    if (macro.name == stored.name and requested_count == stored_count
                            and macro_to_keyboard_events(macro) == macro_to_keyboard_events(stored)):
                        targets[("lcd", logical)] = MacroTarget(before)
                        continue
            plan = plan_macro_upload(
                macro, profile_index=profile, logical_slot=logical,
                layer="lcd", group=group)
            plans[("lcd", logical)] = plan
            targets[("lcd", logical)] = MacroTarget(
                plan.expected_read_payload, plan.keyboard_macro.timing_adjustments)

    updates, suspend_cells = {}, set()
    for (_, logical), plan in plans.items():
        before = (current_slots[logical] if logical in active
                  else read_macro(transport, profile, logical, "lcd"))
        if before != plan.expected_read_payload:
            updates[("lcd", logical)] = plan
            if logical in active:
                suspend_cells.add(logical)

    disabling = {}
    if suspend_cells:
        pages = {}
        for logical in suspend_cells:
            page, cell = divmod(logical, 4)
            pages.setdefault(page, list(state.pages[page].slots))[cell] = "empty"
        disabling["lcd"] = build_lcd_report(state, pages=pages)
    return targets, updates, disabling


def execute_macro_updates(transport, updates, disabling):
    """Store and read back every changed macro before the caller binds buttons."""
    from .button_commands import build_button_read_request
    for layer, packet in disabling.items():
        if layer == "lcd":
            from .lcd_commands import build_lcd_read_request, decode_lcd_response
            transport.send(packet)
            profile = packet[3]
            transport.send(build_lcd_read_request(profile))
            after = decode_lcd_response(transport.get_feature(0x25), profile)
            predicted = bytearray(packet[:61])
            predicted[2] = 0
            expected = decode_lcd_response(predicted, profile)
            if after.signature != expected.signature:
                raise DeviceError("Could not suspend the old LCD macro tile before replacing its data")
            continue
        transport.send(packet)
        transport.send(build_button_read_request(packet[2], layer))
        after = decode_button_response(transport.get_feature(packet[1]), packet[2], layer)
        if b''.join(after.records) != packet[3:51]:
            raise DeviceError('Could not suspend the old macro assignment before replacing its data')
    for (layer, logical), plan in updates.items():
        for packet in plan.packets:
            transport.send(packet)
        actual = read_macro(transport, plan.profile_index, logical, layer)
        if actual != plan.expected_read_payload:
            raise DeviceError('Uploaded macro did not match complete readback; it was not assigned')
