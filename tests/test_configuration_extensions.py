"""Extended presets preserve old drafts, LCD widths, and opaque device values."""

import copy
import json
from pathlib import Path
import tempfile
import unittest

from swarm2.configuration import (
    Action, Configuration, ConfigurationError, DEFAULT_PROFILE_COLOR, Macro,
    PresetStore, read_preset,
)
from swarm2.lcd_commands import decode_lcd_response
from swarm2.service import DeviceService
from swarm2.settings import _plan, lcd_edits, stable
from swarm2.transport import DeviceError
from tests.test_lcd_commands import CAPTURE as LCD_CAPTURE
from tests.test_settings import CAPTURES, with_checksum


class ConfigurationExtensionTests(unittest.TestCase):
    def test_old_version_one_draft_without_new_display_fields_loads_without_rewriting_old_values(self):
        original = Configuration(name="Existing local preset", profile_slot=4).to_dict()
        original["display"].pop("pages")
        original["display"].pop("key_bindings")
        original["display"].pop("macro_bindings")
        original["display"].pop("timeout_value")
        original.pop("appearance")
        original["display"]["timeout_seconds"] = 237
        original["buttons"][3]["primary"] = {"kind": "mouse", "value": "back"}
        before = copy.deepcopy(original)
        restored = Configuration.from_dict(original)
        self.assertEqual(original, before)
        self.assertEqual(restored.display.pages, [])
        self.assertEqual(restored.display.key_bindings, [])
        self.assertEqual(restored.display.macro_bindings, [])
        self.assertEqual(restored.display.timeout_value, 10)
        self.assertEqual(restored.display.timeout_seconds, 237)
        self.assertEqual(restored.buttons[3].primary, Action("mouse", "back"))
        self.assertEqual(restored.appearance.color, DEFAULT_PROFILE_COLOR)
        self.assertIsNone(restored.appearance.image)
        self.assertEqual(restored.profile_slot, 4)
        self.assertEqual(restored.to_dict()["version"], 1)
        self.assertEqual(restored.to_dict()["source"], "local_draft")
        self.assertEqual(Configuration.from_dict(restored.to_dict()), restored)

    def test_old_version_one_file_import_uses_validated_defaults_and_preserves_source_file(self):
        data = Configuration(name="Old file").to_dict()
        data["display"]["pages"] = [["cut", "copy", "paste", "undo"]]
        data["display"].pop("key_bindings")
        data["display"].pop("macro_bindings")
        data["display"].pop("timeout_value")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.json"
            content = json.dumps(data).encode()
            path.write_bytes(content)
            store = PresetStore(Path(directory) / "presets")
            restored = store.import_file(path)
            self.assertEqual(restored.display.pages, [["cut", "copy", "paste", "undo"]])
            self.assertEqual(restored.display.key_bindings, [])
            self.assertEqual(restored.display.macro_bindings, [])
            self.assertEqual(store.list(), [restored])
            self.assertEqual(path.read_bytes(), content)

    def test_pages_with_wide_unknown_and_empty_widgets_roundtrip(self):
        configuration = Configuration()
        configuration.display.pages = [
            ["system_media", None, None, "dpi"],
            ["unknown_45_12", None, None, "unknown_d4_37"],
            ["empty", "play_pause", "next_track", "previous_track"],
        ]
        restored = Configuration.from_dict(configuration.to_dict())
        self.assertEqual(restored, configuration)
        restored.display.pages[0][3] = "empty"
        self.assertEqual(configuration.display.pages[0][3], "dpi")

    def test_general_media_page_roundtrips_and_requires_two_continuations(self):
        configuration = Configuration()
        configuration.display.pages = [
            ["general_media", None, None, "dpi"],
        ]
        restored = Configuration.from_dict(configuration.to_dict())
        self.assertEqual(restored.display.pages, configuration.display.pages)

        for page in (
            ["general_media", "empty", None, "dpi"],
            ["empty", "empty", "general_media", None],
        ):
            data = Configuration().to_dict()
            data["display"]["pages"] = [page]
            with self.subTest(page=page), self.assertRaises(ConfigurationError):
                Configuration.from_dict(data)

    def test_pages_reject_oversize_wrong_types_overlaps_and_forged_widget_names(self):
        invalid = [
            None, {}, "dpi", [["empty"] * 4] * 4, [["empty"] * 3], [["empty"] * 5],
            [[None, "empty", "empty", "empty"]],
            [["system_media", "dpi", None, "empty"]],
            [["empty", "empty", "system_media", None]],
            [["unknown_65_01", None, "dpi", "empty"]],
            [["unknown_xx_12", "empty", "empty", "empty"]],
            [["unknown_45_100", None, None, "empty"]],
            [[False, "empty", "empty", "empty"]],
            [[{"command": "example"}, "empty", "empty", "empty"]],
        ]
        for pages in invalid:
            data = Configuration().to_dict()
            data["display"]["pages"] = pages
            with self.subTest(pages=pages), self.assertRaises(ConfigurationError):
                Configuration.from_dict(data)

    def test_lcd_key_bindings_roundtrip_with_simple_shortcut_and_opaque_values(self):
        configuration = Configuration()
        configuration.display.pages = [
            ["remap_key", "hotkey", "dpi", "empty"],
            ["hotkey", "copy", "remap_key", "empty"],
        ]
        configuration.display.key_bindings = [
            ["F5", "Cmd+Shift+S", None, None],
            ["device:00112233445566778899aa", None, "Enter", None],
        ]
        restored = Configuration.from_dict(configuration.to_dict())
        self.assertEqual(restored, configuration)

    def test_lcd_key_bindings_follow_layout_and_reject_unsafe_values(self):
        base = Configuration().to_dict()
        base["display"]["pages"] = [["remap_key", "hotkey", "dpi", "empty"]]
        invalid = [
            [],
            [["Ctrl+S", "Ctrl+S", None, None]],
            [["A", "Ctrl", None, None]],
            [["A", "Ctrl+S", "F5", None]],
            [["A", "Ctrl+S", None]],
            [["A", "Ctrl+S", None, None], [None] * 4],
            [["A", "device:00112233445566778899AA", None, None]],
            [["A", 42, None, None]],
        ]
        for bindings in invalid:
            data = copy.deepcopy(base)
            data["display"]["key_bindings"] = bindings
            with self.subTest(bindings=bindings), self.assertRaises(ConfigurationError):
                Configuration.from_dict(data)

        empty = copy.deepcopy(base)
        empty["display"]["key_bindings"] = [[None, None, None, None]]
        self.assertEqual(Configuration.from_dict(empty).display.key_bindings,
                         [[None, None, None, None]])

    def test_lcd_macro_bindings_roundtrip_and_reference_the_local_library(self):
        configuration = Configuration(macros=[
            Macro(id="show_desktop", name="Show desktop"),
            Macro(id="paste_plain", name="Paste plain text"),
        ])
        configuration.display.pages = [
            ["macro", "copy", "macro", "empty"],
            ["dpi", "empty", "empty", "empty"],
        ]
        configuration.display.macro_bindings = [
            ["show_desktop", None, "paste_plain", None],
            [None, None, None, None],
        ]
        self.assertEqual(Configuration.from_dict(configuration.to_dict()), configuration)

        missing_rows = copy.deepcopy(configuration.to_dict())
        missing_rows["display"]["macro_bindings"] = []
        self.assertEqual(Configuration.from_dict(missing_rows).display.macro_bindings, [])
        invalid = []
        wrong_slot = copy.deepcopy(configuration.to_dict())
        wrong_slot["display"]["macro_bindings"][0][1] = "show_desktop"
        invalid.append(wrong_slot)
        missing_macro = copy.deepcopy(configuration.to_dict())
        missing_macro["display"]["macro_bindings"][0][0] = "not_in_library"
        invalid.append(missing_macro)
        for data in invalid:
            with self.subTest(bindings=data["display"]["macro_bindings"]), self.assertRaises(ConfigurationError):
                Configuration.from_dict(data)

    def test_timeout_value_bounds_are_independent_of_legacy_seconds(self):
        for value in (-1, 31, True, 1.0, "10", None):
            data = Configuration().to_dict()
            data["display"]["timeout_value"] = value
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                Configuration.from_dict(data)
        for value in (0, 30):
            configuration = Configuration()
            configuration.display.timeout_value = value
            configuration.display.timeout_seconds = 3600
            self.assertEqual(Configuration.from_dict(configuration.to_dict()), configuration)

    def test_extended_actions_and_four_byte_opaque_action_roundtrip_through_disk(self):
        configuration = Configuration()
        actions = [Action("device", "7A12347E"), Action("mouse", "double_click"),
                   Action("media", "stop"), Action("profile", "5"), Action("dpi", "precision_stage_4")]
        for binding, action in zip(configuration.buttons, actions):
            binding.easy_shift = action
        with tempfile.TemporaryDirectory() as directory:
            path = PresetStore(directory).save(configuration)
            self.assertEqual(read_preset(path), configuration)
        for value in ("", "7a1234", "7a12347e00", "0x7a12347e", "7a 12 34 7e", "7a1234xx", "$(test)"):
            configuration.buttons[0].easy_shift = Action("device", value)
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                configuration.validate()

    def test_launch_and_easy_wheel_actions_roundtrip_with_strict_values(self):
        supported = (
            Action("launch", "browser"),
            Action("launch", "calculator"),
            Action("easy_wheel", "dpi"),
            Action("easy_wheel", "volume"),
            Action("easy_wheel", "alt_tab"),
            Action("easy_wheel", "desktop"),
        )
        configuration = Configuration()
        for binding, action in zip(configuration.buttons, supported):
            binding.primary = action
        self.assertEqual(
            Configuration.from_dict(configuration.to_dict()).buttons[:len(supported)],
            configuration.buttons[:len(supported)],
        )

        for action in (
            Action("launch", "dpi"),
            Action("launch", ""),
            Action("easy_wheel", "browser"),
            Action("easy_wheel", "alt-tab"),
        ):
            configuration = Configuration()
            configuration.buttons[0].primary = action
            with self.subTest(action=action), self.assertRaises(ConfigurationError):
                configuration.validate()

    def test_unknown_top_level_or_display_fields_still_rejected_in_version_one(self):
        for section in (None, "display"):
            data = Configuration().to_dict()
            target = data if section is None else data[section]
            target["execute"] = "example"
            with self.subTest(section=section), self.assertRaises(ConfigurationError):
                Configuration.from_dict(data)


