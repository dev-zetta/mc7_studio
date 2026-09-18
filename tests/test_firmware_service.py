"""Exercise real helper pipes with synthetic processes, never USB devices."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from swarm2.firmware_catalog import FirmwareError
from swarm2.firmware_service import FirmwareService


class FirmwareServiceTests(unittest.TestCase):
    prepared = {'device_id': 'fixture', 'role': 'mouse',
                'release_key': 'mouse:5.9.0.0', 'archive_path': '/unused.7z'}

    def run_helper(self, output, *, code=0, progress=lambda value: None, stderr=''):
        real_popen = subprocess.Popen
        # The child consumes the request and emits test data. It imports no
        # swarm2 modules and cannot accidentally enter the hardware helper.
        script = ('import sys\n'
                  'sys.stdin.buffer.read()\n'
                  f'sys.stderr.write({stderr!r})\n'
                  f'sys.stdout.buffer.write({output!r})\n'
                  f'sys.exit({code!r})\n')
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        script_path = Path(directory.name) / "helper.py"
        script_path.write_text(script, encoding="utf-8")
        def fake_helper(command, **kwargs):
            self.assertEqual(command[1:], ['-m', 'swarm2.firmware_hardware'])
            return real_popen([sys.executable, str(script_path)], **kwargs)
        with patch('swarm2.firmware_service.platform.system', return_value='Linux'), patch('swarm2.firmware_service.subprocess.Popen', side_effect=fake_helper):
            return FirmwareService(None).install(self.prepared, progress)

    def test_verified_terminal_result_and_progress_survive_large_stderr(self):
        events = []
        result = self.run_helper(
            b'{"type":"progress","percent":25}\n'
            b'{"result":{"verified":true,"firmware_version":"5.09"}}\n',
            progress=events.append, stderr='diagnostic\n' * 9000)
        self.assertEqual(result['firmware_version'], '5.09')
        self.assertEqual(events, [{'type': 'progress', 'percent': 25}])

    def test_broken_presentation_does_not_stop_an_accepted_operation(self):
        def closed_window(event):
            raise RuntimeError('window closed')
        result = self.run_helper(
            b'{"type":"progress","percent":25}\n'
            b'{"result":{"verified":true}}\n', progress=closed_window)
        self.assertTrue(result['verified'])

    def test_no_false_success_for_incomplete_or_unverified_results(self):
        for output, code in (
            (b'', 0),
            (b'{"result":{"verified":true}}', 0),
            (b'{"result":{"verified":true}}\n', 2),
            (b'{"result":{"verified":false}}\n', 0),
            (b'{"result":{"verified":1}}\n', 0),
            (b'{"result":null}\n', 0),
            (b'{"type":"progress","percent":100}\n', 0),
        ):
            with self.subTest(output=output, code=code), self.assertRaises(FirmwareError):
                self.run_helper(output, code=code)

    def test_terminal_failure_is_reported_even_with_zero_exit_status(self):
        with self.assertRaisesRegex(FirmwareError, 'acknowledgement lost'):
            self.run_helper(b'{"error":"acknowledgement lost","device_may_have_changed":true}\n')

    def test_duplicate_terminal_unknown_trailing_or_malformed_messages_fail(self):
        good = b'{"result":{"verified":true}}\n'
        for output in (good + good, good + b'{"type":"progress"}\n',
                       b'[]\n', b'{}\n', b'not-json\n', good + b'trailing',
                       b'x' * 70000):
            with self.subTest(size=len(output)), self.assertRaises((FirmwareError, ValueError)):
                self.run_helper(output)

    def test_preparation_errors_never_return_a_plan(self):
        for value, code in (({'error': 'cannot read profile'}, 2), ([], 0), ({}, 0)):
            process = subprocess.CompletedProcess([], code, json.dumps(value), '')
            with self.subTest(value=value), patch('subprocess.run', return_value=process), \
                 self.assertRaises(FirmwareError):
                FirmwareService(None).prepare('fixture', 'mouse:5.9.0.0', '/unused.7z', '/unused')

    def test_historical_package_cannot_reach_preparation_helper(self):
        with patch('swarm2.firmware_service.platform.system', return_value='Linux'), patch('swarm2.firmware_service.subprocess.run') as helper, \
             self.assertRaisesRegex(FirmwareError, 'installation is not validated'):
            FirmwareService(None).prepare(
                'fixture', 'mouse:5.4.0.0', '/unused.7z', '/unused')
        helper.assert_not_called()

    def test_windows_can_inspect_but_cannot_reach_installation_helpers(self):
        service = FirmwareService(None)
        with patch('swarm2.firmware_service.platform.system', return_value='Windows'), \
             patch('swarm2.firmware_service.subprocess.run') as prepare_helper, \
             patch('swarm2.firmware_service.subprocess.Popen') as install_helper:
            self.assertFalse(service.installation_available)
            with self.assertRaisesRegex(FirmwareError, 'not yet available on Windows'):
                service.prepare('fixture', 'mouse:5.9.0.0', '/unused.7z', '/unused')
            with self.assertRaisesRegex(FirmwareError, 'not yet available on Windows'):
                service.install(self.prepared)
        prepare_helper.assert_not_called()
        install_helper.assert_not_called()


if __name__ == '__main__':
    unittest.main()
