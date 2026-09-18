"""Offline vectors and lifecycle checks for host-owned LCD actions."""

from pathlib import Path
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from unittest.mock import Mock

from swarm2.configuration import Configuration, ConfigurationError
from swarm2.countdown import (
    CountdownEventTransport, CountdownGuard, validate_countdown_request,
)
from swarm2.countdown_runtime import CountdownBinding, LcdActionRuntime
from swarm2.host_action_commands import (
    decode_host_action_press, decode_host_action_touch,
)
from swarm2.host_actions import (
    HostActionBinding, build_host_action_argv, decode_host_action_record,
    encode_host_action_record, execute_host_action, same_host_action_record,
    validate_host_action_target,
)
from swarm2.lcd_commands import LCD_HOST_ACTION_WIDGETS, LCD_WIDGETS
from swarm2.lcd_layout import move_page
from swarm2.protocol import ProtocolError
from swarm2.screen_key_commands import decode_screen_key_responses
from swarm2.service import DeviceService
from swarm2.settings import _plan
from swarm2.transport import DeviceError
from tests.test_settings import (
    CAPTURES, EMPTY_SCREEN_KEY, with_lcd_page, with_screen_key_records,
)


WEBSITE = "https://www.example.com/path"
FILE_TARGET = "/some/path/report.pdf"
FOLDER_TARGET = "/some/path/Images"


def host_lcd_raw(*signatures):
    values = tuple(signatures) + (b"\xfe\x00",) * (4 - len(signatures))
    return with_lcd_page(bytes.fromhex(CAPTURES["lcd"]), 0, values)


class HostActionCodecTests(unittest.TestCase):
    def test_paths_and_labels_remain_portable_between_operating_systems(self):
        for target in ("/home/user/report.pdf", "C:/Users/user/report.pdf", r"C:\Users\user\report.pdf"):
            with self.subTest(target=target):
                self.assertEqual(validate_host_action_target("open_file", target), target)
                record = encode_host_action_record("open_file", target)
                self.assertEqual(decode_host_action_record(record, "open_file"), "repor")
        for target in ("C:relative.txt", "C:/", "C:/Apps/..", "/home/.."):
            with self.subTest(target=target), self.assertRaises(ValueError):
                validate_host_action_target("open_file", target)

    def test_exact_layout_signatures_and_command_29_records(self):
        self.assertEqual(LCD_HOST_ACTION_WIDGETS,
                         frozenset(("open_application", "open_website",
                                    "open_file", "open_folder")))
        self.assertEqual(LCD_WIDGETS["open_application"].signature, (0x05, 0x03))
        self.assertEqual(LCD_WIDGETS["open_website"].signature, (0x05, 0x05))
        self.assertEqual(LCD_WIDGETS["open_file"].signature, (0x05, 0x06))
        self.assertEqual(LCD_WIDGETS["open_folder"].signature, (0x05, 0x04))
        vectors = {
            ("open_website", WEBSITE): bytes.fromhex("0000070b00") + b"examp\0",
            ("open_file", FILE_TARGET): bytes.fromhex("0000050b00") + b"repor\0",
            ("open_folder", FOLDER_TARGET): bytes.fromhex("0000060b00") + b"Image\0",
        }
        for (widget, target), expected in vectors.items():
            with self.subTest(widget=widget):
                encoded = encode_host_action_record(widget, target)
                self.assertEqual(encoded, expected)
                self.assertEqual(decode_host_action_record(encoded, widget),
                                 expected[5:10].decode("ascii"))

    def test_semantic_record_match_preserves_icon_and_qstrncpy_tail(self):
        requested = encode_host_action_record("open_website", "https://a.test")
        retained = bytearray(requested)
        retained[4] = 9
        retained[5:] = b"a.t\0x\0"
        shorter = encode_host_action_record("open_website", "https://a.t")
        self.assertTrue(same_host_action_record(bytes(retained), shorter,
                                               "open_website"))
        self.assertFalse(same_host_action_record(retained, requested,
                                                "open_file"))

    def test_targets_are_purely_validated_and_reject_unsafe_shapes(self):
        self.assertEqual(validate_host_action_target("open_website", WEBSITE), WEBSITE)
        self.assertEqual(validate_host_action_target("open_file", FILE_TARGET), FILE_TARGET)
        bad = (
            ("open_website", "file:///etc/passwd"),
            ("open_website", "https://user:secret@example.com"),
            ("open_website", "https://example.com/has space"),
            ("open_file", "relative.txt"),
            ("open_folder", "/"),
            ("open_file", "/bad\nname"),
        )
        for widget, target in bad:
            with self.subTest(widget=widget, target=target), self.assertRaises(ValueError):
                validate_host_action_target(widget, target)


