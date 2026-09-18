"""Source-derived playback metadata and guarded modes, without macro execution."""

from dataclasses import replace
import unittest
from unittest.mock import patch

from swarm2.button_commands import BUTTON_SLOTS, decode_button_response
from swarm2.configuration import Action, Macro, MacroEvent
from swarm2.hardware import transact
from swarm2.macro_commands import (
    HELD_MACRO_ASSIGNMENT, NORMAL_MACRO_ASSIGNMENT, KeyboardEvent,
    build_keyboard_macro_image, compile_keyboard_macro, decode_macro_playback,
    macro_playback_metadata, plan_keyboard_macro_upload,
)
from swarm2.macro_io import MacroTarget
from swarm2.macro_profiles import build_macro_button_report, decode_device_macro, plan_macro_upload
from swarm2.protocol import ProtocolError
from swarm2.service import DeviceService
from swarm2.transport import DeviceError
from tests.test_button_commands import PRIMARY
from tests.test_macro_integration import MacroTransport


def mixed(mode, *, macro_id="mode_macro", repeat=999):
    return Macro(id=macro_id, name="Mode test", playback=mode, repeat=repeat, events=[
        MacroEvent("key_down", "F13", 1), MacroEvent("mouse_down", "middle", 500),
        MacroEvent("mouse_up", "middle", 1), MacroEvent("key_up", "F13", 0)])


class MacroModeCodecTests(unittest.TestCase):
    def test_source_mode_vectors_keep_finite_api_compatible(self):
        events = (KeyboardEvent(0x68, True, 50), KeyboardEvent(0x68, False))
        vectors = (("once", 999, "00010207", 1, "once"),
                   ("repeat", 7, "00010207", 7, "repeat"),
                   ("while_held", 999, "00010107", 0, "while_held"),
                   ("toggle", 999, "00010207", 0, "toggle"))
        for mode, repeat, record, count, decoded in vectors:
            with self.subTest(mode=mode):
                self.assertEqual(macro_playback_metadata(mode, repeat), (bytes.fromhex(record), count))
                plan = plan_keyboard_macro_upload(events, profile_index=0, logical_slot=5,
                                                  playback=mode, repeat_count=repeat)
                self.assertEqual(plan.assignment.hex(), record)
                self.assertEqual(plan.playback, decoded)
                self.assertEqual(plan.source_image[72:76], b"\x01\0"+count.to_bytes(2, "little"))
                self.assertEqual(plan.expected_read_payload[76:82], bytes.fromhex("0168b2680000"))
        for count in (1, 7, 999):
            original = plan_keyboard_macro_upload(events, profile_index=0, logical_slot=5, repeat_count=count)
            self.assertEqual(original.assignment, NORMAL_MACRO_ASSIGNMENT)
            self.assertEqual(int.from_bytes(original.source_image[74:76], "little"), count)

    def test_invalid_count_mode_or_assignment_combinations_are_rejected(self):
        compiled = compile_keyboard_macro((KeyboardEvent(4, True, 50), KeyboardEvent(4, False)))
        for mode in ("", "hold", "infinite", 1, []):
            with self.subTest(mode=mode), self.assertRaises(ProtocolError):
                build_keyboard_macro_image(compiled, playback=mode)
        for count in (-1, 0, 1000, True, 1.0):
            for mode in (None, "while_held", "toggle"):
                with self.subTest(count=count, mode=mode), self.assertRaises(ProtocolError):
                    build_keyboard_macro_image(compiled, repeat_count=count, playback=mode)
        invalid = ((HELD_MACRO_ASSIGNMENT, 1), (HELD_MACRO_ASSIGNMENT, 999),
                   (NORMAL_MACRO_ASSIGNMENT, 1000), (NORMAL_MACRO_ASSIGNMENT, True),
                   (bytes.fromhex("00010007"), 0), (bytes.fromhex("00010307"), 0),
                   (bytes.fromhex("00000207"), 0), ("00010207", 0))
        for assignment, count in invalid:
            with self.subTest(assignment=assignment, count=count), self.assertRaises(ProtocolError):
                decode_macro_playback(assignment, count)

    def test_held_and_toggle_import_preserves_mode_group_and_effective_events(self):
        for mode in ("while_held", "toggle"):
            with self.subTest(mode=mode):
                plan = plan_macro_upload(mixed(mode), profile_index=4, logical_slot=10,
                                         layer="easy_shift", group="Vendor group")
                decoded = decode_device_macro(plan.expected_read_payload, profile_index=4,
                    logical_slot=10, layer="easy_shift", assignment=plan.assignment)
                self.assertEqual(decoded.playback, mode)
                self.assertEqual(decoded.repeat, 1)  # No repeat preference is stored in these modes.
                self.assertEqual(decoded.events[1], MacroEvent("mouse_down", "middle", 501))
                again = plan_macro_upload(decoded, profile_index=4, logical_slot=10,
                                         layer="easy_shift", group="Vendor group")
                self.assertEqual(again.assignment, plan.assignment)
                self.assertEqual(again.expected_read_payload, plan.expected_read_payload)
                self.assertEqual(again.keyboard_macro.timing_adjustments, ())

    def test_mode_specific_assignment_requires_validated_plan_and_exact_readback(self):
        state = decode_button_response(PRIMARY, 0)
        plans = {5: plan_macro_upload(mixed("while_held"), profile_index=0, logical_slot=5),
                 6: plan_macro_upload(mixed("toggle"), profile_index=0, logical_slot=6)}
        packet = build_macro_button_report(state, plans, {slot: plan.expected_read_payload for slot, plan in plans.items()})
        self.assertEqual(packet[23:27], HELD_MACRO_ASSIGNMENT)
        self.assertEqual(packet[27:31], NORMAL_MACRO_ASSIGNMENT)
        for slot in range(12):
            if slot not in plans:
                self.assertEqual(packet[3+4*slot:7+4*slot], state.records[slot])
        finite = plan_macro_upload(mixed("repeat"), profile_index=0, logical_slot=5)
        forged = replace(finite, assignment=HELD_MACRO_ASSIGNMENT)
        with self.assertRaisesRegex(ProtocolError, "zero image loop count"):
            build_macro_button_report(state, {5: forged}, {5: forged.expected_read_payload})
        with self.assertRaises(ProtocolError):
            MacroTarget(finite.expected_read_payload, assignment=HELD_MACRO_ASSIGNMENT)
        with self.assertRaises(ProtocolError):
            MacroTarget(plans[5].expected_read_payload[:-1], assignment=HELD_MACRO_ASSIGNMENT)


class MacroModeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.transport = MacroTransport()
        self.service = DeviceService()
        helper = patch.object(DeviceService, "_call", side_effect=lambda request: transact(request, lambda _: self.transport))
        helper.start()
        self.addCleanup(helper.stop)

    def read(self):
        return self.service.read("fixture-mc7", 1)

    def bind(self, snapshot, macro, layer="primary", logical=5):
        snapshot["configuration"].macros.append(macro)
        setattr(snapshot["configuration"].buttons[BUTTON_SLOTS.index(logical)], layer, Action("macro", macro.id))

    def apply(self, snapshot):
        return self.service.apply_section("fixture-mc7", snapshot["configuration"], "buttons", snapshot["baseline"])

    def test_all_modes_upload_import_and_noop_in_both_layers(self):
        for mode in ("once", "repeat", "while_held", "toggle"):
            for layer in ("primary", "easy_shift"):
                with self.subTest(mode=mode, layer=layer):
                    self.transport = MacroTransport()
                    snapshot = self.read()
                    value = mixed(mode, repeat=3)
                    self.bind(snapshot, value, layer)
                    result = self.apply(snapshot)
                    assignment, count = macro_playback_metadata(mode, 3)
                    self.assertTrue(result["summary"]["changed"])
                    self.assertEqual(self.transport.payload_writes, 17)
                    self.assertEqual(self.transport.record(layer, 5), assignment)
                    actual = bytes.fromhex(result["baseline"]["macro_data"][f"{layer}:5"])
                    self.assertEqual(int.from_bytes(actual[74:76], "little"), count)
                    self.assertEqual(result["configuration"].macros[0].id, value.id)
                    self.assertEqual(result["configuration"].macros[0].playback, mode)
                    fresh = self.read()
                    self.assertEqual(fresh["configuration"].macros[0].playback, mode)
                    self.assertEqual(fresh["errors"], {})
                    self.transport.writes.clear()
                    self.transport.payload_writes = 0
                    no_op = self.apply(result)
                    self.assertFalse(no_op["summary"]["changed"])
                    self.assertEqual(self.transport.writes, [])

    def test_held_toggle_switch_preserves_alternate_image_and_changes_only_assignment(self):
        for start, finish in (("while_held", "toggle"), ("toggle", "while_held")):
            with self.subTest(start=start):
                self.transport = MacroTransport()
                plan = plan_macro_upload(mixed(start), profile_index=0, logical_slot=5)
                payload = bytearray(plan.expected_read_payload)
                payload[:40] = b"Vendor group".ljust(40, b"\0")
                payload[76:1036] = bytes(960)
                # Source-supported alternate gap200 would recompile to201.
                payload[76:86] = bytes.fromhex("016800020200e4680000")
                self.transport.seed(None, payload=bytes(payload), assignment=plan.assignment)
                snapshot = self.read()
                snapshot["configuration"].macros[0].playback = finish
                result = self.apply(snapshot)
                self.assertTrue(result["summary"]["changed"])
                self.assertEqual(self.transport.payload_writes, 0)
                self.assertEqual([packet[1] for packet in self.transport.writes], [0x15])
                self.assertEqual(self.transport.record(), macro_playback_metadata(finish)[0])
                self.assertEqual(result["baseline"]["macro_data"]["primary:5"], bytes(payload).hex())
                self.assertEqual(result["configuration"].macros[0].playback, finish)
                self.assertEqual(result["macro_timing_adjustments"], [])

    def test_finite_infinite_replacements_disable_upload_readback_and_rebind(self):
        for start, finish in (("repeat", "while_held"), ("toggle", "once")):
            with self.subTest(start=start):
                self.transport = MacroTransport()
                self.transport.seed(mixed(start, repeat=3))
                snapshot = self.read()
                snapshot["configuration"].macros[0].playback = finish
                result = self.apply(snapshot)
                self.assertEqual(self.transport.writes[0][1], 0x15)
                self.assertEqual(self.transport.writes[0][23:27], bytes(4))
                self.assertEqual(self.transport.payload_writes, 17)
                self.assertEqual(self.transport.writes[-1][1], 0x15)
                self.assertEqual(self.transport.record(), macro_playback_metadata(finish)[0])
                first_payload = next(i for i, event in enumerate(self.transport.events)
                    if event[0] == "send" and event[1][1] == 0x1D)
                disable = self.transport.events.index(("send", self.transport.writes[0]))
                self.assertIn(("get", 0x15), self.transport.events[disable+1:first_payload])
                self.assertEqual(result["configuration"].macros[0].playback, finish)

    def test_interrupted_held_or_toggle_replacement_never_rebinds(self):
        for mode in ("while_held", "toggle"):
            with self.subTest(mode=mode):
                self.transport = MacroTransport()
                self.transport.seed(mixed(mode))
                snapshot = self.read()
                snapshot["configuration"].macros[0].events[1].delay_ms = 75
                self.transport.fail_payload = 3
                with self.assertRaisesRegex(DeviceError, "may remain disabled"):
                    self.apply(snapshot)
                self.assertEqual(self.transport.record(), bytes(4))
                self.assertEqual(self.transport.payload_writes, 3)
                self.assertEqual([p[1] for p in self.transport.writes].count(0x15), 1)

    def test_stale_mode_or_image_blocks_every_mutating_report(self):
        for change in ("assignment", "image"):
            with self.subTest(change=change):
                self.transport = MacroTransport()
                self.transport.seed(mixed("while_held"))
                snapshot = self.read()
                snapshot["configuration"].macros[0].playback = "toggle"
                if change == "assignment":
                    self.transport.set_record("primary", 5, NORMAL_MACRO_ASSIGNMENT)
                else:
                    self.transport.storage[("primary", 5)][40] ^= 1
                with self.assertRaisesRegex(DeviceError, "changed since the last read"):
                    self.apply(snapshot)
                self.assertEqual(self.transport.writes, [])

    def test_nonfinite_macro_cannot_remove_last_primary_click(self):
        for mode in ("while_held", "toggle"):
            for logical in (0, 1):
                with self.subTest(mode=mode, logical=logical):
                    self.transport = MacroTransport()
                    snapshot = self.read()
                    self.bind(snapshot, mixed(mode), logical=logical)
                    with self.assertRaisesRegex((ValueError, DeviceError), "left and right click"):
                        self.apply(snapshot)
                    self.assertEqual(self.transport.writes, [])


if __name__ == "__main__":
    unittest.main()
