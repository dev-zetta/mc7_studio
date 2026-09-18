"""Command 0x29 screen-key vectors and state-preservation checks."""

from dataclasses import replace
import unittest

from swarm2.lcd_commands import decode_lcd_response
from swarm2.protocol import ProtocolError, SUPPORTED_COMMAND_IDS
from swarm2.screen_key_commands import (
    DEVICE_BINDING_PREFIX, SCREEN_KEY_RESPONSES_BYTES, build_screen_key_get_buffer,
    build_screen_key_read_request, build_screen_key_reports, decode_lcd_macro_record,
    decode_screen_key_record, decode_screen_key_responses, encode_lcd_macro_record,
    encode_screen_key_record, expected_screen_key_state,
    opaque_screen_key_binding, screen_key_bindings,
)


LCD_CAPTURE = bytes.fromhex(
    "1025000003"
    "01000064ff010000ff00ff"
    "02000065ff15ff00ff00ff"
    "0300001fff1aff19ff47ff"
    "0400000000000000000000"
    "0500000000000000000000"
    "81"
)


def key_record(action: str, widget: str, *, label: bytes | None = None,
               reserved: int = 0) -> bytes:
    result = bytearray(encode_screen_key_record(action, widget))
    result[4] = reserved
    if label is not None:
        if len(label) != 6:
            raise ValueError("test labels contain six bytes")
        result[5:11] = label
    return bytes(result)


def page_response(profile: int, page: int, records=(), *, reserved: int = 0) -> bytes:
    records = tuple(records)
    records += (bytes(11),) * (4 - len(records))
    raw = bytearray(bytes((0x10, 0x29, 0, 0x29, 0x32, 0, profile, page + 1,
                           reserved))
                    + b"".join(reversed(records)) + b"\0")
    raw[-1] = (-sum(raw[2:-1])) & 0xFF
    return bytes(raw)


