"""Static countdown timer model, LCD layout and editor integration.

These tests use byte fixtures and an offscreen Qt window. They never open a
USB device and deliberately do not exercise the future live timer runtime.
"""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from swarm2.configuration import (
    Configuration, ConfigurationError, CountdownTimer, PresetStore,
)
from swarm2.firmware_backup import merge_supported_profile, read_backup
from swarm2.lcd_commands import (
    LCD_WIDGETS, build_lcd_report, decode_lcd_response,
)
from swarm2.service import DeviceService
from tests.test_firmware_backup import fixture_backup, set_lcd_page
from tests.test_lcd_commands import CAPTURE as LCD_CAPTURE
from tests.test_settings import CAPTURES, with_lcd_page

try:
    from PySide6.QtWidgets import QApplication
    from swarm2.gui.app import MainWindow
    from tests.test_gui import FakeService
except ModuleNotFoundError as error:
    if error.name and error.name.startswith("PySide6"):
        QApplication = MainWindow = FakeService = None
    else:
        raise


class CountdownConfigurationTests(unittest.TestCase):
    def test_new_timer_uses_native_editor_minimum(self):
        self.assertEqual(CountdownTimer().duration_seconds, 1)

    def test_library_and_shared_coordinate_bindings_round_trip(self):
        timer = CountdownTimer(id="tea-timer", name="Tea", duration_seconds=180)
        configuration = Configuration(countdown_timers=[timer])
        configuration.display.pages = [
            ["countdown", "countdown", "dpi", "empty"],
            ["cut", "copy", "paste", "undo"],
        ]
        configuration.display.timer_bindings = [
            [timer.id, timer.id, None, None],
            [None, None, None, None],
        ]

        restored = Configuration.from_dict(configuration.to_dict())

        self.assertEqual(restored, configuration)
        self.assertEqual(restored.display.timer_bindings[0][:2],
                         [timer.id, timer.id])

    def test_schema_one_defaults_and_timer_validation_are_strict(self):
        older = Configuration().to_dict()
        older.pop("countdown_timers")
        older["display"].pop("timer_bindings")
        restored = Configuration.from_dict(older)
        self.assertEqual(restored.countdown_timers, [])
        self.assertEqual(restored.display.timer_bindings, [])

        base = Configuration(
            countdown_timers=[CountdownTimer(id="timer-1", name="Timer")])
        base.display.pages = [["countdown", "dpi", "empty", "empty"]]
        base.display.timer_bindings = [["timer-1", None, None, None]]
        for duration in (0, 601, True, 1.5):
            data = base.to_dict()
            data["countdown_timers"][0]["duration_seconds"] = duration
            with self.subTest(duration=duration), self.assertRaises(ConfigurationError):
                Configuration.from_dict(data)

        wrong_widget = base.to_dict()
        wrong_widget["display"]["timer_bindings"][0][1] = "timer-1"
        missing_timer = base.to_dict()
        missing_timer["display"]["timer_bindings"][0][0] = "missing"
        bad_id = base.to_dict()
        bad_id["countdown_timers"][0]["id"] = "ambiguous_id"
        for data in (wrong_widget, missing_timer, bad_id):
            with self.assertRaises(ConfigurationError):
                Configuration.from_dict(data)


class CountdownLcdTests(unittest.TestCase):
    def test_countdown_tile_has_confirmed_46_00_wire_signature(self):
        self.assertEqual(LCD_WIDGETS["countdown"].signature, (0x46, 0x00))
        state = decode_lcd_response(LCD_CAPTURE, 0)
        report = build_lcd_report(
            state, pages={0: ["countdown", "dpi", "empty", "empty"]})
        self.assertEqual(report[8:16], bytes.fromhex("fe00fe0064004600"))
        response = bytearray(report[:61])
        response[2] = 0
        decoded = decode_lcd_response(response, 0)
        self.assertEqual(
            [widget.key for widget in decoded.pages[0].slots],
            ["countdown", "dpi", "empty", "empty"],
        )

    def test_snapshot_preserves_only_matching_host_timer_coordinates(self):
        timer = CountdownTimer(id="coffee", name="Coffee", duration_seconds=240)
        draft = Configuration(countdown_timers=[timer])
        draft.display.pages = [["countdown", "dpi", "empty", "empty"]]
        draft.display.timer_bindings = [[timer.id, None, None, None]]
        lcd = with_lcd_page(
            bytes.fromhex(CAPTURES["lcd"]), 0,
            (b"\x46\x00", b"\x64\x00", b"\xfe\x00", b"\xfe\x00"),
        )
        result = {
            "raw": CAPTURES["sensor"], "changed": False,
            "settings": {"sensor": CAPTURES["sensor"], "lcd": lcd.hex()},
        }

        matching = DeviceService._snapshot("fixture", 1, result, draft)
        self.assertEqual(
            matching["configuration"].display.timer_bindings[0][0], timer.id)

        changed = copy.deepcopy(result)
        changed_lcd = with_lcd_page(
            lcd, 0,
            (b"\x64\x00", b"\x46\x00", b"\xfe\x00", b"\xfe\x00"),
        )
        changed["settings"]["lcd"] = changed_lcd.hex()
        moved = DeviceService._snapshot("fixture", 1, changed, draft)
        self.assertEqual(moved["configuration"].display.timer_bindings[0],
                         [None, None, None, None])


