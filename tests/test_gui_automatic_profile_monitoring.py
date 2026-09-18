"""GUI coordination tests for local foreground profile rules; no physical USB."""

import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtWidgets import QApplication, QDialog
    from swarm2.gui.app import MainWindow
    from swarm2.gui.tray import BatteryTray
except ModuleNotFoundError as error:
    if error.name and error.name.startswith("PySide6"):
        MainWindow = None
    else:
        raise

from swarm2.automatic_profile_monitor import (
    ForegroundApplicationSnapshot,
    ForegroundApplicationStatus,
)
from swarm2.automatic_profiles import (
    ApplicationIdentity,
    ApplicationProfileRule,
    AutomaticProfileSettings,
    AutomaticProfileStore,
)
from swarm2.configuration import PresetStore
from tests.test_gui import FakeService


if MainWindow is not None:
    class FakeTrayIcon(QObject):
        activated = Signal(object)
        messageClicked = Signal()

        def __init__(self):
            super().__init__()
            self.visible = False

        def show(self): self.visible = True
        def hide(self): self.visible = False
        def setContextMenu(self, menu): self.menu = menu
        def setToolTip(self, text): self.tooltip = text
        def setIcon(self, icon): self.icon = icon
        def showMessage(self, *_args): pass


class FakeMonitor:
    def __init__(self):
        self.result = ForegroundApplicationSnapshot(
            ForegroundApplicationStatus.UNAVAILABLE,
            message="test monitor is idle",
        )
        self.calls = 0

    def poll(self):
        self.calls += 1
        return self.result


