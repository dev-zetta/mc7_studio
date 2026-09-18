"""Firmware helper transactions against synthetic mouse state and local backups."""

import copy
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from swarm2 import firmware_hardware, restore_hardware
from swarm2.firmware_catalog import FirmwareError, known_releases
from swarm2.firmware_package import FirmwarePackage
from swarm2.firmware_update import FirmwareUpdateError
from swarm2.settings import PER_PROFILE, SELECTORS
from swarm2.transport import DeviceError
from tests.test_firmware_commands import OFFER, VERSION, payload
from tests.test_firmware_update import FakeFirmwareTransport
from tests.test_settings import (CAPTURES, SettingsTransport, screen_key_capture,
                                 with_checksum)


def version_response(target=False, *, number=None):
    raw = bytearray(VERSION)
    if target:
        raw[5:9] = OFFER[4:8]
    if number is not None:
        major, minor = divmod(number, 100)
        raw[5:9] = ((minor << 27) | (major << 12) | 0x71).to_bytes(4, "little")
    return bytes(raw)


def status_response(number=504, battery=99):
    raw = bytearray.fromhex(CAPTURES["status"])
    major, minor = divmod(number, 100)
    raw[3:5] = bytes(((minor // 10) * 16 + minor % 10, (major // 10) * 16 + major % 10))
    raw[9] = battery
    return with_checksum(raw)


class ProfileTransport(SettingsTransport):
    def __init__(self, world):
        super().__init__()
        self.world = world
        self.location_id = world.identity
        self.read_errors = world.read_errors
        self.sent = world.normal_sent
        self.selected_profile = 0

    def send(self, report):
        # Preparation/backup may select reads but must never update settings.
        if report[1] != 0x1C:
            raise AssertionError("An ordinary write escaped the firmware/reset guards")
        self.selected_profile = report[4]
        super().send(report)

    def get_feature(self, selector):
        name = next(name for name, value in SELECTORS.items() if value == selector)
        if self.selected != selector:
            raise AssertionError("GET without matching read selector")
        if name in self.read_errors:
            raise DeviceError(self.read_errors[name])
        if name == "status":
            return self.world.status
        index = self.selected_profile if name in PER_PROFILE else 0
        if name == "screen_keys":
            start = self.selected_page * 54
            return self.world.profiles[index][name][start:start + 54]
        return self.world.profiles[index][name]


class UpdateTransport(FakeFirmwareTransport):
    def __init__(self, world, device_id):
        super().__init__()
        self.world = world
        self.device_id = device_id
        self.normal = ProfileTransport(world)
        self.writes = world.outputs
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def get_feature(self, report_id=0x2A, length=64):
        if (report_id, length) != (0x2A, 64):
            raise AssertionError("Wrong CFU feature request")
        return self.world.version

    def set_output(self, report):
        if self.world.transfer_failure:
            self.writes.append(report)
            raise OSError("Fixture output failed")
        super().set_output(report)
        if report[0] == 0x2A and report[1] & 0x40:
            if self.world.upgrade_status:
                self.world.status = status_response(509)
            if self.world.upgrade_cfu:
                self.world.version = version_response(True)


class World:
    def __init__(self):
        self.identity = "fixture-usb-port"
        self.status = status_response()
        self.version = VERSION
        self.outputs, self.normal_sent, self.sessions = [], [], []
        self.now = 100.0
        self.read_errors = {}
        self.transfer_failure = False
        self.upgrade_status = self.upgrade_cfu = True
        self.on_open = None
        self.profiles = []
        for index in range(5):
            records = {name: bytes.fromhex(raw) for name, raw in CAPTURES.items()}
            for name in PER_PROFILE:
                if name == "screen_keys":
                    records[name] = screen_key_capture(index)
                    continue
                raw = bytearray(records[name])
                raw[3] = index
                records[name] = with_checksum(raw)
            self.profiles.append(records)

    def open(self, device_id):
        if self.on_open:
            self.on_open(len(self.sessions))
        session = UpdateTransport(self, device_id)
        self.sessions.append(session)
        return session

    def sleep(self, seconds):
        self.now += seconds


class FirmwareHardwareTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.world = World()
        self.release = known_releases()[0]
        self.package = FirmwarePackage(self.release, OFFER, payload(3), "5.9.0.0", 509, 506, 15)
        inspector = patch("swarm2.firmware_hardware.inspect_firmware_package", return_value=self.package)
        self.inspector = inspector.start()
        self.addCleanup(inspector.stop)
        for name, replacement in (("sleep", lambda seconds: self.world.sleep(seconds)),
                                  ("monotonic", lambda: self.world.now)):
            timer = patch("swarm2.firmware_hardware.time." + name, side_effect=replacement)
            timer.start()
            self.addCleanup(timer.stop)
        self.reset_result = {"acknowledged": True, "reset_completed": True,
                              "completion_event": "1000b10000000000"}
        resetter = patch("swarm2.firmware_reset.perform_factory_reset", return_value=self.reset_result)
        self.resetter = resetter.start()
        self.addCleanup(resetter.stop)
        self.progress = []

    def request(self, operation="prepare", **changes):
        return {"operation": operation, "role": "mouse", "device_id": "fixture-port",
                "release_key": self.release.key,
                "archive_path": str(Path(self.directory.name) / "known.7z"),
                "backup_directory": self.directory.name, **changes}

    def transact(self, request):
        return firmware_hardware.transact(request, transport_factory=self.world.open,
                                           emit=self.progress.append)

    def prepare(self):
        return self.transact(self.request())

    def install(self, prepared):
        return self.transact(self.request("install", prepared=prepared))

    def test_prepare_only_reads_and_persists_complete_backup_before_exposing_install(self):
        result = self.prepare()
        self.assertTrue(result["can_update"])
        self.assertTrue(result["reset_required"])
        self.assertEqual(result["installed_numeric"], 504)
        path = Path(result["backup_path"])
        raw = path.read_bytes()
        backup = json.loads(raw)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), result["backup_sha256"])
        if os.name == "posix":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(backup["profiles"]), 5)
        self.assertEqual(backup["device_id"], "fixture-port")
        self.assertEqual(backup["transport_identity"], "fixture-usb-port")
        self.assertEqual(backup["release_key"], self.release.key)
        self.assertEqual(backup["archive_sha256"], self.release.sha256)
        self.assertIn("Open Application icon pixels", backup["limits"])
        self.assertEqual([bytes.fromhex(profile["settings"]["sensor"])[3]
                          for profile in backup["profiles"]], list(range(5)))
        self.assertTrue(all(profile["errors"] == {} for profile in backup["profiles"]))
        self.assertEqual(self.world.outputs, [])
        self.assertTrue(all(report[1] == 0x1C for report in self.world.normal_sent))
        self.resetter.assert_not_called()
        self.assertEqual(len(self.world.sessions), 1)
        self.assertTrue(self.world.sessions[0].closed)
        self.assertTrue(self.world.sessions[0].access_checked)
        self.assertFalse(self.world.sessions[0].begun)
        self.assertIn("resets mouse settings", result["summary"])
        self.assertIn("Open Application icon pixels", result["summary"])

    def test_usb_permission_failure_during_prepare_prevents_install(self):
        with patch.object(UpdateTransport, 'check_update_access', side_effect=DeviceError('USB access denied')):
            with self.assertRaisesRegex(DeviceError, 'USB access denied'):
                self.prepare()
        self.assertEqual(self.world.outputs, [])
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_prepare_failure_missing_profile_read_cannot_offer_install_or_flash(self):
        self.world.read_errors["lcd"] = "Fixture LCD unavailable"
        with self.assertRaisesRegex(FirmwareError, "every supported profile"):
            self.prepare()
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])
        self.assertEqual(self.world.outputs, [])
        self.resetter.assert_not_called()

    def test_wrong_role_operation_or_unverified_archive_never_opens_transport(self):
        for changes in ({"role": "transmitter"}, {"operation": "flash_now"},
                        {"device_id": ""}, {"device_id": None},
                        {"release_key": "mouse:5.4.0.0"},
                        {"release_key": "mouse:unknown"}):
            with self.subTest(changes=changes), self.assertRaises(FirmwareError):
                self.transact(self.request(**changes))
        self.inspector.side_effect = FirmwareError("Archive hash mismatch")
        with self.assertRaisesRegex(FirmwareError, "hash mismatch"):
            self.prepare()
        self.assertEqual(self.world.sessions, [])
        self.assertEqual(self.world.outputs, [])

    def test_equal_newer_or_unknown_battery_prevents_update(self):
        for number, battery in ((509, 100), (510, 100), (600, 100), (504, 29), (504, 255)):
            self.world.status = status_response(number, battery)
            with self.subTest(number=number, battery=battery), self.assertRaises(FirmwareError):
                self.prepare()
        self.assertEqual(self.world.outputs, [])
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_cfu_platform_installed_and_target_versions_must_match_before_backup(self):
        wrong_platform = bytearray(VERSION)
        wrong_platform[5] ^= 1
        wrong_offer = bytearray(OFFER)
        wrong_offer[4:8] = version_response(number=508)[5:9]
        for version, package in (
            (bytes(wrong_platform), self.package),
            (version_response(number=604), self.package),
            (version_response(number=505), self.package),
            (VERSION, replace(self.package, offer=bytes(wrong_offer))),
        ):
            with self.subTest(version=version.hex(), offer=package.offer.hex()):
                self.world.version = version
                self.inspector.return_value = package
                with self.assertRaisesRegex(FirmwareError, "does not match"):
                    self.prepare()
                self.assertEqual(list(Path(self.directory.name).iterdir()), [])
                self.assertEqual(self.world.outputs, [])
                self.resetter.assert_not_called()

    def test_install_requires_matching_persisted_backup_before_first_offer(self):
        with self.assertRaisesRegex(FirmwareError, "Prepare and review"):
            self.transact(self.request("install"))
        prepared = self.prepare()
        for field, value in (("device_id", "other-device"), ("archive_sha256", "0" * 64),
                             ("backup_sha256", "0" * 64), ("cfu_raw", "00"),
                             ("installed_numeric", 503),
                             ("release_key", "mouse:5.4.0.0")):
            altered = dict(prepared, **{field: value})
            with self.subTest(field=field), self.assertRaises(FirmwareError):
                self.install(altered)
        self.assertEqual(self.world.outputs, [])
        self.resetter.assert_not_called()

    def test_changed_usb_identity_or_device_versions_prevent_offer(self):
        prepared = self.prepare()
        self.world.identity = "another-port"
        with self.assertRaisesRegex(FirmwareError, "changed"):
            self.install(prepared)
        self.world.identity = "fixture-usb-port"
        version = bytearray(VERSION)
        version[5] ^= 1
        self.world.version = bytes(version)
        with self.assertRaisesRegex(FirmwareError, "does not match"):
            self.install(prepared)
        self.world.version = version_response(number=505)
        self.world.status = status_response(505)
        with self.assertRaisesRegex(FirmwareError, "changed"):
            self.install(prepared)
        self.assertEqual(self.world.outputs, [])

    def test_stale_settings_and_assigned_macro_image_prevent_offer(self):
        prepared = self.prepare()
        raw = bytearray(self.world.profiles[3]["screen"])
        raw[3] = 80
        self.world.profiles[0]["screen"] = with_checksum(raw)
        with self.assertRaisesRegex(FirmwareError, "settings changed"):
            self.install(prepared)
        self.assertEqual(self.world.outputs, [])
        # Exercise the macro staleness boundary independently of ordinary data.
        before = [{"settings": {}, "macro_data": {"primary:5": "original"}} for _ in range(5)]
        after = copy.deepcopy(before)
        after[4]["macro_data"]["primary:5"] = "different"
        with self.assertRaisesRegex(FirmwareError, "Assigned macros changed"):
            firmware_hardware._check_profiles(before, after)

    def test_backup_tampering_and_missing_file_prevent_offer(self):
        prepared = self.prepare()
        path = Path(prepared["backup_path"])
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(FirmwareError, "backup changed"):
            self.install(prepared)
        path.unlink()
        with self.assertRaises(OSError):
            self.install(prepared)
        self.assertEqual(self.world.outputs, [])

    def test_install_requires_transfer_reconnect_reset_and_all_profile_readback(self):
        prepared = self.prepare()
        result = self.install(prepared)
        self.assertEqual((result["verified"], result["outcome"], result["firmware_version"]),
                         (True, "updated", "5.09"))
        self.assertEqual(result["profiles_read_back"], 5)
        self.assertEqual(result["backup_path"], prepared["backup_path"])
        self.assertEqual(result["reset"], self.reset_result)
        self.assertEqual([report[0] for report in self.world.outputs], [0x2D, 0x2A, 0x2A, 0x2A])
        self.resetter.assert_called_once()
        self.assertTrue(all(session.closed for session in self.world.sessions))
        self.assertIn("resetting", [item["phase"] for item in self.progress])

    def test_post_transfer_status_and_cfu_must_both_match_before_reset(self):
        for field in ("upgrade_status", "upgrade_cfu"):
            self.world = World()
            # Clock closures refer to self.world, so subsequent attempts remain bounded.
            setattr(self.world, field, False)
            prepared = self.prepare()
            with self.subTest(field=field), self.assertRaisesRegex(DeviceError, "restarted mouse"):
                self.install(prepared)
            self.resetter.assert_not_called()
            self.assertEqual(len(self.world.outputs), 4)

    def test_reconnect_identity_change_prevents_reset_and_never_replays_transfer(self):
        prepared = self.prepare()

        def replace_on_reconnect(index):
            if index >= 2:  # prepare0, install1, reconnect2.
                self.world.identity = "replacement-location"

        self.world.on_open = replace_on_reconnect
        with self.assertRaisesRegex(DeviceError, "different USB identity"):
            self.install(prepared)
        self.resetter.assert_not_called()
        self.assertEqual(len(self.world.outputs), 4)

    def test_final_reopen_revalidates_identity_and_versions_before_reset(self):
        prepared = self.prepare()

        def changed_final_handle(index):
            if index >= 3:
                self.world.status = status_response(504)

        self.world.on_open = changed_final_handle
        with self.assertRaisesRegex(FirmwareError, "before settings migration"):
            self.install(prepared)
        self.resetter.assert_not_called()
        self.assertEqual(len(self.world.outputs), 4)

    def test_reset_failure_is_never_retried_in_reconnect_loop(self):
        prepared = self.prepare()
        self.resetter.side_effect = DeviceError("Uncertain reset completion")
        with self.assertRaisesRegex(DeviceError, "Uncertain reset completion"):
            self.install(prepared)
        self.resetter.assert_called_once()
        self.assertEqual(len(self.world.outputs), 4)

    def test_no_reset_is_sent_when_existing_version_meets_manifest_threshold(self):
        self.world.status = status_response(506)
        self.world.version = version_response(number=506)
        prepared = self.prepare()
        self.assertFalse(prepared["reset_required"])
        result = self.install(prepared)
        self.assertTrue(result["verified"])
        self.assertIsNone(result["reset"])
        self.resetter.assert_not_called()

    def test_failure_after_offer_is_uncertain_and_does_not_attempt_reset(self):
        prepared = self.prepare()
        self.world.transfer_failure = True
        with self.assertRaises(FirmwareUpdateError) as caught:
            self.install(prepared)
        self.assertTrue(caught.exception.device_may_have_changed)
        self.assertEqual(len(self.world.outputs), 1)
        self.resetter.assert_not_called()

    def test_nonregular_and_symlink_backup_paths_fail_without_blocking(self):
        prepared = self.prepare()
        directory = Path(self.directory.name)
        link = directory / "backup-link"
        link.symlink_to(prepared["backup_path"])
        fifo = directory / "backup-pipe"
        paths = [directory, link]
        if hasattr(os, "mkfifo"):
            os.mkfifo(fifo)
            paths.append(fifo)
        for path in paths:
            altered = dict(prepared, backup_path=str(path))
            with self.subTest(path=path), self.assertRaises((OSError, FirmwareError)):
                firmware_hardware._load_backup(altered)
        self.assertEqual(self.world.outputs, [])