class CountdownFirmwareMergeTests(unittest.TestCase):
    def test_raw_backup_does_not_invent_or_move_host_timer_data(self):
        value = fixture_backup()
        source_page = ["countdown", "dpi", "empty", "empty"]
        set_lcd_page(value["profiles"][0], 0, 0, source_page)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backup.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            profile = read_backup(path).profiles[0]

        self.assertEqual(profile.configuration.countdown_timers, [])
        self.assertTrue(all(binding is None
                            for row in profile.configuration.display.timer_bindings
                            for binding in row))
        current_bundle = fixture_backup()["profiles"][0]
        current = DeviceService._snapshot(
            "fixture-port", 1, copy.deepcopy(current_bundle))
        current_page = copy.deepcopy(current["configuration"].display.pages[0])

        plan = merge_supported_profile(profile, current)

        self.assertEqual(plan.configuration.display.pages[0], current_page)
        self.assertEqual(plan.configuration.countdown_timers, [])
        self.assertFalse(any(binding for row in plan.configuration.display.timer_bindings
                             for binding in row))
        self.assertTrue(any("host-only data" in warning for warning in plan.warnings))


@unittest.skipIf(MainWindow is None, "Install the gui extra to exercise Qt")
class CountdownGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.window = MainWindow(
            service=FakeService(),
            store=PresetStore(Path(self.directory.name) / "presets"),
            auto_discover=False,
        )
        self.window.draft.display.pages = [
            ["countdown", "dpi", "empty", "empty"],
            ["cut", "copy", "paste", "undo"],
        ]
        self.window._load_draft()

    def tearDown(self):
        self.window.dirty = False
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.directory.cleanup()

    def test_editor_assigns_renames_moves_and_guards_deletion(self):
        self.window._new_countdown_timer()
        timer = self.window.draft.countdown_timers[0]
        self.window.countdown_timer_name.setText("Coffee")
        self.window.countdown_timer_duration.setValue(240)
        self.window._edit_countdown_timer()
        self.assertEqual((timer.name, timer.duration_seconds), ("Coffee", 240))

        self.window._lcd_timer_changed(0, 0, timer.id)
        self.assertEqual(self.window.draft.display.timer_bindings[0][0], timer.id)
        self.window._delete_countdown_timer()
        self.assertEqual(self.window.draft.countdown_timers, [timer])
        self.assertIn("assigned to an LCD tile", self.window.message.text())

        self.window.lcd_tabs.setCurrentIndex(0)
        self.window._move_lcd_page(1)
        self.assertEqual(self.window.draft.display.pages[1][0], "countdown")
        self.assertEqual(self.window.draft.display.timer_bindings[1][0], timer.id)

        self.window._lcd_slot_changed(1, 0, "empty")
        self.assertIsNone(self.window.draft.display.timer_bindings[1][0])
        self.window._delete_countdown_timer()
        self.assertEqual(self.window.draft.countdown_timers, [])
        self.window.draft.validate()

    def test_hardware_page_merge_keeps_only_still_matching_assignment(self):
        timer = CountdownTimer(id="tea", name="Tea", duration_seconds=180)
        self.window.draft.countdown_timers = [timer]
        self.window.draft.display.timer_bindings = [
            [timer.id, None, None, None], [None] * 4]
        self.window._load_draft()
        snapshot = self.window.service.snapshot()
        snapshot["configuration"].display.pages = copy.deepcopy(
            self.window.draft.display.pages)
        snapshot["verified_fields"] = ["display.pages"]
        self.window._merge_verified(snapshot)
        self.assertEqual(self.window.draft.display.timer_bindings[0][0], timer.id)

        changed = self.window.service.snapshot()
        changed["configuration"].display.pages = [
            ["dpi", "countdown", "empty", "empty"],
            ["cut", "copy", "paste", "undo"],
        ]
        changed["verified_fields"] = ["display.pages"]
        self.window._merge_verified(changed)
        self.assertEqual(self.window.draft.display.timer_bindings[0], [None] * 4)
        self.window.draft.validate()


if __name__ == "__main__":
    unittest.main()
