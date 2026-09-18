import unittest
from dataclasses import replace

from swarm2.lighting_commands import (
    LIGHTING_EFFECT_IDS, brightness_from_percent, build_lighting_get_buffer,
    build_lighting_read_request, build_lighting_report, decode_lighting_response,
    expected_lighting_state,
)
from swarm2.protocol import ProtocolError


LIVE_LIGHTING = bytes.fromhex("10 2a 00 00 0f ff 05 05 00 00 00 e8")
FW509_LIGHTING = bytes.fromhex("10 2a 00 00 01 0f ff 05 05 00 00 00 e7")


def response(*, profile=0, flags=0x0F, brightness=255, effect=5, speed=5, color=(0, 0, 0)):
    raw = bytearray((0x10, 0x2A, 0, profile, flags, brightness, effect, speed, *color, 0))
    raw[-1] = (-sum(raw[2:-1])) & 255
    return bytes(raw)


def extended_response(*, profile=0, enabled=1, led_timeout=0x0F, brightness=255,
                      effect=5, speed=5, color=(0, 0, 0)):
    raw = bytearray((0x10, 0x2A, 0, profile, enabled, led_timeout,
                     brightness, effect, speed, *color, 0))
    raw[-1] = (-sum(raw[2:-1])) & 255
    return bytes(raw)


