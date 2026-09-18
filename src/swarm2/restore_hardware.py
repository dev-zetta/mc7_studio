"""Bounded, explicitly requested restoration of readable MC7 settings.

Preparation only reads the mouse. Apply revalidates every artifact and profile,
then uses the existing typed settings transactions. There is no raw replay,
firmware transfer, reset, implicit retry, or automatic rollback here.
"""

from datetime import datetime, timezone
from copy import deepcopy
import hashlib
import json
import os
from .file_io import open_regular_read
from pathlib import Path
import stat
import sys
import threading
import uuid

from .configuration import Configuration
from .button_commands import BUTTON_SLOTS
from .firmware_backup import read_backup, merge_supported_profile
from .firmware_catalog import FirmwareError
from .firmware_commands import decode_realtek_version, decode_version_response
from .firmware_hardware import _check_profiles, _identity, _status
from .firmware_transport import FirmwareTransport
from .macro_io import prepare_lcd_macro_updates, prepare_macro_updates
from .service import DeviceService
from .settings import PER_PROFILE, REQUIRED, _plan, decode, read_bundle, stable, transact_settings
from .status_commands import decode_status_response
from .transport import DeviceError

HARD_TIMEOUT_SECONDS = 600
MAX_PLAN_BYTES = 8 * 1024 * 1024
PLAN_SCHEMA = 'swarm2.mc7.settings-restore-plan.v1'


class RestoreError(DeviceError):
    def __init__(self, message, *, device_may_have_changed=False, completed=()):
        super().__init__(message)
        self.device_may_have_changed = device_may_have_changed
        self.completed = list(completed)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise FirmwareError('Duplicate key in the restore plan')
        result[key] = value
    return result


def _write_artifact(directory, value, prefix):
    directory = Path(directory)
    if not directory.is_dir():
        raise FirmwareError('Choose an existing folder for the restore backup')
    data = (json.dumps(value, indent=2, allow_nan=False) + '\n').encode()
    if len(data) > MAX_PLAN_BYTES:
        raise FirmwareError('The restore artifact exceeds the supported size')
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    name = f'{prefix}{timestamp}-{uuid.uuid4().hex}.json'
    path = directory / name
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        if path.read_bytes() != data:
            raise FirmwareError('The restore artifact readback did not match')
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return str(path.resolve()), hashlib.sha256(data).hexdigest()


def _load_plan(prepared):
    if not isinstance(prepared, dict):
        raise FirmwareError('Prepare and review this restore before applying it')
    path = Path(prepared['plan_path'])
    descriptor = open_regular_read(path)
    with os.fdopen(descriptor, 'rb') as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise FirmwareError('The restore plan must be a regular file')
        data = source.read(MAX_PLAN_BYTES + 1)
    if len(data) > MAX_PLAN_BYTES or hashlib.sha256(data).hexdigest() != prepared.get('plan_sha256'):
        raise FirmwareError('The restore plan changed; prepare this restore again')
    value = json.loads(data, object_pairs_hook=_object)
    if not isinstance(value, dict) or value.get('schema') != PLAN_SCHEMA:
        raise FirmwareError('The restore plan has an unsupported format')
    return value


def _notify(emit, value):
    try:
        emit({'type': 'progress', **value})
    except Exception:
        # Presentation failure must not interrupt a settings transaction.
        pass


def _snapshot(device_id, slot, bundle, identity):
    return DeviceService._snapshot(device_id, slot, {**bundle, 'transport_identity': identity})


def _read_profiles(transport):
    profiles = [read_bundle(transport.normal, index) for index in range(5)]
    if any(profile.get('errors') for profile in profiles):
        raise FirmwareError('Read all five profiles and assigned macros successfully before restoring settings')
    return profiles


def _check_bundle(previous, current):
    old, new = previous['settings'], current['settings']
    if (previous.get('macro_data') != current.get('macro_data') or old.keys() != new.keys()
            or any(stable(name, bytes.fromhex(old[name])) != stable(name, bytes.fromhex(new[name]))
                   for name in old if name != 'status')):
        raise FirmwareError('Mouse settings or assigned macros changed during restoration; read the mouse again')


