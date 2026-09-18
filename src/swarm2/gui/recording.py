"""Bounded macro recording from one focused Qt widget; no global input hooks."""

from __future__ import annotations

import copy
import sys
import time
from collections.abc import Callable

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QListWidget,
    QPushButton, QVBoxLayout,
)

from ..configuration import Configuration, MAX_MACRO_EVENTS, Macro, MacroEvent
from .widgets import STYLE, label


MAX_RECORDING_MS = 60_000


class RecordingSession:
    """Timed input collection that reserves capacity for every pending release."""

    def __init__(self, *, max_events=MAX_MACRO_EVENTS, clock: Callable[[], float] = time.monotonic):
        if type(max_events) is not int or not 2 <= max_events <= MAX_MACRO_EVENTS:
            raise ValueError("Recording needs room for 2 to 1,000 events")
        self.max_events = max_events
        self.clock = clock
        self.events: list[MacroEvent] = []
        self.pressed: dict[tuple[str, str], str] = {}
        self.active = False
        self.reason = "Ready"
        self.started_at = 0.0
        self.last_event_ms = 0

    def start(self):
        self.events.clear()
        self.pressed.clear()
        self.started_at = self.clock()
        self.last_event_ms = 0
        self.reason = "Recording"
        self.active = True

    @property
    def elapsed_ms(self):
        return max(0, min(MAX_RECORDING_MS, round((self.clock() - self.started_at) * 1000))) if self.active else self.last_event_ms

    def tick(self):
        if self.active and self.elapsed_ms >= MAX_RECORDING_MS:
            self.stop("Stopped at the 60-second limit")

    def capture(self, category: str, value: str, down: bool):
        self.tick()
        if not self.active:
            return
        identity = (category, value.lower())
        # Qt auto-repeat is filtered by the widget. Also reject duplicate downs
        # and releases whose press happened outside this recording.
        if (down and identity in self.pressed) or (not down and identity not in self.pressed):
            return
        pending_count = len(self.pressed) + (1 if down else -1)
        if len(self.events) + 1 + pending_count > self.max_events:
            self.stop("Stopped at the event limit")
            return
        now = self.elapsed_ms
        if self.events:
            self.events[-1].delay_ms = now - self.last_event_ms
        self.events.append(MacroEvent(f"{category}_{'down' if down else 'up'}", value, 0))
        self.last_event_ms = now
        if down:
            self.pressed[identity] = value
        else:
            del self.pressed[identity]
        if len(self.events) >= self.max_events:
            self.stop("Stopped at the event limit")

    def stop(self, reason="Stopped"):
        if not self.active:
            return
        now = self.elapsed_ms
        released = bool(self.pressed)
        for (category, _), value in reversed(list(self.pressed.items())):
            self.events[-1].delay_ms = now - self.last_event_ms
            self.events.append(MacroEvent(f"{category}_up", value, 0))
            self.last_event_ms = now
        self.pressed.clear()
        self.active = False
        self.reason = reason + (" · held keys and buttons released in the recording" if released else "")


_KEYS = {
    Qt.Key.Key_Control: "Ctrl", Qt.Key.Key_Meta: "Meta", Qt.Key.Key_Alt: "Alt", Qt.Key.Key_Shift: "Shift",
    Qt.Key.Key_Return: "Return", Qt.Key.Key_Enter: "Enter", Qt.Key.Key_Space: "Space",
    Qt.Key.Key_Tab: "Tab", Qt.Key.Key_Backtab: "Tab", Qt.Key.Key_Backspace: "Backspace",
    Qt.Key.Key_Delete: "Delete", Qt.Key.Key_Insert: "Insert", Qt.Key.Key_Home: "Home", Qt.Key.Key_End: "End",
    Qt.Key.Key_PageUp: "PageUp", Qt.Key.Key_PageDown: "PageDown", Qt.Key.Key_Up: "Up", Qt.Key.Key_Down: "Down",
    Qt.Key.Key_Left: "Left", Qt.Key.Key_Right: "Right", Qt.Key.Key_CapsLock: "CapsLock",
    Qt.Key.Key_NumLock: "NumLock", Qt.Key.Key_ScrollLock: "ScrollLock", Qt.Key.Key_Print: "PrintScreen",
    Qt.Key.Key_Pause: "Pause", Qt.Key.Key_Menu: "Menu", Qt.Key.Key_Minus: "Minus", Qt.Key.Key_Equal: "Equal",
    Qt.Key.Key_Plus: "Plus", Qt.Key.Key_Comma: "Comma", Qt.Key.Key_Period: "Period", Qt.Key.Key_Slash: "Slash",
    Qt.Key.Key_Backslash: "Backslash", Qt.Key.Key_Semicolon: "Semicolon", Qt.Key.Key_Apostrophe: "Quote",
    Qt.Key.Key_BracketLeft: "BracketLeft", Qt.Key.Key_BracketRight: "BracketRight", Qt.Key.Key_QuoteLeft: "Grave",
}
_BUTTONS = {Qt.MouseButton.LeftButton: "left", Qt.MouseButton.RightButton: "right",
            Qt.MouseButton.MiddleButton: "middle", Qt.MouseButton.BackButton: "back", Qt.MouseButton.ForwardButton: "forward"}


