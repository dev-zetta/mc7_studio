"""Offline Qt tests for application-icon preparation."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QColor, QIcon, QImage, QPixmap
    from PySide6.QtWidgets import QApplication
    from swarm2.gui.custom_icon import (
        ICON_CANVAS_HEIGHT, ICON_CANVAS_WIDTH, ICON_WIRE_RGBA_BYTES,
        prepare_application_icon, prepare_custom_icon, preview_from_wire_rgba,
    )
except ModuleNotFoundError as error:
    if error.name and error.name.startswith("PySide6"):
        prepare_custom_icon = None
    else:
        raise


@unittest.skipIf(prepare_custom_icon is None, "Install the gui extra to exercise Qt")
class GuiCustomIconTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "icon.png"

    def tearDown(self):
        self.directory.cleanup()

    def test_custom_image_is_contained_centered_and_rotated_for_wire(self):
        source = QImage(100, 50, QImage.Format.Format_RGBA8888)
        source.fill(QColor(20, 40, 60, 255))
        self.assertTrue(source.save(str(self.path)))
        prepared = prepare_custom_icon(self.path)
        self.assertEqual(
            prepared.preview.size(), QSize(ICON_CANVAS_WIDTH, ICON_CANVAS_HEIGHT))
        self.assertEqual(len(prepared.rgba), ICON_WIRE_RGBA_BYTES)
        self.assertEqual(prepared.preview.pixelColor(0, 0).alpha(), 0)
        self.assertEqual(prepared.preview.pixelColor(32, 31), QColor(20, 40, 60, 255))
        self.assertEqual(preview_from_wire_rgba(prepared.rgba), prepared.preview)

    def test_tall_image_is_contained_without_center_crop(self):
        source = QImage(10, 100, QImage.Format.Format_RGBA8888)
        source.fill(QColor("red"))
        self.assertTrue(source.save(str(self.path)))
        prepared = prepare_custom_icon(self.path)
        self.assertEqual(prepared.preview.pixelColor(32, 31), QColor("red"))
        self.assertEqual(prepared.preview.pixelColor(20, 31).alpha(), 0)

    def test_non_image_and_oversized_geometry_are_rejected(self):
        self.path.write_text("not an image")
        with self.assertRaisesRegex(ValueError, "PNG or JPEG"):
            prepare_custom_icon(self.path)
        with patch("swarm2.gui.custom_icon.QImageReader") as factory:
            reader = factory.return_value
            reader.format.return_value = b"png"
            reader.size.return_value = QSize(4001, 4000)
            with self.assertRaisesRegex(ValueError, "16 million"):
                prepare_custom_icon(self.path)
            reader.read.assert_not_called()

    def test_application_icon_provider_is_used_without_starting_target(self):
        app_path = Path(self.directory.name) / "demo"
        app_path.write_bytes(b"#!/bin/sh\n")
        image = QImage(32, 32, QImage.Format.Format_RGBA8888)
        image.fill(QColor("blue"))
        pixmap = QPixmap.fromImage(image)
        with patch("swarm2.gui.custom_icon.QFileIconProvider") as provider:
            provider.return_value.icon.return_value = QIcon(pixmap)
            prepared = prepare_application_icon(app_path)
        self.assertEqual(prepared.filename, str(app_path))
        self.assertEqual(prepared.preview.pixelColor(32, 31), QColor("blue"))

    def test_wire_preview_rejects_wrong_payload(self):
        for value in (b"", bytes(ICON_WIRE_RGBA_BYTES - 1), bytearray(ICON_WIRE_RGBA_BYTES)):
            with self.subTest(value=type(value).__name__), self.assertRaises(ValueError):
                preview_from_wire_rgba(value)


if __name__ == "__main__":
    unittest.main()
