"""Firmware UI boundaries with fake workers; no network or device access."""

import copy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from swarm2.configuration import PresetStore
from swarm2.firmware_catalog import release_by_key
from tests import test_gui as gui_fixture

if gui_fixture.MainWindow is not None:
    from PySide6.QtGui import QCloseEvent
    from swarm2.gui.firmware import FirmwareDialog


class FakeFirmwareService:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.gate = None
        self.entered = threading.Event()
        self.status = {"firmware_version": "5.04", "firmware_catalog_version": "5.4.0.0"}
        self.preparation = {"can_update": True, "summary": "Five profiles backed up; target mouse 5.9.0.0",
                            "release_key": "mouse:5.9.0.0", "plan_path": "/fake/plan.json"}
        self.installation = {"verified": True, "firmware_version": "5.09",
                             "backup_path": "/fake/settings-backup.json"}

    def _called(self, *args):
        self.calls.append(args)
        self.entered.set()
        if self.gate is not None and not self.gate.wait(3):
            raise RuntimeError("Fake firmware worker timed out")
        if self.failure:
            raise RuntimeError(self.failure)

    def read_status(self, device_id):
        self._called("read_status", device_id)
        return copy.deepcopy(self.status)

    @staticmethod
    def package(release_key, path):
        release = release_by_key(release_key)
        if release is None:
            raise AssertionError(f"Unknown fixture release {release_key}")
        return {"role": release.role, "release_key": release.key, "path": str(path),
                "version": release.package_version, "size": release.size,
                "sha256": release.sha256,
                "installation_supported": release.installation_supported}

    def download(self, release_key, path):
        self._called("download", release_key, path)
        return self.package(release_key, path)

    def inspect(self, release_key, path):
        self._called("inspect", release_key, path)
        return self.package(release_key, path)

    def prepare(self, device_id, release_key, path, backup_directory):
        self._called("prepare", device_id, release_key, path, backup_directory)
        return copy.deepcopy(self.preparation)

    def install(self, prepared, progress):
        progress({"type": "progress", "phase": "transferring", "percent": 25})
        self._called("install", copy.deepcopy(prepared))
        return copy.deepcopy(self.installation)