class HostActionConfigurationTests(unittest.TestCase):
    def configured(self):
        result = Configuration()
        result.display.pages = [[
            "open_website", "open_file", "open_folder", "empty",
        ]]
        result.display.host_action_bindings = [[
            WEBSITE, FILE_TARGET, FOLDER_TARGET, None,
        ]]
        return result

    def test_binding_matrix_roundtrips_and_old_preset_defaults_empty(self):
        configured = self.configured()
        self.assertEqual(Configuration.from_dict(configured.to_dict()), configured)
        old = configured.to_dict()
        old["display"].pop("host_action_bindings")
        self.assertEqual(Configuration.from_dict(old).display.host_action_bindings, [])

    def test_binding_matrix_follows_tiles_and_accepts_explicit_unresolved_target(self):
        data = self.configured().to_dict()
        data["display"]["host_action_bindings"][0][0] = None
        self.assertIsNone(Configuration.from_dict(data).display.host_action_bindings[0][0])
        data["display"]["host_action_bindings"][0][3] = WEBSITE
        with self.assertRaises(ConfigurationError):
            Configuration.from_dict(data)

    def test_page_move_requires_and_preserves_host_targets(self):
        pages = self.configured().display.pages + [["cut", "copy", "paste", "undo"]]
        bindings = self.configured().display.host_action_bindings + [[None] * 4]
        self.assertEqual(move_page(pages, 0, 1, host_action_bindings=bindings),
                         [pages[1], pages[0]])
        with self.assertRaisesRegex(ConfigurationError, "unresolved host action"):
            move_page(pages, 0, 1, host_action_bindings=[])


class HostActionTouchAndRuntimeTests(unittest.TestCase):
    WEBSITE_PRESS = bytes.fromhex("1033130505010100")
    WEBSITE_RELEASE = bytes.fromhex("1033130505010000")
    COUNTDOWN_PRESS = bytes.fromhex("1033124600000100")

    def test_strict_touch_decoder_uses_reversed_wire_slot_coordinates(self):
        touch = decode_host_action_touch(self.WEBSITE_PRESS)
        self.assertEqual((touch.widget_key, touch.page_index, touch.slot_index,
                          touch.pressed), ("open_website", 0, 0, True))
        self.assertFalse(decode_host_action_touch(self.WEBSITE_RELEASE).pressed)
        self.assertIsNone(decode_host_action_press(self.WEBSITE_RELEASE))
        self.assertEqual(
            (decode_host_action_touch(bytes.fromhex("1033210506010100")).page_index,
             decode_host_action_touch(bytes.fromhex("1033210506010100")).slot_index),
            (1, 2))
        self.assertEqual(decode_host_action_touch(
            bytes.fromhex("1033300504010100")).widget_key, "open_folder")
        for malformed in (bytes(7), bytes.fromhex("1033000505010100"),
                          bytes.fromhex("1033140505010100")):
            with self.subTest(malformed=malformed), self.assertRaises(ProtocolError):
                decode_host_action_touch(malformed)
        for unrelated in (
                bytes.fromhex("1033130505000100"),
                bytes.fromhex("1033130505010101")):
            with self.subTest(unrelated=unrelated):
                self.assertIsNone(decode_host_action_touch(unrelated))

    def test_one_runtime_dispatches_host_edges_and_countdown_touches(self):
        transport = SimpleNamespace(send=Mock())
        launched, messages = [], []
        runtime = LcdActionRuntime(
            [CountdownBinding("timer", 0, 1, 2)],
            [HostActionBinding("open_website", 0, 0, WEBSITE)],
            transport, messages.append, clock=lambda: 10,
            launcher=launched.append,
        )
        runtime.start()
        self.assertEqual(messages[-1], {
            "type": "ready", "timers": 1, "positions": 1, "host_actions": 1,
        })
        self.assertTrue(runtime.observe(self.WEBSITE_PRESS))
        self.assertTrue(runtime.observe(self.WEBSITE_PRESS))
        self.assertEqual(len(launched), 1)
        self.assertTrue(runtime.observe(self.WEBSITE_RELEASE))
        self.assertTrue(runtime.observe(self.WEBSITE_PRESS))
        self.assertEqual(len(launched), 2)
        self.assertTrue(runtime.observe(self.COUNTDOWN_PRESS, now=20))
        self.assertEqual(runtime.running_timer_ids, ("timer",))
        with self.assertRaisesRegex(ValueError, "configured once"):
            LcdActionRuntime(
                [CountdownBinding("timer", 0, 0, 1)],
                [HostActionBinding("open_website", 0, 0, WEBSITE)], transport)

    def test_host_only_start_and_stop_both_guard_the_listener_state(self):
        guards = Mock()
        runtime = LcdActionRuntime(
            [], [HostActionBinding("open_website", 0, 0, WEBSITE)],
            SimpleNamespace(send=Mock()), guard=guards, launcher=Mock())
        runtime.start()
        runtime.stop()
        self.assertEqual(guards.call_count, 2)


