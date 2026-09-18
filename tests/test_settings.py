"""Full service/helper setting transactions with captured USB read fixtures.

The fixtures were captured on 2026-09-15. They are embedded so tests never
depend on private research artifacts or access a physical mouse.
"""

import copy
import unittest
from unittest.mock import patch

from swarm2.configuration import Action
from swarm2.hardware import transact
from swarm2.service import DeviceService
from swarm2.settings import REQUIRED, SELECTORS
from swarm2.transport import DeviceError


def screen_key_page(profile, page, records=bytes(44)):
    raw = bytearray(bytes((0x10, 0x29, 0, 0x29, 0x32, 0, profile, page + 1))
                    + b"\0" + records + b"\0")
    raw[-1] = (-sum(raw[2:-1])) & 255
    return bytes(raw)


def screen_key_capture(profile=0):
    return b"".join(screen_key_page(profile, page) for page in range(3))


EMPTY_SCREEN_KEY = bytes(11)
REMAP_A = bytes.fromhex("0000040c00410000000000")
HOTKEY_CTRL_S = bytes.fromhex("0016010600530000000000")


def with_screen_key_records(raw, page, logical_records):
    """Replace one page using the editor's left-to-right logical slot order."""
    if len(logical_records) != 4 or any(len(record) != 11 for record in logical_records):
        raise AssertionError("A screen-key page needs four eleven-byte records")
    result = bytearray(raw)
    start = page * 54
    response = bytearray(result[start:start + 54])
    # Command 0x29 stores records right-to-left on the wire.
    response[9:53] = b"".join(reversed(logical_records))
    response[-1] = (-sum(response[2:-1])) & 255
    result[start:start + 54] = response
    return bytes(result)


def with_lcd_page(raw, page, logical_signatures):
    """Replace one LCD page using four left-to-right two-byte signatures."""
    if len(logical_signatures) != 4 or any(len(value) != 2 for value in logical_signatures):
        raise AssertionError("An LCD page needs four two-byte signatures")
    result = bytearray(raw)
    offset = 5 + page * 11
    result[offset + 3:offset + 11] = b"".join(reversed(logical_signatures))
    return with_checksum(result)


CAPTURES = {
    "background": "102c000000000000",
    "status": "1009000405204019ff63011b",
    # mc7-live-sensor-profile1.json; first 48 bytes returned by GET_FEATURE.
    "sensor": "10100000020200000004010700ff000001010f0000ff000101170000646401011f000000ff01013f00e320ca0185014b",
    # live-settings-reads.json.
    "profile": "1012000005fb",
    "lighting": "102a00000fff0505000000e8",
    "primary": "101500000000010100000201000003010000090100000a010000050100000601000001020000010a0000060300000801000007010000000000000000000000ad",
    "easy_shift": "1016000000000101000002010000040300000703000008030000060400000504000001080000010a0000000000000303000002030000000000000000000000ad",
    "haptic": "10240002fe",
    "screen": "102b00640a92",
    # advanced-write-restore.json, original global settings.
    "standby": "10050003fd",
    "debounce": "101a000505f6",
    "eco": "1026000000",
    "screen_keys": screen_key_capture().hex(),
    "lcd": ("1025000003"
            "01000064ff010000ff00ff"
            "02000065ff15ff00ff00ff"
            "0300001fff1aff19ff47ff"
            "0400000000000000000000"
            "0500000000000000000000"
            "81"),
}


def with_checksum(raw):
    result = bytearray(raw)
    result[-1] = (-sum(result[2:-1])) & 255
    return bytes(result)


