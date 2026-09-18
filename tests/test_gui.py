"""Exercise the native editor with a fake service, never a physical mouse."""

import copy
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication, QDialog, QFileDialog
    from swarm2.gui.app import MainWindow
except ModuleNotFoundError as error:
    if error.name and error.name.startswith("PySide6"):
        MainWindow = None
    else:
        raise

from swarm2.configuration import Action, Configuration, Macro, MacroEvent, PresetStore
from swarm2.sensor_commands import SCREEN_TIMEOUT_VALUES
from swarm2.service import DeviceService
from tests.test_macro_snapshot import bind, macro, result_for


class FakeService:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.status = None
        self.active_profile = 1
        self.capabilities = ["read_settings", "read_sensor", "read_active_profile", "apply_sensor", "apply_lighting", "apply_buttons", "apply_display", "apply_power", "activate_display", "switch_profile"]
        self.verified_fields = ["sensor.stages", "sensor.current_stage", "sensor.dpi_indicator_enabled"]
        self.configuration = Configuration()
        self.configuration.sensor.stages[0].value = 650
        self.configuration.sensor.current_stage = 0

    def discover(self):
        self.calls.append("discover")
        return [{"id": "test-mc7", "label": "MC7 test mouse", "product_id": 0x502C,
                 "connected": True, "capabilities": self.capabilities}]

    def snapshot(self, slot=1):
        return {"device_id": "test-mc7", "profile_slot": slot,
                "configuration": copy.deepcopy(self.configuration),
                "verified_fields": self.verified_fields,
                "summary": {"dpi": self.configuration.sensor.stages[self.configuration.sensor.current_stage].value,
                            "stage": self.configuration.sensor.current_stage + 1,
                            "active_profile": self.active_profile,
                            "read_at": "2026-09-15T12:00:00Z", "transport": "Fake USB", "status": self.status},
                "baseline": {"opaque": "test-baseline"}}

    def read(self, device_id, profile_slot=1, draft=None):
        self.calls.append(("read", device_id, profile_slot))
        if self.failure:
            raise RuntimeError(self.failure)
        return self.snapshot(profile_slot)

    def apply(self, device_id, configuration, baseline=None):
        self.calls.append(("apply", device_id, copy.deepcopy(configuration), baseline))
        if self.failure:
            raise RuntimeError(self.failure)
        self.configuration = copy.deepcopy(configuration)
        return self.snapshot(configuration.profile_slot)

    def apply_section(self, device_id, configuration, section, baseline=None):
        self.calls.append(("apply_section", device_id, section, copy.deepcopy(configuration), baseline))
        if self.failure:
            raise RuntimeError(self.failure)
        setattr(self.configuration, section, copy.deepcopy(getattr(configuration, section)))
        return self.snapshot(configuration.profile_slot)

    def activate(self, device_id, profile_slot=1):
        self.calls.append(("activate", device_id, profile_slot))
        snapshot = self.snapshot(profile_slot)
        snapshot["summary"]["activation_acknowledged"] = True
        return snapshot

    def setup_display(self, device_id, profile_slot=1):
        self.calls.append(("setup_display", device_id, profile_slot))
        self.configuration.display.pages = [["next_track", "led_brightness", "play_pause", "dpi"]]
        snapshot = self.snapshot(profile_slot)
        snapshot["verified_fields"] = self.verified_fields + ["display.pages", "display.brightness"]
        snapshot["summary"].update(lcd_setup_verified=True, changed=True)
        return snapshot

    def switch_profile(self, device_id, profile_slot=1, draft=None):
        self.calls.append(("switch_profile", device_id, profile_slot))
        changed = self.active_profile != profile_slot
        self.active_profile = profile_slot
        snapshot = self.snapshot(profile_slot)
        snapshot["summary"].update(changed=changed, activation_acknowledged=True)
        return snapshot

    def read_active_profile(self, device_id):
        self.calls.append(("read_active_profile", device_id))
        if self.failure:
            raise RuntimeError(self.failure)
        return {"active_profile": self.active_profile, "profile_count": 5,
                "energy_saving": False, "changed": False}

    def upload_background(self, device_id, rgba):
        self.calls.append(("upload_background", device_id, rgba))
        if self.failure:
            raise RuntimeError(self.failure)
        return {"acknowledged": True, "pixel_readback": False, "scope": "all_profiles"}