def _targets(source, profiles, device_id, identity):
    targets, warnings = [], list(source.warnings)
    globals_seen = {}
    for slot, (profile, current) in enumerate(zip(source.profiles, profiles), 1):
        snapshot = _snapshot(device_id, slot, current, identity)
        merged = merge_supported_profile(profile, snapshot)
        config = merged.configuration
        if not isinstance(config, Configuration) or config.profile_slot != slot:
            raise FirmwareError('A restore target has an invalid profile')
        config.validate()
        sections = tuple(merged.sections)
        if len(sections) != len(set(sections)) or any(section not in REQUIRED for section in sections):
            raise FirmwareError('A restore target contains an unsupported settings section')
        if config.sensor.lift_off_distance != snapshot['configuration'].sensor.lift_off_distance:
            raise FirmwareError('Restore must preserve the current lift-off calibration')
        global_values = {}
        if 'sensor' in sections:
            global_values['debounce'] = config.sensor.debounce_ms
        if 'display' in sections:
            global_values.update(screen=(config.display.brightness, config.display.timeout_value),
                                 haptic=config.display.haptic_intensity)
            if config.display.background_index is not None:
                global_values['background'] = config.display.background_index
        if 'power' in sections:
            global_values.update(standby=config.power.standby_value, eco=config.power.eco_mode,
                                 energy_saving=config.power.energy_saving)
        for key, value in global_values.items():
            if key in globals_seen and globals_seen[key] != value:
                raise FirmwareError(f'The restore targets disagree on the shared {key} setting')
            globals_seen[key] = value
        targets.append({'profile_slot': slot, 'configuration': config.to_dict(), 'sections': list(sections)})
        warnings.extend(merged.warnings)
    if len(targets) != 5:
        raise FirmwareError('Restore requires all five profiles')
    return targets, list(dict.fromkeys(warnings))


def _inspect_section(normal, target, section, bundle):
    config = Configuration.from_dict(target['configuration'])
    raw = {name: bytes.fromhex(value) for name, value in bundle['settings'].items()}
    macro_plans, updates, disabling = {}, {}, {}
    if section == 'buttons':
        macro_plans, updates, disabling = prepare_macro_updates(
            normal, config, raw, bundle.get('macro_data', {}), bundle.get('macro_data', {}))
    elif section == 'display' and config.display.pages:
        macro_plans, updates, disabling = prepare_lcd_macro_updates(
            normal, config, raw['lcd'], bundle.get('macro_data', {}),
            bundle.get('macro_data', {}))
    commands, predicted = _plan(section, config, raw, config.profile_slot - 1,
                                macro_plans=macro_plans, force_layers=disabling,
                                force_lcd='lcd' in disabling)
    if any(command[1] == 0x19 for command in commands):
        raise FirmwareError('Restore cannot change lift-off calibration')
    adjustments = [{'profile_slot': config.profile_slot, 'layer': layer, 'logical_slot': logical,
                    'event_index': item.event_index, 'requested_ticks': item.requested_ticks,
                    'encoded_ticks': item.encoded_ticks}
                   for (layer, logical), plan in macro_plans.items() for item in plan.timing_adjustments]
    expected = deepcopy(bundle)
    expected['settings'].update({name: bytes(value).hex() for name, value in predicted.items()})
    expected['raw'] = expected['settings']['sensor']
    if section == 'buttons':
        # Preserve opaque assigned macros too. The ordinary transaction checks
        # requested macros, whereas restoration must also catch unrelated drift.
        assigned = {f'{layer}:{logical}' for layer in ('primary', 'easy_shift')
                    for logical in BUTTON_SLOTS if predicted[layer][7 + logical * 4] == 7}
        expected['macro_data'] = {key: value for key, value in bundle.get('macro_data', {}).items()
                                  if key.startswith('lcd:') or key in assigned}
        expected['macro_data'].update({f'{layer}:{logical}': plan.expected_read_payload.hex()
                                       for (layer, logical), plan in macro_plans.items()})
    elif section == 'display' and config.display.pages:
        lcd = decode('lcd', bytes(predicted['lcd']), config.profile_slot - 1)
        assigned = {
            f'lcd:{page * 4 + cell}'
            for page, current in enumerate(lcd.pages[:min(3, lcd.page_count)])
            for cell, widget in enumerate(current.slots)
            if widget is not None and widget.key == 'macro'
        }
        expected['macro_data'] = {
            key: value for key, value in bundle.get('macro_data', {}).items()
            if not key.startswith('lcd:') or key in assigned
        }
        expected['macro_data'].update({
            f'{layer}:{logical}': plan.expected_read_payload.hex()
            for (layer, logical), plan in macro_plans.items()
        })
    return bool(commands or updates), adjustments, expected


def _inspect_all(normal, targets, profiles):
    changes, adjustments = [], []
    for target, bundle in zip(targets, profiles):
        for section in target['sections']:
            changed, timing, _ = _inspect_section(normal, target, section, bundle)
            if changed:
                changes.append({'profile_slot': target['profile_slot'], 'section': section})
            adjustments.extend(timing)
    return changes, adjustments