class SettingsTransport:
    """A small wire-level fixture, independent of the production planners."""

    def __init__(self):
        self.records = {name: bytes.fromhex(raw) for name, raw in CAPTURES.items()}
        self.selected = None
        self.selected_page = 0
        self.sent = []
        self.writes = []
        self.acknowledgements = []
        self.read_errors = {}
        self.fail_write_number = None
        self.fail_reads_after_write = set()
        self.ignore_writes = False
        self.location_id = "fixture-usb-port"
        self.closed = False

    def __enter__(self):
        self.closed = False
        return self

    def __exit__(self, *exc):
        self.closed = True

    def send(self, packet):
        if not isinstance(packet, bytes) or len(packet) != 64:
            raise AssertionError("Every transaction must use a complete HID feature report")
        self.sent.append(packet)
        if packet[1] == 0x1C:
            self.selected = packet[3]
            if self.selected == 0x29:
                self.selected_page = packet[5] - 1
            return
        self.writes.append(packet)
        if len(self.writes) == self.fail_write_number:
            raise DeviceError("Simulated disconnect before acknowledgement")
        if self.ignore_writes:
            return
        command = packet[1]
        if command in (0x15, 0x16):
            name = "primary" if command == 0x15 else "easy_shift"
            raw = bytearray(self.records[name])
            raw[3] = packet[2]
            raw[4:52] = packet[3:51]
        elif command == 0x2A:
            name = "lighting"
            raw = bytearray(self.records[name])
            raw[3] = packet[2]
            raw[4:-1] = packet[4:len(raw)-1]
        elif command == 0x2B:
            name = "screen"
            raw = bytearray(self.records[name])
            raw[3:5] = packet[2:4]
        elif command == 0x2C:
            raw = bytearray(self.records["background"])
            raw[3] = packet[2]
            self.records["background"] = bytes(raw)
            return
        elif command == 0x25:
            name = "lcd"
            raw = bytearray(packet[:61])
            raw[2] = 0
        elif command == 0x29:
            name = "screen_keys"
            start = (packet[5] - 1) * 54
            response = bytearray(self.records[name][start:start + 54])
            response[9:53] = packet[7:51]
            response[-1] = (-sum(response[2:-1])) & 255
            self.records[name] = (self.records[name][:start] + bytes(response)
                                  + self.records[name][start + 54:])
            return
        elif command in (0x24, 0x05, 0x26):
            name = {0x24: "haptic", 0x05: "standby", 0x26: "eco"}[command]
            raw = bytearray(self.records[name])
            raw[3] = packet[2]
        elif command == 0x1A:
            name = "debounce"
            raw = bytearray(self.records[name])
            raw[3:5] = packet[2:4]
        elif command == 0x12:
            name = "profile"
            raw = bytearray(self.records[name])
            raw[3] = packet[2] & 0x7F
            raw[4] = packet[3]
        else:
            raise AssertionError(f"This test has not modeled command {command:#x}")
        self.records[name] = with_checksum(raw)

    def get_feature(self, selector):
        if self.selected != selector:
            raise AssertionError("GET must follow a matching acknowledged selector")
        name = next(name for name, value in SELECTORS.items() if value == selector)
        if name in self.read_errors:
            raise DeviceError(self.read_errors[name])
        if self.writes and name in self.fail_reads_after_write:
            raise DeviceError("Simulated readback disconnect")
        if name == "screen_keys":
            start = self.selected_page * 54
            return self.records[name][start:start + 54]
        return self.records[name]


class SettingsIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.transport = SettingsTransport()
        self.service = DeviceService()
        self.helper_requests = []

        def helper(request):
            self.helper_requests.append(copy.deepcopy(request))
            return transact(request, lambda _: self.transport)

        self.helper_patch = patch.object(DeviceService, "_call", side_effect=helper)
        self.helper_patch.start()
        self.addCleanup(self.helper_patch.stop)

    def read(self):
        return self.service.read("fixture-mc7", 1)

    def test_full_read_maps_actual_records_into_verified_snapshot(self):
        snapshot = self.read()
        config = snapshot["configuration"]
        config.validate()
        self.assertEqual(snapshot["errors"], {})
        self.assertEqual(snapshot["baseline"]["settings"], CAPTURES)
        self.assertEqual(snapshot["baseline"]["transport_identity"], "fixture-usb-port")
        self.assertEqual(snapshot["summary"]["dpi"], 3200)
        self.assertEqual(config.sensor.polling_rate, 4000)
        self.assertTrue(config.sensor.motion_sync)
        self.assertEqual(config.sensor.debounce_ms, 5)
        self.assertEqual(config.lighting.effect, "aimo")
        self.assertEqual(config.lighting.color, "#000000")
        self.assertEqual(config.lighting.brightness, 100)
        self.assertEqual(config.lighting.speed, 50)
        self.assertEqual(config.power.led_timeout_value, 15)
        self.assertEqual(config.display.haptic_intensity, "medium")
        self.assertEqual(config.display.timeout_value, 10)
        self.assertEqual(config.display.pages, [
            ["download_swarm", None, None, "dpi"],
            ["game_bar", "system_media", None, None],
            ["cut", "copy", "paste", "undo"],
        ])
        self.assertIn("display.pages", snapshot["verified_fields"])
        self.assertEqual(config.display.key_bindings, [[None] * 4 for _ in range(3)])
        self.assertIn("display.key_bindings", snapshot["verified_fields"])
        self.assertEqual(config.power.standby_value, 3)
        self.assertFalse(config.power.eco_mode)
        self.assertEqual(config.buttons[0].primary, Action("mouse", "left"))
        self.assertIn("buttons", snapshot["verified_fields"])
        self.assertIn("lighting.brightness", snapshot["verified_fields"])
        self.assertIn("power.led_timeout_value", snapshot["verified_fields"])
        self.assertNotIn("display.widgets", snapshot["verified_fields"])
        self.assertNotIn("macros", snapshot["verified_fields"])
        self.assertEqual(self.transport.writes, [])
        self.assertTrue(self.transport.closed)

    def test_full_read_maps_screen_key_wire_records_to_logical_lcd_slots(self):
        self.transport.records["lcd"] = with_lcd_page(
            self.transport.records["lcd"], 2,
            (b"\x05\x00", b"\x05\x01", b"\x1a\xff", b"\x1f\xff"))
        self.transport.records["screen_keys"] = with_screen_key_records(
            self.transport.records["screen_keys"], 2,
            (REMAP_A, HOTKEY_CTRL_S, EMPTY_SCREEN_KEY, EMPTY_SCREEN_KEY))

        snapshot = self.read()

        self.assertEqual(snapshot["configuration"].display.pages[2],
                         ["remap_key", "hotkey", "paste", "undo"])
        self.assertEqual(snapshot["configuration"].display.key_bindings[2],
                         ["A", "Ctrl+S", None, None])
        self.assertIn("display.key_bindings", snapshot["verified_fields"])
        self.assertEqual(self.transport.writes, [])

    def test_active_profile_read_is_global_bounded_and_write_free(self):
        result = self.service.read_active_profile("fixture-mc7")
        self.assertEqual(result, {
            "active_profile": 1,
            "profile_count": 5,
            "energy_saving": False,
            "raw": CAPTURES["profile"],
            "transport_identity": "fixture-usb-port",
            "changed": False,
            "acknowledgements": [],
        })
        self.assertEqual(self.helper_requests[-1], {
            "operation": "read_active_profile", "device_id": "fixture-mc7",
            "profile_slot": 1,
        })
        self.assertEqual(self.transport.writes, [])
        self.assertEqual([packet[3] for packet in self.transport.sent], [0x12])

    def test_switch_to_reported_profile_sends_no_profile_write(self):
        result = self.service.switch_profile("fixture-mc7", 1)
        self.assertFalse(result["summary"]["changed"])
        self.assertTrue(result["summary"]["activation_acknowledged"])
        self.assertEqual(self.transport.writes, [])

    def test_unchanged_apply_of_every_section_sends_no_configuration_report(self):
        snapshot = self.read()
        for section in REQUIRED:
            with self.subTest(section=section):
                result = self.service.apply_section("fixture-mc7", snapshot["configuration"], section, snapshot["baseline"])
                self.assertFalse(result["summary"]["changed"])
                self.assertEqual(result["baseline"]["settings"], CAPTURES)
                self.assertEqual(self.transport.writes, [])

    def test_stale_baseline_for_every_section_blocks_all_writes(self):
        mutations = {"sensor": ("sensor", 9, 3), "buttons": ("primary", 18, 8),
                     "lighting": ("lighting", 5, 254), "display": ("screen", 3, 99),
                     "power": ("standby", 3, 4)}
        for section, (name, offset, value) in mutations.items():
            with self.subTest(section=section):
                self.transport = SettingsTransport()
                snapshot = self.read()
                raw = bytearray(self.transport.records[name])
                raw[offset] = value
                self.transport.records[name] = with_checksum(raw)
                with self.assertRaisesRegex(DeviceError, "changed since the last read"):
                    self.service.apply_section("fixture-mc7", snapshot["configuration"], section, snapshot["baseline"])
                self.assertEqual(self.transport.writes, [])

    def test_unrelated_stale_setting_does_not_block_or_get_overwritten(self):
        snapshot = self.read()
        raw = bytearray(self.transport.records["sensor"])
        raw[9] = 3
        self.transport.records["sensor"] = with_checksum(raw)
        expected_sensor = self.transport.records["sensor"]
        config = snapshot["configuration"]
        config.lighting.brightness = 50
        result = self.service.apply_section("fixture-mc7", config, "lighting", snapshot["baseline"])
        self.assertTrue(result["summary"]["changed"])
        self.assertEqual([packet[1] for packet in self.transport.writes], [0x2A])
        self.assertEqual(self.transport.records["sensor"], expected_sensor)

    def test_firmware_509_extended_lighting_toggle_and_timeout_are_verified(self):
        self.transport.records["lighting"] = bytes.fromhex("102a0000010fff0505000000e7")
        snapshot = self.read()
        self.assertEqual(snapshot["configuration"].lighting.effect, "aimo")
        self.assertEqual(snapshot["configuration"].power.led_timeout_value, 15)

        config = snapshot["configuration"]
        config.lighting.effect = "off"
        result = self.service.apply_section("fixture-mc7", config, "lighting", snapshot["baseline"])
        self.assertEqual(self.transport.writes[-1][:13].hex(), "102a0001000fff050500000000")
        self.assertEqual(result["configuration"].lighting.effect, "off")
        self.assertEqual(result["baseline"]["settings"]["lighting"], "102a0000000fff0505000000e8")

        config = result["configuration"]
        config.power.led_timeout_value = 14
        restored = self.service.apply_section("fixture-mc7", config, "power", result["baseline"])
        self.assertEqual(self.transport.writes[-1][:13].hex(), "102a0001000eff050500000000")
        self.assertEqual(restored["configuration"].power.led_timeout_value, 14)
        self.assertEqual(restored["baseline"]["settings"]["lighting"], "102a0000000eff0505000000e9")

    def test_firmware_509_enabled_effect_zero_unchanged_is_a_noop(self):
        self.transport.records["lighting"] = with_checksum(
            bytes.fromhex("102a0000010fff000500000000"))
        snapshot = self.read()
        self.assertEqual(snapshot["configuration"].lighting.effect, "off")

        result = self.service.apply_section(
            "fixture-mc7", snapshot["configuration"], "lighting", snapshot["baseline"])
        self.assertFalse(result["summary"]["changed"])
        self.assertEqual(self.transport.writes, [])

    def test_unsupported_led_timeout_is_preserved_during_other_power_edit(self):
        original = with_checksum(bytes.fromhex("102a0000011fff050500000000"))
        self.transport.records["lighting"] = original
        snapshot = self.read()
        self.assertNotIn("power.led_timeout_value", snapshot["verified_fields"])
        snapshot["configuration"].power.standby_value = 4

        result = self.service.apply_section(
            "fixture-mc7", snapshot["configuration"], "power", snapshot["baseline"])
        self.assertTrue(result["summary"]["changed"])
        self.assertEqual([packet[1] for packet in self.transport.writes], [0x05])
        self.assertEqual(self.transport.records["lighting"], original)

    def test_button_write_changes_only_requested_record_and_preserves_hidden_slot(self):
        snapshot = self.read()
        before = self.transport.records["primary"]
        config = snapshot["configuration"]
        config.buttons[3].primary = Action("media", "mute")
        result = self.service.apply_section("fixture-mc7", config, "buttons", snapshot["baseline"])
        self.assertTrue(result["summary"]["changed"])
        self.assertEqual(len(self.transport.writes), 1)
        packet = self.transport.writes[0]
        self.assertEqual(packet[:3], bytes.fromhex("10 15 00"))
        self.assertEqual(packet[15:19], bytes.fromhex("00 00 06 03"))
        for slot in range(12):
            if slot != 3:
                self.assertEqual(packet[3 + slot * 4:7 + slot * 4], before[4 + slot * 4:8 + slot * 4])
        self.assertEqual(packet[51:], bytes(13))
        self.assertEqual(self.transport.records["easy_shift"].hex(), CAPTURES["easy_shift"])
        self.assertEqual(result["configuration"].buttons[3].primary, Action("media", "mute"))

    def test_essential_click_guard_validates_before_any_write(self):
        snapshot = self.read()
        config = snapshot["configuration"]
        config.buttons[0].primary = Action("disabled", "")
        with self.assertRaisesRegex(ValueError, "left and right click"):
            self.service.apply_section("fixture-mc7", config, "buttons", snapshot["baseline"])
        self.assertEqual(self.transport.writes, [])

    def test_unavailable_optional_record_is_reported_and_blocks_only_affected_section(self):
        self.transport.read_errors["eco"] = "Fixture ECO read failed"
        snapshot = self.read()
        self.assertEqual(snapshot["errors"], {"eco": "Fixture ECO read failed"})
        self.assertNotIn("power.eco_mode", snapshot["verified_fields"])
        self.assertNotIn("eco", snapshot["baseline"]["settings"])
        with self.assertRaisesRegex(DeviceError, "eco settings successfully"):
            self.service.apply_section("fixture-mc7", snapshot["configuration"], "power", snapshot["baseline"])
        self.assertEqual(self.transport.writes, [])
        result = self.service.apply_section("fixture-mc7", snapshot["configuration"], "lighting", snapshot["baseline"])
        self.assertFalse(result["summary"]["changed"])

    def test_required_sensor_read_failure_does_not_create_a_snapshot(self):
        self.transport.read_errors["sensor"] = "Fixture sensor read failed"
        with self.assertRaisesRegex(DeviceError, "Fixture sensor read failed"):
            self.read()
        self.assertEqual(self.transport.writes, [])

    def test_partial_write_failure_reports_uncertainty_and_stops(self):
        snapshot = self.read()
        config = snapshot["configuration"]
        config.display.brightness = 80
        config.display.haptic_intensity = "low"
        self.transport.fail_write_number = 2
        with self.assertRaisesRegex(DeviceError, "Some settings may have changed"):
            self.service.apply_section("fixture-mc7", config, "display", snapshot["baseline"])
        self.assertEqual([packet[1] for packet in self.transport.writes], [0x2B, 0x24])
        self.assertEqual(self.transport.records["screen"][3], 80)
        self.assertEqual(self.transport.records["haptic"][3], 2)
        self.assertTrue(self.transport.closed)

    def test_missing_or_mismatched_readback_never_reports_apply_success(self):
        for ignore_write in (False, True):
            with self.subTest(ignore_write=ignore_write):
                self.transport = SettingsTransport()
                snapshot = self.read()
                snapshot["configuration"].lighting.brightness = 50
                if ignore_write:
                    self.transport.ignore_writes = True
                else:
                    self.transport.fail_reads_after_write.add("lighting")
                with self.assertRaisesRegex(DeviceError, "Some settings may have changed"):
                    self.service.apply_section("fixture-mc7", snapshot["configuration"], "lighting", snapshot["baseline"])
                self.assertEqual(len(self.transport.writes), 1)

    def test_wrong_device_profile_or_usb_location_never_writes(self):
        for field, value in (("device_id", "other"), ("profile_slot", 2), ("transport_identity", "different-port")):
            with self.subTest(field=field):
                snapshot = self.read()
                snapshot["baseline"][field] = value
                with self.assertRaisesRegex(DeviceError, "Read this mouse/profile"):
                    self.service.apply_section("fixture-mc7", snapshot["configuration"], "lighting", snapshot["baseline"])
                self.assertEqual(self.transport.writes, [])

    def test_lcd_setup_removes_prompt_preserves_other_pages_and_is_idempotent(self):
        before = self.read()
        result = self.service.setup_display("fixture-mc7", 1)
        self.assertTrue(result["summary"]["lcd_setup_verified"])
        self.assertTrue(result["summary"]["changed"])
        self.assertNotIn("download_swarm", result["configuration"].display.pages[0])
        self.assertEqual(result["configuration"].display.pages[0][:3], ["next_track", "led_brightness", "play_pause"])
        self.assertEqual(result["configuration"].display.pages[0][3], "dpi")
        self.assertEqual(result["configuration"].display.pages[1:], before["configuration"].display.pages[1:])
        self.assertEqual([packet[1] for packet in self.transport.writes], [0x25])
        self.assertEqual(self.transport.records["lcd"][38:60], bytes.fromhex(CAPTURES["lcd"])[38:60])
        again = self.service.setup_display("fixture-mc7", 1)
        self.assertTrue(again["summary"]["lcd_setup_verified"])
        self.assertFalse(again["summary"]["changed"])
        self.assertEqual(len(self.transport.writes), 1)

    def test_lcd_setup_clears_hidden_prompt_keys_before_replacing_prompt(self):
        self.transport.records["screen_keys"] = with_screen_key_records(
            self.transport.records["screen_keys"], 0,
            (REMAP_A, HOTKEY_CTRL_S, REMAP_A, EMPTY_SCREEN_KEY))

        result = self.service.setup_display("fixture-mc7", 1)

        self.assertTrue(result["summary"]["lcd_setup_verified"])
        self.assertEqual([packet[1] for packet in self.transport.writes], [0x29, 0x25])
        self.assertEqual(self.transport.writes[0][:7],
                         bytes.fromhex("10 29 32 00 00 01 00"))
        self.assertEqual(self.transport.writes[0][7:51], EMPTY_SCREEN_KEY * 4)
        self.assertEqual(result["configuration"].display.key_bindings[0], [None] * 4)

    def test_lcd_setup_repairs_non_rendering_polling_tile_without_replacing_neighbors(self):
        snapshot = self.read()
        snapshot["configuration"].display.pages[0] = ["polling_rate", "led_brightness", "play_pause", "dpi"]
        self.service.apply_section("fixture-mc7", snapshot["configuration"], "display", snapshot["baseline"])
        result = self.service.setup_display("fixture-mc7", 1)
        self.assertTrue(result["summary"]["changed"])
        self.assertEqual(result["configuration"].display.pages[0], ["next_track", "led_brightness", "play_pause", "dpi"])
        self.assertEqual(result["configuration"].display.pages[1:], snapshot["configuration"].display.pages[1:])

    def test_display_page_apply_changes_one_slot_and_preserves_other_settings(self):
        snapshot = self.read()
        config = snapshot["configuration"]
        config.display.pages[2][1] = "play_pause"
        result = self.service.apply_section("fixture-mc7", config, "display", snapshot["baseline"])
        self.assertEqual(result["configuration"].display.pages, config.display.pages)
        self.assertEqual([packet[1] for packet in self.transport.writes], [0x25])
        for name in CAPTURES.keys() - {"lcd"}:
            self.assertEqual(self.transport.records[name].hex(), CAPTURES[name])
        self.assertEqual(self.transport.records["lcd"][38:60], bytes.fromhex(CAPTURES["lcd"])[38:60])

    def test_general_media_layout_uses_static_app_1024_record_and_readback(self):
        snapshot = self.read()
        config = snapshot["configuration"]
        config.display.pages[0] = ["general_media", None, None, "dpi"]

        result = self.service.apply_section(
            "fixture-mc7", config, "display", snapshot["baseline"])

        self.assertEqual([packet[1] for packet in self.transport.writes], [0x25])
        self.assertEqual(
            self.transport.writes[0][5:16].hex(),
            "0100006400000000004500",
        )
        self.assertEqual(
            result["configuration"].display.pages[0],
            ["general_media", None, None, "dpi"],
        )
        self.assertEqual(
            self.transport.records["screen_keys"].hex(),
            CAPTURES["screen_keys"],
        )

    def test_launch_obs_layout_uses_only_static_command_25_record(self):
        snapshot = self.read()
        config = snapshot["configuration"]
        config.display.pages[0] = [
            "launch_obs", "empty", "empty", "empty"]

        result = self.service.apply_section(
            "fixture-mc7", config, "display", snapshot["baseline"])

        self.assertEqual([packet[1] for packet in self.transport.writes], [0x25])
        self.assertEqual(
            self.transport.writes[0][5:16].hex(),
            "010000fe00fe00fe004b00",
        )
        self.assertEqual(
            result["configuration"].display.pages[0],
            ["launch_obs", "empty", "empty", "empty"],
        )
        self.assertEqual(
            self.transport.records["screen_keys"].hex(),
            CAPTURES["screen_keys"],
        )

    def test_obs_studio_mode_layout_uses_only_static_command_25_record(self):
        snapshot = self.read()
        config = snapshot["configuration"]
        config.display.pages[0] = [
            "obs_studio_mode", "empty", "empty", "empty"]

        result = self.service.apply_section(
            "fixture-mc7", config, "display", snapshot["baseline"])

        self.assertEqual([packet[1] for packet in self.transport.writes], [0x25])
        self.assertEqual(
            self.transport.writes[0][5:16].hex(),
            "010000fe00fe00fe004309",
        )
        self.assertEqual(
            result["configuration"].display.pages[0],
            ["obs_studio_mode", "empty", "empty", "empty"],
        )
        self.assertEqual(
            self.transport.records["screen_keys"].hex(),
            CAPTURES["screen_keys"],
        )

    def test_display_apply_writes_screen_keys_before_layout_and_verifies_both(self):
        snapshot = self.read()
        config = snapshot["configuration"]
        config.display.pages[2][:2] = ["remap_key", "hotkey"]
        config.display.key_bindings[2][:2] = ["A", "Ctrl+S"]
        original_keys = self.transport.records["screen_keys"]

        result = self.service.apply_section(
            "fixture-mc7", config, "display", snapshot["baseline"])

        self.assertTrue(result["summary"]["changed"])
        self.assertEqual([packet[1] for packet in self.transport.writes], [0x29, 0x25])
        key_write = self.transport.writes[0]
        self.assertEqual(key_write[:7], bytes.fromhex("10 29 32 00 00 03 00"))
        self.assertEqual(key_write[7:51],
                         EMPTY_SCREEN_KEY * 2 + HOTKEY_CTRL_S + REMAP_A)
        self.assertEqual(key_write[51:], bytes(13))
        self.assertEqual(self.transport.records["screen_keys"][:108], original_keys[:108])
        self.assertEqual(result["configuration"].display.pages[2],
                         ["remap_key", "hotkey", "paste", "undo"])
        self.assertEqual(result["configuration"].display.key_bindings[2],
                         ["A", "Ctrl+S", None, None])
        for name in CAPTURES.keys() - {"lcd", "screen_keys"}:
            self.assertEqual(self.transport.records[name].hex(), CAPTURES[name])

    def test_clearing_one_screen_key_preserves_its_neighbor_without_layout_write(self):
        self.transport.records["lcd"] = with_lcd_page(
            self.transport.records["lcd"], 2,
            (b"\x05\x00", b"\x05\x01", b"\x1a\xff", b"\x1f\xff"))
        self.transport.records["screen_keys"] = with_screen_key_records(
            self.transport.records["screen_keys"], 2,
            (REMAP_A, HOTKEY_CTRL_S, EMPTY_SCREEN_KEY, EMPTY_SCREEN_KEY))
        original_lcd = self.transport.records["lcd"]
        snapshot = self.read()
        snapshot["configuration"].display.key_bindings[2][0] = None

        result = self.service.apply_section(
            "fixture-mc7", snapshot["configuration"], "display", snapshot["baseline"])

        self.assertEqual([packet[1] for packet in self.transport.writes], [0x29])
        self.assertEqual(self.transport.writes[0][7:51],
                         EMPTY_SCREEN_KEY * 2 + HOTKEY_CTRL_S + EMPTY_SCREEN_KEY)
        self.assertEqual(result["configuration"].display.key_bindings[2],
                         [None, "Ctrl+S", None, None])
        self.assertEqual(self.transport.records["lcd"], original_lcd)

    def test_hidden_screen_key_record_survives_unrelated_lcd_tile_edit(self):
        self.transport.records["screen_keys"] = with_screen_key_records(
            self.transport.records["screen_keys"], 2,
            (EMPTY_SCREEN_KEY, EMPTY_SCREEN_KEY, REMAP_A, EMPTY_SCREEN_KEY))
        hidden_before = self.transport.records["screen_keys"]
        snapshot = self.read()
        self.assertEqual(snapshot["configuration"].display.key_bindings[2], [None] * 4)
        snapshot["configuration"].display.pages[2][1] = "play_pause"

        self.service.apply_section(
            "fixture-mc7", snapshot["configuration"], "display", snapshot["baseline"])

        self.assertEqual([packet[1] for packet in self.transport.writes], [0x25])
        self.assertEqual(self.transport.records["screen_keys"], hidden_before)

    def test_stale_screen_key_baseline_blocks_display_before_any_write(self):
        snapshot = self.read()
        self.transport.records["screen_keys"] = with_screen_key_records(
            self.transport.records["screen_keys"], 0,
            (REMAP_A, EMPTY_SCREEN_KEY, EMPTY_SCREEN_KEY, EMPTY_SCREEN_KEY))
        snapshot["configuration"].display.brightness = 80

        with self.assertRaisesRegex(DeviceError, "screen_keys settings changed since the last read"):
            self.service.apply_section(
                "fixture-mc7", snapshot["configuration"], "display", snapshot["baseline"])

        self.assertEqual(self.transport.writes, [])

    def test_screen_key_readback_mismatch_reports_uncertain_state(self):
        self.transport.records["lcd"] = with_lcd_page(
            self.transport.records["lcd"], 2,
            (b"\x05\x00", b"\x19\xff", b"\x1a\xff", b"\x1f\xff"))
        snapshot = self.read()
        snapshot["configuration"].display.key_bindings[2][0] = "A"
        self.transport.ignore_writes = True

        with self.assertRaisesRegex(
                DeviceError, "screen_keys readback does not match.*Some settings may have changed"):
            self.service.apply_section(
                "fixture-mc7", snapshot["configuration"], "display", snapshot["baseline"])

        self.assertEqual([packet[1] for packet in self.transport.writes], [0x29])

    def test_stale_lcd_baseline_blocks_display_even_with_unchanged_pages(self):
        snapshot = self.read()
        raw = bytearray(self.transport.records["lcd"])
        raw[30] = 0x20  # Page three: Undo becomes Redo.
        self.transport.records["lcd"] = with_checksum(raw)
        snapshot["configuration"].display.brightness = 80
        with self.assertRaisesRegex(DeviceError, "changed since the last read"):
            self.service.apply_section("fixture-mc7", snapshot["configuration"], "display", snapshot["baseline"])
        self.assertEqual(self.transport.writes, [])

    def test_unverified_lcd_write_reports_uncertainty(self):
        self.transport.ignore_writes = True
        with self.assertRaisesRegex(DeviceError, "LCD layout.*readback"):
            self.service.setup_display("fixture-mc7", 1)
        self.assertEqual(len(self.transport.writes), 1)


if __name__ == "__main__":
    unittest.main()
