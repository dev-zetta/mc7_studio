"""Opt-in LCD telemetry with fake service workers, never a physical mouse."""

import copy
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtWidgets import QApplication, QDialog
    from swarm2.gui.app import MainWindow
except ModuleNotFoundError as error:
    if error.name and error.name.startswith("PySide6"):
        MainWindow = QCloseEvent = None
    else:
        raise

from swarm2.configuration import PresetStore
from tests.test_gui import FakeService
from tests.test_settings import CAPTURES, with_checksum


class HostLcdService(FakeService):
    def __init__(self):
        super().__init__()
        self.capabilities.append("host_lcd")
        self.verified_fields.append("display.pages")
        self.configuration.display.pages = [
            ["cpu_load", "ram_usage", "dpi", "empty"],
            ["game_bar", "system_media", None, None],
            ["cut", "copy", "paste", "undo"],
        ]
        self.failure = None
        self.gate = None
        self.entered = threading.Event()
        self.result = {
            "acknowledged": True, "cpu_percent": 42, "ram_percent": 73,
            "widgets": 2,
            "updated_values": {"cpu_load": 42, "ram_usage": 73},
            "unavailable_widgets": [],
        }
        self.mutate_argument = False
        self.gpu_source_records = [
            {"id": "pci:0000:03:00.0", "label": "PCI 0000:03:00.0",
             "primary": True, "load_available": True,
             "temperature_available": True},
            {"id": "pci:0000:08:00.0", "label": "PCI 0000:08:00.0",
             "primary": False, "load_available": True,
             "temperature_available": True},
        ]

    def snapshot(self, slot=1):
        result = super().snapshot(slot)
        lcd = bytearray.fromhex(CAPTURES["lcd"])
        lcd[3] = slot - 1
        lcd[5:16] = bytes.fromhex("010000fe00640041003a00")
        result["baseline"] = {"settings": {"lcd": with_checksum(lcd).hex()}}
        result["summary"]["active_profile"] = slot
        return result

    def gpu_sources(self):
        return copy.deepcopy(self.gpu_source_records)

    def update_host_lcd(self, device_id, profile_slot, baseline, gpu_source=None):
        call = ("host_lcd", device_id, profile_slot, copy.deepcopy(baseline))
        self.calls.append(call if gpu_source is None else (*call, gpu_source))
        self.entered.set()
        if self.gate is not None and not self.gate.wait(2):
            raise RuntimeError("Test LCD worker timed out")
        if self.failure:
            raise RuntimeError(self.failure)
        if self.mutate_argument:
            baseline["settings"]["lcd"] = "argument mutation must not reach the window"
        return copy.deepcopy(self.result)