class HostActionExecutionTests(unittest.TestCase):
    def test_foreign_platform_path_cannot_be_resolved_on_this_host(self):
        target = "/home/user/file.txt" if os.name == "nt" else "C:/Users/user/file.txt"
        binding = HostActionBinding("open_file", 0, 0, target)
        with patch("swarm2.host_actions.Path.resolve") as resolve:
            with self.assertRaisesRegex(DeviceError, "on this operating system"):
                build_host_action_argv(binding)
        resolve.assert_not_called()

    def test_launcher_builds_fixed_argv_and_never_uses_a_shell(self):
        binding = HostActionBinding("open_website", 0, 0, WEBSITE)
        self.assertEqual(build_host_action_argv(
            binding, system="Linux", which=lambda name: "/usr/bin/xdg-open"),
            ("/usr/bin/xdg-open", WEBSITE))
        process = Mock()
        execute_host_action(binding, system="Darwin", process_factory=process)
        self.assertEqual(process.call_args.args, (("/usr/bin/open", WEBSITE),))
        self.assertFalse(process.call_args.kwargs["shell"])
        self.assertTrue(process.call_args.kwargs["close_fds"])

    def test_file_and_folder_type_are_checked_only_at_execution(self):
        here = Path(__file__).resolve()
        self.assertEqual(build_host_action_argv(
            HostActionBinding("open_file", 0, 0, str(here)), system="Darwin"),
            ("/usr/bin/open", str(here)))
        self.assertEqual(build_host_action_argv(
            HostActionBinding("open_folder", 0, 0, str(here.parent)), system="Darwin"),
            ("/usr/bin/open", str(here.parent)))
        with self.assertRaisesRegex(DeviceError, "not a directory"):
            build_host_action_argv(
                HostActionBinding("open_folder", 0, 0, str(here)), system="Darwin")


