"""Desktop countdown controller tests without opening a USB device."""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QProcess
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtWidgets import QApplication
    from swarm2.gui.app import MainWindow
except ModuleNotFoundError as error:
    if error.name and error.name.startswith("PySide6"):
        QApplication = MainWindow = QCloseEvent = QProcess = None
    else:
        raise

from swarm2.configuration import Configuration, CountdownTimer, PresetStore
from swarm2.host_actions import encode_host_action_record
from swarm2.lcd_commands import LCD_WIDGETS, decode_lcd_response
from tests.test_gui import FakeService
from tests.test_settings import (
    CAPTURES, screen_key_capture, with_lcd_page, with_screen_key_records,
)


class FakeSignal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, *args):
        for callback in tuple(self.callbacks):
            callback(*args)


class FakeProcess:
    def __init__(self, _parent=None):
        self.started = FakeSignal()
        self.readyReadStandardOutput = FakeSignal()
        self.readyReadStandardError = FakeSignal()
        self.errorOccurred = FakeSignal()
        self.finished = FakeSignal()
        self.mode = None
        self.program = None
        self.arguments = None
        self.writes = []
        self.stdout = bytearray()
        self.stderr = bytearray()
        self.closed_input = False
        self.deleted = False
        self.kill_count = 0
        self.write_result = None
        self.start_error = None
        self._state = QProcess.ProcessState.NotRunning

    def setProcessChannelMode(self, mode):
        self.mode = mode

    def start(self, program, arguments):
        self.program, self.arguments = program, list(arguments)
        if self.start_error is not None:
            raise self.start_error
        self._state = QProcess.ProcessState.Running
        self.started.emit()

    def state(self):
        return self._state

    def write(self, data):
        self.writes.append(bytes(data))
        return len(data) if self.write_result is None else self.write_result

    def closeWriteChannel(self):
        self.closed_input = True

    def readAllStandardOutput(self):
        value = bytes(self.stdout)
        self.stdout.clear()
        return value

    def readAllStandardError(self):
        value = bytes(self.stderr)
        self.stderr.clear()
        return value

    def errorString(self):
        return "fixture process error"

    def deleteLater(self):
        self.deleted = True

    def kill(self):
        self.kill_count += 1
        self.finish(exit_code=9, exit_status=QProcess.ExitStatus.CrashExit)

    def push(self, *messages):
        self.stdout.extend(b"".join(
            json.dumps(message).encode("utf-8") + b"\n"
            for message in messages
        ))
        self.readyReadStandardOutput.emit()

    def push_raw(self, value):
        self.stdout.extend(value)
        self.readyReadStandardOutput.emit()

    def finish(self, *, exit_code=0,
               exit_status=QProcess.ExitStatus.NormalExit):
        if self._state == QProcess.ProcessState.NotRunning:
            return
        self._state = QProcess.ProcessState.NotRunning
        self.finished.emit(exit_code, exit_status)


