"""Main-window calibration, page ordering and background merge boundaries.

Uses the shared fake service; never constructs or opens a hardware transport.
"""

import copy
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from tests import test_gui as gui_fixture
from swarm2.configuration import Action, PresetStore


class BatchService(gui_fixture.FakeService):
    def __init__(self):
        super().__init__()
        self.capabilities.append("upload_background")
        self.verified_fields += ["sensor.angle_tuning", "sensor.angle_tuning_enabled",
                                 "sensor.angle_snapping", "display.background_index"]
        self.configuration.display.background_index = 0
        self.active_profile = 1
        self.background_result = {"acknowledged": True, "selection_verified": True,
                                  "selection_after": "102c0001aabbccdd"}

    def snapshot(self, slot=1):
        result = super().snapshot(slot)
        result["summary"]["active_profile"] = self.active_profile
        result["baseline"] = {"device_id": "test-mc7", "profile_slot": slot,
            "settings": {"background": "102c000000000000", "sensor": "retained-sensor"}}
        return result

    def upload_background(self, device_id, rgba):
        super().upload_background(device_id, rgba)
        return copy.deepcopy(self.background_result)


@unittest.skipIf(gui_fixture.MainWindow is None, "Install the gui extra to exercise Qt")
class GuiBatchThreeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = gui_fixture.QApplication.instance() or gui_fixture.QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = BatchService()
        self.window = gui_fixture.MainWindow(service=self.service,
            store=PresetStore(Path(self.directory.name)/"presets"), auto_discover=False)

    def tearDown(self):
        self.wait_for_job()
        self.window.dirty = False
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.directory.cleanup()

    def wait_for_job(self):
        deadline = time.monotonic()+3
        while self.window._job is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.app.processEvents()
        self.assertIsNone(self.window._job, "Fake device job did not finish")

    def read(self):
        self.window.navigation.setCurrentRow(1)
        self.window.refresh_device()
        self.wait_for_job()
        self.window.read_mouse()
        self.wait_for_job()

    def upload(self):
        with patch("swarm2.gui.app.BackgroundDialog") as factory:
            factory.return_value.exec.return_value = gui_fixture.QDialog.DialogCode.Accepted
            factory.return_value.prepared.rgba = bytes(86336)
            self.window.choose_background()
        self.wait_for_job()

    def test_dpi_calibration_uses_read_value_and_changes_only_actual_stage_in_draft(self):
        self.read()
        self.window.draft.sensor.stages[0].value = 1200
        self.window.draft.sensor.stages[1].value = 1350
        self.window.draft.sensor.polling_rate = 8000
        self.window.draft.lighting.brightness = 37
        self.window.draft.buttons[5].primary = Action("media", "mute")
        before = self.window.draft.to_dict()
        baseline = copy.deepcopy(self.window.snapshot)
        calls = copy.deepcopy(self.service.calls)
        with patch("swarm2.gui.app.DpiCalibrationDialog") as factory:
            factory.return_value.exec.return_value = gui_fixture.QDialog.DialogCode.Accepted
            factory.return_value.suggested_dpi = 950
            self.window.calibrate_dpi()
            factory.assert_called_once_with(self.window, current_dpi=650, settings_verified=True)
        expected = copy.deepcopy(before)
        expected["sensor"]["stages"][0]["value"] = 950
        self.assertEqual(self.window.draft.to_dict(), expected)
        self.assertEqual(self.window.snapshot, baseline)
        self.assertEqual(self.service.calls, calls)
        self.assertTrue(self.window.dirty)
        self.assertIn("Apply sensitivity", self.window.message.text())

    def test_angle_calibration_uses_verified_sensor_and_updates_only_angle_and_enable(self):
        self.service.configuration.sensor.angle_tuning = 7
        self.service.configuration.sensor.angle_tuning_enabled = True
        self.service.configuration.sensor.angle_snapping = True
        self.read()
        self.window.draft.sensor.angle_tuning = 0
        self.window.draft.sensor.angle_tuning_enabled = False
        self.window.draft.sensor.angle_snapping = False
        self.window.draft.power.eco_mode = True
        before = self.window.draft.to_dict()
        baseline = copy.deepcopy(self.window.snapshot)
        calls = copy.deepcopy(self.service.calls)
        with patch("swarm2.gui.app.AngleCalibrationDialog") as factory:
            factory.return_value.exec.return_value = gui_fixture.QDialog.DialogCode.Accepted
            factory.return_value.suggested_angle = -8
            self.window.calibrate_angle()
            # A real dialog blocks a rotated/snapped read. Unsaved draft
            # values must not accidentally bypass that prerequisite.
            factory.assert_called_once_with(self.window, current_angle=7,
                angle_enabled=True, angle_snapping=True, settings_verified=True)
        expected = copy.deepcopy(before)
        expected["sensor"].update(angle_tuning=-8, angle_tuning_enabled=True)
        self.assertEqual(self.window.draft.to_dict(), expected)
        self.assertEqual(self.window.snapshot, baseline)
        self.assertEqual(self.service.calls, calls)

    def test_cancel_or_missing_suggestion_keeps_every_draft_field_and_makes_no_device_call(self):
        self.read()
        self.window.dirty = False
        before = self.window.draft.to_dict()
        calls = copy.deepcopy(self.service.calls)
        for name, method, attribute in (("DpiCalibrationDialog", "calibrate_dpi", "suggested_dpi"),
                                        ("AngleCalibrationDialog", "calibrate_angle", "suggested_angle")):
            for accepted, suggested in ((False, 1000), (True, None)):
                with self.subTest(dialog=name, accepted=accepted):
                    with patch("swarm2.gui.app."+name) as factory:
                        factory.return_value.exec.return_value = (gui_fixture.QDialog.DialogCode.Accepted
                            if accepted else gui_fixture.QDialog.DialogCode.Rejected)
                        setattr(factory.return_value, attribute, suggested)
                        getattr(self.window, method)()
                    self.assertEqual(self.window.draft.to_dict(), before)
                    self.assertFalse(self.window.dirty)
                    self.assertEqual(self.service.calls, calls)

    def test_calibration_requires_matching_device_profile_active_profile_and_verified_fields(self):
        self.read()
        valid = copy.deepcopy(self.window.snapshot)
        cases = [(None, None), ("device_id", "different-mouse"), ("profile_slot", 2),
                 ("active_profile", 2), ("active_profile", None), ("verified_fields", [])]
        calls = copy.deepcopy(self.service.calls)
        before = self.window.draft.to_dict()
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.window.snapshot = copy.deepcopy(valid)
                if field is None:
                    self.window.snapshot = None
                elif field == "active_profile":
                    self.window.snapshot["summary"][field] = value
                else:
                    self.window.snapshot[field] = value
                self.window._update_capabilities()
                self.assertFalse(self.window.dpi_calibration_button.isEnabled())
                self.assertFalse(self.window.angle_calibration_button.isEnabled())
                with patch("swarm2.gui.app.DpiCalibrationDialog") as dpi, patch("swarm2.gui.app.AngleCalibrationDialog") as angle:
                    self.window.calibrate_dpi()
                    self.window.calibrate_angle()
                    dpi.assert_not_called()
                    angle.assert_not_called()
                self.assertEqual(self.window.draft.to_dict(), before)
                self.assertEqual(self.service.calls, calls)

    def test_dpi_requires_actual_read_stage_and_accepts_serialized_snapshot_configuration(self):
        self.read()
        self.window.snapshot["configuration"] = self.window.snapshot["configuration"].to_dict()
        self.window.current_stage.setCurrentIndex(1)
        self.assertFalse(self.window.dpi_calibration_button.isEnabled())
        self.assertTrue(self.window.angle_calibration_button.isEnabled())
        with patch("swarm2.gui.app.DpiCalibrationDialog") as factory:
            self.window.calibrate_dpi()
            factory.assert_not_called()
        self.window.current_stage.setCurrentIndex(0)
        self.assertTrue(self.window.dpi_calibration_button.isEnabled())
        with patch("swarm2.gui.app.DpiCalibrationDialog") as factory:
            factory.return_value.exec.return_value = gui_fixture.QDialog.DialogCode.Rejected
            self.window.calibrate_dpi()
            factory.assert_called_once_with(self.window, current_dpi=650, settings_verified=True)

    def test_page_move_preserves_wide_widgets_pending_settings_and_device_baseline(self):
        self.read()
        original_pages = [["system_media", None, None, "dpi"],
                          ["next_track", "play_pause", "led_brightness", "stop"],
                          ["empty", "download_swarm", None, None]]
        self.window.draft.display.pages = copy.deepcopy(original_pages)
        self.window.draft.lighting.brightness = 37
        self.window._load_draft()
        baseline = copy.deepcopy(self.window.snapshot)
        calls = copy.deepcopy(self.service.calls)
        self.window.lcd_tabs.setCurrentIndex(0)
        self.assertFalse(self.window.lcd_move_left.isEnabled())
        self.assertTrue(self.window.lcd_move_right.isEnabled())
        self.window._move_lcd_page(1)
        self.assertEqual(self.window.draft.display.pages,
                         [original_pages[1], original_pages[0], original_pages[2]])
        self.assertEqual(self.window.lcd_tabs.currentIndex(), 1)
        self.assertFalse(self.window.lcd_controls[1][1].isEnabled())
        self.assertFalse(self.window.lcd_controls[1][2].isEnabled())
        self.assertEqual(self.window.lcd_controls[1][0].currentData(), "system_media")
        self.assertEqual(self.window.draft.lighting.brightness, 37)
        self.assertEqual(self.window.snapshot, baseline)
        self.assertEqual(self.service.calls, calls)
        self.window.draft.validate()
        self.assertTrue(self.window.dirty)

    def test_page_move_keeps_key_bindings_with_their_tiles(self):
        self.window.draft.display.pages = [
            ["remap_key", "copy", "paste", "undo"],
            ["hotkey", "dpi", "led_brightness", "play_pause"],
        ]
        self.window.draft.display.key_bindings = [
            ["F5", None, None, None],
            ["Ctrl+Shift+S", None, None, None],
        ]
        self.window._load_draft()
        self.window.lcd_tabs.setCurrentIndex(0)
        self.window._move_lcd_page(1)
        self.assertEqual(self.window.draft.display.pages[0][0], "hotkey")
        self.assertEqual(self.window.draft.display.key_bindings, [
            ["Ctrl+Shift+S", None, None, None],
            ["F5", None, None, None],
        ])
        self.assertEqual(self.window.lcd_key_controls[0][0].text(), "Ctrl+Shift+S")
        self.assertEqual(self.window.lcd_key_controls[1][0].text(), "F5")
        self.window.draft.validate()

    def test_page_move_keeps_macro_bindings_with_their_tiles(self):
        macro = gui_fixture.Macro(id="lcd_macro", name="LCD macro")
        self.window.draft.macros = [macro]
        self.window.draft.display.pages = [
            ["macro", "copy", "paste", "undo"],
            ["dpi", "led_brightness", "play_pause", "stop"],
        ]
        self.window.draft.display.macro_bindings = [
            [macro.id, None, None, None],
            [None, None, None, None],
        ]
        self.window._load_draft()
        self.window.lcd_tabs.setCurrentIndex(0)
        self.window._move_lcd_page(1)
        self.assertEqual(self.window.draft.display.pages[1][0], "macro")
        self.assertEqual(self.window.draft.display.macro_bindings, [
            [None, None, None, None],
            [macro.id, None, None, None],
        ])
        self.assertEqual(self.window.lcd_macro_controls[1][0].currentData(), macro.id)
        self.window.draft.validate()

    def test_page_move_refuses_unreadable_lcd_macro(self):
        self.window.draft.display.pages = [
            ["macro", "copy", "paste", "undo"],
            ["dpi", "led_brightness", "play_pause", "stop"],
        ]
        self.window.draft.display.macro_bindings = [[None] * 4, [None] * 4]
        self.window._load_draft()
        self.window.dirty = False
        before = self.window.draft.to_dict()
        self.window.lcd_tabs.setCurrentIndex(0)

        self.assertFalse(self.window.lcd_move_right.isEnabled())
        self.assertIn("unreadable LCD macro", self.window.lcd_move_right.toolTip())
        self.window._move_lcd_page(1)

        self.assertEqual(self.window.draft.to_dict(), before)
        self.assertFalse(self.window.dirty)
        self.assertEqual(self.service.calls, [])
        self.assertEqual(self.window.message.objectName(), "error")

    def test_page_move_refuses_unknown_position_dependent_widget_without_changes(self):
        self.window.draft.display.pages = [["unknown_65_01", None, None, "dpi"],
                                           ["empty", "empty", "empty", "empty"]]
        self.window._load_draft()
        self.window.dirty = False
        before = self.window.draft.to_dict()
        self.window.lcd_tabs.setCurrentIndex(0)
        self.assertFalse(self.window.lcd_move_right.isEnabled())
        self.assertIn("unrecognized", self.window.lcd_move_right.toolTip())
        self.window._move_lcd_page(1)
        self.assertEqual(self.window.draft.to_dict(), before)
        self.assertFalse(self.window.dirty)
        self.assertEqual(self.service.calls, [])
        self.assertEqual(self.window.message.objectName(), "error")

    def test_background_upload_updates_verified_selection_and_preserves_every_pending_edit(self):
        self.read()
        self.window.draft.sensor.stages[0].value = 1250
        self.window.draft.display.brightness = 23
        self.window.draft.display.pages = [["system_media", None, None, "dpi"]]
        self.window.draft.lighting.brightness = 37
        self.window.draft.buttons[5].primary = Action("media", "mute")
        self.window.draft.power.eco_mode = True
        before = self.window.draft.to_dict()
        original_snapshot = self.window.snapshot
        original_copy = copy.deepcopy(original_snapshot)
        self.upload()
        expected = copy.deepcopy(before)
        expected["display"]["background_index"] = 1
        self.assertEqual(self.window.draft.to_dict(), expected)
        self.assertEqual(self.window.background_selection.currentData(), 1)
        self.assertEqual(self.window.snapshot["baseline"]["settings"]["background"], "102c0001aabbccdd")
        self.assertEqual(self.window.snapshot["baseline"]["settings"]["sensor"], "retained-sensor")
        self.assertEqual(self.window.snapshot["configuration"].display.background_index, 1)
        self.assertEqual(self.window.snapshot["configuration"].sensor.stages[0].value, 650)
        self.assertEqual(original_snapshot, original_copy)
        self.assertEqual(self.service.calls[-1], ("upload_background", "test-mc7", bytes(86336)))
        self.assertTrue(self.window.apply_button.isEnabled())
        self.assertTrue(self.window.dirty)
        self.assertIn("pixels cannot be read back", self.window.message.text())

    def test_background_verified_selection_updates_serialized_snapshot_configuration(self):
        self.read()
        self.window.snapshot["configuration"] = self.window.snapshot["configuration"].to_dict()
        self.window.draft.display.brightness = 27
        self.upload()
        self.assertEqual(self.window.snapshot["configuration"]["display"]["background_index"], 1)
        self.assertEqual(self.window.draft.display.brightness, 27)

    def test_negative_acknowledgement_invalidates_baseline_without_clobbering_draft(self):
        for response in ({"acknowledged": False}, {}):
            with self.subTest(response=response):
                self.read()
                self.window.draft.lighting.brightness = 37
                before = self.window.draft.to_dict()
                self.service.background_result = response
                self.upload()
                self.assertIsNone(self.window.snapshot)
                self.assertFalse(self.window.apply_button.isEnabled())
                self.assertEqual(self.window.draft.to_dict(), before)
                self.assertEqual(self.window.message.objectName(), "error")

    def test_unverified_selection_or_upload_exception_invalidates_baseline(self):
        responses = [
            {"acknowledged": True},
            {"acknowledged": True, "selection_verified": False, "selection_after": "102c000100000000"},
            {"acknowledged": True, "selection_verified": True, "selection_after": "102c000000000000"},
            {"acknowledged": True, "selection_verified": True, "selection_after": "not-hex"},
        ]
        for response in responses:
            with self.subTest(response=response):
                self.read()
                self.window.draft.display.brightness = 23
                before = self.window.draft.to_dict()
                self.service.background_result = response
                self.upload()
                self.assertIsNone(self.window.snapshot)
                self.assertFalse(self.window.apply_button.isEnabled())
                self.assertEqual(self.window.draft.to_dict(), before)
        self.read()
        self.window.draft.display.brightness = 25
        before = self.window.draft.to_dict()
        self.service.failure = "The mouse may have changed; read it again."
        self.upload()
        self.assertIsNone(self.window.snapshot)
        self.assertFalse(self.window.apply_button.isEnabled())
        self.assertEqual(self.window.draft.to_dict(), before)
        self.assertEqual(self.window.message.objectName(), "error")


if __name__ == "__main__":
    unittest.main()