@unittest.skipIf(gui_fixture.MainWindow is None, "Install the gui extra to exercise Qt")
class FirmwareDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = gui_fixture.QApplication.instance() or gui_fixture.QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = FakeFirmwareService()
        self.dialog = FirmwareDialog(self.service, "test-mc7")
        self.wait_for_task()

    def tearDown(self):
        if self.service.gate is not None:
            self.service.gate.set()
        self.wait_for_task()
        self.dialog.close()
        self.dialog.deleteLater()
        self.app.processEvents()
        self.directory.cleanup()

    def wait_for_task(self):
        # Initial status work starts from a zero-delay timer, so process events
        # before checking whether the dialog has an active task.
        self.app.processEvents()
        deadline = time.monotonic() + 4
        while self.dialog.task is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.app.processEvents()
        self.assertIsNone(self.dialog.task, "Fake firmware task did not finish")

    def choose_release(self, release_key):
        index = self.dialog.release_choice.findData(release_key)
        self.assertGreaterEqual(index, 0)
        self.dialog.release_choice.setCurrentIndex(index)

    def download(self, release_key="mouse:5.9.0.0"):
        self.choose_release(release_key)
        path = str(Path(self.directory.name) / f"selected {release_key.replace(':', '-')}.7z")
        with patch("swarm2.gui.firmware.QFileDialog.getSaveFileName", return_value=(path, "")):
            self.dialog.download_button.click()
        self.wait_for_task()
        return path

    def prepare(self):
        with patch("swarm2.gui.firmware.QFileDialog.getExistingDirectory", return_value=self.directory.name):
            self.dialog.prepare_button.click()
        self.wait_for_task()

    def test_open_reads_installed_version_without_preparation_or_installation(self):
        self.assertEqual(self.service.calls, [("read_status", "test-mc7")])
        self.assertIn("5.04", self.dialog.installed.text())
        self.assertIn("5.4.0.0", self.dialog.installed.text())
        self.assertFalse(self.dialog.prepare_button.isEnabled())
        self.assertFalse(self.dialog.update_button.isEnabled())
        self.assertFalse(self.dialog.device_may_have_changed)
        self.assertIsNone(self.dialog.outcome)
        self.assertIn("catalog dated", self.dialog.release_detail.text())

    def test_offline_dialog_downloads_without_any_device_status_or_prepare(self):
        self.dialog.deleteLater()
        self.app.processEvents()
        self.service.calls.clear()
        self.dialog = FirmwareDialog(self.service)
        path = self.download()
        self.assertEqual(self.service.calls, [("download", "mouse:5.9.0.0", path)])
        self.assertFalse(self.dialog.prepare_button.isEnabled())
        self.assertFalse(self.dialog.update_button.isEnabled())

    def test_download_uses_selected_role_and_exact_user_path_without_flash(self):
        for release_key in ("mouse:5.9.0.0", "transmitter:5.4.0.0"):
            with self.subTest(release_key=release_key):
                self.service.calls.clear()
                path = self.download(release_key)
                self.assertEqual(self.service.calls, [("download", release_key, path)])
                self.assertEqual(self.dialog.package, self.service.package(release_key, path))
                self.assertIn(path, self.dialog.package_detail.text())
                self.assertIn(self.dialog.package["sha256"], self.dialog.package_detail.text())
                self.assertIn("No firmware has been installed", self.dialog.status.text())
                self.assertFalse(self.dialog.device_may_have_changed)
                self.assertFalse(self.dialog.update_button.isEnabled())
                self.assertEqual(self.dialog.prepare_button.isEnabled(),
                                 release_key == "mouse:5.9.0.0")

    def test_open_validates_selected_role_and_path_before_preparation(self):
        self.choose_release("transmitter:5.4.0.0")
        path = str(Path(self.directory.name) / "existing transmitter.7z")
        with patch("swarm2.gui.firmware.QFileDialog.getOpenFileName", return_value=(path, "")):
            self.dialog.open_button.click()
        self.wait_for_task()
        self.assertEqual(self.service.calls[-1],
                         ("inspect", "transmitter:5.4.0.0", path))
        self.assertEqual(self.dialog.package["role"], "transmitter")
        self.assertFalse(self.dialog.prepare_button.isEnabled())
        with patch("swarm2.gui.firmware.QFileDialog.getExistingDirectory") as chooser:
            self.dialog.prepare()
        chooser.assert_not_called()
        self.assertFalse(any(call[0] == "prepare" for call in self.service.calls))

    def test_cancelling_file_choosers_performs_no_background_operation(self):
        calls = list(self.service.calls)
        with patch("swarm2.gui.firmware.QFileDialog.getSaveFileName", return_value=("", "")):
            self.dialog.download_button.click()
        with patch("swarm2.gui.firmware.QFileDialog.getOpenFileName", return_value=("", "")):
            self.dialog.open_button.click()
        self.app.processEvents()
        self.assertEqual(self.service.calls, calls)
        self.assertIsNone(self.dialog.task)
        self.assertIsNone(self.dialog.package)

    def test_prepare_uses_mouse_identity_verified_path_and_chosen_backup_directory(self):
        path = self.download()
        self.service.calls.clear()
        self.prepare()
        self.assertEqual(self.service.calls, [("prepare", "test-mc7", "mouse:5.9.0.0",
                                               path, self.directory.name)])
        self.assertEqual(self.dialog.prepared, self.service.preparation)
        self.assertTrue(self.dialog.update_button.isEnabled())
        self.assertFalse(self.dialog.device_may_have_changed)
        self.assertIsNone(self.dialog.outcome)

    def test_preparation_refusal_keeps_update_disabled(self):
        self.download()
        self.service.preparation = {"can_update": False, "summary": "Update unavailable",
                                    "reason": "Installed firmware is already the available version"}
        self.prepare()
        self.assertFalse(self.dialog.update_button.isEnabled())
        self.assertIn("already the available version", self.dialog.status.text())
        self.assertFalse(self.dialog.device_may_have_changed)

    def test_package_change_invalidates_prepared_update(self):
        self.download()
        self.prepare()
        self.choose_release("transmitter:5.4.0.0")
        self.assertIsNone(self.dialog.prepared)
        self.assertIsNone(self.dialog.package)
        self.assertFalse(self.dialog.update_button.isEnabled())
        self.assertFalse(self.dialog.prepare_button.isEnabled())
        self.assertIn("transmitter", self.dialog.release_detail.text())
        self.assertIn("10F5:502E", self.dialog.release_detail.text())

    def test_original_archive_downloads_and_inspects_but_cannot_prepare(self):
        path = self.download("mouse:5.4.0.0")
        self.assertEqual(self.service.calls[-1],
                         ("download", "mouse:5.4.0.0", path))
        self.assertFalse(self.dialog.package["installation_supported"])
        self.assertFalse(self.dialog.prepare_button.isEnabled())
        self.assertIn("Installation remains disabled", self.dialog.preparation_detail.text())
        with patch("swarm2.gui.firmware.QFileDialog.getExistingDirectory") as chooser:
            self.dialog.prepare()
        chooser.assert_not_called()
        self.assertFalse(any(call[0] == "prepare" for call in self.service.calls))

    def test_new_download_clears_old_plan_before_worker_finishes(self):
        self.download()
        self.prepare()
        self.service.gate = threading.Event()
        self.service.entered.clear()
        with patch("swarm2.gui.firmware.QFileDialog.getSaveFileName", return_value=("/fake/new.7z", "")):
            self.dialog.download_button.click()
        self.assertTrue(self.service.entered.wait(1))
        self.assertIsNone(self.dialog.prepared)
        self.assertFalse(self.dialog.update_button.isEnabled())
        self.service.gate.set()
        self.wait_for_task()
        self.assertIsNone(self.dialog.prepared)
        self.assertFalse(self.dialog.update_button.isEnabled())

    def test_background_failure_does_not_present_verified_package_or_enable_update(self):
        self.service.failure = "Official firmware archive failed checksum verification"
        self.download()
        self.assertIsNone(self.dialog.package)
        self.assertIsNone(self.dialog.prepared)
        self.assertFalse(self.dialog.prepare_button.isEnabled())
        self.assertFalse(self.dialog.update_button.isEnabled())
        self.assertIn("checksum verification", self.dialog.status.text())
        self.assertNotIn("Verified mouse package", self.dialog.package_detail.text())
        self.assertFalse(self.dialog.device_may_have_changed)

    def test_busy_worker_blocks_close_and_duplicate_jobs(self):
        self.service.gate = threading.Event()
        self.service.entered.clear()
        self.dialog.refresh_status()
        self.assertTrue(self.service.entered.wait(1))
        self.dialog.show()
        self.app.processEvents()
        calls = list(self.service.calls)
        self.dialog.refresh_status()
        self.dialog.reject()
        event = QCloseEvent()
        self.dialog.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertTrue(self.dialog.isVisible())
        for widget in (self.dialog.release_choice, self.dialog.download_button,
                       self.dialog.open_button, self.dialog.close_button,
                       self.dialog.prepare_button, self.dialog.update_button):
            self.assertFalse(widget.isEnabled())
        self.assertEqual(self.service.calls, calls)
        self.service.gate.set()
        self.wait_for_task()
        self.assertTrue(self.dialog.close_button.isEnabled())
        self.dialog.reject()
        self.assertFalse(self.dialog.isVisible())

    def test_only_explicit_update_click_starts_once_and_consumes_preparation(self):
        self.dialog.install()
        self.assertFalse(any(call[0] == "install" for call in self.service.calls))
        self.download()
        self.prepare()
        prepared = copy.deepcopy(self.dialog.prepared)
        self.assertFalse(any(call[0] == "install" for call in self.service.calls))
        self.service.gate = threading.Event()
        self.service.entered.clear()
        self.dialog.update_button.click()
        self.assertTrue(self.service.entered.wait(1))
        self.app.processEvents()
        self.assertTrue(self.dialog.device_may_have_changed)
        self.assertIsNone(self.dialog.prepared)
        self.assertIsNone(self.dialog.outcome)
        self.assertFalse(self.dialog.update_button.isEnabled())
        self.assertIn("25%", self.dialog.status.text())
        self.dialog.install()
        self.dialog.reject()
        event = QCloseEvent()
        self.dialog.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertEqual([call for call in self.service.calls if call[0] == "install"],
                         [("install", prepared)])
        self.service.gate.set()
        self.wait_for_task()
        self.assertEqual(self.dialog.outcome, self.service.installation)
        self.assertIn("5.09", self.dialog.installed.text())
        self.assertIn("installed and verified", self.dialog.status.text())
        self.assertIn("/fake/settings-backup.json", self.dialog.status.text())
        self.assertFalse(self.dialog.update_button.isEnabled())
        self.dialog.install()
        self.assertEqual(len([call for call in self.service.calls if call[0] == "install"]), 1)

    def test_install_failure_requires_new_preparation_and_leaves_state_uncertain(self):
        self.download()
        self.prepare()
        self.service.failure = "The firmware helper stopped without a complete result"
        self.dialog.update_button.click()
        self.wait_for_task()
        self.assertTrue(self.dialog.device_may_have_changed)
        self.assertIsNone(self.dialog.prepared)
        self.assertIsNone(self.dialog.outcome)
        self.assertIn("without a complete result", self.dialog.status.text())
        self.assertFalse(self.dialog.update_button.isEnabled())
        self.assertNotIn("5.09", self.dialog.installed.text())

    def test_unverified_terminal_result_is_not_presented_as_success(self):
        self.download()
        self.prepare()
        self.service.installation = {"verified": False, "firmware_version": "5.09",
                                     "backup_path": "/fake/backup.json"}
        self.dialog.update_button.click()
        self.wait_for_task()
        self.assertIsNone(self.dialog.outcome)
        self.assertIsNone(self.dialog.prepared)
        self.assertTrue(self.dialog.device_may_have_changed)
        self.assertFalse(self.dialog.update_button.isEnabled())
        self.assertIn("could not be verified", self.dialog.status.text())
        self.assertNotIn("5.09", self.dialog.installed.text())