class PreservedDeviceValuesTests(unittest.TestCase):
    def test_energy_plan_predicts_checksum_valid_semantic_firmware_readback(self):
        result = {"raw": CAPTURES["sensor"], "changed": False, "settings": CAPTURES}
        configuration = DeviceService._snapshot("fixture", 1, result)["configuration"]
        configuration.power.energy_saving = True
        raw = {name: bytes.fromhex(value) for name, value in CAPTURES.items()}
        commands, expected = _plan("power", configuration, raw, 0)
        self.assertEqual(commands, [bytes.fromhex("1012801559") + bytes(59)])
        observed = bytes.fromhex("10120000f50b")
        self.assertEqual(stable("profile", bytes(expected["profile"])), stable("profile", observed))
        self.assertEqual(raw["profile"], bytes.fromhex(CAPTURES["profile"]))

    def test_opaque_button_is_preserved_after_preset_roundtrip_but_cannot_be_forged(self):
        primary = bytearray.fromhex(CAPTURES["primary"])
        primary[24:28] = bytes.fromhex("7a12347e")  # logical slot five
        settings = {"sensor": CAPTURES["sensor"], "primary": primary.hex(),
                    "easy_shift": CAPTURES["easy_shift"]}
        result = {"raw": CAPTURES["sensor"], "changed": False, "settings": settings}
        snapshot = DeviceService._snapshot("fixture", 1, result)
        configuration = Configuration.from_dict(snapshot["configuration"].to_dict())
        self.assertEqual(configuration.buttons[5].primary, Action("device", "7a12347e"))
        raw = {name: bytes.fromhex(value) for name, value in settings.items()}
        commands, _ = _plan("buttons", configuration, raw, 0)
        self.assertEqual(commands, [])
        configuration.buttons[5].primary = Action("device", "7a12347f")
        with self.assertRaisesRegex(DeviceError, "only be preserved"):
            _plan("buttons", configuration, raw, 0)

    def test_unknown_lcd_widget_can_only_be_preserved_on_its_existing_page(self):
        raw = bytearray(LCD_CAPTURE)
        raw[30:32] = bytes.fromhex("d437")
        state = decode_lcd_response(raw, 0)
        pages = [[None if widget is None else widget.key for widget in page.slots]
                 for page in state.pages[:3]]
        configuration = Configuration()
        configuration.display.pages = pages
        restored = Configuration.from_dict(configuration.to_dict())
        self.assertEqual(lcd_edits(state, restored.display.pages), {})
        restored.display.pages[0] = ["unknown_d4_37", "empty", "empty", "empty"]
        with self.assertRaisesRegex(DeviceError, "current page"):
            lcd_edits(state, restored.display.pages)

    def test_unknown_lighting_effect_or_speed_never_turns_into_an_implicit_default_write(self):
        for effect, speed in ((0x7E, 5), (5, 0), (5, 127)):
            raw = bytearray.fromhex(CAPTURES["lighting"])
            raw[6], raw[7] = effect, speed
            raw = with_checksum(raw)
            result = {"raw": CAPTURES["sensor"], "changed": False,
                      "settings": {"sensor": CAPTURES["sensor"], "lighting": raw.hex()}}
            snapshot = DeviceService._snapshot("fixture", 1, result)
            configuration = snapshot["configuration"]
            for brightness in (configuration.lighting.brightness, 50):
                configuration.lighting.brightness = brightness
                with self.subTest(effect=effect, speed=speed, brightness=brightness), self.assertRaises(DeviceError):
                    _plan("lighting", configuration, {"lighting": raw}, 0)


if __name__ == "__main__":
    unittest.main()