@unittest.skipIf(MainWindow is None, "Install the gui extra to exercise Qt")
class CountdownGuiRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = FakeService()
        self.processes = []
        self.next_process_write_result = None
        self.next_process_start_error = None

        def process_factory(parent):
            process = FakeProcess(parent)
            process.write_result = self.next_process_write_result
            process.start_error = self.next_process_start_error
            self.processes.append(process)
            return process

        self.window = MainWindow(
            service=self.service,
            store=PresetStore(Path(self.directory.name) / "presets"),
            auto_discover=False,
            countdown_process_factory=process_factory,
        )
        lcd = with_lcd_page(
            bytes.fromhex(CAPTURES["lcd"]), 0,
            (b"\x46\x00", b"\x64\x00", b"\xfe\x00", b"\xfe\x00"),
        )
        lcd_state = decode_lcd_response(lcd, 0)
        pages = [[widget.key if widget is not None else None
                  for widget in page.slots]
                 for page in lcd_state.pages[:lcd_state.page_count]]
        timer = CountdownTimer(id="tea-timer", name="Tea", duration_seconds=3)
        configuration = Configuration(countdown_timers=[timer])
        configuration.display.pages = pages
        configuration.display.timer_bindings = [
            [timer.id, None, None, None], [None] * 4, [None] * 4,
        ]
        self.fixture_configuration = copy.deepcopy(configuration)
        self.window.draft = configuration
        self.window.devices = [{
            "id": "test-mc7", "label": "MC7 fixture", "connected": True,
            "capabilities": ["read_settings", "read_status", "host_lcd", "countdown"],
        }]
        self.window.selected_device_id = "test-mc7"
        self.window.snapshot = {
            "device_id": "test-mc7", "profile_slot": 1,
            "configuration": copy.deepcopy(configuration),
            "verified_fields": ["display.pages"],
            "summary": {"active_profile": 1},
            "baseline": {
                "device_id": "test-mc7", "profile_slot": 1,
                "transport_identity": "fixture-usb-port",
                "settings": {"profile": CAPTURES["profile"], "lcd": lcd.hex()},
            },
        }
        self.fixture_snapshot = copy.deepcopy(self.window.snapshot)
        self.window._load_draft()
        self.window._update_capabilities()

    def restore_fixture(self):
        self.window.draft = copy.deepcopy(self.fixture_configuration)
        self.window.snapshot = copy.deepcopy(self.fixture_snapshot)
        self.window.dirty = False
        self.window._load_draft()
        self.window._update_capabilities()

    def configure_host_actions(self, *, include_timer=True):
        timer = CountdownTimer(
            id="tea-timer", name="Tea", duration_seconds=3)
        targets = {
            "open_website": "https://example.com/mc7",
            "open_file": "/home/example/Documents/mc7.txt",
            "open_folder": "/home/example/Documents",
        }
        widgets = (["countdown"] if include_timer else []) + list(targets)
        widgets += ["empty"] * (4 - len(widgets))
        lcd = with_lcd_page(
            bytes.fromhex(CAPTURES["lcd"]), 0,
            tuple(bytes(LCD_WIDGETS[widget].signature) for widget in widgets),
        )
        lcd_state = decode_lcd_response(lcd, 0)
        pages = [[widget.key if widget is not None else None
                  for widget in page.slots]
                 for page in lcd_state.pages[:lcd_state.page_count]]
        configuration = Configuration(
            countdown_timers=[timer] if include_timer else [])
        configuration.display.pages = pages
        configuration.display.timer_bindings = [[None] * 4 for _ in pages]
        configuration.display.host_action_bindings = [[None] * 4 for _ in pages]
        records = []
        for slot, widget in enumerate(widgets):
            if widget == "countdown" or widget == "empty":
                records.append(bytes(11))
                continue
            target = targets[widget]
            configuration.display.host_action_bindings[0][slot] = target
            records.append(encode_host_action_record(widget, target))
        if include_timer:
            configuration.display.timer_bindings[0][0] = timer.id
        screen_keys = with_screen_key_records(
            screen_key_capture(), 0, tuple(records))
        self.window.draft = configuration
        self.window.snapshot = {
            "device_id": "test-mc7", "profile_slot": 1,
            "configuration": copy.deepcopy(configuration),
            "verified_fields": ["display.pages"],
            "summary": {"active_profile": 1},
            "baseline": {
                "device_id": "test-mc7", "profile_slot": 1,
                "transport_identity": "fixture-usb-port",
                "settings": {
                    "profile": CAPTURES["profile"], "lcd": lcd.hex(),
                    "screen_keys": screen_keys.hex(),
                },
            },
        }
        self.window.dirty = False
        self.window._load_draft()
        self.window._update_capabilities()
        return targets

    def configure_general_media(self):
        lcd = with_lcd_page(
            bytes.fromhex(CAPTURES["lcd"]), 0,
            (b"\x45\x00", b"\x00\x00", b"\x00\x00", b"\x64\x00"),
        )
        lcd_state = decode_lcd_response(lcd, 0)
        pages = [[widget.key if widget is not None else None
                  for widget in page.slots]
                 for page in lcd_state.pages[:lcd_state.page_count]]
        configuration = Configuration()
        configuration.display.pages = pages
        self.window.draft = configuration
        self.window.snapshot = {
            "device_id": "test-mc7", "profile_slot": 1,
            "configuration": copy.deepcopy(configuration),
            "verified_fields": ["display.pages"],
            "summary": {"active_profile": 1},
            "baseline": {
                "device_id": "test-mc7", "profile_slot": 1,
                "transport_identity": "fixture-usb-port",
                "settings": {
                    "profile": CAPTURES["profile"], "lcd": lcd.hex(),
                },
            },
        }
        self.window.dirty = False
        self.window._load_draft()
        self.window._update_capabilities()

    def configure_launch_obs(self):
        lcd = with_lcd_page(
            bytes.fromhex(CAPTURES["lcd"]), 0,
            (b"\x4b\x00", b"\xfe\x00", b"\xfe\x00", b"\xfe\x00"),
        )
        lcd_state = decode_lcd_response(lcd, 0)
        pages = [
            [widget.key if widget is not None else None for widget in page.slots]
            for page in lcd_state.pages[:lcd_state.page_count]
        ]
        configuration = Configuration()
        configuration.display.pages = pages
        self.window.draft = configuration
        self.window.snapshot = {
            "device_id": "test-mc7", "profile_slot": 1,
            "configuration": copy.deepcopy(configuration),
            "verified_fields": ["display.pages"],
            "summary": {"active_profile": 1},
            "baseline": {
                "device_id": "test-mc7", "profile_slot": 1,
                "transport_identity": "fixture-usb-port",
                "settings": {
                    "profile": CAPTURES["profile"], "lcd": lcd.hex(),
                },
            },
        }
        self.window.dirty = False
        self.window._load_draft()
        self.window._update_capabilities()

    def configure_obs_screenshot(self):
        lcd = with_lcd_page(
            bytes.fromhex(CAPTURES["lcd"]), 0,
            (b"\x43\x07", b"\xfe\x00", b"\xfe\x00", b"\xfe\x00"),
        )
        lcd_state = decode_lcd_response(lcd, 0)
        pages = [
            [widget.key if widget is not None else None for widget in page.slots]
            for page in lcd_state.pages[:lcd_state.page_count]
        ]
        configuration = Configuration()
        configuration.display.pages = pages
        self.window.draft = configuration
        self.window.snapshot = {
            "device_id": "test-mc7", "profile_slot": 1,
            "configuration": copy.deepcopy(configuration),
            "verified_fields": ["display.pages"],
            "summary": {"active_profile": 1},
            "baseline": {
                "device_id": "test-mc7", "profile_slot": 1,
                "transport_identity": "fixture-usb-port",
                "settings": {
                    "profile": CAPTURES["profile"], "lcd": lcd.hex(),
                },
            },
        }
        self.window.dirty = False
        self.window._load_draft()
        self.window._update_capabilities()

    def configure_obs_studio_mode(self):
        lcd = with_lcd_page(
            bytes.fromhex(CAPTURES["lcd"]), 0,
            (b"\x43\x09", b"\xfe\x00", b"\xfe\x00", b"\xfe\x00"),
        )
        lcd_state = decode_lcd_response(lcd, 0)
        pages = [
            [widget.key if widget is not None else None for widget in page.slots]
            for page in lcd_state.pages[:lcd_state.page_count]
        ]
        configuration = Configuration()
        configuration.display.pages = pages
        self.window.draft = configuration
        self.window.snapshot = {
            "device_id": "test-mc7", "profile_slot": 1,
            "configuration": copy.deepcopy(configuration),
            "verified_fields": ["display.pages"],
            "summary": {"active_profile": 1},
            "baseline": {
                "device_id": "test-mc7", "profile_slot": 1,
                "transport_identity": "fixture-usb-port",
                "settings": {
                    "profile": CAPTURES["profile"], "lcd": lcd.hex(),
                },
            },
        }
        self.window.dirty = False
        self.window._load_draft()
        self.window._update_capabilities()

    def tearDown(self):
        process = self.window._countdown_process
        if process is not None:
            process.kill()
        self.window.dirty = False
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.directory.cleanup()

    def start_listener(self):
        self.assertTrue(self.window.countdown_checkbox.isEnabled())
        self.window.countdown_checkbox.setChecked(True)
        process = self.processes[-1]
        request = json.loads(process.writes[0])
        return process, request

    def test_start_request_uses_exact_read_baseline_and_bound_timer(self):
        self.window.snapshot["baseline"]["settings"].update({
            "sensor": "ignored", "macros": ["also ignored"],
        })
        process, request = self.start_listener()

        self.assertEqual((process.program, process.arguments[-1]),
                         (os.sys.executable, "swarm2.countdown"))
        self.assertEqual(request["baseline"], {
            "device_id": "test-mc7", "profile_slot": 1,
            "transport_identity": "fixture-usb-port",
            "settings": {"profile": CAPTURES["profile"],
                         "lcd": self.window.snapshot["baseline"]["settings"]["lcd"]},
        })
        self.assertEqual(request["bindings"], [{
            "timer_id": "tea-timer", "page_index": 0, "slot_index": 0,
            "duration_seconds": 3,
        }])
        self.assertEqual(request["host_action_bindings"], [])
        self.assertTrue(self.window._device_or_dialog_busy())
        self.assertFalse(self.window.read_button.isEnabled())
        self.assertFalse(self.window.host_lcd_checkbox.isEnabled())
        self.assertFalse(self.window.background_button.isEnabled())

    def test_mixed_listener_sends_screen_keys_and_accepts_exact_launch_event(self):
        targets = self.configure_host_actions(include_timer=True)

        process, request = self.start_listener()

        self.assertEqual(request["bindings"], [{
            "timer_id": "tea-timer", "page_index": 0, "slot_index": 0,
            "duration_seconds": 3,
        }])
        self.assertEqual(request["host_action_bindings"], [
            {"widget_key": "open_website", "page_index": 0,
             "slot_index": 1, "target": targets["open_website"]},
            {"widget_key": "open_file", "page_index": 0,
             "slot_index": 2, "target": targets["open_file"]},
            {"widget_key": "open_folder", "page_index": 0,
             "slot_index": 3, "target": targets["open_folder"]},
        ])
        self.assertEqual(request["baseline"]["settings"], {
            "profile": CAPTURES["profile"],
            "lcd": self.window.snapshot["baseline"]["settings"]["lcd"],
            "screen_keys": self.window.snapshot["baseline"]["settings"][
                "screen_keys"],
        })
        process.push({"type": "ready", "timers": 1, "positions": 1,
                      "host_actions": 3})
        self.assertIn("3 host action tile(s)", self.window.countdown_status.text())
        process.push({
            "type": "host_action", "event": "opened",
            "widget": "open_website", "page_index": 0, "slot_index": 1,
        })
        self.assertEqual(
            self.window.countdown_status.text(),
            "Open website opened from page 1, slot 2.",
        )
        self.assertIsNotNone(self.window.snapshot)

    def test_host_only_listener_starts_and_requires_screen_key_baseline(self):
        self.configure_host_actions(include_timer=False)
        process, request = self.start_listener()
        self.assertEqual(request["bindings"], [])
        self.assertEqual(len(request["host_action_bindings"]), 3)
        process.push({"type": "ready", "timers": 0, "positions": 0,
                      "host_actions": 3})
        self.assertIn("Active for 3 host action tile(s)",
                      self.window.countdown_status.text())

        process.kill()
        self.configure_host_actions(include_timer=False)
        del self.window.snapshot["baseline"]["settings"]["screen_keys"]
        self.window._update_capabilities()
        self.assertFalse(self.window.countdown_checkbox.isEnabled())
        request, error = self.window._countdown_start_request()
        self.assertIsNone(request)
        self.assertIn("LCD action records", error)

    def test_launch_obs_tile_uses_listener_without_target_or_screen_key_record(self):
        self.configure_launch_obs()
        self.assertGreaterEqual(
            self.window.lcd_controls[0][0].findData("launch_obs"), 0
        )
        self.assertEqual(
            self.window.lcd_controls[0][0].currentData(), "launch_obs"
        )

        process, request = self.start_listener()
        self.assertEqual(request["bindings"], [])
        self.assertEqual(request["host_action_bindings"], [])
        self.assertEqual(request["general_media_bindings"], [])
        self.assertEqual(request["obs_launch_bindings"], [
            {"page_index": 0, "slot_index": 0},
        ])
        self.assertNotIn("screen_keys", request["baseline"]["settings"])
        process.push({
            "type": "ready", "timers": 0, "positions": 0,
            "host_actions": 1,
        })
        self.assertIn("1 host action tile(s)", self.window.countdown_status.text())
        process.push({
            "type": "host_action", "event": "opened",
            "widget": "launch_obs", "page_index": 0, "slot_index": 0,
        })
        self.assertEqual(
            self.window.countdown_status.text(),
            "Launch OBS opened from page 1, slot 1.",
        )

    def test_launch_obs_failure_is_recoverable_and_keeps_verified_read(self):
        self.configure_launch_obs()
        process, _ = self.start_listener()
        process.push({
            "type": "ready", "timers": 0, "positions": 0,
            "host_actions": 1,
        })
        snapshot = self.window.snapshot

        process.push({
            "type": "host_action", "event": "failed",
            "widget": "launch_obs", "page_index": 0, "slot_index": 0,
            "error": "Install OBS Studio so its obs executable is on PATH",
        })

        self.assertIs(self.window._countdown_process, process)
        self.assertIs(self.window.snapshot, snapshot)
        self.assertEqual(process.kill_count, 0)
        self.assertEqual(
            self.window.countdown_status.text(),
            "Launch OBS failed: Install OBS Studio so its obs executable is on PATH",
        )
        process.push({
            "type": "host_action", "event": "opened",
            "widget": "launch_obs", "page_index": 0, "slot_index": 0,
        })
        self.assertEqual(
            self.window.countdown_status.text(),
            "Launch OBS opened from page 1, slot 1.",
        )

    def test_obs_screenshot_uses_listener_and_accepts_strict_results(self):
        self.configure_obs_screenshot()
        self.assertGreaterEqual(
            self.window.lcd_controls[0][0].findData("obs_screenshot"), 0
        )
        process, request = self.start_listener()
        self.assertEqual(request["bindings"], [])
        self.assertEqual(request["host_action_bindings"], [])
        self.assertEqual(request["general_media_bindings"], [])
        self.assertEqual(request["obs_screenshot_bindings"], [
            {"page_index": 0, "slot_index": 0},
        ])
        self.assertNotIn("screen_keys", request["baseline"]["settings"])
        process.push({
            "type": "ready", "timers": 0, "positions": 0,
            "host_actions": 1,
        })
        process.push({
            "type": "host_action", "event": "triggered",
            "widget": "obs_screenshot", "page_index": 0, "slot_index": 0,
        })
        self.assertEqual(
            self.window.countdown_status.text(),
            "OBS screenshot triggered from page 1, slot 1.",
        )
        process.push({
            "type": "host_action", "event": "failed",
            "widget": "obs_screenshot", "page_index": 0, "slot_index": 0,
            "error": "Enable the WebSocket server in OBS",
        })
        self.assertEqual(
            self.window.countdown_status.text(),
            "OBS screenshot failed: Enable the WebSocket server in OBS",
        )
        self.assertEqual(process.kill_count, 0)

    def test_obs_studio_mode_request_and_verified_transition_status_are_strict(self):
        self.configure_obs_studio_mode()
        self.assertGreaterEqual(
            self.window.lcd_controls[0][0].findData("obs_studio_mode"), 0
        )
        process, request = self.start_listener()
        self.assertEqual(request["bindings"], [])
        self.assertEqual(request["host_action_bindings"], [])
        self.assertEqual(request["general_media_bindings"], [])
        self.assertEqual(request["obs_studio_mode_bindings"], [
            {"page_index": 0, "slot_index": 0},
        ])
        self.assertNotIn("screen_keys", request["baseline"]["settings"])
        process.push({
            "type": "ready", "timers": 0, "positions": 0,
            "host_actions": 1,
        })
        process.push({
            "type": "host_action", "event": "toggled",
            "widget": "obs_studio_mode", "page_index": 0, "slot_index": 0,
            "old_enabled": False, "new_enabled": True,
        })
        self.assertEqual(
            self.window.countdown_status.text(),
            "OBS Studio mode enabled from page 1, slot 1.",
        )
        process.push({
            "type": "host_action", "event": "failed",
            "widget": "obs_studio_mode", "page_index": 0, "slot_index": 0,
            "error": "Enable the WebSocket server in OBS",
        })
        self.assertEqual(
            self.window.countdown_status.text(),
            "OBS Studio mode failed: Enable the WebSocket server in OBS",
        )
        self.assertEqual(process.kill_count, 0)

    def test_invalid_obs_studio_mode_transition_aborts_helper(self):
        base = {
            "type": "host_action", "event": "toggled",
            "widget": "obs_studio_mode", "page_index": 0, "slot_index": 0,
            "old_enabled": False, "new_enabled": True,
        }
        invalid = (
            {key: value for key, value in base.items()
             if key != "new_enabled"},
            dict(base, old_enabled=0),
            dict(base, new_enabled=False),
            dict(base, extra=True),
            dict(base, event="triggered"),
        )
        for message in invalid:
            with self.subTest(message=message):
                self.configure_obs_studio_mode()
                process, _request = self.start_listener()
                process.push({
                    "type": "ready", "timers": 0, "positions": 0,
                    "host_actions": 1,
                })
                process.push(message)
                self.assertEqual(process.kill_count, 1)
                self.assertIsNone(self.window.snapshot)

    def test_invalid_launch_obs_failures_abort_the_helper(self):
        valid = {
            "type": "host_action", "event": "failed",
            "widget": "launch_obs", "page_index": 0, "slot_index": 0,
            "error": "OBS is unavailable",
        }
        invalid = [
            (False, valid),
            (True, {key: value for key, value in valid.items()
                    if key != "error"}),
            (True, dict(valid, extra=True)),
            (True, dict(valid, page_index=1)),
            (True, dict(valid, widget="open_application")),
            (True, dict(valid, error="")),
            (True, dict(valid, error="bad\nerror")),
            (True, dict(valid, error="x" * 4097)),
        ]
        for ready_first, message in invalid:
            with self.subTest(ready_first=ready_first, message=message):
                self.configure_launch_obs()
                process, _ = self.start_listener()
                if ready_first:
                    process.push({
                        "type": "ready", "timers": 0, "positions": 0,
                        "host_actions": 1,
                    })
                process.push(message)
                self.assertEqual(process.kill_count, 1)
                self.assertIsNone(self.window.snapshot)
                self.assertIn(
                    "invalid host-action message",
                    self.window.countdown_status.text(),
                )

    def test_general_media_only_uses_same_process_and_strict_messages(self):
        self.configure_general_media()
        process, request = self.start_listener()
        self.assertEqual(request["bindings"], [])
        self.assertEqual(request["host_action_bindings"], [])
        self.assertEqual(request["general_media_bindings"], [
            {"page_index": 0, "slot_index": 0},
        ])
        self.assertNotIn("screen_keys", request["baseline"]["settings"])

        process.push({
            "type": "ready", "timers": 0, "positions": 0,
            "media_panels": 1,
        })
        self.assertIn("1 General Media tile(s)",
                      self.window.countdown_status.text())
        process.push({
            "type": "media_action", "event": "dispatched",
            "action": "play_pause", "page_index": 0, "slot_index": 0,
        })
        self.assertEqual(
            self.window.countdown_status.text(),
            "Play pause requested from page 1, slot 1.",
        )
        process.push({
            "type": "media_action", "event": "failed", "action": "next",
            "page_index": 0, "slot_index": 0,
            "error": "No MPRIS player is running",
        })
        self.assertEqual(
            self.window.countdown_status.text(),
            "Next failed: No MPRIS player is running",
        )
        self.assertIsNotNone(self.window.snapshot)
        process.push({
            "type": "media_action", "event": "dispatched",
            "action": "repeat", "page_index": 0, "slot_index": 0,
        })
        self.assertEqual(
            self.window.countdown_status.text(),
            "Repeat requested from page 1, slot 1.",
        )

    def test_profile_player_choice_roundtrips_into_media_start_request(self):
        player = "org.mpris.MediaPlayer2.vlc"
        self.configure_general_media()
        self.window._media_players = ({
            "id": player, "label": "VLC", "backend": "mpris",
        },)
        self.window._populate_media_players()

        index = self.window.media_player_selector.findData(player)
        self.assertGreater(index, 0)
        self.window.media_player_selector.setCurrentIndex(index)
        self.assertEqual(
            self.window.draft.display.preferred_media_player, player)
        self.assertTrue(self.window.dirty)
        self.assertIn("VLC", self.window.media_player_status.text())

        process, request = self.start_listener()
        self.assertEqual(request["preferred_media_player"], player)
        self.assertEqual(request["general_media_bindings"], [
            {"page_index": 0, "slot_index": 0},
        ])
        process.push({
            "type": "ready", "timers": 0, "positions": 0,
            "media_panels": 1,
        })
        self.assertEqual(process.kill_count, 0)

    def test_unavailable_saved_player_stays_selected_without_fallback(self):
        player = "org.mpris.MediaPlayer2.vlc"
        self.configure_general_media()
        self.window.draft.display.preferred_media_player = player
        self.window._media_players = ({
            "id": "org.mpris.MediaPlayer2.other",
            "label": "Other", "backend": "mpris",
        },)
        self.window._load_draft()

        self.assertEqual(self.window.media_player_selector.currentData(), player)
        self.assertIn("unavailable",
                      self.window.media_player_selector.currentText())
        self.assertIn("will not control another player",
                      self.window.media_player_status.text())
        _, request = self.start_listener()
        self.assertEqual(request["preferred_media_player"], player)

    def test_player_inventory_boundary_and_close_wait_are_bounded(self):
        player = {
            "id": "org.mpris.MediaPlayer2.vlc",
            "label": "VLC", "backend": "mpris",
        }
        self.assertEqual(
            self.window._validated_media_players([player]), (player,))
        for invalid in (
            [dict(player, extra=True)],
            [dict(player, id="vlc")],
            [dict(player, label="bad\nlabel")],
            [player, player],
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.window._validated_media_players(invalid)

        class PendingJob:
            def __init__(self):
                self.waits = []

            def isRunning(self):
                return True

            def wait(self, milliseconds):
                self.waits.append(milliseconds)
                return False

        job = PendingJob()
        self.window._media_player_job = job
        event = QCloseEvent()
        with patch.object(self.window, "_sync_tray_options"):
            self.window.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertEqual(job.waits, [2000])
        self.assertIn("Media player discovery is finishing",
                      self.window.message.text())
        self.window._media_player_job = None

    def test_combined_timer_launch_and_general_media_share_one_request(self):
        player = "org.mpris.MediaPlayer2.vlc"
        target = "https://example.com/mc7"
        lcd = with_lcd_page(
            bytes.fromhex(CAPTURES["lcd"]), 0,
            (b"\x46\x00", b"\x05\x05", b"\xfe\x00", b"\xfe\x00"),
        )
        lcd = with_lcd_page(
            lcd, 1,
            (b"\x45\x00", b"\x00\x00", b"\x00\x00", b"\x64\x00"),
        )
        state = decode_lcd_response(lcd, 0)
        pages = [[widget.key if widget is not None else None
                  for widget in page.slots]
                 for page in state.pages[:state.page_count]]
        timer = CountdownTimer(
            id="tea-timer", name="Tea", duration_seconds=3)
        configuration = Configuration(countdown_timers=[timer])
        configuration.display.pages = pages
        configuration.display.timer_bindings = [[None] * 4 for _ in pages]
        configuration.display.timer_bindings[0][0] = timer.id
        configuration.display.host_action_bindings = [
            [None] * 4 for _ in pages]
        configuration.display.host_action_bindings[0][1] = target
        configuration.display.preferred_media_player = player
        screen_keys = with_screen_key_records(
            screen_key_capture(), 0,
            (bytes(11), encode_host_action_record("open_website", target),
             bytes(11), bytes(11)),
        )
        self.window.draft = configuration
        self.window.snapshot = {
            "device_id": "test-mc7", "profile_slot": 1,
            "configuration": copy.deepcopy(configuration),
            "verified_fields": ["display.pages"],
            "summary": {"active_profile": 1},
            "baseline": {
                "device_id": "test-mc7", "profile_slot": 1,
                "transport_identity": "fixture-usb-port",
                "settings": {
                    "profile": CAPTURES["profile"], "lcd": lcd.hex(),
                    "screen_keys": screen_keys.hex(),
                },
            },
        }
        self.window.dirty = False
        self.window._load_draft()
        process, request = self.start_listener()

        self.assertEqual(request["bindings"], [{
            "timer_id": "tea-timer", "page_index": 0, "slot_index": 0,
            "duration_seconds": 3,
        }])
        self.assertEqual(request["host_action_bindings"], [{
            "widget_key": "open_website", "page_index": 0,
            "slot_index": 1, "target": target,
        }])
        self.assertEqual(request["general_media_bindings"], [{
            "page_index": 1, "slot_index": 0,
        }])
        self.assertEqual(request["preferred_media_player"], player)
        process.push({
            "type": "ready", "timers": 1, "positions": 1,
            "host_actions": 1, "media_panels": 1,
        })
        self.assertEqual(process.kill_count, 0)
        self.assertIn("1 General Media tile(s)",
                      self.window.countdown_status.text())

    def test_general_media_helper_messages_cannot_claim_other_actions(self):
        invalid = (
            {"type": "ready", "timers": 0, "positions": 0,
             "media_panels": 2},
            {"type": "media_action", "event": "dispatched",
             "action": "stop", "page_index": 0, "slot_index": 0},
            {"type": "media_action", "event": "dispatched",
             "action": "next", "page_index": 0, "slot_index": 1},
            {"type": "media_action", "event": "dispatched",
             "action": "next", "page_index": 0, "slot_index": 0,
             "player_id": "unrequested"},
            {"type": "media_action", "event": "unsupported",
             "action": "repeat", "page_index": 0, "slot_index": 0},
        )
        for message in invalid:
            with self.subTest(message=message):
                self.configure_general_media()
                process, _ = self.start_listener()
                if message["type"] != "ready":
                    process.push({
                        "type": "ready", "timers": 0, "positions": 0,
                        "media_panels": 1,
                    })
                process.push(message)
                self.assertEqual(process.kill_count, 1)
                self.assertIsNone(self.window.snapshot)

    def test_ready_and_host_action_messages_match_requested_launches_exactly(self):
        invalid_ready = [
            {"type": "ready", "timers": 1, "positions": 1},
            {"type": "ready", "timers": 1, "positions": 1,
             "host_actions": 2},
        ]
        for message in invalid_ready:
            with self.subTest(ready=message):
                self.configure_host_actions(include_timer=True)
                process, _ = self.start_listener()
                process.push(message)
                self.assertEqual(process.kill_count, 1)
                self.assertIsNone(self.window.snapshot)
                self.assertIn("invalid ready message",
                              self.window.countdown_status.text())

        invalid_events = [
            {"type": "host_action", "event": "opened",
             "widget": "open_file", "page_index": 0, "slot_index": 1},
            {"type": "host_action", "event": "opened",
             "widget": "open_website", "page_index": 1, "slot_index": 1},
            {"type": "host_action", "event": "closed",
             "widget": "open_website", "page_index": 0, "slot_index": 1},
            {"type": "host_action", "event": "opened",
             "widget": "open_website", "page_index": 0, "slot_index": 1,
             "target": "https://example.com/mc7"},
        ]
        for message in invalid_events:
            with self.subTest(event=message):
                self.configure_host_actions(include_timer=True)
                process, _ = self.start_listener()
                process.push({"type": "ready", "timers": 1,
                              "positions": 1, "host_actions": 3})
                process.push(message)
                self.assertEqual(process.kill_count, 1)
                self.assertIsNone(self.window.snapshot)
                self.assertIn("invalid host-action message",
                              self.window.countdown_status.text())

        self.restore_fixture()
        process, _ = self.start_listener()
        process.push({"type": "ready", "timers": 1, "positions": 1,
                      "host_actions": 0})
        self.assertEqual(process.kill_count, 1)
        self.assertIn("invalid ready message",
                      self.window.countdown_status.text())

    def test_partial_start_write_aborts_without_using_unverified_snapshot(self):
        self.next_process_write_result = 1

        self.window.countdown_checkbox.setChecked(True)

        process = self.processes[-1]
        self.assertEqual(process.kill_count, 1)
        self.assertIsNone(self.window._countdown_process)
        self.assertIsNone(self.window.snapshot)
        self.assertIn("could not be written completely",
                      self.window.countdown_status.text())

    def test_ready_timer_events_and_verified_stop_update_status(self):
        process, _ = self.start_listener()
        process.push({"type": "ready", "timers": 1, "positions": 1})
        self.assertIn("Active for 1 timer", self.window.countdown_status.text())
        process.push({"type": "timer", "event": "started",
                      "timer_id": "tea-timer", "remaining_seconds": 3})
        self.assertEqual(self.window.countdown_status.text(),
                         "Tea started: 3 seconds remaining.")
        process.push({"type": "timer", "event": "completed",
                      "timer_id": "tea-timer", "remaining_seconds": 0})
        self.assertIn("Tea finished", self.window.countdown_status.text())

        self.window.countdown_checkbox.setChecked(False)
        self.assertEqual(process.writes[-1], b'{"command":"stop"}\n')
        self.assertTrue(process.closed_input)
        process.push(
            {"type": "stopped"},
            {"type": "result", "outcome": "stopped", "verified": True,
             "display_may_be_stale": False},
        )
        process.finish()
        self.assertIsNone(self.window._countdown_process)
        self.assertIn("Stopped by the user", self.window.countdown_status.text())
        self.assertIsNotNone(self.window.snapshot)

    def test_repeated_stop_request_writes_one_terminal_command(self):
        process, _ = self.start_listener()
        process.push({"type": "ready", "timers": 1, "positions": 1})

        self.window._request_countdown_stop("First reason.")
        self.window._request_countdown_stop("Final reason.")

        self.assertEqual(process.writes.count(b'{"command":"stop"}\n'), 1)
        process.push(
            {"type": "stopped"},
            {"type": "result", "outcome": "stopped", "verified": True,
             "display_may_be_stale": False},
        )
        process.finish()
        self.assertIn("Final reason", self.window.countdown_status.text())

    def test_local_edit_stops_listener_and_helper_error_invalidates_read(self):
        process, _ = self.start_listener()
        process.push({"type": "ready", "timers": 1, "positions": 1})
        self.window._changed()
        self.assertEqual(process.writes[-1], b'{"command":"stop"}\n')
        self.assertIn("Stopping", self.window.countdown_status.text())
        process.push({
            "type": "result", "outcome": "error", "verified": False,
            "display_may_be_stale": True,
            "error": "The LCD layout changed; read it again",
        })
        process.finish(exit_code=2)
        self.assertIsNone(self.window.snapshot)
        self.assertIn("LCD layout changed", self.window.countdown_status.text())

    def test_replacing_local_draft_stops_persistent_listener(self):
        process, _ = self.start_listener()
        process.push({"type": "ready", "timers": 1, "positions": 1})

        self.window.new_draft()

        self.assertEqual(process.writes.count(b'{"command":"stop"}\n'), 1)
        self.assertEqual(self.window.draft.countdown_timers, [])
        self.assertIn("Stopping", self.window.countdown_status.text())

    def test_invalid_status_and_following_output_preserve_first_failure(self):
        process, _ = self.start_listener()
        process.push_raw(
            b'{"type":"ready","timers":2,"positions":1}\n'
            b'{"type":"ready","timers":1,"positions":1}\n')

        self.assertEqual(process.kill_count, 1)
        self.assertIsNone(self.window._countdown_process)
        self.assertIsNone(self.window.snapshot)
        self.assertIn("invalid ready message",
                      self.window.countdown_status.text())

    def test_timer_status_must_match_requested_id_duration_and_transitions(self):
        invalid_messages = [
            {"type": "timer", "event": "started", "timer_id": "other",
             "remaining_seconds": 3},
            {"type": "timer", "event": "started", "timer_id": "tea-timer",
             "remaining_seconds": 2},
            {"type": "timer", "event": "tick", "timer_id": "tea-timer",
             "remaining_seconds": 2},
        ]
        for message in invalid_messages:
            with self.subTest(message=message):
                self.restore_fixture()
                process, _ = self.start_listener()
                process.push({"type": "ready", "timers": 1, "positions": 1})
                process.push(message)
                self.assertEqual(process.kill_count, 1)
                self.assertIsNone(self.window.snapshot)
                self.assertIn("invalid timer", self.window.countdown_status.text())

    def test_verified_result_requires_stopped_message_and_clean_exit(self):
        process, _ = self.start_listener()
        process.push({"type": "ready", "timers": 1, "positions": 1})
        self.window.countdown_checkbox.setChecked(False)
        process.push({
            "type": "result", "outcome": "stopped", "verified": True,
            "display_may_be_stale": False,
        })
        self.assertEqual(process.kill_count, 1)
        self.assertIsNone(self.window.snapshot)

        self.restore_fixture()
        process, _ = self.start_listener()
        process.push({"type": "ready", "timers": 1, "positions": 1})
        self.window.countdown_checkbox.setChecked(False)
        process.push(
            {"type": "stopped"},
            {"type": "result", "outcome": "stopped", "verified": True,
             "display_may_be_stale": False},
        )
        process.finish(exit_code=9,
                       exit_status=QProcess.ExitStatus.CrashExit)
        self.assertIsNone(self.window.snapshot)
        self.assertIn("did not exit cleanly", self.window.countdown_status.text())

    def test_stale_process_signals_cannot_mutate_replacement_session(self):
        first, _ = self.start_listener()
        first.push({"type": "ready", "timers": 1, "positions": 1})
        self.window.countdown_checkbox.setChecked(False)
        first.push(
            {"type": "stopped"},
            {"type": "result", "outcome": "stopped", "verified": True,
             "display_may_be_stale": False},
        )
        first.finish()
        second, _ = self.start_listener()

        first.push_raw(b'{"type":"unknown"}\n')
        first.errorOccurred.emit(QProcess.ProcessError.Crashed)
        first.finished.emit(9, QProcess.ExitStatus.CrashExit)

        self.assertIs(self.window._countdown_process, second)
        self.assertIsNotNone(self.window.snapshot)
        self.assertEqual(second.kill_count, 0)

    def test_background_dialog_is_interlocked_while_helper_owns_usb(self):
        self.start_listener()
        with patch("swarm2.gui.app.BackgroundDialog") as dialog:
            self.window.choose_background()
        dialog.assert_not_called()

    def test_close_to_tray_keeps_listener_and_explicit_quit_stops_it(self):
        process, _ = self.start_listener()
        process.push({"type": "ready", "timers": 1, "positions": 1})
        self.window.battery_tray.enabled = True
        self.window.keep_running_checkbox.blockSignals(True)
        self.window.keep_running_checkbox.setChecked(True)
        self.window.keep_running_checkbox.blockSignals(False)

        hidden_event = QCloseEvent()
        with patch.object(self.window, "_sync_tray_options"):
            self.window.closeEvent(hidden_event)
        self.assertFalse(hidden_event.isAccepted())
        self.assertTrue(self.window._hidden_to_tray)
        self.assertIs(self.window._countdown_process, process)
        self.assertFalse(self.window._countdown_stop_requested)

        self.window._tray_quit_requested = True
        quit_event = QCloseEvent()
        with patch.object(self.window, "_sync_tray_options"):
            self.window.closeEvent(quit_event)
        self.window._tray_quit_requested = False
        self.assertFalse(quit_event.isAccepted())
        self.assertTrue(self.window._countdown_close_pending)
        self.assertTrue(self.window._countdown_quit_pending)
        self.assertFalse(self.window.centralWidget().isEnabled())
        self.assertEqual(process.writes.count(b'{"command":"stop"}\n'), 1)

        process.push(
            {"type": "stopped"},
            {"type": "result", "outcome": "stopped", "verified": True,
             "display_may_be_stale": False},
        )
        process.finish()
        final_event = QCloseEvent()
        scheduled = []
        with (patch.object(self.window, "_sync_tray_options"),
              patch("swarm2.gui.app.QTimer.singleShot",
                    side_effect=lambda delay, callback:
                    scheduled.append((delay, callback)))):
            self.window.closeEvent(final_event)
        self.assertTrue(final_event.isAccepted())
        self.assertFalse(self.window._countdown_close_pending)
        self.assertFalse(self.window._countdown_quit_pending)
        self.assertEqual([delay for delay, _ in scheduled], [0])

    def test_unapplied_or_unbound_timer_layout_cannot_start(self):
        self.window.draft.display.pages[0][0] = "dpi"
        self.window._update_capabilities()
        self.assertFalse(self.window.countdown_checkbox.isEnabled())
        _, error = self.window._countdown_start_request()
        self.assertIn("Apply the current LCD layout", error)

        self.window.draft.display.pages[0][0] = "countdown"
        self.window.draft.display.timer_bindings[0][0] = None
        self.window._update_capabilities()
        self.assertFalse(self.window.countdown_checkbox.isEnabled())
        _, error = self.window._countdown_start_request()
        self.assertIn("Assign every", error)


if __name__ == "__main__":
    unittest.main()
