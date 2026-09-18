"""End-to-end LCD touch macro transactions against byte-level USB fixtures."""

import unittest
from unittest.mock import patch

from swarm2.configuration import Macro, MacroEvent
from swarm2.hardware import transact
from swarm2.lcd_commands import decode_lcd_response
from swarm2.macro_profiles import plan_macro_upload
from swarm2.screen_key_commands import encode_lcd_macro_record
from swarm2.service import DeviceService
from swarm2.transport import DeviceError
from tests.test_macro_integration import MacroTransport
from tests.test_settings import (EMPTY_SCREEN_KEY, with_lcd_page,
                                 with_screen_key_records)


def tap(key="F13", *, macro_id="lcd_macro", name="LCD macro", playback="once",
        repeat=1):
    return Macro(id=macro_id, name=name, playback=playback, repeat=repeat, events=[
        MacroEvent("key_down", key, 50), MacroEvent("key_up", key, 0)])


def seed_page_three_macro(transport, macro, *, trigger=None):
    transport.records["lcd"] = with_lcd_page(
        transport.records["lcd"], 2,
        (b"\x05\x02", b"\x19\xff", b"\x1a\xff", b"\x1f\xff"))
    plan = plan_macro_upload(macro, profile_index=0, logical_slot=8, layer="lcd")
    transport.storage[("lcd", 8)] = bytearray(
        plan.expected_read_payload + bytes(1054 - len(plan.expected_read_payload)))
    record = trigger if trigger is not None else encode_lcd_macro_record(
        macro.name, macro.playback)
    transport.records["screen_keys"] = with_screen_key_records(
        transport.records["screen_keys"], 2,
        (record, EMPTY_SCREEN_KEY, EMPTY_SCREEN_KEY, EMPTY_SCREEN_KEY))
    return plan


class LcdMacroIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.transport = MacroTransport()
        self.service = DeviceService()
        helper = patch.object(
            DeviceService, "_call",
            side_effect=lambda request: transact(request, lambda _: self.transport))
        helper.start()
        self.addCleanup(helper.stop)

    def read(self):
        return self.service.read("fixture-mc7", 1)

    def apply(self, snapshot):
        return self.service.apply_section(
            "fixture-mc7", snapshot["configuration"], "display", snapshot["baseline"])

    def test_new_tile_upload_readback_trigger_and_layout_are_ordered(self):
        snapshot = self.read()
        macro = tap()
        config = snapshot["configuration"]
        config.macros.append(macro)
        config.display.pages[2][0] = "macro"
        config.display.macro_bindings = [[None] * 4 for _ in config.display.pages]
        config.display.macro_bindings[2][0] = macro.id

        result = self.apply(snapshot)

        self.assertTrue(result["summary"]["changed"])
        self.assertEqual(self.transport.payload_writes, 17)
        writes = self.transport.writes
        self.assertEqual([packet[1] for packet in writes[-2:]], [0x29, 0x25])
        final_payload = max(i for i, event in enumerate(self.transport.events)
                            if event[0] == "send" and event[1][1] == 0x1D)
        trigger = next(i for i, event in enumerate(self.transport.events)
                       if event[0] == "send" and event[1][1] == 0x29)
        layout = max(i for i, event in enumerate(self.transport.events)
                     if event[0] == "send" and event[1][1] == 0x25)
        self.assertLess(final_payload, trigger)
        self.assertLess(trigger, layout)
        selectors = [event[1] for event in self.transport.events
                     if event[0] == "send" and event[1][:3] == b"\x10\x1c\x02"]
        self.assertEqual({packet[4] for packet in selectors}, {41})
        self.assertEqual(
            result["baseline"]["macro_data"]["lcd:8"],
            plan_macro_upload(macro, profile_index=0, logical_slot=8,
                              layer="lcd").expected_read_payload.hex())
        self.assertEqual(result["configuration"].display.macro_bindings[2][0], macro.id)
        self.assertIn("display.macro_bindings", result["verified_fields"])
        # Decode through the production snapshot state rather than depending on
        # response wire order here.
        from swarm2.screen_key_commands import decode_screen_key_responses
        keys = decode_screen_key_responses(
            bytes.fromhex(result["baseline"]["settings"]["screen_keys"]), 0)
        self.assertEqual(keys.pages[2].records[0], encode_lcd_macro_record(macro.name))

        self.transport.writes.clear()
        self.transport.payload_writes = 0
        no_op = self.apply(result)
        self.assertFalse(no_op["summary"]["changed"])
        self.assertEqual(self.transport.writes, [])

    def test_replacement_suspends_active_tile_then_restores_layout(self):
        seed_page_three_macro(self.transport, tap())
        snapshot = self.read()
        snapshot["configuration"].macros[0].events = tap("F14").events

        result = self.apply(snapshot)

        self.assertTrue(result["summary"]["changed"])
        self.assertEqual(self.transport.payload_writes, 17)
        self.assertEqual(self.transport.writes[0][1], 0x25)
        self.assertEqual(self.transport.writes[-1][1], 0x25)
        suspended = bytearray(self.transport.writes[0][:61])
        suspended[2] = 0
        self.assertEqual(decode_lcd_response(suspended, 0).pages[2].slots[0].key, "empty")
        self.assertEqual(decode_lcd_response(self.transport.records["lcd"], 0)
                         .pages[2].slots[0].key, "macro")
        self.assertEqual(result["configuration"].macros[0].events[0].value, "F14")

    def test_interrupted_replacement_leaves_macro_tile_inert(self):
        seed_page_three_macro(self.transport, tap())
        snapshot = self.read()
        snapshot["configuration"].macros[0].events = tap("F14").events
        self.transport.fail_payload = 3

        with self.assertRaisesRegex(DeviceError, "LCD macro tile may remain disabled"):
            self.apply(snapshot)

        self.assertEqual(self.transport.payload_writes, 3)
        self.assertEqual(decode_lcd_response(self.transport.records["lcd"], 0)
                         .pages[2].slots[0].key, "empty")
        self.assertEqual([packet[1] for packet in self.transport.writes].count(0x25), 1)

    def test_stale_active_image_blocks_every_write(self):
        seed_page_three_macro(self.transport, tap())
        snapshot = self.read()
        snapshot["configuration"].macros[0].events = tap("F14").events
        self.transport.storage[("lcd", 8)][40] ^= 1

        with self.assertRaisesRegex(DeviceError, "LCD macro changed since the last read"):
            self.apply(snapshot)

        self.assertEqual(self.transport.writes, [])

    def test_toggle_import_requires_matching_touch_record_mode(self):
        seed_page_three_macro(self.transport, tap(playback="toggle"))
        snapshot = self.read()
        macro_id = snapshot["configuration"].display.macro_bindings[2][0]
        self.assertIsNotNone(macro_id)
        self.assertEqual(snapshot["configuration"].macros[0].playback, "toggle")
        self.assertEqual(snapshot["errors"], {})

        self.transport = MacroTransport()
        seed_page_three_macro(
            self.transport, tap(playback="toggle"),
            trigger=encode_lcd_macro_record("LCD macro", "once"))
        mismatch = self.read()
        self.assertIsNone(mismatch["configuration"].display.macro_bindings[2][0])
        self.assertIn("trigger mode and stored loop count", mismatch["errors"]["macro/lcd:8"])

    def test_unsupported_while_held_tile_is_preserved_during_unrelated_display_edit(self):
        held = tap(playback="while_held")
        # The touch callback has no mode-1 branch. Seed its shared image format
        # through a physical slot and pair it with an opaque mode-1 touch record.
        self.transport.records["lcd"] = with_lcd_page(
            self.transport.records["lcd"], 2,
            (b"\x05\x02", b"\x19\xff", b"\x1a\xff", b"\x1f\xff"))
        plan = plan_macro_upload(held, profile_index=0, logical_slot=5, layer="primary")
        self.transport.storage[("lcd", 8)] = bytearray(
            plan.expected_read_payload + bytes(1054-len(plan.expected_read_payload)))
        self.transport.records["screen_keys"] = with_screen_key_records(
            self.transport.records["screen_keys"], 2,
            (bytes.fromhex("0001010700") + b"Held\0\0", EMPTY_SCREEN_KEY,
             EMPTY_SCREEN_KEY, EMPTY_SCREEN_KEY))
        snapshot = self.read()
        self.assertIsNone(snapshot["configuration"].display.macro_bindings[2][0])
        self.assertIn("macro/lcd:8", snapshot["errors"])
        original_keys = self.transport.records["screen_keys"]
        snapshot["configuration"].display.brightness = 80

        result = self.apply(snapshot)

        self.assertTrue(result["summary"]["changed"])
        self.assertEqual([packet[1] for packet in self.transport.writes], [0x2B])
        self.assertEqual(self.transport.records["screen_keys"], original_keys)
        self.assertEqual(decode_lcd_response(self.transport.records["lcd"], 0)
                         .pages[2].slots[0].key, "macro")


if __name__ == "__main__":
    unittest.main()
