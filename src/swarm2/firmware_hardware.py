"""Isolated firmware preparation and explicitly requested installation.

Prepare performs reads and writes a local backup. Only operation=install may
send CFU output or the source-required post-upgrade reset.
"""

from datetime import datetime, timezone
import hashlib
import json
import os
from .file_io import open_regular_read
from pathlib import Path
import sys
import threading
import time
import stat
import uuid

from .firmware_catalog import FirmwareError, release_by_key
from .firmware_commands import decode_version_response, decode_realtek_version, parse_offer
from .firmware_package import inspect_firmware_package
from .firmware_transport import FirmwareTransport
from .settings import read_bundle, read_raw, stable
from .status_commands import decode_status_response, compare_catalog_version
from .transport import DeviceError
from .firmware_update import FirmwareUpdateError

HARD_TIMEOUT_SECONDS = 1920
MAX_BACKUP_BYTES = 4 * 1024 * 1024


def _identity(transport):
    return str(getattr(transport.normal, 'location_id', transport.device_id))


def _status(transport):
    return decode_status_response(read_raw(transport.normal, 'status', 0))


def _read_profiles(transport):
    profiles = [read_bundle(transport.normal, index) for index in range(5)]
    if any(profile.get('errors') for profile in profiles):
        raise FirmwareError('Read every supported profile and assigned macro successfully before updating firmware')
    return profiles


def _write_backup(directory, value):
    directory = Path(directory)
    if not directory.is_dir():
        raise FirmwareError('Choose an existing folder for the settings backup')
    data = (json.dumps(value, indent=2) + '\n').encode()
    if len(data) > MAX_BACKUP_BYTES:
        raise FirmwareError('Mouse settings backup exceeds the supported size')
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    name = f'mc7-before-firmware-{timestamp}-{uuid.uuid4().hex}.json'
    path = directory / name
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        # Verify what was persisted before exposing an installation action.
        if path.read_bytes() != data:
            raise FirmwareError('Settings backup readback did not match')
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return str(path.resolve()), hashlib.sha256(data).hexdigest()


def _load_backup(prepared):
    path = Path(prepared['backup_path'])
    descriptor = open_regular_read(path)
    with os.fdopen(descriptor, 'rb') as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise FirmwareError('The settings backup must be a regular file')
        data = source.read(MAX_BACKUP_BYTES + 1)
    if len(data) > MAX_BACKUP_BYTES or hashlib.sha256(data).hexdigest() != prepared.get('backup_sha256'):
        raise FirmwareError('The settings backup changed; prepare this update again')
    value = json.loads(data)
    if not isinstance(value, dict) or value.get('schema') != 'swarm2.mc7.firmware-backup.v1':
        raise FirmwareError('The settings backup is not a supported firmware backup')
    return value


def _check_profiles(before, after):
    if not isinstance(before, list) or len(before) != 5:
        raise FirmwareError('The settings backup does not contain all five profiles')
    for previous, current in zip(before, after):
        if previous.get('macro_data') != current.get('macro_data'):
            raise FirmwareError('Assigned macros changed since preparation; prepare again')
        old, new = previous['settings'], current['settings']
        if old.keys() != new.keys() or any(stable(key, bytes.fromhex(old[key])) != stable(key, bytes.fromhex(new[key]))
                                          for key in old if key != 'status'):
            raise FirmwareError('Mouse settings changed since preparation; prepare again')


