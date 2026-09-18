"""Read firmware backups and run isolated, explicitly requested settings restore."""

import json
import os
from pathlib import Path
import selectors
from .pipe_io import pipe_selector
import subprocess
import time

from .runtime import helper_command

MAX_RESPONSE_BYTES = 8 * 1024 * 1024  # Bounded by the saved restore plan.


class RestoreServiceError(ValueError):
    def __init__(self, message, *, device_may_have_changed=False, completed=()):
        super().__init__(message)
        self.device_may_have_changed = device_may_have_changed
        self.completed = list(completed)


class RestoreService:
    def inspect(self, path):
        from .firmware_backup import read_backup
        backup = read_backup(path)
        warnings = list(dict.fromkeys(backup.warnings + tuple(
            warning for profile in backup.profiles for warning in profile.warnings)))
        return {'path': str(Path(path).resolve()), 'sha256': backup.sha256,
                'firmware_version': backup.firmware_version,
                'device_id': backup.device_id, 'profile_count': len(backup.profiles),
                'read_at': backup.value['read_at'], 'warnings': warnings}

    def prepare(self, device_id, backup_path, backup_directory, *, expected_sha256=None):
        request = {'operation': 'prepare', 'device_id': device_id,
                   'backup_path': str(backup_path), 'backup_directory': str(backup_directory)}
        if expected_sha256 is not None:
            request['backup_sha256'] = expected_sha256
        try:
            process = subprocess.run(helper_command('swarm2.restore_hardware'),
                input=json.dumps(request), text=True, capture_output=True, timeout=180)
        except subprocess.TimeoutExpired as error:
            raise RestoreServiceError('Restore preparation timed out; no settings were restored') from error
        try:
            value = json.loads(process.stdout)
        except ValueError as error:
            raise RestoreServiceError('Restore preparation returned no valid result') from error
        if not isinstance(value, dict) or process.returncode or 'error' in value or 'result' not in value:
            raise RestoreServiceError(value.get('error', 'Restore preparation failed')
                                      if isinstance(value, dict) else 'Restore preparation failed')
        result = value['result']
        if not isinstance(result, dict):
            raise RestoreServiceError('Restore preparation returned an invalid plan')
        return result

    def restore(self, prepared, progress=lambda value: None):
        request = {'operation': 'apply', 'device_id': prepared['device_id'], 'prepared': prepared}
        process = subprocess.Popen(helper_command('swarm2.restore_hardware'),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        terminal, buffer = None, bytearray()
        deadline = time.monotonic() + 630
        try:
            process.stdin.write(json.dumps(request).encode())
            process.stdin.close()
            with pipe_selector() as ready:
                ready.register(process.stdout, selectors.EVENT_READ, 'output')
                ready.register(process.stderr, selectors.EVENT_READ, 'error')
                while ready.get_map():
                    if time.monotonic() >= deadline:
                        raise RestoreServiceError('Settings restore exceeded its deadline; some settings may have changed',
                                                  device_may_have_changed=True)
                    for key, _ in ready.select(.1):
                        chunk = os.read(key.fileobj.fileno(), 4096)
                        if not chunk:
                            ready.unregister(key.fileobj)
                            continue
                        if key.data == 'error':
                            continue
                        buffer.extend(chunk)
                        if len(buffer) > MAX_RESPONSE_BYTES:
                            raise ValueError('The restore helper returned an oversized response')
                        while b'\n' in buffer:
                            line, _, rest = buffer.partition(b'\n')
                            buffer = bytearray(rest)
                            value = json.loads(line)
                            if not isinstance(value, dict) or terminal is not None:
                                raise ValueError('The restore helper returned an invalid message sequence')
                            if value.get('type') == 'progress':
                                try:
                                    progress(value)
                                except Exception:
                                    pass  # Presentation failure is not a device abort command.
                            elif 'error' in value or 'result' in value:
                                terminal = value
                            else:
                                raise ValueError('The restore helper returned an unknown message')
            process.wait(timeout=5)
            if buffer or terminal is None:
                raise ValueError('The restore helper stopped without a complete result')
            if 'error' in terminal:
                completed = terminal.get('completed', [])
                changed = terminal.get('device_may_have_changed', True)
                if not isinstance(completed, list) or type(changed) is not bool:
                    raise ValueError('The restore helper returned invalid failure details')
                message = str(terminal['error'])
                if completed:
                    message += f' Completed {len(completed)} profile sections before stopping.'
                if changed:
                    message += ' Some settings may have changed; read the mouse before continuing.'
                raise RestoreServiceError(message,
                    device_may_have_changed=changed, completed=completed)
            result = terminal['result']
            if process.returncode or not isinstance(result, dict) or result.get('verified') is not True:
                raise ValueError('The restore helper did not verify the restored settings')
            return result
        except RestoreServiceError:
            raise
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            raise RestoreServiceError(f'{error}. Some settings may have changed; read the mouse before continuing.',
                                      device_may_have_changed=True) from error
        finally:
            if not process.stdin.closed:
                process.stdin.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()