def key_name(key: int, modifiers=Qt.KeyboardModifier.NoModifier, *, platform=None, mac_swap=True):
    platform = sys.platform if platform is None else platform
    if platform == "darwin" and key in (Qt.Key.Key_Control, Qt.Key.Key_Meta):
        command = key == (Qt.Key.Key_Control if mac_swap else Qt.Key.Key_Meta)
        return "Cmd" if command else "Ctrl"
    if Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
        return chr(key)
    if Qt.Key.Key_0 <= key <= Qt.Key.Key_9:
        return ("Num" if modifiers & Qt.KeyboardModifier.KeypadModifier else "") + chr(key)
    if Qt.Key.Key_F1 <= key <= Qt.Key.Key_F24:
        return f"F{key - Qt.Key.Key_F1 + 1}"
    return _KEYS.get(key)


class CaptureArea(QFrame):
    input_event = Signal(str, str, bool)
    stop_requested = Signal(str)
    cancel_requested = Signal()
    unsupported_input = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.recording = False
        self.setObjectName("captureArea")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(150)
        self.setAccessibleName("Macro recording area")
        layout = QVBoxLayout(self)
        self.caption = label("Ready to record", "sectionTitle")
        self.caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.help = label("Release any held keys, then click Record.", "muted", True)
        self.help.setAlignment(Qt.AlignmentFlag.AlignCenter)
        for widget in (self.caption, self.help):
            widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            layout.addWidget(widget)

    def event(self, event):
        if self.recording:
            kind = event.type()
            if kind == QEvent.Type.ShortcutOverride:
                event.accept()
                return True
            if kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
                event.accept()
                if event.isAutoRepeat():
                    return True
                down = kind == QEvent.Type.KeyPress
                if event.key() == Qt.Key.Key_F8:
                    if down:
                        self.stop_requested.emit("Stopped with F8")
                elif event.key() == Qt.Key.Key_Escape:
                    if down:
                        self.cancel_requested.emit()
                else:
                    name = key_name(event.key(), event.modifiers(), mac_swap=not QApplication.testAttribute(Qt.ApplicationAttribute.AA_MacDontSwapCtrlAndMeta))
                    if name:
                        self.input_event.emit("key", name, down)
                    elif down:
                        self.unsupported_input.emit("This key is not supported by the macro format and was skipped.")
                return True
            if kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick, QEvent.Type.MouseButtonRelease):
                event.accept()
                if not self.rect().contains(event.position().toPoint()):
                    self.stop_requested.emit("Stopped when the mouse left the recording area")
                elif event.button() in _BUTTONS:
                    self.input_event.emit("mouse", _BUTTONS[event.button()], kind != QEvent.Type.MouseButtonRelease)
                return True
            if kind == QEvent.Type.Wheel:
                event.accept()
                self.unsupported_input.emit("Wheel scrolling is not supported by the macro format and was skipped.")
                return True
            if kind == QEvent.Type.FocusOut:
                self.stop_requested.emit("Stopped when focus left the recording area")
        return super().event(event)