class ScreenKeyCommandTests(unittest.TestCase):
    def setUp(self):
        self.simple = key_record("A", "remap_key", label=b"Alpha\0", reserved=0x7A)
        self.hotkey = key_record("Ctrl+Shift+S", "hotkey")
        self.opaque = bytes.fromhex("123456789a") + b"opaque"
        self.raw = b"".join((
            page_response(0, 0, (self.simple, self.hotkey, bytes(11), self.opaque), reserved=0xA1),
            page_response(0, 1),
            page_response(0, 2),
        ))
        self.state = decode_screen_key_responses(self.raw, 0)
        self.lcd = decode_lcd_response(LCD_CAPTURE, 0)
        key_lcd_raw = bytearray(LCD_CAPTURE)
        key_lcd_raw[8:16] = bytes.fromhex("64fffeff05010500")
        self.key_lcd = decode_lcd_response(key_lcd_raw, 0)
        self.current_pages = [
            ["remap_key", "hotkey", "empty", "dpi"],
            ["game_bar", "system_media", None, None],
            ["cut", "copy", "paste", "undo"],
        ]

    def test_read_requests_and_get_buffer_match_recovered_headers(self):
        self.assertIn(0x29, SUPPORTED_COMMAND_IDS)
        self.assertEqual(build_screen_key_read_request(4, 2),
                         bytes.fromhex("101c0029040300") + bytes(57))
        self.assertEqual(build_screen_key_get_buffer(),
                         bytes.fromhex("102900293200") + bytes(58))
        for profile, page in ((-1, 0), (5, 0), (True, 0), (0, -1), (0, 3), (0, True)):
            with self.subTest(profile=profile, page=page), self.assertRaises(ProtocolError):
                build_screen_key_read_request(profile, page)

    def test_three_page_response_decodes_exact_records_reserved_and_signature(self):
        self.assertEqual(len(self.raw), SCREEN_KEY_RESPONSES_BYTES)
        self.assertEqual(self.state.raw, self.raw)
        self.assertEqual(len(self.state.pages), 3)
        self.assertEqual(self.state.pages[0].records,
                         (self.simple, self.hotkey, bytes(11), self.opaque))
        self.assertEqual(self.state.pages[0].raw[8], 0xA1)
        self.assertEqual(self.state.signature,
                         (0, tuple(page.signature for page in self.state.pages)))

    def test_known_bindings_empty_and_opaque_records_are_distinct(self):
        bindings = screen_key_bindings(self.state, self.current_pages)
        self.assertEqual(bindings[0][:3], ("A", "Ctrl+Shift+S", None))
        # A non-key tile never exposes its hidden record as an editable binding.
        self.assertIsNone(bindings[0][3])
        self.assertEqual(decode_screen_key_record(self.simple, "remap_key"), "A")
        self.assertEqual(decode_screen_key_record(self.hotkey, "hotkey"), "Ctrl+Shift+S")
        self.assertEqual(opaque_screen_key_binding(self.opaque),
                         DEVICE_BINDING_PREFIX + self.opaque.hex())

    def test_keyboard_records_reuse_button_encoding_and_ascii_label_layout(self):
        self.assertEqual(encode_screen_key_record("A", "remap_key"),
                         bytes.fromhex("0000040c00") + b"A\0\0\0\0\0")
        self.assertEqual(encode_screen_key_record("Cmd+F5", "hotkey"),
                         bytes.fromhex("003e080600") + b"F5\0\0\0\0")
        self.assertEqual(encode_screen_key_record("Escape", "remap_key")[5:], b"Escap\0")
        # Hot-key records permit a zero modifier mask but not a modifier as the final key.
        self.assertEqual(encode_screen_key_record("S", "hotkey")[:4], bytes.fromhex("00160006"))
        with self.assertRaises(ProtocolError):
            encode_screen_key_record("Ctrl+S", "remap_key")
        with self.assertRaises(ProtocolError):
            encode_screen_key_record("Ctrl", "hotkey")

    def test_lcd_macro_record_uses_finite_touch_mode_and_truncated_name(self):
        record = encode_lcd_macro_record("Compile project", "repeat")
        self.assertEqual(record, bytes.fromhex("0001000700") + b"Compi\0")
        self.assertEqual(decode_lcd_macro_record(record), "Compi")
        toggle = encode_lcd_macro_record("Toggle macro", "toggle")
        self.assertEqual(toggle, bytes.fromhex("0001020700") + b"Toggl\0")
        self.assertEqual(decode_lcd_macro_record(toggle), "Toggl")
        stale_tail = bytes.fromhex("0001000700") + b"A\0old\0"
        self.assertEqual(decode_lcd_macro_record(stale_tail), "A")
        for playback in ("while_held", "invalid"):
            with self.subTest(playback=playback), self.assertRaises(ProtocolError):
                encode_lcd_macro_record("Macro", playback)
        for record in (bytes(11), bytes.fromhex("0001010700") + b"Macro\0",
                       bytes.fromhex("0001000701") + b"Macro\0"):
            with self.subTest(record=record), self.assertRaises(ProtocolError):
                decode_lcd_macro_record(record)

    def test_new_macro_tile_gets_trigger_record_before_layout_write(self):
        desired_pages = [list(row) for row in self.current_pages]
        desired_pages[0][2] = "macro"
        bindings = [list(row) for row in screen_key_bindings(self.state, self.lcd)]
        macro_records = [[None] * 4 for _ in range(3)]
        macro_records[0][2] = encode_lcd_macro_record("Build")
        reports, expected = build_screen_key_reports(
            self.state, self.lcd, desired_pages, bindings, macro_records)
        self.assertEqual(len(reports), 1)
        self.assertEqual(expected.pages[0].records[2], macro_records[0][2])
        # Page records are emitted right-to-left after the seven-byte header.
        self.assertEqual(reports[0][18:29], macro_records[0][2])
        self.assertEqual(decode_lcd_macro_record(expected.pages[0].records[2]), "Build")

    def test_existing_macro_tile_preserves_opaque_trigger_when_no_replacement_requested(self):
        macro_record = bytes.fromhex("0001020700") + b"Toggl\0"
        raw = (page_response(0, 0, (self.simple, self.hotkey, macro_record, self.opaque),
                             reserved=0xA1) + self.raw[54:])
        state = decode_screen_key_responses(raw, 0)
        lcd_raw = bytearray(LCD_CAPTURE)
        lcd_raw[8:16] = bytes.fromhex("64ff050205010500")
        lcd = decode_lcd_response(lcd_raw, 0)
        pages = [[None if widget is None else widget.key for widget in page.slots]
                 for page in lcd.pages[:3]]
        bindings = [list(row) for row in screen_key_bindings(state, pages)]
        reports, expected = build_screen_key_reports(state, lcd, pages, bindings)
        self.assertEqual(reports, ())
        self.assertEqual(expected.pages[0].records[2], macro_record)

    def test_semantically_unchanged_macro_record_preserves_qstrncpy_stale_tail(self):
        macro_record = bytes.fromhex("0001000700") + b"A\0old\0"
        raw = (page_response(0, 0, (self.simple, self.hotkey, macro_record, self.opaque),
                             reserved=0xA1) + self.raw[54:])
        state = decode_screen_key_responses(raw, 0)
        lcd_raw = bytearray(LCD_CAPTURE)
        lcd_raw[8:16] = bytes.fromhex("64ff050205010500")
        lcd = decode_lcd_response(lcd_raw, 0)
        pages = [[None if widget is None else widget.key for widget in page.slots]
                 for page in lcd.pages[:3]]
        bindings = [list(row) for row in screen_key_bindings(state, pages)]
        macro_records = [[None] * 4 for _ in range(3)]
        macro_records[0][2] = encode_lcd_macro_record("A")
        reports, expected = build_screen_key_reports(
            state, lcd, pages, bindings, macro_records)
        self.assertEqual(reports, ())
        self.assertEqual(expected.pages[0].records[2], macro_record)

    def test_only_changed_pages_are_written_and_reread_state_is_predicted(self):
        desired = [list(row) for row in screen_key_bindings(self.state, self.current_pages)]
        desired[0][1] = "Meta+F5"
        reports, expected = build_screen_key_reports(
            self.state, self.key_lcd, self.current_pages, desired)
        self.assertEqual(len(reports), 1)
        self.assertEqual(len(reports[0]), 64)
        self.assertEqual(reports[0][:7], bytes.fromhex("10293200000100"))
        self.assertEqual(reports[0][7:18], self.opaque)
        self.assertEqual(reports[0][29:40], encode_screen_key_record("Meta+F5", "hotkey"))
        self.assertEqual(reports[0][40:51], self.simple)
        self.assertEqual(reports[0][51:], bytes(13))
        self.assertEqual(expected.pages[0].records[0], self.simple)
        self.assertEqual(expected.pages[0].records[1],
                         encode_screen_key_record("Meta+F5", "hotkey"))
        self.assertEqual(expected.pages[0].raw[8], 0xA1)
        self.assertEqual(sum(expected.pages[0].raw[2:]) & 0xFF, 0)
        self.assertEqual(expected.signature,
                         expected_screen_key_state(self.state, self.key_lcd,
                                                   self.current_pages, desired).signature)

    def test_semantically_unchanged_known_record_preserves_label_and_reserved_byte(self):
        desired = [list(row) for row in screen_key_bindings(self.state, self.current_pages)]
        reports, expected = build_screen_key_reports(
            self.state, self.key_lcd, self.current_pages, desired)
        self.assertEqual(reports, ())
        self.assertEqual(expected, self.state)
        self.assertEqual(expected.pages[0].records[0], self.simple)
        self.assertEqual(expected.pages[0].records[0][4:], b"\x7aAlpha\0")

    def test_layout_change_clears_hidden_records_before_new_binding_encoding(self):
        # The captured LCD starts with a wide non-key tile over slots 0..2.
        # Its command-0x29 bytes must not become active merely because key
        # widgets replace that layout.
        bindings = [list(row) for row in screen_key_bindings(self.state, self.lcd)]
        reports, expected = build_screen_key_reports(
            self.state, self.lcd, self.current_pages, bindings)
        self.assertEqual(len(reports), 1)
        self.assertEqual(expected.pages[0].records[:3], (bytes(11), bytes(11), bytes(11)))
        self.assertEqual(expected.pages[0].records[3], self.opaque)

        bindings[0][0] = "A"
        _, expected = build_screen_key_reports(
            self.state, self.lcd, self.current_pages, bindings)
        self.assertEqual(expected.pages[0].records[0],
                         encode_screen_key_record("A", "remap_key"))
        self.assertNotEqual(expected.pages[0].records[0], self.simple)

    def test_hidden_non_key_record_is_preserved_until_its_tile_changes(self):
        bindings = [list(row) for row in screen_key_bindings(self.state, self.current_pages)]
        reports, expected = build_screen_key_reports(
            self.state, self.key_lcd, self.current_pages, bindings)
        self.assertEqual(reports, ())
        self.assertEqual(expected.pages[0].records[3], self.opaque)

        changed_pages = [list(row) for row in self.current_pages]
        changed_pages[0][3] = "game_bar"
        reports, expected = build_screen_key_reports(
            self.state, self.key_lcd, changed_pages, bindings)
        self.assertEqual(reports[0][7:18], bytes(11))
        self.assertEqual(expected.pages[0].records[3], bytes(11))

        invalid = [list(row) for row in bindings]
        invalid[0][3] = "A"
        with self.assertRaisesRegex(ProtocolError, "Only remap_key and hotkey"):
            build_screen_key_reports(self.state, self.key_lcd, self.current_pages, invalid)

    def test_opaque_marker_requires_exact_record_same_slot_and_same_tile(self):
        # Change the current physical slot from DPI to a key tile so its opaque
        # command-0x29 record is surfaced to configuration as a device marker.
        lcd_raw = bytearray(LCD_CAPTURE)
        lcd_raw[8:10] = bytes((0x05, 0x00))
        key_lcd = decode_lcd_response(lcd_raw, 0)
        key_pages = [[None if widget is None else widget.key for widget in page.slots]
                     for page in key_lcd.pages[:3]]
        bindings = [list(row) for row in screen_key_bindings(self.state, key_pages)]
        marker = bindings[0][3]
        self.assertEqual(marker, opaque_screen_key_binding(self.opaque))
        reports, expected = build_screen_key_reports(
            self.state, key_lcd, key_pages, bindings)
        self.assertEqual(reports, ())
        self.assertEqual(expected.pages[0].records[3], self.opaque)

        moved = [list(row) for row in bindings]
        moved[0][2], moved[0][3] = marker, None
        moved_pages = [list(row) for row in key_pages]
        moved_pages[0] = ["empty", "empty", "hotkey", "remap_key"]
        with self.assertRaises(ProtocolError):
            build_screen_key_reports(self.state, key_lcd, moved_pages, moved)
        imported = [list(row) for row in bindings]
        imported[1][0] = marker
        imported_pages = [list(row) for row in key_pages]
        imported_pages[1] = ["remap_key", "system_media", None, None]
        with self.assertRaisesRegex(ProtocolError, "unchanged logical tile"):
            build_screen_key_reports(self.state, key_lcd, imported_pages, imported)
        changed_tile = [list(row) for row in key_pages]
        changed_tile[0][3] = "hotkey"
        with self.assertRaisesRegex(ProtocolError, "unchanged logical tile"):
            build_screen_key_reports(self.state, key_lcd, changed_tile, bindings)

    def test_malformed_responses_identity_checksum_and_lengths_are_rejected(self):
        bad_values = [self.raw[:-1], self.raw + b"\0", b"", list(self.raw)]
        for value in bad_values:
            with self.subTest(value_type=type(value)), self.assertRaises(ProtocolError):
                decode_screen_key_responses(value, 0)
        for offset, value in ((0, 0), (1, 0x28), (2, 1), (3, 0), (4, 0),
                              (5, 1), (6, 1), (7, 2), (53, self.raw[53] ^ 1),
                              (54 + 7, 1)):
            bad = bytearray(self.raw)
            bad[offset] = value
            with self.subTest(offset=offset), self.assertRaises(ProtocolError):
                decode_screen_key_responses(bad, 0)

    def test_invalid_matrices_bindings_and_forged_states_are_rejected(self):
        bindings = screen_key_bindings(self.state, self.current_pages)
        invalid = (
            (self.current_pages[:2], bindings),
            ([row[:3] for row in self.current_pages], bindings),
            (self.current_pages, bindings[:2]),
            (self.current_pages, [list(row[:3]) for row in bindings]),
            (self.current_pages, [[object()] * 4 for _ in range(3)]),
        )
        for pages, desired in invalid:
            with self.subTest(pages=pages), self.assertRaises(ProtocolError):
                build_screen_key_reports(self.state, self.key_lcd, pages, desired)
        desired = [list(row) for row in bindings]
        desired[1][0] = "A"
        with self.assertRaisesRegex(ProtocolError, "remap_key and hotkey"):
            build_screen_key_reports(self.state, self.key_lcd, self.current_pages, desired)
        with self.assertRaises(ProtocolError):
            build_screen_key_reports(replace(self.state, profile_index=1), self.key_lcd,
                                     self.current_pages, bindings)
        with self.assertRaises(ProtocolError):
            build_screen_key_reports(self.state, replace(self.key_lcd, profile_index=1),
                                     self.current_pages, bindings)

    def test_invalid_or_non_ascii_labels_make_records_opaque(self):
        pages = [["remap_key"] * 4] + [["dpi"] * 4 for _ in range(2)]
        for label in (b"ABCDEF", b"A\0B\0\0\0", b"\xff\0\0\0\0\0", bytes(6)):
            record = key_record("A", "remap_key", label=label)
            raw = page_response(0, 0, (record,)) + self.raw[54:]
            state = decode_screen_key_responses(raw, 0)
            self.assertEqual(screen_key_bindings(state, pages)[0][0],
                             opaque_screen_key_binding(record))


if __name__ == "__main__":
    unittest.main()
