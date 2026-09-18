"""Exercise the local automatic-profile editor without opening a device."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication, QDialog
    from swarm2.gui.automatic_profiles import AutomaticProfilesDialog
except ModuleNotFoundError as error:
    if error.name and error.name.startswith("PySide6"):
        AutomaticProfilesDialog = None
    else:
        raise

from swarm2.automatic_profiles import (
    ApplicationProfileRule,
    AutomaticProfileSettings,
)


def rule(
    name: str,
    slot: int,
    value: str,
    *,
    kind: str = "executable_path",
    enabled: bool = True,
    rule_id: str,
) -> ApplicationProfileRule:
    return ApplicationProfileRule(
        name=name,
        profile_slot=slot,
        match_kind=kind,
        match_value=value,
        enabled=enabled,
        rule_id=rule_id,
    )


@unittest.skipIf(
    AutomaticProfilesDialog is None, "Install the gui extra to exercise Qt"
)
class AutomaticProfilesDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.initial = AutomaticProfileSettings(
            enabled=True,
            default_profile_slot=2,
            rules=(
                rule("Game", 3, "/opt/games/game", rule_id="game"),
                rule(
                    "Editor",
                    4,
                    "com.example.editor",
                    kind="bundle_id",
                    enabled=False,
                    rule_id="editor",
                ),
            ),
        )
        self.dialog = AutomaticProfilesDialog(self.initial)

    def tearDown(self):
        self.dialog.close()
        self.dialog.deleteLater()
        self.app.processEvents()

    def test_loads_global_settings_and_first_ordered_rule(self):
        self.assertTrue(self.dialog.enabled_checkbox.isChecked())
        self.assertEqual(self.dialog.default_profile_spin.value(), 2)
        self.assertEqual(self.dialog.rule_list.count(), 2)
        self.assertEqual(self.dialog.rule_list.currentRow(), 0)
        self.assertEqual(self.dialog.rule_name_edit.text(), "Game")
        self.assertEqual(self.dialog.rule_profile_spin.value(), 3)
        self.assertEqual(self.dialog.identity_value_edit.text(), "/opt/games/game")
        self.assertTrue(self.dialog.identity_value_edit.isReadOnly())
        self.assertEqual(self.dialog.settings(), self.initial.normalized())

    def test_edits_name_enabled_state_and_profile_without_changing_identity(self):
        self.dialog.rule_list.setCurrentRow(1)
        self.dialog.rule_name_edit.setText("Writing app")
        self.dialog.rule_enabled_checkbox.setChecked(True)
        self.dialog.rule_profile_spin.setValue(5)

        result = self.dialog.settings()
        edited = result.rules[1]
        self.assertEqual(edited.name, "Writing app")
        self.assertTrue(edited.enabled)
        self.assertEqual(edited.profile_slot, 5)
        self.assertEqual(edited.rule_id, "editor")
        self.assertEqual(edited.match_kind, "bundle_id")
        self.assertEqual(edited.match_value, "com.example.editor")

    def test_move_and_remove_preserve_explicit_priority(self):
        self.dialog.rule_list.setCurrentRow(1)
        self.assertTrue(self.dialog.move_selected(-1))
        self.assertEqual(
            [item.rule_id for item in self.dialog.settings().rules],
            ["editor", "game"],
        )
        self.assertEqual(self.dialog.rule_list.currentRow(), 0)
        self.assertFalse(self.dialog.move_selected(-1))

        self.assertTrue(self.dialog.remove_selected())
        self.assertEqual(
            [item.rule_id for item in self.dialog.settings().rules], ["game"]
        )
        self.assertEqual(self.dialog.rule_list.currentRow(), 0)

    def test_executable_chooser_stores_the_resolved_exact_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "real-program"
            executable.write_bytes(b"test")
            alias = root / "program-link"
            alias.symlink_to(executable)
            self.dialog.default_profile_spin.setValue(5)
            with patch(
                "swarm2.gui.automatic_profiles.QFileDialog.getOpenFileName",
                return_value=(str(alias), ""),
            ):
                self.assertTrue(self.dialog.add_executable())

        added = self.dialog.settings().rules[-1]
        self.assertEqual(added.match_kind, "executable_path")
        expected = executable.resolve().as_posix()
        self.assertEqual(added.match_value, expected.casefold() if os.name == "nt" else expected)
        self.assertEqual(added.profile_slot, 5)
        self.assertEqual(added.name, executable.name)
        self.assertEqual(self.dialog.rule_list.currentRow(), 2)

    def test_bundle_prompt_normalizes_case_and_rejects_duplicate_identity(self):
        with patch(
            "swarm2.gui.automatic_profiles.QInputDialog.getText",
            return_value=("Com.Example.GameTool", True),
        ):
            self.assertTrue(self.dialog.add_bundle_id())

        added = self.dialog.settings().rules[-1]
        self.assertEqual(added.match_kind, "bundle_id")
        self.assertEqual(added.match_value, "com.example.gametool")
        self.assertEqual(added.name, "GameTool")
        count = self.dialog.rule_list.count()
        self.assertFalse(self.dialog.add_bundle_id("com.example.gametool"))
        self.assertEqual(self.dialog.rule_list.count(), count)
        self.assertIn("already has", self.dialog.error_label.text())
        self.assertFalse(self.dialog.error_label.isHidden())

    def test_cancelled_choosers_do_not_add_rules(self):
        with patch(
            "swarm2.gui.automatic_profiles.QFileDialog.getOpenFileName",
            return_value=("", ""),
        ):
            self.assertFalse(self.dialog.add_executable())
        with patch(
            "swarm2.gui.automatic_profiles.QInputDialog.getText",
            return_value=("", False),
        ):
            self.assertFalse(self.dialog.add_bundle_id())
        self.assertEqual(self.dialog.rule_list.count(), 2)

    def test_save_validates_and_exposes_an_immutable_result(self):
        self.dialog.rule_name_edit.clear()
        self.dialog.accept()
        self.assertEqual(self.dialog.result(), QDialog.DialogCode.Rejected)
        self.assertIsNone(self.dialog.result_settings)
        self.assertIn("nonempty", self.dialog.error_label.text())
        self.assertFalse(self.dialog.error_label.isHidden())

        self.dialog.rule_name_edit.setText("Game restored")
        self.dialog.enabled_checkbox.setChecked(False)
        self.dialog.save_button.click()
        self.assertEqual(self.dialog.result(), QDialog.DialogCode.Accepted)
        self.assertIsNotNone(self.dialog.result_settings)
        self.assertFalse(self.dialog.result_settings.enabled)
        self.assertEqual(self.dialog.result_settings.rules[0].name, "Game restored")

    def test_invalid_bundle_id_is_reported_inline(self):
        self.assertFalse(self.dialog.add_bundle_id("not a bundle id"))
        self.assertIn("reverse-domain identifier", self.dialog.error_label.text())
        self.assertEqual(self.dialog.rule_list.count(), 2)


if __name__ == "__main__":
    unittest.main()