def _preflight(transport, package):
    from .firmware_update import inspect_update
    status = _status(transport)
    comparison = compare_catalog_version(status, package.package_version,
        role=package.release.role, product_id=package.release.product_id)
    if comparison >= 0:
        raise FirmwareError('The mouse already has this firmware or a newer version; reinstall and downgrade are disabled')
    if status.battery_percent is None or status.battery_percent < 30:
        raise FirmwareError('Charge the mouse to at least 30% before updating firmware')
    version = decode_version_response(transport.get_feature())
    installed_parts = decode_realtek_version(version.version_raw)
    target_parts = decode_realtek_version(parse_offer(package.offer).version_raw)
    if (installed_parts[:2] != target_parts[:2]
            or installed_parts[2:] != (status.firmware_major, status.firmware_minor)
            or target_parts[2:] != (package.fw_version // 100, package.fw_version % 100)):
        raise FirmwareError('The CFU platform or firmware version does not match the mouse and package')
    inspect_update(version, package)
    return status, version


def transact(request, *, transport_factory=FirmwareTransport, emit=lambda value: None):
    operation = request.get('operation')
    if operation not in ('prepare', 'install') or request.get('role') != 'mouse':
        raise FirmwareError('Native firmware installation currently supports the directly connected MC7 mouse only')
    release = release_by_key(request.get('release_key'))
    if release is None or release.role != 'mouse' or not release.installation_supported:
        raise FirmwareError('Choose the supported current mouse package; historical installation is not validated')
    device_id = request.get('device_id')
    if not isinstance(device_id, str) or not device_id or len(device_id) > 1024:
        raise FirmwareError('Select the directly connected USB mouse')
    package = inspect_firmware_package(request['archive_path'], release)
    with transport_factory(device_id) as transport:
        status, cfu = _preflight(transport, package)
        profiles = _read_profiles(transport)
        if operation == 'prepare':
            transport.check_update_access()
            backup = {'schema': 'swarm2.mc7.firmware-backup.v1', 'device_id': device_id,
                      'transport_identity': _identity(transport), 'release_key': release.key,
                      'archive_sha256': release.sha256,
                      'status_raw': status.raw.hex(), 'cfu_raw': cfu.raw.hex(), 'profiles': profiles,
                      'read_at': datetime.now(timezone.utc).isoformat(),
                      'limits': ('Readable profile settings and assigned macros only; custom LCD '
                                 'background pixels and Open Application icon pixels cannot be backed up.')}
            path, digest = _write_backup(request['backup_directory'], backup)
            reset = package.requires_reset(status.firmware_numeric)
            summary = (f"Mouse firmware {status.firmware_version} → {package.fw_version // 100}.{package.fw_version % 100:02d}.\n"
                       f"Five readable profiles and assigned macros saved to:\n{path}\n")
            summary += ('This upgrade resets mouse settings. Custom LCD background pixels and Open Application icon pixels cannot be backed up. Local presets stay on this computer.\n' if reset else 'No version-threshold settings reset is required.\n')
            summary += 'After updating, use Restore settings backup to review and restore supported settings and assigned macros. Custom calibration, background and icon pixels, and unsupported device data cannot be recreated. Restoration is a separate action.\n'
            summary += ('Native firmware installation is experimental. One Linux MC7 5.04 → 5.09 '
                        'update has passed; downgrade, recovery and macOS remain unvalidated.\n')
            summary += 'Keep the mouse connected directly by USB and the computer powered throughout installation. Installation cannot be cancelled once transfer starts.'
            return {'can_update': True, 'summary': summary, 'backup_path': path, 'backup_sha256': digest,
                    'device_id': device_id, 'role': 'mouse', 'release_key': release.key,
                    'archive_path': str(Path(request['archive_path']).resolve()),
                    'archive_sha256': release.sha256, 'cfu_raw': cfu.raw.hex(),
                    'installed_numeric': status.firmware_numeric, 'reset_required': reset}

        prepared = request.get('prepared')
        if not isinstance(prepared, dict):
            raise FirmwareError('Prepare and review this update before installing')
        backup = _load_backup(prepared)
        if (prepared.get('device_id') != device_id or backup.get('device_id') != device_id
                or prepared.get('release_key') != release.key
                or backup.get('release_key') != release.key
                or backup.get('transport_identity') != _identity(transport)
                or backup.get('archive_sha256') != release.sha256
                or prepared.get('archive_sha256') != release.sha256
                or prepared.get('cfu_raw') != cfu.raw.hex() or backup.get('cfu_raw') != cfu.raw.hex()
                or prepared.get('installed_numeric') != status.firmware_numeric):
            raise FirmwareError('The prepared mouse or package changed; prepare this update again')
        _check_profiles(backup['profiles'], profiles)
        from .firmware_update import run_update
        emit({'type': 'progress', 'phase': 'transferring', 'percent': 0})
        transfer = run_update(transport, package, progress_callback=lambda progress: emit({'type': 'progress', **progress}))
    # The offer requests a device restart. Reopen only the selected physical
    # USB location and verify both version formats before considering success.
    emit({'type': 'progress', 'phase': 'reconnecting'})
    expected_offer = parse_offer(package.offer)
    deadline, last_error = time.monotonic() + 60, None
    time.sleep(5)
    while time.monotonic() < deadline:
        try:
            with transport_factory(device_id) as transport:
                if _identity(transport) != backup['transport_identity']:
                    raise FirmwareError('The restarted mouse has a different USB identity')
                final_status = _status(transport)
                final_cfu = decode_version_response(transport.get_feature())
                if (final_status.firmware_numeric != package.fw_version
                        or final_cfu.version_raw != expected_offer.version_raw):
                    raise FirmwareError('The restarted mouse has not reported the expected firmware version')
                # A reset is never retried. Leave this reconnect loop before
                # issuing it, so a failure cannot accidentally replay the reset.
                break
        except (OSError, ValueError, DeviceError) as error:
            last_error = error
            time.sleep(1)
    else:
        raise DeviceError(f'Firmware was transferred but the restarted mouse could not be verified: {last_error}')
    reset_result = None
    with transport_factory(device_id) as transport:
        # Reopening after a successful reconnect must not reset a replacement
        # device, even if it exposes the same ordinary settings interface.
        if _identity(transport) != backup['transport_identity']:
            raise FirmwareError('The mouse USB identity changed before settings migration')
        final_status = _status(transport)
        final_cfu = decode_version_response(transport.get_feature())
        if final_status.firmware_numeric != package.fw_version or final_cfu.version_raw != expected_offer.version_raw:
            raise FirmwareError('The mouse firmware changed before settings migration; no reset was sent')
        if package.requires_reset(status.firmware_numeric):
            from .firmware_reset import FirmwareResetTransport, perform_factory_reset
            emit({'type': 'progress', 'phase': 'resetting'})
            reset_result = perform_factory_reset(FirmwareResetTransport(transport.normal))
        final_status = _status(transport)
        final_cfu = decode_version_response(transport.get_feature())
        after = _read_profiles(transport)
        if final_status.firmware_numeric != package.fw_version or final_cfu.version_raw != expected_offer.version_raw:
            raise DeviceError('Post-update firmware verification failed; read the mouse again before any further update')
    return {'verified': True, 'outcome': 'updated', 'firmware_version': final_status.firmware_version,
            'backup_path': prepared['backup_path'], 'transfer': transfer, 'reset': reset_result,
            'profiles_read_back': len(after)}


def main():
    operation = None
    watchdog = threading.Timer(HARD_TIMEOUT_SECONDS, lambda: os._exit(3))
    watchdog.daemon = True
    watchdog.start()
    def progress(value):
        try:
            print(json.dumps(value), flush=True)
        except (OSError, BrokenPipeError):
            # Keep completing the accepted firmware operation if its UI went
            # away. Losing a progress pipe is not a supported device abort.
            pass
    try:
        raw = sys.stdin.buffer.read(16385)
        if len(raw) > 16384:
            raise FirmwareError('Firmware request is too large')
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise FirmwareError('Firmware request must be an object')
        operation = request.get('operation')
        result = transact(request, emit=progress)
        print(json.dumps({'result': result}), flush=True)
        return 0
    except (OSError, ValueError, DeviceError, FirmwareUpdateError, KeyError, TypeError) as error:
        print(json.dumps({'error': str(error),
                          'device_may_have_changed': getattr(error, 'device_may_have_changed', operation == 'install'),
                          'records_acknowledged': getattr(error, 'records_acknowledged', 0),
                          'content_records_sent': getattr(error, 'content_records_sent', None),
                          'phase': getattr(error, 'phase', None)}), flush=True)
        return 2
    finally:
        watchdog.cancel()


if __name__ == '__main__':
    raise SystemExit(main())