@unittest.skipIf(MainWindow is None, "Install the gui extra to exercise Qt")
class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = FakeService()
        self.store = PresetStore(Path(self.directory.name) / "presets")
        self.window = MainWindow(service=self.service, store=self.store, auto_discover=False)

    def tearDown(self):
        self.wait_for_job()
        self.window.dirty = False
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.directory.cleanup()

    def wait_for_job(self):
        deadline = time.monotonic() + 3
        while self.window._job is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.app.processEvents()
        self.assertIsNone(self.window._job, "Device job did not finish")

    def discover(self):
        self.window.refresh_device()
        self.wait_for_job()

    def read(self):
        self.window.navigation.setCurrentRow(1)
        self.discover()
        self.window.read_mouse()
        self.wait_for_job()

    def test_timeout_controls_show_minutes_without_changing_wire_values(self):
        self.assertEqual(
            [self.window.screen_timeout.itemData(index)
             for index in range(self.window.screen_timeout.count())],
            list(SCREEN_TIMEOUT_VALUES),
        )
        self.assertEqual(
            [self.window.screen_timeout.itemText(index)
             for index in range(self.window.screen_timeout.count())],
            [f"{value} min" for value in SCREEN_TIMEOUT_VALUES],
        )
        self.assertEqual(self.window.screen_timeout.accessibleName(),
                         "Screen timeout (minutes)")
        self.assertEqual(self.window.standby_timeout.suffix(), " min")
        self.assertEqual(self.window.led_timeout.suffix(), " min")

        self.window.screen_timeout.setCurrentIndex(
            self.window.screen_timeout.findData(25))
        self.window.standby_timeout.setValue(30)
        self.window.led_timeout.setValue(0)
        self.assertEqual(self.window.draft.display.timeout_value, 25)
        self.assertEqual(self.window.draft.power.standby_value, 30)
        self.assertEqual(self.window.draft.power.led_timeout_value, 0)
        self.assertEqual(self.service.calls, [])

    def test_portable_onboard_shortcuts_are_offered_without_windows_only_tiles(self):
        self.window.draft.display.pages = [["empty"] * 4]
        self.window._load_draft()
        choices = {
            self.window.lcd_controls[0][0].itemData(index)
            for index in range(self.window.lcd_controls[0][0].count())
        }
        self.assertTrue({
            "launch_browser", "browser_back", "browser_forward", "calculator",
        }.issubset(choices))
        self.assertTrue({"emoji", "screenshot", "game_bar"}.isdisjoint(choices))
        self.assertEqual(self.service.calls, [])

    def test_host_launch_tiles_offer_target_editor_and_store_website(self):
        self.window.draft.display.pages = [["empty"] * 4, ["empty"] * 4]
        self.window._load_draft()
        tile = self.window.lcd_controls[0][0]
        editor = self.window.lcd_host_action_editors[0][0]
        browse = self.window.lcd_host_action_browse_buttons[0][0]

        choices = {
            tile.itemData(index) for index in range(tile.count())
        }
        self.assertTrue({
            "open_website", "open_file", "open_folder",
        }.issubset(choices))
        self.assertFalse(editor.isEnabled())
        self.assertTrue(editor.isHidden())

        tile.setCurrentIndex(tile.findData("open_website"))
        self.assertTrue(editor.isEnabled())
        self.assertFalse(editor.isHidden())
        self.assertTrue(browse.isHidden())
        self.assertEqual(editor.placeholderText(), "https://example.com")
        self.assertFalse(self.window.lcd_move_right.isEnabled())
        self.assertIn("unresolved host action",
                      self.window.lcd_move_right.toolTip())
        editor.setText("https://example.com/mc7")

        self.assertEqual(self.window.draft.display.host_action_bindings, [
            ["https://example.com/mc7", None, None, None],
            [None, None, None, None],
        ])
        self.assertTrue(self.window.lcd_move_right.isEnabled())
        self.assertTrue(self.window.dirty)
        self.assertEqual(self.service.calls, [])
        self.window.draft.validate()

    def test_host_file_and_folder_tiles_use_choosers_and_keep_cancelled_value(self):
        self.window.draft.display.pages = [["empty"] * 4]
        self.window._load_draft()
        tile = self.window.lcd_controls[0][0]
        editor = self.window.lcd_host_action_editors[0][0]
        browse = self.window.lcd_host_action_browse_buttons[0][0]

        tile.setCurrentIndex(tile.findData("open_file"))
        self.assertFalse(browse.isHidden())
        self.assertEqual(browse.text(), "Choose file…")
        with patch("swarm2.gui.app.QFileDialog.getOpenFileName",
                   return_value=("/home/example/Documents/mc7.txt", "")) as choose:
            browse.click()
        choose.assert_called_once()
        self.assertEqual(editor.text(), "/home/example/Documents/mc7.txt")
        self.assertEqual(
            self.window.draft.display.host_action_bindings[0][0],
            "/home/example/Documents/mc7.txt",
        )

        tile.setCurrentIndex(tile.findData("open_folder"))
        self.assertEqual(editor.text(), "")
        self.assertEqual(browse.text(), "Choose folder…")
        with patch("swarm2.gui.app.QFileDialog.getExistingDirectory",
                   return_value="/home/example/Documents") as choose:
            browse.click()
        choose.assert_called_once()
        self.assertEqual(editor.text(), "/home/example/Documents")
        with patch("swarm2.gui.app.QFileDialog.getExistingDirectory",
                   return_value=""):
            browse.click()
        self.assertEqual(editor.text(), "/home/example/Documents")
        self.window.draft.validate()

    def test_host_launch_target_survives_neighbor_edit_and_moves_with_page(self):
        self.window.draft.display.pages = [
            ["open_website", "copy", "paste", "undo"],
            ["open_folder", "dpi", "led_brightness", "play_pause"],
        ]
        self.window.draft.display.host_action_bindings = [
            ["https://example.com/mc7", None, None, None],
            ["/home/example/Documents", None, None, None],
        ]
        self.window._load_draft()

        neighbor = self.window.lcd_controls[0][1]
        neighbor.setCurrentIndex(neighbor.findData("next_track"))
        self.assertEqual(
            self.window.draft.display.host_action_bindings[0][0],
            "https://example.com/mc7",
        )

        self.window.lcd_tabs.setCurrentIndex(0)
        self.assertTrue(self.window.lcd_move_right.isEnabled())
        self.window._move_lcd_page(1)
        self.assertEqual(self.window.draft.display.pages[0][0], "open_folder")
        self.assertEqual(self.window.draft.display.host_action_bindings, [
            ["/home/example/Documents", None, None, None],
            ["https://example.com/mc7", None, None, None],
        ])
        self.assertEqual(
            self.window.lcd_host_action_editors[1][0].text(),
            "https://example.com/mc7",
        )

        moved_tile = self.window.lcd_controls[1][0]
        moved_tile.setCurrentIndex(moved_tile.findData("dpi"))
        self.assertIsNone(
            self.window.draft.display.host_action_bindings[1][0])
        self.assertEqual(
            self.window.draft.display.host_action_bindings[0][0],
            "/home/example/Documents",
        )
        self.window.draft.validate()

    def test_lcd_read_keeps_matching_host_target_and_clears_changed_tile(self):
        self.window.draft.display.pages = [[
            "open_website", "open_file", "copy", "paste",
        ]]
        self.window.draft.display.host_action_bindings = [[
            "https://example.com/mc7", "/home/example/Documents/mc7.txt",
            None, None,
        ]]
        self.service.configuration.display.pages = [[
            "open_website", "open_folder", "copy", "paste",
        ]]
        self.service.configuration.display.host_action_bindings = [[
            "https://example.com/mc7", None, None, None,
        ]]
        self.service.verified_fields.append("display.pages")

        self.read()

        self.assertEqual(self.window.draft.display.host_action_bindings, [[
            "https://example.com/mc7", None, None, None,
        ]])
        self.assertEqual(
            self.window.lcd_host_action_editors[0][0].text(),
            "https://example.com/mc7",
        )
        self.assertEqual(self.window.lcd_host_action_editors[0][1].text(), "")
        self.window.draft.validate()

    def test_lcd_read_clears_target_rejected_by_device_snapshot(self):
        self.window.draft.display.pages = [[
            "open_website", "copy", "paste", "undo",
        ]]
        self.window.draft.display.host_action_bindings = [[
            "https://example.com/mc7", None, None, None,
        ]]
        self.service.configuration.display.pages = copy.deepcopy(
            self.window.draft.display.pages)
        # A real DeviceService snapshot leaves this unresolved when the
        # command-0x29 trigger does not match the local target.
        self.service.configuration.display.host_action_bindings = [[None] * 4]
        self.service.verified_fields.append("display.pages")

        self.read()

        self.assertEqual(self.window.draft.display.host_action_bindings,
                         [[None, None, None, None]])
        self.assertEqual(self.window.lcd_host_action_editors[0][0].text(), "")

    def test_launch_and_discovery_never_read_or_apply(self):
        self.discover()
        self.assertEqual(self.service.calls, ["discover"])
        self.assertTrue(self.window.read_button.isEnabled())
        self.assertFalse(self.window.apply_button.isEnabled())
        self.assertEqual(self.window.overview_dpi.text(), "Not read")
        self.assertEqual(self.window.stack.count(), 8)
        self.assertFalse(self.window.dirty)

    def test_read_merges_verified_fields_without_replacing_other_edits(self):
        self.window.draft.lighting.brightness = 37
        self.window.draft.sensor.polling_rate = 8000
        self.read()
        self.assertEqual(self.window.draft.sensor.stages[0].value, 650)
        self.assertEqual(self.window.draft.lighting.brightness, 37)
        self.assertEqual(self.window.draft.sensor.polling_rate, 8000)
        self.assertIn("2026-09-15T12:00:00Z", self.window.device_summary.text())
        self.assertTrue(self.window.apply_button.isEnabled())
        self.assertTrue(self.window.dirty)

    def test_real_service_read_and_explicit_switch_keep_local_macro_identity(self):
        saved = macro()
        self.window.draft.macros = [saved]
        bind(self.window.draft, "primary", 5, saved.id)

        class FixtureService(DeviceService):
            def __init__(self, result):
                self.result = result
                self.requests = []
                self.read_drafts = []
                self.switch_drafts = []

            def _call(self, request):
                self.requests.append(copy.deepcopy(request))
                return copy.deepcopy(self.result)

            def read(self, device_id, profile_slot=1, draft=None):
                self.read_drafts.append(draft)
                return super().read(device_id, profile_slot, draft)

            def switch_profile(self, device_id, profile_slot, draft=None):
                self.switch_drafts.append(draft)
                return super().switch_profile(device_id, profile_slot, draft)

        service = FixtureService(result_for({("primary", 5): saved}))
        self.window.service = service
        self.window._device_discovered([{
            "id": "test-mc7", "label": "MC7 test mouse", "product_id": 0x502C,
            "connected": True,
            "capabilities": ["read_settings", "read_sensor", "switch_profile"],
        }])

        read_draft = self.window.draft.to_dict()
        self.window.read_mouse()
        self.wait_for_job()
        self.assertEqual(service.read_drafts[0].to_dict(), read_draft)
        self.assertIsNot(service.read_drafts[0], self.window.draft)
        self.assertEqual([item.id for item in self.window.draft.macros], [saved.id])
        self.assertEqual(self.window.draft.buttons[5].primary, Action("macro", saved.id))

        switch_draft = self.window.draft.to_dict()
        self.window.activate_profile()
        self.wait_for_job()
        self.assertEqual(service.switch_drafts[0].to_dict(), switch_draft)
        self.assertIsNot(service.switch_drafts[0], self.window.draft)
        self.assertEqual([item.id for item in self.window.draft.macros], [saved.id])
        self.assertEqual(self.window.draft.buttons[5].primary, Action("macro", saved.id))
        self.assertEqual(
            [request["operation"] for request in service.requests],
            ["read_settings", "switch_profile"],
        )

    def test_apply_passes_baseline_and_requires_matching_profile(self):
        self.read()
        self.window.profile_slot.setValue(2)
        self.assertFalse(self.window.apply_button.isEnabled())
        self.window.profile_slot.setValue(1)
        self.window.dpi_controls[0][1].setValue(900)
        self.window.apply_to_mouse()
        self.wait_for_job()
        kind, device, configuration, baseline = self.service.calls[-1]
        self.assertEqual((kind, device), ("apply", "test-mc7"))
        self.assertEqual(configuration.sensor.stages[0].value, 900)
        self.assertEqual(baseline, {"opaque": "test-baseline"})
        self.assertIn("read back successfully", self.window.message.text())

    def test_failed_apply_does_not_claim_success(self):
        self.read()
        self.service.failure = "Mouse disconnected during readback"
        self.window.apply_to_mouse()
        self.wait_for_job()
        self.assertEqual(self.window.message.text(), self.service.failure)
        self.assertEqual(self.window.message.objectName(), "error")
        self.assertFalse(self.window.apply_button.isEnabled())

    def test_unavailable_backend_keeps_apply_disabled(self):
        self.window._device_discovered([{"id": "test-mc7", "label": "MC7", "capabilities": []}])
        self.assertFalse(self.window.read_button.isEnabled())
        self.assertFalse(self.window.apply_button.isEnabled())
        self.window.apply_to_mouse()
        self.assertEqual(self.service.calls, [])

    def test_stage_enable_rules_preserve_valid_active_stage(self):
        self.window.dpi_controls[1][1].setValue(825)
        self.assertEqual(self.window.draft.sensor.stages[1].value, 850)
        self.assertEqual(self.window.dpi_controls[1][1].value(), 850)
        for index in (0, 2, 3, 4):
            self.window.dpi_controls[index][0].setChecked(False)
        self.window.dpi_controls[1][0].setChecked(False)
        self.assertTrue(self.window.draft.sensor.stages[1].enabled)
        self.window.draft.validate()
        self.window.dpi_controls[2][0].setChecked(True)
        self.window.dpi_controls[1][0].setChecked(False)
        self.assertEqual(self.window.draft.sensor.current_stage, 2)
        self.window.draft.validate()

    def test_preset_export_import_and_macro_editor_round_trip(self):
        self.window.draft.name = "Test preset"
        self.window.draft.display.haptic_intensity = "high"
        self.window.draft.power.eco_mode = True
        self.window._new_macro()
        self.window._add_delay()
        self.window.macro_name.setText("Pause briefly")
        self.window._edit_macro_meta()
        self.assertTrue(self.window.save_preset())
        saved = self.store.list()[0]
        self.assertEqual(saved.macros[0].events[0].delay_ms, 100)
        self.assertTrue(saved.power.eco_mode)
        filename = str(Path(self.directory.name) / "export.json")
        with patch.object(QFileDialog, "getSaveFileName", return_value=(filename, "")):
            self.window.export_preset()
        self.window.draft = Configuration()
        self.window.dirty = False
        with patch.object(QFileDialog, "getOpenFileName", return_value=(filename, "")):
            self.window.import_preset()
        self.assertEqual(self.window.draft.display.haptic_intensity, "high")
        self.assertEqual(self.window.draft.macros[0].name, "Pause briefly")
        self.assertTrue(self.window.dirty)

    def test_invalid_macro_is_not_saved(self):
        self.window.draft.macros.append(Macro(events=[MacroEvent(kind="key_down", value="A", delay_ms=0)]))
        self.assertFalse(self.window.save_preset())
        self.assertEqual(self.store.list(), [])
        self.assertEqual(self.window.message.objectName(), "error")

    def test_cancelled_recording_does_not_create_macro_or_change_draft(self):
        with patch("swarm2.gui.app.RecordingDialog") as factory:
            factory.return_value.exec.return_value = QDialog.DialogCode.Rejected
            self.window._record_macro()
        self.assertEqual(self.window.draft.macros, [])
        self.assertFalse(self.window.dirty)
        self.assertEqual(self.service.calls, [])

    def test_invalid_existing_macro_is_reported_before_recording_a_new_take(self):
        self.window.draft.macros = [Macro(events=[MacroEvent("key_down", "A", 0)])]
        self.window._refresh_macros()
        with patch("swarm2.gui.app.RecordingDialog") as factory:
            self.window._record_macro()
            factory.assert_not_called()
        self.assertIn("Correct this macro before recording", self.window.message.text())
        self.assertEqual(len(self.window.draft.macros[0].events), 1)

    def test_recording_appends_to_selected_macro_and_respects_remaining_capacity(self):
        macro = Macro(name="Existing", events=[MacroEvent("delay", "", 20)])
        self.window.draft.macros.append(macro)
        self.window._refresh_macros()
        recorded = [MacroEvent("key_down", "A", 0), MacroEvent("key_up", "A", 30)]
        with patch("swarm2.gui.app.RecordingDialog") as factory:
            factory.return_value.exec.return_value = QDialog.DialogCode.Accepted
            factory.return_value.recorded_events = recorded
            self.window._record_macro()
            factory.assert_called_once_with(self.window, max_events=999)
        self.assertEqual(macro.events[1:], recorded)
        self.assertEqual(macro.name, "Existing")
        self.assertTrue(self.window.dirty)
        self.window.draft.validate()
        self.assertEqual(self.service.calls, [])

    def test_accepted_recording_creates_macro_only_after_acceptance(self):
        recorded = [MacroEvent("mouse_down", "left", 0), MacroEvent("mouse_up", "left", 55)]
        with patch("swarm2.gui.app.RecordingDialog") as factory:
            factory.return_value.exec.return_value = QDialog.DialogCode.Accepted
            factory.return_value.recorded_events = recorded
            self.window._record_macro()
        self.assertEqual(self.window.draft.macros[0].events, recorded)
        self.assertTrue(self.window.dirty)
        self.assertEqual(self.service.calls, [])

    def test_apply_is_scoped_to_current_page(self):
        self.read()
        for page in (0, 5, 6):
            self.window.navigation.setCurrentRow(page)
            self.assertFalse(self.window.apply_button.isEnabled())
        for page, text in ((1, "sensitivity"), (2, "buttons"), (3, "lighting"), (4, "display settings"), (7, "power settings")):
            self.window.navigation.setCurrentRow(page)
            self.assertTrue(self.window.apply_button.isEnabled())
            self.assertEqual(self.window.apply_button.text(), f"Apply {text}")

    def test_full_read_merges_supported_sections_and_preserves_local_macro_library(self):
        self.service.verified_fields += ["buttons", "lighting.brightness", "display.timeout_value", "power.eco_mode"]
        self.service.configuration.buttons[0].primary = Action("device", "030012ab")
        self.service.configuration.lighting.brightness = 23
        self.service.configuration.display.timeout_value = 17
        self.service.configuration.power.eco_mode = True
        self.window.draft.macros = [Macro(name="Local only")]
        self.read()
        self.assertEqual(self.window.draft.buttons[0].primary.kind, "device")
        picker = self.window.button_table.cellWidget(0, 1).layout().itemAt(0).widget()
        self.assertEqual(picker.currentText(), "On-device action (preserved)")
        self.assertEqual(self.window.draft.lighting.brightness, 23)
        self.assertEqual(self.window.draft.display.timeout_value, 17)
        self.assertTrue(self.window.draft.power.eco_mode)
        self.assertEqual(self.window.draft.macros[0].name, "Local only")

    def test_device_macro_merge_replaces_matching_id_and_preserves_other_local_macros(self):
        local = Macro(id="local", name="Local only")
        self.window.draft.macros = [local, Macro(id="onboard", name="Old onboard")]
        incoming = Macro(id="onboard", name="Mouse macro", events=[MacroEvent("key_down", "A", 0), MacroEvent("key_up", "A", 5)])
        self.service.configuration.macros = [incoming]
        self.service.configuration.buttons[3].primary = Action("macro", "onboard")
        self.service.verified_fields += ["macros", "buttons"]
        self.read()
        self.assertEqual(self.window.draft.macros, [local, incoming])
        self.assertEqual(self.window.draft.buttons[3].primary.value, "onboard")
        self.window.draft.validate()

    def test_action_pickers_show_actual_values_for_both_layers_after_reload(self):
        macro = Macro(id="local_macro", name="Recorded shortcut")
        self.window.draft.macros = [macro]
        self.window.draft.buttons[4].primary = Action("macro", macro.id)
        self.window.draft.buttons[4].easy_shift = Action("keyboard", "Ctrl+Shift+S")
        self.window.draft.buttons[5].easy_shift = Action("device", "01020304")
        self.window._load_draft()
        for row, binding in enumerate(self.window.draft.buttons):
            for column, field in ((1, "primary"), (2, "easy_shift")):
                with self.subTest(row=row, field=field):
                    picker = self.window.button_table.cellWidget(row, column).layout().itemAt(0).widget()
                    expected = getattr(binding, field)
                    self.assertEqual(picker.currentData(), (expected.kind, expected.value))
        self.assertFalse(self.window.dirty)

    def test_action_pickers_offer_launch_and_easy_wheel_actions_on_both_layers(self):
        expected = {
            ("launch", "browser"): "Launch · Browser",
            ("launch", "calculator"): "Launch · Calculator",
            ("easy_wheel", "dpi"): "Easy Wheel · DPI",
            ("easy_wheel", "volume"): "Easy Wheel · Volume",
            ("easy_wheel", "alt_tab"): "Easy Wheel · Alt Tab",
            ("easy_wheel", "desktop"): "Easy Wheel · Desktop",
        }
        self.window._load_buttons()
        pickers = []
        for column in (1, 2):
            picker = self.window.button_table.cellWidget(0, column).layout().itemAt(0).widget()
            pickers.append(picker)
            choices = {
                picker.itemData(index): picker.itemText(index)
                for index in range(picker.count())
            }
            with self.subTest(column=column):
                for action, caption in expected.items():
                    self.assertEqual(choices.get(action), caption)
        self.assertFalse(self.window.dirty)
        pickers[0].setCurrentIndex(next(
            index for index in range(pickers[0].count())
            if pickers[0].itemData(index) == ("launch", "browser")))
        pickers[1].setCurrentIndex(next(
            index for index in range(pickers[1].count())
            if pickers[1].itemData(index) == ("easy_wheel", "alt_tab")))
        self.assertEqual(
            self.window.draft.buttons[0].primary, Action("launch", "browser"))
        self.assertEqual(
            self.window.draft.buttons[0].easy_shift,
            Action("easy_wheel", "alt_tab"),
        )
        self.assertTrue(self.window.dirty)

    def test_buttons_apply_merges_required_macros_and_reports_timing_adjustments(self):
        local = Macro(id="local", name="Local only")
        self.window.draft.macros = [local]
        incoming = Macro(id="device_macro", name="Uploaded")
        self.service.configuration.macros = [incoming]
        self.service.configuration.buttons[3].primary = Action("macro", incoming.id)
        self.service.verified_fields += ["macros", "buttons"]
        snapshot = self.service.snapshot()
        snapshot["macro_timing_adjustments"] = [{"macro_id": incoming.id}]
        self.window._mouse_applied(snapshot, "buttons")
        self.assertEqual(self.window.draft.macros, [local, incoming])
        self.assertIn("timing resolution", self.window.message.text())
        self.window.draft.validate()

    def test_full_library_preserves_local_data_and_defers_unresolvable_device_buttons(self):
        self.window.draft.macros = [Macro(id=f"local{index}", name=f"Local {index}") for index in range(64)]
        self.service.configuration.macros = [Macro(id="onboard", name="Mouse macro")]
        self.service.configuration.buttons[3].primary = Action("macro", "onboard")
        self.service.verified_fields += ["macros", "buttons"]
        before = copy.deepcopy(self.window.draft.buttons)
        self.read()
        self.assertEqual(len(self.window.draft.macros), 64)
        self.assertEqual(self.window.draft.buttons, before)
        self.assertIn("exceed 64", self.window.message.text())
        self.window.navigation.setCurrentRow(2)
        self.assertFalse(self.window.apply_button.isEnabled())
        self.window.draft.validate()
        self.window.draft.macros.pop()
        self.window.read_mouse()
        self.wait_for_job()
        self.assertTrue(self.window.apply_button.isEnabled())
        self.assertEqual(self.window.draft.buttons[3].primary, Action("macro", "onboard"))
        self.window.draft.validate()

    def test_full_library_defers_lcd_macro_layout_as_one_coherent_state(self):
        self.window.draft.macros = [
            Macro(id=f"local{index}", name=f"Local {index}")
            for index in range(64)]
        self.window.draft.display.pages = [["macro", "copy", "paste", "undo"]]
        self.window.draft.display.macro_bindings = [
            ["local0", None, None, None]]
        before = copy.deepcopy(self.window.draft.display)
        self.service.configuration.macros = [Macro(id="onboard", name="Mouse macro")]
        self.service.configuration.display.pages = [["copy", "macro", "paste", "undo"]]
        self.service.configuration.display.macro_bindings = [
            [None, "onboard", None, None]]
        self.service.verified_fields += [
            "macros", "display.pages", "display.macro_bindings"]

        self.read()

        self.assertEqual(self.window.draft.display, before)
        self.assertIn("button or LCD assignments", self.window.message.text())
        self.window.navigation.setCurrentRow(4)
        self.assertFalse(self.window.apply_button.isEnabled())
        self.window.draft.validate()

        self.window.draft.macros.pop()
        self.window.read_mouse()
        self.wait_for_job()
        self.assertEqual(self.window.draft.display.pages,
                         [["copy", "macro", "paste", "undo"]])
        self.assertEqual(self.window.draft.display.macro_bindings,
                         [[None, "onboard", None, None]])
        self.assertTrue(self.window.apply_button.isEnabled())
        self.window.draft.validate()

    def test_fresh_read_without_macros_clears_a_previous_library_capacity_warning(self):
        self.window._macro_merge_warning = "Previous device had too many macros"
        self.service.verified_fields.append("buttons")
        self.read()
        self.window.navigation.setCurrentRow(2)
        self.assertEqual(self.window._macro_merge_warning, "")
        self.assertTrue(self.window.apply_button.isEnabled())

    def test_section_apply_keeps_other_pages_unsent_edits(self):
        self.service.verified_fields += ["lighting.brightness", "power.eco_mode"]
        self.read()
        self.window.draft.lighting.brightness = 30
        self.window.draft.power.eco_mode = True
        self.window.draft.sensor.stages[0].value = 1100
        self.window.navigation.setCurrentRow(3)
        self.window.apply_to_mouse()
        self.wait_for_job()
        self.assertEqual(self.service.calls[-1][0:3], ("apply_section", "test-mc7", "lighting"))
        self.assertEqual(self.window.draft.lighting.brightness, 30)
        self.assertTrue(self.window.draft.power.eco_mode)
        self.assertEqual(self.window.draft.sensor.stages[0].value, 1100)
        self.assertFalse(self.service.configuration.power.eco_mode)
        self.assertEqual(self.service.configuration.sensor.stages[0].value, 650)

    def test_display_setup_reads_layout_and_keeps_other_display_edits(self):
        self.discover()
        self.window.draft.lighting.brightness = 29
        self.window.draft.display.brightness = 80
        self.window.setup_display()
        self.wait_for_job()
        self.assertEqual(self.service.calls[-1], ("setup_display", "test-mc7", 1))
        self.assertEqual(self.window.draft.lighting.brightness, 29)
        self.assertEqual(self.window.draft.display.brightness, 80)
        self.assertEqual(self.window.draft.display.pages, self.service.configuration.display.pages)
        self.assertIn("layout updated and read back", self.window.message.text())
        self.assertIn("Check the LCD", self.window.message.text())

    def test_fresh_lcd_editor_has_no_invented_layout_or_hardware_calls(self):
        self.assertEqual(self.window.draft.display.pages, [])
        self.assertFalse(self.window.lcd_tabs.isTabEnabled(0))
        self.assertFalse(self.window.lcd_controls[0][0].isEnabled())
        self.assertFalse(self.window.lcd_key_controls[0][0].isEnabled())
        self.assertEqual(self.service.calls, [])

    def test_lcd_key_tiles_enable_binding_fields_and_supply_valid_defaults(self):
        self.window.draft.display.pages = [["empty"] * 4]
        self.window._load_draft()
        tile = self.window.lcd_controls[0][0]
        binding = self.window.lcd_key_controls[0][0]
        self.assertGreaterEqual(tile.findData("remap_key"), 0)
        self.assertGreaterEqual(tile.findData("hotkey"), 0)
        self.assertFalse(binding.isEnabled())

        tile.setCurrentIndex(tile.findData("remap_key"))
        self.assertTrue(binding.isEnabled())
        self.assertEqual(binding.text(), "A")
        binding.setText("F5")
        self.assertEqual(self.window.draft.display.key_bindings, [["F5", None, None, None]])

        tile.setCurrentIndex(tile.findData("hotkey"))
        self.assertEqual(binding.text(), "Ctrl+S")
        self.assertEqual(self.window.draft.display.key_bindings[0][0], "Ctrl+S")
        binding.clear()
        self.assertIsNone(self.window.draft.display.key_bindings[0][0])
        self.window.draft.validate()
        tile.setCurrentIndex(tile.findData("dpi"))
        self.assertFalse(binding.isEnabled())
        self.assertEqual(self.window.draft.display.key_bindings[0][0], None)
        self.window.draft.validate()

    def test_lcd_macro_tile_selects_library_id_and_clears_on_replacement(self):
        first = Macro(id="first", name="First macro")
        second = Macro(id="second", name="Second macro")
        self.window.draft.macros = [first, second]
        self.window.draft.display.pages = [["empty"] * 4]
        self.window._load_draft()
        tile = self.window.lcd_controls[0][0]
        selector = self.window.lcd_macro_controls[0][0]
        self.assertGreaterEqual(tile.findData("macro"), 0)

        tile.setCurrentIndex(tile.findData("macro"))
        self.assertTrue(selector.isEnabled())
        self.assertIsNone(selector.currentData())
        selector.setCurrentIndex(selector.findData(second.id))
        self.assertEqual(self.window.draft.display.macro_bindings,
                         [[second.id, None, None, None]])
        self.window.draft.validate()

        tile.setCurrentIndex(tile.findData("dpi"))
        self.assertFalse(selector.isEnabled())
        self.assertIsNone(self.window.draft.display.macro_bindings[0][0])
        self.window.draft.validate()

    def test_lcd_macro_selector_excludes_while_held_playback(self):
        once = Macro(id="once", name="Once macro")
        held = Macro(id="held", name="Held macro", playback="while_held")
        self.window.draft.macros = [once, held]
        self.window.draft.display.pages = [["macro", "copy", "paste", "undo"]]
        self.window._load_draft()
        selector = self.window.lcd_macro_controls[0][0]

        self.assertGreaterEqual(selector.findData(once.id), 0)
        self.assertEqual(selector.findData(held.id), -1)

        self.window.draft.display.macro_bindings = [[held.id, None, None, None]]
        self.window._load_lcd_pages()
        held_index = selector.findData(held.id)
        self.assertGreaterEqual(held_index, 0)
        self.assertIn("While held unavailable", selector.itemText(held_index))
        self.assertFalse(selector.model().item(held_index).isEnabled())

        selector.setCurrentIndex(selector.findData(once.id))
        self.assertEqual(self.window.draft.display.macro_bindings[0][0], once.id)
        self.window.draft.validate()

    def test_lcd_macro_reference_survives_neighbor_edit_and_blocks_deletion(self):
        macro = Macro(id="lcd_macro", name="LCD macro")
        self.window.draft.macros = [macro]
        self.window.draft.display.pages = [["macro", "copy", "paste", "undo"]]
        self.window.draft.display.macro_bindings = [[macro.id, None, None, None]]
        self.window._load_draft()
        neighbor = self.window.lcd_controls[0][1]
        neighbor.setCurrentIndex(neighbor.findData("play_pause"))
        self.assertEqual(self.window.draft.display.macro_bindings[0][0], macro.id)
        self.window._delete_macro()
        self.assertEqual(self.window.draft.macros, [macro])
        self.assertIn("LCD tile", self.window.message.text())
        self.window.draft.validate()

    def test_lcd_neighbor_edit_retains_opaque_key_binding(self):
        opaque = "device:00112233445566778899aa"
        self.window.draft.display.pages = [["remap_key", "copy", "paste", "undo"]]
        self.window.draft.display.key_bindings = [[opaque, None, None, None]]
        self.window._load_draft()
        self.assertEqual(self.window.lcd_key_controls[0][0].text(), "")
        self.assertIn("preserved", self.window.lcd_key_controls[0][0].placeholderText())
        neighbor = self.window.lcd_controls[0][1]
        neighbor.setCurrentIndex(neighbor.findData("play_pause"))
        self.assertEqual(self.window.draft.display.key_bindings[0][0], opaque)
        self.window.draft.validate()

    def test_lcd_read_keeps_existing_windows_tiles_and_wide_continuations(self):
        self.service.verified_fields.append("display.pages")
        self.service.configuration.display.pages = [["game_bar", "system_media", None, None], ["cut", "copy", "paste", "undo"]]
        self.read()
        self.assertIn("Windows", self.window.lcd_controls[0][0].currentText())
        self.assertIn("preserved", self.window.lcd_controls[0][0].currentText())
        self.assertEqual(self.window.lcd_controls[0][1].currentData(), "system_media")
        self.assertFalse(self.window.lcd_controls[0][2].isEnabled())
        self.assertEqual(self.window.lcd_controls[0][3].currentText(), "Covered by slot 2")
        self.assertFalse(self.window.lcd_tabs.isTabEnabled(2))
        self.assertEqual(self.window.lcd_controls[1][1].findData("game_bar"), -1)
        self.assertEqual(self.window.lcd_controls[1][1].findData("polling_rate"), -1)

    def test_lcd_wide_replacement_and_shrink_release_covered_slots(self):
        self.window.draft.display.pages = [["dpi", "system_media", None, None]]
        self.window._load_draft()
        first = self.window.lcd_controls[0][0]
        first.setCurrentIndex(first.findData("system_media"))
        self.assertEqual(self.window.draft.display.pages, [["system_media", None, None, "empty"]])
        self.window.draft.validate()

    def test_general_media_is_offered_as_a_static_three_slot_tile(self):
        self.window.draft.display.pages = [["empty"] * 4]
        self.window._load_draft()
        first = self.window.lcd_controls[0][0]
        self.assertGreaterEqual(first.findData("general_media"), 0)
        self.assertGreaterEqual(first.findData("system_media"), 0)
        self.assertGreaterEqual(
            self.window.lcd_controls[0][1].findData("general_media"), 0)
        self.assertEqual(
            self.window.lcd_controls[0][2].findData("general_media"), -1)
        first.setCurrentIndex(first.findData("general_media"))
        self.assertEqual(
            self.window.draft.display.pages,
            [["general_media", None, None, "empty"]],
        )
        self.assertIn("General media controls", first.currentText())
        self.assertIn("3 slots", first.currentText())
        self.assertFalse(self.window.lcd_controls[0][1].isEnabled())
        self.assertFalse(self.window.lcd_controls[0][2].isEnabled())
        self.assertEqual(self.window.lcd_controls[0][2].findData("general_media"), -1)
        self.assertFalse(self.window.lcd_host_action_editors[0][0].isVisible())
        self.window.draft.validate()
        first.setCurrentIndex(first.findData("dpi"))
        self.assertEqual(self.window.draft.display.pages, [["dpi", "empty", "empty", "empty"]])
        self.assertTrue(self.window.lcd_controls[0][1].isEnabled())
        self.assertEqual(self.window.lcd_controls[0][2].findData("system_media"), -1)
        self.assertTrue(self.window.dirty)
        self.assertEqual(self.service.calls, [])
        self.window.draft.validate()

    def test_lcd_unknown_tile_survives_neighbor_edit_and_preset_round_trip(self):
        self.window.draft.display.pages = [["unknown_72_03", "copy", "paste", "undo"]]
        self.window._load_draft()
        self.assertEqual(self.window.lcd_controls[0][0].currentText(), "On-device widget (preserved)")
        control = self.window.lcd_controls[0][1]
        control.setCurrentIndex(control.findData("play_pause"))
        self.assertEqual(self.window.draft.display.pages[0][0], "unknown_72_03")
        self.assertTrue(self.window.save_preset())
        self.assertEqual(self.store.list()[0].display.pages, self.window.draft.display.pages)

    def test_lcd_page_apply_sends_edited_layout_only_on_explicit_apply(self):
        self.service.verified_fields.append("display.pages")
        self.service.configuration.display.pages = [["cut", "copy", "paste", "undo"]]
        self.read()
        self.window.navigation.setCurrentRow(4)
        control = self.window.lcd_controls[0][2]
        control.setCurrentIndex(control.findData("next_track"))
        self.assertEqual(self.service.calls[-1][0], "read")
        self.window.apply_to_mouse()
        self.wait_for_job()
        self.assertEqual(self.service.calls[-1][:3], ("apply_section", "test-mc7", "display"))
        self.assertEqual(self.service.configuration.display.pages[0][2], "next_track")

    def test_partial_read_identifies_missing_settings_without_overwriting_draft(self):
        snapshot = self.service.snapshot()
        snapshot["errors"] = {"lcd": "fixture failed"}
        self.window.draft.display.pages = [["cut", "copy", "paste", "undo"]]
        self.window._mouse_read(snapshot)
        self.assertIn("Could not read: LCD pages", self.window.message.text())
        self.assertEqual(self.window.draft.display.pages[0][0], "cut")

    def test_profile_activation_is_explicit(self):
        self.discover()
        self.window.profile_slot.setValue(3)
        self.assertEqual(self.service.calls, ["discover"])
        self.window.activate_profile()
        self.wait_for_job()
        self.assertEqual(self.service.calls[-1], ("switch_profile", "test-mc7", 3))
        self.assertEqual(self.window.snapshot["profile_slot"], 3)

    def test_each_apply_capability_is_respected(self):
        self.service.capabilities = ["read_settings", "apply_lighting"]
        self.read()
        self.assertFalse(self.window.apply_button.isEnabled())
        self.window.navigation.setCurrentRow(3)
        self.assertTrue(self.window.apply_button.isEnabled())
        self.assertFalse(self.window.activate_display_button.isEnabled())
        self.assertFalse(self.window.activate_profile_button.isEnabled())

    def test_device_status_reports_received_values_and_unknown_values_remain_unknown(self):
        self.service.status = {"firmware_version": "4.5", "battery_percent": 63, "charging": True}
        self.read()
        self.assertEqual(self.window.overview_battery.text(), "Battery: 63% · charging")
        self.assertIn("Firmware: 4.5", self.window.device_status.text())
        self.service.status = {"firmware_version": "4.5", "battery_percent": None, "charging": None}
        self.window.read_mouse()
        self.wait_for_job()
        self.assertEqual(self.window.overview_battery.text(), "Battery: not reported")
        self.assertIn("Charging: not reported", self.window.device_status.text())

    def test_background_upload_requires_dialog_acceptance_and_reports_acknowledgement(self):
        self.service.capabilities.append("upload_background")
        self.discover()
        payload = bytes(86336)
        with patch("swarm2.gui.app.BackgroundDialog") as factory:
            dialog = factory.return_value
            dialog.exec.return_value = QDialog.DialogCode.Rejected
            self.window.choose_background()
            self.assertEqual(self.service.calls, ["discover"])
            dialog.exec.return_value = QDialog.DialogCode.Accepted
            dialog.prepared.rgba = payload
            self.window.choose_background()
            factory.assert_called_with(self.window, can_upload=True)
        self.wait_for_job()
        self.assertEqual(self.service.calls[-1], ("upload_background", "test-mc7", payload))
        self.assertIn("acknowledged", self.window.message.text())
        self.assertIn("pixels cannot be read back", self.window.message.text())
        self.assertFalse(self.window.dirty)

    def test_background_upload_failure_invalidates_baseline(self):
        self.service.capabilities.append("upload_background")
        self.read()
        self.service.failure = "Incomplete background transfer"
        with patch("swarm2.gui.app.BackgroundDialog") as factory:
            factory.return_value.exec.return_value = QDialog.DialogCode.Accepted
            factory.return_value.prepared.rgba = bytes(86336)
            self.window.choose_background()
        self.wait_for_job()
        self.assertIsNone(self.window.snapshot)
        self.assertEqual(self.window.message.objectName(), "error")
        self.assertIn("Incomplete background transfer", self.window.message.text())


if __name__ == "__main__":
    unittest.main()
