"""Exact pure vectors for Swarm II media-volume app 1025."""

import unittest

from swarm2.media_volume_commands import (
    MEDIA_VOLUME_APP_ID,
    MediaVolumeAction,
    apply_media_volume_action,
    decode_media_volume_press,
)
from swarm2.protocol import ProtocolError


class MediaVolumeDecoderTests(unittest.TestCase):
    def test_all_three_subtypes_decode_to_recovered_actions(self):
        expected = (
            MediaVolumeAction.DECREASE,
            MediaVolumeAction.INCREASE,
            MediaVolumeAction.SET_ONE,
        )
        for subtype, action in enumerate(expected):
            with self.subTest(subtype=subtype, action=action):
                raw = bytes((0x10, 0x33, 0x13, 0x7E, subtype, 1, 1, 0))
                press = decode_media_volume_press(
                    raw, app_id=MEDIA_VOLUME_APP_ID)
                self.assertEqual(
                    (
                        press.page_index,
                        press.wire_slot,
                        press.main_type,
                        press.event_type,
                        press.action,
                        press.raw,
                    ),
                    (0, 3, 0x7E, 1, action, raw),
                )

    def test_coordinates_cover_recovered_mc7_event_range_without_reversal(self):
        for page_index in range(3):
            for wire_slot in range(4):
                with self.subTest(page=page_index, wire_slot=wire_slot):
                    packed = ((page_index + 1) << 4) | wire_slot
                    press = decode_media_volume_press(
                        bytes((0x10, 0x33, packed, 0xA5, 0, 1, 1, 0)),
                        app_id=MEDIA_VOLUME_APP_ID,
                    )
                    self.assertEqual(
                        (press.page_index, press.wire_slot, press.main_type),
                        (page_index, wire_slot, 0xA5),
                    )

    def test_release_unrelated_app_and_unknown_subtype_are_ignored(self):
        release = bytes.fromhex("1033137e00010000")
        self.assertIsNone(decode_media_volume_press(
            release, app_id=MEDIA_VOLUME_APP_ID))
        press = bytes.fromhex("1033137e00010100")
        self.assertIsNone(decode_media_volume_press(press, app_id=1024))
        unknown_subtype = bytes.fromhex("1033137e03010100")
        self.assertIsNone(decode_media_volume_press(
            unknown_subtype, app_id=MEDIA_VOLUME_APP_ID))

    def test_header_selector_status_and_trailer_are_exact(self):
        valid = bytearray.fromhex("1033137e00010100")
        mutations = {
            "report_id": (0, 0x11),
            "event_family": (1, 0x34),
            "event_selector": (5, 0x00),
            "invalid_status": (6, 0x02),
            "trailer": (7, 0x01),
        }
        for name, (offset, value) in mutations.items():
            with self.subTest(field=name):
                malformed = valid.copy()
                malformed[offset] = value
                self.assertIsNone(decode_media_volume_press(
                    malformed, app_id=MEDIA_VOLUME_APP_ID))

    def test_bytearray_is_normalized_and_unresolved_main_type_is_preserved(self):
        press = decode_media_volume_press(
            bytearray.fromhex("103313ab02010100"),
            app_id=MEDIA_VOLUME_APP_ID,
        )
        self.assertIs(type(press.raw), bytes)
        self.assertEqual(press.main_type, 0xAB)

    def test_invalid_input_app_id_and_coordinates_raise_protocol_errors(self):
        for value in (
            None,
            "1033137e00010100",
            memoryview(bytes(8)),
            bytes(7),
            bytes(9),
        ):
            with self.subTest(value=value), self.assertRaises(ProtocolError):
                decode_media_volume_press(value, app_id=MEDIA_VOLUME_APP_ID)
        for app_id in (None, True, -1, 0x10000, 1025.0, "1025"):
            with self.subTest(app_id=app_id), self.assertRaises(ProtocolError):
                decode_media_volume_press(bytes(8), app_id=app_id)
        for packed in (0x03, 0x43, 0x14):
            with self.subTest(packed=packed), self.assertRaises(ProtocolError):
                decode_media_volume_press(
                    bytes((0x10, 0x33, packed, 0x7E, 0, 1, 1, 0)),
                    app_id=MEDIA_VOLUME_APP_ID,
                )


class MediaVolumeTransitionTests(unittest.TestCase):
    def test_source_exact_boundary_cases(self):
        cases = (
            (1, MediaVolumeAction.DECREASE, 0),
            (0, MediaVolumeAction.DECREASE, 0),
            (2, MediaVolumeAction.DECREASE, 0),
            (3, MediaVolumeAction.DECREASE, 1),
            (98, MediaVolumeAction.INCREASE, 100),
            (99, MediaVolumeAction.INCREASE, 100),
            (100, MediaVolumeAction.INCREASE, 100),
            (0, MediaVolumeAction.SET_ONE, 1),
            (100, MediaVolumeAction.SET_ONE, 1),
        )
        for current, action, expected in cases:
            with self.subTest(current=current, action=action):
                self.assertEqual(
                    apply_media_volume_action(current, action), expected)

    def test_every_valid_volume_matches_the_recovered_clamped_formula(self):
        for current in range(101):
            with self.subTest(current=current, action="decrease"):
                self.assertEqual(
                    apply_media_volume_action(
                        current, MediaVolumeAction.DECREASE),
                    max(0, current - 2),
                )
            with self.subTest(current=current, action="increase"):
                self.assertEqual(
                    apply_media_volume_action(
                        current, MediaVolumeAction.INCREASE),
                    min(100, current + 2),
                )
            with self.subTest(current=current, action="set_one"):
                self.assertEqual(
                    apply_media_volume_action(
                        current, MediaVolumeAction.SET_ONE),
                    1,
                )

    def test_volume_and_action_types_are_strict(self):
        for volume in (-1, 101, False, 1.0, "50", None):
            with self.subTest(volume=volume), self.assertRaises(ProtocolError):
                apply_media_volume_action(volume, MediaVolumeAction.INCREASE)
        for action in ("increase", 1, True, None, object()):
            with self.subTest(action=action), self.assertRaises(ProtocolError):
                apply_media_volume_action(50, action)


if __name__ == "__main__":
    unittest.main()
