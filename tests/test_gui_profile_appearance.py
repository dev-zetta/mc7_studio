"""Portable profile-library appearance; no device I/O."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtWidgets import QApplication, QFileDialog
    from swarm2.gui.app import MainWindow
    from swarm2.gui.profile_appearance import (
        MAX_SOURCE_IMAGE_BYTES, PROFILE_THUMBNAIL_SIZE, decode_profile_image,
        prepare_profile_image,
    )
except ModuleNotFoundError as error:
    if error.name and error.name.startswith("PySide6"):
        MainWindow = None
    else:
        raise

from swarm2.configuration import Configuration, PresetStore, profile_image_bytes
from tests.test_gui import FakeService


@unittest.skipIf(MainWindow is None, "Install the gui extra to exercise Qt")
class GuiProfileAppearanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.image_path = self.root / "profile.png"
        self.image = QImage(180, 90, QImage.Format.Format_RGBA8888)
        self.image.fill(QColor("red"))
        for x in range(45, 135):
            for y in range(90):
                self.image.setPixelColor(x, y, QColor("green"))
        self.assertTrue(self.image.save(str(self.image_path)))
        self.service = FakeService()
        self.store = PresetStore(self.root / "presets")
        self.window = MainWindow(
            service=self.service, store=self.store, auto_discover=False)

    def tearDown(self):
        self.window.dirty = False
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.temporary.cleanup()

    def test_source_is_cropped_normalized_and_embedded(self):
        prepared = prepare_profile_image(self.image_path)
        self.assertEqual(
            prepared.preview.size(),
            QSize(PROFILE_THUMBNAIL_SIZE, PROFILE_THUMBNAIL_SIZE),
        )
        self.assertEqual(prepared.preview.pixelColor(0, 45), QColor("green"))
        self.assertLess(len(profile_image_bytes(prepared.data_url)), 256 * 1024)
        self.assertEqual(
            decode_profile_image(prepared.data_url).size(), prepared.preview.size())

    def test_editor_save_duplicate_export_import_and_remove_preserve_appearance(self):
        with patch.object(
                QFileDialog, "getOpenFileName",
                return_value=(str(self.image_path), "")):
            self.window.choose_profile_image()
        self.window.profile_color.colorChanged.emit("#123abc")
        self.assertEqual(self.window.draft.appearance.color, "#123ABC")
        self.assertIsNotNone(self.window.draft.appearance.image)
        self.assertFalse(self.window.profile_image_preview.pixmap().isNull())
        self.assertTrue(self.window.dirty)
        self.assertEqual(self.service.calls, [])

        self.window.draft.name = "Visual preset"
        self.assertTrue(self.window.save_preset())
        saved = self.store.list()[0]
        self.assertEqual(saved.appearance, self.window.draft.appearance)
        self.assertFalse(self.window.preset_list.item(0).icon().isNull())
        self.assertIn("#123ABC", self.window.preset_list.item(0).toolTip())

        expected = saved.appearance
        self.window.duplicate_draft()
        self.assertEqual(self.window.draft.appearance, expected)
        exported = self.root / "visual.json"
        with patch.object(
                QFileDialog, "getSaveFileName",
                return_value=(str(exported), "")):
            self.window.export_preset()
        self.window.draft = Configuration()
        self.window.dirty = False
        with patch.object(
                QFileDialog, "getOpenFileName",
                return_value=(str(exported), "")):
            self.window.import_preset()
        self.assertEqual(self.window.draft.appearance, expected)
        self.assertFalse(self.window.profile_image_preview.pixmap().isNull())

        self.window.clear_profile_image()
        self.assertIsNone(self.window.draft.appearance.image)
        self.assertEqual(self.window.profile_image_preview.text(), "No image")
        self.assertFalse(self.window.clear_profile_image_button.isEnabled())

    def test_invalid_or_oversized_source_keeps_existing_draft(self):
        existing = prepare_profile_image(self.image_path).data_url
        self.window.draft.appearance.image = existing
        broken = self.root / "broken.png"
        broken.write_text("not an image")
        with patch.object(
                QFileDialog, "getOpenFileName", return_value=(str(broken), "")):
            self.window.choose_profile_image()
        self.assertEqual(self.window.draft.appearance.image, existing)
        self.assertEqual(self.window.message.objectName(), "error")

        with broken.open("wb") as output:
            output.truncate(MAX_SOURCE_IMAGE_BYTES + 1)
        with self.assertRaisesRegex(ValueError, "16 MiB"):
            prepare_profile_image(broken)


if __name__ == "__main__":
    unittest.main()
