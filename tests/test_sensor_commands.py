import unittest

from swarm2.protocol import ProtocolError
from swarm2.sensor_commands import (
    POLLING_RATES, SETTING_RESPONSE_LENGTHS,
    build_angle_report, build_debounce_report, build_eco_report, build_haptic_report,
    build_lift_off_reports, build_polling_report, build_screen_report,
    build_setting_get_buffer, build_setting_read_request, build_standby_report,
    decode_advanced_sensor_response, decode_debounce_response, decode_eco_response,
    decode_haptic_response, decode_screen_response, decode_standby_response,
)


# Native Linux USB capture; no device access occurs in this test module.
SENSOR = bytes.fromhex(
    "10100000020200000004010700ff000001010f0000ff000101170000646401011f000000ff"
    "01013f00e320ca0185014b")
GLOBAL_CAPTURES = {
    0x05: (decode_standby_response, bytes.fromhex("10050003fd")),
    0x1A: (decode_debounce_response, bytes.fromhex("101a000505f6")),
    0x24: (decode_haptic_response, bytes.fromhex("10240002fe")),
    0x26: (decode_eco_response, bytes.fromhex("1026000000")),
    0x2B: (decode_screen_response, bytes.fromhex("102b00640a92")),
}


class SensorCommandTests(unittest.TestCase):
    def assertReport(self, actual, prefix):
        expected = bytes.fromhex(prefix)
        self.assertEqual(actual, expected + bytes(64 - len(expected)))

    def test_sensor_capture_and_separate_connection_rates(self):
        state = decode_advanced_sensor_response(SENSOR, 0)
        self.assertEqual(state.raw, SENSOR)
        self.assertEqual(state.polling_signature, (0, 4000, True))
        self.assertEqual(state.angle_signature, (0, False, 0, False))
        self.assertEqual(state.polling_rate_wireless, 4000)
        self.assertEqual(state.lift_off_raw, 0x85)
        different = bytearray(SENSOR)
        different[5] = 0
        different[7] = 0xE2
        different[8] = 1
        different[45] = 0x92
        state = decode_advanced_sensor_response(different, 0)
        self.assertEqual(state.polling_rate_usb, 4000)
        self.assertEqual(state.polling_rate_wireless, 1000)
        self.assertEqual(state.angle_tuning, -30)
        self.assertEqual(state.lift_off_raw, 0x92)  # Preserve unknown calibration state.

    def test_advanced_response_identity_and_unknown_fields(self):
        for offset, value in ((0, 0), (1, 0x11), (2, 1), (3, 1), (4, 7), (5, 255),
                              (6, 2), (7, 31), (7, 225), (8, 255), (46, 2)):
            with self.subTest(offset=offset, value=value):
                malformed = bytearray(SENSOR)
                malformed[offset] = value
                with self.assertRaises(ProtocolError):
                    decode_advanced_sensor_response(malformed, 0)

    def test_polling_enum_order_and_motion_bit(self):
        for enum, frequency in enumerate(POLLING_RATES):
            for sync in (False, True):
                with self.subTest(frequency=frequency, sync=sync):
                    report = build_polling_report(profile_index=4, polling_rate=frequency,
                                                  motion_sync=sync)
                    self.assertEqual(report[:5], bytes((0x10, 0x10, 4, enum | (sync << 4), 0)))
                    self.assertEqual(report[5:], bytes(59))
        state = decode_advanced_sensor_response(SENSOR, 0)
        self.assertReport(build_polling_report(profile_index=0, polling_rate=1000,
                                              motion_sync=state.motion_sync), "1010001000")

    def test_angle_signed_encoding_and_disable_profile_mask(self):
        self.assertReport(build_angle_report(profile_index=4, angle_snapping=True,
                          angle_tuning=-30, angle_tuning_enabled=False), "1011a4e20100")
        self.assertReport(build_angle_report(profile_index=1, angle_snapping=False,
                          angle_tuning=30, angle_tuning_enabled=True), "1011011e0000")
        state = decode_advanced_sensor_response(SENSOR, 0)
        self.assertReport(build_angle_report(profile_index=state.profile_index,
                          angle_snapping=True, angle_tuning=state.angle_tuning,
                          angle_tuning_enabled=state.angle_tuning_enabled), "1011a0000100")

    def test_global_lift_off_is_two_ordered_reports(self):
        for level, value in (("very_low", "00"), ("low", "85")):
            reset, setting = build_lift_off_reports(level)
            self.assertReport(reset, "10199300")
            self.assertReport(setting, "1019" + value + "00")
        for unsupported in ("high", "custom", 3, None):
            with self.assertRaises(ProtocolError):
                build_lift_off_reports(unsupported)

    def test_global_request_and_get_identity(self):
        for selector in GLOBAL_CAPTURES:
            self.assertReport(build_setting_read_request(selector), f"101c00{selector:02x}000000")
            self.assertReport(build_setting_get_buffer(selector), f"10{selector:02x}")
        for unsupported in (0x10, 0x12, 0x25, 0xFF, True, "5"):
            for builder in (build_setting_read_request, build_setting_get_buffer):
                with self.assertRaises(ProtocolError):
                    builder(unsupported)

    def test_global_capture_decoders_preserve_uninterpreted_trailer(self):
        standby = decode_standby_response(GLOBAL_CAPTURES[0x05][1])
        debounce = decode_debounce_response(GLOBAL_CAPTURES[0x1A][1])
        haptic = decode_haptic_response(GLOBAL_CAPTURES[0x24][1])
        eco = decode_eco_response(GLOBAL_CAPTURES[0x26][1])
        screen = decode_screen_response(GLOBAL_CAPTURES[0x2B][1])
        self.assertEqual(standby.standby_value, 3)
        self.assertEqual(debounce.signature, (5, 5))
        self.assertEqual(haptic.intensity, 2)
        self.assertFalse(eco.enabled)
        self.assertEqual(screen.signature, (100, 10))
        for selector, (decoder, capture) in GLOBAL_CAPTURES.items():
            self.assertEqual(decoder(capture).raw, capture)
            self.assertEqual(len(capture), SETTING_RESPONSE_LENGTHS[selector])

    def test_each_global_decoder_rejects_wrong_selector_status_and_length(self):
        for selector, (decoder, capture) in GLOBAL_CAPTURES.items():
            malformed = [capture[:-1], capture + b"\0", capture + bytes(64 - len(capture))]
            for offset in (0, 1, 2):
                changed = bytearray(capture)
                changed[offset] ^= 1
                malformed.append(bytes(changed))
            for value in malformed:
                with self.subTest(selector=selector, value=value.hex()):
                    with self.assertRaises(ProtocolError):
                        decoder(value)

    def test_global_writes_use_vendor_layout_and_preserve_coupled_fields(self):
        self.assertReport(build_debounce_report(debounce_ms=0), "101a000000")
        self.assertReport(build_debounce_report(debounce_ms=10), "101a0a0a00")
        self.assertReport(build_haptic_report(intensity=3), "10240300")
        self.assertReport(build_standby_report(standby_value=30), "10051e00")
        self.assertReport(build_eco_report(enabled=True), "10260100")
        screen = decode_screen_response(GLOBAL_CAPTURES[0x2B][1])
        self.assertReport(build_screen_report(brightness=60, timeout_value=screen.timeout_value),
                          "102b3c0a00")

    def test_screen_brightness_uses_confirmed_five_step_range(self):
        for value in (20, 40, 60, 80, 100):
            self.assertEqual(build_screen_report(brightness=value, timeout_value=10)[2], value)
        for value in (0, 1, 19, 21, 50, 99, True):
            with self.subTest(value=value), self.assertRaises(ProtocolError):
                build_screen_report(brightness=value, timeout_value=10)

    def test_screen_timeout_uses_display_menu_value_table(self):
        for value in (0, 1, 2, 3, 4, 5, 10, 15, 20, 25, 30):
            self.assertEqual(build_screen_report(brightness=100, timeout_value=value)[3], value)
        for value in (6, 9, 11, 21, 29, True):
            with self.subTest(value=value), self.assertRaises(ProtocolError):
                build_screen_report(brightness=100, timeout_value=value)

    def test_invalid_edits_do_not_truncate_or_coerce_values(self):
        invalid = [
            lambda: build_polling_report(profile_index=5, polling_rate=1000, motion_sync=False),
            lambda: build_polling_report(profile_index=0, polling_rate=3000, motion_sync=False),
            lambda: build_polling_report(profile_index=0, polling_rate=1000, motion_sync=1),
            lambda: build_angle_report(profile_index=True, angle_snapping=False,
                                      angle_tuning=0, angle_tuning_enabled=False),
            lambda: build_angle_report(profile_index=0, angle_snapping=False,
                                      angle_tuning=-31, angle_tuning_enabled=False),
            lambda: build_angle_report(profile_index=0, angle_snapping=0,
                                      angle_tuning=0, angle_tuning_enabled=False),
            lambda: build_debounce_report(debounce_ms=11),
            lambda: build_haptic_report(intensity=-1),
            lambda: build_standby_report(standby_value=0),
            lambda: build_eco_report(enabled="false"),
            lambda: build_screen_report(brightness=101, timeout_value=10),
            lambda: build_screen_report(brightness=60, timeout_value=31),
        ]
        for invoke in invalid:
            with self.assertRaises(ProtocolError):
                invoke()


if __name__ == "__main__":
    unittest.main()
