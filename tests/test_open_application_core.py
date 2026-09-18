"""Offline acceptance for Open Application records, presets and launch argv."""

import json
import os
from pathlib import Path
import tempfile
import unittest

from swarm2.configuration import (
    Configuration, ConfigurationError, HOST_ACTION_ICON_PREFIX,
    HOST_ACTION_ICON_RGBA_BYTES, MAX_PRESET_BYTES,
    encode_host_action_icon_rgba, host_action_icon_rgba,
)
from swarm2.countdown import validate_countdown_request
from swarm2.host_action_commands import decode_host_action_touch
from swarm2.host_actions import (
    HostActionBinding, build_host_action_argv, decode_host_action_record,
    encode_host_action_record, same_host_action_record,
    validate_host_action_target,
)
from swarm2.lcd_commands import LCD_WIDGETS
from swarm2.lcd_layout import move_page
from swarm2.protocol import ProtocolError
from swarm2.screen_key_commands import (
    build_screen_key_reports, decode_screen_key_responses, screen_key_bindings,
)
from swarm2.lcd_commands import decode_lcd_response
from swarm2.transport import DeviceError
from tests.test_settings import (
    CAPTURES, EMPTY_SCREEN_KEY, with_lcd_page, with_screen_key_records,
)


APPLICATION = "/opt/Example Suite/Example.AppImage"
ICON_RGBA = bytes((1, 2, 3, 4)) * (HOST_ACTION_ICON_RGBA_BYTES // 4)
ICON = encode_host_action_icon_rgba(ICON_RGBA)


class OpenApplicationRecordTests(unittest.TestCase):
    def test_layout_touch_and_exact_command_29_record(self):
        self.assertEqual(LCD_WIDGETS["open_application"].signature, (0x05, 0x03))
        first = encode_host_action_record(
            "open_application", APPLICATION, icon_index=0)
        last = encode_host_action_record(
            "open_application", APPLICATION, icon_index=19)
        self.assertEqual(first, bytes.fromhex("0000010b01") + b"Examp\0")
        self.assertEqual(last[4], 20)
        self.assertEqual(decode_host_action_record(first, "open_application"), "Examp")
        touch = decode_host_action_touch(bytes.fromhex("1033130503010100"))
        self.assertEqual(
            (touch.widget_key, touch.page_index, touch.slot_index, touch.pressed),
            ("open_application", 0, 0, True))

    def test_application_record_requires_and_compares_icon_reference(self):
        requested = encode_host_action_record(
            "open_application", APPLICATION, icon_index=3)
        same = bytearray(requested)
        same[5:] = b"Exa\0x\0"
        shorter = encode_host_action_record(
            "open_application", "/opt/Exa", icon_index=3)
        self.assertTrue(same_host_action_record(same, shorter, "open_application"))
        different_icon = bytearray(shorter)
        different_icon[4] = 5
        self.assertFalse(same_host_action_record(
            different_icon, shorter, "open_application"))
        for icon_index in (None, -1, 20, True):
            with self.subTest(icon_index=icon_index), self.assertRaises(ProtocolError):
                encode_host_action_record(
                    "open_application", APPLICATION, icon_index=icon_index)
        with self.assertRaises(ProtocolError):
            encode_host_action_record(
                "open_website", "https://example.com", icon_index=0)
        for reference in (0, 21):
            invalid = bytearray(requested)
            invalid[4] = reference
            with self.subTest(reference=reference), self.assertRaises(ProtocolError):
                decode_host_action_record(invalid, "open_application")


class OpenApplicationPresetTests(unittest.TestCase):
    @staticmethod
    def configured() -> Configuration:
        result = Configuration()
        result.display.pages = [[
            "open_application", "open_website", "empty", "empty"]]
        result.display.host_action_bindings = [[
            APPLICATION, "https://example.com", None, None]]
        result.display.host_action_icon_bindings = [[ICON, None, None, None]]
        return result

    def test_icon_data_url_is_exact_canonical_rgba_and_roundtrips(self):
        self.assertTrue(ICON.startswith(HOST_ACTION_ICON_PREFIX))
        self.assertEqual(host_action_icon_rgba(ICON), ICON_RGBA)
        with self.assertRaises(ConfigurationError):
            host_action_icon_rgba(ICON + "A")
        with self.assertRaises(ConfigurationError):
            encode_host_action_icon_rgba(ICON_RGBA[:-1])
        configured = self.configured()
        self.assertEqual(Configuration.from_dict(configured.to_dict()), configured)
        self.assertLess(
            len(json.dumps(configured.to_dict()).encode("utf-8")),
            MAX_PRESET_BYTES)

    def test_icon_is_required_exactly_for_resolved_application_tiles(self):
        data = self.configured().to_dict()
        data["display"]["host_action_icon_bindings"][0][0] = None
        with self.assertRaisesRegex(ConfigurationError, "required"):
            Configuration.from_dict(data)
        data = self.configured().to_dict()
        data["display"]["host_action_icon_bindings"][0][1] = ICON
        with self.assertRaisesRegex(ConfigurationError, "must be null"):
            Configuration.from_dict(data)

        unresolved = Configuration()
        unresolved.display.pages = [[
            "open_application", "empty", "empty", "empty"]]
        self.assertEqual(
            Configuration.from_dict(unresolved.to_dict()).display.host_action_icon_bindings,
            [])

    def test_older_v1_display_defaults_icon_matrix_empty(self):
        ordinary = Configuration()
        ordinary.display.pages = [[
            "open_website", "empty", "empty", "empty"]]
        ordinary.display.host_action_bindings = [[
            "https://example.com", None, None, None]]
        data = ordinary.to_dict()
        data["display"].pop("host_action_icon_bindings")
        self.assertEqual(
            Configuration.from_dict(data).display.host_action_icon_bindings, [])

    def test_page_move_validates_parallel_icon_rows(self):
        configured = self.configured()
        pages = configured.display.pages + [["cut", "copy", "paste", "undo"]]
        targets = configured.display.host_action_bindings + [[None] * 4]
        icons = configured.display.host_action_icon_bindings + [[None] * 4]
        self.assertEqual(
            move_page(
                pages, 0, 1, host_action_bindings=targets,
                host_action_icon_bindings=icons),
            [pages[1], pages[0]])
        with self.assertRaisesRegex(ConfigurationError, "custom icon"):
            move_page(pages, 0, 1, host_action_bindings=targets)


class OpenApplicationPlanningAndExecutionTests(unittest.TestCase):
    def test_screen_key_planner_requires_explicit_icon_index_matrix(self):
        lcd_raw = with_lcd_page(
            bytes.fromhex(CAPTURES["lcd"]), 0,
            (b"\x05\x03", b"\xfe\x00", b"\xfe\x00", b"\xfe\x00"))
        lcd = decode_lcd_response(lcd_raw, 0)
        keys = decode_screen_key_responses(
            bytes.fromhex(CAPTURES["screen_keys"]), 0)
        pages = [
            [None if widget is None else widget.key for widget in page.slots]
            for page in lcd.pages[:3]]
        bindings = [list(row) for row in screen_key_bindings(keys, lcd)]
        targets = [[None] * 4 for _ in range(3)]
        targets[0][0] = APPLICATION
        indices = [[None] * 4 for _ in range(3)]
        indices[0][0] = 7
        reports, expected = build_screen_key_reports(
            keys, lcd, pages, bindings, None, targets, indices)
        self.assertEqual(len(reports), 1)
        self.assertEqual(
            expected.pages[0].records[0],
            encode_host_action_record(
                "open_application", APPLICATION, icon_index=7))
        with self.assertRaisesRegex(ProtocolError, "custom-icon index"):
            build_screen_key_reports(keys, lcd, pages, bindings, None, targets)

    def test_listener_baseline_requires_matching_application_icon_reference(self):
        lcd = with_lcd_page(
            bytes.fromhex(CAPTURES["lcd"]), 0,
            (b"\x05\x03", b"\xfe\x00", b"\xfe\x00", b"\xfe\x00"))
        keys = with_screen_key_records(
            bytes.fromhex(CAPTURES["screen_keys"]), 0,
            (encode_host_action_record(
                "open_application", APPLICATION, icon_index=3),
             EMPTY_SCREEN_KEY, EMPTY_SCREEN_KEY, EMPTY_SCREEN_KEY))
        request = {
            "command": "start", "device_id": "fixture", "profile_slot": 1,
            "baseline": {
                "device_id": "fixture", "profile_slot": 1,
                "transport_identity": "fixture",
                "settings": {
                    "profile": CAPTURES["profile"], "lcd": lcd.hex(),
                    "screen_keys": keys.hex(),
                },
            },
            "host_action_bindings": [{
                "widget_key": "open_application", "page_index": 0,
                "slot_index": 0, "target": APPLICATION, "icon_index": 3,
            }],
        }
        prepared = validate_countdown_request(request)
        self.assertEqual(prepared.host_action_bindings[0].icon_index, 3)
        request["host_action_bindings"][0]["icon_index"] = 4
        with self.assertRaisesRegex(DeviceError, "stored LCD key definition"):
            validate_countdown_request(request)
        request["host_action_bindings"][0].pop("icon_index")
        with self.assertRaisesRegex(DeviceError, "custom-icon index"):
            validate_countdown_request(request)

    def test_validation_is_offline_and_execution_resolves_executable(self):
        missing = "/definitely/missing/swarm2-application"
        self.assertEqual(
            validate_host_action_target("open_application", missing), missing)
        for target in ("relative/application", "/", "/opt/.."):
            with self.subTest(target=target), self.assertRaises(ValueError):
                validate_host_action_target("open_application", target)

        repo_tmp = Path(__file__).resolve().parents[1] / "tmp"
        repo_tmp.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=repo_tmp) as directory:
            executable = Path(directory) / "application"
            executable.write_bytes(b"#!/bin/sh\nexit 0\n")
            executable.chmod(0o700)
            binding = HostActionBinding(
                "open_application", 0, 0, str(executable), 2)
            self.assertEqual(
                build_host_action_argv(binding, system="Linux"),
                (os.fspath(executable.resolve()),))
            self.assertEqual(
                build_host_action_argv(binding, system="Darwin"),
                (os.fspath(executable.resolve()),))
            executable.chmod(0o600)
            if os.name == "posix":
                with self.assertRaisesRegex(DeviceError, "not executable"):
                    build_host_action_argv(binding, system="Linux")

            bundle = Path(directory) / "Example.app"
            bundle.mkdir()
            bundle_binding = HostActionBinding(
                "open_application", 0, 0, str(bundle), 2)
            self.assertEqual(
                build_host_action_argv(bundle_binding, system="Darwin"),
                ("/usr/bin/open", os.fspath(bundle.resolve())))


if __name__ == "__main__":
    unittest.main()
