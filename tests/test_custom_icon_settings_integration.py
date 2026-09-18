"""End-to-end custom-icon settings transactions against USB byte fixtures."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from swarm2.configuration import encode_host_action_icon_rgba
from swarm2.hardware import transact
from swarm2.host_actions import encode_host_action_record
from swarm2.image_commands import CUSTOM_ICON_RGBA_BYTES
from swarm2.lcd_commands import LCD_WIDGETS, decode_lcd_response
from swarm2.screen_key_commands import decode_screen_key_responses
from swarm2.service import DeviceService
from swarm2.transport import DeviceError
from tests.test_settings import (
    CAPTURES,
    EMPTY_SCREEN_KEY,
    SettingsTransport,
    screen_key_capture,
    with_lcd_page,
    with_screen_key_records,
)


APPLICATION = "/opt/Fixture Application"
APPLICATION_TILE = b"\x05\x03"
REAL_DEVICE_CALL = DeviceService._call


def icon_value(seed: int = 1) -> str:
    pixel = bytes((seed & 0xFF, (seed * 3) & 0xFF, (seed * 7) & 0xFF, 0xFF))
    return encode_host_action_icon_rgba(pixel * (CUSTOM_ICON_RGBA_BYTES // 4))


class CustomIconSettingsTransport(SettingsTransport):
    """Settings fixture extended with global image stores and five key profiles."""

    def __init__(self):
        super().__init__()
        self.profile_screen_keys = [screen_key_capture(profile) for profile in range(5)]
        self.records["screen_keys"] = self.profile_screen_keys[0]
        self.selected_profile = 0
        self.screen_key_reads = []
        self.image_exchanges = []
        self.timeline = []
        self.fail_first_image_selector = False

    def set_screen_key_record(self, profile, page, slot, record):
        state = decode_screen_key_responses(self.profile_screen_keys[profile], profile)
        records = list(state.pages[page].records)
        records[slot] = record
        self.profile_screen_keys[profile] = with_screen_key_records(
            self.profile_screen_keys[profile], page, records)
        if profile == 0:
            self.records["screen_keys"] = self.profile_screen_keys[0]

    def send(self, packet):
        if packet[1] == 0x1C and packet[3] == 0x29:
            self.selected_profile = packet[4]
            self.screen_key_reads.append((packet[4], packet[5] - 1))
            self.timeline.append(("read_screen_keys", packet[4], packet[5] - 1))
        elif packet[1] in (0x29, 0x25):
            self.timeline.append(("write_setting", packet[1]))
        super().send(packet)
        if packet[1] == 0x29:
            profile = packet[4]
            if profile != 0:
                raise AssertionError("The fixture only edits target profile zero")
            self.profile_screen_keys[profile] = self.records["screen_keys"]

    def get_feature(self, selector):
        if selector != 0x29:
            return super().get_feature(selector)
        if self.selected != selector:
            raise AssertionError("GET must follow a matching acknowledged selector")
        start = self.selected_page * 54
        source = self.profile_screen_keys[self.selected_profile]
        return source[start:start + 54]

    def exchange_image(self, report, *, delay_ms):
        command = report[2]
        if len(report) != 64 or report[:2] != b"\x10\xa5":
            raise AssertionError("Image exchange requires a full A5 report")
        expected_delay = 2500 if command == 0xFF else 30
        if delay_ms != expected_delay:
            raise AssertionError("Image exchange used the wrong source delay")
        self.image_exchanges.append((report, delay_ms))
        self.timeline.append(("image", command))
        if self.fail_first_image_selector and len(self.image_exchanges) == 1:
            return bytes((0x10, 0xA5, command - 1))
        return bytes((0x10, 0xA5, command))


class CustomIconSettingsIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.transport = CustomIconSettingsTransport()
        self.service = DeviceService()
        self.helper_results = []

        def helper(request):
            result = transact(request, lambda _device_id: self.transport)
            self.helper_results.append(copy.deepcopy(result))
            return result

        helper_patch = patch.object(DeviceService, "_call", side_effect=helper)
        helper_patch.start()
        self.addCleanup(helper_patch.stop)

    def read(self, draft=None):
        return self.service.read("fixture-mc7", 1, draft=draft)

    @staticmethod
    def configure_application(configuration, icon, *, page=2, slot=0,
                              target=APPLICATION):
        row_count = len(configuration.display.pages)
        previous_widget = configuration.display.pages[page][slot]
        configuration.display.pages[page][slot] = "open_application"
        # Replacing a wide tile also replaces its two continuation cells.
        previous_width = (LCD_WIDGETS[previous_widget].width
                          if previous_widget in LCD_WIDGETS else 1)
        for continuation in range(slot + 1, min(slot + previous_width, 4)):
            configuration.display.pages[page][continuation] = "empty"
        configuration.display.host_action_bindings = [
            [None] * 4 for _ in range(row_count)]
        configuration.display.host_action_icon_bindings = [
            [None] * 4 for _ in range(row_count)]
        configuration.display.host_action_bindings[page][slot] = target
        configuration.display.host_action_icon_bindings[page][slot] = icon

    def reset_activity(self):
        self.transport.sent.clear()
        self.transport.writes.clear()
        self.transport.screen_key_reads.clear()
        self.transport.image_exchanges.clear()
        self.transport.timeline.clear()

    def test_new_application_scans_every_profile_uploads_then_binds_and_rereads(self):
        snapshot = self.read()
        icon = icon_value()
        self.configure_application(snapshot["configuration"], icon)
        self.reset_activity()

        result = self.service.apply_section(
            "fixture-mc7", snapshot["configuration"], "display",
            snapshot["baseline"])

        first_image = next(index for index, event in enumerate(self.transport.timeline)
                           if event[0] == "image")
        reads_before_image = [event[1:] for event in self.transport.timeline[:first_image]
                              if event[0] == "read_screen_keys"]
        self.assertEqual(reads_before_image, [
            (profile, page) for profile in range(5) for page in range(3)])
        final_image = max(index for index, event in enumerate(self.transport.timeline)
                          if event[0] == "image")
        setting_writes = [
            (index, event[1]) for index, event in enumerate(self.transport.timeline)
            if event[0] == "write_setting"]
        self.assertEqual([command for _index, command in setting_writes], [0x29, 0x25])
        self.assertTrue(all(final_image < index for index, _command in setting_writes))
        self.assertEqual(self.transport.image_exchanges[0][0][:3], b"\x10\xa5\x69")

        keys = decode_screen_key_responses(self.transport.records["screen_keys"], 0)
        self.assertEqual(
            keys.pages[2].records[0],
            encode_host_action_record("open_application", APPLICATION, icon_index=0))
        lcd = decode_lcd_response(self.transport.records["lcd"], 0)
        self.assertEqual(lcd.pages[2].slots[0].key, "open_application")
        self.assertEqual(
            result["baseline"]["settings"]["screen_keys"],
            self.transport.records["screen_keys"].hex())
        self.assertEqual(result["configuration"].display.host_action_icon_bindings[2][0],
                         icon)
        upload = self.helper_results[-1]["custom_icon_uploads"][0]
        self.assertEqual((upload["icon_index"], upload["completed_packets"]),
                         (0, len(self.transport.image_exchanges)))
        self.assertFalse(upload["pixel_readback"])

    def test_selector_mismatch_stops_before_any_trigger_or_layout_binding(self):
        snapshot = self.read()
        self.configure_application(snapshot["configuration"], icon_value())
        original_lcd = self.transport.records["lcd"]
        original_keys = self.transport.records["screen_keys"]
        self.reset_activity()
        self.transport.fail_first_image_selector = True

        with self.assertRaisesRegex(
                DeviceError, r"transfer stopped after 0/\d+ packets.*no LCD tile was bound"):
            self.service.apply_section(
                "fixture-mc7", snapshot["configuration"], "display",
                snapshot["baseline"])

        self.assertEqual(len(self.transport.image_exchanges), 1)
        self.assertFalse(any(event[0] == "write_setting"
                             for event in self.transport.timeline))
        self.assertEqual(self.transport.records["lcd"], original_lcd)
        self.assertEqual(self.transport.records["screen_keys"], original_keys)

    def test_allocator_never_overwrites_a_store_referenced_by_another_profile(self):
        occupied = encode_host_action_record(
            "open_application", "/opt/Other", icon_index=0)
        self.transport.set_screen_key_record(4, 1, 3, occupied)
        profile_four_before = self.transport.profile_screen_keys[4]
        snapshot = self.read()
        self.configure_application(snapshot["configuration"], icon_value(2))
        self.reset_activity()

        result = self.service.apply_section(
            "fixture-mc7", snapshot["configuration"], "display",
            snapshot["baseline"])

        self.assertEqual(self.transport.image_exchanges[0][0][2], 106)
        keys = decode_screen_key_responses(self.transport.records["screen_keys"], 0)
        self.assertEqual(keys.pages[2].records[0][4], 2)
        self.assertEqual(self.transport.profile_screen_keys[4], profile_four_before)
        self.assertEqual(
            self.helper_results[-1]["custom_icon_uploads"][0]["icon_index"], 1)
        self.assertTrue(result["summary"]["changed"])

    def test_unchanged_known_icon_keeps_its_index_and_skips_pixel_upload(self):
        icon = icon_value(3)
        snapshot = self.read()
        self.configure_application(snapshot["configuration"], icon)
        applied = self.service.apply_section(
            "fixture-mc7", snapshot["configuration"], "display",
            snapshot["baseline"])
        self.assertEqual(applied["baseline"]["host_action_icon_bindings"][2][0], icon)
        self.assertEqual(applied["baseline"]["host_action_icon_indices"][2][0], 0)
        self.reset_activity()

        result = self.service.apply_section(
            "fixture-mc7", applied["configuration"], "display",
            applied["baseline"])

        self.assertFalse(result["summary"]["changed"])
        self.assertEqual(self.transport.image_exchanges, [])
        self.assertEqual(self.transport.writes, [])
        self.assertEqual(self.transport.screen_key_reads, [
            (profile, page) for profile in range(5) for page in range(3)])

    def test_configuration_readback_mismatch_never_reports_apply_success(self):
        snapshot = self.read()
        self.configure_application(snapshot["configuration"], icon_value(4))
        self.reset_activity()
        self.transport.ignore_writes = True

        with self.assertRaisesRegex(
                DeviceError, r"(lcd|screen_keys) readback does not match.*read again"):
            self.service.apply_section(
                "fixture-mc7", snapshot["configuration"], "display",
                snapshot["baseline"])

        self.assertTrue(self.transport.image_exchanges)
        self.assertEqual([packet[1] for packet in self.transport.writes], [0x29, 0x25])
        self.assertEqual(
            decode_lcd_response(self.transport.records["lcd"], 0).pages[2].slots[0].key,
            "cut")
        self.assertEqual(
            decode_screen_key_responses(
                self.transport.records["screen_keys"], 0).pages[2].records[0],
            EMPTY_SCREEN_KEY)

    def test_partial_layouts_overlay_current_hardware_pages(self):
        for page_count in (1, 2, 3):
            with self.subTest(page_count=page_count):
                self.transport = CustomIconSettingsTransport()
                snapshot = self.read()
                original = decode_lcd_response(self.transport.records["lcd"], 0)
                configuration = snapshot["configuration"]
                configuration.display.pages = configuration.display.pages[:page_count]
                configuration.display.key_bindings = (
                    configuration.display.key_bindings[:page_count])
                configuration.display.macro_bindings = (
                    configuration.display.macro_bindings[:page_count])
                configuration.display.timer_bindings = (
                    configuration.display.timer_bindings[:page_count])
                self.configure_application(
                    configuration, icon_value(page_count), page=page_count - 1)
                self.reset_activity()

                result = self.service.apply_section(
                    "fixture-mc7", configuration, "display", snapshot["baseline"])

                observed = decode_lcd_response(self.transport.records["lcd"], 0)
                self.assertEqual(
                    observed.pages[page_count - 1].slots[0].key,
                    "open_application")
                for page_index in range(page_count, 3):
                    self.assertEqual(
                        observed.pages[page_index].slots,
                        original.pages[page_index].slots)
                self.assertEqual(len(result["configuration"].display.pages), 3)
                self.assertEqual(self.transport.image_exchanges[0][0][2], 105)

    def test_snapshot_separates_local_icon_from_verified_store_mapping(self):
        icon = icon_value(5)
        lcd = with_lcd_page(
            bytes.fromhex(CAPTURES["lcd"]), 2,
            (APPLICATION_TILE, b"\x19\xff", b"\x1a\xff", b"\x1f\xff"))
        good_record = encode_host_action_record(
            "open_application", APPLICATION, icon_index=3)
        good_keys = with_screen_key_records(
            screen_key_capture(0), 2,
            (good_record, EMPTY_SCREEN_KEY, EMPTY_SCREEN_KEY, EMPTY_SCREEN_KEY))
        settings = dict(CAPTURES, lcd=lcd.hex(), screen_keys=good_keys.hex())
        base_result = {
            "raw": CAPTURES["sensor"], "settings": settings, "errors": {},
            "changed": False, "transport_identity": "fixture-usb-port",
        }
        unresolved = DeviceService._snapshot("fixture-mc7", 1, base_result)
        draft = unresolved["configuration"]
        self.configure_application(draft, icon)

        unproven = DeviceService._snapshot(
            "fixture-mc7", 1, base_result, draft)
        self.assertEqual(
            unproven["configuration"].display.host_action_icon_bindings[2][0], icon)
        self.assertIsNone(unproven["baseline"]["host_action_icon_bindings"][2][0])
        self.assertIsNone(unproven["baseline"]["host_action_icon_indices"][2][0])

        reported = [[None] * 4 for _ in range(3)]
        reported[2][0] = 3
        matching = DeviceService._snapshot(
            "fixture-mc7", 1,
            {**base_result, "custom_icon_indices": reported}, draft)
        self.assertEqual(matching["baseline"]["host_action_icon_bindings"][2][0], icon)
        self.assertEqual(matching["baseline"]["host_action_icon_indices"][2][0], 3)

        mismatched = copy.deepcopy(reported)
        mismatched[2][0] = 4
        untrusted = DeviceService._snapshot(
            "fixture-mc7", 1,
            {**base_result, "custom_icon_indices": mismatched}, draft)
        self.assertEqual(
            untrusted["configuration"].display.host_action_icon_bindings[2][0], icon)
        self.assertIsNone(untrusted["baseline"]["host_action_icon_bindings"][2][0])
        self.assertIsNone(untrusted["baseline"]["host_action_icon_indices"][2][0])

        for label, record in (
                ("wrong function", encode_host_action_record(
                    "open_file", APPLICATION)),
                ("invalid index", good_record[:4] + b"\x00" + good_record[5:])):
            with self.subTest(label=label):
                bad_settings = dict(settings)
                bad_settings["screen_keys"] = with_screen_key_records(
                    screen_key_capture(0), 2,
                    (record, EMPTY_SCREEN_KEY, EMPTY_SCREEN_KEY,
                     EMPTY_SCREEN_KEY)).hex()
                snapshot = DeviceService._snapshot(
                    "fixture-mc7", 1,
                    {**base_result, "settings": bad_settings}, draft)
                self.assertIsNone(
                    snapshot["configuration"].display.host_action_icon_bindings[2][0])
                self.assertIsNone(
                    snapshot["configuration"].display.host_action_bindings[2][0])

        missing_keys = dict(settings)
        missing_keys.pop("screen_keys")
        snapshot = DeviceService._snapshot(
            "fixture-mc7", 1, {**base_result, "settings": missing_keys}, draft)
        self.assertIsNone(
            snapshot["configuration"].display.host_action_icon_bindings[2][0])

    def test_missing_or_mismatched_reuse_index_forces_a_fresh_upload(self):
        snapshot = self.read()
        icon = icon_value(6)
        self.configure_application(snapshot["configuration"], icon)
        first = self.service.apply_section(
            "fixture-mc7", snapshot["configuration"], "display",
            snapshot["baseline"])
        self.assertEqual(first["baseline"]["host_action_icon_indices"][2][0], 0)

        missing = copy.deepcopy(first["baseline"])
        missing.pop("host_action_icon_indices")
        self.reset_activity()
        second = self.service.apply_section(
            "fixture-mc7", first["configuration"], "display", missing)
        self.assertEqual(self.transport.image_exchanges[0][0][2], 106)
        self.assertEqual(second["baseline"]["host_action_icon_indices"][2][0], 1)

        mismatched = copy.deepcopy(second["baseline"])
        mismatched["host_action_icon_indices"][2][0] = 0
        self.reset_activity()
        third = self.service.apply_section(
            "fixture-mc7", second["configuration"], "display", mismatched)
        self.assertEqual(self.transport.image_exchanges[0][0][2], 105)
        self.assertEqual(third["baseline"]["host_action_icon_indices"][2][0], 0)

    def test_display_helper_deadline_scales_with_application_tile_count(self):
        def request(count):
            pages = [[None] * 4 for _ in range(3)]
            for index in range(count):
                pages[index // 4][index % 4] = "open_application"
            return {
                "operation": "apply_settings", "section": "display",
                "configuration": {"display": {"pages": pages}},
            }

        completed = SimpleNamespace(stdout=json.dumps({"result": {}}), returncode=0)
        with patch("swarm2.service.subprocess.run", return_value=completed) as run:
            self.assertEqual(REAL_DEVICE_CALL(request(1)), {})
            self.assertEqual(run.call_args.kwargs["timeout"], 90)
            self.assertEqual(REAL_DEVICE_CALL(request(2)), {})
            self.assertEqual(run.call_args.kwargs["timeout"], 95)
            self.assertEqual(REAL_DEVICE_CALL(request(12)), {})
            self.assertEqual(run.call_args.kwargs["timeout"], 345)


if __name__ == "__main__":
    unittest.main()