@unittest.skipIf(MainWindow is None, "Install the gui extra to exercise Qt")
class AutomaticProfileMonitoringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.service = FakeService()
        self.monitor = FakeMonitor()
        self.automatic_store = AutomaticProfileStore(root / "automatic.json")
        self.automatic_store.save(AutomaticProfileSettings())
        self.icon = FakeTrayIcon()

        def tray_factory(parent):
            return BatteryTray(
                parent,
                icon=self.icon,
                available=lambda: True,
                messages_supported=lambda: True,
            )

        with patch("swarm2.gui.app.BatteryTray", side_effect=tray_factory):
            self.window = MainWindow(
                service=self.service,
                store=PresetStore(root / "presets"),
                automatic_store=self.automatic_store,
                application_monitor=self.monitor,
                auto_discover=False,
            )
        self.window._device_discovered(self.service.discover())
        self.service.calls.clear()

    def tearDown(self):
        self.window.automatic_profile_timer.stop()
        self.wait_for_jobs()
        self.window._automatic_settings = AutomaticProfileSettings()
        self.window._sync_automatic_profile_monitor()
        self.window.keep_running_checkbox.setChecked(False)
        self.window.dirty = False
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.directory.cleanup()

    def wait_for_jobs(self):
        self.app.processEvents()
        deadline = time.monotonic() + 3
        while ((self.window._job is not None
                or self.window._automatic_scan_job is not None)
               and time.monotonic() < deadline):
            self.app.processEvents()
            time.sleep(0.005)
        self.app.processEvents()
        self.assertIsNone(self.window._job, "Device job did not finish")
        self.assertIsNone(
            self.window._automatic_scan_job, "Foreground scan did not finish"
        )

    @staticmethod
    def settings(*, enabled=True, default=1, target=3):
        rule = ApplicationProfileRule(
            rule_id="game",
            name="Game",
            enabled=True,
            profile_slot=target,
            match_kind="executable_path",
            match_value="/opt/game",
        )
        return AutomaticProfileSettings(
            enabled=enabled,
            default_profile_slot=default,
            rules=(rule,),
        ).normalized()

    def enable(self, *, default=1, target=3):
        self.window._automatic_settings = self.settings(
            default=default, target=target
        )
        self.window._automatic_settings_error = ""
        self.window._invalidate_automatic_profile_context()
        self.window._update_automatic_profile_card()
        self.window.automatic_profile_timer.stop()

    def feed(self, sample):
        marker = object()
        self.window._automatic_scan_job = marker
        self.window._automatic_profile_sampled(
            marker,
            self.window._automatic_revision,
            self.window.selected_device_id,
            sample,
        )
        if self.window._automatic_scan_job is marker:
            self.window._automatic_scan_job = None

    @staticmethod
    def game_sample():
        return ForegroundApplicationSnapshot(
            ForegroundApplicationStatus.AVAILABLE,
            ApplicationIdentity(executable_path="/opt/game"),
        )

    def test_two_identical_samples_debounce_and_same_slot_is_write_free(self):
        self.enable(target=2)
        self.service.active_profile = 2
        self.window.draft.profile_slot = 4
        self.window.draft.lighting.brightness = 37
        self.window.dirty = True
        self.window._saved = True
        self.window.snapshot = self.service.snapshot(4)
        snapshot = self.window.snapshot
        baseline = snapshot["baseline"]
        draft = self.window.draft.to_dict()

        self.feed(self.game_sample())
        self.assertEqual(self.service.calls, [])
        self.assertIn("Confirming", self.window.automatic_profile_status.text())
        self.feed(self.game_sample())
        self.wait_for_jobs()

        self.assertEqual(
            self.service.calls, [("read_active_profile", "test-mc7")]
        )
        self.assertIs(self.window.snapshot, snapshot)
        self.assertIs(self.window.snapshot["baseline"], baseline)
        self.assertEqual(self.window.snapshot["summary"]["active_profile"], 2)
        self.assertEqual(self.window.draft.to_dict(), draft)
        self.assertEqual(self.window.draft.profile_slot, 4)
        self.assertTrue(self.window.dirty)
        self.assertTrue(self.window._saved)
        self.assertIn("already active", self.window.automatic_profile_status.text())
        for _ in range(4):
            self.feed(self.game_sample())
        self.assertEqual(
            self.service.calls, [("read_active_profile", "test-mc7")]
        )

    def test_verified_switch_stops_lcd_and_invalidates_only_device_baseline(self):
        self.enable(target=3)
        self.service.active_profile = 1
        self.window.draft.profile_slot = 4
        self.window.draft.lighting.brightness = 41
        self.window.dirty = True
        self.window._saved = True
        self.window.snapshot = self.service.snapshot(4)
        draft = self.window.draft.to_dict()
        self.window.host_lcd_checkbox.blockSignals(True)
        self.window.host_lcd_checkbox.setChecked(True)
        self.window.host_lcd_checkbox.blockSignals(False)

        self.feed(self.game_sample())
        self.feed(self.game_sample())
        self.wait_for_jobs()

        self.assertEqual(
            self.service.calls,
            [
                ("read_active_profile", "test-mc7"),
                ("switch_profile", "test-mc7", 3),
            ],
        )
        self.assertEqual(self.service.active_profile, 3)
        self.assertFalse(self.window.host_lcd_checkbox.isChecked())
        self.assertIsNone(self.window.snapshot)
        self.assertEqual(self.window.draft.to_dict(), draft)
        self.assertEqual(self.window.draft.profile_slot, 4)
        self.assertTrue(self.window.dirty)
        self.assertTrue(self.window._saved)
        self.assertFalse(self.window.apply_button.isEnabled())
        self.assertIn("Switched to profile 3", self.window.automatic_profile_status.text())

    def test_failures_never_select_default_and_retry_needs_fresh_samples(self):
        self.enable(default=2, target=3)
        unavailable = ForegroundApplicationSnapshot(
            ForegroundApplicationStatus.UNAVAILABLE,
            message="Wayland foreground identity is unavailable",
        )
        no_application = ForegroundApplicationSnapshot(
            ForegroundApplicationStatus.NO_APPLICATION,
        )
        self.feed(unavailable)
        self.feed(unavailable)
        self.feed(no_application)
        self.feed(
            ForegroundApplicationSnapshot(
                ForegroundApplicationStatus.ERROR, message="temporary X11 error"
            )
        )
        self.feed(no_application)
        self.assertEqual(self.service.calls, [])

        self.feed(no_application)
        self.wait_for_jobs()
        self.assertEqual(self.service.calls[-1], ("switch_profile", "test-mc7", 2))

        self.service.calls.clear()
        self.service.failure = "USB read failed"
        self.feed(self.game_sample())
        self.feed(self.game_sample())
        self.wait_for_jobs()
        self.assertNotIn(
            ("switch_profile", "test-mc7", 3), self.service.calls
        )
        self.assertIn("Waiting for fresh", self.window.automatic_profile_status.text())
        self.service.failure = None
        self.service.calls.clear()
        self.feed(self.game_sample())
        self.assertEqual(self.service.calls, [])
        self.feed(self.game_sample())
        self.wait_for_jobs()
        self.assertEqual(self.service.calls[-1], ("switch_profile", "test-mc7", 3))

    def test_busy_and_stale_samples_cannot_start_device_work(self):
        self.enable(target=3)
        marker = object()
        self.window._job = marker
        self.feed(self.game_sample())
        self.window._job = None
        self.assertEqual(self.service.calls, [])

        scan = object()
        self.window._automatic_scan_job = scan
        self.window._automatic_profile_sampled(
            scan,
            self.window._automatic_revision - 1,
            self.window.selected_device_id,
            self.game_sample(),
        )
        self.window._automatic_scan_job = None
        self.assertEqual(self.service.calls, [])
        self.assertEqual(self.window._automatic_candidate_count, 0)

    def test_disable_stops_timer_and_automatic_monitor_keeps_tray_lifecycle(self):
        self.window._automatic_settings = self.settings(enabled=True)
        self.window._invalidate_automatic_profile_context()
        self.window._sync_automatic_profile_monitor()
        self.assertTrue(self.window.automatic_profile_timer.isActive())
        self.assertTrue(self.window.battery_tray.enabled)
        self.assertTrue(self.icon.visible)
        self.assertFalse(self.window.battery_monitor_checkbox.isChecked())
        self.assertTrue(self.window.keep_running_checkbox.isEnabled())

        self.window.keep_running_checkbox.setChecked(True)
        event = QCloseEvent()
        self.window.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertTrue(self.window._hidden_to_tray)
        self.assertTrue(self.window.automatic_profile_timer.isActive())

        self.window._automatic_settings = AutomaticProfileSettings()
        self.window._sync_automatic_profile_monitor()
        self.assertFalse(self.window.automatic_profile_timer.isActive())
        self.assertFalse(self.window.battery_tray.enabled)
        self.assertFalse(self.icon.visible)
        self.assertFalse(self.window.keep_running_checkbox.isChecked())

    def test_rule_dialog_acceptance_persists_and_starts_monitor(self):
        desired = self.settings(enabled=True, default=2, target=4)
        with patch(
            "swarm2.gui.automatic_profiles.AutomaticProfilesDialog"
        ) as dialog_type:
            dialog = dialog_type.return_value
            dialog.exec.return_value = QDialog.DialogCode.Accepted
            dialog.result_settings = desired
            self.window.manage_automatic_profiles()
            dialog_type.assert_called_once_with(
                AutomaticProfileSettings(), self.window
            )
            dialog.deleteLater.assert_called_once_with()
        self.assertEqual(self.automatic_store.load(), desired)
        self.assertEqual(self.window._automatic_settings, desired)
        self.assertTrue(self.window.automatic_profile_timer.isActive())
        self.assertIn("On", self.window.automatic_profile_summary.text())


if __name__ == "__main__":
    unittest.main()
