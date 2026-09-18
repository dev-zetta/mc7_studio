"""Explicit firmware download and preparation; opening never flashes a mouse."""

from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Signal, Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QFileDialog, QHBoxLayout,
                              QPushButton, QVBoxLayout, QScrollArea, QWidget, QLayout,
                              QSizePolicy)

from ..firmware_catalog import current_release, known_releases, release_by_key
from .widgets import STYLE, label


class FirmwareTask(QThread):
    completed = Signal(object)
    failed = Signal(str)
    progress = Signal(object)

    def __init__(self, operation, parent=None, *, streaming=False):
        super().__init__(parent)
        self.operation = operation
        self.streaming = streaming

    def run(self):
        try:
            self.completed.emit(self.operation(self.progress.emit) if self.streaming else self.operation())
        except Exception as error:
            self.failed.emit(str(error))


class FirmwareDialog(QDialog):
    def __init__(self, service, device_id=None, parent=None):
        super().__init__(parent)
        self.service, self.device_id = service, device_id
        self.task = None
        self.package = None
        self.prepared = None
        self.device_may_have_changed = False
        self.outcome = None
        self.restore_requested = False
        self.restore_backup_path = None
        self.setWindowTitle("MC7 firmware")
        self.setMinimumWidth(660)
        self.resize(740, 780)
        self.setStyleSheet(STYLE)
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
        scroll.setWidget(content)
        outer.addWidget(scroll)
        body.setContentsMargins(28, 26, 28, 24)
        body.setSpacing(16)
        body.addWidget(label("Firmware", "title"))
        self.installed = label("Mouse firmware: not read", "sectionTitle", True)
        body.addWidget(self.installed)
        self.release_choice = QComboBox()
        for release in known_releases():
            current = current_release(release.role)
            suffix = "current" if release == current else (
                "original 5.04 archive" if release.role == "mouse" and release.firmware_version == 504
                else "historical")
            self.release_choice.addItem(
                f"{release.role.capitalize()} · {release.package_version} · {suffix}", release.key)
        self.release_choice.currentIndexChanged.connect(self._release_changed)
        body.addWidget(self.release_choice)
        self.release_detail = label("", "muted", True)
        self.release_detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.addWidget(self.release_detail)
        self.download_button = QPushButton("Download official package…")
        self.download_button.clicked.connect(self.download)
        self.open_button = QPushButton("Open downloaded package…")
        self.open_button.clicked.connect(self.open_package)
        row = QHBoxLayout()
        row.addWidget(self.download_button)
        row.addWidget(self.open_button)
        body.addLayout(row)
        self.package_detail = label("Download verifies the exact package size and hashes. It does not install firmware.", "muted", True)
        self.package_detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.addWidget(self.package_detail)
        self.prepare_button = QPushButton("Prepare mouse update…")
        self.prepare_button.clicked.connect(self.prepare)
        body.addWidget(self.prepare_button)
        self.preparation_detail = label("Preparation checks the connected mouse and saves its five profiles before an update can be started.", "notice", True)
        self.preparation_detail.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        body.addWidget(self.preparation_detail)
        self.update_button = QPushButton("Update mouse firmware")
        self.update_button.clicked.connect(self.install)
        self.update_button.setEnabled(False)
        body.addWidget(self.update_button)
        self.status = label("", "muted", True)
        body.addWidget(self.status)
        self.restore_button = QPushButton('Restore settings backup…')
        self.restore_button.clicked.connect(self.request_restore)
        body.addWidget(self.restore_button)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.reject)
        body.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignRight)
        self._release_changed()
        if device_id:
            QTimer.singleShot(0, self.refresh_status)

    def _release_changed(self, *_):
        self.package = self.prepared = None
        release = release_by_key(self.release_choice.currentData())
        if release is None:
            return
        platform_installation = getattr(self.service, "installation_available", True)
        availability = ("Download and inspection are supported; firmware installation is not yet available on Windows."
                        if not platform_installation else
                        "Installation is supported for this current mouse update."
                        if release.installation_supported else
                        "Download and inspection are supported; reinstall or downgrade is not yet validated.")
        self.release_detail.setText(f"Official {release.role} package {release.package_version} · verified catalog dated {release.catalog_date}.\nTarget USB {release.vendor_id:04X}:{release.product_id:04X} · {release.size:,} bytes.\n{availability}")
        self.package_detail.setText("Choose or download the package for this target.")
        self.preparation_detail.setText(
            "Preparation checks the connected mouse and saves its readable profiles and assigned macros. No update starts automatically."
            if release.installation_supported and platform_installation else
            "This official historical mouse package can be preserved and inspected. Installation remains disabled until recovery behavior is validated."
            if release.role == "mouse" else
            "Transmitter packages can be downloaded and verified. Installing transmitter firmware is not yet supported.")
        self._controls()

    def _controls(self):
        idle = self.task is None
        self.release_choice.setEnabled(idle)
        self.download_button.setEnabled(idle)
        self.open_button.setEnabled(idle)
        self.close_button.setEnabled(idle)
        self.restore_button.setEnabled(idle)
        self.prepare_button.setEnabled(idle and self.device_id is not None and self.package is not None
                                       and self.package["role"] == "mouse"
                                       and self.package.get("installation_supported") is True
                                       and getattr(self.service, "installation_available", True))
        self.update_button.setEnabled(idle and bool(self.prepared and self.prepared.get("can_update")))

    def _run(self, operation, completed, text, *, streaming=False):
        if self.task is not None:
            return
        self.status.setText(text)
        self.task = FirmwareTask(operation, self, streaming=streaming)
        self.task.completed.connect(completed)
        self.task.failed.connect(self._failed)
        self.task.finished.connect(self._finished)
        self.task.progress.connect(self._progress)
        self._controls()
        self.task.start()

    def _finished(self):
        task, self.task = self.task, None
        task.deleteLater()
        self._controls()

    def _failed(self, message):
        self.prepared = None
        self.status.setText(message)

    def refresh_status(self):
        if self.device_id:
            self._run(lambda: self.service.read_status(self.device_id), self._status_received, "Reading installed mouse firmware…")

    def _status_received(self, result):
        version = result.get("firmware_version")
        catalog = result.get("firmware_catalog_version")
        self.installed.setText(f"Installed mouse firmware: {version or 'not reported'}" + (f" · package format {catalog}" if catalog else ""))
        self.status.setText("Installed version read from the connected USB mouse.")

    def download(self):
        release_key = self.release_choice.currentData()
        release = release_by_key(release_key)
        if release is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Download official firmware", release.filename, "Firmware package (*.7z)")
        if path:
            self.prepared = None
            self._run(lambda: self.service.download(release_key, path), self._downloaded, "Downloading and verifying the official firmware archive…")

    def _downloaded(self, result):
        self.package = result
        self.prepared = None
        self.preparation_detail.setText(
            "Prepare this package for the connected mouse before installing."
            if result.get('installation_supported') is True else
            "Historical mouse archive verified. Installation remains disabled until recovery behavior is validated."
            if result['role'] == "mouse" else
            "Transmitter package verified. Installing transmitter firmware is not yet supported.")
        self.package_detail.setText(f"Verified {result['role']} package {result['version']}\n{result['path']}\nSHA-256: {result['sha256']}")
        self.status.setText("Download verified. No firmware has been installed.")

    def open_package(self):
        release_key = self.release_choice.currentData()
        path, _ = QFileDialog.getOpenFileName(self, "Open official firmware package", "", "Firmware package (*.7z)")
        if path:
            self.prepared = self.package = None
            self._run(lambda: self.service.inspect(release_key, path), self._downloaded, "Verifying firmware archive and image contents…")

    def prepare(self):
        if not self.prepare_button.isEnabled():
            return
        package = dict(self.package)
        directory = QFileDialog.getExistingDirectory(self, "Choose a folder for the mouse settings backup", str(Path(package['path']).parent))
        if directory:
            self.prepared = None
            self._run(lambda: self.service.prepare(self.device_id, package['release_key'], package['path'], directory), self._prepared, "Checking compatibility and backing up the mouse profiles…")

    def _prepared(self, result):
        self.prepared = result
        self.restore_backup_path = result.get('backup_path')
        self.preparation_detail.setText(result['summary'])
        self.preparation_detail.setMinimumHeight(self.preparation_detail.heightForWidth(self.preparation_detail.width()))
        self.status.setText("Ready for an explicit update." if result.get('can_update') else result.get('reason', 'Installation is unavailable for this device.'))

    def install(self):
        if not self.update_button.isEnabled() or self.task is not None or not self.prepared:
            return
        prepared = dict(self.prepared)
        self.prepared = None  # One attempt per reviewed preparation.
        self.outcome = None
        self.device_may_have_changed = True
        self._run(lambda progress: self.service.install(prepared, progress), self._installed,
                  "Starting the reviewed firmware update. Keep the mouse connected; this operation cannot be cancelled.", streaming=True)

    def _progress(self, value):
        phase = value.get('phase', '')
        percent = value.get('percent')
        if phase in ('transferring', 'transfer', 'content'):
            self.status.setText(f"Uploading firmware{f' · {percent}%' if percent is not None else ''}. Keep the mouse connected.")
        elif phase == 'reconnecting':
            self.status.setText("Waiting for the mouse to restart and checking its firmware…")
        elif phase == 'resetting':
            self.status.setText("Completing the settings reset required by this firmware…")

    def _installed(self, result):
        if not isinstance(result, dict) or result.get('verified') is not True:
            self._failed("Firmware installation could not be verified. Read the mouse before continuing.")
            return
        self.outcome = result
        self.restore_backup_path = result['backup_path']
        self.installed.setText(f"Installed mouse firmware: {result['firmware_version']}")
        self.status.setText(f"Firmware installed and verified. Settings backup: {result['backup_path']}")

    def request_restore(self):
        if self.task is None:
            self.restore_requested = True
            self.accept()

    def reject(self):
        if self.task is None:
            super().reject()

    def closeEvent(self, event):
        event.ignore() if self.task is not None else event.accept()
