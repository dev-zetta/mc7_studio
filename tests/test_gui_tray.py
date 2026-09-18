"""Battery monitoring tests with fake telemetry and tray; no physical USB."""

import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QApplication, QDialog, QSystemTrayIcon
    from swarm2.gui.app import MainWindow
    from swarm2.gui.tray import BatteryAlertPolicy, BatteryTray, battery_icon, normalized_status
except ModuleNotFoundError as error:
    if error.name and error.name.startswith("PySide6"):
        MainWindow = None
    else:
        raise

from swarm2.configuration import PresetStore
from tests.test_gui import FakeService


if MainWindow is not None:
    class FakeTrayIcon(QObject):
        activated = Signal(object)
        messageClicked = Signal()

        def __init__(self):
            super().__init__()
            self.visible = False
            self.messages = []

        def show(self): self.visible = True
        def hide(self): self.visible = False
        def setContextMenu(self, menu): self.menu = menu
        def setToolTip(self, text): self.tooltip = text
        def setIcon(self, icon): self.icon = icon
        def showMessage(self, *args): self.messages.append(args)


class StatusService(FakeService):
    def __init__(self):
        super().__init__()
        self.capabilities.append("read_status")
        self.status = {"firmware_version": "0.00.test", "battery_percent": 54, "charging": False}
        self.status_failure = None
        self.status_gate = None

    def read_status(self, device_id):
        self.calls.append(("read_status", device_id))
        if self.status_gate is not None and not self.status_gate.wait(2):
            raise RuntimeError("Test telemetry timed out")
        if self.status_failure:
            raise RuntimeError(self.status_failure)
        return self.status


@unittest.skipIf(MainWindow is None, "Install the gui extra to exercise Qt")
class BatteryPolicyTests(unittest.TestCase):
    def test_single_alert_and_hysteresis(self):
        policy = BatteryAlertPolicy()
        def update(level):
            return policy.update("mouse", {"battery_percent": level, "charging": False}, enabled=True)
        self.assertFalse(update(21))
        self.assertTrue(update(20))
        for level in (19, 20, 21, 24, 18):
            self.assertFalse(update(level))
        self.assertFalse(update(25))
        self.assertTrue(update(19))

    def test_unknown_charging_disconnect_and_device_switch(self):
        policy = BatteryAlertPolicy()
        for charging in (None, True):
            self.assertFalse(policy.update("mouse", {"battery_percent": 5, "charging": charging}, enabled=True))
        low = {"battery_percent": 5, "charging": False}
        self.assertFalse(policy.update("mouse", low, enabled=False))
        self.assertTrue(policy.update("mouse", low, enabled=True))
        self.assertFalse(policy.update("mouse", None, enabled=True))
        self.assertFalse(policy.update("mouse", low, enabled=True))
        self.assertTrue(policy.update("other", low, enabled=True))

    def test_malformed_values_are_unknown_not_zero(self):
        for value in (-1, 101, True, "5", 5.1):
            self.assertIsNone(normalized_status({"battery_percent": value})["battery_percent"])
        self.assertEqual(normalized_status(None), {"firmware_version": None, "battery_percent": None, "charging": None})


@unittest.skipIf(MainWindow is None, "Install the gui extra to exercise Qt")
class BatteryTrayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.icon = FakeTrayIcon()
        self.available = True
        self.messages = True
        self.tray = BatteryTray(icon=self.icon, available=lambda: self.available,
                                messages_supported=lambda: self.messages)

    def tearDown(self):
        self.tray.set_enabled(False)
        self.tray.menu.deleteLater()
        self.tray.deleteLater()
        self.app.processEvents()

    def test_no_implicit_show_and_original_icon(self):
        self.assertFalse(self.icon.visible)
        for status in (None, {"battery_percent": 0}, {"battery_percent": 100, "charging": True}):
            self.assertFalse(battery_icon(status).isNull())
        self.assertTrue(self.tray.set_enabled(True))
        self.assertTrue(self.icon.visible)
        self.available = False
        self.assertFalse(self.tray.set_enabled(True))
        self.assertFalse(self.icon.visible)

    def test_notifications_require_explicit_options_and_platform_support(self):
        low = {"battery_percent": 20, "charging": False}
        self.tray.update_status("mouse", low)
        self.tray.set_enabled(True)
        self.assertEqual(self.icon.messages, [])
        self.messages = False
        self.tray.set_notifications(True)
        self.assertEqual(self.icon.messages, [])
        self.messages = True
        self.tray.update_status("mouse", low)
        self.assertEqual(len(self.icon.messages), 1)
        self.tray.update_status("mouse", None)
        self.tray.update_status("mouse", low)
        self.assertEqual(len(self.icon.messages), 1)
        self.assertIn("20%", self.icon.tooltip)

    def test_menu_and_click_routes(self):
        events = []
        self.tray.show_requested.connect(lambda: events.append("show"))
        self.tray.read_requested.connect(lambda: events.append("read"))
        self.tray.quit_requested.connect(lambda: events.append("quit"))
        self.tray.show_action.trigger()
        self.tray.read_action.trigger()
        self.tray.quit_action.trigger()
        self.icon.activated.emit(QSystemTrayIcon.ActivationReason.Trigger)
        self.icon.messageClicked.emit()
        self.assertEqual(events, ["show", "read", "quit", "show", "show"])


