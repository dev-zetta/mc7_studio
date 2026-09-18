"""Restore transactions against independent byte-level mouse fixtures."""

from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from swarm2 import restore_hardware
from swarm2.firmware_backup import read_backup
from swarm2.firmware_catalog import known_releases
from swarm2.macro_profiles import plan_macro_upload
from swarm2.screen_key_commands import encode_lcd_macro_record
from swarm2.settings import PER_PROFILE, SELECTORS, decode, read_bundle
from swarm2.transport import DeviceError
from tests.test_firmware_hardware import status_response, version_response
from tests.test_macro_integration import MacroTransport, tap
from tests.test_settings import (EMPTY_SCREEN_KEY, screen_key_capture, with_checksum,
                                 with_lcd_page, with_screen_key_records)


class ProfileStorage(MacroTransport):
    @staticmethod
    def assert_packet(packet):
        if len(packet) != 64 or not 0 <= packet[3] < 5 or packet[6] != sum(packet[:6]) & 255:
            raise AssertionError('Invalid macro read/write selection')

    def send(self, packet):
        if packet[1] in (0x14, 0x10, 0x11):
            self.sent.append(packet)
            self.writes.append(packet)
            raw = bytearray(self.records['sensor'])
            if packet[1] == 0x14:
                raw[9] = packet[2] & 15
                for index in range(5):
                    raw[10+7*index] = (packet[3] >> index) & 1
                    raw[11+7*index:13+7*index] = packet[4+2*index:6+2*index]
                    raw[13+7*index:16+7*index] = packet[14+3*index:17+3*index]
                    raw[16+7*index] = (packet[3] >> 7) & 1
            elif packet[1] == 0x10:
                raw[4] = raw[5] = packet[3] & 15
                raw[46] = (packet[3] >> 4) & 1
            else:
                raw[6:9] = bytes((packet[4], packet[3], not bool(packet[2] & 0xA0)))
            self.records['sensor'] = with_checksum(raw)
            return
        super().send(packet)


class World:
    def __init__(self):
        self.identity = 'fixture-usb-port'
        self.version = version_response()
        self.profiles = [ProfileStorage() for _ in range(5)]
        for index, profile in enumerate(self.profiles):
            for name in PER_PROFILE:
                if name == 'screen_keys':
                    profile.records[name] = screen_key_capture(index)
                    continue
                raw = bytearray(profile.records[name])
                raw[3] = index
                profile.records[name] = with_checksum(raw)
            profile.records['status'] = status_response()
        self.sessions, self.writes = [], []
        self.fail_write = None
        self.mutate_on_write = None

    def open(self, device_id):
        session = Transport(self, device_id)
        self.sessions.append(session)
        return session

    def set_global(self, name, offset, value):
        for profile in self.profiles:
            raw = bytearray(profile.records[name])
            raw[offset] = value
            profile.records[name] = with_checksum(raw)

    def upgrade(self, number=509):
        self.version = version_response(number=number)
        for profile in self.profiles:
            profile.records['status'] = status_response(number)


class Transport:
    def __init__(self, world, device_id):
        self.world, self.device_id = world, device_id
        self.normal = self
        self.location_id = world.identity
        self.acknowledgements = []
        self.selected_profile = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def get_feature(self, selector=None):
        if selector is None:
            return self.world.version
        return self.world.profiles[self.selected_profile].get_feature(selector)

    def send(self, packet):
        command = packet[1]
        is_read = command == 0x1C and packet[2] != 2
        if command == 0x1C:
            if packet[2] in (2, 3):
                self.selected_profile = packet[3]
            else:
                self.selected_profile = packet[4]
        elif command == 0x14:
            self.selected_profile = packet[2] >> 4
        elif command in (0x10, 0x11):
            self.selected_profile = packet[2] & 15
        elif command in (0x15, 0x16, 0x2A):
            self.selected_profile = packet[2]
        elif command == 0x25:
            self.selected_profile = packet[3]
        elif command == 0x29:
            self.selected_profile = packet[4]
        if not is_read:
            if command in (0x13, 0x19):
                raise AssertionError('Restore attempted reset or lift-off calibration')
            self.world.writes.append(packet)
            if len(self.world.writes) == self.world.fail_write:
                raise DeviceError('Simulated settings disconnect')
        self.world.profiles[self.selected_profile].send(packet)
        # Global reports affect every profile; the fixture deliberately models
        # this shared state to catch stale per-profile baselines.
        global_name = {0x2B: 'screen', 0x2C: 'background', 0x24: 'haptic', 0x05: 'standby',
                       0x26: 'eco', 0x1A: 'debounce', 0x12: 'profile'}.get(command)
        if global_name:
            value = self.world.profiles[self.selected_profile].records[global_name]
            for profile in self.world.profiles:
                profile.records[global_name] = value
        if not is_read and self.world.mutate_on_write:
            self.world.mutate_on_write(self.world)

    def set_output(self, report):
        raise AssertionError('Settings restore must never send CFU output')


class RestoreHardwareTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.world = World()
        self.source = Path(self.directory.name) / 'source.json'
        self.save_source()
        self.progress = []

    def save_source(self):
        with self.world.open('fixture-device') as transport:
            profiles = [read_bundle(transport, index) for index in range(5)]
        value = {'schema': 'swarm2.mc7.firmware-backup.v1', 'device_id': 'fixture-device',
                 'transport_identity': self.world.identity, 'archive_sha256': known_releases()[0].sha256,
                 'status_raw': self.world.profiles[0].records['status'].hex(),
                 'cfu_raw': self.world.version.hex(), 'profiles': profiles,
                 'read_at': '2026-09-16T10:00:00+00:00', 'limits': 'Synthetic test backup.'}
        self.source.write_text(json.dumps(value))
        self.world.sessions.clear()
        self.world.writes.clear()

    def prepare(self, **changes):
        return restore_hardware.transact({'operation': 'prepare', 'device_id': 'fixture-device',
            'backup_path': str(self.source), 'backup_directory': self.directory.name, **changes},
            transport_factory=self.world.open, emit=self.progress.append)

    def apply(self, prepared):
        return restore_hardware.transact({'operation': 'apply', 'device_id': 'fixture-device', 'prepared': prepared},
            transport_factory=self.world.open, emit=self.progress.append)

    def mutate_source(self, transform):
        value = json.loads(self.source.read_text())
        transform(value)
        self.source.write_text(json.dumps(value))

    def test_prepare_is_read_only_and_creates_hashed_private_artifacts(self):
        prepared = self.prepare()
        self.assertFalse(prepared['can_restore'])
        self.assertEqual(prepared['reason'], 'No supported changes are needed.')
        self.assertEqual(prepared['changes'], [])
        self.assertEqual(self.world.writes, [])
        for key, digest in (('plan_path', 'plan_sha256'), ('current_backup_path', 'current_backup_sha256')):
            path = Path(prepared[key])
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), prepared[digest])
        current_backup = json.loads(Path(prepared['current_backup_path']).read_text())
        self.assertIn('Open Application icon pixels', current_backup['limits'])
        self.assertIn('Open Application icon pixels', prepared['summary'])
        read_backup(prepared['current_backup_path'])
        self.assertTrue(all(session.closed for session in self.world.sessions))

    def test_unchanged_restore_verifies_all_five_without_settings_writes(self):
        result = self.apply(self.prepare())
        self.assertTrue(result['verified'])
        self.assertEqual(result['profiles_read_back'], 5)
        self.assertEqual(len(result['completed']), 25)
        self.assertFalse(any(item['changed'] for item in result['completed']))
        self.assertEqual(self.world.writes, [])

    def test_restore_after_upgrade_preserves_current_active_profile_and_calibration(self):
        self.world.upgrade()
        self.world.set_global('profile', 3, 3)
        self.world.set_global('screen', 3, 60)
        self.world.set_global('standby', 3, 12)
        for index, profile in enumerate(self.world.profiles):
            raw = bytearray(profile.records['sensor'])
            raw[45] = 0x92  # A custom DCU result must survive every profile.
            raw[9] = index % 4
            profile.records['sensor'] = with_checksum(raw)
            raw = bytearray(profile.records['lighting'])
            raw[5] = 128
            profile.records['lighting'] = with_checksum(raw)
        prepared = self.prepare()
        self.assertTrue(prepared['can_restore'])
        result = self.apply(prepared)
        self.assertTrue(result['verified'])
        self.assertEqual({item['profile_slot'] for item in result['completed']}, set(range(1, 6)))
        for profile in self.world.profiles:
            self.assertEqual(profile.records['profile'][3], 3)
            self.assertEqual(profile.records['sensor'][45], 0x92)
            self.assertEqual(profile.records['sensor'][9], 4)
            self.assertEqual(profile.records['screen'][3], 100)
            self.assertEqual(profile.records['standby'][3], 3)
            self.assertEqual(profile.records['lighting'][5], 255)
        self.assertEqual(sum(report[1] == 0x2B for report in self.world.writes), 1)
        self.assertEqual(sum(report[1] == 0x05 for report in self.world.writes), 1)
        self.assertFalse(any(report[1] in (0x13, 0x19) for report in self.world.writes))

    def test_restore_504_compact_lighting_onto_509_extended_layout(self):
        # Exact per-profile records from the firmware 5.04 backup made before
        # the hardware upgrade, and their firmware 5.09 extended equivalents.
        compact = (
            '102a00000fff0505000000e8',
            '102a00010fff0105ff0000ec',
            '102a00020fff010500ff00eb',
            '102a00030fff01050000ffea',
            '102a00040fff0105ff8c005d',
        )
        restored_extended = (
            '102a0000010fff0505000000e7',
            '102a0001010fff0105ff0000eb',
            '102a0002010fff010500ff00ea',
            '102a0003010fff01050000ffe9',
            '102a0004010fff0105ff8c005c',
        )
        for profile, record in zip(self.world.profiles, compact):
            profile.records['lighting'] = bytes.fromhex(record)
        self.save_source()

        # Give each 5.09 profile a different timeout and unrelated visible
        # settings. The extended enable byte must remain in byte 4 while the
        # compact backup timeout moves from byte 4 to byte 5.
        target_timeouts = (1, 4, 7, 10, 13)
        for index, (profile, record, timeout) in enumerate(
                zip(self.world.profiles, restored_extended, target_timeouts)):
            raw = bytearray.fromhex(record)
            raw[4] = 1
            raw[5] = timeout
            raw[6] = 64 + index
            raw[7] = 6
            raw[8] = 9
            raw[9:12] = bytes((16 + index, 32 + index, 48 + index))
            profile.records['lighting'] = with_checksum(raw)
        self.world.upgrade(509)

        prepared = self.prepare()
        self.assertEqual(
            {(change['profile_slot'], change['section']) for change in prepared['changes']},
            {(slot, section) for slot in range(1, 6) for section in ('lighting', 'power')},
        )
        result = self.apply(prepared)
        self.assertTrue(result['verified'])
        self.assertEqual(result['profiles_read_back'], 5)

        lighting_writes = [report for report in self.world.writes if report[1] == 0x2A]
        self.assertEqual(len(lighting_writes), 10)
        colors = ((0, 0, 0), (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 140, 0))
        effects = ('aimo', 'static', 'static', 'static', 'static')
        effect_ids = (5, 1, 1, 1, 1)
        for index, (profile, expected, initial_timeout) in enumerate(
                zip(self.world.profiles, restored_extended, target_timeouts)):
            writes = [report for report in lighting_writes if report[2] == index]
            self.assertEqual(len(writes), 2)
            # Lighting restoration keeps the current 5.09-only enable and
            # timeout bytes while changing only the source-visible fields.
            self.assertEqual(writes[0][4:6], bytes((1, initial_timeout)))
            self.assertEqual(writes[0][6:9], bytes((255, effect_ids[index], 5)))
            self.assertEqual(writes[0][9:12], bytes(colors[index]))
            # Power restoration then writes the compact source timeout at its
            # extended offset without shifting brightness/effect/RGB fields.
            self.assertEqual(writes[1][4:6], bytes((1, 15)))
            self.assertEqual(writes[1][6:12], writes[0][6:12])
            self.assertEqual(writes[0][12:], bytes(52))
            self.assertEqual(writes[1][12:], bytes(52))

            self.assertEqual(profile.records['lighting'].hex(), expected)
            state = decode('lighting', profile.records['lighting'], index)
            self.assertEqual(state.enabled_raw, 1)
            self.assertEqual(state.led_timeout_raw, 15)
            self.assertEqual(state.brightness_percent, 100)
            self.assertEqual(state.effect, effects[index])
            self.assertEqual(state.speed, 5)
            self.assertEqual(state.color, colors[index])

    def test_invalid_source_is_rejected_before_opening_usb(self):
        self.source.write_text('{"schema":"unsupported"}')
        with self.assertRaises(restore_hardware.RestoreError) as caught:
            self.prepare()
        self.assertFalse(caught.exception.device_may_have_changed)
        self.assertEqual(self.world.sessions, [])

    def test_inspected_source_hash_mismatch_rejects_prepare_before_usb(self):
        with self.assertRaisesRegex(restore_hardware.RestoreError, 'backup changed') as caught:
            self.prepare(backup_sha256='0' * 64)
        self.assertFalse(caught.exception.device_may_have_changed)
        self.assertEqual(self.world.sessions, [])

    def test_identity_or_firmware_platform_mismatch_blocks_prepare(self):
        self.world.identity = 'different-usb-port'
        with self.assertRaisesRegex(restore_hardware.RestoreError, 'different mouse'):
            self.prepare()
        self.assertEqual(self.world.writes, [])
        self.world.identity = 'fixture-usb-port'
        version = bytearray(self.world.version)
        version[5] ^= 1
        self.world.version = bytes(version)
        with self.assertRaisesRegex(restore_hardware.RestoreError, 'incompatible'):
            self.prepare()
        self.assertEqual(self.world.writes, [])

    def test_source_plan_or_current_backup_edit_blocks_apply_before_usb(self):
        for key in ('source', 'plan_path', 'current_backup_path'):
            with self.subTest(key=key):
                prepared = self.prepare()
                path = self.source if key == 'source' else Path(prepared[key])
                original = path.read_bytes()
                path.write_bytes(original + b' ')
                self.world.sessions.clear()
                with self.assertRaises(restore_hardware.RestoreError) as caught:
                    self.apply(prepared)
                self.assertFalse(caught.exception.device_may_have_changed)
                self.assertEqual(self.world.sessions, [])
                path.write_bytes(original)

    def test_rehashed_tampered_target_is_recomputed_before_usb(self):
        prepared = self.prepare()
        path = Path(prepared['plan_path'])
        value = json.loads(path.read_text())
        value['targets'][0]['configuration']['sensor']['debounce_ms'] = 10
        path.write_text(json.dumps(value))
        prepared['plan_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.world.sessions.clear()
        with self.assertRaisesRegex(restore_hardware.RestoreError, 'targets changed'):
            self.apply(prepared)
        self.assertEqual(self.world.sessions, [])

    def test_stale_settings_identity_or_firmware_blocks_every_write(self):
        for kind in ('settings', 'identity', 'firmware', 'active'):
            with self.subTest(kind=kind):
                self.world = World()
                self.save_source()
                prepared = self.prepare()
                if kind == 'settings':
                    self.world.set_global('screen', 3, 77)
                elif kind == 'active':
                    self.world.set_global('profile', 3, 1)
                elif kind == 'identity':
                    self.world.identity = 'new-port'
                else:
                    self.world.upgrade()
                with self.assertRaises(restore_hardware.RestoreError) as caught:
                    self.apply(prepared)
                self.assertFalse(caught.exception.device_may_have_changed)
                self.assertEqual(self.world.writes, [])

    def test_battery_telemetry_change_does_not_block_restore(self):
        prepared = self.prepare()
        self.world.set_global('status', 9, 78)
        self.assertTrue(self.apply(prepared)['verified'])
        self.assertEqual(self.world.writes, [])

    def test_macro_validation_of_last_profile_happens_before_first_write(self):
        self.world.set_global('screen', 3, 60)
        prepared = self.prepare()
        original = restore_hardware.prepare_macro_updates
        def reject_last(normal, config, *args):
            if config.profile_slot == 5:
                raise DeviceError('Last profile macro cannot be compiled')
            return original(normal, config, *args)
        with patch.object(restore_hardware, 'prepare_macro_updates', side_effect=reject_last):
            with self.assertRaisesRegex(restore_hardware.RestoreError, 'cannot be compiled') as caught:
                self.apply(prepared)
        self.assertFalse(caught.exception.device_may_have_changed)
        self.assertEqual(self.world.writes, [])

    def test_partial_failure_reports_completed_sections_without_retry_or_rollback(self):
        self.world.set_global('screen', 3, 60)
        self.world.set_global('standby', 3, 12)
        prepared = self.prepare()
        self.world.fail_write = 2
        with self.assertRaisesRegex(restore_hardware.RestoreError, 'may have changed') as caught:
            self.apply(prepared)
        self.assertTrue(caught.exception.device_may_have_changed)
        self.assertTrue(any(item['section'] == 'display' and item['changed'] for item in caught.exception.completed))
        self.assertFalse(any(item['section'] == 'power' for item in caught.exception.completed))
        self.assertEqual(len(self.world.writes), 2)
        self.assertEqual(self.world.profiles[0].records['screen'][3], 100)
        self.assertEqual(self.world.profiles[0].records['standby'][3], 12)

    def test_active_profile_change_during_restore_stops_with_uncertainty(self):
        self.world.set_global('screen', 3, 60)
        prepared = self.prepare()
        self.world.mutate_on_write = lambda world: world.set_global('profile', 3, 2)
        with self.assertRaises(restore_hardware.RestoreError) as caught:
            self.apply(prepared)
        self.assertTrue(caught.exception.device_may_have_changed)
        self.assertEqual(len(self.world.writes), 1)

    def test_progress_observer_failure_does_not_abort_settings_restore(self):
        self.world.set_global('screen', 3, 60)
        prepared = self.prepare()
        def broken(value):
            raise RuntimeError('The UI disappeared')
        result = restore_hardware.transact({'operation': 'apply', 'device_id': 'fixture-device', 'prepared': prepared},
            transport_factory=self.world.open, emit=broken)
        self.assertTrue(result['verified'])

    def test_noop_restore_reports_read_errors_without_writes(self):
        prepared = self.prepare()
        self.world.profiles[4].read_errors['lcd'] = 'Synthetic missing LCD read'
        with self.assertRaises(restore_hardware.RestoreError) as caught:
            self.apply(prepared)
        self.assertFalse(caught.exception.device_may_have_changed)
        self.assertEqual(self.world.writes, [])

    def test_restore_keyboard_macro_uses_complete_upload_and_readback(self):
        self.world.profiles[0].seed(tap())
        self.save_source()
        self.world.profiles[0].set_record('primary', 5, bytes.fromhex('00000501'))
        self.world.profiles[0].storage.clear()
        result = self.apply(self.prepare())
        self.assertTrue(result['verified'])
        self.assertEqual(self.world.profiles[0].payload_writes, 17)
        self.assertEqual(self.world.profiles[0].record('primary', 5)[3], 7)
        self.assertIn('primary:5', read_bundle(self.world.open('fixture-device'), 0)['macro_data'])

    def test_restore_lcd_macro_uses_coordinate_slot_and_complete_readback(self):
        profile = self.world.profiles[0]
        macro = tap(name='LCD restore')
        plan = plan_macro_upload(macro, profile_index=0, logical_slot=8, layer='lcd')
        profile.records['lcd'] = with_lcd_page(
            profile.records['lcd'], 2,
            (b'\x05\x02', b'\x19\xff', b'\x1a\xff', b'\x1f\xff'))
        profile.records['screen_keys'] = with_screen_key_records(
            profile.records['screen_keys'], 2,
            (encode_lcd_macro_record(macro.name), EMPTY_SCREEN_KEY,
             EMPTY_SCREEN_KEY, EMPTY_SCREEN_KEY))
        profile.storage[('lcd', 8)] = bytearray(
            plan.expected_read_payload + bytes(1054 - len(plan.expected_read_payload)))
        self.save_source()

        profile.records['lcd'] = with_lcd_page(
            profile.records['lcd'], 2,
            (b'\x19\xff', b'\x1a\xff', b'\x1f\xff', b'\x47\xff'))
        profile.records['screen_keys'] = screen_key_capture(0)
        profile.storage.clear()

        prepared = self.prepare()
        self.assertIn({'profile_slot': 1, 'section': 'display'}, prepared['changes'])
        result = self.apply(prepared)

        self.assertTrue(result['verified'])
        self.assertEqual(profile.payload_writes, 17)
        restored = read_bundle(self.world.open('fixture-device'), 0)
        self.assertEqual(restored['macro_data']['lcd:8'], plan.expected_read_payload.hex())
        self.assertEqual(decode('lcd', profile.records['lcd'], 0).pages[2].slots[0].key,
                         'macro')

    def test_restore_preserves_opaque_current_lcd_macro_page_and_image(self):
        profile = self.world.profiles[0]
        profile.records['lcd'] = with_lcd_page(
            profile.records['lcd'], 2,
            (b'\x05\x02', b'\x19\xff', b'\x1a\xff', b'\x1f\xff'))
        opaque_trigger = bytes.fromhex('0001010700') + b'Held\0\0'
        profile.records['screen_keys'] = with_screen_key_records(
            profile.records['screen_keys'], 2,
            (opaque_trigger, EMPTY_SCREEN_KEY, EMPTY_SCREEN_KEY, EMPTY_SCREEN_KEY))
        opaque_image = b'\x7f' * 1037
        profile.storage[('lcd', 8)] = bytearray(
            opaque_image + bytes(1054 - len(opaque_image)))
        before_lcd = profile.records['lcd']
        before_keys = profile.records['screen_keys']

        prepared = self.prepare()
        result = self.apply(prepared)

        self.assertTrue(result['verified'])
        self.assertTrue(any('opaque current macro tile' in warning
                            for warning in prepared['warnings']))
        self.assertEqual(profile.payload_writes, 0)
        self.assertEqual(profile.records['lcd'], before_lcd)
        self.assertEqual(profile.records['screen_keys'], before_keys)
        self.assertEqual(read_bundle(self.world.open('fixture-device'), 0)
                         ['macro_data']['lcd:8'], opaque_image.hex())

    def test_macro_transfer_failure_is_reported_without_binding_or_retry(self):
        self.world.profiles[0].seed(tap())
        self.save_source()
        profile = self.world.profiles[0]
        profile.set_record('primary', 5, bytes.fromhex('00000501'))
        profile.storage.clear()
        prepared = self.prepare()
        profile.fail_payload = 4
        with self.assertRaises(restore_hardware.RestoreError) as caught:
            self.apply(prepared)
        self.assertTrue(caught.exception.device_may_have_changed)
        self.assertEqual(profile.payload_writes, 4)
        self.assertEqual(profile.record('primary', 5), bytes.fromhex('00000501'))
        self.assertFalse(any(item['section'] == 'buttons' for item in caught.exception.completed))

    def test_final_readback_detects_changes_after_individual_section_checks(self):
        self.world.set_global('screen', 3, 60)
        prepared = self.prepare()
        original, calls = restore_hardware._read_profiles, []
        def drift_on_final(transport):
            calls.append(True)
            if len(calls) == 2:
                self.world.set_global('screen', 3, 80)
            return original(transport)
        with patch.object(restore_hardware, '_read_profiles', side_effect=drift_on_final):
            with self.assertRaisesRegex(restore_hardware.RestoreError, 'Final readback') as caught:
                self.apply(prepared)
        self.assertTrue(caught.exception.device_may_have_changed)
        self.assertEqual(len(caught.exception.completed), 25)
        self.assertEqual(sum(packet[1] == 0x2B for packet in self.world.writes), 1)

    def test_final_hidden_record_drift_cannot_be_adopted_as_preserved_state(self):
        self.world.profiles[0].set_record('primary', 9, bytes.fromhex('deadbeef'))
        self.world.set_global('screen', 3, 60)
        prepared = self.prepare()
        original, calls = restore_hardware._read_profiles, []
        def drift_on_final(transport):
            calls.append(True)
            if len(calls) == 2:
                self.world.profiles[0].set_record('primary', 9, bytes.fromhex('cafebabe'))
            return original(transport)
        with patch.object(restore_hardware, '_read_profiles', side_effect=drift_on_final):
            with self.assertRaisesRegex(restore_hardware.RestoreError, 'Final readback') as caught:
                self.apply(prepared)
        self.assertTrue(caught.exception.device_may_have_changed)
        self.assertEqual(len(caught.exception.completed), 25)

    def test_between_section_hidden_record_drift_stops_before_next_write(self):
        self.world.profiles[0].set_record('primary', 9, bytes.fromhex('deadbeef'))
        self.world.set_global('screen', 3, 60)
        self.world.set_global('standby', 3, 12)
        prepared = self.prepare()
        original = restore_hardware.transact_settings
        def drift_after_display(request, transport):
            result = original(request, transport)
            if request['profile_slot'] == 1 and request['section'] == 'display':
                self.world.profiles[0].set_record('primary', 9, bytes.fromhex('cafebabe'))
            return result
        with patch.object(restore_hardware, 'transact_settings', side_effect=drift_after_display):
            with self.assertRaisesRegex(restore_hardware.RestoreError, 'changed during restoration') as caught:
                self.apply(prepared)
        self.assertTrue(caught.exception.device_may_have_changed)
        self.assertTrue(any(item['section'] == 'display' for item in caught.exception.completed))
        self.assertFalse(any(item['section'] == 'power' for item in caught.exception.completed))
        self.assertEqual(len(self.world.writes), 1)

    def test_drift_inside_noop_transaction_is_not_adopted_into_rolling_baseline(self):
        self.world.profiles[0].set_record('primary', 9, bytes.fromhex('deadbeef'))
        self.world.set_global('screen', 3, 60)
        prepared = self.prepare()
        original = restore_hardware.transact_settings
        def drift_before_noop(request, transport):
            if request['profile_slot'] == 1 and request['section'] == 'sensor':
                self.world.profiles[0].set_record('primary', 9, bytes.fromhex('cafebabe'))
            return original(request, transport)
        with patch.object(restore_hardware, 'transact_settings', side_effect=drift_before_noop):
            with self.assertRaisesRegex(restore_hardware.RestoreError, 'changed during restoration') as caught:
                self.apply(prepared)
        self.assertFalse(caught.exception.device_may_have_changed)
        self.assertEqual(caught.exception.completed, [])
        self.assertEqual(self.world.writes, [])

    def test_hidden_and_opaque_button_records_survive_supported_restore_writes(self):
        profile = self.world.profiles[0]
        profile.set_record('primary', 9, bytes.fromhex('deadbeef'))
        profile.set_record('primary', 7, bytes.fromhex('cafeba00'))
        profile.set_record('primary', 5, bytes.fromhex('00000403'))
        profile.set_record('easy_shift', 9, bytes.fromhex('aabbccfe'))
        before = {('primary', 9): profile.record('primary', 9),
                  ('primary', 7): profile.record('primary', 7),
                  ('easy_shift', 9): profile.record('easy_shift', 9)}
        prepared = self.prepare()
        self.assertTrue(any('opaque action' in warning for warning in prepared['warnings']))
        self.assertTrue(self.apply(prepared)['verified'])
        self.assertEqual(profile.record('primary', 5), bytes.fromhex('00000501'))
        for (layer, slot), expected in before.items():
            self.assertEqual(profile.record(layer, slot), expected)
        self.assertTrue(any(report[1] == 0x15 for report in self.world.writes))

    def test_main_rejects_nonobject_and_emits_partial_failure_fields(self):
        for request in ([], {'operation': 'apply'}):
            output = io.StringIO()
            error = restore_hardware.RestoreError('Partial failure', device_may_have_changed=True,
                                                  completed=[{'profile_slot': 1, 'section': 'sensor'}])
            with patch.object(restore_hardware.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode()))), \
                    patch.object(restore_hardware, 'transact', side_effect=error), redirect_stdout(output):
                self.assertEqual(restore_hardware.main(), 2)
            result = json.loads(output.getvalue())
            self.assertIn('error', result)
            if isinstance(request, dict):
                self.assertTrue(result['device_may_have_changed'])
                self.assertEqual(result['completed'], error.completed)


if __name__ == '__main__':
    unittest.main()