class BackupArtifactTests(unittest.TestCase):
    def test_same_clock_tick_creates_distinct_backups_without_overwriting(self):
        instant = datetime(2026, 9, 22, tzinfo=timezone.utc)
        writers = (
            (firmware_hardware, firmware_hardware._write_backup),
            (restore_hardware, lambda directory, value:
             restore_hardware._write_artifact(directory, value, 'mc7-before-restore-')),
        )
        for module, write in writers:
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as directory:
                with patch.object(module, 'datetime') as clock:
                    clock.now.return_value = instant
                    first, first_hash = write(directory, {'value': 1})
                    second, second_hash = write(directory, {'value': 2})
                self.assertNotEqual(first, second)
                for path, digest, value in ((first, first_hash, 1), (second, second_hash, 2)):
                    data = Path(path).read_bytes()
                    self.assertEqual(json.loads(data), {'value': value})
                    self.assertEqual(hashlib.sha256(data).hexdigest(), digest)


class FirmwareHelperJsonTests(unittest.TestCase):
    def invoke(self, request, *, error=None):
        output = io.StringIO()
        with patch("swarm2.firmware_hardware.sys.stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode()))), \
             patch("swarm2.firmware_hardware.threading.Timer"), \
             patch("swarm2.firmware_hardware.transact", side_effect=error) as transact, redirect_stdout(output):
            code = firmware_hardware.main()
        return code, json.loads(output.getvalue()), transact

    def test_transfer_failure_preserves_uncertainty_and_acknowledged_record_count(self):
        failure = FirmwareUpdateError("Fixture transfer stopped", device_may_have_changed=True,
                                       records_acknowledged=23)
        code, result, _ = self.invoke({"operation": "install"}, error=failure)
        self.assertEqual(code, 2)
        self.assertTrue(result["device_may_have_changed"])
        self.assertEqual(result["records_acknowledged"], 23)
        self.assertIn("Fixture transfer stopped", result["error"])

    def test_pre_offer_failure_can_report_no_device_change(self):
        failure = FirmwareUpdateError("Fixture preflight stopped", device_may_have_changed=False)
        code, result, _ = self.invoke({"operation": "install"}, error=failure)
        self.assertEqual(code, 2)
        self.assertFalse(result["device_may_have_changed"])

    def test_non_object_input_produces_structured_error_without_opening_device(self):
        for request in ([], "install", None):
            with self.subTest(request=request):
                code, result, transact = self.invoke(request)
                self.assertEqual(code, 2)
                self.assertIn("object", result["error"])
                self.assertFalse(result["device_may_have_changed"])
                transact.assert_not_called()


if __name__ == "__main__":
    unittest.main()
