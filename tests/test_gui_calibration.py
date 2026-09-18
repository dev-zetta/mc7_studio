"""Synthetic local Qt pointer events; these never measure a physical mouse."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication, QDialog
    from swarm2.gui.calibration import AngleCalibrationDialog, DpiCalibrationDialog
except ModuleNotFoundError as error:
    if error.name and error.name.startswith("PySide6"):
        AngleCalibrationDialog = None
    else:
        raise


@unittest.skipIf(AngleCalibrationDialog is None, "Install the gui extra to exercise Qt")
class GuiCalibrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def display(self, dialog):
        dialog.show()
        self.app.processEvents()
        self.addCleanup(dialog.close)
        return dialog

    def pointer(self, canvas, point, *, click=False):
        kind = QEvent.Type.MouseButtonPress if click else QEvent.Type.MouseMove
        button = Qt.MouseButton.LeftButton if click else Qt.MouseButton.NoButton
        event = QMouseEvent(kind, QPointF(*point), QPointF(*point), button, button, Qt.KeyboardModifier.NoModifier)
        self.app.sendEvent(canvas, event)
        self.app.processEvents()

    def angle_run(self, dialog, count=10):
        self.app.processEvents()  # Apply notice/layout changes before measuring coordinates.
        canvas = dialog.canvas
        y = canvas.height()//2
        self.pointer(canvas, (canvas.MARGIN, y), click=True)
        for index in range(count):
            point = (canvas.width()-canvas.MARGIN, y+50) if index % 2 == 0 else (canvas.MARGIN, y)
            self.pointer(canvas, point)

    def test_angle_requires_verified_unrotated_unsnapped_state(self):
        for params in ({}, {"settings_verified": True, "angle_snapping": True},
                       {"settings_verified": True, "angle_enabled": True, "current_angle": 5}):
            with self.subTest(params=params):
                dialog = self.display(AngleCalibrationDialog(**params))
                self.assertFalse(dialog.start_button.isEnabled())
                dialog.start()
                dialog.accept()
                self.assertIsNone(dialog.suggested_angle)
                self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)

    def test_angle_ten_passes_only_stage_suggestion_after_explicit_accept(self):
        dialog = self.display(AngleCalibrationDialog(settings_verified=True))
        dialog.start()
        self.angle_run(dialog)
        self.assertTrue(dialog.accept_button.isEnabled())
        self.assertIsNone(dialog.suggested_angle)
        proposed = dialog._result.suggested_angle
        self.assertLess(proposed, 0)
        dialog.accept()
        self.assertEqual(dialog.suggested_angle, proposed)
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)

    def test_practice_cannot_be_accepted_and_restart_discards_old_result(self):
        dialog = self.display(AngleCalibrationDialog(settings_verified=True))
        dialog.start(practice=True)
        self.angle_run(dialog, count=3)
        self.assertFalse(dialog.accept_button.isEnabled())
        dialog.accept()
        self.assertIsNone(dialog.suggested_angle)
        dialog.start()
        self.angle_run(dialog)
        self.assertTrue(dialog.accept_button.isEnabled())
        dialog.start()
        self.assertFalse(dialog.accept_button.isEnabled())
        self.assertIsNone(dialog._result)

    def test_resize_during_angle_run_invalidates_session_and_cancel_clears_output(self):
        dialog = self.display(AngleCalibrationDialog(settings_verified=True))
        dialog.start()
        self.angle_run(dialog, count=1)
        dialog.resize(dialog.width()+100, dialog.height())
        self.app.processEvents()
        self.assertFalse(dialog.canvas._started)
        self.assertIn("changed size", dialog.notice.text())
        self.assertFalse(dialog.accept_button.isEnabled())
        dialog.reject()
        self.assertIsNone(dialog.suggested_angle)

    def test_dpi_requires_verified_read_and_pointer_motion_before_each_attempt(self):
        blocked = self.display(DpiCalibrationDialog(current_dpi=1000))
        self.assertFalse(blocked.start_button.isEnabled())
        blocked.accept()
        self.assertIsNone(blocked.suggested_dpi)
        dialog = self.display(DpiCalibrationDialog(current_dpi=1000, settings_verified=True))
        dialog.start()
        origin = dialog.canvas.origin()
        self.pointer(dialog.canvas, (origin.x(), origin.y()), click=True)
        self.pointer(dialog.canvas, dialog.canvas.target(), click=True)
        self.assertEqual(dialog.canvas.samples, [])
        self.assertFalse(dialog.accept_button.isEnabled())

    def test_dpi_exact_target_aim_returns_current_dpi_only_after_accept(self):
        dialog = self.display(DpiCalibrationDialog(current_dpi=1000, settings_verified=True))
        dialog.start()
        canvas = dialog.canvas
        origin = canvas.origin()
        previous = (origin.x(), origin.y())
        self.pointer(canvas, previous, click=True)
        for _ in range(5):
            self.pointer(canvas, (previous[0]+1, previous[1]))
            target = canvas.target()
            self.pointer(canvas, target, click=True)
            previous = target
        self.assertEqual(len(canvas.samples), 5)
        self.assertEqual(dialog._result.suggested_dpi, 1000)
        self.assertIsNone(dialog.suggested_dpi)
        self.assertTrue(dialog.accept_button.isEnabled())
        dialog.accept()
        self.assertEqual(dialog.suggested_dpi, 1000)
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)

    def test_dpi_zero_travel_does_not_advance_and_resize_cancels_run(self):
        dialog = self.display(DpiCalibrationDialog(current_dpi=1000, settings_verified=True))
        dialog.start()
        canvas = dialog.canvas
        origin = canvas.origin()
        point = (origin.x(), origin.y())
        self.pointer(canvas, point, click=True)
        self.pointer(canvas, point)
        self.pointer(canvas, point, click=True)
        self.assertEqual(canvas.samples, [])
        self.assertIn("32 pointer pixels", dialog.notice.text())
        dialog.resize(dialog.width()+100, dialog.height())
        self.app.processEvents()
        self.assertFalse(canvas._started)
        self.assertFalse(dialog.accept_button.isEnabled())
        dialog.reject()
        self.assertIsNone(dialog.suggested_dpi)


if __name__ == "__main__":
    unittest.main()
