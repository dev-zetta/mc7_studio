"""Offline GUI coverage for Open Application targets and custom icons."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    from swarm2.gui.app import MainWindow
    from swarm2.gui.custom_icon import (
        ICON_WIRE_RGBA_BYTES, PreparedCustomIcon, preview_from_wire_rgba,
    )
except ModuleNotFoundError as error:
    if error.name and error.name.startswith("PySide6"):
        QApplication = MainWindow = None
    else:
        raise

from swarm2.configuration import (
    Configuration, ConfigurationError, PresetStore,
    encode_host_action_icon_rgba,
    host_action_icon_rgba,
)
from swarm2.host_actions import encode_host_action_record
from swarm2.lcd_commands import LCD_WIDGETS, decode_lcd_response
from tests.test_gui import FakeService
from tests.test_settings import (
    CAPTURES, screen_key_capture, with_lcd_page, with_screen_key_records,
)


@unittest.skipIf(MainWindow is None, "Install the gui extra to exercise Qt")
class GuiOpenApplicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        repo_tmp = Path(__file__).resolve().parents[1] / "tmp"
        repo_tmp.mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=repo_tmp)
        self.root = Path(self.directory.name)
        self.service = FakeService()
        self.window = MainWindow(
            service=self.service,
            store=PresetStore(self.root / "presets"),
            auto_discover=False,
        )
        self.window.draft.display.pages = [
            ["empty"] * 4, ["cut", "copy", "paste", "undo"],
        ]
        self.window._load_draft()
        self.application = self.root / "Example.AppImage"
        self.application.write_bytes(b"#!/bin/sh\nexit 0\n")
        self.application.chmod(0o700)

    def tearDown(self):
        self.window.dirty = False
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.directory.cleanup()

    @staticmethod
    def prepared(filename, component):
        rgba = bytes((component, component + 1, component + 2, 255)) * (
            ICON_WIRE_RGBA_BYTES // 4)
        return PreparedCustomIcon(
            os.fspath(filename), preview_from_wire_rgba(rgba), rgba)

    def select_application_tile(self):
        tile = self.window.lcd_controls[0][0]
        self.assertGreaterEqual(tile.findData("open_application"), 0)
        tile.setCurrentIndex(tile.findData("open_application"))
        return tile

    def choose_application(self, prepared):
        with (patch("swarm2.gui.app.QFileDialog.getOpenFileName",
                    return_value=(os.fspath(self.application), "")) as choose,
              patch("swarm2.gui.app.prepare_application_icon",
                    return_value=prepared) as extract):
            self.window.lcd_host_action_browse_buttons[0][0].click()
        choose.assert_called_once()
        extract.assert_called_once_with(self.application)

    def test_choose_application_extracts_icon_and_custom_image_can_replace_it(self):
        self.select_application_tile()
        editor = self.window.lcd_host_action_editors[0][0]
        target_button = self.window.lcd_host_action_browse_buttons[0][0]
        preview = self.window.lcd_host_action_icon_previews[0][0]
        state = self.window.lcd_host_action_icon_states[0][0]
        auto_button = self.window.lcd_host_action_icon_auto_buttons[0][0]
        custom_button = self.window.lcd_host_action_icon_browse_buttons[0][0]
        clear_button = self.window.lcd_host_action_icon_clear_buttons[0][0]

        self.assertEqual(target_button.text(), "Choose app…")
        self.assertFalse(target_button.isHidden())
        self.assertFalse(preview.isHidden())
        self.assertEqual(state.text(), "Choose an application")
        self.assertFalse(auto_button.isEnabled())
        self.assertFalse(custom_button.isEnabled())

        system_icon = self.prepared(self.application, 10)
        self.choose_application(system_icon)
        self.assertEqual(editor.text(), os.fspath(self.application))
        self.assertEqual(
            host_action_icon_rgba(
                self.window.draft.display.host_action_icon_bindings[0][0]),
            system_icon.rgba,
        )
        self.assertEqual(state.text(), "Icon ready")
        self.assertFalse(preview.pixmap().isNull())
        self.assertTrue(auto_button.isEnabled())
        self.assertTrue(custom_button.isEnabled())
        self.assertTrue(clear_button.isEnabled())

        image = self.root / "override.png"
        image.write_bytes(b"test fixture; decoder is mocked")
        custom_icon = self.prepared(image, 40)
        with (patch("swarm2.gui.app.QFileDialog.getOpenFileName",
                    return_value=(os.fspath(image), "")) as choose,
              patch("swarm2.gui.app.prepare_custom_icon",
                    return_value=custom_icon) as prepare):
            custom_button.click()
        choose.assert_called_once()
        prepare.assert_called_once_with(os.fspath(image))
        self.assertEqual(
            host_action_icon_rgba(
                self.window.draft.display.host_action_icon_bindings[0][0]),
            custom_icon.rgba,
        )
        self.window.draft.validate()

        clear_button.click()
        self.assertIsNone(
            self.window.draft.display.host_action_icon_bindings[0][0])
        self.assertEqual(state.text(), "Icon required")
        self.assertEqual(preview.text(), "No icon")
        with self.assertRaisesRegex(ConfigurationError, "icon.*required"):
            self.window.draft.validate()

        with patch("swarm2.gui.app.prepare_application_icon",
                   return_value=system_icon) as extract:
            auto_button.click()
        extract.assert_called_once_with(self.application)
        self.window.draft.validate()

    def test_target_edit_clears_stale_icon_and_page_move_keeps_parallel_binding(self):
        self.select_application_tile()
        icon = self.prepared(self.application, 70)
        self.choose_application(icon)

        self.window.lcd_tabs.setCurrentIndex(0)
        self.assertTrue(self.window.lcd_move_right.isEnabled())
        self.window._move_lcd_page(1)
        encoded = encode_host_action_icon_rgba(icon.rgba)
        self.assertEqual(
            self.window.draft.display.pages[1][0], "open_application")
        self.assertEqual(
            self.window.draft.display.host_action_bindings[1][0],
            os.fspath(self.application),
        )
        self.assertEqual(
            self.window.draft.display.host_action_icon_bindings,
            [[None] * 4, [encoded, None, None, None]],
        )

        replacement = self.root / "Replacement.AppImage"
        replacement.write_bytes(b"#!/bin/sh\nexit 0\n")
        replacement.chmod(0o700)
        editor = self.window.lcd_host_action_editors[1][0]
        editor.setText(os.fspath(replacement))
        self.assertEqual(
            self.window.draft.display.host_action_bindings[1][0],
            os.fspath(replacement),
        )
        self.assertIsNone(
            self.window.draft.display.host_action_icon_bindings[1][0])
        self.assertEqual(
            self.window.lcd_host_action_icon_states[1][0].text(),
            "Icon required",
        )

        tile = self.window.lcd_controls[1][0]
        tile.setCurrentIndex(tile.findData("dpi"))
        self.assertIsNone(
            self.window.draft.display.host_action_bindings[1][0])
        self.assertIsNone(
            self.window.draft.display.host_action_icon_bindings[1][0])

    def test_invalid_application_and_custom_icon_leave_existing_binding_unchanged(self):
        self.select_application_tile()
        icon = self.prepared(self.application, 100)
        self.choose_application(icon)
        expected_target = os.fspath(self.application)
        expected_icon = encode_host_action_icon_rgba(icon.rgba)

        not_executable = self.root / "NotExecutable"
        not_executable.write_text("plain file")
        with patch("swarm2.gui.app.os.access", return_value=False), patch("swarm2.gui.app.QFileDialog.getOpenFileName",
                   return_value=(os.fspath(not_executable), "")):
            self.window.lcd_host_action_browse_buttons[0][0].click()
        self.assertIn("not executable", self.window.message.text())
        self.assertEqual(
            self.window.draft.display.host_action_bindings[0][0],
            expected_target,
        )
        self.assertEqual(
            self.window.draft.display.host_action_icon_bindings[0][0],
            expected_icon,
        )

        bad_image = self.root / "broken.png"
        bad_image.write_bytes(b"not an image")
        with (patch("swarm2.gui.app.QFileDialog.getOpenFileName",
                    return_value=(os.fspath(bad_image), "")),
              patch("swarm2.gui.app.prepare_custom_icon",
                    side_effect=ValueError("Choose a PNG or JPEG image."))):
            self.window.lcd_host_action_icon_browse_buttons[0][0].click()
        self.assertIn("PNG or JPEG", self.window.message.text())
        self.assertEqual(
            self.window.draft.display.host_action_bindings[0][0],
            expected_target,
        )
        self.assertEqual(
            self.window.draft.display.host_action_icon_bindings[0][0],
            expected_icon,
        )

    def test_listener_request_uses_icon_reference_from_fresh_mouse_record(self):
        icon = self.prepared(self.application, 130)
        lcd = with_lcd_page(
            bytes.fromhex(CAPTURES["lcd"]), 0,
            (bytes(LCD_WIDGETS["open_application"].signature),
             b"\xfe\x00", b"\xfe\x00", b"\xfe\x00"),
        )
        lcd_state = decode_lcd_response(lcd, 0)
        pages = [
            [None if widget is None else widget.key for widget in page.slots]
            for page in lcd_state.pages[:lcd_state.page_count]
        ]
        configuration = Configuration()
        configuration.display.pages = pages
        configuration.display.host_action_bindings = [
            [None] * 4 for _ in pages]
        configuration.display.host_action_bindings[0][0] = os.fspath(
            self.application)
        configuration.display.host_action_icon_bindings = [
            [None] * 4 for _ in pages]
        configuration.display.host_action_icon_bindings[0][0] = (
            encode_host_action_icon_rgba(icon.rgba))
        records = (
            encode_host_action_record(
                "open_application", os.fspath(self.application),
                icon_index=7),
            bytes(11), bytes(11), bytes(11),
        )
        screen_keys = with_screen_key_records(
            screen_key_capture(), 0, records)
        self.window.draft = configuration
        self.window.devices = [{
            "id": "test-mc7", "capabilities": ["countdown"],
        }]
        self.window.selected_device_id = "test-mc7"
        self.window.snapshot = {
            "device_id": "test-mc7", "profile_slot": 1,
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

        request, error = self.window._countdown_start_request()

        self.assertEqual(error, "")
        self.assertEqual(request["host_action_bindings"], [{
            "widget_key": "open_application", "page_index": 0,
            "slot_index": 0, "target": os.fspath(self.application),
            "icon_index": 7,
        }])


if __name__ == "__main__":
    unittest.main()
