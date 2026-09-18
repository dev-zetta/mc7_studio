import copy
import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zlib

from swarm2.configuration import (
    Action, Configuration, ConfigurationError, DEFAULT_PROFILE_COLOR, Macro,
    MacroEvent, MAX_PRESET_BYTES, PROFILE_IMAGE_PREFIX, PresetStore,
    configuration_directory, profile_image_bytes, read_preset,
)


ONE_PIXEL_PNG = (
    PROFILE_IMAGE_PREFIX
    + "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class ConfigurationTests(unittest.TestCase):
    def test_defaults_are_independent_local_drafts(self):
        first, second = Configuration(), Configuration()
        first.sensor.stages[0].value = 900
        first.buttons[0].primary.value = "right"
        self.assertEqual(second.sensor.stages[0].value, 400)
        self.assertEqual(second.buttons[0].primary.value, "left")
        self.assertEqual(second.to_dict()["source"], "local_draft")
        self.assertEqual(Configuration.from_dict(second.to_dict()), second)

    def test_roundtrip_every_configuration_area_and_macro_reference(self):
        configuration = Configuration(name="Work / design – Linux", profile_slot=5)
        configuration.appearance.color = "#13579B"
        configuration.appearance.image = ONE_PIXEL_PNG
        configuration.sensor.stages[-1].value = 30000
        configuration.sensor.current_stage = 4
        configuration.sensor.stages[0].enabled = False
        configuration.sensor.stages[1].color = "#Ab12ef"
        configuration.sensor.polling_rate = 8000
        configuration.sensor.motion_sync = True
        configuration.sensor.angle_snapping = True
        configuration.sensor.angle_tuning = -30
        configuration.sensor.angle_tuning_enabled = True
        configuration.sensor.dpi_indicator_enabled = False
        configuration.sensor.lift_off_distance = "very_low"
        configuration.lighting.effect = "wave"
        configuration.lighting.brightness = 43
        configuration.lighting.speed = 72
        configuration.display.widgets = ["cpu_temperature", "dpi"]
        configuration.display.haptic_intensity = "high"
        configuration.display.timeout_seconds = 3600
        configuration.power.standby_value = 30
        configuration.power.led_timeout_value = 0
        configuration.power.eco_mode = True
        configuration.power.energy_saving = True
        macro = Macro(id="save-shortcut", name="Save", playback="repeat", repeat=2, events=[
            MacroEvent("key_down", "Ctrl", 0), MacroEvent("key_down", "S", 25),
            MacroEvent("key_up", "S", 10), MacroEvent("key_up", "Ctrl", 0),
            MacroEvent("delay", "", 250),
        ])
        configuration.macros = [macro]
        configuration.buttons[0].easy_shift = Action("macro", macro.id)
        configuration.buttons[1].easy_shift = Action("keyboard", "Ctrl+Shift+S")
        encoded = json.loads(json.dumps(configuration.to_dict()))
        self.assertEqual(Configuration.from_dict(encoded), configuration)

    def test_profile_appearance_is_portable_and_strictly_validated(self):
        configuration = Configuration()
        configuration.appearance.color = "#Ab12ef"
        configuration.appearance.image = ONE_PIXEL_PNG
        restored = Configuration.from_dict(configuration.to_dict())
        self.assertEqual(restored.appearance, configuration.appearance)
        self.assertTrue(profile_image_bytes(restored.appearance.image).startswith(b"\x89PNG"))

        valid = configuration.to_dict()
        invalid = [
            None,
            {"color": "purple", "image": ONE_PIXEL_PNG},
            {"color": "#123456", "image": "relative/image.png"},
            {"color": "#123456", "image": PROFILE_IMAGE_PREFIX + "%%%"},
            {"color": "#123456", "image": PROFILE_IMAGE_PREFIX + base64.b64encode(b"not png").decode()},
            {"color": "#123456", "image": None, "path": "/tmp/image.png"},
        ]
        for appearance in invalid:
            data = copy.deepcopy(valid)
            data["appearance"] = appearance
            with self.subTest(appearance=appearance), self.assertRaises(ConfigurationError):
                Configuration.from_dict(data)

        damaged = bytearray(profile_image_bytes(ONE_PIXEL_PNG))
        damaged[-1] ^= 1
        data = copy.deepcopy(valid)
        data["appearance"]["image"] = (
            PROFILE_IMAGE_PREFIX + base64.b64encode(damaged).decode("ascii"))
        with self.assertRaisesRegex(ConfigurationError, "damaged|ending"):
            Configuration.from_dict(data)

        def png_chunk(kind, content):
            checksum = zlib.crc32(content, zlib.crc32(kind)) & 0xFFFFFFFF
            return len(content).to_bytes(4, "big") + kind + content + checksum.to_bytes(4, "big")

        header = bytes.fromhex("00000001000000010806000000")
        malformed = (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", header)
                     + png_chunk(b"IDAT", b"not a zlib stream")
                     + png_chunk(b"IEND", b""))
        data = copy.deepcopy(valid)
        data["appearance"]["image"] = (
            PROFILE_IMAGE_PREFIX + base64.b64encode(malformed).decode("ascii")
        )
        with self.assertRaisesRegex(ConfigurationError, "compressed PNG data"):
            Configuration.from_dict(data)

        indexed_header = bytes.fromhex("00000001000000010103000000")
        indexed_pixels = zlib.compress(b"\x00\x00")
        missing_palette = (b"\x89PNG\r\n\x1a\n"
                           + png_chunk(b"IHDR", indexed_header)
                           + png_chunk(b"IDAT", indexed_pixels)
                           + png_chunk(b"IEND", b""))
        data["appearance"]["image"] = (
            PROFILE_IMAGE_PREFIX + base64.b64encode(missing_palette).decode("ascii")
        )
        with self.assertRaisesRegex(ConfigurationError, "pixel-data ordering"):
            Configuration.from_dict(data)

        indexed = (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", indexed_header)
                   + png_chunk(b"PLTE", b"\xff\x00\x00")
                   + png_chunk(b"IDAT", indexed_pixels)
                   + png_chunk(b"IEND", b""))
        data["appearance"]["image"] = (
            PROFILE_IMAGE_PREFIX + base64.b64encode(indexed).decode("ascii")
        )
        self.assertEqual(
            profile_image_bytes(Configuration.from_dict(data).appearance.image),
            indexed,
        )

        split = len(indexed_pixels) // 2
        separated_idat = (b"\x89PNG\r\n\x1a\n"
                          + png_chunk(b"IHDR", indexed_header)
                          + png_chunk(b"PLTE", b"\xff\x00\x00")
                          + png_chunk(b"IDAT", indexed_pixels[:split])
                          + png_chunk(b"tEXt", b"key\x00value")
                          + png_chunk(b"IDAT", indexed_pixels[split:])
                          + png_chunk(b"IEND", b""))
        data["appearance"]["image"] = (
            PROFILE_IMAGE_PREFIX + base64.b64encode(separated_idat).decode("ascii")
        )
        with self.assertRaisesRegex(ConfigurationError, "pixel-data ordering"):
            Configuration.from_dict(data)

    def test_schema_versions_types_unknown_fields_and_hardware_claim_rejected(self):
        valid = Configuration().to_dict()
        cases = [None, [], {**valid, "version": 2}, {**valid, "version": True},
                 {**valid, "schema": "pickle"}, {**valid, "source": "device_read"},
                 {**valid, "execute": "example"}, {**valid, "name": ""},
                 {**valid, "name": "name\u0000"}, {**valid, "profile_slot": True},
                 {**valid, "profile_slot": 6}, {**valid, "buttons": valid["buttons"][:-1]}]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ConfigurationError):
                Configuration.from_dict(data)

    def test_dpi_physical_bounds_increment_enable_and_scalar_types(self):
        for bad_value in (0, 49, 51, 30050, True, 800.0, "800"):
            configuration = Configuration()
            configuration.sensor.stages[0].value = bad_value
            with self.subTest(value=bad_value), self.assertRaises(ConfigurationError):
                configuration.to_dict()
        for bad_value in (False, 60, 8000.0, "1000"):
            configuration = Configuration()
            configuration.sensor.polling_rate = bad_value
            with self.subTest(polling_rate=bad_value), self.assertRaises(ConfigurationError):
                configuration.validate()
        configuration = Configuration()
        configuration.sensor.stages[configuration.sensor.current_stage].enabled = False
        with self.assertRaisesRegex(ConfigurationError, "current DPI stage must be enabled"):
            configuration.validate()
        configuration = Configuration()
        configuration.sensor.stages[0].enabled = 1
        with self.assertRaises(ConfigurationError):
            configuration.validate()

    def test_custom_easy_aim_dpi_action_roundtrips_and_validates_steps(self):
        for dpi in (50, 200, 1600, 30000):
            configuration = Configuration()
            configuration.buttons[8].primary = Action("dpi", f"precision_custom_{dpi}")
            with self.subTest(dpi=dpi):
                self.assertEqual(Configuration.from_dict(configuration.to_dict()), configuration)
        for value in ("precision_custom_0", "precision_custom_49", "precision_custom_51",
                      "precision_custom_30050", "precision_custom_0050",
                      "precision_custom_50.0", "precision_custom_"):
            configuration = Configuration()
            configuration.buttons[8].primary = Action("dpi", value)
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                configuration.validate()

    def test_unsafe_action_forms_and_unknown_macro_references_rejected(self):
        for action in [Action("shell", "touch /tmp/example"), Action("command", "calc.exe"),
                       Action("keyboard", "Ctrl+S;touch /tmp/example"),
                       Action("keyboard", "$(example)"), Action("keyboard", "A+B"),
                       Action("macro", "missing"), Action("disabled", "payload")]:
            configuration = Configuration()
            configuration.buttons[0].primary = action
            with self.subTest(action=action), self.assertRaises(ConfigurationError):
                configuration.validate()

    def test_malformed_and_unbalanced_macros_are_rejected(self):
        bad_macros = [
            Macro(events=[MacroEvent("key_down", "A", 0)]),
            Macro(events=[MacroEvent("key_up", "A", 0)]),
            Macro(events=[MacroEvent("key_down", "A", 0), MacroEvent("key_down", "A", 0)]),
            Macro(events=[MacroEvent("shell", "example", 0)]),
            Macro(events=[MacroEvent("delay", "", -1)]),
            Macro(events=[MacroEvent("delay", "", True)]),
            Macro(events=[MacroEvent("delay", "", 60001)]),
            Macro(events=[MacroEvent("delay", "", 0)] * 1001),
            Macro(repeat=0), Macro(repeat=1000), Macro(playback="execute"),
        ]
        for macro in bad_macros:
            configuration = Configuration(macros=[macro])
            with self.subTest(macro=macro), self.assertRaises(ConfigurationError):
                configuration.validate()
        duplicate = Macro(id="repeated")
        with self.assertRaisesRegex(ConfigurationError, "unique ID"):
            Configuration(macros=[duplicate, copy.deepcopy(duplicate)]).validate()

    def test_button_ids_and_display_layout_are_not_ambiguous(self):
        configuration = Configuration()
        configuration.buttons[-1].button_id = 1
        with self.assertRaisesRegex(ConfigurationError, "exactly once"):
            configuration.validate()
        configuration = Configuration()
        configuration.display.widgets = ["dpi", "dpi"]
        with self.assertRaisesRegex(ConfigurationError, "only once"):
            configuration.validate()


class PresetStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = PresetStore(self.root / "presets")

    def test_save_import_export_list_delete_and_names_cannot_escape_store(self):
        self.assertEqual(self.store.list(), [])
        configuration = Configuration(name="../../My presets / 日本語")
        configuration.appearance.color = "#123ABC"
        configuration.appearance.image = ONE_PIXEL_PNG
        path = self.store.save(configuration)
        self.assertEqual(path.parent, self.store.directory)
        self.assertEqual(self.store.list(), [configuration])
        if os.name == "posix":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        configuration.sensor.stages[0].value = 600
        self.store.save(configuration)
        self.assertEqual(len(self.store.list()), 1)
        self.assertEqual(self.store.list()[0].sensor.stages[0].value, 600)
        exported = self.store.export(configuration, self.root / "exported.json")
        self.assertEqual(self.store.import_file(exported, save=False), configuration)
        self.assertTrue(self.store.delete(configuration.name))
        self.assertFalse(self.store.delete(configuration.name))
        self.assertEqual(self.store.list(), [])
        self.assertEqual(self.store.import_file(exported), configuration)
        self.assertEqual(self.store.list(), [configuration])

    def test_failed_replace_preserves_previous_valid_preset_and_cleans_temp(self):
        configuration = Configuration()
        path = self.store.save(configuration)
        original = path.read_bytes()
        configuration.sensor.stages[0].value = 750
        with patch("swarm2.configuration.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaisesRegex(OSError, "disk failure"):
                self.store.save(configuration)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(self.store.directory.iterdir()), [path])

    def test_import_name_collision_preserves_local_edit(self):
        original = Configuration(name="Gaming")
        self.store.save(original)
        imported = Configuration(name="Gaming")
        imported.sensor.polling_rate = 8000
        path = self.store.export(imported, self.root / "shared.json")
        result = self.store.import_file(path)
        self.assertEqual(result.name, "Gaming (imported)")
        self.assertEqual([item.sensor.polling_rate for item in self.store.list()], [1000, 8000])
        self.assertEqual(self.store.import_file(path), result)
        self.assertEqual(len(self.store.list()), 2)

    def test_invalid_save_does_not_replace_existing_preset(self):
        configuration = Configuration()
        path = self.store.save(configuration)
        original = path.read_bytes()
        configuration.sensor.stages[0].value = 33
        with self.assertRaises(ConfigurationError):
            self.store.save(configuration)
        self.assertEqual(path.read_bytes(), original)

    def test_import_has_size_encoding_structure_and_duplicate_key_limits(self):
        path = self.root / "malformed.json"
        payloads = [b"x" * (MAX_PRESET_BYTES + 1), b"\xff", b"not json", b"[]", b"null",
                    b"{" + b'"name":"a","name":"b"' + b"}",
                    b"[" * 2000 + b"]" * 2000, b'{"value":NaN}']
        for raw in payloads:
            path.write_bytes(raw)
            with self.subTest(raw=raw[:50]), self.assertRaises(ConfigurationError):
                self.store.import_file(path)
            self.assertFalse(self.store.directory.exists())

    def test_malicious_action_import_is_never_saved_or_executed(self):
        data = Configuration().to_dict()
        marker = self.root / "should-not-exist"
        data["buttons"][0]["primary"] = {"kind": "shell", "value": f"touch {marker}"}
        path = self.root / "malicious.json"
        path.write_text(json.dumps(data))
        with self.assertRaises(ConfigurationError):
            self.store.import_file(path)
        self.assertFalse(marker.exists())
        self.assertFalse(self.store.directory.exists())

    def test_bad_stored_preset_reports_filename(self):
        self.store.directory.mkdir()
        (self.store.directory / "broken.json").write_text("[]")
        with self.assertRaisesRegex(ConfigurationError, "broken.json"):
            self.store.list()

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO test needs POSIX")
    def test_fifo_import_is_rejected_without_waiting_for_a_writer(self):
        path = self.root / "pipe.json"
        os.mkfifo(path)
        with self.assertRaisesRegex(ConfigurationError, "regular JSON"):
            read_preset(path)

    def test_platform_directories_and_relative_xdg_override(self):
        with patch("swarm2.configuration.sys.platform", "darwin"), patch("swarm2.configuration.Path.home", return_value=self.root):
            self.assertEqual(configuration_directory(), self.root / "Library/Application Support/swarm2")
        with patch("swarm2.configuration.sys.platform", "win32"), patch.dict(os.environ, {"APPDATA": str(self.root)}):
            self.assertEqual(configuration_directory(), self.root / "MC7 Studio")
        with patch("swarm2.configuration.sys.platform", "linux"), patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root)}):
            self.assertEqual(configuration_directory(), self.root / "swarm2")
        with patch("swarm2.configuration.sys.platform", "linux"), patch.dict(os.environ, {"XDG_CONFIG_HOME": "relative"}), patch("swarm2.configuration.Path.home", return_value=self.root):
            self.assertEqual(configuration_directory(), self.root / ".config/swarm2")


if __name__ == "__main__":
    unittest.main()
