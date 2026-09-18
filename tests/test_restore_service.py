"""Restore helper protocol checks through real pipes, without device access."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from swarm2.firmware_catalog import FirmwareError
from swarm2.restore_service import RestoreService, RestoreServiceError


class RestoreServiceTests(unittest.TestCase):
    prepared = {'device_id': 'fixture', 'backup_path': '/unused.json'}

    def run_helper(self, output, *, code=0, progress=lambda value: None, stderr=''):
        real_popen = subprocess.Popen
        # This child only consumes JSON and emits supplied bytes. It never
        # imports the product helper, discovers devices or opens USB handles.
        script = ('import json, sys\n'
                  'request = json.load(sys.stdin)\n'
                  'assert request["operation"] == "apply"\n'
                  'assert request["device_id"] == "fixture"\n'
                  f'sys.stderr.write({stderr!r})\n'
                  f'sys.stdout.buffer.write({output!r})\n'
                  f'sys.exit({code!r})\n')

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        script_path = Path(directory.name) / "helper.py"
        script_path.write_text(script, encoding="utf-8")
        def fake_helper(command, **kwargs):
            self.assertEqual(command[1:], ['-m', 'swarm2.restore_hardware'])
            return real_popen([sys.executable, str(script_path)], **kwargs)

        with patch('swarm2.restore_service.subprocess.Popen', side_effect=fake_helper):
            return RestoreService().restore(self.prepared, progress)

    def test_verified_terminal_with_progress_and_large_stderr(self):
        events = []
        expected = {'verified': True, 'profiles_read_back': 5,
                    'current_backup_path': '/fixture/before.json'}
        output = (json.dumps({'type': 'progress', 'phase': 'restoring',
                              'profile_slot': 3, 'section': 'buttons'}) + '\n'
                  + json.dumps({'result': expected}) + '\n').encode()
        result = self.run_helper(output, progress=events.append, stderr='diagnostic\n' * 9000)
        self.assertEqual(result, expected)
        self.assertEqual(events, [{'type': 'progress', 'phase': 'restoring',
                                   'profile_slot': 3, 'section': 'buttons'}])

    def test_progress_callback_failure_does_not_abort_restore(self):
        def closed_window(event):
            raise RuntimeError('window closed')

        result = self.run_helper(
            b'{"type":"progress","phase":"restoring"}\n'
            b'{"type":"progress","phase":"verifying"}\n'
            b'{"result":{"verified":true}}\n', progress=closed_window)
        self.assertTrue(result['verified'])

    def test_bounded_large_timing_report_is_not_mistaken_for_a_failed_restore(self):
        expected = {'verified': True, 'warnings': ['x' * 70000]}
        result = self.run_helper((json.dumps({'result': expected}) + '\n').encode())
        self.assertEqual(result, expected)

    def test_output_limit_is_enforced_before_parsing_an_unbounded_line(self):
        with patch('swarm2.restore_service.MAX_RESPONSE_BYTES', 65536), \
             self.assertRaisesRegex(RestoreServiceError, 'oversized'):
            self.run_helper(b'x' * 70000)

    def test_progress_or_incomplete_result_never_claims_success(self):
        for output, code in (
            (b'', 0),
            (b'{"type":"progress","percent":100}\n', 0),
            (b'{"result":{"verified":true}}', 0),
            (b'{"result":{"verified":true}}\n', 2),
            (b'{"result":{"verified":false}}\n', 0),
            (b'{"result":{"verified":1}}\n', 0),
            (b'{"result":{"verified":"true"}}\n', 0),
            (b'{"result":null}\n', 0),
            (b'{"result":[]}\n', 0),
        ):
            with self.subTest(output=output, code=code), self.assertRaises(RestoreServiceError) as raised:
                self.run_helper(output, code=code)
            self.assertIs(raised.exception.device_may_have_changed, True)

    def test_duplicate_trailing_malformed_unknown_or_oversized_output_fails(self):
        good = b'{"result":{"verified":true}}\n'
        for output in (good + good, good + b'{"type":"progress"}\n',
                       b'[]\n', b'{}\n', b'not-json\n', b'\xff\n',
                       good + b'trailing', b'x' * 70000):
            with self.subTest(size=len(output)), self.assertRaises(RestoreServiceError) as raised:
                self.run_helper(output)
            self.assertIs(raised.exception.device_may_have_changed, True)

    def test_structured_partial_failure_preserves_completed_sections(self):
        completed = [{'profile_slot': 1, 'section': 'sensor'},
                     {'profile_slot': 1, 'section': 'lighting'}]
        output = (json.dumps({'error': 'button readback mismatch',
                              'completed': completed, 'device_may_have_changed': True}) + '\n').encode()
        # A terminal error stays an error even when a child exits zero.
        with self.assertRaisesRegex(RestoreServiceError, 'button readback mismatch') as raised:
            self.run_helper(output)
        self.assertIs(raised.exception.device_may_have_changed, True)
        self.assertEqual(raised.exception.completed, completed)
        self.assertIn('Completed 2 profile sections', str(raised.exception))
        self.assertIn('read the mouse', str(raised.exception))

    def test_prewrite_failure_can_explicitly_report_unchanged_device(self):
        output = b'{"error":"backup changed","completed":[],"device_may_have_changed":false}\n'
        with self.assertRaisesRegex(RestoreServiceError, 'backup changed') as raised:
            self.run_helper(output, code=2)
        self.assertIs(raised.exception.device_may_have_changed, False)
        self.assertEqual(raised.exception.completed, [])
        self.assertNotIn('Some settings may have changed', str(raised.exception))

    def test_failure_without_uncertainty_flag_is_conservative(self):
        with self.assertRaises(RestoreServiceError) as raised:
            self.run_helper(b'{"error":"helper stopped"}\n')
        self.assertIs(raised.exception.device_may_have_changed, True)

    def test_malformed_error_metadata_becomes_a_conservative_service_failure(self):
        for metadata in ({'completed': None}, {'completed': 'sensor'},
                         {'device_may_have_changed': 'false'}, {'device_may_have_changed': 0}):
            output = (json.dumps({'error': 'invalid helper error', **metadata}) + '\n').encode()
            with self.subTest(metadata=metadata), self.assertRaises(RestoreServiceError) as raised:
                self.run_helper(output)
            self.assertIs(raised.exception.device_may_have_changed, True)
            self.assertEqual(raised.exception.completed, [])

    def test_inspection_is_offline_and_deduplicates_warnings(self):
        backup = SimpleNamespace(
            sha256='a' * 64, firmware_version='5.04', device_id='fixture',
            value={'read_at': '2026-09-16T12:00:00+00:00'},
            profiles=tuple(SimpleNamespace(warnings=('opaque action retained',)) for _ in range(5)),
            warnings=('calibration retained', 'opaque action retained'))
        with (patch('swarm2.firmware_backup.read_backup', return_value=backup) as read,
              patch('swarm2.restore_service.subprocess.Popen', side_effect=AssertionError('Unexpected helper')),
              patch('swarm2.restore_service.subprocess.run', side_effect=AssertionError('Unexpected helper'))):
            result = RestoreService().inspect('fixture.json')
        read.assert_called_once_with('fixture.json')
        self.assertEqual(result, {'path': str(Path('fixture.json').resolve()),
                                 'sha256': 'a' * 64, 'firmware_version': '5.04',
                                 'device_id': 'fixture', 'profile_count': 5,
                                 'read_at': backup.value['read_at'],
                                 'warnings': ['calibration retained', 'opaque action retained']})

    def test_inspection_preserves_validation_failure(self):
        with (patch('swarm2.firmware_backup.read_backup', side_effect=FirmwareError('bad backup')),
              self.assertRaisesRegex(FirmwareError, 'bad backup')):
            RestoreService().inspect('/unused.json')

    def test_preparation_serializes_paths_and_returns_only_a_result_dict(self):
        expected = {'can_restore': True, 'summary': 'Review changes', 'device_id': 'fixture'}
        process = subprocess.CompletedProcess([], 0, json.dumps({'result': expected}), '')
        with patch('swarm2.restore_service.subprocess.run', return_value=process) as run:
            result = RestoreService().prepare('fixture', Path('/backup.json'), Path('/before'))
        self.assertEqual(result, expected)
        self.assertEqual(run.call_args.args[0][1:], ['-m', 'swarm2.restore_hardware'])
        self.assertEqual(json.loads(run.call_args.kwargs['input']),
                         {'operation': 'prepare', 'device_id': 'fixture',
                          'backup_path': str(Path('/backup.json')), 'backup_directory': str(Path('/before'))})
        self.assertEqual(run.call_args.kwargs['timeout'], 180)

    def test_preparation_failure_never_returns_a_plan(self):
        for value, code in (({'error': 'cannot read profile'}, 2), ([], 0), ({}, 0),
                            ({'result': None}, 0), ({'result': []}, 0), ({'result': {}}, 2),
                            ({'error': 'cannot read profile', 'result': {'can_restore': True}}, 0)):
            process = subprocess.CompletedProcess([], code, json.dumps(value), '')
            with self.subTest(value=value, code=code), \
                 patch('swarm2.restore_service.subprocess.run', return_value=process), \
                 self.assertRaises(RestoreServiceError):
                RestoreService().prepare('fixture', '/backup.json', '/before')

    def test_preparation_malformed_result_and_timeout_are_safe_failures(self):
        process = subprocess.CompletedProcess([], 0, 'not-json', '')
        with patch('swarm2.restore_service.subprocess.run', return_value=process), \
             self.assertRaisesRegex(RestoreServiceError, 'no valid result'):
            RestoreService().prepare('fixture', '/backup.json', '/before')
        with patch('swarm2.restore_service.subprocess.run',
                   side_effect=subprocess.TimeoutExpired('fixture helper', 180)), \
             self.assertRaisesRegex(RestoreServiceError, 'no settings were restored') as raised:
            RestoreService().prepare('fixture', '/backup.json', '/before')
        self.assertIs(raised.exception.device_may_have_changed, False)


if __name__ == '__main__':
    unittest.main()