@unittest.skipIf(MainWindow is None, "Install the gui extra to exercise Qt")
class HostLcdGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = HostLcdService()
        self.window = MainWindow(service=self.service,
                                 store=PresetStore(Path(self.directory.name)),
                                 auto_discover=False)
        self.window.refresh_device()
        self.wait_for_job()
        self.window.read_mouse()
        self.wait_for_job()
        self.wait_for_gpu_sources()

    def tearDown(self):
        self.window.host_lcd_checkbox.setChecked(False)
        if self.service.gate is not None:
            self.service.gate.set()
        self.wait_for_job()
        self.window.keep_running_checkbox.setChecked(False)
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
        self.assertIsNone(self.window._job)

    def wait_for_gpu_sources(self):
        deadline = time.monotonic() + 3
        while self.window._gpu_source_job is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.app.processEvents()
        self.assertIsNone(self.window._gpu_source_job)

    def host_calls(self):
        return [call for call in self.service.calls if isinstance(call, tuple) and call[0] == "host_lcd"]

    def enable(self):
        self.window.host_lcd_checkbox.setChecked(True)
        self.wait_for_job()

    def test_read_and_draft_edit_never_start_telemetry_implicitly(self):
        self.assertTrue(self.window.host_lcd_checkbox.isEnabled())
        self.assertFalse(self.window.host_lcd_checkbox.isChecked())
        self.assertFalse(self.window.host_lcd_timer.isActive())
        self.window.draft.display.pages[0][0] = "dpi"
        self.window._changed()
        self.window._poll_host_lcd()
        self.assertEqual(self.host_calls(), [])

    def test_explicit_enable_starts_bounded_timer_and_uses_read_layout(self):
        # An unapplied local layout is not sent as if it were the mouse's layout.
        self.window.draft.display.pages[0][0] = "dpi"
        self.window.dirty = True
        baseline = copy.deepcopy(self.window.snapshot["baseline"])
        self.enable()
        self.assertTrue(self.window.host_lcd_timer.isActive())
        self.assertGreaterEqual(self.window.host_lcd_timer.interval(), 2000)
        self.assertEqual(self.host_calls(), [("host_lcd", "test-mc7", 1, baseline)])
        self.assertIn("CPU 42%", self.window.host_lcd_status.text())
        self.assertIn("RAM 73%", self.window.host_lcd_status.text())

    def test_gpu_and_temperature_layout_is_available_and_reports_missing_source(self):
        lcd = bytearray.fromhex(CAPTURES["lcd"])
        lcd[5:16] = bytes.fromhex("010000fe00370039003800")
        self.window.snapshot["baseline"]["settings"]["lcd"] = with_checksum(lcd).hex()
        self.service.result.update(
            widgets=2,
            updated_values={"gpu_load": 17, "gpu_temperature": 63},
            unavailable_widgets=["cpu_temperature"],
        )
        self.window._update_capabilities()
        self.assertTrue(self.window.host_lcd_checkbox.isEnabled())
        self.enable()
        status = self.window.host_lcd_status.text()
        self.assertIn("GPU 17%", status)
        self.assertIn("GPU temp 63°C", status)
        self.assertIn("CPU temp unavailable", status)

    def test_all_monitoring_widgets_are_offered_by_the_editor(self):
        choices = {self.window.lcd_controls[0][0].itemData(index)
                   for index in range(self.window.lcd_controls[0][0].count())}
        self.assertTrue({"cpu_load", "cpu_temperature", "gpu_load",
                         "gpu_temperature", "ram_usage"}.issubset(choices))

    def test_specific_gpu_source_is_saved_and_forwarded_to_live_sampling(self):
        self.assertEqual(self.window.gpu_source_selector.count(), 3)
        self.assertIn("system primary", self.window.gpu_source_selector.itemText(1))
        self.window.gpu_source_selector.setCurrentIndex(2)
        self.assertEqual(self.window.gpu_source_store.load(), "pci:0000:08:00.0")
        self.enable()
        self.assertEqual(self.host_calls()[-1][-1], "pci:0000:08:00.0")
        self.assertIn("PCI 0000:08:00.0", self.window.host_lcd_status.text())

    def test_saved_missing_gpu_stays_selected_without_automatic_fallback(self):
        self.window.gpu_source_selector.setCurrentIndex(2)
        self.service.gpu_source_records = self.service.gpu_source_records[:1]
        self.window._refresh_gpu_sources()
        self.wait_for_gpu_sources()
        self.assertEqual(
            self.window.gpu_source_selector.currentData(), "pci:0000:08:00.0")
        self.assertIn("unavailable", self.window.gpu_source_selector.currentText())
        self.assertIn("will be skipped", self.window.gpu_source_status.text())
        self.enable()
        self.assertEqual(self.host_calls()[-1][-1], "pci:0000:08:00.0")

    def test_changing_gpu_source_stops_an_active_live_session(self):
        self.enable()
        count = len(self.host_calls())
        self.window.gpu_source_selector.setCurrentIndex(2)
        self.assertFalse(self.window.host_lcd_checkbox.isChecked())
        self.assertFalse(self.window.host_lcd_timer.isActive())
        self.assertIn("GPU source changed", self.window.host_lcd_status.text())
        self.window._poll_host_lcd()
        self.assertEqual(len(self.host_calls()), count)

    def test_timers_continue_when_close_waits_for_gpu_discovery(self):
        class PendingGpuJob:
            @staticmethod
            def isRunning():
                return True

            @staticmethod
            def wait(milliseconds):
                self.assertEqual(milliseconds, 2000)
                return False

        self.window.status_timer.start()
        self.window.host_lcd_timer.start()
        self.window.automatic_profile_timer.start()
        self.window.dirty = False
        self.window._gpu_source_job = PendingGpuJob()
        event = QCloseEvent()
        try:
            self.window.closeEvent(event)
        finally:
            self.window._gpu_source_job = None
        self.assertFalse(event.isAccepted())
        self.assertTrue(self.window.status_timer.isActive())
        self.assertTrue(self.window.host_lcd_timer.isActive())
        self.assertTrue(self.window.automatic_profile_timer.isActive())

    def test_success_and_failure_preserve_dirty_draft_snapshot_and_baseline(self):
        self.window.draft.lighting.brightness = 43
        self.window.dirty = True
        draft = self.window.draft.to_dict()
        snapshot = self.window.snapshot
        baseline = copy.deepcopy(snapshot["baseline"])
        self.service.mutate_argument = True
        self.enable()
        self.assertEqual(self.window.draft.to_dict(), draft)
        self.assertIs(self.window.snapshot, snapshot)
        self.assertEqual(self.window.snapshot["baseline"], baseline)
        self.assertTrue(self.window.dirty)
        self.service.failure = "Mouse disconnected"
        self.window._poll_host_lcd()
        self.wait_for_job()
        self.assertFalse(self.window.host_lcd_checkbox.isChecked())
        self.assertFalse(self.window.host_lcd_timer.isActive())
        self.assertIn("Mouse disconnected", self.window.host_lcd_status.text())
        self.assertEqual(self.window.draft.to_dict(), draft)
        self.assertIs(self.window.snapshot, snapshot)
        self.assertEqual(self.window.snapshot["baseline"], baseline)
        self.assertTrue(self.window.dirty)

    def test_busy_and_modal_pause_without_disabling_opt_in(self):
        self.service.gate = threading.Event()
        self.window.host_lcd_checkbox.setChecked(True)
        self.assertTrue(self.service.entered.wait(1))
        self.window._poll_host_lcd()
        self.assertEqual(len(self.host_calls()), 1)
        self.assertTrue(self.window.host_lcd_checkbox.isChecked())
        self.service.gate.set()
        self.wait_for_job()
        dialog = QDialog(self.window)
        dialog.setModal(True)
        dialog.show()
        self.app.processEvents()
        try:
            self.assertIs(self.app.activeModalWidget(), dialog)
            self.window._poll_host_lcd()
            self.assertEqual(len(self.host_calls()), 1)
            self.assertIsNone(self.window._job)
            self.assertTrue(self.window.host_lcd_checkbox.isChecked())
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_profile_device_and_read_layout_changes_stop_before_more_writes(self):
        for change in ("profile", "device", "layout", "active_profile", "missing_snapshot"):
            with self.subTest(change=change):
                self.window.selected_device_id = "test-mc7"
                self.window.draft.profile_slot = 1
                self.window.snapshot = self.service.snapshot()
                self.enable()
                count = len(self.host_calls())
                if change == "profile":
                    self.window.draft.profile_slot = 2
                elif change == "device":
                    self.window.selected_device_id = "different-mouse"
                elif change == "layout":
                    self.window.snapshot["baseline"]["settings"]["lcd"] = CAPTURES["lcd"]
                elif change == "active_profile":
                    self.window.snapshot["summary"]["active_profile"] = 2
                else:
                    self.window.snapshot = None
                self.window._poll_host_lcd()
                self.assertEqual(len(self.host_calls()), count)
                self.assertFalse(self.window.host_lcd_checkbox.isChecked())
                self.assertFalse(self.window.host_lcd_timer.isActive())

    def test_missing_ack_stops_instead_of_reporting_success(self):
        self.service.result["acknowledged"] = False
        self.enable()
        self.assertFalse(self.window.host_lcd_checkbox.isChecked())
        self.assertFalse(self.window.host_lcd_timer.isActive())
        self.assertIn("did not acknowledge", self.window.host_lcd_status.text())

    def test_disable_during_worker_ignores_late_result_and_prevents_next_update(self):
        self.service.gate = threading.Event()
        self.window.host_lcd_checkbox.setChecked(True)
        self.assertTrue(self.service.entered.wait(1))
        self.window.host_lcd_checkbox.setChecked(False)
        self.assertFalse(self.window.host_lcd_timer.isActive())
        self.service.gate.set()
        self.wait_for_job()
        self.assertIn("Stopped", self.window.host_lcd_status.text())
        self.assertNotIn("Sent CPU", self.window.host_lcd_status.text())
        self.window._poll_host_lcd()
        self.assertEqual(len(self.host_calls()), 1)

    def test_capability_and_active_read_are_required(self):
        for change in ("capability", "profile", "layout"):
            with self.subTest(change=change):
                self.window.devices[0]["capabilities"] = [*self.service.capabilities]
                self.window.snapshot = self.service.snapshot()
                if change == "capability":
                    self.window.devices[0]["capabilities"].remove("host_lcd")
                elif change == "profile":
                    self.window.snapshot["summary"]["active_profile"] = 3
                else:
                    self.window.snapshot["baseline"]["settings"]["lcd"] = CAPTURES["lcd"]
                self.window._update_capabilities()
                self.assertFalse(self.window.host_lcd_checkbox.isEnabled())
                self.window.host_lcd_checkbox.setChecked(True)
                self.assertFalse(self.window.host_lcd_checkbox.isChecked())
        self.assertEqual(self.host_calls(), [])
