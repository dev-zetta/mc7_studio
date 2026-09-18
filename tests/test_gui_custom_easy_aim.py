"""GUI coverage for the bounded custom Easy-Aim button picker."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication, QComboBox
    from swarm2.gui.app import CUSTOM_EASY_AIM_PICKER, MainWindow
except ModuleNotFoundError as error:
    if error.name and error.name.startswith("PySide6"):
        MainWindow = None
    else:
        raise

from swarm2.configuration import Action
from tests.test_gui import FakeService


@unittest.skipIf(MainWindow is None, "Install the gui extra to exercise Qt")
class CustomEasyAimGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = MainWindow(service=FakeService(), auto_discover=False)

    def tearDown(self):
        self.window.dirty = False
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def _combo(self):
        container = self.window.button_table.cellWidget(0, 1)
        combo = container.layout().itemAt(0).widget()
        self.assertIsInstance(combo, QComboBox)
        return combo

    @staticmethod
    def _index(combo, data):
        return next(index for index in range(combo.count())
                    if combo.itemData(index) == data)

    def test_custom_picker_prompts_with_bounds_normalizes_and_displays_value(self):
        combo = self._combo()
        picker = self._index(combo, CUSTOM_EASY_AIM_PICKER)
        self.assertEqual(combo.itemText(picker), "DPI · Easy-Aim custom…")

        with patch("swarm2.gui.app.QInputDialog.getInt", return_value=(825, True)) as prompt:
            combo.setCurrentIndex(picker)

        prompt.assert_called_once_with(
            self.window, "Custom Easy-Aim", "DPI (50–30,000, in steps of 50):",
            1600, 50, 30000, 50,
        )
        self.assertEqual(
            self.window.draft.buttons[0].primary,
            Action("dpi", "precision_custom_850"),
        )
        combo = self._combo()
        self.assertEqual(combo.currentData(), ("dpi", "precision_custom_850"))
        self.assertEqual(combo.currentText(), "DPI · Easy-Aim 850 DPI")
        self.assertTrue(self.window.dirty)

    def test_existing_custom_value_survives_reload_and_cancelled_repick(self):
        binding = self.window.draft.buttons[0]
        binding.primary = Action("dpi", "precision_custom_1250")
        self.window._load_buttons()
        combo = self._combo()
        self.assertEqual(combo.currentData(), ("dpi", "precision_custom_1250"))
        self.assertEqual(combo.currentText(), "DPI · Easy-Aim 1,250 DPI")

        with patch("swarm2.gui.app.QInputDialog.getInt", return_value=(5000, False)) as prompt:
            combo.setCurrentIndex(self._index(combo, CUSTOM_EASY_AIM_PICKER))

        self.assertEqual(binding.primary, Action("dpi", "precision_custom_1250"))
        prompt.assert_called_once_with(
            self.window, "Custom Easy-Aim", "DPI (50–30,000, in steps of 50):",
            1250, 50, 30000, 50,
        )
        combo = self._combo()
        self.assertEqual(combo.currentData(), ("dpi", "precision_custom_1250"))
        self.assertEqual(combo.currentText(), "DPI · Easy-Aim 1,250 DPI")


if __name__ == "__main__":
    unittest.main()
