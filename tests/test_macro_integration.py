"""End-to-end macro transactions against an independent byte-storage fixture."""

import unittest
from unittest.mock import patch

from swarm2.button_commands import BUTTON_SLOTS
from swarm2.configuration import Action, Macro, MacroEvent
from swarm2.hardware import transact
from swarm2.macro_profiles import NORMAL_MACRO_ASSIGNMENT, plan_macro_upload
from swarm2.macro_commands import LCD_MACRO_RAW_SLOTS
from swarm2.service import DeviceService
from swarm2.transport import DeviceError
from tests.test_settings import SettingsTransport, with_checksum


def tap(key="F13", *, name="Integration macro", macro_id="local_macro"):
    return Macro(id=macro_id, name=name, events=[MacroEvent("key_down", key, 50),
                                                MacroEvent("key_up", key, 0)])


class MacroTransport(SettingsTransport):
    def __init__(self):
        super().__init__()
        self.storage = {}
        self.macro_selection = None
        self.events = []
        self.payload_writes = 0
        self.fail_payload = None
        self.corrupt_uploaded_readback = False
        self.final_macro_read_failure = None
        self.bound_after_upload = False
        self.read_macro_errors = set()

    def seed(self, macro, logical=5, layer="primary", *, payload=None, assignment=None):
        if payload is None:
            plan = plan_macro_upload(macro, profile_index=0, logical_slot=logical, layer=layer)
            payload = plan.expected_read_payload
            if assignment is None:
                assignment = plan.assignment
        if assignment is None:
            assignment = NORMAL_MACRO_ASSIGNMENT
        self.storage[(layer, logical)] = bytearray(payload + bytes(1054-len(payload)))
        self.set_record(layer, logical, assignment)

    def set_record(self, layer, logical, record):
        raw = bytearray(self.records[layer])
        raw[4+4*logical:8+4*logical] = record
        self.records[layer] = with_checksum(raw)

    def record(self, layer="primary", logical=5):
        return self.records[layer][4+4*logical:8+4*logical]

    def send(self, packet):
        self.events.append(("send", packet))
        if packet[:3] in (b"\x10\x1c\x02", b"\x10\x1c\x03"):
            self.sent.append(packet)
            self.assert_packet(packet)
            wire_slot = packet[4]
            if wire_slot in LCD_MACRO_RAW_SLOTS:
                layer, logical = "lcd", LCD_MACRO_RAW_SLOTS.index(wire_slot)
            else:
                layer, logical = (("easy_shift", wire_slot-15)
                                  if wire_slot >= 15 else ("primary", wire_slot))
            self.macro_selection = (packet[2], layer, logical, packet[5])
            if packet[2] == 2:
                self.writes.append(packet)
            return
        if packet[:2] == b"\x10\x1d":
            self.sent.append(packet)
            self.writes.append(packet)
            self.payload_writes += 1
            if self.payload_writes == self.fail_payload:
                raise DeviceError("Simulated macro payload disconnect")
            mode, layer, logical, chunk = self.macro_selection
            if mode != 2:
                raise AssertionError("Macro data requires a write selector")
            storage = self.storage.setdefault((layer, logical), bytearray(1054))
            storage[chunk*62:(chunk+1)*62] = packet[2:]
            self.macro_selection = None
            return
        self.macro_selection = None
        super().send(packet)
        if self.payload_writes and packet[1] in (0x15, 0x16):
            if any(packet[6+i*4] == 7 for i in range(12)):
                self.bound_after_upload = True

    @staticmethod
    def assert_packet(packet):
        if len(packet) != 64 or packet[3] != 0 or packet[6] != sum(packet[:6]) & 255:
            raise AssertionError("Invalid macro selector identity, profile or checksum")

    def get_feature(self, selector):
        if selector == 0x1D:
            mode, layer, logical, chunk = self.macro_selection
            if mode != 3:
                raise AssertionError("Macro GET requires an acknowledged read selector")
            self.events.append(("macro_get", layer, logical, chunk))
            if (layer, logical) in self.read_macro_errors:
                raise DeviceError("Simulated macro read failure")
            if self.bound_after_upload and self.final_macro_read_failure == "missing":
                raise DeviceError("Simulated final macro read failure")
            storage = self.storage.get((layer, logical), bytes(1054))
            payload = bytes(storage[chunk*61:(chunk+1)*61])
            if self.corrupt_uploaded_readback and self.payload_writes and chunk == 16:
                payload = payload[:-1] + bytes((payload[-1] ^ 0xFF,))
            if self.bound_after_upload and self.final_macro_read_failure == "corrupt" and chunk == 16:
                payload = payload[:-1] + bytes((payload[-1] ^ 0xFF,))
            self.macro_selection = None
            return b"\x10\x1d\0" + payload
        self.events.append(("get", selector))
        return super().get_feature(selector)


class MacroIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.transport = MacroTransport()
        self.service = DeviceService()
        helper = patch.object(DeviceService, "_call", side_effect=lambda request: transact(request, lambda _: self.transport))
        helper.start()
        self.addCleanup(helper.stop)

    def read(self):
        return self.service.read("fixture-mc7", 1)

    def bind(self, snapshot, macro, logical=5, layer="primary"):
        config = snapshot["configuration"]
        config.macros.append(macro)
        binding = next(b for b in config.buttons if BUTTON_SLOTS[b.button_id-1] == logical)
        setattr(binding, layer, Action("macro", macro.id))

    def apply(self, snapshot):
        return self.service.apply_section("fixture-mc7", snapshot["configuration"], "buttons", snapshot["baseline"])

    def edit_imported(self, snapshot):
        macro = snapshot["configuration"].macros[0]
        macro.events = [MacroEvent("key_down", "F14", 50), MacroEvent("key_up", "F14", 0)]
        return macro

    def test_new_macro_upload_and_complete_readback_precede_assignment_in_both_layers(self):
        self.transport.set_record("primary", 9, bytes.fromhex("deadbeef"))
        self.transport.set_record("easy_shift", 9, bytes.fromhex("cafeba00"))
        self.transport.set_record("primary", 7, bytes.fromhex("aabbccfe"))
        snapshot = self.read()
        for layer, logical, macro in (("primary", 5, tap()), ("easy_shift", 10, tap("F14", macro_id="shift_macro"))):
            self.bind(snapshot, macro, logical, layer)
        before = dict(self.transport.records)
        result = self.apply(snapshot)
        self.assertTrue(result["summary"]["changed"])
        self.assertIn("macros", result["verified_fields"])
        self.assertEqual(self.transport.payload_writes, 34)
        events = self.transport.events
        first_assignment = min(i for i, event in enumerate(events) if event[0] == "send" and event[1][1] in (0x15, 0x16))
        final_upload = max(i for i, event in enumerate(events) if event[0] == "send" and event[1][1] == 0x1D)
        self.assertGreater(first_assignment, final_upload)
        # Both slots have a complete post-upload 17-chunk read before binding.
        for layer, logical in (("primary", 5), ("easy_shift", 10)):
            wire_slot = logical + (15 if layer == "easy_shift" else 0)
            last_slot_upload = max(i for i, event in enumerate(events)
                                   if event[0] == "send" and event[1][:3] == b"\x10\x1c\x02"
                                   and event[1][4:6] == bytes((wire_slot, 16))) + 1
            read_positions = [i for i, event in enumerate(events) if event == ("macro_get", layer, logical, 16)]
            self.assertTrue(any(last_slot_upload < i < first_assignment for i in read_positions))
            self.assertEqual(self.transport.record(layer, logical), NORMAL_MACRO_ASSIGNMENT)
        for name in before.keys() - {"primary", "easy_shift"}:
            self.assertEqual(self.transport.records[name], before[name])
        for layer, edited in (("primary", 5), ("easy_shift", 10)):
            for logical in range(12):
                if logical != edited:
                    self.assertEqual(self.transport.record(layer, logical), before[layer][4+4*logical:8+4*logical])
        self.assertEqual(result["configuration"].buttons[5].primary, Action("macro", "local_macro"))

    def test_replacement_disables_and_verifies_button_before_any_macro_payload(self):
        self.transport.seed(tap())
        snapshot = self.read()
        macro = self.edit_imported(snapshot)
        result = self.apply(snapshot)
        writes = self.transport.writes
        self.assertEqual(writes[0][1], 0x15)
        self.assertEqual(writes[0][3+5*4:7+5*4], bytes(4))
        self.assertEqual(writes[-1][1], 0x15)
        self.assertEqual(writes[-1][3+5*4:7+5*4], NORMAL_MACRO_ASSIGNMENT)
        suspend = self.transport.events.index(("send", writes[0]))
        first_payload = next(i for i, e in enumerate(self.transport.events) if e[0] == "send" and e[1][1] == 0x1D)
        self.assertIn(("get", 0x15), self.transport.events[suspend+1:first_payload])
        expected = plan_macro_upload(macro, profile_index=0, logical_slot=5).expected_read_payload
        self.assertEqual(result["baseline"]["macro_data"]["primary:5"], expected.hex())

    def test_interrupted_replacement_leaves_assignment_disabled_without_retry_or_rebind(self):
        self.transport.seed(tap())
        snapshot = self.read()
        self.edit_imported(snapshot)
        self.transport.fail_payload = 3
        with self.assertRaisesRegex(DeviceError, "may remain disabled"):
            self.apply(snapshot)
        self.assertEqual(self.transport.payload_writes, 3)
        self.assertEqual(self.transport.record(), bytes(4))
        self.assertEqual([p[1] for p in self.transport.writes].count(0x15), 1)
        self.assertTrue(self.transport.closed)

    def test_failed_disable_readback_prevents_macro_write(self):
        self.transport.seed(tap())
        snapshot = self.read()
        self.edit_imported(snapshot)
        self.transport.ignore_writes = True
        with self.assertRaisesRegex(DeviceError, "Could not suspend"):
            self.apply(snapshot)
        self.assertEqual(self.transport.payload_writes, 0)
        self.assertEqual(self.transport.record(), NORMAL_MACRO_ASSIGNMENT)

    def test_macro_readback_mismatch_after_replacement_leaves_button_disabled(self):
        self.transport.seed(tap())
        snapshot = self.read()
        self.edit_imported(snapshot)
        self.transport.corrupt_uploaded_readback = True
        with self.assertRaisesRegex(DeviceError, "did not match complete readback"):
            self.apply(snapshot)
        self.assertEqual(self.transport.payload_writes, 17)
        self.assertEqual(self.transport.record(), bytes(4))

    def test_stale_macro_baseline_blocks_all_mutating_reports(self):
        self.transport.seed(tap())
        snapshot = self.read()
        self.edit_imported(snapshot)
        replacement = plan_macro_upload(tap("F15"), profile_index=0, logical_slot=5).expected_read_payload
        self.transport.storage[("primary", 5)][:1037] = replacement
        with self.assertRaisesRegex(DeviceError, "macro changed since the last read"):
            self.apply(snapshot)
        self.assertEqual(self.transport.writes, [])

    def test_stale_button_baseline_blocks_upload_for_a_new_macro(self):
        snapshot = self.read()
        self.bind(snapshot, tap())
        self.transport.set_record("primary", 3, bytes.fromhex("00000603"))
        with self.assertRaisesRegex(DeviceError, "primary settings changed"):
            self.apply(snapshot)
        self.assertEqual(self.transport.writes, [])

    def test_lost_or_corrupt_final_macro_readback_never_reports_success(self):
        for failure in ("missing", "corrupt"):
            with self.subTest(failure=failure):
                self.transport = MacroTransport()
                snapshot = self.read()
                self.bind(snapshot, tap())
                self.transport.final_macro_read_failure = failure
                with self.assertRaisesRegex(DeviceError, "macro|Macro"):
                    self.apply(snapshot)
                self.assertTrue(self.transport.bound_after_upload)
                self.assertEqual(self.transport.payload_writes, 17)
                self.assertEqual([p[1] for p in self.transport.writes].count(0x15), 1)

    def test_missing_current_macro_or_baseline_prevents_replacement(self):
        for missing in ("current", "baseline"):
            with self.subTest(missing=missing):
                self.transport = MacroTransport()
                self.transport.seed(tap())
                snapshot = self.read()
                self.edit_imported(snapshot)
                if missing == "current":
                    self.transport.read_macro_errors.add(("primary", 5))
                else:
                    snapshot["baseline"]["macro_data"].clear()
                with self.assertRaisesRegex(DeviceError, "Read the assigned macro successfully"):
                    self.apply(snapshot)
                self.assertEqual(self.transport.writes, [])

    def test_unchanged_imported_macro_never_rewrites_image_or_button(self):
        self.transport.seed(tap())
        snapshot = self.read()
        result = self.apply(snapshot)
        self.assertFalse(result["summary"]["changed"])
        self.assertEqual(self.transport.writes, [])
        self.assertEqual(result["baseline"]["macro_data"], snapshot["baseline"]["macro_data"])

    def test_existing_identical_unassigned_data_is_verified_then_bound_without_reupload(self):
        original = self.transport.record()
        macro = tap()
        self.transport.seed(macro)
        self.transport.set_record("primary", 5, original)
        snapshot = self.read()
        self.bind(snapshot, macro)
        result = self.apply(snapshot)
        self.assertTrue(result["summary"]["changed"])
        self.assertEqual(self.transport.payload_writes, 0)
        self.assertEqual([p[1] for p in self.transport.writes], [0x15])
        self.assertEqual(self.transport.record(), NORMAL_MACRO_ASSIGNMENT)
        self.assertEqual(result["configuration"].buttons[5].primary, Action("macro", macro.id))

    def test_unchanged_vendor_group_metadata_is_preserved_without_macro_rewrite(self):
        payload = bytearray(plan_macro_upload(tap(), profile_index=0, logical_slot=5).expected_read_payload)
        payload[:40] = b"Vendor user group".ljust(40, b"\0")
        self.transport.seed(None, payload=bytes(payload))
        snapshot = self.read()
        self.assertIn("macros", snapshot["verified_fields"])
        result = self.apply(snapshot)
        self.assertFalse(result["summary"]["changed"])
        self.assertEqual(self.transport.writes, [])
        self.assertEqual(result["baseline"]["macro_data"]["primary:5"], bytes(payload).hex())

    def test_alternate_valid_encoding_survives_noop_and_unrelated_button_edit_exactly(self):
        # Both encodings are valid device state, but recompiling would round
        # the first delay (200 -> 201) or second one (0 -> 1).
        for encoded, delay in (("016800020200e4680000", 200), ("016880680000", 0)):
            with self.subTest(encoded=encoded):
                self.transport = MacroTransport()
                payload = bytearray(plan_macro_upload(tap(), profile_index=0, logical_slot=5).expected_read_payload)
                payload[76:1036] = bytes(960)
                event_bytes = bytes.fromhex(encoded)
                payload[76:76+len(event_bytes)] = event_bytes
                self.transport.seed(None, payload=bytes(payload))
                snapshot = self.read()
                self.assertIn("macros", snapshot["verified_fields"])
                self.assertEqual(snapshot["configuration"].macros[0].events[0].delay_ms, delay)
                no_op = self.apply(snapshot)
                self.assertFalse(no_op["summary"]["changed"])
                self.assertEqual(self.transport.writes, [])
                self.assertEqual(no_op["macro_timing_adjustments"], [])
                no_op["configuration"].buttons[3].primary = Action("media", "mute")
                changed = self.apply(no_op)
                self.assertTrue(changed["summary"]["changed"])
                self.assertEqual(self.transport.payload_writes, 0)
                self.assertEqual([packet[1] for packet in self.transport.writes], [0x15])
                self.assertEqual(changed["baseline"]["macro_data"]["primary:5"], bytes(payload).hex())
                self.assertEqual(changed["configuration"].macros[0].events[0].delay_ms, delay)

    def test_changed_imported_macro_retains_validated_vendor_group(self):
        payload = bytearray(plan_macro_upload(tap(), profile_index=0, logical_slot=5).expected_read_payload)
        group = b"Vendor user group".ljust(40, b"\0")
        payload[:40] = group
        self.transport.seed(None, payload=bytes(payload))
        snapshot = self.read()
        self.edit_imported(snapshot)
        result = self.apply(snapshot)
        self.assertTrue(result["summary"]["changed"])
        self.assertEqual(self.transport.payload_writes, 17)
        actual = bytes.fromhex(result["baseline"]["macro_data"]["primary:5"])
        self.assertEqual(actual[:40], group)
        self.assertEqual(result["configuration"].macros[0].events[0].value, "F14")

    def test_unsupported_assigned_macro_rejects_before_any_settings_or_macro_write(self):
        invalid = [tap(name="Name with non-ASCII é"), tap(),
                   Macro(id="mouse_macro", events=[MacroEvent("mouse_down", "scroll_up", 50), MacroEvent("mouse_up", "scroll_up", 0)])]
        invalid[1].events[-1].delay_ms = 50
        for macro in invalid:
            with self.subTest(macro=macro):
                self.transport = MacroTransport()
                snapshot = self.read()
                self.bind(snapshot, macro)
                snapshot["configuration"].buttons[3].primary = Action("media", "mute")
                with self.assertRaises(ValueError):
                    self.apply(snapshot)
                self.assertEqual(self.transport.writes, [])

    def test_shared_mixed_macro_upload_import_normalization_and_noop_in_both_layers(self):
        value = Macro(id="shared_input", name="Mixed input", events=[
            MacroEvent("key_down", "F13", 1), MacroEvent("mouse_down", "middle", 500),
            MacroEvent("mouse_up", "middle", 1), MacroEvent("key_up", "F13", 0)])
        snapshot = self.read()
        self.bind(snapshot, value, 5, "primary")
        snapshot["configuration"].buttons[BUTTON_SLOTS.index(10)].easy_shift = Action("macro", value.id)
        result = self.apply(snapshot)
        self.assertTrue(result["summary"]["changed"])
        self.assertEqual(self.transport.payload_writes, 34)
        self.assertEqual(len(result["configuration"].macros), 1)
        effective = result["configuration"].macros[0]
        self.assertEqual(effective.id, value.id)
        self.assertEqual(effective.events[1], MacroEvent("mouse_down", "middle", 501))
        for layer, logical in (("primary", 5), ("easy_shift", 10)):
            self.assertEqual(self.transport.record(layer, logical), NORMAL_MACRO_ASSIGNMENT)
            binding = result["configuration"].buttons[BUTTON_SLOTS.index(logical)]
            self.assertEqual(getattr(binding, layer), Action("macro", value.id))
        self.assertEqual(len(result["macro_timing_adjustments"]), 2)
        self.transport.writes.clear()
        self.transport.payload_writes = 0
        no_op = self.apply(result)
        self.assertFalse(no_op["summary"]["changed"])
        self.assertEqual(self.transport.writes, [])
        self.assertEqual(no_op["macro_timing_adjustments"], [])
        fresh = self.read()
        self.assertEqual(len(fresh["configuration"].macros), 2)
        self.assertTrue(all(item.events == effective.events for item in fresh["configuration"].macros))
        self.assertEqual(fresh["errors"], {})

    def test_unbalanced_or_alias_conflicting_mixed_inputs_fail_before_writes(self):
        invalid = [
            [MacroEvent("key_down", "F13", 1), MacroEvent("mouse_down", "left", 50),
             MacroEvent("key_up", "F13", 0)],
            [MacroEvent("mouse_down", "left", 50), MacroEvent("mouse_up", "right", 0)],
            [MacroEvent("key_down", "Ctrl", 1), MacroEvent("mouse_down", "middle", 1),
             MacroEvent("key_down", "Control", 1), MacroEvent("key_up", "Control", 1),
             MacroEvent("mouse_up", "middle", 1), MacroEvent("key_up", "Ctrl", 0)],
        ]
        for events in invalid:
            with self.subTest(events=events):
                self.transport = MacroTransport()
                snapshot = self.read()
                original = self.transport.record()
                self.bind(snapshot, Macro(id="invalid_mixed", events=events))
                with self.assertRaises(ValueError):
                    self.apply(snapshot)
                self.assertEqual(self.transport.writes, [])
                self.assertEqual(self.transport.record(), original)

    def test_unknown_private_mouse_codes_remain_opaque_on_read_and_noop(self):
        for code in range(0xF5, 0x100):
            with self.subTest(code=code):
                self.transport = MacroTransport()
                payload = bytearray(plan_macro_upload(tap(), profile_index=0, logical_slot=5).expected_read_payload)
                payload[77] = payload[79] = code
                self.transport.seed(None, payload=bytes(payload))
                snapshot = self.read()
                self.assertEqual(snapshot["configuration"].macros, [])
                self.assertEqual(snapshot["configuration"].buttons[5].primary, Action("device", "00010207"))
                self.assertIn("macro/primary:5", snapshot["errors"])
                no_op = self.apply(snapshot)
                self.assertFalse(no_op["summary"]["changed"])
                self.assertEqual(self.transport.writes, [])
                self.assertEqual(no_op["baseline"]["macro_data"]["primary:5"], bytes(payload).hex())

    def test_primary_left_or_right_click_cannot_be_replaced_by_macro(self):
        for logical in (0, 1):
            with self.subTest(logical=logical):
                self.transport = MacroTransport()
                snapshot = self.read()
                self.bind(snapshot, tap(), logical)
                with self.assertRaisesRegex((ValueError, DeviceError), "left and right click"):
                    self.apply(snapshot)
                self.assertEqual(self.transport.writes, [])

    def test_supported_import_and_opaque_macro_assignment_survive_unrelated_button_edit(self):
        self.transport.seed(tap(), logical=5)
        unknown = bytes.fromhex("aa550907")
        opaque_data = b"\x7f" * 1037
        self.transport.seed(None, logical=6, assignment=unknown, payload=opaque_data)
        snapshot = self.read()
        self.assertIn("macros", snapshot["verified_fields"])
        self.assertEqual(snapshot["configuration"].buttons[6].primary, Action("device", unknown.hex()))
        self.assertIn("macro/primary:6", snapshot["errors"])
        snapshot["configuration"].buttons[3].primary = Action("media", "mute")
        result = self.apply(snapshot)
        self.assertEqual(self.transport.payload_writes, 0)
        self.assertEqual(self.transport.record(logical=6), unknown)
        self.assertEqual(result["baseline"]["macro_data"]["primary:6"], opaque_data.hex())
        self.assertEqual(result["configuration"].buttons[6].primary, Action("device", unknown.hex()))

    def test_unknown_bytes_in_normal_macro_are_preserved_as_opaque_during_other_edit(self):
        payload = bytearray(plan_macro_upload(tap(), profile_index=0, logical_slot=5).expected_read_payload)
        payload[1025] = 0xAB
        self.transport.seed(None, payload=bytes(payload))
        snapshot = self.read()
        self.assertNotIn("macros", snapshot["verified_fields"])
        self.assertEqual(snapshot["configuration"].buttons[5].primary, Action("device", NORMAL_MACRO_ASSIGNMENT.hex()))
        self.assertIn("macro/primary:5", snapshot["errors"])
        snapshot["configuration"].buttons[3].primary = Action("media", "mute")
        result = self.apply(snapshot)
        self.assertEqual(self.transport.payload_writes, 0)
        self.assertEqual(self.transport.record(), NORMAL_MACRO_ASSIGNMENT)
        self.assertEqual(result["baseline"]["macro_data"]["primary:5"], bytes(payload).hex())


if __name__ == "__main__":
    unittest.main()