def _binding(source, device_id, transport, status, cfu):
    value = source.value
    if value.get('device_id') != device_id or value.get('transport_identity') != _identity(transport):
        raise FirmwareError('This backup belongs to a different mouse USB identity')
    previous = decode_status_response(bytes.fromhex(value['status_raw']))
    previous_cfu = decode_version_response(bytes.fromhex(value['cfu_raw']))
    old_parts, parts = decode_realtek_version(previous_cfu.version_raw), decode_realtek_version(cfu.version_raw)
    if (previous.role != 'mouse' or status.role != 'mouse' or cfu.component_id != 0 or cfu.bank != 2
            or old_parts[:2] != parts[:2]
            or old_parts[2:] != (previous.firmware_major, previous.firmware_minor)
            or parts[2:] != (status.firmware_major, status.firmware_minor)):
        raise FirmwareError('The backup and current mouse firmware platform are incompatible')


def _preserved(bundle, initial):
    before = {name: bytes.fromhex(value) for name, value in initial['settings'].items()}
    after = {name: bytes.fromhex(value) for name, value in bundle['settings'].items()}
    profile = before['sensor'][3]
    if decode('sensor', before['sensor'], profile).lift_off_raw != decode('sensor', after['sensor'], profile).lift_off_raw:
        raise FirmwareError('The lift-off calibration changed during restoration')
    if decode('profile', before['profile'], profile).current_profile != decode('profile', after['profile'], profile).current_profile:
        raise FirmwareError('The active profile changed during restoration; read the mouse again')


