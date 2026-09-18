"""Supported device macro imports preserve distinct sequences and local IDs."""

import copy
from dataclasses import replace
import hashlib
import unittest
from unittest.mock import patch

from swarm2.button_commands import BUTTON_SLOTS
from swarm2.configuration import Action, Configuration, Macro, MacroEvent
from swarm2.macro_profiles import NORMAL_MACRO_ASSIGNMENT, plan_macro_upload
from swarm2.service import DeviceService
from tests.test_settings import CAPTURES, with_checksum


DEVICE_ID = "macro-snapshot-fixture"
NAMESPACE = hashlib.sha256(DEVICE_ID.encode()).hexdigest()[:8]


def macro(key="F13", delay=50, *, macro_id="saved_macro", name="Saved macro", **kwargs):
    return Macro(id=macro_id, name=name, events=[MacroEvent("key_down", key, delay),
        MacroEvent("key_up", key, 0)], **kwargs)


def action_at(configuration, layer, logical):
    return getattr(configuration.buttons[BUTTON_SLOTS.index(logical)], layer)


def bind(configuration, layer, logical, macro_id):
    setattr(configuration.buttons[BUTTON_SLOTS.index(logical)], layer, Action("macro", macro_id))


def result_for(macros):
    settings = {name: CAPTURES[name] for name in ("sensor", "primary", "easy_shift")}
    data = {}
    for (layer, logical), value in macros.items():
        assignment = NORMAL_MACRO_ASSIGNMENT
        if isinstance(value, Macro):
            plan = plan_macro_upload(value, profile_index=0, logical_slot=logical, layer=layer)
            value, assignment = plan.expected_read_payload, plan.assignment
        raw = bytearray.fromhex(settings[layer])
        raw[4+4*logical:8+4*logical] = assignment
        settings[layer] = with_checksum(raw).hex()
        data[f"{layer}:{logical}"] = value.hex()
    return {"raw": CAPTURES["sensor"], "settings": settings, "macro_data": data, "changed": False}


