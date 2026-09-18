"""Explicit restore UI with fake services; never opens hardware or the network."""

import copy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tests import test_gui as fixture
from swarm2.configuration import PresetStore

if fixture.MainWindow is not None:
    from PySide6.QtGui import QCloseEvent
    from swarm2.gui.restore import RestoreDialog


class FakeRestoreService:
    def __init__(self):
        self.calls = []
        self.gate = None
        self.entered = threading.Event()
        self.failure = None
        self.prepared = {'can_restore': True, 'device_id': 'fixture-mc7',
                         'plan_path': '/fake/plan.json', 'plan_sha256': 'a' * 64,
                         'summary': 'Restore five profiles; current calibration and custom images are preserved.'}
        self.result = {'verified': True, 'profiles_read_back': 5,
                       'current_backup_path': '/fake/before-restore.json'}

    def _call(self, *args):
        self.calls.append(args)
        self.entered.set()
        if self.gate is not None and not self.gate.wait(3):
            raise RuntimeError('Fake restore worker timed out')
        if self.failure:
            raise ValueError(self.failure)

    def inspect(self, path):
        self._call('inspect', str(path))
        return {'path': str(path), 'sha256': 'b' * 64, 'profile_count': 5,
                'firmware_version': '5.04', 'read_at': '2026-09-16',
                'warnings': ['Custom calibration and unreadable image pixels are not restored.']}

    def prepare(self, device_id, path, directory, *, expected_sha256=None):
        self._call('prepare', device_id, path, directory, expected_sha256)
        return copy.deepcopy(self.prepared)

    def restore(self, prepared, progress):
        progress({'phase': 'restoring', 'profile_slot': 2, 'section': 'lighting'})
        self._call('restore', copy.deepcopy(prepared))
        return copy.deepcopy(self.result)


@unittest.skipIf(fixture.MainWindow is None, 'Install the gui extra to exercise Qt')
class RestoreDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = fixture.QApplication.instance() or fixture.QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = FakeRestoreService()
        self.dialog = RestoreDialog(self.service, 'fixture-mc7')
        self.path = str(Path(self.directory.name) / 'old settings.json')

    def tearDown(self):
        if self.service.gate:
            self.service.gate.set()
        self.wait()
        self.dialog.close()
        self.dialog.deleteLater()
        self.app.processEvents()
        self.directory.cleanup()

    def wait(self):
        self.app.processEvents()
        deadline = time.monotonic() + 4
        while self.dialog.task is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.app.processEvents()
        self.assertIsNone(self.dialog.task)

    def inspect(self):
        with patch('swarm2.gui.restore.QFileDialog.getOpenFileName', return_value=(self.path, '')):
            self.dialog.open_button.click()
        self.wait()

    def prepare(self):
        self.inspect()
        with patch('swarm2.gui.restore.QFileDialog.getExistingDirectory', return_value=self.directory.name):
            self.dialog.prepare_button.click()
        self.wait()

    def test_open_is_inert_and_offline_inspection_never_prepares_or_restores(self):
        self.assertEqual(self.service.calls, [])
        self.assertFalse(self.dialog.prepare_button.isEnabled())
        self.assertFalse(self.dialog.restore_button.isEnabled())
        self.dialog.device_id = None
        self.inspect()
        self.assertEqual(self.service.calls, [('inspect', self.path)])
        self.assertFalse(self.dialog.prepare_button.isEnabled())
        self.assertFalse(self.dialog.device_may_have_changed)
        self.assertIn('5.04', self.dialog.backup_detail.text())
        self.assertIn('not restored', self.dialog.preview.text())

    def test_initial_backup_path_is_only_inspected(self):
        self.dialog.close()
        self.dialog.deleteLater()
        self.dialog = RestoreDialog(self.service, initial_path=self.path)
        self.wait()
        self.assertEqual(self.service.calls, [('inspect', self.path)])
        self.assertFalse(self.dialog.restore_button.isEnabled())

    def test_preparation_binds_target_and_directory_and_does_not_restore(self):
        self.prepare()
        self.assertEqual(self.service.calls[-1], ('prepare', 'fixture-mc7', self.path, self.directory.name, 'b' * 64))
        self.assertTrue(self.dialog.restore_button.isEnabled())
        self.assertFalse(self.dialog.device_may_have_changed)
        self.assertEqual(self.dialog.preview.text(), self.service.prepared['summary'])

    def test_cancel_file_and_backup_folder_do_not_schedule_work(self):
        with patch('swarm2.gui.restore.QFileDialog.getOpenFileName', return_value=('', '')):
            self.dialog.open_button.click()
        self.assertEqual(self.service.calls, [])
        self.inspect()
        with patch('swarm2.gui.restore.QFileDialog.getExistingDirectory', return_value=''):
            self.dialog.prepare_button.click()
        self.assertEqual(self.service.calls, [('inspect', self.path)])

    def test_changed_or_invalid_backup_invalidates_reviewed_plan(self):
        self.prepare()
        self.service.failure = 'Invalid settings backup'
        self.dialog.inspect('/fake/different.json')
        self.assertIsNone(self.dialog.prepared)
        self.assertIsNone(self.dialog.backup)
        self.assertFalse(self.dialog.restore_button.isEnabled())
        self.wait()
        self.assertIn('Invalid settings backup', self.dialog.status.text())

    def test_no_changes_plan_does_not_enable_restore(self):
        self.service.prepared.update(can_restore=False, reason='No supported changes are needed.')
        self.prepare()
        self.assertFalse(self.dialog.restore_button.isEnabled())
        self.assertIn('No supported changes', self.dialog.status.text())

    def test_prepared_review_shows_skipped_fields_changes_and_macro_normalization(self):
        self.service.prepared.update(
            changes=[{'profile_slot': 2, 'section': 'buttons'}],
            warnings=['An unknown LCD page will be preserved.'],
            macro_timing_adjustments=[{'profile_slot': 2, 'layer': 'primary', 'logical_slot': 3,
                                       'event_index': 0, 'requested_ticks': 0, 'encoded_ticks': 1}])
        self.prepare()
        text = self.dialog.preview.text()
        self.assertIn('Profile 2: buttons', text)
        self.assertIn('unknown LCD page', text)
        self.assertIn('0 → 1 ticks', text)

    def test_wrapped_review_remains_readable_after_resize(self):
        self.dialog.show()
        self.service.prepared['warnings'] = ['Preserve unsupported settings and calibration. ' * 40]
        self.prepare()
        for width in (740, 620):
            self.dialog.resize(width, 760)
            for _ in range(3):
                self.app.processEvents()
            preview = self.dialog.preview
            self.assertGreaterEqual(preview.height(), preview.heightForWidth(preview.width()))

    def test_restore_requires_explicit_click_consumes_plan_and_reports_verified_result(self):
        self.prepare()
        self.assertFalse(any(call[0] == 'restore' for call in self.service.calls))
        self.dialog.restore_button.click()
        self.assertIsNone(self.dialog.prepared)
        self.assertTrue(self.dialog.device_may_have_changed)
        self.wait()
        self.assertEqual(self.service.calls[-1], ('restore', self.service.prepared))
        self.assertEqual(self.dialog.outcome, self.service.result)
        self.assertIn('restored and verified', self.dialog.status.text())
        self.assertFalse(self.dialog.restore_button.isEnabled())

    def test_unverified_result_and_partial_failure_do_not_report_success_or_retry(self):
        self.prepare()
        self.service.result['verified'] = False
        self.dialog.restore_button.click()
        self.wait()
        self.assertIsNone(self.dialog.outcome)
        self.assertIn('could not be verified', self.dialog.status.text())
        self.assertTrue(self.dialog.device_may_have_changed)
        self.assertIsNone(self.dialog.prepared)
        self.dialog.restore()
        self.assertEqual(sum(call[0] == 'restore' for call in self.service.calls), 1)

    def test_later_failed_restore_does_not_reuse_previous_success(self):
        self.prepare()
        self.dialog.restore_button.click()
        self.wait()
        self.assertIsNotNone(self.dialog.outcome)
        self.prepare()
        self.service.failure = 'Disconnected after changing lighting'
        self.dialog.restore_button.click()
        self.wait()
        self.assertIsNone(self.dialog.outcome)
        self.assertIn('Disconnected', self.dialog.status.text())

    def test_running_restore_blocks_close_reentry_and_other_actions(self):
        self.prepare()
        self.service.gate = threading.Event()
        self.service.entered.clear()
        self.dialog.show()
        self.dialog.restore_button.click()
        self.assertTrue(self.service.entered.wait(1))
        for button in (self.dialog.open_button, self.dialog.prepare_button,
                       self.dialog.restore_button, self.dialog.close_button):
            self.assertFalse(button.isEnabled())
        event = QCloseEvent()
        self.dialog.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.dialog.reject()
        self.assertTrue(self.dialog.isVisible())
        self.dialog.restore()
        self.assertEqual(sum(call[0] == 'restore' for call in self.service.calls), 1)
        self.service.gate.set()
        self.wait()