class RecordingDialog(QDialog):
    """Call exec(); accepted results are available as recorded_events."""

    def __init__(self, parent=None, *, max_events=MAX_MACRO_EVENTS, clock=time.monotonic):
        super().__init__(parent)
        self.session = RecordingSession(max_events=max_events, clock=clock)
        self.recorded_events: list[MacroEvent] = []
        self.skipped_inputs = 0
        self.setWindowTitle("Record a macro")
        self.resize(680, 650)
        self.setStyleSheet(STYLE + "QFrame#captureArea { background: #20202f; border: 2px solid #61527b; border-radius: 10px; } QFrame#captureArea:focus { border-color: #b39aff; }")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(15)
        layout.addWidget(label("Record your sequence", "title"))
        layout.addWidget(label("Key presses and mouse clicks are captured only in the focused area below. F8 stops; Escape cancels. Nothing is played back.", "muted", True))
        self.area = CaptureArea()
        self.area.input_event.connect(self._capture)
        self.area.stop_requested.connect(self.stop)
        self.area.cancel_requested.connect(self.reject)
        self.area.unsupported_input.connect(self._unsupported)
        layout.addWidget(self.area)
        self.status = label("Ready · up to 60 seconds", "muted", True)
        self.limit_label = label(f"Capacity: {max_events:,} events · held keys are released when recording stops.", "muted", True)
        layout.addWidget(self.status)
        layout.addWidget(self.limit_label)
        self.warning = label("", "notice", True)
        self.warning.hide()
        layout.addWidget(self.warning)
        self.preview = QListWidget()
        self.preview.setAccessibleName("Recorded macro events")
        self.preview.setMinimumHeight(160)
        layout.addWidget(self.preview, 1)
        buttons = QHBoxLayout()
        self.record_button = QPushButton("Record")
        self.stop_button = QPushButton("Stop (F8)")
        self.use_button = QPushButton("Use recording")
        self.use_button.setObjectName("primary")
        self.cancel_button = QPushButton("Cancel")
        for button, callback in ((self.record_button, self.start), (self.stop_button, self.stop), (self.use_button, self.accept), (self.cancel_button, self.reject)):
            button.setAutoDefault(False)
            button.clicked.connect(callback)
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._tick)
        self._refresh()

    def start(self):
        self.session.start()
        self.skipped_inputs = 0
        self.warning.hide()
        self.preview.clear()
        self.area.recording = True
        self.area.setFocus(Qt.FocusReason.OtherFocusReason)
        self.timer.start()
        self._refresh()

    def stop(self, reason="Stopped"):
        self.session.stop(reason if isinstance(reason, str) else "Stopped")
        self.area.recording = False
        self.timer.stop()
        self._refresh()

    def _capture(self, category, value, down):
        self.session.capture(category, value, down)
        self._refresh()

    def _tick(self):
        self.session.tick()
        self._refresh()

    def _unsupported(self, message):
        self.skipped_inputs += 1
        self.warning.setText(f"{message} Skipped inputs: {self.skipped_inputs}.")
        self.warning.show()

    def _refresh(self):
        active = self.session.active
        self.area.recording = active
        if not active:
            self.timer.stop()
        self.record_button.setEnabled(not active)
        self.record_button.setText("Record again" if self.session.events else "Record")
        self.stop_button.setEnabled(active)
        self.use_button.setEnabled(not active and bool(self.session.events))
        self.area.caption.setText("Recording · type or click here" if active else "Ready to record" if not self.session.events else "Recording stopped")
        self.area.help.setText("F8 stops · Escape cancels · leaving this area’s focus stops recording" if active else "Review the events, then choose Use recording or Record again.")
        self.status.setText(f"{self.session.reason} · {len(self.session.events):,} events · {self.session.elapsed_ms / 1000:.1f} s")
        count = self.preview.count()
        for index in range(max(0, count - 1), len(self.session.events)):
            event = self.session.events[index]
            text = f"{event.kind.replace('_', ' '):<12}    {event.value:<10}    wait {event.delay_ms} ms"
            if index < count:
                self.preview.item(index).setText(text)
            else:
                self.preview.addItem(text)
        if self.preview.count():
            self.preview.scrollToBottom()

    def accept(self):
        if self.session.active or not self.session.events:
            return
        events = copy.deepcopy(self.session.events)
        Configuration(macros=[Macro(events=events)]).validate()
        self.recorded_events = events
        super().accept()

    def reject(self):
        self.stop("Cancelled")
        self.recorded_events = []
        super().reject()

    def event(self, event):
        if event.type() == QEvent.Type.WindowDeactivate and hasattr(self, "session"):
            self.stop("Stopped when the recording window lost focus")
        return super().event(event)
