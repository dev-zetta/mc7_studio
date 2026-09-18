"""Main-window surface-calibration integration with fake dialogs and services."""

import copy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from tests import test_gui as gui_fixture
from tests.test_settings import CAPTURES
from swarm2.configuration import Action, Macro, MacroEvent, PresetStore

if gui_fixture.MainWindow is not None:
    from PySide6.QtGui import QCloseEvent


class DcuService(gui_fixture.FakeService):
    def __init__(self):
        super().__init__()
        self.capabilities += ["calibrate_lift_off", "read_status", "host_lcd"]
        self.verified_fields += ["sensor.lift_off_distance", "lighting.brightness",
                                 "buttons", "display.pages", "power.eco_mode"]
        self.revision = 0
        self.read_gate = None
        self.read_entered = threading.Event()
        self.configuration.sensor.lift_off_distance = "low"

    def snapshot(self, slot=1):
        result = super().snapshot(slot)
        lcd = bytearray.fromhex(CAPTURES["lcd"])
        lcd[3] = slot - 1
        lcd[5:16] = bytes.fromhex("010000fe00640041003a00")
        result["baseline"] = {"revision": self.revision,
                              "settings": {"lcd": lcd.hex()}}
        result["summary"]["active_profile"] = slot
        return result

    def read(self, device_id, profile_slot=1, draft=None):
        self.read_entered.set()
        if self.read_gate is not None and not self.read_gate.wait(2):
            raise RuntimeError("Fake calibration refresh timed out")
        return super().read(device_id, profile_slot, draft)

    def read_status(self, device_id):
        self.calls.append(("read_status", device_id))
        return {"firmware_version": "5.04", "battery_percent": 75, "charging": False}

    def update_host_lcd(self, device_id, profile_slot, baseline):
        self.calls.append(("host_lcd", device_id, profile_slot, baseline))
        return {"acknowledged": True, "cpu_percent": 20, "ram_percent": 30, "widgets": 2}