@unittest.skipIf(fixture.MainWindow is None, 'Install the gui extra to exercise Qt')
class MainWindowRestoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = fixture.QApplication.instance() or fixture.QApplication([])

    def test_restore_modal_blocks_device_jobs_and_preserves_dirty_draft(self):
        with tempfile.TemporaryDirectory() as directory:
            window = fixture.MainWindow(store=PresetStore(Path(directory)), auto_discover=False)
            window.draft.lighting.brightness = 37
            window._changed()
            before = window.draft.to_dict()
            snapshot = {'sentinel': 'preserve on inspect'}
            window.snapshot = snapshot
            observed = []
            def modal(dialog):
                self.assertIs(window._restore_dialog, dialog)
                self.assertFalse(window.firmware_button.isEnabled())
                self.assertFalse(window.restore_button.isEnabled())
                window._run_job(lambda: observed.append('bad'), lambda result: None, '')
                self.assertIsNone(window._job)
                return 0
            with patch.object(RestoreDialog, 'exec', modal):
                window.manage_restore()
            self.assertEqual(observed, [])
            self.assertEqual(window.draft.to_dict(), before)
            self.assertTrue(window.dirty)
            self.assertIs(window.snapshot, snapshot)
            self.assertIsNone(window._restore_dialog)
            window.dirty = False
            window.close()

    def test_restore_attempt_invalidates_baseline_but_keeps_local_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            window = fixture.MainWindow(store=PresetStore(Path(directory)), auto_discover=False)
            window.draft.lighting.brightness = 37
            window._changed()
            before = window.draft.to_dict()
            window.snapshot = {'sentinel': 'stale after restore'}
            def modal(dialog):
                dialog.device_may_have_changed = True
                return 0
            with patch.object(RestoreDialog, 'exec', modal):
                window.manage_restore()
            self.assertIsNone(window.snapshot)
            self.assertEqual(window.draft.to_dict(), before)
            self.assertTrue(window.dirty)
            self.assertIn('could not be verified', window.message.text())
            window.dirty = False
            window.close()
