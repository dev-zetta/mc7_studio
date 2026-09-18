"""Focused recording behavior, timing, and bounded balanced output."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QDialog, QLineEdit
    from swarm2.gui.recording import MAX_RECORDING_MS, RecordingDialog, RecordingSession, key_name
except ModuleNotFoundError as error:
    if error.name and error.name.startswith("PySide6"):
        RecordingDialog = None
    else:
        raise

from swarm2.configuration import Configuration, Macro


class Clock:
    def __init__(self):
        self.seconds = 0.0

    def __call__(self):
        return self.seconds

    def advance(self, milliseconds):
        self.seconds += milliseconds / 1000


@unittest.skipIf(RecordingDialog is None, "Install the gui extra to exercise Qt")
class RecordingSessionTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.session = RecordingSession(clock=self.clock)
        self.session.start()

    def validate(self):
        Configuration(macros=[Macro(events=self.session.events)]).validate()

    def test_modifier_and_key_timing_ignore_initial_reaction_time(self):
        self.clock.advance(500)
        self.session.capture("key", "Ctrl", True)
        self.clock.advance(30)
        self.session.capture("key", "S", True)
        self.clock.advance(45)
        self.session.capture("key", "S", False)
        self.clock.advance(10)
        self.session.capture("key", "Ctrl", False)
        self.session.stop()
        self.assertEqual([event.delay_ms for event in self.session.events], [30, 45, 10, 0])
        self.validate()

    def test_duplicate_down_and_unmatched_release_are_ignored(self):
        self.session.capture("key", "B", False)
        self.session.capture("key", "A", True)
        self.session.capture("key", "A", True)
        self.session.capture("key", "A", False)
        self.session.stop()
        self.assertEqual(len(self.session.events), 2)
        self.validate()

    def test_stop_releases_held_inputs_in_reverse_order(self):
        self.session.capture("key", "Ctrl", True)
        self.session.capture("mouse", "left", True)
        self.clock.advance(120)
        self.session.stop()
        self.assertEqual([(e.kind, e.value, e.delay_ms) for e in self.session.events[-2:]], [("mouse_up", "left", 0), ("key_up", "Ctrl", 0)])
        self.assertEqual(self.session.events[1].delay_ms, 120)
        self.assertIn("held keys", self.session.reason)
        self.validate()

    def test_event_limit_always_reserves_releases(self):
        self.session = RecordingSession(max_events=4, clock=self.clock)
        self.session.start()
        self.session.capture("key", "Ctrl", True)
        self.session.capture("key", "A", True)
        self.assertTrue(self.session.active)
        self.clock.advance(20)
        self.session.capture("key", "B", True)
        self.assertFalse(self.session.active)
        self.assertEqual(len(self.session.events), 4)
        self.assertNotIn("B", [event.value for event in self.session.events])
        self.validate()

    def test_two_event_capacity_retains_actual_key_hold_duration(self):
        self.session = RecordingSession(max_events=2, clock=self.clock)
        self.session.start()
        self.session.capture("key", "A", True)
        self.clock.advance(150)
        self.session.capture("key", "A", False)
        self.assertEqual(self.session.events[0].delay_ms, 150)
        self.assertFalse(self.session.active)
        self.validate()

    def test_timer_limit_releases_keys_and_rejects_later_input(self):
        self.session.capture("key", "A", True)
        self.clock.advance(MAX_RECORDING_MS + 500)
        self.session.tick()
        self.session.capture("key", "B", True)
        self.assertFalse(self.session.active)
        self.assertEqual(self.session.events[0].delay_ms, MAX_RECORDING_MS)
        self.assertEqual(len(self.session.events), 2)
        self.validate()

    def test_recorded_hold_duration_matches_native_bridge_after_event_contract(self):
        from swarm2.macro_profiles import macro_to_keyboard_events
        self.session.capture("key", "A", True)
        self.clock.advance(175)
        self.session.capture("key", "A", False)
        self.session.stop()
        events = macro_to_keyboard_events(Macro(events=self.session.events))
        self.assertTrue(events[0].pressed)
        self.assertEqual(events[0].delay_after_ticks, 175)
        self.assertFalse(events[1].pressed)
        self.assertEqual(events[1].delay_after_ticks, 0)

    def test_platform_modifiers_and_keypad_names(self):
        self.assertEqual(key_name(Qt.Key.Key_Control, platform="darwin"), "Cmd")
        self.assertEqual(key_name(Qt.Key.Key_Meta, platform="darwin"), "Ctrl")
        self.assertEqual(key_name(Qt.Key.Key_Meta, platform="darwin", mac_swap=False), "Cmd")
        self.assertEqual(key_name(Qt.Key.Key_Control, platform="linux"), "Ctrl")
        self.assertEqual(key_name(Qt.Key.Key_3, Qt.KeyboardModifier.KeypadModifier), "Num3")
        self.assertIsNone(key_name(Qt.Key.Key_unknown))


@unittest.skipIf(RecordingDialog is None, "Install the gui extra to exercise Qt")
class RecordingDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.clock = Clock()
        self.dialog = RecordingDialog(clock=self.clock)
        self.dialog.show()
        self.dialog.activateWindow()
        self.app.processEvents()

    def tearDown(self):
        self.dialog.close()
        self.dialog.deleteLater()
        self.app.processEvents()

    def key(self, key, down=True, *, repeat=False):
        event = QKeyEvent(QEvent.Type.KeyPress if down else QEvent.Type.KeyRelease, key, Qt.KeyboardModifier.NoModifier, "", repeat)
        self.app.sendEvent(self.dialog.area, event)

    def test_only_capture_area_events_are_recorded_and_start_click_is_excluded(self):
        QTest.mouseClick(self.dialog.record_button, Qt.MouseButton.LeftButton)
        self.assertEqual(self.dialog.session.events, [])
        self.key(Qt.Key.Key_A)
        self.clock.advance(40)
        self.key(Qt.Key.Key_A, False)
        outside = QLineEdit()
        QTest.keyClick(outside, Qt.Key.Key_B)
        self.assertEqual(outside.text(), "b")
        self.assertEqual([e.value for e in self.dialog.session.events], ["A", "A"])
        self.dialog.stop()
        self.dialog.accept()
        self.assertEqual(self.dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(self.dialog.recorded_events[0].delay_ms, 40)

    def test_tab_return_and_shortcuts_are_captured_without_activating_dialog_actions(self):
        self.dialog.start()
        event = QKeyEvent(QEvent.Type.ShortcutOverride, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
        event.ignore()
        self.app.sendEvent(self.dialog.area, event)
        self.assertTrue(event.isAccepted())
        for key in (Qt.Key.Key_Tab, Qt.Key.Key_Return):
            self.key(key)
            self.key(key, False)
        self.assertTrue(self.dialog.session.active)
        self.assertEqual([e.value for e in self.dialog.session.events], ["Tab", "Tab", "Return", "Return"])
        self.assertEqual(self.dialog.result(), QDialog.DialogCode.Rejected)

    def test_auto_repeat_is_ignored_and_f8_stops_without_recording_it(self):
        self.dialog.start()
        self.key(Qt.Key.Key_A)
        self.key(Qt.Key.Key_A, False, repeat=True)
        self.key(Qt.Key.Key_A, repeat=True)
        self.clock.advance(200)
        self.key(Qt.Key.Key_F8)
        self.assertFalse(self.dialog.session.active)
        self.assertEqual([e.value for e in self.dialog.session.events], ["A", "A"])
        self.assertEqual(self.dialog.session.events[0].delay_ms, 200)

    def test_clicks_inside_area_record_mouse_events(self):
        self.dialog.start()
        QTest.mouseClick(self.dialog.area, Qt.MouseButton.RightButton)
        self.dialog.stop()
        self.assertEqual([(e.kind, e.value) for e in self.dialog.session.events], [("mouse_down", "right"), ("mouse_up", "right")])

    def test_side_clicks_inside_area_record_uploadable_mouse_events(self):
        from swarm2.macro_profiles import macro_to_keyboard_events

        self.dialog.start()
        QTest.mouseClick(self.dialog.area, Qt.MouseButton.BackButton)
        QTest.mouseClick(self.dialog.area, Qt.MouseButton.ForwardButton)
        self.dialog.stop()
        self.assertEqual([(e.kind, e.value) for e in self.dialog.session.events], [
            ("mouse_down", "back"), ("mouse_up", "back"),
            ("mouse_down", "forward"), ("mouse_up", "forward"),
        ])
        events = macro_to_keyboard_events(Macro(events=self.dialog.session.events))
        self.assertEqual([event.key_code for event in events], [0xF4, 0xF4, 0xF3, 0xF3])

    def test_focus_loss_stops_and_balances_keys(self):
        self.dialog.start()
        self.key(Qt.Key.Key_Shift)
        self.app.sendEvent(self.dialog, QEvent(QEvent.Type.WindowDeactivate))
        self.assertFalse(self.dialog.session.active)
        self.assertEqual(self.dialog.session.events[-1].kind, "key_up")
        Configuration(macros=[Macro(events=self.dialog.session.events)]).validate()

    def test_escape_cancels_and_never_exposes_recorded_results(self):
        self.dialog.start()
        self.key(Qt.Key.Key_A)
        self.key(Qt.Key.Key_Escape)
        self.assertFalse(self.dialog.session.active)
        self.assertEqual(self.dialog.recorded_events, [])
        self.assertEqual(self.dialog.result(), QDialog.DialogCode.Rejected)

    def test_unknown_key_is_reported_and_record_again_resets_take(self):
        self.dialog.start()
        self.key(Qt.Key.Key_unknown)
        self.assertEqual(self.dialog.skipped_inputs, 1)
        self.assertFalse(self.dialog.warning.isHidden())
        self.key(Qt.Key.Key_A)
        self.dialog.stop()
        self.dialog.start()
        self.assertEqual(self.dialog.session.events, [])
        self.assertEqual(self.dialog.preview.count(), 0)
        self.assertTrue(self.dialog.warning.isHidden())


if __name__ == "__main__":
    unittest.main()
