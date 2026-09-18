"""Exact offline vectors for MC7 General Media app 1024."""

import unittest

from swarm2.general_media_commands import (
    GeneralMediaAction,
    GeneralMediaDisplayState,
    GeneralMediaSlotForm,
    build_general_media_report,
    decode_general_media_press,
)
from swarm2.protocol import ProtocolError


class GeneralMediaDecoderTests(unittest.TestCase):
    def test_all_five_subtypes_decode_to_the_original_actions(self):
        expected = (
            GeneralMediaAction.SHUFFLE,
            GeneralMediaAction.NEXT,
            GeneralMediaAction.PLAY_PAUSE,
            GeneralMediaAction.PREVIOUS,
            GeneralMediaAction.REPEAT,
        )
        for subtype, action in enumerate(expected):
            with self.subTest(subtype=subtype, action=action):
                raw = bytes((0x10, 0x33, 0x13, 0x45, subtype, 1, 1, 0))
                press = decode_general_media_press(raw)
                self.assertEqual(
                    (press.page_index, press.logical_slot, press.action, press.raw),
                    (0, 0, action, raw),
                )

    def test_page_and_reversed_slot_coordinates_cover_the_wire_range(self):
        for page_index in range(3):
            for logical_slot in range(4):
                with self.subTest(page=page_index, slot=logical_slot):
                    packed = ((page_index + 1) << 4) | (3 - logical_slot)
                    press = decode_general_media_press(bytes((
                        0x10, 0x33, packed, 0x45, 2, 1, 1, 0,
                    )))
                    self.assertEqual(
                        (press.page_index, press.logical_slot),
                        (page_index, logical_slot),
                    )

    def test_release_is_ignored_and_bytearray_is_normalized(self):
        release = bytes.fromhex("1033134502010000")
        self.assertIsNone(decode_general_media_press(release))
        press = decode_general_media_press(bytearray.fromhex("1033134502010100"))
        self.assertIs(type(press.raw), bytes)

    def test_header_type_selector_status_and_trailer_are_exact(self):
        valid = bytearray.fromhex("1033134502010100")
        mutations = {
            "report_id": (0, 0x11),
            "event_family": (1, 0x34),
            "widget_type": (3, 0x65),
            "event_selector": (5, 0x00),
            "invalid_status": (6, 0x02),
            "trailer": (7, 0x01),
        }
        for name, (offset, value) in mutations.items():
            with self.subTest(field=name):
                malformed = valid.copy()
                malformed[offset] = value
                self.assertIsNone(decode_general_media_press(malformed))

    def test_unknown_subtype_is_not_claimed(self):
        self.assertIsNone(decode_general_media_press(
            bytes.fromhex("1033134505010100")
        ))

    def test_invalid_length_type_and_coordinate_raise_protocol_errors(self):
        for value in (None, "1033134502010100", memoryview(bytes(8)), bytes(7), bytes(9)):
            with self.subTest(value=value), self.assertRaises(ProtocolError):
                decode_general_media_press(value)
        for packed in (0x03, 0x43, 0x14):
            with self.subTest(packed=packed), self.assertRaises(ProtocolError):
                decode_general_media_press(bytes((
                    0x10, 0x33, packed, 0x45, 2, 1, 1, 0,
                )))


class GeneralMediaDisplayTests(unittest.TestCase):
    def test_exact_a3_0f_packet_uses_touch_coordinate_by_default(self):
        report = build_general_media_report(
            GeneralMediaDisplayState(True, True, True),
            page_index=0,
            logical_slot=0,
        )
        self.assertEqual(
            report,
            bytes.fromhex("10a3000101030f010001000001") + bytes(51),
        )

    def test_all_eight_flag_combinations_have_exact_offsets(self):
        for bits in range(8):
            repeat_active = bool(bits & 1)
            playing = bool(bits & 2)
            shuffle_active = bool(bits & 4)
            with self.subTest(
                repeat=repeat_active,
                playing=playing,
                shuffle=shuffle_active,
            ):
                report = build_general_media_report(
                    GeneralMediaDisplayState(
                        repeat_active, playing, shuffle_active),
                    page_index=1,
                    logical_slot=2,
                )
                expected = bytearray(64)
                expected[:13] = bytes((
                    0x10, 0xA3, 0, 1, 2, 1, 0x0F,
                    int(repeat_active), 0, int(playing), 0, 0,
                    int(shuffle_active),
                ))
                self.assertEqual(report, bytes(expected))
                self.assertEqual(len(report), 64)

    def test_both_original_slot_forms_cover_every_coordinate(self):
        state = GeneralMediaDisplayState(False, True, False)
        for page_index in range(3):
            for logical_slot in range(4):
                for slot_form, expected_slot in (
                    (GeneralMediaSlotForm.SCAN_INDEX, logical_slot),
                    (GeneralMediaSlotForm.TOUCH_COORDINATE, 3 - logical_slot),
                ):
                    with self.subTest(
                        page=page_index, slot=logical_slot, form=slot_form,
                    ):
                        report = build_general_media_report(
                            state,
                            page_index=page_index,
                            logical_slot=logical_slot,
                            slot_form=slot_form,
                        )
                        self.assertEqual(
                            report[:13],
                            bytes((
                                0x10, 0xA3, 0, 1,
                                page_index + 1, expected_slot, 0x0F,
                                0, 0, 1, 0, 0, 0,
                            )),
                        )
                        self.assertEqual(report[13:], bytes(51))

    def test_state_flags_require_actual_booleans(self):
        for field in range(3):
            for invalid in (0, 1, None, "true", object()):
                values = [False, False, False]
                values[field] = invalid
                with self.subTest(field=field, value=invalid), self.assertRaises(
                    ProtocolError
                ):
                    GeneralMediaDisplayState(*values)

    def test_position_form_and_state_types_are_strict(self):
        state = GeneralMediaDisplayState(False, False, False)
        for page_index in (-1, 3, False, 1.0, "0", None):
            with self.subTest(page=page_index), self.assertRaises(ProtocolError):
                build_general_media_report(
                    state, page_index=page_index, logical_slot=0)
        for logical_slot in (-1, 4, False, 1.0, "0", None):
            with self.subTest(slot=logical_slot), self.assertRaises(ProtocolError):
                build_general_media_report(
                    state, page_index=0, logical_slot=logical_slot)
        for slot_form in ("touch_coordinate", "scan_index", None, 0, True):
            with self.subTest(form=slot_form), self.assertRaises(ProtocolError):
                build_general_media_report(
                    state, page_index=0, logical_slot=0,
                    slot_form=slot_form,
                )
        for invalid_state in (None, object(), (False, False, False)):
            with self.subTest(state=invalid_state), self.assertRaises(ProtocolError):
                build_general_media_report(
                    invalid_state, page_index=0, logical_slot=0)

    def test_builder_revalidates_a_forged_frozen_state(self):
        state = GeneralMediaDisplayState(False, False, False)
        object.__setattr__(state, "playing", 1)
        with self.assertRaisesRegex(ProtocolError, "playing must be a boolean"):
            build_general_media_report(state, page_index=0, logical_slot=0)


if __name__ == "__main__":
    unittest.main()