class MacroSnapshotTests(unittest.TestCase):
    def snapshot(self, draft, macros):
        result = DeviceService._snapshot(DEVICE_ID, 1, result_for(macros), draft)
        result["configuration"].validate()
        return result

    def test_public_read_and_switch_accept_draft_for_identity_reconciliation(self):
        saved = macro()
        draft = Configuration(macros=[saved])
        bind(draft, "primary", 5, saved.id)
        result = result_for({("primary", 5): saved})

        for method, operation in (("read", "read_settings"),
                                  ("switch_profile", "switch_profile")):
            with self.subTest(method=method), patch.object(
                    DeviceService, "_call", return_value=result) as call:
                snapshot = getattr(DeviceService(), method)(
                    DEVICE_ID, 1, draft=draft)
                actual = snapshot["configuration"]
                self.assertEqual([item.id for item in actual.macros], [saved.id])
                self.assertEqual(
                    action_at(actual, "primary", 5), Action("macro", saved.id))
                call.assert_called_once_with({
                    "operation": operation,
                    "device_id": DEVICE_ID,
                    "profile_slot": 1,
                })
        self.assertEqual(draft.macros, [saved])
        self.assertEqual(action_at(draft, "primary", 5), Action("macro", saved.id))

    def test_shared_local_id_retains_distinct_external_slot_change_in_either_order(self):
        saved = macro()
        changed = macro("F14")
        for order in ((5, 6), (6, 5)):
            with self.subTest(order=order):
                draft = Configuration(macros=[saved])
                for logical in (5, 6):
                    bind(draft, "primary", logical, saved.id)
                original = copy.deepcopy(draft.to_dict())
                snapshot = self.snapshot(draft, {("primary", logical):
                    saved if logical == 5 else changed for logical in order})
                actual = snapshot["configuration"]
                library = {item.id: item for item in actual.macros}
                self.assertEqual(action_at(actual, "primary", 5), Action("macro", saved.id))
                new_id = f"device_{NAMESPACE}_p1_primary_6"
                self.assertEqual(action_at(actual, "primary", 6), Action("macro", new_id))
                self.assertEqual(library[saved.id], saved)
                self.assertEqual(library[new_id].events, changed.events)
                self.assertEqual(len(library), 2)
                self.assertEqual(snapshot["errors"], {})
                self.assertEqual(draft.to_dict(), original)

    def test_both_changed_slots_keep_original_local_library_entry(self):
        saved = macro()
        draft = Configuration(macros=[saved])
        bind(draft, "primary", 5, saved.id)
        bind(draft, "easy_shift", 5, saved.id)
        changed = {("primary", 5): macro("F14"), ("easy_shift", 5): macro("F15")}
        actual = self.snapshot(draft, changed)["configuration"]
        library = {item.id: item for item in actual.macros}
        self.assertEqual(library[saved.id], saved)
        self.assertEqual(len(library), 3)
        for (layer, logical), expected in changed.items():
            action = action_at(actual, layer, logical)
            self.assertEqual(action.value, f"device_{NAMESPACE}_p1_{layer}_{logical}")
            self.assertEqual(library[action.value].events, expected.events)

    def test_newly_applied_macro_keeps_requested_id_and_effective_normalized_timing(self):
        for macro_id in ("my_new_macro", f"device_{NAMESPACE}_p1_primary_5"):
            with self.subTest(macro_id=macro_id):
                requested = macro(delay=500, macro_id=macro_id)
                draft = Configuration(macros=[requested])
                bind(draft, "primary", 5, requested.id)
                result = result_for({("primary", 5): requested})
                result["changed"] = True
                adjustments = [{"layer": "primary", "logical_slot": 5, "event_index": 0,
                                "requested_ticks": 500, "encoded_ticks": 501}]
                result["macro_timing_adjustments"] = adjustments
                snapshot = DeviceService._snapshot(DEVICE_ID, 1, result, draft)
                actual = snapshot["configuration"]
                self.assertEqual(action_at(actual, "primary", 5), Action("macro", requested.id))
                self.assertEqual(len(actual.macros), 1)
                self.assertEqual(actual.macros[0].events[0].delay_ms, 501)
                self.assertEqual(requested.events[0].delay_ms, 500)
                self.assertEqual(snapshot["macro_timing_adjustments"], adjustments)
                self.assertEqual(snapshot["errors"], {})

    def test_name_repeat_and_playback_mismatches_do_not_reuse_requested_id(self):
        saved = macro()
        variants = (replace(saved, name="External name"),
                    replace(saved, repeat=3, playback="repeat"),
                    replace(saved, events=macro(delay=51).events))
        for changed in variants:
            with self.subTest(changed=changed):
                draft = Configuration(macros=[saved])
                bind(draft, "primary", 5, saved.id)
                actual = self.snapshot(draft, {("primary", 5): changed})["configuration"]
                self.assertNotEqual(action_at(actual, "primary", 5).value, saved.id)
                self.assertEqual(next(item for item in actual.macros if item.id == saved.id), saved)
        unsupported = replace(saved, playback="toggle")
        draft = Configuration(macros=[unsupported])
        bind(draft, "primary", 5, unsupported.id)
        actual = self.snapshot(draft, {("primary", 5): saved})["configuration"]
        self.assertNotEqual(action_at(actual, "primary", 5).value, unsupported.id)
        self.assertEqual(next(item for item in actual.macros if item.id == unsupported.id), unsupported)

    def test_existing_device_id_collision_retains_saved_sequence_with_new_suffix(self):
        old_id = f"device_{NAMESPACE}_p1_primary_5"
        saved = macro(macro_id=old_id)
        draft = Configuration(macros=[saved])
        bind(draft, "primary", 5, saved.id)
        changed = macro("F14")
        actual = self.snapshot(draft, {("primary", 5): changed})["configuration"]
        self.assertEqual(action_at(actual, "primary", 5).value, old_id+"_2")
        self.assertEqual(actual.macros[0], saved)
        again = self.snapshot(actual, {("primary", 5): changed})["configuration"]
        self.assertEqual(action_at(again, "primary", 5).value, old_id+"_2")
        self.assertEqual(len(again.macros), 2)

    def test_full_library_retains_unknown_assignment_and_reports_failed_import(self):
        saved = macro()
        library = [saved, *(Macro(id=f"local_{i}", name=f"Saved {i}") for i in range(63))]
        draft = Configuration(macros=library)
        bind(draft, "primary", 5, saved.id)
        bind(draft, "primary", 6, saved.id)
        snapshot = self.snapshot(draft, {("primary", 5): saved, ("primary", 6): macro("F14")})
        actual = snapshot["configuration"]
        self.assertEqual(actual.macros, library)
        self.assertEqual(action_at(actual, "primary", 5), Action("macro", saved.id))
        self.assertEqual(action_at(actual, "primary", 6), Action("device", "00010207"))
        self.assertIn("library is full", snapshot["errors"]["macro/primary:6"])
        self.assertIn("primary:6", snapshot["baseline"]["macro_data"])

    def test_shared_requested_id_does_not_merge_distinct_valid_native_timings(self):
        saved = macro(delay=500)
        draft = Configuration(macros=[saved])
        for logical in (5, 6):
            bind(draft, "primary", logical, saved.id)
        # Alternative source-supported encoding gives exactly500; the ordinary
        # compiler gives501. Both individually match the draft's accepted
        # semantics, but they remain distinct incoming device sequences.
        canonical = plan_macro_upload(saved, profile_index=0, logical_slot=5).expected_read_payload
        encoded = bytes.fromhex("016800020800e4680000")
        alternative = canonical[:76] + encoded + bytes(961-len(encoded))
        snapshot = self.snapshot(draft, {("primary", 5): alternative, ("primary", 6): canonical})
        actual = snapshot["configuration"]
        library = {item.id: item for item in actual.macros}
        first, second = (action_at(actual, "primary", logical).value for logical in (5, 6))
        self.assertNotEqual(first, second)
        self.assertEqual(library[first].events[0].delay_ms, 500)
        self.assertEqual(library[second].events[0].delay_ms, 501)

    def test_shared_local_id_preserves_distinct_external_held_and_toggle_modes(self):
        held = macro(playback="while_held", repeat=999)
        draft = Configuration(macros=[held])
        bind(draft, "primary", 5, held.id)
        bind(draft, "easy_shift", 5, held.id)
        result = self.snapshot(draft, {("primary", 5): held,
                                      ("easy_shift", 5): replace(held, playback="toggle")})
        actual = result["configuration"]
        library = {item.id: item for item in actual.macros}
        self.assertEqual(action_at(actual, "primary", 5).value, held.id)
        alternate = action_at(actual, "easy_shift", 5).value
        self.assertNotEqual(alternate, held.id)
        self.assertEqual(library[held.id].playback, "while_held")
        self.assertEqual(library[alternate].playback, "toggle")
        self.assertEqual(library[held.id].events, library[alternate].events)

    def test_normalized_held_and_toggle_uploads_keep_requested_ids(self):
        for playback in ("while_held", "toggle"):
            with self.subTest(playback=playback):
                requested = macro(delay=500, playback=playback, repeat=999)
                draft = Configuration(macros=[requested])
                bind(draft, "primary", 5, requested.id)
                actual = self.snapshot(draft, {("primary", 5): requested})["configuration"]
                self.assertEqual(action_at(actual, "primary", 5).value, requested.id)
                self.assertEqual(len(actual.macros), 1)
                self.assertEqual(actual.macros[0].playback, playback)
                self.assertEqual(actual.macros[0].events[0].delay_ms, 501)


if __name__ == "__main__":
    unittest.main()