@unittest.skipIf(gui_fixture.MainWindow is None, "Install the gui extra to exercise Qt")
class DcuMainWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = gui_fixture.QApplication.instance() or gui_fixture.QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = DcuService()
        self.window = gui_fixture.MainWindow(service=self.service,
            store=PresetStore(Path(self.directory.name) / "presets"), auto_discover=False)
        self.window.navigation.setCurrentRow(1)
        self.window.refresh_device()
        self.wait_for_job()
        self.window.read_mouse()
        self.wait_for_job()
        self.service.read_entered.clear()

    def tearDown(self):
        if self.service.read_gate is not None:
            self.service.read_gate.set()
        self.window._dcu_dialog = None
        self.window.host_lcd_checkbox.setChecked(False)
        self.window.battery_monitor_checkbox.setChecked(False)
        self.window.keep_running_checkbox.setChecked(False)
        self.wait_for_job()
        self.window.dirty = False
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.directory.cleanup()

    def wait_for_job(self):
        deadline = time.monotonic() + 3
        while self.window._job is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.app.processEvents()
        self.assertIsNone(self.window._job, "Fake calibration refresh did not finish")

    def pending_edits(self):
        draft = self.window.draft
        draft.sensor.stages[0].value = 1250
        draft.sensor.polling_rate = 8000
        draft.lighting.brightness = 37
        draft.buttons[5].primary = Action("media", "mute")
        draft.display.brightness = 40
        draft.power.eco_mode = True
        draft.macros = [Macro(name="Pending macro", events=[
            MacroEvent("key_down", "F13", 0), MacroEvent("key_up", "F13", 50)])]
        self.window._load_draft()
        self.window._changed()
        return draft.to_dict()

    def dialog(self, factory, outcome=None, *, changed=False):
        dialog = factory.return_value
        dialog.outcome = outcome
        dialog.device_may_have_changed = changed
        dialog.exec.return_value = gui_fixture.QDialog.DialogCode.Rejected
        return dialog

    def test_open_and_close_without_start_preserves_draft_baseline_and_device_calls(self):
        before = self.pending_edits()
        snapshot = self.window.snapshot
        calls = copy.deepcopy(self.service.calls)
        with patch("swarm2.gui.app.DcuCalibrationDialog") as factory:
            dialog = self.dialog(factory)
            self.window.calibrate_lift_off()
            factory.assert_called_once_with("test-mc7", self.window)
            dialog.exec.assert_called_once_with()
            dialog.deleteLater.assert_called_once_with()
        self.assertIsNone(self.window._dcu_dialog)
        self.assertIs(self.window.snapshot, snapshot)
        self.assertEqual(self.window.draft.to_dict(), before)
        self.assertEqual(self.service.calls, calls)
        self.assertTrue(self.window.dirty)
        self.assertTrue(self.window.apply_button.isEnabled())

    def test_verified_cancel_refreshes_baseline_without_merging_over_pending_edits(self):
        before = self.pending_edits()
        calls = copy.deepcopy(self.service.calls)
        self.service.revision = 1
        # A refreshed device value must enter the baseline without overwriting
        # a separate unsaved draft value on this or another page.
        self.service.configuration.sensor.stages[0].value = 2000
        self.service.configuration.lighting.brightness = 60
        self.service.read_gate = threading.Event()
        with patch("swarm2.gui.app.DcuCalibrationDialog") as factory:
            self.dialog(factory, {"outcome": "cancelled", "verified": True}, changed=True)
            self.window.calibrate_lift_off()
        self.assertTrue(self.service.read_entered.wait(1))
        self.assertIsNone(self.window.snapshot)
        self.assertFalse(self.window.apply_button.isEnabled())
        self.assertEqual(self.window.draft.to_dict(), before)
        self.service.read_gate.set()
        self.wait_for_job()
        self.assertEqual(self.service.calls, calls + [("read", "test-mc7", 1)])
        self.assertEqual(self.window.snapshot["baseline"]["revision"], 1)
        self.assertEqual(self.window.snapshot["configuration"].sensor.stages[0].value, 2000)
        self.assertEqual(self.window.draft.to_dict(), before)
        self.assertTrue(self.window.dirty)
        self.assertTrue(self.window.apply_button.isEnabled())

    def test_verified_commit_changes_only_lift_off_and_refreshes_the_baseline(self):
        before = self.pending_edits()
        calls = copy.deepcopy(self.service.calls)
        self.service.revision = 2
        self.service.configuration.sensor.lift_off_distance = "custom"
        self.service.configuration.sensor.stages[0].value = 2000
        with patch("swarm2.gui.app.DcuCalibrationDialog") as factory:
            self.dialog(factory, {"outcome": "committed", "verified": True}, changed=True)
            self.window.calibrate_lift_off()
        self.wait_for_job()
        expected = copy.deepcopy(before)
        expected["sensor"]["lift_off_distance"] = "custom"
        self.assertEqual(self.window.draft.to_dict(), expected)
        self.assertEqual(self.window.snapshot["baseline"]["revision"], 2)
        self.assertEqual(self.window.snapshot["configuration"].sensor.lift_off_distance, "custom")
        self.assertEqual(self.service.calls, calls + [("read", "test-mc7", 1)])
        self.assertTrue(self.window.dirty)
        self.assertTrue(self.window.apply_button.isEnabled())

    def test_verified_cancel_does_not_mark_a_previously_clean_draft_dirty(self):
        self.window.dirty = False
        before = self.window.draft.to_dict()
        with patch("swarm2.gui.app.DcuCalibrationDialog") as factory:
            self.dialog(factory, {"outcome": "cancelled", "verified": True}, changed=True)
            self.window.calibrate_lift_off()
        self.wait_for_job()
        self.assertEqual(self.window.draft.to_dict(), before)
        self.assertFalse(self.window.dirty)

    def test_uncertain_result_clears_baseline_disables_apply_and_retains_all_edits(self):
        before = self.pending_edits()
        calls = copy.deepcopy(self.service.calls)
        with patch("swarm2.gui.app.DcuCalibrationDialog") as factory:
            self.dialog(factory, {"outcome": "uncertain", "verified": False,
                                  "error": "The cancellation could not be checked"}, changed=True)
            self.window.calibrate_lift_off()
        self.assertIsNone(self.window.snapshot)
        self.assertFalse(self.window.apply_button.isEnabled())
        self.assertFalse(self.window.dcu_calibration_button.isEnabled())
        self.assertEqual(self.window.draft.to_dict(), before)
        self.assertTrue(self.window.dirty)
        self.assertEqual(self.service.calls, calls)
        self.assertIn("cancellation could not be checked", self.window.message.text())
        self.assertEqual(self.window.message.objectName(), "error")

    def test_missing_or_unverified_result_never_refreshes_or_accepts_custom(self):
        before = self.pending_edits()
        original = copy.deepcopy(self.window.snapshot)
        calls = copy.deepcopy(self.service.calls)
        for outcome in (None, {"outcome": "committed", "verified": False},
                        {"outcome": "cancelled", "verified": False}):
            with self.subTest(outcome=outcome):
                self.window.snapshot = copy.deepcopy(original)
                self.window._update_capabilities()
                with patch("swarm2.gui.app.DcuCalibrationDialog") as factory:
                    self.dialog(factory, outcome, changed=True)
                    self.window.calibrate_lift_off()
                self.assertIsNone(self.window.snapshot)
                self.assertFalse(self.window.apply_button.isEnabled())
                self.assertEqual(self.window.draft.to_dict(), before)
                self.assertEqual(self.service.calls, calls)

    def test_refresh_failure_leaves_no_apply_baseline_and_preserves_pending_edits(self):
        before = self.pending_edits()
        self.service.failure = "Mouse disconnected during calibration refresh"
        with patch("swarm2.gui.app.DcuCalibrationDialog") as factory:
            self.dialog(factory, {"outcome": "cancelled", "verified": True}, changed=True)
            self.window.calibrate_lift_off()
        self.wait_for_job()
        self.assertIsNone(self.window.snapshot)
        self.assertFalse(self.window.apply_button.isEnabled())
        self.assertEqual(self.window.draft.to_dict(), before)
        self.assertTrue(self.window.dirty)
        self.assertIn("disconnected during calibration refresh", self.window.message.text())

    def test_changed_calibration_stops_opted_in_live_lcd_updates(self):
        self.window.host_lcd_checkbox.setChecked(True)
        self.wait_for_job()
        self.assertTrue(self.window.host_lcd_timer.isActive())
        before = len(self.service.calls)
        with patch("swarm2.gui.app.DcuCalibrationDialog") as factory:
            self.dialog(factory, {"outcome": "uncertain", "verified": False}, changed=True)
            self.window.calibrate_lift_off()
        self.assertFalse(self.window.host_lcd_checkbox.isChecked())
        self.assertFalse(self.window.host_lcd_timer.isActive())
        self.assertIsNone(self.window._host_lcd_context)
        self.window._poll_host_lcd()
        self.assertEqual(len(self.service.calls), before)

    def test_dialog_blocks_all_worker_paths_and_main_window_close(self):
        calls = copy.deepcopy(self.service.calls)
        self.window.host_lcd_checkbox.blockSignals(True)
        self.window.host_lcd_checkbox.setChecked(True)
        self.window.host_lcd_checkbox.blockSignals(False)
        self.window._host_lcd_context = self.window._host_lcd_ready()
        self.window.battery_monitor_checkbox.blockSignals(True)
        self.window.battery_monitor_checkbox.setChecked(True)
        self.window.battery_monitor_checkbox.blockSignals(False)
        with patch("swarm2.gui.app.DcuCalibrationDialog") as factory:
            dialog = self.dialog(factory)

            def while_open():
                self.assertIs(self.window._dcu_dialog, dialog)
                self.assertFalse(self.window.apply_button.isEnabled())
                self.assertFalse(self.window.read_button.isEnabled())
                self.assertFalse(self.window.dcu_calibration_button.isEnabled())
                operation, completed = Mock(), Mock()
                for telemetry in (False, True):
                    self.window._run_job(operation, completed, "Must not start", telemetry=telemetry)
                self.window.refresh_device()
                self.window.battery_tray.read_requested.emit()
                self.window._poll_status()
                self.window._poll_host_lcd()
                self.window.apply_to_mouse()
                self.window.calibrate_lift_off()
                operation.assert_not_called()
                completed.assert_not_called()
                self.assertIsNone(self.window._job)
                self.assertEqual(self.service.calls, calls)
                close_event = QCloseEvent()
                self.window.closeEvent(close_event)
                self.assertFalse(close_event.isAccepted())
                factory.assert_called_once()
                return gui_fixture.QDialog.DialogCode.Rejected

            dialog.exec.side_effect = while_open
            self.window.calibrate_lift_off()
        self.assertIsNone(self.window._dcu_dialog)
        self.assertEqual(self.service.calls, calls)
        self.assertTrue(self.window.apply_button.isEnabled())

    def test_calibration_requires_capability_and_matching_read(self):
        original = copy.deepcopy(self.window.snapshot)
        for kind in ("no_read", "wrong_device", "wrong_profile", "no_capability"):
            with self.subTest(kind=kind):
                self.window.snapshot = copy.deepcopy(original)
                self.service.capabilities[:] = [*self.service.capabilities, "calibrate_lift_off"]
                if kind == "no_read":
                    self.window.snapshot = None
                elif kind == "wrong_device":
                    self.window.snapshot["device_id"] = "another-mouse"
                elif kind == "wrong_profile":
                    self.window.snapshot["profile_slot"] = 2
                else:
                    self.service.capabilities[:] = [value for value in self.service.capabilities
                                                    if value != "calibrate_lift_off"]
                self.window._update_capabilities()
                self.assertFalse(self.window.dcu_calibration_button.isEnabled())
                with patch("swarm2.gui.app.DcuCalibrationDialog") as factory:
                    self.window.calibrate_lift_off()
                    factory.assert_not_called()

    def test_repeat_count_is_enabled_only_for_repeat_and_keeps_its_local_value(self):
        self.assertFalse(self.window.macro_repeat.isEnabled())
        self.window._new_macro()
        self.assertFalse(self.window.macro_repeat.isEnabled())
        for mode in ("repeat", "while_held", "toggle", "once", "repeat"):
            with self.subTest(mode=mode):
                index = self.window.macro_playback.findData(mode)
                self.assertGreaterEqual(index, 0)
                self.window.macro_playback.setCurrentIndex(index)
                if mode == "repeat":
                    self.window.macro_repeat.setValue(7)
                self.assertEqual(self.window.macro_repeat.isEnabled(), mode == "repeat")
                self.assertEqual(self.window.draft.macros[0].playback, mode)
                self.assertEqual(self.window.draft.macros[0].repeat, 7)
        calls = copy.deepcopy(self.service.calls)
        self.window._refresh_macros()
        self.assertTrue(self.window.macro_repeat.isEnabled())
        self.assertEqual(self.window.macro_repeat.value(), 7)
        self.assertEqual(self.service.calls, calls)


if __name__ == "__main__":
    unittest.main()
