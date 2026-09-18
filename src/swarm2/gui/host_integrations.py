"""In-app checks and explicit installers for desktop integration."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..autostart import AutostartState
from ..host_integrations import (
    HostIntegrationManager,
    HostIntegrationSnapshot,
    IntegrationState,
)
from .widgets import STYLE, card, label


class HostIntegrationTask(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, operation: Callable[[], object], parent=None):
        super().__init__(parent)
        self.operation = operation

    def run(self):
        try:
            self.completed.emit(self.operation())
        except Exception as error:
            self.failed.emit(str(error))


class HostIntegrationsDialog(QDialog):
    """Inspect host setup without touching the mouse or changing a local draft."""

    def __init__(self, manager=None, parent=None):
        super().__init__(parent)
        self.manager = manager if manager is not None else HostIntegrationManager()
        self.task: HostIntegrationTask | None = None
        self.snapshot: HostIntegrationSnapshot | None = None
        self.setWindowTitle("Host integration")
        self.resize(760, 820)
        self.setMinimumWidth(620)
        self.setStyleSheet(STYLE)

        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(28, 26, 28, 24)
        body.setSpacing(16)
        scroll.setWidget(content)
        outer.addWidget(scroll)

        body.addWidget(label("Host integration", "title"))
        body.addWidget(
            label(
                "Manage start-at-login, the optional GNOME Wayland companion and "
                "the Linux USB access rule. These actions do not open the mouse "
                "or change its settings.",
                "muted",
                True,
            )
        )

        frame, section = card(
            "Start at login",
            "Open MC7 Studio when you sign in. This setting applies only to this user.",
        )
        self.autostart_status = label("Not checked yet.", "muted", True)
        self.autostart_status.setAccessibleName("Start at login status")
        self.autostart_status.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        section.addWidget(self.autostart_status)
        self.autostart_path = label("Entry path: not checked.", "muted", True)
        self.autostart_path.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        section.addWidget(self.autostart_path)
        autostart_actions = QHBoxLayout()
        self.autostart_install_button = QPushButton(
            "Install or update start-at-login entry"
        )
        self.autostart_install_button.clicked.connect(self.install_autostart)
        self.autostart_remove_button = QPushButton("Remove start-at-login entry")
        self.autostart_remove_button.clicked.connect(self.remove_autostart)
        autostart_actions.addWidget(self.autostart_install_button)
        autostart_actions.addWidget(self.autostart_remove_button)
        autostart_actions.addStretch()
        section.addLayout(autostart_actions)
        body.addWidget(frame)

        frame, section = card(
            "GNOME Wayland automatic profiles",
            "The per-user companion reports only the focused window process ID. "
            "Installation does not enable it.",
        )
        self.gnome_status = label("Not checked yet.", "muted", True)
        self.gnome_status.setAccessibleName("GNOME companion status")
        self.gnome_status.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        section.addWidget(self.gnome_status)
        self.gnome_path = label("Installation path: not checked.", "muted", True)
        self.gnome_path.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        section.addWidget(self.gnome_path)
        gnome_actions = QHBoxLayout()
        self.gnome_install_button = QPushButton("Install or update companion")
        self.gnome_install_button.clicked.connect(self.install_gnome)
        self.gnome_enable_button = QPushButton("Enable companion")
        self.gnome_enable_button.clicked.connect(self.enable_gnome)
        gnome_actions.addWidget(self.gnome_install_button)
        gnome_actions.addWidget(self.gnome_enable_button)
        gnome_actions.addStretch()
        section.addLayout(gnome_actions)
        body.addWidget(frame)

        frame, section = card(
            "MC7 USB access",
            "The udev rule grants the signed-in desktop user access to supported MC7 HID and USB interfaces.",
        )
        self.udev_status = label("Not checked yet.", "muted", True)
        self.udev_status.setAccessibleName("MC7 udev rule status")
        self.udev_status.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        section.addWidget(self.udev_status)
        self.udev_path = label("Effective rule: not checked.", "muted", True)
        self.udev_path.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        section.addWidget(self.udev_path)
        self.udev_install_button = QPushButton("Install or update udev rule…")
        self.udev_install_button.clicked.connect(self.install_udev)
        section.addWidget(
            self.udev_install_button, 0, Qt.AlignmentFlag.AlignLeft
        )
        body.addWidget(frame)

        self.operation_status = label("Checking this computer…", "notice", True)
        self.operation_status.setAccessibleName("Host integration operation status")
        body.addWidget(self.operation_status)
        actions = QHBoxLayout()
        self.check_button = QPushButton("Check again")
        self.check_button.clicked.connect(self.refresh)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.reject)
        actions.addWidget(self.check_button)
        actions.addStretch()
        actions.addWidget(self.close_button)
        body.addLayout(actions)
        body.addStretch()

        self._controls()
        QTimer.singleShot(0, self.refresh)

    def _controls(self):
        idle = self.task is None
        autostart = self.snapshot.autostart if self.snapshot is not None else None
        gnome = self.snapshot.gnome if self.snapshot is not None else None
        udev = self.snapshot.udev if self.snapshot is not None else None
        self.check_button.setEnabled(idle)
        self.close_button.setEnabled(idle)
        self.autostart_install_button.setEnabled(
            bool(
                idle
                and autostart
                and autostart.can_install
                and autostart.state
                in (AutostartState.MISSING, AutostartState.NEEDS_UPDATE)
            )
        )
        self.autostart_remove_button.setEnabled(
            bool(idle and autostart and autostart.can_remove)
        )
        self.gnome_install_button.setEnabled(
            bool(
                idle
                and gnome
                and gnome.can_install
                and gnome.state
                in (IntegrationState.MISSING, IntegrationState.NEEDS_UPDATE)
            )
        )
        self.gnome_enable_button.setEnabled(
            bool(idle and gnome and gnome.can_enable)
        )
        self.udev_install_button.setEnabled(
            bool(
                idle
                and udev
                and udev.can_install
                and udev.state
                in (IntegrationState.MISSING, IntegrationState.NEEDS_UPDATE)
            )
        )

    def _run(self, operation, completed, status):
        if self.task is not None:
            return
        self._set_operation_status(status)
        self.task = HostIntegrationTask(operation, self)
        self.task.completed.connect(completed)
        self.task.failed.connect(self._failed)
        self.task.finished.connect(self._finished)
        self._controls()
        self.task.start()

    def _finished(self):
        task, self.task = self.task, None
        if task is not None:
            task.deleteLater()
        self._controls()

    def _failed(self, message):
        self._set_operation_status(
            message or "The host integration operation failed.", error=True
        )

    def _set_operation_status(self, text, *, error=False):
        self.operation_status.setText(text)
        self.operation_status.setObjectName("error" if error else "notice")
        self.operation_status.style().unpolish(self.operation_status)
        self.operation_status.style().polish(self.operation_status)

    def _show_snapshot(self, snapshot, message="Host integration check completed."):
        if not isinstance(snapshot, HostIntegrationSnapshot):
            self._failed("The host integration check returned an invalid result.")
            return
        self.snapshot = snapshot
        self.autostart_status.setText(snapshot.autostart.detail)
        self.autostart_path.setText(
            f"Entry path: {snapshot.autostart.path}"
            if snapshot.autostart.path
            else "Entry path: not applicable."
        )
        self.gnome_status.setText(snapshot.gnome.detail)
        self.gnome_path.setText(
            f"Installation path: {snapshot.gnome.destination}"
            if snapshot.gnome.destination
            else "Installation path: not applicable."
        )
        self.udev_status.setText(snapshot.udev.detail)
        self.udev_path.setText(
            f"Effective rule: {snapshot.udev.path}"
            if snapshot.udev.path
            else "Effective rule: none."
        )
        self._set_operation_status(message)
        self._controls()

    def refresh(self):
        self._run(
            self.manager.check_all,
            self._show_snapshot,
            "Checking this computer…",
        )

    def install_autostart(self):
        if not self.autostart_install_button.isEnabled():
            return
        self._run(
            self.manager.install_autostart,
            lambda result: self._show_snapshot(
                result,
                "The per-user start-at-login entry is installed. The operating "
                "system is expected to load it at the next sign-in.",
            ),
            "Installing the start-at-login entry for this user…",
        )

    def remove_autostart(self):
        if not self.autostart_remove_button.isEnabled():
            return
        self._run(
            self.manager.remove_autostart,
            lambda result: self._show_snapshot(
                result,
                "MC7 Studio will no longer start automatically for this user.",
            ),
            "Removing the start-at-login entry for this user…",
        )

    def install_gnome(self):
        if not self.gnome_install_button.isEnabled():
            return
        self._run(
            self.manager.install_gnome_extension,
            lambda result: self._show_snapshot(
                result,
                "The companion was installed for this user and was not enabled. "
                "Follow the GNOME status above for the next step.",
            ),
            "Installing the companion for this user…",
        )

    def enable_gnome(self):
        if not self.gnome_enable_button.isEnabled():
            return
        answer = QMessageBox.question(
            self,
            "Enable GNOME companion",
            "Enable the MC7 Studio companion for this GNOME user? It exposes only "
            "the focused window process ID over the local session bus.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._run(
            self.manager.enable_gnome_extension,
            lambda result: self._show_snapshot(
                result, "The GNOME Wayland companion is enabled."
            ),
            "Enabling the GNOME Wayland companion…",
        )

    def install_udev(self):
        if not self.udev_install_button.isEnabled():
            return
        answer = QMessageBox.question(
            self,
            "Install MC7 USB access rule",
            "Install the bundled rule in /etc/udev/rules.d and reload udev rules? "
            "Administrator authentication will be requested. Reconnect the mouse "
            "and receiver afterward.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._run(
            self.manager.install_udev_rule,
            lambda result: self._show_snapshot(
                result,
                "The udev rule was installed and reloaded. Reconnect the mouse and receiver.",
            ),
            "Waiting for administrator authorization…",
        )

    def reject(self):
        if self.task is None:
            super().reject()

    def closeEvent(self, event):
        if self.task is not None:
            event.ignore()
        else:
            event.accept()
