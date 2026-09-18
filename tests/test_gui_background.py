"""Bounded Qt image conversion and explicit upload preparation; no hardware."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtWidgets import QApplication, QDialog, QFileDialog
    from swarm2.gui.background import BackgroundDialog, MAX_IMAGE_BYTES, prepare_background
except ModuleNotFoundError as error:
    if error.name and error.name.startswith("PySide6"):
        BackgroundDialog = None
    else:
        raise


@unittest.skipIf(BackgroundDialog is None, "Install the gui extra to exercise Qt")
class GuiBackgroundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "background.png"
        self.image = QImage(284, 76, QImage.Format.Format_RGBA8888)
        self.image.fill(QColor(10, 20, 30, 255))

    def tearDown(self):
        self.directory.cleanup()

    def save(self):
        self.assertTrue(self.image.save(str(self.path)))

    def test_rgba_payload_has_exact_geometry_channel_order_and_rotation(self):
        self.image.setPixelColor(283, 0, QColor(1, 2, 3, 4))
        self.image.setPixelColor(0, 75, QColor(50, 60, 70, 80))
        self.save()
        prepared = prepare_background(self.path)
        self.assertEqual(prepared.preview.size(), QSize(284, 76))
        self.assertEqual(len(prepared.rgba), 86_336)
        self.assertEqual(prepared.rgba[:4], bytes((1, 2, 3, 4)))
        self.assertEqual(prepared.rgba[-4:], bytes((50, 60, 70, 80)))
        self.assertEqual(prepared.preview.pixelColor(283, 0), QColor(1, 2, 3, 4))

    def test_center_crop_retains_middle_of_tall_source(self):
        self.image = QImage(284, 152, QImage.Format.Format_RGBA8888)
        self.image.fill(QColor("red"))
        for y in range(38, 114):
            for x in range(284):
                self.image.setPixelColor(x, y, QColor("green"))
        self.save()
        prepared = prepare_background(self.path)
        self.assertEqual(prepared.preview.pixelColor(0, 0), QColor("green"))
        self.assertEqual(prepared.preview.pixelColor(283, 75), QColor("green"))

    def test_jpeg_and_tiny_images_scale_to_display_size(self):
        self.path = self.path.with_suffix(".jpg")
        self.image = QImage(2, 2, QImage.Format.Format_RGB888)
        self.image.fill(QColor(0, 255, 0))
        self.save()
        prepared = prepare_background(self.path)
        self.assertEqual(prepared.preview.size(), QSize(284, 76))
        self.assertGreater(prepared.preview.pixelColor(150, 40).green(), 240)

    def test_extreme_aspect_ratio_is_cropped_before_expanding(self):
        self.image = QImage(1, 10000, QImage.Format.Format_RGBA8888)
        self.image.fill(QColor("red"))
        self.image.setPixelColor(0, 4999, QColor("blue"))
        self.save()
        prepared = prepare_background(self.path)
        self.assertEqual(prepared.preview.pixelColor(140, 38), QColor("blue"))
        self.assertEqual(len(prepared.rgba), 86_336)

    def test_non_image_and_oversized_files_are_rejected(self):
        self.path.write_text("not an image")
        with self.assertRaisesRegex(ValueError, "PNG or JPEG"):
            prepare_background(self.path)
        with self.path.open("wb") as output:
            output.truncate(MAX_IMAGE_BYTES + 1)
        with self.assertRaisesRegex(ValueError, "16 MiB"):
            prepare_background(self.path)

    def test_pixel_limit_is_checked_before_decoding(self):
        self.save()
        with patch("swarm2.gui.background.QImageReader") as factory:
            reader = factory.return_value
            reader.format.return_value = b"png"
            reader.size.return_value = QSize(4001, 4000)
            with self.assertRaisesRegex(ValueError, "16 million"):
                prepare_background(self.path)
            reader.read.assert_not_called()

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO requires POSIX")
    def test_non_regular_file_is_rejected_without_waiting(self):
        os.mkfifo(self.path)
        with self.assertRaisesRegex(ValueError, "regular"):
            prepare_background(self.path)

    def test_offline_preview_is_available_but_cannot_accept_upload(self):
        self.save()
        dialog = BackgroundDialog(can_upload=False)
        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(self.path), "")):
            dialog.choose_image()
        self.assertIsNotNone(dialog.prepared)
        self.assertFalse(dialog.upload_button.isEnabled())
        dialog.accept()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        dialog.close()

    def test_selection_needs_explicit_upload_and_failure_clears_previous_image(self):
        self.save()
        dialog = BackgroundDialog(can_upload=True)
        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(self.path), "")):
            dialog.choose_image()
        self.assertTrue(dialog.upload_button.isEnabled())
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        self.path.write_text("broken replacement")
        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(self.path), "")):
            dialog.choose_image()
        self.assertIsNone(dialog.prepared)
        self.assertFalse(dialog.upload_button.isEnabled())
        dialog.close()


if __name__ == "__main__":
    unittest.main()
