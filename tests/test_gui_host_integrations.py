"""Exercise the host integration dialog with fakes and no system writes."""

import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication, QMessageBox
    from swarm2.gui.app import MainWindow
    from swarm2.gui.host_integrations import HostIntegrationsDialog
except ModuleNotFoundError as error:
    if error.name and error.name.startswith("PySide6"):
        HostIntegrationsDialog = None
        MainWindow = None
    else:
        raise

from swarm2.autostart import AutostartState, AutostartStatus
from swarm2.host_integrations import (
    GnomeExtensionStatus,
    HostIntegrationSnapshot,
    IntegrationState,
    UdevRuleStatus,
)
from swarm2.automatic_profiles import AutomaticProfileStore
from swarm2.configuration import PresetStore


def snapshot(*, autostart="missing", gnome="missing", udev="missing"):
    autostart_values = {
        "missing": AutostartStatus(
            AutostartState.MISSING,
            "Start-at-login entry missing.",
            "/home/test/.config/autostart/swarm2-mc7.desktop",
            False,
            False,
            True,
            False,
        ),
        "current": AutostartStatus(
            AutostartState.CURRENT,
            "Start-at-login entry current.",
            "/home/test/.config/autostart/swarm2-mc7.desktop",
            True,
            True,
            True,
            True,
        ),
        "not_applicable": AutostartStatus(
            AutostartState.NOT_APPLICABLE,
            "Start at login is unavailable.",
            None,
            False,
            False,
            False,
            False,
        ),
    }
    gnome_values = {
        "missing": GnomeExtensionStatus(
            IntegrationState.MISSING,
            "Companion missing.",
            "/home/test/.local/share/gnome-shell/extensions/test",
            False,
            False,
            False,
            False,
            True,
            False,
        ),
        "ready": GnomeExtensionStatus(
            IntegrationState.CURRENT,
            "Companion current; enable it.",
            "/home/test/.local/share/gnome-shell/extensions/test",
            True,
            True,
            True,
            False,
            True,
            True,
        ),
        "enabled": GnomeExtensionStatus(
            IntegrationState.CURRENT,
            "Companion enabled.",
            "/home/test/.local/share/gnome-shell/extensions/test",
            True,
            True,
            True,
            True,
            True,
            False,
        ),
        "pending": GnomeExtensionStatus(
            IntegrationState.CURRENT,
            "Log out and back in.",
            "/home/test/.local/share/gnome-shell/extensions/test",
            True,
            True,
            False,
            False,
            True,
            False,
        ),
    }
    udev_values = {
        "missing": UdevRuleStatus(
            IntegrationState.MISSING,
            "Rule missing.",
            None,
            False,
            False,
            True,
        ),
        "current": UdevRuleStatus(
            IntegrationState.CURRENT,
            "Rule installed and current.",
            "/etc/udev/rules.d/70-swarm2-mc7.rules",
            True,
            True,
            True,
        ),
    }
    return HostIntegrationSnapshot(
        autostart_values[autostart], gnome_values[gnome], udev_values[udev]
    )


class FakeManager:
    def __init__(self, value=None):
        self.value = value or snapshot()
        self.calls = []

    def check_all(self):
        self.calls.append("check")
        return self.value

    def install_gnome_extension(self):
        self.calls.append("install_gnome")
        self.value = snapshot(gnome="pending")
        return self.value

    def install_autostart(self):
        self.calls.append("install_autostart")
        self.value = snapshot(autostart="current")
        return self.value

    def remove_autostart(self):
        self.calls.append("remove_autostart")
        self.value = snapshot(autostart="missing")
        return self.value

    def enable_gnome_extension(self):
        self.calls.append("enable_gnome")
        self.value = snapshot(gnome="enabled")
        return self.value

    def install_udev_rule(self):
        self.calls.append("install_udev")
        self.value = snapshot(udev="current")
        return self.value


