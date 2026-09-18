"""Review and restore supported settings from a five-profile firmware backup."""

from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (QDialog, QFileDialog, QLayout, QPushButton,
                              QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from .firmware import FirmwareTask
from .widgets import STYLE, label


class RestoreDialog(QDialog):
    def __init__(self, service, device_id=None, parent=None, *, initial_path=None):
        super().__init__(parent)
        self.service, self.device_id = service, device_id
        self.task = self.backup = self.prepared = self.outcome = None
        self.device_may_have_changed = False
        self.setWindowTitle('Restore MC7 settings')
        self.resize(740, 760)
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
        body.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
        scroll.setWidget(content)
        outer.addWidget(scroll)
        body.addWidget(label('Restore settings', 'title'))
        body.addWidget(label('Restore supported settings and assigned macros from a five-profile MC7 backup. Preparation reads the mouse and saves its current settings before any restoration.', 'muted', True))
        self.open_button = QPushButton('Open settings backup…')
        self.open_button.clicked.connect(self.open_backup)
        body.addWidget(self.open_button)
        self.backup_detail = label('Choose a backup created during firmware or restore preparation.', 'muted', True)
        self.backup_detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.addWidget(self.backup_detail)
        self.prepare_button = QPushButton('Prepare restore…')
        self.prepare_button.clicked.connect(self.prepare)
        body.addWidget(self.prepare_button)
        self.preview = label('The current active profile, lift-off calibration, custom background and unsupported device data are preserved. No firmware is installed by this workflow.', 'notice', True)
        self.preview.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.preview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.addWidget(self.preview)
        self.restore_button = QPushButton('Restore supported settings')
        self.restore_button.clicked.connect(self.restore)
        body.addWidget(self.restore_button)
        self.status = label('Connect the mouse directly by USB to prepare a restore.' if device_id is None else '', 'muted', True)
        body.addWidget(self.status)
        self.close_button = QPushButton('Close')
        self.close_button.clicked.connect(self.reject)
        body.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignRight)
        self._controls()
        if initial_path:
            QTimer.singleShot(0, lambda: self.inspect(initial_path))

    def _controls(self):
        idle = self.task is None
        self.open_button.setEnabled(idle)
        self.close_button.setEnabled(idle)
        self.prepare_button.setEnabled(idle and self.device_id is not None and self.backup is not None)
        self.restore_button.setEnabled(idle and bool(self.prepared and self.prepared.get('can_restore')))

    def _run(self, operation, completed, text, *, streaming=False):
        if self.task is not None:
            return
        self.status.setText(text)
        self.task = FirmwareTask(operation, self, streaming=streaming)
        self.task.completed.connect(completed)
        self.task.failed.connect(self._failed)
        self.task.progress.connect(self._progress)
        self.task.finished.connect(self._finished)
        self._controls()
        self.task.start()

    def _finished(self):
        task, self.task = self.task, None
        task.deleteLater()
        self._controls()

    def _failed(self, message):
        self.prepared = None
        self.status.setText(message)

    def open_backup(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Open MC7 settings backup', '', 'Settings backup (*.json)')
        if path:
            self.inspect(path)

    def inspect(self, path):
        if self.task is not None:
            return
        self.backup = self.prepared = None
        self.preview.setText('Prepare a fresh comparison before restoring this backup.')
        self._fit_preview()
        self._run(lambda: self.service.inspect(path), self._inspected, 'Checking the settings backup…')

    def _inspected(self, result):
        self.backup = result
        text = (f"{result['profile_count']} profiles · firmware {result['firmware_version']} · saved {result['read_at']}\n"
                f"{result['path']}\nSHA-256: {result['sha256']}")
        self.backup_detail.setText(text)
        self.preview.setText('\n'.join(result.get('warnings', [])) or 'Backup checked. Prepare a comparison with the connected mouse before restoring.')
        self._fit_preview()
        self.status.setText('Backup checked. No mouse settings have been changed.')

    def prepare(self):
        if not self.prepare_button.isEnabled():
            return
        backup = dict(self.backup)
        directory = QFileDialog.getExistingDirectory(self, 'Save the current mouse settings before restore', str(Path(backup['path']).parent))
        if directory:
            self.prepared = None
            self._run(lambda: self.service.prepare(self.device_id, backup['path'], directory,
                                                   expected_sha256=backup['sha256']),
                      self._prepared, 'Reading five profiles, checking restoration and saving the current settings…')

    def _prepared(self, result):
        self.prepared = result
        parts = [result['summary']]
        changes = result.get('changes', [])
        if changes:
            parts.append('Changed settings:')
            for slot in range(1, 6):
                sections = [item['section'] for item in changes if item['profile_slot'] == slot]
                if sections:
                    parts.append(f"Profile {slot}: {', '.join(sections)}")
        if result.get('warnings'):
            parts.append('Preserved or skipped settings:')
            parts.extend(result['warnings'])
        if result.get('macro_timing_adjustments'):
            parts.append('Macro delay changes (device timing units):')
            adjustments = result['macro_timing_adjustments']
            for item in adjustments[:100]:
                parts.append(f"Profile {item['profile_slot']}, {item['layer']} slot {item['logical_slot']}, "
                             f"event {item['event_index']}: {item['requested_ticks']} → {item['encoded_ticks']} ticks")
            if len(adjustments) > 100:
                parts.append(f"{len(adjustments) - 100} more timing changes. The full list is saved in the restore plan:\n{result['plan_path']}")
        self.preview.setText('\n\n'.join(parts))
        self._fit_preview()
        self.status.setText('Review the prepared restore, then choose Restore supported settings.' if result.get('can_restore') else result.get('reason', 'No supported changes are needed.'))

    def restore(self):
        if not self.restore_button.isEnabled() or self.task is not None or not self.prepared:
            return
        prepared, self.prepared = dict(self.prepared), None
        self.outcome = None
        self.device_may_have_changed = True
        self._run(lambda progress: self.service.restore(prepared, progress), self._restored,
                  'Restoring the reviewed settings. Keep the mouse connected.', streaming=True)

    def _progress(self, event):
        if event.get('phase') == 'verifying':
            self.status.setText('Reading all five profiles to verify the restored settings…')
        elif event.get('phase') == 'restoring':
            self.status.setText(f"Restoring profile {event.get('profile_slot', '?')} · {event.get('section', 'settings')}. Keep the mouse connected.")

    def _restored(self, result):
        if not isinstance(result, dict) or result.get('verified') is not True:
            self._failed('Restoration could not be verified. Read the mouse before continuing.')
            return
        self.outcome = result
        self.status.setText(f"Supported settings restored and verified across {result['profiles_read_back']} profiles.\nBefore-restore backup: {result['current_backup_path']}")

    def reject(self):
        if self.task is None:
            super().reject()

    def _fit_preview(self):
        self.preview.setMinimumHeight(max(0, self.preview.heightForWidth(self.preview.width())))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self._fit_preview)

    def closeEvent(self, event):
        event.ignore() if self.task is not None else event.accept()
