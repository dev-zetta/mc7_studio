"""Macro editor/upload boundary and guarded assignments, without device I/O."""

from dataclasses import replace
import unittest

from swarm2.button_commands import decode_button_response
from swarm2.configuration import ConfigurationError, Macro, MacroEvent
from swarm2.macro_commands import KeyboardEvent, MouseEvent, TimingAdjustment
from swarm2.macro_profiles import (
    NORMAL_MACRO_ASSIGNMENT, build_macro_button_report, decode_device_macro,
    macro_to_keyboard_events, plan_macro_upload,
)
from swarm2.protocol import ProtocolError
from tests.test_button_commands import PRIMARY, SECONDARY


def tap(key="F13", delay=50, **kwargs):
    return Macro(name="Readback test", events=[MacroEvent("key_down", key, delay),
                                             MacroEvent("key_up", key, 0)], **kwargs)


def native_payload():
    # Captured 2026-09-15 from the inactive, unassigned primary slot5 in
    # profile index1, after uploading through the vendor-derived packet plan.
    return (b"MC7 Studio" + bytes(30)
            + b"Native macro 0123456789abcdefgh" + bytes(1)
            + bytes.fromhex("010001000168b2680000") + bytes(955))


class MacroProfileTests(unittest.TestCase):
    def test_lcd_touch_macro_roundtrip_supports_proven_playback_modes(self):
        for playback, repeat in (("once", 999), ("repeat", 3), ("toggle", 999)):
            with self.subTest(playback=playback):
                source = tap(playback=playback, repeat=repeat)
                plan = plan_macro_upload(source, profile_index=4, logical_slot=11, layer="lcd")
                decoded = decode_device_macro(
                    plan.expected_read_payload, profile_index=4, logical_slot=11,
                    layer="lcd", assignment=NORMAL_MACRO_ASSIGNMENT)
                self.assertEqual(decoded.id, "device_p5_lcd_11")
                self.assertEqual((decoded.playback, decoded.repeat),
                                 (playback, 1 if playback in ("once", "toggle") else repeat))
        for playback in ("while_held",):
            with self.subTest(playback=playback), self.assertRaisesRegex(
                    ProtocolError, "once, repeat, and toggle"):
                plan_macro_upload(tap(playback=playback), profile_index=0,
                                  logical_slot=0, layer="lcd")

    def test_native_readback_import_and_replan_are_exact(self):
        payload = native_payload()
        self.assertEqual(len(payload), 1037)
        macro = decode_device_macro(payload, profile_index=1, logical_slot=5,
                                    assignment=NORMAL_MACRO_ASSIGNMENT)
        self.assertEqual(macro.id, "device_p2_primary_5")
        self.assertEqual(macro.name, "Native macro 0123456789abcdefgh")
        self.assertEqual((macro.playback, macro.repeat), ("once", 1))
        self.assertEqual(macro.events, [MacroEvent("key_down", "F13", 50),
                                       MacroEvent("key_up", "F13", 0)])
        self.assertEqual(plan_macro_upload(macro, profile_index=1, logical_slot=5)
                         .expected_read_payload, payload)

    def test_supplied_vendor_group_preserves_payload_and_survives_edits(self):
        group = "Vendor game presets"
        payload = group.encode("ascii").ljust(40, b"\0") + native_payload()[40:]
        macro = decode_device_macro(payload, profile_index=1, logical_slot=5,
                                    assignment=NORMAL_MACRO_ASSIGNMENT)
        plan = plan_macro_upload(macro, profile_index=1, logical_slot=5, group=group)
        self.assertEqual(plan.expected_read_payload, payload)
        changed = replace(macro, events=tap("F14").events)
        edited = plan_macro_upload(changed, profile_index=1, logical_slot=5, group=group)
        self.assertEqual(edited.expected_read_payload[:40], payload[:40])
        self.assertEqual(edited.keyboard_macro.events[0].key_code, 0x69)
        self.assertNotEqual(edited.expected_read_payload[76:82], payload[76:82])

    def test_supplied_group_uses_the_existing_bounded_ascii_validation(self):
        plan = plan_macro_upload(tap(), profile_index=0, logical_slot=5, group="x"*39)
        self.assertEqual(plan.expected_read_payload[:40], b"x"*39+b"\0")
        for group in ("", " ", "x"*40, "Name\0suffix", "Name\n", "日本語", None, 3):
            with self.subTest(group=group), self.assertRaises(ProtocolError):
                plan_macro_upload(tap(), profile_index=0, logical_slot=5, group=group)

    def test_compiler_generated_normalized_delays_reencode_identically(self):
        delays = (0, 1, 19, 20, 21, 127, 128, 139, 140, 141, 159, 160, 161,
                  500, 501, 999, 59980, 59981, 59999)
        for delay in delays:
            with self.subTest(delay=delay):
                plan = plan_macro_upload(tap(delay=delay), profile_index=0, logical_slot=5)
                macro = decode_device_macro(plan.expected_read_payload, profile_index=0,
                    logical_slot=5, assignment=NORMAL_MACRO_ASSIGNMENT)
                repeated = plan_macro_upload(macro, profile_index=0, logical_slot=5)
                self.assertEqual(repeated.expected_read_payload, plan.expected_read_payload)
                self.assertEqual(repeated.keyboard_macro.timing_adjustments, ())

    def test_alternate_native_delay_encoding_requires_semantic_noop_preservation(self):
        # The decoder accepts source-documented scale2 and zero short delays.
        # Recompilation applies vendor normalization, so the transaction owner
        # must retain an unchanged native image before considering a new plan.
        for encoded, decoded_delay, resulting_delay in (
                ("016800020200e4680000", 200, 201),
                ("016880680000", 0, 1)):
            raw_events = bytes.fromhex(encoded)
            payload = native_payload()[:76] + raw_events + bytes(961-len(raw_events))
            with self.subTest(encoded=encoded):
                macro = decode_device_macro(payload, profile_index=0, logical_slot=5,
                                            assignment=NORMAL_MACRO_ASSIGNMENT)
                self.assertEqual(macro.events[0].delay_ms, decoded_delay)
                plan = plan_macro_upload(macro, profile_index=0, logical_slot=5)
                self.assertEqual(plan.keyboard_macro.timing_adjustments,
                                 (TimingAdjustment(0, decoded_delay, resulting_delay),))
                self.assertNotEqual(plan.expected_read_payload, payload)

    def test_explicit_delays_attach_to_previous_key_and_report_normalization(self):
        macro = Macro(name="Explicit delay", events=[MacroEvent("delay", "", 0),
            MacroEvent("key_down", "A", 100), MacroEvent("delay", "", 400),
            MacroEvent("key_up", "A", 0), MacroEvent("delay", "", 0)])
        self.assertEqual(macro_to_keyboard_events(macro),
                         (KeyboardEvent(4, True, 500), KeyboardEvent(4, False)))
        plan = plan_macro_upload(macro, profile_index=0, logical_slot=5)
        self.assertEqual(plan.keyboard_macro.timing_adjustments,
                         (TimingAdjustment(0, 500, 501),))
        decoded = decode_device_macro(plan.expected_read_payload, profile_index=0,
            logical_slot=5, assignment=NORMAL_MACRO_ASSIGNMENT)
        self.assertEqual(decoded.events[0].delay_ms, 501)

    def test_mac_and_common_key_aliases_use_physical_hid_usages(self):
        for name, code in (("Cmd", 0xE3), ("Command", 0xE3), ("Super", 0xE3),
                           ("Option", 0xE2), ("Control", 0xE0), ("Esc", 0x29),
                           ("Return", 0x28), ("Plus", 0x2E), ("f24", 0x73)):
            with self.subTest(key=name):
                plan = plan_macro_upload(tap(name), profile_index=0, logical_slot=5)
                self.assertEqual(plan.keyboard_macro.events,
                                 (KeyboardEvent(code, True, 50), KeyboardEvent(code, False)))

    def test_once_ignores_stale_repeat_setting_and_finite_repeat_is_in_image(self):
        once = plan_macro_upload(tap(repeat=999), profile_index=0, logical_slot=5)
        self.assertEqual(once.expected_read_payload[74:76], b"\x01\0")
        plan = plan_macro_upload(tap(playback="repeat", repeat=999),
                                 profile_index=4, logical_slot=11, layer="easy_shift")
        self.assertEqual(plan.expected_read_payload[74:76], b"\xe7\x03")
        decoded = decode_device_macro(plan.expected_read_payload, profile_index=4,
            logical_slot=11, layer="easy_shift", assignment=NORMAL_MACRO_ASSIGNMENT)
        self.assertEqual(decoded.id, "device_p5_easy_shift_11")
        self.assertEqual((decoded.playback, decoded.repeat), ("repeat", 999))

    def test_unknown_actions_alias_double_press_and_unrepresentable_delays_rejected(self):
        invalid = [Macro(name="Empty"), tap(delay=60000),
            Macro(events=[MacroEvent("delay", "", 1), *tap().events]),
            Macro(events=[*tap().events, MacroEvent("delay", "", 1)]),
            Macro(events=[MacroEvent("mouse_down", "scroll_up", 50), MacroEvent("mouse_up", "scroll_up", 0)]),
            Macro(events=[MacroEvent("key_down", "Ctrl", 1), MacroEvent("key_down", "Control", 1),
                          MacroEvent("key_up", "Control", 1), MacroEvent("key_up", "Ctrl", 0)]),
            Macro(events=[MacroEvent("key_down", "A", 60000), MacroEvent("delay", "", 1),
                          MacroEvent("key_up", "A", 0)])]
        for macro in invalid:
            with self.subTest(macro=macro), self.assertRaises((ProtocolError, ConfigurationError)):
                plan_macro_upload(macro, profile_index=0, logical_slot=5)
        for playback in ("held", "repeat_forever", "script"):
            with self.subTest(playback=playback), self.assertRaises((ProtocolError, ConfigurationError)):
                plan_macro_upload(tap(playback=playback), profile_index=0, logical_slot=5)

    def test_macro_name_is_bounded_printable_ascii_before_upload(self):
        for name in ("", "x"*32, "Hello\nthere", "日本語"):
            with self.subTest(name=name), self.assertRaises((ProtocolError, ConfigurationError)):
                plan_macro_upload(replace(tap(), name=name), profile_index=0, logical_slot=5)

    def test_local_mouse_clicks_and_explicit_delays_roundtrip_with_distinct_event_types(self):
        for button in ("left", "right", "middle", "back", "forward"):
            macro = Macro(name="Mouse click", events=[MacroEvent("mouse_down", button, 25),
                MacroEvent("delay", "", 25), MacroEvent("mouse_up", button, 0)])
            with self.subTest(button=button):
                plan = plan_macro_upload(macro, profile_index=0, logical_slot=5)
                self.assertEqual(plan.keyboard_macro.events,
                                 (MouseEvent(button, True, 50), MouseEvent(button, False)))
                stored = decode_device_macro(plan.expected_read_payload, profile_index=0,
                    logical_slot=5, assignment=NORMAL_MACRO_ASSIGNMENT)
                self.assertEqual(stored.events, [MacroEvent("mouse_down", button, 50),
                                                MacroEvent("mouse_up", button, 0)])
                self.assertEqual(plan_macro_upload(stored, profile_index=0, logical_slot=5)
                                 .expected_read_payload, plan.expected_read_payload)

    def test_mixed_keyboard_mouse_macro_preserves_aliases_repeats_and_timing(self):
        macro = Macro(name="Mixed input", playback="repeat", repeat=3, events=[
            MacroEvent("key_down", "Control", 1), MacroEvent("mouse_down", "middle", 100),
            MacroEvent("delay", "", 400), MacroEvent("mouse_up", "middle", 1),
            MacroEvent("key_up", "Control", 0)])
        plan = plan_macro_upload(macro, profile_index=4, logical_slot=11, layer="easy_shift")
        self.assertEqual(plan.keyboard_macro.events, (KeyboardEvent(0xE0, True, 1),
            MouseEvent("middle", True, 501), MouseEvent("middle", False, 1), KeyboardEvent(0xE0, False)))
        self.assertEqual(plan.keyboard_macro.timing_adjustments, (TimingAdjustment(1, 500, 501),))
        stored = decode_device_macro(plan.expected_read_payload, profile_index=4,
            logical_slot=11, layer="easy_shift", assignment=NORMAL_MACRO_ASSIGNMENT)
        self.assertEqual((stored.repeat, stored.playback), (3, "repeat"))
        self.assertEqual(stored.events[1], MacroEvent("mouse_down", "middle", 501))
        self.assertEqual(stored.events[0].value, "Ctrl")

    def test_scrolling_stays_local_only(self):
        for button in ("scroll_up", "scroll_down"):
            value = Macro(events=[MacroEvent("mouse_down", button, 50), MacroEvent("mouse_up", button, 0)])
            with self.subTest(button=button), self.assertRaises((ProtocolError, ConfigurationError)):
                plan_macro_upload(value, profile_index=0, logical_slot=5)

    def test_partial_erased_or_unknown_mode_payload_never_promotes_to_editor(self):
        for payload in (b"", native_payload()[:-1], native_payload()+b"\0", bytes(1037), b"\xff"*1037):
            with self.subTest(length=len(payload)), self.assertRaises(ProtocolError):
                decode_device_macro(payload, profile_index=1, logical_slot=5,
                                    assignment=NORMAL_MACRO_ASSIGNMENT)
        for record in (bytes.fromhex("00010007"), bytes.fromhex("00010107"),
                       bytes.fromhex("00000207"), bytes.fromhex("00000101"), "00010207"):
            with self.subTest(record=record), self.assertRaises(ProtocolError):
                decode_device_macro(native_payload(), profile_index=1, logical_slot=5,
                                    assignment=record)

    def test_unknown_metadata_events_and_nonzero_trailers_stay_opaque(self):
        for start, replacement in ((0, b"\xff"), (10, b"\0X"), (40, b"\n"),
                                   (72, b"\x02\0"), (74, b"\xff\xff"), (74, b"\xe8\x03"),
                                   (77, b"\xf0"), (78, b"\x32"), (82, b"\x01"),
                                   (1022, b"\x01"), (1036, b"\x01")):
            raw = bytearray(native_payload())
            raw[start:start+len(replacement)] = replacement
            with self.subTest(offset=start), self.assertRaises((ProtocolError, ConfigurationError)):
                decode_device_macro(raw, profile_index=1, logical_slot=5,
                                    assignment=NORMAL_MACRO_ASSIGNMENT)

    def test_assignment_changes_only_selected_records_preserving_hidden_and_opaque(self):
        raw = bytearray(PRIMARY)
        raw[4+8*4:8+8*4] = bytes.fromhex("7a12347e")
        state = decode_button_response(raw, 0)
        plan = plan_macro_upload(tap(), profile_index=0, logical_slot=5)
        packet = build_macro_button_report(state, {5: plan}, {5: plan.expected_read_payload})
        records = tuple(packet[3+4*i:7+4*i] for i in range(12))
        self.assertEqual(len(packet), 64)
        self.assertEqual(packet[:3], bytes.fromhex("101500"))
        self.assertEqual(packet[51:], bytes(13))
        self.assertEqual(records[5], bytes.fromhex("00010207"))
        self.assertEqual([i for i in range(12) if records[i] != state.records[i]], [5])
        self.assertEqual(records[8], bytes.fromhex("7a12347e"))
        self.assertEqual(records[9], bytes.fromhex("00000603"))

    def test_easy_shift_assignments_have_independent_slots_and_preserve_primary_clicks(self):
        state = decode_button_response(SECONDARY, 0, "easy_shift")
        plans = {slot: plan_macro_upload(tap(), profile_index=0, logical_slot=slot,
                                         layer="easy_shift") for slot in (0, 5, 11)}
        packet = build_macro_button_report(state, plans,
                                          {slot: plan.expected_read_payload for slot, plan in plans.items()})
        self.assertEqual(packet[:3], bytes.fromhex("101600"))
        for slot in range(12):
            self.assertEqual(packet[3+4*slot:7+4*slot],
                             NORMAL_MACRO_ASSIGNMENT if slot in plans else state.records[slot])

    def test_missing_short_wrong_or_stale_readback_cannot_assign(self):
        state = decode_button_response(PRIMARY, 0)
        plan = plan_macro_upload(tap(), profile_index=0, logical_slot=5)
        other = plan_macro_upload(tap("F14"), profile_index=0, logical_slot=5)
        for reads in ({}, {5: b""}, {5: plan.expected_read_payload[:-1]},
                      {5: other.expected_read_payload}, {6: plan.expected_read_payload}):
            with self.subTest(reads=reads), self.assertRaisesRegex(ProtocolError, "Read back"):
                build_macro_button_report(state, {5: plan}, reads)

    def test_mismatched_target_or_forged_plan_and_baseline_cannot_assign(self):
        state = decode_button_response(PRIMARY, 0)
        plan = plan_macro_upload(tap(), profile_index=0, logical_slot=5)
        for changed in (replace(plan, profile_index=1), replace(plan, logical_slot=6),
                        replace(plan, layer="easy_shift"), replace(plan, packets=plan.packets[:-2]),
                        replace(plan, source_image=plan.source_image[:-1]+b"\xff"),
                        replace(plan, keyboard_macro=replace(plan.keyboard_macro, events=()))):
            with self.subTest(plan=changed), self.assertRaises(ProtocolError):
                build_macro_button_report(state, {5: changed}, {5: changed.expected_read_payload})
        with self.assertRaisesRegex(ProtocolError, "baseline records"):
            build_macro_button_report(replace(state, records=(bytes(4),)*12),
                                      {5: plan}, {5: plan.expected_read_payload})

    def test_primary_left_right_clicks_and_hidden_slot_are_protected(self):
        state = decode_button_response(PRIMARY, 0)
        for slot in (0, 1):
            plan = plan_macro_upload(tap(), profile_index=0, logical_slot=slot)
            with self.subTest(slot=slot), self.assertRaisesRegex(ProtocolError, "left and right click"):
                build_macro_button_report(state, {slot: plan}, {slot: plan.expected_read_payload})
        plan = plan_macro_upload(tap(), profile_index=0, logical_slot=5)
        for slot in (9, True, "5", 12):
            with self.subTest(slot=slot), self.assertRaises(ProtocolError):
                build_macro_button_report(state, {slot: plan}, {slot: plan.expected_read_payload})


if __name__ == "__main__":
    unittest.main()