def transact(request, *, transport_factory=FirmwareTransport, emit=lambda value: None):
    operation = request.get('operation')
    if operation not in ('prepare', 'apply'):
        raise FirmwareError('Unsupported settings restore operation')
    device_id = request.get('device_id')
    if not isinstance(device_id, str) or not device_id or len(device_id) > 1024:
        raise FirmwareError('Select the directly connected USB mouse')
    completed, may_have_changed = [], False
    try:
        plan = None
        if operation == 'prepare':
            source = read_backup(request['backup_path'], expected_sha256=request.get('backup_sha256'))
        else:
            plan = _load_plan(request.get('prepared'))
            if plan.get('device_id') != device_id:
                raise FirmwareError('The prepared restore belongs to another mouse')
            source = read_backup(plan['source_path'], expected_sha256=plan['source_sha256'])
            before_backup = read_backup(plan['current_backup_path'], expected_sha256=plan['current_backup_sha256'])
            if before_backup.value['profiles'] != plan.get('profiles'):
                raise FirmwareError('The prepared restore baseline changed')
            expected_targets, _ = _targets(source, plan['profiles'], device_id, plan['transport_identity'])
            if expected_targets != plan.get('targets'):
                raise FirmwareError('The prepared settings targets changed; prepare this restore again')
        # No USB handle is opened until all source files have been validated.
        with transport_factory(device_id) as transport:
            status = _status(transport)
            cfu = decode_version_response(transport.get_feature())
            _binding(source, device_id, transport, status, cfu)
            profiles = _read_profiles(transport)
            identity = _identity(transport)
            if operation == 'prepare':
                targets, warnings = _targets(source, profiles, device_id, identity)
                changes, adjustments = _inspect_all(transport.normal, targets, profiles)
                if adjustments:
                    warnings.append('Some macro delays will be rounded to the supported device timing; the preview lists these adjustments.')
                current = {'schema': 'swarm2.mc7.firmware-backup.v1', 'device_id': device_id,
                           'transport_identity': identity, 'archive_sha256': source.value['archive_sha256'],
                           'status_raw': status.raw.hex(), 'cfu_raw': cfu.raw.hex(), 'profiles': profiles,
                           'read_at': datetime.now(timezone.utc).isoformat(),
                           'limits': ('Before settings restore: readable settings and assigned macros only; '
                                      'custom LCD background pixels and Open Application icon pixels cannot be backed up.')}
                path, digest = _write_artifact(request['backup_directory'], current, 'mc7-before-restore-')
                read_backup(path, expected_sha256=digest)
                plan = {'schema': PLAN_SCHEMA, 'device_id': device_id, 'transport_identity': identity,
                        'source_path': str(Path(request['backup_path']).resolve()), 'source_sha256': source.sha256,
                        'current_backup_path': path, 'current_backup_sha256': digest,
                        'installed_numeric': status.firmware_numeric, 'cfu_raw': cfu.raw.hex(),
                        'profiles': profiles, 'targets': targets, 'warnings': warnings,
                        'macro_timing_adjustments': adjustments}
                plan_path, plan_digest = _write_artifact(request['backup_directory'], plan, 'mc7-restore-plan-')
                summary = (f'Restore supported settings across five profiles ({len(changes)} changed sections).\n'
                           f'Current settings saved to:\n{path}\n'
                           'The current active profile and lift-off calibration are preserved. '
                           'Custom LCD background pixels and Open Application icon pixels cannot be restored.\n'
                           'Restoration stops on the first failure; completed settings are not automatically rolled back.')
                return {'can_restore': bool(changes),
                        'reason': '' if changes else 'No supported changes are needed.',
                        'device_id': device_id, 'profile_count': 5,
                        'plan_path': plan_path, 'plan_sha256': plan_digest,
                        'current_backup_path': path, 'current_backup_sha256': digest,
                        'summary': summary, 'warnings': warnings, 'changes': changes,
                        'macro_timing_adjustments': adjustments}
            if (plan['transport_identity'] != identity or plan['cfu_raw'] != cfu.raw.hex()
                    or plan['installed_numeric'] != status.firmware_numeric):
                raise FirmwareError('The mouse or firmware changed since preparation; prepare again')
            _binding(before_backup, device_id, transport, status, cfu)
            _check_profiles(plan['profiles'], profiles)
            targets = plan['targets']
            # Validate all five target profiles, including macro compilation and
            # currently unassigned macro slots, before the first settings write.
            _inspect_all(transport.normal, targets, profiles)
            expected_profiles = deepcopy(profiles)
            total = sum(len(target['sections']) for target in targets)
            for target in targets:
                slot = target['profile_slot']
                for section in target['sections']:
                    bundle = read_bundle(transport.normal, slot - 1)
                    if bundle.get('errors'):
                        raise FirmwareError('Read every supported setting successfully before restoring a section')
                    _check_bundle(expected_profiles[slot - 1], bundle)
                    _preserved(bundle, profiles[slot - 1])
                    changed, _, predicted = _inspect_section(transport.normal, target, section, bundle)
                    _notify(emit, {'phase': 'restoring', 'profile_slot': slot, 'section': section,
                                   'percent': len(completed) * 100 // max(total, 1)})
                    baseline = _snapshot(device_id, slot, bundle, identity)['baseline']
                    may_have_changed |= changed
                    result = transact_settings({'operation': 'apply_settings', 'device_id': device_id,
                        'profile_slot': slot, 'section': section, 'configuration': target['configuration'],
                        'baseline': baseline}, transport.normal)
                    if result.get('errors'):
                        raise FirmwareError('Restore readback did not include every supported setting')
                    _check_bundle(predicted, result)
                    _preserved(result, profiles[slot - 1])
                    expected_profiles[slot - 1] = deepcopy(result)
                    for expected in expected_profiles:
                        for name, value in result['settings'].items():
                            if name not in PER_PROFILE and name != 'status':
                                expected['settings'][name] = value
                    completed.append({'profile_slot': slot, 'section': section, 'changed': result['changed']})
            _notify(emit, {'phase': 'verifying', 'percent': 100})
            after = _read_profiles(transport)
            try:
                _check_profiles(expected_profiles, after)
            except FirmwareError as error:
                raise FirmwareError(f'Final readback changed outside the verified restore transactions: {error}') from error
            for initial, final in zip(profiles, after):
                _preserved(final, initial)
            remaining, _ = _inspect_all(transport.normal, targets, after)
            if remaining:
                raise FirmwareError('Final readback did not match every supported restore target')
            if (_status(transport).firmware_numeric != plan['installed_numeric']
                    or decode_version_response(transport.get_feature()).raw.hex() != plan['cfu_raw']):
                raise FirmwareError('The firmware changed during settings restoration')
            return {'verified': True, 'outcome': 'restored', 'completed': completed,
                    'profiles_read_back': len(after), 'current_backup_path': plan['current_backup_path'],
                    'warnings': plan['warnings'], 'macro_timing_adjustments': plan['macro_timing_adjustments']}
    except (OSError, ValueError, DeviceError, KeyError, TypeError) as error:
        note = ' Some settings may have changed; read the mouse again before retrying.' if may_have_changed else ''
        raise RestoreError(str(error) + note, device_may_have_changed=may_have_changed, completed=completed) from error


def main():
    watchdog = threading.Timer(HARD_TIMEOUT_SECONDS, lambda: os._exit(3))
    watchdog.daemon = True
    watchdog.start()
    def progress(value):
        try:
            print(json.dumps(value), flush=True)
        except (OSError, BrokenPipeError):
            pass
    try:
        raw = sys.stdin.buffer.read(16385)
        if len(raw) > 16384:
            raise FirmwareError('Settings restore request is too large')
        request = json.loads(raw, object_pairs_hook=_object)
        if not isinstance(request, dict):
            raise FirmwareError('Settings restore request must be an object')
        result = transact(request, emit=progress)
        print(json.dumps({'result': result}), flush=True)
        return 0
    except (OSError, ValueError, DeviceError, KeyError, TypeError) as error:
        print(json.dumps({'error': str(error),
                          'device_may_have_changed': getattr(error, 'device_may_have_changed', False),
                          'completed': getattr(error, 'completed', [])}), flush=True)
        return 2
    finally:
        watchdog.cancel()


if __name__ == '__main__':
    raise SystemExit(main())