class LightingCommandsTests(unittest.TestCase):
    def test_live_compact_response_decodes_without_using_checksum_as_blue(self):
        state = decode_lighting_response(LIVE_LIGHTING, 0)
        self.assertEqual(state.raw, LIVE_LIGHTING)
        self.assertIsNone(state.enabled_raw)
        self.assertEqual(state.led_timeout_raw, 0x0F)
        self.assertEqual(state.brightness_raw, 255)
        self.assertEqual(state.brightness_percent, 100)
        self.assertEqual(state.effect, "aimo")
        self.assertEqual(state.speed, 5)
        self.assertEqual(state.color, (0, 0, 0))
        self.assertEqual(decode_lighting_response(LIVE_LIGHTING + bytes(52), 0), state)

    def test_firmware_509_extended_response_decodes_all_shifted_fields(self):
        state = decode_lighting_response(FW509_LIGHTING, 0)
        self.assertEqual(state.raw, FW509_LIGHTING)
        self.assertTrue(state.extended_layout)
        self.assertEqual(state.enabled_raw, 1)
        self.assertEqual(state.led_timeout_raw, 0x0F)
        self.assertEqual(state.brightness_raw, 255)
        self.assertEqual(state.effect, "aimo")
        self.assertEqual(state.speed, 5)
        self.assertEqual(state.color, (0, 0, 0))
        self.assertEqual(decode_lighting_response(FW509_LIGHTING + bytes(51), 0), state)
        with self.assertRaisesRegex(ProtocolError, "enable"):
            decode_lighting_response(extended_response(enabled=2), 0)
        disabled = decode_lighting_response(extended_response(enabled=0, effect=5), 0)
        self.assertEqual(disabled.effect, "off")
        self.assertEqual(disabled.effect_id, 5)

    def test_padded_zero_checksum_layout_is_rejected_when_ambiguous(self):
        extended = bytes.fromhex("10 2a 00 00 01 0f e6 05 05 00 00 00 00")
        self.assertTrue(decode_lighting_response(extended, 0).extended_layout)
        with self.assertRaisesRegex(ProtocolError, "ambiguous"):
            decode_lighting_response(extended + bytes(51), 0)

        compact = response(flags=1)
        self.assertFalse(decode_lighting_response(compact, 0).extended_layout)
        with self.assertRaisesRegex(ProtocolError, "ambiguous"):
            decode_lighting_response(compact + bytes(52), 0)

    def test_read_selector_and_get_initialization_are_different_messages(self):
        self.assertEqual(build_lighting_read_request(4), bytes.fromhex("10 1c 00 2a 04 00 00") + bytes(57))
        self.assertEqual(build_lighting_get_buffer(), bytes.fromhex("10 2a") + bytes(62))
        for invalid in (-1, 5, True, 1.0, "0"):
            with self.subTest(invalid=invalid), self.assertRaises(ProtocolError):
                build_lighting_read_request(invalid)

    def test_response_checks_header_status_profile_checksum_and_layout(self):
        malformed = [LIVE_LIGHTING[:-1], LIVE_LIGHTING + b"\0\0", LIVE_LIGHTING + bytes(51),
                     LIVE_LIGHTING + bytes(51) + b"\1", list(LIVE_LIGHTING), LIVE_LIGHTING.hex()]
        for index in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11):
            bad = bytearray(LIVE_LIGHTING)
            bad[index] ^= 1
            malformed.append(bytes(bad))
        for raw in malformed:
            with self.subTest(raw=raw), self.assertRaises(ProtocolError):
                decode_lighting_response(raw, 0)
        with self.assertRaises(ProtocolError):
            decode_lighting_response(LIVE_LIGHTING, 1)

    def test_unknown_effect_and_speed_are_not_replaced_with_defaults(self):
        state = decode_lighting_response(response(effect=0xF1, speed=0xFE), 0)
        self.assertIsNone(state.effect)
        self.assertEqual(state.effect_id, 0xF1)
        self.assertEqual(state.speed, 0xFE)

    def test_effect_ids_match_mc7_runtime_catalog_and_conversion_functions(self):
        self.assertEqual(dict(LIGHTING_EFFECT_IDS), {
            "off": 0, "static": 1, "blink": 2, "breathing": 3,
            "heartbeat": 4, "aimo": 5, "wave": 6,
        })

    def test_percent_editor_preserves_every_unchanged_raw_brightness(self):
        for raw in range(256):
            state = decode_lighting_response(response(brightness=raw), 0)
            self.assertEqual(brightness_from_percent(state.brightness_percent, baseline=state), raw)
        self.assertEqual(brightness_from_percent(0), 0)
        self.assertEqual(brightness_from_percent(100), 255)
        self.assertEqual(brightness_from_percent(50), 128)
        for invalid in (-1, 101, False, 1.0, "50"):
            with self.subTest(invalid=invalid), self.assertRaises(ProtocolError):
                brightness_from_percent(invalid)

    def test_write_matches_hardware_verified_brightness_change_and_restore(self):
        before = decode_lighting_response(LIVE_LIGHTING, 0)
        report = build_lighting_report(before, brightness_raw=254)
        self.assertEqual(report, bytes.fromhex("10 2a 00 01 0f fe 05 05 00 00 00") + bytes(53))
        after = expected_lighting_state(report, before)
        self.assertEqual(after.raw.hex(), "102a00000ffe0505000000e9")
        restored = expected_lighting_state(build_lighting_report(after, brightness_raw=255), after)
        self.assertEqual(restored, before)

    def test_write_preserves_flags_profile_and_all_unspecified_fields(self):
        before = decode_lighting_response(response(profile=4, flags=0xA7, color=(17, 39, 241)), 4)
        report = build_lighting_report(before, speed=10)
        after = expected_lighting_state(report, before)
        self.assertEqual(after.profile_index, 4)
        self.assertEqual(after.led_timeout_raw, 0xA7)
        self.assertEqual(after.color, before.color)
        self.assertEqual(after.brightness_raw, before.brightness_raw)
        self.assertEqual(after.effect_id, before.effect_id)
        self.assertEqual(after.speed, 10)
        self.assertEqual(report[11:], bytes(53))

    def test_write_color_channel_order_and_effect_catalog(self):
        before = decode_lighting_response(LIVE_LIGHTING, 0)
        for effect, effect_id in LIGHTING_EFFECT_IDS.items():
            report = build_lighting_report(before, effect=effect, color=(0x12, 0x34, 0x56))
            self.assertEqual(report[6], effect_id)
            self.assertEqual(report[8:11], bytes.fromhex("12 34 56"))

    def test_write_rejects_invalid_controls_and_forged_baseline(self):
        before = decode_lighting_response(LIVE_LIGHTING, 0)
        cases = [
            {"brightness_raw": -1}, {"brightness_raw": 256}, {"brightness_raw": True},
            {"speed": 0}, {"speed": 11}, {"speed": 1.0}, {"speed": False},
            {"led_timeout_raw": -1}, {"led_timeout_raw": 31}, {"led_timeout_raw": False},
            {"effect": "rainbow"}, {"effect": 1}, {"effect": []},
            {"color": (0, 1)}, {"color": (0, 1, 2, 3)}, {"color": (0, 0, 256)},
            {"color": (0, -1, 0)}, {"color": (False, 0, 0)}, {"color": "#123456"},
        ]
        for options in cases:
            with self.subTest(options=options), self.assertRaises(ProtocolError):
                build_lighting_report(before, **options)
        with self.assertRaises(ProtocolError):
            build_lighting_report(replace(before, led_timeout_raw=0))
        with self.assertRaises(ProtocolError):
            build_lighting_report(None)
        for enabled in (False, True, 0, 1, "yes"):
            with self.subTest(enabled=enabled), self.assertRaises(ProtocolError):
                build_lighting_report(before, enabled=enabled)

    def test_unedited_unknown_fields_round_trip_without_fabricated_defaults(self):
        before = decode_lighting_response(response(effect=0xF0, speed=0), 0)
        self.assertEqual(expected_lighting_state(build_lighting_report(before), before), before)

    def test_firmware_509_write_matches_vendor_13_byte_layout_and_prediction(self):
        before = decode_lighting_response(FW509_LIGHTING, 0)
        report = build_lighting_report(before, brightness_raw=254, led_timeout_raw=14, enabled=False)
        self.assertEqual(report[:13], bytes.fromhex("10 2a 00 01 00 0e fe 05 05 00 00 00 00"))
        self.assertEqual(report[13:], bytes(51))
        after = expected_lighting_state(report, before)
        self.assertEqual(after.raw.hex(), "102a0000000efe0505000000ea")
        self.assertEqual(after.effect, "off")

    def test_prediction_requires_a_valid_layout_baseline(self):
        compact = decode_lighting_response(LIVE_LIGHTING, 0)
        with self.assertRaises(ProtocolError):
            expected_lighting_state(build_lighting_report(compact), None)


if __name__ == "__main__":
    unittest.main()
