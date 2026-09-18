"""MC7 button captures and static-source vectors; no hardware access."""

from dataclasses import replace
import unittest

from swarm2.button_commands import (
    ACTION_CODES, BUTTON_SLOTS, build_button_get_buffer, build_button_read_request,
    build_button_report, decode_action, decode_button_response, encode_action, replace_actions,
)
from swarm2.configuration import Action
from swarm2.protocol import ProtocolError


# Wired profile zero, captured by the root agent on 2026-09-15; HID GET = 64.
PRIMARY = bytes.fromhex(
    "101500000000010100000201000003010000090100000a010000050100000601"
    "000001020000010a0000060300000801000007010000000000000000000000ad"
)
SECONDARY = bytes.fromhex(
    "1016000000000101000002010000040300000703000008030000060400000504"
    "000001080000010a0000000000000303000002030000000000000000000000ad"
)


class ButtonCommandTests(unittest.TestCase):
    def test_capture_maps_all_eleven_visible_controls_and_preserves_twelfth_slot(self):
        state = decode_button_response(PRIMARY, 0)
        self.assertEqual(len(state.records), 12)
        self.assertEqual(BUTTON_SLOTS, (0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 11))
        expected = [Action("mouse", value) for value in
                    ("left", "right", "middle", "scroll_up", "scroll_down", "forward", "back")]
        expected += [Action("dpi", "cycle"), Action("easy_shift", "hold"),
                     Action("mouse", "tilt_right"), Action("mouse", "tilt_left")]
        self.assertEqual([decode_action(state.records[slot]) for slot in BUTTON_SLOTS], expected)
        self.assertEqual(state.records[9], bytes.fromhex("00000603"))
        self.assertEqual(state.raw, PRIMARY)

    def test_read_selectors_include_explicit_profile_layer_and_report(self):
        self.assertEqual(build_button_read_request(4, "primary"), bytes.fromhex("101c0015040000") + bytes(57))
        self.assertEqual(build_button_read_request(0, "easy_shift"), bytes.fromhex("101c0016000000") + bytes(57))
        self.assertEqual(build_button_get_buffer("easy_shift"), bytes.fromhex("1016") + bytes(62))

    def test_full_write_vector_edits_one_side_button_and_preserves_every_other_slot(self):
        state = decode_button_response(PRIMARY, 0)
        report = build_button_report(state, {5: Action("media", "play_pause")})
        expected = bytes.fromhex(
            "1015000000010100000201000003010000090100000a010000040300000601"
            "000001020000010a000006030000080100000701"
        ) + bytes(13)
        self.assertEqual(report, expected)
        records = tuple(report[3 + 4 * i:7 + 4 * i] for i in range(12))
        self.assertEqual(records, replace_actions(state, {5: Action("media", "play_pause")}))
        self.assertEqual([index for index, (a, b) in enumerate(zip(records, state.records)) if a != b], [5])
        self.assertEqual(state.raw, PRIMARY)

    def test_easy_shift_uses_separate_command_and_preserves_key_aliases(self):
        state = decode_button_response(SECONDARY, 0, "easy_shift")
        self.assertEqual(decode_action(state.records[5]), Action("keyboard", "PageDown"))
        self.assertEqual(decode_action(state.records[6]), Action("keyboard", "PageUp"))
        changes = {5: Action("keyboard", "PageDown"), 10: Action("keyboard", "Ctrl+Shift+S")}
        records = replace_actions(state, changes)
        self.assertEqual(records[5], state.records[5])
        self.assertEqual(records[10], bytes.fromhex("00160306"))
        self.assertEqual(build_button_report(state, changes)[:3], bytes.fromhex("101600"))

    def test_action_codes_match_catalog_and_roundtrip(self):
        vectors = {
            ("mouse", "left"): "00000101", ("mouse", "back"): "00000601",
            ("media", "play_pause"): "00000403", ("media", "mute"): "00000603",
            ("profile", "5"): "00000808", ("easy_shift", "toggle"): "0000030a",
            ("keyboard", "A"): "0000040c", ("keyboard", "F24"): "0000730c",
            ("keyboard", "Ctrl+S"): "00160106",
            ("keyboard", "Meta+F5"): "003e0806",
            ("keyboard", "Ctrl+Shift+Alt+Meta+Enter"): "00280f06",
        }
        for action, hex_value in vectors.items():
            with self.subTest(action=action):
                self.assertEqual(encode_action(Action(*action)).hex(), hex_value)
                self.assertEqual(decode_action(bytes.fromhex(hex_value)), Action(*action))
        for action in ACTION_CODES:
            with self.subTest(action=action):
                self.assertEqual(decode_action(encode_action(Action(*action))), Action(*action))

    def test_launch_and_easy_wheel_catalog_records_roundtrip(self):
        vectors = {
            ("launch", "browser"): "00000903",
            ("launch", "calculator"): "00001103",
            ("easy_wheel", "dpi"): "00000309",
            ("easy_wheel", "volume"): "00000409",
            ("easy_wheel", "alt_tab"): "00000509",
            ("easy_wheel", "desktop"): "00000609",
        }
        for action, record in vectors.items():
            with self.subTest(action=action):
                encoded = encode_action(Action(*action))
                self.assertEqual(encoded, bytes.fromhex(record))
                self.assertEqual(decode_action(encoded), Action(*action))

    def test_custom_easy_aim_dpi_uses_big_endian_step_index(self):
        vectors = {
            50: "00000c02",
            200: "00030c02",
            1600: "001f0c02",
            30000: "02570c02",
        }
        for dpi, hex_value in vectors.items():
            action = Action("dpi", f"precision_custom_{dpi}")
            with self.subTest(dpi=dpi):
                self.assertEqual(encode_action(action), bytes.fromhex(hex_value))
                self.assertEqual(decode_action(bytes.fromhex(hex_value)), action)
        self.assertIsNone(decode_action(bytes.fromhex("02580c02")))

    def test_macos_shortcut_aliases_compile_to_gui_modifier(self):
        self.assertEqual(encode_action(Action("keyboard", "Cmd+C")), bytes.fromhex("00060806"))
        self.assertEqual(encode_action(Action("keyboard", "Option+Left")), bytes.fromhex("00500406"))

    def test_unknown_assignment_is_preserved_in_unrelated_edit(self):
        raw = bytearray(PRIMARY)
        raw[4 + 8 * 4:8 + 8 * 4] = bytes.fromhex("7a12347e")
        state = decode_button_response(raw, 0)
        self.assertIsNone(decode_action(state.records[8]))
        report = build_button_report(state, {5: Action("keyboard", "F5")})
        self.assertEqual(report[3 + 8 * 4:7 + 8 * 4], bytes.fromhex("7a12347e"))

    def test_hidden_slot_and_essential_click_removal_are_rejected(self):
        state = decode_button_response(PRIMARY, 0)
        for changes in ({9: Action("disabled", "")}, {0: Action("disabled", "")},
                        {1: Action("keyboard", "A")}, {True: Action("disabled", "")}):
            with self.subTest(changes=changes), self.assertRaises(ProtocolError):
                build_button_report(state, changes)
        # Moving a click is allowed if another visible primary button retains it.
        records = replace_actions(state, {0: Action("keyboard", "A"), 5: Action("mouse", "left")})
        self.assertEqual(decode_action(records[5]), Action("mouse", "left"))

    def test_malformed_identity_lengths_layers_and_baselines_are_rejected(self):
        for raw in (PRIMARY[:53], PRIMARY[:-1], PRIMARY + b"\0", list(PRIMARY)):
            with self.subTest(raw=raw), self.assertRaises(ProtocolError):
                decode_button_response(raw, 0)
        for offset, value in ((0, 1), (1, 0x16), (2, 1), (3, 1)):
            raw = bytearray(PRIMARY)
            raw[offset] = value
            with self.subTest(offset=offset), self.assertRaises(ProtocolError):
                decode_button_response(raw, 0)
        for profile in (-1, 5, True, "0"):
            with self.subTest(profile=profile), self.assertRaises(ProtocolError):
                build_button_read_request(profile)
        for layer in (None, 0x15, "secondary", "macro"):
            with self.subTest(layer=layer), self.assertRaises(ProtocolError):
                build_button_get_buffer(layer)
        state = decode_button_response(PRIMARY, 0)
        with self.assertRaisesRegex(ProtocolError, "baseline records"):
            replace_actions(replace(state, records=(bytes(4),) * 12), {})

    def test_unsupported_actions_are_never_silently_mapped(self):
        for action in (Action("shell", "example"), Action("macro", "pending-upload"),
                       Action("dpi", "precision_custom_0"), Action("dpi", "precision_custom_51"),
                       Action("dpi", "precision_custom_30050"), Action("dpi", "precision_custom_0050"),
                       Action("dpi", "precision_custom_" + "9" * 1000),
                       Action("keyboard", "Ctrl+S;example"), Action("keyboard", "A+B"),
                       Action("keyboard", "Ctrl+Control+S"), Action("keyboard", "Ctrl+Shift"),
                       Action("keyboard", "Ctrl+"), Action("unknown", "00000101")):
            with self.subTest(action=action), self.assertRaises(ProtocolError):
                encode_action(action)


if __name__ == "__main__":
    unittest.main()