@unittest.skipIf(
    HostIntegrationsDialog is None, "Install the gui extra to exercise Qt"
)
class HostIntegrationsDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.manager = FakeManager()
        self.dialog = HostIntegrationsDialog(self.manager)
        self.wait()

    def tearDown(self):
        self.wait()
        self.dialog.close()
        self.dialog.deleteLater()
        self.app.processEvents()

    def wait(self):
        deadline = time.monotonic() + 3
        self.app.processEvents()
        while getattr(self, "dialog", None) is not None and self.dialog.task is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.app.processEvents()
        if getattr(self, "dialog", None) is not None:
            self.assertIsNone(self.dialog.task, "Host integration task did not finish")

    def test_initial_check_enables_only_relevant_actions(self):
        self.assertEqual(self.manager.calls, ["check"])
        self.assertTrue(self.dialog.autostart_install_button.isEnabled())
        self.assertFalse(self.dialog.autostart_remove_button.isEnabled())
        self.assertTrue(self.dialog.gnome_install_button.isEnabled())
        self.assertFalse(self.dialog.gnome_enable_button.isEnabled())
        self.assertTrue(self.dialog.udev_install_button.isEnabled())
        self.assertIn("Companion missing", self.dialog.gnome_status.text())
        self.assertIn("Rule missing", self.dialog.udev_status.text())

    def test_install_and_remove_start_at_login_are_explicit(self):
        self.dialog.autostart_install_button.click()
        self.wait()
        self.assertIn("install_autostart", self.manager.calls)
        self.assertFalse(self.dialog.autostart_install_button.isEnabled())
        self.assertTrue(self.dialog.autostart_remove_button.isEnabled())
        self.assertIn("next sign-in", self.dialog.operation_status.text())

        self.dialog.autostart_remove_button.click()
        self.wait()
        self.assertIn("remove_autostart", self.manager.calls)
        self.assertTrue(self.dialog.autostart_install_button.isEnabled())
        self.assertFalse(self.dialog.autostart_remove_button.isEnabled())
        self.assertIn("no longer", self.dialog.operation_status.text())

    def test_unsupported_platform_disables_start_at_login_actions(self):
        self.manager.value = snapshot(autostart="not_applicable")
        self.dialog.refresh()
        self.wait()
        self.assertFalse(self.dialog.autostart_install_button.isEnabled())
        self.assertFalse(self.dialog.autostart_remove_button.isEnabled())
        self.assertIn("unavailable", self.dialog.autostart_status.text())

    def test_start_at_login_failure_keeps_the_checked_state(self):
        self.manager.install_autostart = MagicMock(
            side_effect=RuntimeError("The login directory is read-only.")
        )
        before = self.dialog.snapshot
        self.dialog.autostart_install_button.click()
        self.wait()
        self.assertIs(self.dialog.snapshot, before)
        self.assertTrue(self.dialog.autostart_install_button.isEnabled())
        self.assertFalse(self.dialog.autostart_remove_button.isEnabled())
        self.assertEqual(self.dialog.operation_status.objectName(), "error")
        self.assertIn("read-only", self.dialog.operation_status.text())

    def test_installing_companion_does_not_enable_it(self):
        self.dialog.gnome_install_button.click()
        self.wait()
        self.assertEqual(self.manager.calls, ["check", "install_gnome"])
        self.assertNotIn("enable_gnome", self.manager.calls)
        self.assertFalse(self.dialog.gnome_enable_button.isEnabled())
        self.assertIn("not enabled", self.dialog.operation_status.text())
        self.assertIn("Log out and back in", self.dialog.gnome_status.text())

    def test_enable_is_a_separate_confirmed_action(self):
        self.manager.value = snapshot(gnome="ready")
        self.dialog.refresh()
        self.wait()
        self.assertTrue(self.dialog.gnome_enable_button.isEnabled())
        with patch.object(
            QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            self.dialog.gnome_enable_button.click()
        self.wait()
        self.assertIn("enable_gnome", self.manager.calls)
        self.assertIn("enabled", self.dialog.operation_status.text())

    def test_udev_install_requires_confirmation(self):
        with patch.object(
            QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.Cancel,
        ):
            self.dialog.udev_install_button.click()
        self.assertNotIn("install_udev", self.manager.calls)

        with patch.object(
            QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            self.dialog.udev_install_button.click()
        self.wait()
        self.assertIn("install_udev", self.manager.calls)
        self.assertIn("Reconnect", self.dialog.operation_status.text())
        self.assertFalse(self.dialog.udev_install_button.isEnabled())


@unittest.skipIf(MainWindow is None, "Install the gui extra to exercise Qt")
class HostIntegrationsMainWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_device_page_opens_dialog_with_injected_manager(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = FakeManager()
            window = MainWindow(
                service=object(),
                store=PresetStore(Path(directory) / "presets"),
                automatic_store=AutomaticProfileStore(Path(directory) / "automatic.json"),
                host_integration_manager=manager,
                auto_discover=False,
            )
            dialog = MagicMock()
            dialog.exec.return_value = 0
            with patch(
                "swarm2.gui.host_integrations.HostIntegrationsDialog",
                return_value=dialog,
            ) as factory:
                window.host_integrations_button.click()
            factory.assert_called_once_with(manager, window)
            dialog.exec.assert_called_once_with()
            dialog.deleteLater.assert_called_once_with()
            self.assertIsNone(window._host_integrations_dialog)
            window.dirty = False
            window.close()
            window.deleteLater()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
