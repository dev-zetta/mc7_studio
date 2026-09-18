"""Offline backup import, malformed-file guards and conservative restore merges."""

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from swarm2.configuration import (
    Action, Configuration, HOST_ACTION_ICON_RGBA_BYTES,
    encode_host_action_icon_rgba,
)
from swarm2.firmware_backup import BACKUP_SCHEMA, merge_supported_profile, read_backup
from swarm2.firmware_catalog import FirmwareError
from swarm2.host_actions import encode_host_action_record
from swarm2.lcd_commands import build_lcd_report, decode_lcd_response
from swarm2.macro_profiles import NORMAL_MACRO_ASSIGNMENT
from swarm2.screen_key_commands import encode_lcd_macro_record, encode_screen_key_record
from swarm2.service import DeviceService
from swarm2.settings import _plan
from tests.test_firmware_commands import VERSION
from tests.test_firmware_hardware import World
from tests.test_macro_profiles import native_payload
from tests.test_settings import with_checksum


APPLICATION = "/opt/Example Suite/Example.AppImage"
APPLICATION_ICON = encode_host_action_icon_rgba(
    bytes((1, 2, 3, 255)) * (HOST_ACTION_ICON_RGBA_BYTES // 4))


def fixture_backup():
    world = World()
    profiles = []
    for records in world.profiles:
        settings = {name: raw.hex() for name, raw in records.items()}
        # This is the original v1 shape, written before command 0x29 support.
        settings.pop("screen_keys", None)
        profiles.append({"raw": settings["sensor"], "settings": settings,
                         "errors": {}, "macro_data": {}, "changed": False})
    return {"schema": BACKUP_SCHEMA, "device_id": "fixture-port", "transport_identity": "fixture-usb-port",
            "archive_sha256": "a" * 64, "status_raw": world.status.hex(), "cfu_raw": VERSION.hex(),
            "profiles": profiles, "read_at": "2026-09-16T09:14:17+00:00", "limits": "Readable settings only"}


def edit_record(bundle, name, offset, value):
    raw = bytearray.fromhex(bundle["settings"][name])
    if isinstance(value, bytes):
        raw[offset:offset + len(value)] = value
    else:
        raw[offset] = value
    bundle["settings"][name] = with_checksum(raw).hex()
    if name == "sensor":
        bundle["raw"] = bundle["settings"][name]


def screen_key_record(profile, pages=None):
    pages = pages or [[bytes(11) for _ in range(4)] for _ in range(3)]
    responses = []
    for page_index, records in enumerate(pages):
        response = bytearray((0x10, 0x29, 0, 0x29, 0x32, 0, profile, page_index + 1))
        response.append(0)
        response.extend(b"".join(reversed(records)))
        response.append((-sum(response[2:])) & 0xFF)
        responses.append(response)
    return bytes().join(responses)


def set_lcd_page(bundle, profile, page_index, page):
    state = decode_lcd_response(bytes.fromhex(bundle["settings"]["lcd"]), profile)
    response = bytearray(build_lcd_report(state, pages={page_index: page})[:61])
    response[2] = 0
    response[60] = (-sum(response[2:60])) & 0xFF
    bundle["settings"]["lcd"] = response.hex()


def add_screen_keys(value, profile=0, pages=None):
    for index, bundle in enumerate(value["profiles"]):
        bundle["settings"]["screen_keys"] = screen_key_record(
            index, pages if index == profile else None).hex()


class FirmwareBackupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "backup.json"
        self.value = fixture_backup()

    def write(self, value=None):
        self.path.write_text(json.dumps(self.value if value is None else value))
        return self.path

    def read(self):
        return read_backup(self.write())

    def current(self, index=0, bundle=None, draft=None):
        return DeviceService._snapshot("fixture-port", index + 1,
            copy.deepcopy(bundle or fixture_backup()["profiles"][index]), draft)

    def test_complete_backup_decodes_five_indexed_portable_drafts(self):
        backup = self.read()
        self.assertEqual(backup.sha256, hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(backup.firmware_version, "5.04")
        self.assertEqual((backup.device_id, backup.transport_identity), ("fixture-port", "fixture-usb-port"))
        self.assertEqual([profile.profile_slot for profile in backup.profiles], [1, 2, 3, 4, 5])
        self.assertTrue(backup.warnings)
        for profile in backup.profiles:
            profile.configuration.validate()
            self.assertEqual(profile.configuration.profile_slot, profile.profile_slot)
            self.assertIn("sensor.stages", profile.verified_fields)
        self.assertEqual(read_backup(self.path, backup.sha256).sha256, backup.sha256)

    def test_legacy_backup_without_screen_keys_restores_static_lcd_pages(self):
        page = ["dpi", "led_brightness", "play_pause", "next_track"]
        set_lcd_page(self.value["profiles"][0], 0, 0, page)
        profile = self.read().profiles[0]
        self.assertNotIn("screen_keys", profile.snapshot["baseline"]["settings"])
        plan = merge_supported_profile(profile, self.current())
        self.assertEqual(plan.configuration.display.pages[0], page)
        self.assertEqual(plan.configuration.display.key_bindings, [])

    def test_screen_key_records_are_bounded_and_decoded_with_their_lcd_page(self):
        page = ["remap_key", "hotkey", "play_pause", "next_track"]
        records = [[encode_screen_key_record("F5", "remap_key"),
                    encode_screen_key_record("Ctrl+S", "hotkey"), bytes(11), bytes(11)],
                   *[[bytes(11) for _ in range(4)] for _ in range(2)]]
        set_lcd_page(self.value["profiles"][0], 0, 0, page)
        add_screen_keys(self.value, pages=records)
        profile = self.read().profiles[0]
        self.assertIn("display.key_bindings", profile.verified_fields)
        self.assertEqual(profile.configuration.display.key_bindings[0],
                         ["F5", "Ctrl+S", None, None])

        valid = self.value["profiles"][0]["settings"]["screen_keys"]
        for raw in ("00" * 163, valid[:-2]):
            changed = copy.deepcopy(self.value)
            changed["profiles"][0]["settings"]["screen_keys"] = raw
            with self.subTest(length=len(raw) // 2), self.assertRaises(FirmwareError):
                read_backup(self.write(changed))

    def test_supported_lcd_key_page_and_bindings_restore_together(self):
        page = ["remap_key", "hotkey", "play_pause", "next_track"]
        source_records = [[encode_screen_key_record("F5", "remap_key"),
                           encode_screen_key_record("Ctrl+S", "hotkey"), bytes(11), bytes(11)],
                          *[[bytes(11) for _ in range(4)] for _ in range(2)]]
        set_lcd_page(self.value["profiles"][0], 0, 0, page)
        add_screen_keys(self.value, pages=source_records)
        profile = self.read().profiles[0]

        target = fixture_backup()["profiles"][0]
        set_lcd_page(target, 0, 0, page)
        target_records = [[encode_screen_key_record("F6", "remap_key"),
                           encode_screen_key_record("Ctrl+C", "hotkey"), bytes(11), bytes(11)],
                          *[[bytes(11) for _ in range(4)] for _ in range(2)]]
        target["settings"]["screen_keys"] = screen_key_record(0, target_records).hex()
        plan = merge_supported_profile(profile, self.current(bundle=target))
        self.assertIn("display", plan.sections)
        self.assertEqual(plan.configuration.display.pages[0], page)
        self.assertEqual(plan.configuration.display.key_bindings[0],
                         ["F5", "Ctrl+S", None, None])

    def test_missing_or_opaque_screen_keys_preserve_the_current_page(self):
        source_page = ["remap_key", "hotkey", "play_pause", "next_track"]
        target_page = ["hotkey", "remap_key", "next_track", "play_pause"]
        target_records = [[encode_screen_key_record("Ctrl+C", "hotkey"),
                           encode_screen_key_record("F6", "remap_key"), bytes(11), bytes(11)],
                          *[[bytes(11) for _ in range(4)] for _ in range(2)]]
        target = fixture_backup()["profiles"][0]
        set_lcd_page(target, 0, 0, target_page)
        target["settings"]["screen_keys"] = screen_key_record(0, target_records).hex()
        current = self.current(bundle=target)

        set_lcd_page(self.value["profiles"][0], 0, 0, source_page)
        legacy_profile = self.read().profiles[0]
        legacy_plan = merge_supported_profile(legacy_profile, current)
        self.assertEqual(legacy_plan.configuration.display.pages[0], target_page)
        self.assertEqual(legacy_plan.configuration.display.key_bindings[0],
                         ["Ctrl+C", "F6", None, None])
        self.assertTrue(any("complete source and current" in warning
                            for warning in legacy_plan.warnings))

        opaque = bytes.fromhex("0102030405060708090a0b")
        source_records = [[opaque, encode_screen_key_record("Ctrl+S", "hotkey"),
                           bytes(11), bytes(11)],
                          *[[bytes(11) for _ in range(4)] for _ in range(2)]]
        add_screen_keys(self.value, pages=source_records)
        opaque_profile = self.read().profiles[0]
        opaque_plan = merge_supported_profile(opaque_profile, current)
        self.assertEqual(opaque_plan.configuration.display.pages[0], target_page)
        self.assertEqual(opaque_plan.configuration.display.key_bindings[0],
                         ["Ctrl+C", "F6", None, None])
        self.assertTrue(any("opaque screen-key binding" in warning
                            for warning in opaque_plan.warnings))

        source_records[0][0] = encode_screen_key_record("F5", "remap_key")
        self.value = fixture_backup()
        set_lcd_page(self.value["profiles"][0], 0, 0, source_page)
        add_screen_keys(self.value, pages=source_records)
        target_records[0][0] = opaque
        target["settings"]["screen_keys"] = screen_key_record(0, target_records).hex()
        current_opaque = self.current(bundle=target)
        current_plan = merge_supported_profile(self.read().profiles[0], current_opaque)
        self.assertEqual(current_plan.configuration.display.pages[0], target_page)
        self.assertEqual(current_plan.configuration.display.key_bindings[0][0],
                         "device:" + opaque.hex())

    def test_missing_current_screen_keys_retain_its_key_page(self):
        source_page = ["remap_key", "hotkey", "play_pause", "next_track"]
        source_records = [[encode_screen_key_record("F5", "remap_key"),
                           encode_screen_key_record("Ctrl+S", "hotkey"), bytes(11), bytes(11)],
                          *[[bytes(11) for _ in range(4)] for _ in range(2)]]
        set_lcd_page(self.value["profiles"][0], 0, 0, source_page)
        add_screen_keys(self.value, pages=source_records)

        target = fixture_backup()["profiles"][0]
        target_page = ["hotkey", "remap_key", "next_track", "play_pause"]
        set_lcd_page(target, 0, 0, target_page)
        current = self.current(bundle=target)
        self.assertNotIn("screen_keys", current["baseline"]["settings"])

        plan = merge_supported_profile(self.read().profiles[0], current)
        self.assertEqual(plan.configuration.display.pages[0], target_page)
        self.assertEqual(plan.configuration.display.key_bindings[0], [None] * 4)
        self.assertTrue(any("complete source and current" in warning
                            for warning in plan.warnings))

    def test_host_launch_backup_warns_and_does_not_create_an_unbound_tile(self):
        source_page = ["open_website", "dpi", "led_brightness", "play_pause"]
        set_lcd_page(self.value["profiles"][0], 0, 0, source_page)
        source_records = [[
            encode_host_action_record("open_website", "https://example.com"),
            bytes(11), bytes(11), bytes(11),
        ], *[[bytes(11) for _ in range(4)] for _ in range(2)]]
        add_screen_keys(self.value, pages=source_records)
        profile = self.read().profiles[0]

        self.assertTrue(all(target is None
                            for row in profile.configuration.display.host_action_bindings
                            for target in row))
        self.assertTrue(any("launch targets are host-only" in warning
                            for warning in profile.warnings))

        target = fixture_backup()["profiles"][0]
        target_page = ["next_track", "led_brightness", "play_pause", "dpi"]
        set_lcd_page(target, 0, 0, target_page)
        target["settings"]["screen_keys"] = screen_key_record(0).hex()
        current = self.current(bundle=target)
        plan = merge_supported_profile(profile, current)

        self.assertEqual(plan.configuration.display.pages[0], target_page)
        self.assertEqual(plan.configuration.display.host_action_bindings, [])
        self.assertTrue(any("host-only launch targets" in warning
                            for warning in plan.warnings))
        raw = {name: bytes.fromhex(value)
               for name, value in current["baseline"]["settings"].items()}
        _plan("display", plan.configuration, raw, 0)

    def test_restore_preserves_current_launch_page_target_and_trigger(self):
        target = fixture_backup()["profiles"][0]
        target_page = ["open_folder", "dpi", "led_brightness", "play_pause"]
        target_value = "/home/example/Documents"
        set_lcd_page(target, 0, 0, target_page)
        records = [[
            encode_host_action_record("open_folder", target_value),
            bytes(11), bytes(11), bytes(11),
        ], *[[bytes(11) for _ in range(4)] for _ in range(2)]]
        target["settings"]["screen_keys"] = screen_key_record(0, records).hex()
        current = self.current(bundle=target)
        current["configuration"].display.host_action_bindings = [
            [target_value, None, None, None],
            [None] * 4,
            [None] * 4,
        ]
        current["configuration"].validate()

        plan = merge_supported_profile(self.read().profiles[0], current)

        self.assertEqual(plan.configuration.display.pages[0], target_page)
        self.assertEqual(
            plan.configuration.display.host_action_bindings[0][0], target_value)
        self.assertTrue(any("host-only launch targets" in warning
                            for warning in plan.warnings))
        raw = {name: bytes.fromhex(value)
               for name, value in current["baseline"]["settings"].items()}
        commands, _ = _plan("display", plan.configuration, raw, 0)
        self.assertFalse(any(command[1] in (0x25, 0x29) for command in commands))

    def test_application_backup_is_unresolved_and_warns_that_icons_are_unreadable(self):
        source_page = ["open_application", "dpi", "led_brightness", "play_pause"]
        set_lcd_page(self.value["profiles"][0], 0, 0, source_page)
        source_records = [[
            encode_host_action_record(
                "open_application", APPLICATION, icon_index=3),
            bytes(11), bytes(11), bytes(11),
        ], *[[bytes(11) for _ in range(4)] for _ in range(2)]]
        add_screen_keys(self.value, pages=source_records)

        profile = self.read().profiles[0]

        self.assertTrue(all(
            target is None
            for row in profile.configuration.display.host_action_bindings
            for target in row))
        self.assertTrue(all(
            icon is None
            for row in profile.configuration.display.host_action_icon_bindings
            for icon in row))
        self.assertTrue(any(
            "Open Application icon pixels cannot be read back" in warning
            for warning in profile.warnings))

    def test_restore_merge_preserves_current_application_target_icon_and_trigger(self):
        target = fixture_backup()["profiles"][0]
        target_page = ["open_application", "dpi", "led_brightness", "play_pause"]
        set_lcd_page(target, 0, 0, target_page)
        records = [[
            encode_host_action_record(
                "open_application", APPLICATION, icon_index=3),
            bytes(11), bytes(11), bytes(11),
        ], *[[bytes(11) for _ in range(4)] for _ in range(2)]]
        target["settings"]["screen_keys"] = screen_key_record(0, records).hex()

        draft = self.current(bundle=target)["configuration"]
        draft.display.host_action_bindings[0][0] = APPLICATION
        draft.display.host_action_icon_bindings[0][0] = APPLICATION_ICON
        draft.validate()
        current = self.current(bundle=target, draft=draft)

        set_lcd_page(self.value["profiles"][0], 0, 0, target_page)
        add_screen_keys(self.value, pages=records)
        plan = merge_supported_profile(self.read().profiles[0], current)

        self.assertEqual(plan.configuration.display.pages[0], target_page)
        self.assertEqual(
            plan.configuration.display.host_action_bindings[0][0], APPLICATION)
        self.assertEqual(
            plan.configuration.display.host_action_icon_bindings[0][0],
            APPLICATION_ICON)
        serialized = Configuration.from_dict(plan.configuration.to_dict())
        self.assertEqual(
            serialized.display.host_action_icon_bindings[0][0], APPLICATION_ICON)
        self.assertTrue(any(
            "Open Application icons are preserved" in warning
            for warning in plan.warnings))

        raw = {name: bytes.fromhex(value)
               for name, value in current["baseline"]["settings"].items()}
        icon_indices = [[None] * 4 for _ in plan.configuration.display.pages]
        icon_indices[0][0] = 3
        commands, _ = _plan(
            "display", plan.configuration, raw, 0,
            host_action_icon_indices=icon_indices)
        self.assertFalse(any(
            command[1] in (0x25, 0x29) for command in commands))

    def test_updater_release_key_is_optional_and_preserved(self):
        legacy = self.read()
        self.assertNotIn("release_key", legacy.value)

        self.value["release_key"] = "mouse:5.9.0.0"
        current = self.read()
        self.assertEqual(current.value["release_key"], "mouse:5.9.0.0")

    def test_invalid_release_key_and_other_unknown_fields_are_rejected(self):
        for release_key in (None, "", "mouse:5.9", "transmitter:5.9.0.0",
                            "mouse:5.9.0.0\n"):
            self.value = fixture_backup()
            self.value["release_key"] = release_key
            with self.subTest(release_key=release_key), self.assertRaises(FirmwareError):
                self.read()

        self.value = fixture_backup()
        self.value["release_key"] = "mouse:5.9.0.0"
        self.value["unexpected"] = 1
        with self.assertRaises(FirmwareError):
            self.read()

    def test_digest_mismatch_rejects_changed_reviewed_file(self):
        self.write()
        with self.assertRaisesRegex(FirmwareError, "changed"):
            read_backup(self.path, "0" * 64)
        for digest in (False, "xx", "A" * 64):
            with self.subTest(digest=digest), self.assertRaises(FirmwareError):
                read_backup(self.path, digest)

    def test_duplicate_keys_constants_bad_utf8_deep_json_and_oversize_rejected(self):
        for raw in (b'{"schema":1,"schema":2}', b'{"x":NaN}', b'\xff', b'[' * 1500,
                    b' ' * (4 * 1024 * 1024 + 1)):
            self.path.write_bytes(raw)
            with self.subTest(length=len(raw)), self.assertRaises(FirmwareError):
                read_backup(self.path)

    def test_symlink_fifo_directory_not_read_or_followed(self):
        self.write()
        link = self.path.with_name("link")
        link.symlink_to(self.path)
        fifo = self.path.with_name("fifo")
        paths = [link, self.path.parent]
        if hasattr(os, "mkfifo"):
            os.mkfifo(fifo)
            paths.append(fifo)
        for path in paths:
            with self.subTest(path=path), self.assertRaises((OSError, FirmwareError)):
                read_backup(path)

    def test_schema_identity_timestamp_and_five_profile_shape_checked(self):
        for key, value in (("schema", "other"), ("device_id", "bad\nidentity"),
                           ("transport_identity", ""), ("archive_sha256", "bad"),
                           ("read_at", "2026-09-16"), ("profiles", [])):
            changed = copy.deepcopy(self.value)
            changed[key] = value
            with self.subTest(key=key), self.assertRaises(FirmwareError):
                read_backup(self.write(changed))
        changed = copy.deepcopy(self.value)
        changed["unexpected"] = 1
        with self.assertRaises(FirmwareError):
            read_backup(self.write(changed))

    def test_reordered_profiles_and_sensor_duplicate_disagreement_rejected(self):
        self.value["profiles"][0], self.value["profiles"][1] = self.value["profiles"][1], self.value["profiles"][0]
        with self.assertRaisesRegex(FirmwareError, "profile does not match"):
            self.read()
        self.value = fixture_backup()
        self.value["profiles"][2]["raw"] = self.value["profiles"][0]["raw"]
        with self.assertRaisesRegex(FirmwareError, "disagrees"):
            self.read()

    def test_missing_unknown_truncated_or_error_records_rejected(self):
        changes = [lambda b: b["settings"].pop("screen"),
                   lambda b: b["settings"].update(unknown="00"),
                   lambda b: b["settings"].update(screen="102b00"),
                   lambda b: b["errors"].update(screen="read failed"),
                   lambda b: b.update(changed=0)]
        for change in changes:
            self.value = fixture_backup()
            change(self.value["profiles"][0])
            with self.subTest(change=change), self.assertRaises(FirmwareError):
                self.read()

    def test_version_and_cfu_platform_must_agree(self):
        for offset in (5, 6, 7, 8, 9):
            raw = bytearray(VERSION)
            raw[offset] ^= 1
            self.value["cfu_raw"] = raw.hex()
            with self.subTest(offset=offset), self.assertRaises(FirmwareError):
                self.read()

    def test_global_changes_rejected_but_dynamic_battery_allowed(self):
        edit_record(self.value["profiles"][3], "screen", 3, 80)
        with self.assertRaisesRegex(FirmwareError, "Global settings changed"):
            self.read()
        self.value = fixture_backup()
        edit_record(self.value["profiles"][3], "status", 9, 50)
        self.read()

    def test_unassigned_missing_extra_and_truncated_macro_images_rejected(self):
        bundle = self.value["profiles"][0]
        bundle["macro_data"]["primary:5"] = native_payload().hex()
        with self.assertRaisesRegex(FirmwareError, "assigned macros"):
            self.read()
        edit_record(bundle, "primary", 4 + 5 * 4, NORMAL_MACRO_ASSIGNMENT)
        self.read()
        bundle["macro_data"]["primary:5"] = "00"
        with self.assertRaisesRegex(FirmwareError, "seventeen"):
            self.read()
        bundle["macro_data"] = {}
        with self.assertRaisesRegex(FirmwareError, "assigned macros"):
            self.read()

    def test_unknown_macro_format_is_opaque_and_never_selected_for_restore(self):
        bundle = self.value["profiles"][0]
        edit_record(bundle, "primary", 4 + 5 * 4, NORMAL_MACRO_ASSIGNMENT)
        unknown = bytearray(native_payload())
        unknown[72] = 2
        bundle["macro_data"]["primary:5"] = unknown.hex()
        profile = self.read().profiles[0]
        self.assertIn("macro/primary:5", profile.errors)
        self.assertTrue(any("opaque" in warning for warning in profile.warnings))
        plan = merge_supported_profile(profile, self.current())
        self.assertEqual(plan.configuration.buttons[5].primary, self.current()["configuration"].buttons[5].primary)
        self.assertEqual(plan.configuration.macros, [])

    def test_supported_macro_restored_as_typed_events_with_unique_id(self):
        bundle = self.value["profiles"][0]
        edit_record(bundle, "primary", 4 + 5 * 4, NORMAL_MACRO_ASSIGNMENT)
        bundle["macro_data"]["primary:5"] = native_payload().hex()
        profile = self.read().profiles[0]
        current = self.current()
        current["configuration"].macros = copy.deepcopy(profile.configuration.macros)
        plan = merge_supported_profile(profile, current)
        self.assertIn("buttons", plan.sections)
        self.assertEqual(len(plan.configuration.macros), 2)
        self.assertNotEqual(plan.configuration.macros[0].id, plan.configuration.macros[1].id)
        binding = next(binding for binding in plan.configuration.buttons if binding.primary.kind == "macro")
        self.assertEqual(binding.primary.value, plan.configuration.macros[-1].id)
        self.assertEqual(plan.configuration.macros[-1].events, profile.configuration.macros[0].events)

    def test_supported_lcd_macro_backup_import_and_restore(self):
        source_page = ["macro", "copy", "paste", "undo"]
        set_lcd_page(self.value["profiles"][0], 0, 2, source_page)
        records = [[bytes(11) for _ in range(4)] for _ in range(3)]
        records[2][0] = encode_lcd_macro_record("Native macro")
        add_screen_keys(self.value, pages=records)
        self.value["profiles"][0]["macro_data"]["lcd:8"] = native_payload().hex()

        profile = self.read().profiles[0]

        self.assertIn("display.macro_bindings", profile.verified_fields)
        self.assertTrue(any("no known per-slot erase" in warning
                            for warning in profile.warnings))
        source_id = profile.configuration.display.macro_bindings[2][0]
        self.assertIsNotNone(source_id)
        self.assertEqual(profile.configuration.macros[0].name,
                         "Native macro 0123456789abcdefgh")

        target = fixture_backup()["profiles"][0]
        target["settings"]["screen_keys"] = screen_key_record(0).hex()
        current = self.current(bundle=target)
        plan = merge_supported_profile(profile, current)

        self.assertIn("display", plan.sections)
        self.assertEqual(plan.configuration.display.pages[2], source_page)
        restored_id = plan.configuration.display.macro_bindings[2][0]
        self.assertIsNotNone(restored_id)
        self.assertIn(restored_id, {macro.id for macro in plan.configuration.macros})
        self.assertEqual(
            next(macro for macro in plan.configuration.macros if macro.id == restored_id).events,
            profile.configuration.macros[0].events)
        plan.configuration.validate()

    def test_opaque_current_lcd_macro_page_is_preserved(self):
        current_page = ["macro", "copy", "paste", "undo"]
        target = fixture_backup()["profiles"][0]
        set_lcd_page(target, 0, 2, current_page)
        current_records = [[bytes(11) for _ in range(4)] for _ in range(3)]
        current_records[2][0] = bytes.fromhex("0001010700") + b"Held\0\0"
        target["settings"]["screen_keys"] = screen_key_record(0, current_records).hex()
        target["macro_data"]["lcd:8"] = (b"\x7f" * 1037).hex()
        current = self.current(bundle=target)
        self.assertNotIn("display.macro_bindings", current["verified_fields"])
        self.assertIsNone(current["configuration"].display.macro_bindings[2][0])

        source_page = ["macro", "next_track", "paste", "undo"]
        set_lcd_page(self.value["profiles"][0], 0, 2, source_page)
        source_records = [[bytes(11) for _ in range(4)] for _ in range(3)]
        source_records[2][0] = encode_lcd_macro_record("Native macro")
        add_screen_keys(self.value, pages=source_records)
        self.value["profiles"][0]["macro_data"]["lcd:8"] = native_payload().hex()

        plan = merge_supported_profile(self.read().profiles[0], current)

        self.assertEqual(plan.configuration.display.pages[2], current_page)
        self.assertIsNone(plan.configuration.display.macro_bindings[2][0])
        self.assertTrue(any("opaque current macro tile" in warning
                            for warning in plan.warnings))
        plan.configuration.validate()

    def test_merge_copies_supported_fields_preserves_lod_background_and_local_display_fields(self):
        edit_record(self.value["profiles"][0], "sensor", 11, b"\x0b\0")
        profile = self.read().profiles[0]
        current_bundle = fixture_backup()["profiles"][0]
        edit_record(current_bundle, "sensor", 45, 0x49)
        edit_record(current_bundle, "background", 3, 1)
        current = self.current(bundle=current_bundle)
        current["configuration"].display.timeout_seconds = 777
        current["configuration"].power.led_timeout_value = 17
        before = copy.deepcopy(current)
        plan = merge_supported_profile(profile, current)
        self.assertEqual(plan.sections, ("sensor", "lighting", "buttons", "display", "power"))
        self.assertEqual(plan.configuration.sensor.stages[0].value, 600)
        self.assertEqual(plan.configuration.sensor.lift_off_distance, "custom")
        self.assertEqual(plan.configuration.display.background_index, 1)
        self.assertEqual(plan.configuration.display.timeout_seconds, 777)
        self.assertEqual(plan.configuration.power.led_timeout_value, 15)
        self.assertEqual(current, before)

    def test_opaque_current_or_backup_actions_preserved(self):
        edit_record(self.value["profiles"][0], "primary", 4 + 5 * 4, bytes.fromhex("01020304"))
        profile = self.read().profiles[0]
        current_bundle = fixture_backup()["profiles"][0]
        edit_record(current_bundle, "primary", 4 + 6 * 4, bytes.fromhex("01030405"))
        current = self.current(bundle=current_bundle)
        plan = merge_supported_profile(profile, current)
        for button in (6, 7):
            self.assertEqual(plan.configuration.buttons[button - 1].primary, current["configuration"].buttons[button - 1].primary)

    def test_unknown_lighting_source_or_target_skips_whole_section(self):
        for target in (False, True):
            self.value = fixture_backup()
            current_bundle = fixture_backup()["profiles"][0]
            bundle = current_bundle if target else self.value["profiles"][0]
            edit_record(bundle, "lighting", 6, 127)
            current = self.current(bundle=current_bundle)
            plan = merge_supported_profile(self.read().profiles[0], current)
            with self.subTest(target=target):
                self.assertNotIn("lighting", plan.sections)
                self.assertEqual(plan.configuration.lighting, current["configuration"].lighting)

    def test_asymmetric_paired_settings_are_preserved(self):
        current_bundle = fixture_backup()["profiles"][0]
        edit_record(current_bundle, "sensor", 4, 1)
        edit_record(current_bundle, "debounce", 4, 3)
        current = self.current(bundle=current_bundle)
        plan = merge_supported_profile(self.read().profiles[0], current)
        self.assertEqual(plan.configuration.sensor.polling_rate, 2000)
        self.assertEqual(plan.configuration.sensor.debounce_ms, current["configuration"].sensor.debounce_ms)
        self.assertTrue(any("USB/wireless" in warning for warning in plan.warnings))
        self.assertTrue(any("paired debounce" in warning for warning in plan.warnings))

    def test_unknown_lcd_pages_preserved_and_disabled_pages_not_enabled(self):
        current_bundle = fixture_backup()["profiles"][0]
        # Widget record starts at byte8; unknown id0xEE decodes to an opaque key.
        edit_record(current_bundle, "lcd", 8, 0xEE)
        current = self.current(bundle=current_bundle)
        plan = merge_supported_profile(self.read().profiles[0], current)
        self.assertEqual(plan.configuration.display.pages[0], current["configuration"].display.pages[0])
        self.assertTrue(any("unknown widgets" in warning for warning in plan.warnings))
        edit_record(current_bundle, "lcd", 4, 1)
        current = self.current(bundle=current_bundle)
        plan = merge_supported_profile(self.read().profiles[0], current)
        self.assertEqual(len(plan.configuration.display.pages), 1)
        self.assertTrue(any("not currently enabled" in warning for warning in plan.warnings))

    def test_readable_but_unsupported_screen_values_preserve_current_pair(self):
        for offset, value in ((3, 77), (4, 13)):
            self.value = fixture_backup()
            for bundle in self.value["profiles"]:
                edit_record(bundle, "screen", offset, value)
            current = self.current()
            plan = merge_supported_profile(self.read().profiles[0], current)
            with self.subTest(offset=offset):
                self.assertEqual(plan.configuration.display.brightness, current["configuration"].display.brightness)
                self.assertEqual(plan.configuration.display.timeout_value, current["configuration"].display.timeout_value)
                self.assertTrue(any("outside supported write" in warning for warning in plan.warnings))

    def test_wrong_target_slot_rejected(self):
        with self.assertRaisesRegex(FirmwareError, "matching target"):
            merge_supported_profile(self.read().profiles[0], self.current(1))


if __name__ == "__main__":
    unittest.main()