@unittest.skipIf(MainWindow is None, "Install the gui extra to exercise Qt")
class BatteryMonitoringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = StatusService()
        self.available = True
        self.icon = FakeTrayIcon()
        def factory(parent):
            return BatteryTray(parent, icon=self.icon, available=lambda: self.available,
                               messages_supported=lambda: True)
        with patch("swarm2.gui.app.BatteryTray", side_effect=factory):
            self.window = MainWindow(service=self.service, store=PresetStore(Path(self.directory.name)), auto_discover=False)
        self.window.refresh_device()
        self.wait_for_job()

    def tearDown(self):
        if self.service.status_gate is not None:
            self.service.status_gate.set()
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

    def test_monitoring_defaults_off(self):
        self.assertEqual(self.service.calls, ["discover"])
        self.assertFalse(self.window.status_timer.isActive())
        self.assertFalse(self.window.battery_monitor_checkbox.isChecked())
        self.assertFalse(self.window.keep_running_checkbox.isEnabled())
        self.assertFalse(self.icon.visible)

    def test_success_and_disconnect_leave_draft_and_baseline_unchanged(self):
        self.window.read_mouse()
        self.wait_for_job()
        self.window.draft.lighting.brightness = 43
        self.window.dirty = True
        before = self.window.draft.to_dict()
        snapshot = self.window.snapshot
        self.window.refresh_status()
        self.wait_for_job()
        self.assertIn("54%", self.window.overview_battery.text())
        self.assertEqual(self.window.draft.to_dict(), before)
        self.assertIs(self.window.snapshot, snapshot)
        self.assertTrue(self.window.dirty)
        self.service.status_failure = "Mouse disconnected"
        self.window.refresh_status()
        self.wait_for_job()
        self.assertIn("not reported", self.window.overview_battery.text())
        self.assertIn("Mouse disconnected", self.window.status_read_note.text())
        self.assertEqual(self.window.draft.to_dict(), before)
        self.assertIs(self.window.snapshot, snapshot)
        self.assertTrue(self.window.dirty)

    def test_timer_skips_busy_worker_and_modal_dialog(self):
        self.window.battery_monitor_checkbox.setChecked(True)
        self.wait_for_job()
        self.service.status_gate = threading.Event()
        self.window.refresh_status()
        count = len(self.service.calls)
        self.window._poll_status()
        self.assertIsNotNone(self.window._job)
        self.service.status_gate.set()
        self.wait_for_job()
        self.assertLessEqual(len(self.service.calls), count + 1)
        dialog = QDialog(self.window)
        dialog.setModal(True)
        dialog.show()
        self.app.processEvents()
        try:
            count = len(self.service.calls)
            self.window._poll_status()
            self.assertIsNone(self.window._job)
            self.assertEqual(len(self.service.calls), count)
        finally:
            dialog.close()

    def test_stale_result_ignored(self):
        self.window._status_received("previous-mouse", self.service.status)
        self.assertNotIn("54%", self.window.overview_battery.text())

    def test_keep_running_requires_opt_in_and_tray_loss_restores_window(self):
        self.window.show()
        self.window.battery_monitor_checkbox.setChecked(True)
        self.wait_for_job()
        self.window.keep_running_checkbox.setChecked(True)
        self.window.dirty = True
        with patch.object(self.window, "_can_replace_draft") as prompt:
            self.assertFalse(self.window.close())
            prompt.assert_not_called()
        self.assertFalse(self.window.isVisible())
        self.assertTrue(self.window.dirty)
        self.assertTrue(self.window.status_timer.isActive())
        self.available = False
        self.window._sync_tray_options()
        self.assertTrue(self.window.isVisible())
        self.assertFalse(self.window.keep_running_checkbox.isChecked())
        self.assertFalse(self.window.keep_running_checkbox.isEnabled())

    def test_quit_respects_dirty_cancel_and_busy_worker(self):
        self.window.battery_monitor_checkbox.setChecked(True)
        self.wait_for_job()
        self.window.keep_running_checkbox.setChecked(True)
        self.window.dirty = True
        with patch.object(self.window, "_can_replace_draft", return_value=False), patch.object(self.app, "quit") as quit_app:
            self.window.quit_from_tray()
            quit_app.assert_not_called()
        self.service.status_gate = threading.Event()
        self.window.refresh_status()
        with patch.object(self.app, "quit") as quit_app:
            self.window.quit_from_tray()
            quit_app.assert_not_called()
        self.assertIsNotNone(self.window._job)
        self.service.status_gate.set()
        self.wait_for_job()
        with patch.object(self.window, "_can_replace_draft", return_value=True), patch.object(self.app, "quit") as quit_app:
            self.window.quit_from_tray()
            quit_app.assert_called_once()
        self.assertFalse(self.window.status_timer.isActive())
        self.assertFalse(self.icon.visible)

    def test_unavailable_tray_keeps_monitoring_in_window(self):
        self.available = False
        self.window.battery_monitor_checkbox.setChecked(True)
        self.wait_for_job()
        self.assertTrue(self.window.status_timer.isActive())
        self.assertFalse(self.window.battery_notifications_checkbox.isEnabled())
        self.assertFalse(self.window.keep_running_checkbox.isEnabled())
        self.assertIn("no system tray", self.window.tray_availability.text())
        self.assertIn("54%", self.window.overview_battery.text())


if __name__ == "__main__":
    unittest.main()