class FirmwareDeviceService(gui_fixture.FakeService):
    def __init__(self):
        super().__init__()
        self.capabilities.append("read_status")
        self.status = {"firmware_version": "5.04", "firmware_catalog_version": "5.4.0.0",
                       "battery_percent": 72, "charging": False}

    def read_status(self, device_id):
        self.calls.append(("read_status", device_id))
        if self.failure:
            raise RuntimeError(self.failure)
        return copy.deepcopy(self.status)


@unittest.skipIf(gui_fixture.MainWindow is None, "Install the gui extra to exercise Qt")
class FirmwareMainWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = gui_fixture.QApplication.instance() or gui_fixture.QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = FirmwareDeviceService()
        self.window = gui_fixture.MainWindow(service=self.service,
            store=PresetStore(Path(self.directory.name) / "presets"), auto_discover=False)
        self.window.navigation.setCurrentRow(1)
        self.window.refresh_device()
        self.wait_for_job()
        self.window.read_mouse()
        self.wait_for_job()
        self.window.draft.lighting.brightness = 37
        self.window.draft.sensor.stages[0].value = 1250
        self.window.dirty = True

    def tearDown(self):
        self.window._firmware_dialog = None
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
        self.assertIsNone(self.window._job)

    def test_overview_version_updates_and_clears_without_changing_pending_edits(self):
        draft = self.window.draft.to_dict()
        snapshot = self.window.snapshot
        self.assertIn("5.04", self.window.overview_firmware.text())
        self.window._status_received("other-device", {"firmware_version": "9.99"})
        self.assertIn("5.04", self.window.overview_firmware.text())
        self.window._status_received("test-mc7", {"firmware_version": "5.09"})
        self.assertIn("5.09", self.window.overview_firmware.text())
        self.window._status_failed("test-mc7", "Mouse disconnected")
        self.assertNotIn("5.09", self.window.overview_firmware.text())
        self.assertIn("not reported", self.window.overview_firmware.text())
        self.assertEqual(self.window.draft.to_dict(), draft)
        self.assertIs(self.window.snapshot, snapshot)
        self.assertTrue(self.window.dirty)

    def test_open_close_preserves_snapshot_draft_and_selected_target(self):
        draft, snapshot = self.window.draft.to_dict(), self.window.snapshot
        calls = copy.deepcopy(self.service.calls)
        with patch("swarm2.gui.firmware.FirmwareDialog") as factory:
            dialog = factory.return_value
            dialog.device_may_have_changed = False
            dialog.outcome = None
            self.window.manage_firmware()
            args = factory.call_args.args
            self.assertIs(args[0].devices, self.service)
            self.assertEqual(args[1:], ("test-mc7", self.window))
            dialog.exec.assert_called_once_with()
        self.assertIsNone(self.window._firmware_dialog)
        self.assertIs(self.window.snapshot, snapshot)
        self.assertEqual(self.window.draft.to_dict(), draft)
        self.assertEqual(self.service.calls, calls)
        self.assertTrue(self.window.dirty)

    def test_download_failure_in_real_dialog_preserves_mainwindow_draft_and_snapshot(self):
        draft, snapshot = self.window.draft.to_dict(), self.window.snapshot
        firmware_service = FakeFirmwareService()
        firmware_service.failure = "Official server is unavailable"

        def exercise_dialog(dialog):
            self.app.processEvents()
            deadline = time.monotonic() + 3
            while dialog.task is not None and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.005)
            with patch("swarm2.gui.firmware.QFileDialog.getSaveFileName", return_value=("/fake/mouse.7z", "")):
                dialog.download_button.click()
            deadline = time.monotonic() + 3
            while dialog.task is not None and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.005)
            self.app.processEvents()
            self.assertIsNone(dialog.task)
            self.assertIn("server is unavailable", dialog.status.text())
            self.assertFalse(dialog.device_may_have_changed)
            return gui_fixture.QDialog.DialogCode.Rejected

        with patch("swarm2.firmware_service.FirmwareService", return_value=firmware_service), \
             patch("swarm2.gui.firmware.FirmwareDialog.exec", exercise_dialog):
            self.window.manage_firmware()
        self.assertEqual(firmware_service.calls, [("read_status", "test-mc7"),
                                                  ("download", "mouse:5.9.0.0", "/fake/mouse.7z")])
        self.assertEqual(self.window.draft.to_dict(), draft)
        self.assertIs(self.window.snapshot, snapshot)
        self.assertTrue(self.window.dirty)
        self.assertTrue(self.window.apply_button.isEnabled())

    def test_attempted_update_invalidates_snapshot_and_status_but_preserves_draft(self):
        for verified in (True, False):
            with self.subTest(verified=verified):
                self.window.snapshot = self.service.snapshot()
                self.window._set_device_status(self.service.status)
                draft = self.window.draft.to_dict()
                calls = copy.deepcopy(self.service.calls)
                with patch("swarm2.gui.firmware.FirmwareDialog") as factory:
                    dialog = factory.return_value
                    dialog.device_may_have_changed = True
                    dialog.outcome = {"verified": verified}
                    self.window.manage_firmware()
                self.assertIsNone(self.window.snapshot)
                self.assertIn("not reported", self.window.overview_firmware.text())
                self.assertFalse(self.window.apply_button.isEnabled())
                self.assertEqual(self.window.draft.to_dict(), draft)
                self.assertEqual(self.service.calls, calls)
                self.assertTrue(self.window.dirty)
                self.assertEqual(self.window.message.objectName() == "error", not verified)

    def test_no_connected_status_capability_opens_download_only(self):
        self.window.devices[0]["capabilities"] = []
        with patch("swarm2.gui.firmware.FirmwareDialog") as factory:
            factory.return_value.device_may_have_changed = False
            factory.return_value.outcome = None
            self.window.manage_firmware()
            self.assertIsNone(factory.call_args.args[1])

    def test_calibration_or_existing_worker_prevents_opening_second_dialog(self):
        for attribute in ("_dcu_dialog", "_firmware_dialog", "_job"):
            with self.subTest(attribute=attribute):
                setattr(self.window, attribute, object())
                try:
                    with patch("swarm2.gui.firmware.FirmwareDialog") as factory:
                        self.window.manage_firmware()
                        factory.assert_not_called()
                finally:
                    setattr(self.window, attribute, None)
