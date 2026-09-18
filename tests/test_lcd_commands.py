import dataclasses
import unittest

from swarm2.lcd_commands import (
    LCD_GENERAL_MEDIA_APP_ID,
    LCD_ONBOARD_SHORTCUT_APP_IDS, LCD_ONBOARD_SHORTCUT_WIDGETS,
    LCD_RESPONSE_BYTES, LCD_WIDGETS, LcdWidget, build_lcd_get_buffer,
    build_lcd_read_request, build_lcd_report, decode_lcd_response,
    edited_lcd_signature,
)
from swarm2.protocol import ProtocolError


CAPTURE = bytes.fromhex(
    "1025000003"
    "01000064ff010000ff00ff"
    "02000065ff15ff00ff00ff"
    "0300001fff1aff19ff47ff"
    "0400000000000000000000"
    "0500000000000000000000"
    "81"
)


class LcdCommandsTests(unittest.TestCase):
    def setUp(self):
        self.state = decode_lcd_response(CAPTURE, 0)

    def test_capture_identifies_download_tile_and_existing_pages(self):
        self.assertEqual(self.state.page_count, 3)
        keys = [tuple(None if w is None else w.key for w in page.slots)
                for page in self.state.pages[:3]]
        self.assertEqual(keys, [
            ("download_swarm", None, None, "dpi"),
            ("game_bar", "system_media", None, None),
            ("cut", "copy", "paste", "undo"),
        ])
        self.assertEqual(self.state.raw, CAPTURE)
        self.assertIsNone(self.state.pages[3].slots)
        self.assertIsNone(self.state.pages[4].slots)

    def test_select_request_has_explicit_profile_and_zero_padding(self):
        self.assertEqual(build_lcd_read_request(4), bytes.fromhex("101c0025040000") + bytes(57))
        self.assertEqual(build_lcd_get_buffer(), bytes.fromhex("1025") + bytes(62))
        for value in (-1, 5, True, "0"):
            with self.subTest(value=value), self.assertRaises(ProtocolError):
                build_lcd_read_request(value)

    def test_named_page_replacement_matches_vendor_slot_reversal(self):
        report = build_lcd_report(self.state, pages={
            0: ["dpi", "led_brightness", "polling_rate", "empty"],
        })
        self.assertEqual(len(report), 64)
        self.assertEqual(report[:16].hex(), "10253b0003010000fe00660063006400")
        # Unchanged exposed pages still undergo the vendor's read/write conversion.
        self.assertEqual(report[16:27].hex(), "0200000000000065001500")
        self.assertEqual(report[27:38].hex(), "0300001f001a0019004700")
        self.assertEqual(report[38:61], CAPTURE[38:61])
        self.assertEqual(report[61:], bytes(3))

    def test_wide_widget_golden_vector_and_semantic_readback(self):
        edits = {0: ["system_media", None, None, "polling_rate"]}
        report = build_lcd_report(self.state, pages=edits)
        self.assertEqual(report[5:16].hex(), "0100006600000000006500")
        # A valid normalized feature response: status replaces write length.
        response = bytearray(report[:61])
        response[2] = 0
        response[-1] = 0x37  # response trailer does not affect settings equality
        actual = decode_lcd_response(response, 0)
        self.assertEqual(actual.signature, edited_lcd_signature(self.state, pages=edits))

    def test_captured_hardware_write_and_restore_semantics(self):
        before = bytearray(CAPTURE)
        before[3] = 1
        before[60] = 0x80
        state = decode_lcd_response(before, 1)
        after = bytes.fromhex(
            "1025000103010000fe006300660064000200000000000065001500"
            "0300001f001a001900470004000000000000000000000500000000000000000000af")
        restored = bytes.fromhex(
            "102500010301000064000000000001000200000000000065001500"
            "0300001f001a00190047000400000000000000000000050000000000000000000075")
        edits = {0: ["dpi", "polling_rate", "led_brightness", "empty"]}
        self.assertEqual(decode_lcd_response(after, 1).signature,
                         edited_lcd_signature(state, pages=edits))
        self.assertEqual(decode_lcd_response(restored, 1).signature, state.signature)
        self.assertNotEqual(restored, bytes(before))

    def test_unknown_widgets_and_reserved_records_survive_other_page_edit(self):
        response = bytearray(CAPTURE)
        response[6:8] = b"\xaa\xbb"
        response[30:32] = b"\xd4\x37"  # unknown pair on third page
        response[39:49] = bytes(range(1, 11))
        response[50:60] = bytes(range(11, 21))
        state = decode_lcd_response(response, 0)
        report = build_lcd_report(state, pages={0: ["dpi"] * 4})
        self.assertEqual(report[6:8], b"\xaa\xbb")
        self.assertEqual(report[30:32], b"\xd4\x37")
        self.assertEqual(report[38:61], response[38:61])
        unknown = state.pages[2].slots[3]
        self.assertEqual(unknown.key, "unknown_d4_37")
        build_lcd_report(state, pages={2: list(state.pages[2].slots)})
        with self.assertRaises(ProtocolError):
            build_lcd_report(state, pages={0: [unknown, "empty", "empty", "empty"]})
        with self.assertRaises(ProtocolError):
            build_lcd_report(state, pages={0: [LcdWidget("invented", "Invented", 0x88)] * 4})

    def test_reply_identity_length_and_page_ids_are_strict(self):
        for value in (CAPTURE[:-1], CAPTURE + bytes(3), b"", "not bytes"):
            with self.subTest(value=value), self.assertRaises(ProtocolError):
                decode_lcd_response(value, 0)
        for offset, value in ((0, 4), (1, 0x24), (2, 1), (3, 1), (4, 0), (4, 6), (5, 2), (49, 4)):
            bad = bytearray(CAPTURE)
            bad[offset] = value
            with self.subTest(offset=offset, value=value), self.assertRaises(ProtocolError):
                decode_lcd_response(bad, 0)
        with self.assertRaises(ProtocolError):
            decode_lcd_response(CAPTURE, True)

    def test_unsafe_page_edits_and_wide_overlap_are_rejected(self):
        for edits in (
            {3: ["dpi"] * 4}, {True: ["dpi"] * 4}, {"0": ["dpi"] * 4},
            {0: ["system_media", "dpi", "empty", "empty"]},
            {0: ["empty", "empty", "empty", "system_media"]},
            {0: [None, "dpi", "dpi", "dpi"]}, {0: ["clock"] * 4},
            {0: "dpi"}, {0: ["dpi"] * 3}, [],
        ):
            with self.subTest(edits=edits), self.assertRaises(ProtocolError):
                build_lcd_report(self.state, pages=edits)
        response = bytearray(CAPTURE)
        response[4] = 1
        with self.assertRaises(ProtocolError):
            build_lcd_report(decode_lcd_response(response, 0), pages={1: ["dpi"] * 4})

    def test_modified_state_cannot_bypass_fresh_raw_validation(self):
        forged = dataclasses.replace(self.state, page_count=1)
        with self.assertRaises(ProtocolError):
            build_lcd_report(forged, pages={})
        with self.assertRaises(ProtocolError):
            build_lcd_report(None, pages={})

    def test_overfull_raw_page_is_not_silently_truncated(self):
        response = bytearray(CAPTURE)
        response[8:16] = bytes.fromhex("0100010064006600")
        with self.assertRaises(ProtocolError):
            decode_lcd_response(response, 0)

    def test_media_subtypes_remain_distinct(self):
        edits = {0: ["play_pause", "next_track", "previous_track", "stop"]}
        report = build_lcd_report(self.state, pages=edits)
        self.assertEqual(report[8:16].hex(), "6003600260016000")
        response = bytearray(report[:61])
        response[2] = 0
        decoded = decode_lcd_response(response, 0)
        self.assertEqual([w.wire_subtype for w in decoded.pages[0].slots], [0, 1, 2, 3])
        self.assertEqual(LCD_WIDGETS["system_media"].width, 3)

    def test_general_media_is_a_distinct_three_slot_static_layout(self):
        widget = LCD_WIDGETS["general_media"]
        self.assertEqual(LCD_GENERAL_MEDIA_APP_ID, 1024)
        self.assertEqual(widget.label, "General media controls")
        self.assertEqual(widget.signature, (0x45, 0))
        self.assertEqual(widget.width, 3)
        self.assertNotEqual(widget.signature, LCD_WIDGETS["system_media"].signature)

        edits = {0: ["general_media", None, None, "dpi"]}
        report = build_lcd_report(self.state, pages=edits)
        self.assertEqual(report[5:16].hex(), "0100006400000000004500")
        response = bytearray(report[:61])
        response[2] = 0
        decoded = decode_lcd_response(response, 0)
        self.assertEqual(
            [None if value is None else value.key for value in decoded.pages[0].slots],
            ["general_media", None, None, "dpi"],
        )

        for invalid in (
            ["general_media", "empty", None, "empty"],
            ["empty", "empty", "general_media", None],
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ProtocolError):
                build_lcd_report(self.state, pages={0: invalid})

    def test_key_and_macro_tiles_use_distinct_type_five_subtypes(self):
        self.assertEqual(LCD_WIDGETS["remap_key"].signature, (0x05, 0))
        self.assertEqual(LCD_WIDGETS["hotkey"].signature, (0x05, 1))
        self.assertEqual(LCD_WIDGETS["macro"].signature, (0x05, 2))
        edits = {0: ["remap_key", "hotkey", "macro", "empty"]}
        report = build_lcd_report(self.state, pages=edits)
        self.assertEqual(report[8:16].hex(), "fe00050205010500")
        response = bytearray(report[:61])
        response[2] = 0
        decoded = decode_lcd_response(response, 0)
        self.assertEqual([widget.key for widget in decoded.pages[0].slots],
                         ["remap_key", "hotkey", "macro", "empty"])

    def test_system_monitoring_widget_wire_types_are_distinct(self):
        self.assertEqual({key: LCD_WIDGETS[key].wire_type for key in (
            "gpu_temperature", "gpu_load", "cpu_temperature", "cpu_load", "ram_usage")}, {
                "gpu_temperature": 0x37, "gpu_load": 0x38,
                "cpu_temperature": 0x39, "cpu_load": 0x3A, "ram_usage": 0x41,
            })
        edits = {0: ["gpu_temperature", "gpu_load", "cpu_temperature", "cpu_load"]}
        report = build_lcd_report(self.state, pages=edits)
        self.assertEqual(report[8:16].hex(), "3a00390038003700")

    def test_onboard_shortcut_catalog_ids_and_wire_records_are_distinct(self):
        self.assertEqual(dict(LCD_ONBOARD_SHORTCUT_APP_IDS), {
            "emoji": 31,
            "launch_browser": 34,
            "browser_back": 35,
            "browser_forward": 36,
            "calculator": 37,
            "screenshot": 26,
        })
        self.assertEqual(LCD_ONBOARD_SHORTCUT_WIDGETS,
                         frozenset(LCD_ONBOARD_SHORTCUT_APP_IDS))
        self.assertEqual({key: LCD_WIDGETS[key].signature
                          for key in LCD_ONBOARD_SHORTCUT_APP_IDS}, {
            "emoji": (0x18, 0x00),
            "launch_browser": (0x1B, 0x00),
            "browser_back": (0x1C, 0x00),
            "browser_forward": (0x1D, 0x00),
            "calculator": (0x1E, 0x00),
            "screenshot": (0x3E, 0x00),
        })

    def test_onboard_shortcut_layout_golden_vectors_round_trip(self):
        edits = {
            0: ["emoji", "launch_browser", "browser_back", "browser_forward"],
            1: ["calculator", "screenshot", "empty", "empty"],
        }
        report = build_lcd_report(self.state, pages=edits)
        self.assertEqual(report[8:16].hex(), "1d001c001b001800")
        self.assertEqual(report[19:27].hex(), "fe00fe003e001e00")
        response = bytearray(report[:LCD_RESPONSE_BYTES])
        response[2] = 0
        decoded = decode_lcd_response(response, 0)
        self.assertEqual([[widget.key for widget in page.slots]
                          for page in decoded.pages[:2]], list(edits.values()))


if __name__ == "__main__":
    unittest.main()