class HostActionSettingsTests(unittest.TestCase):
    def draft_and_result(self, record=None):
        lcd = host_lcd_raw(b"\x05\x05")
        screen_keys = with_screen_key_records(
            bytes.fromhex(CAPTURES["screen_keys"]), 0,
            (record or encode_host_action_record("open_website", WEBSITE),
             EMPTY_SCREEN_KEY, EMPTY_SCREEN_KEY, EMPTY_SCREEN_KEY))
        draft = Configuration()
        draft.display.pages = [["open_website", "empty", "empty", "empty"]]
        draft.display.host_action_bindings = [[WEBSITE, None, None, None]]
        settings = dict(CAPTURES, lcd=lcd.hex(), screen_keys=screen_keys.hex())
        return draft, {"raw": CAPTURES["sensor"], "settings": settings,
                       "changed": False}

    def test_snapshot_retains_target_only_for_matching_tile_and_trigger(self):
        draft, result = self.draft_and_result()
        snapshot = DeviceService._snapshot("fixture", 1, result, draft)
        self.assertEqual(snapshot["configuration"].display.host_action_bindings[0][0],
                         WEBSITE)
        mismatch = encode_host_action_record("open_file", FILE_TARGET)
        draft, result = self.draft_and_result(mismatch)
        snapshot = DeviceService._snapshot("fixture", 1, result, draft)
        self.assertIsNone(snapshot["configuration"].display.host_action_bindings[0][0])

    def test_display_plan_writes_trigger_before_new_layout_and_predicts_it(self):
        raw = {name: bytes.fromhex(value) for name, value in CAPTURES.items()}
        snapshot = DeviceService._snapshot(
            "fixture", 1, {"raw": CAPTURES["sensor"], "settings": CAPTURES,
                            "changed": False})
        draft = snapshot["configuration"]
        draft.display.pages[2][0] = "open_website"
        draft.display.host_action_bindings = [[None] * 4 for _ in range(3)]
        draft.display.host_action_bindings[2][0] = WEBSITE
        reports, predicted = _plan("display", draft, raw, 0)
        self.assertEqual([report[1] for report in reports], [0x29, 0x25])
        screen = decode_screen_key_responses(bytes(predicted["screen_keys"]), 0)
        self.assertEqual(screen.pages[2].records[0],
                         encode_host_action_record("open_website", WEBSITE))

    def test_helper_accepts_host_only_request_bound_to_the_stored_tile(self):
        lcd = host_lcd_raw(b"\x05\x05")
        screen_keys = with_screen_key_records(
            bytes.fromhex(CAPTURES["screen_keys"]), 0,
            (encode_host_action_record("open_website", WEBSITE),
             EMPTY_SCREEN_KEY, EMPTY_SCREEN_KEY, EMPTY_SCREEN_KEY))
        request = {
            "command": "start", "device_id": "fixture", "profile_slot": 1,
            "baseline": {"device_id": "fixture", "profile_slot": 1,
                         "transport_identity": "fixture",
                         "settings": {"profile": CAPTURES["profile"],
                                      "lcd": lcd.hex(),
                                      "screen_keys": screen_keys.hex()}},
            "host_action_bindings": [{
                "widget_key": "open_website", "page_index": 0,
                "slot_index": 0, "target": WEBSITE,
            }],
        }
        prepared = validate_countdown_request(request)
        self.assertEqual(prepared.bindings, ())
        self.assertEqual(prepared.host_action_bindings[0].target, WEBSITE)
        request["host_action_bindings"][0]["slot_index"] = 1
        with self.assertRaisesRegex(DeviceError, "stored LCD tile"):
            validate_countdown_request(request)

    def test_helper_requires_matching_screen_key_baseline_for_host_actions(self):
        lcd = host_lcd_raw(b"\x05\x05")
        request = {
            "command": "start", "device_id": "fixture", "profile_slot": 1,
            "baseline": {"device_id": "fixture", "profile_slot": 1,
                         "transport_identity": "fixture",
                         "settings": {"profile": CAPTURES["profile"],
                                      "lcd": lcd.hex()}},
            "host_action_bindings": [{
                "widget_key": "open_website", "page_index": 0,
                "slot_index": 0, "target": WEBSITE,
            }],
        }
        with self.assertRaisesRegex(DeviceError, "key definitions"):
            validate_countdown_request(request)
        request["baseline"]["settings"]["screen_keys"] = CAPTURES["screen_keys"]
        with self.assertRaisesRegex(DeviceError, "stored LCD key definition"):
            validate_countdown_request(request)

    def test_event_transport_allows_only_bounded_screen_key_page_reads(self):
        for profile in (0, 4):
            for page in (1, 3):
                report = bytes((0x10, 0x1C, 0, 0x29, profile, page, 0)) + bytes(57)
                CountdownEventTransport._validate_report(report)
        for identity in ((5, 1), (0, 0), (0, 4)):
            report = bytes((0x10, 0x1C, 0, 0x29, *identity, 0)) + bytes(57)
            with self.assertRaises(DeviceError):
                CountdownEventTransport._validate_report(report)

    def test_guard_checks_screen_key_signature_when_host_actions_are_enabled(self):
        lcd = host_lcd_raw(b"\x05\x05")
        keys = with_screen_key_records(
            bytes.fromhex(CAPTURES["screen_keys"]), 0,
            (encode_host_action_record("open_website", WEBSITE),
             EMPTY_SCREEN_KEY, EMPTY_SCREEN_KEY, EMPTY_SCREEN_KEY))
        from swarm2.display_commands import decode_profile_response
        from swarm2.lcd_commands import decode_lcd_response
        prepared = SimpleNamespace(
            profile_index=0,
            expected_profile=decode_profile_response(bytes.fromhex(CAPTURES["profile"])),
            expected_lcd=decode_lcd_response(lcd, 0),
            expected_screen_keys=decode_screen_key_responses(keys, 0),
        )
        guard = CountdownGuard(prepared, object())
        with unittest.mock.patch(
                "swarm2.countdown.read_raw",
                side_effect=[bytes.fromhex(CAPTURES["profile"]), lcd, keys]):
            guard()
        changed = with_screen_key_records(
            keys, 0, (EMPTY_SCREEN_KEY,) * 4)
        with unittest.mock.patch(
                "swarm2.countdown.read_raw",
                side_effect=[bytes.fromhex(CAPTURES["profile"]), lcd, changed]):
            with self.assertRaisesRegex(DeviceError, "key definitions changed"):
                guard()


if __name__ == "__main__":
    unittest.main()
