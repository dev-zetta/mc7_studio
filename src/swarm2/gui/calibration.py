"""Local pointer aim dialogs; accepting returns a suggestion and never uses USB."""

from collections import deque
import time

from PySide6.QtCore import QPoint, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QDialog, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from ..calibration import (ANGLE_STROKES, DPI_SAMPLES, PRACTICE_STROKES,
                          AngleCalibrationSession, DpiSample, dpi_sample_value,
                          suggest_dpi, validate_current_dpi)
from .widgets import STYLE, label


class _AimCanvas(QWidget):
    progress = Signal(int, int)
    completed = Signal(object)
    message = Signal(str)
    invalidated = Signal()
    MARGIN = 48
    RADIUS = 22

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(600, 240)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._armed = False
        self._started = False
        self._started_at = None
        self._trace = deque(maxlen=2048)
        self._cursor = None

    def origin(self):
        return QPoint(self.MARGIN, self.height() // 2)

    def _point(self, event):
        point = event.position().toPoint()
        if not self.rect().contains(point):
            return None
        return point.x(), point.y()

    def _is_origin(self, point):
        origin = self.origin()
        return (point[0]-origin.x())**2 + (point[1]-origin.y())**2 <= self.RADIUS**2

    def _begin(self):
        self._armed = True
        self._started = False
        self._started_at = None
        self._trace.clear()
        self._cursor = None
        self.update()

    def stop(self):
        self._armed = self._started = False
        self._trace.clear()
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._started:
            self.stop()
            self.invalidated.emit()

    def _paint_base(self):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#101216"))
        painter.setPen(QPen(QColor("#434857"), 1, Qt.PenStyle.DashLine))
        painter.drawLine(self.MARGIN, self.height()//2, self.width()-self.MARGIN, self.height()//2)
        points = list(self._trace)
        painter.setPen(QPen(QColor("#686079"), 2))
        for start, end in zip(points, points[1:]):
            painter.drawLine(QPoint(*start), QPoint(*end))
        if self._armed and not self._started:
            painter.setPen(QPen(QColor("#c6b3ff"), 2))
            painter.setBrush(QColor("#493760"))
            painter.drawEllipse(self.origin(), self.RADIUS, self.RADIUS)
            painter.drawText(self.rect().adjusted(90, 10, -20, -10),
                             Qt.AlignmentFlag.AlignCenter, "Click the start circle, then move toward the target")
        return painter


class AngleCanvas(_AimCanvas):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAccessibleName("Angle alignment pointer area")
        self.session = None
        self.practice = False

    def begin(self, *, practice=False):
        self.practice = practice
        self.session = None
        self._begin()
        self.progress.emit(0, PRACTICE_STROKES if practice else ANGLE_STROKES)

    def mousePressEvent(self, event):
        point = self._point(event)
        if (event.button() == Qt.MouseButton.LeftButton and point is not None
                and self._armed and not self._started and self._is_origin(point)):
            self.session = AngleCalibrationSession(point, strokes=PRACTICE_STROKES if self.practice else ANGLE_STROKES)
            self._started = True
            self._started_at = time.monotonic()
            self._trace.append(point)
            self.message.emit("Move side to side through the highlighted borders. Keep each pass as straight as feels natural.")
            self.update()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        point = self._point(event)
        if self._started and point is not None and not self.session.complete:
            self._trace.append(point)
            right = self.session.next_direction > 0
            crossed = point[0] >= self.width()-self.MARGIN if right else point[0] <= self.MARGIN
            if crossed:
                try:
                    self.session.add_endpoint(point)
                except ValueError as error:
                    self.message.emit(str(error))
                else:
                    self.progress.emit(len(self.session.angles), self.session.stroke_count)
                    if self.session.complete:
                        self._started = self._armed = False
                        self.completed.emit(None if self.practice else self.session.result())
            self.update()
        super().mouseMoveEvent(event)

    def paintEvent(self, _event):
        painter = self._paint_base()
        if self._started and self.session is not None:
            right = self.session.next_direction > 0
            x = self.width()-self.MARGIN if right else self.MARGIN
            painter.setPen(QPen(QColor("#c6b3ff"), 3))
            painter.drawLine(x, 18, x, self.height()-18)
            painter.drawText(QRectF(x-38, 18, 76, 30), Qt.AlignmentFlag.AlignCenter, "NEXT")
        painter.end()


class AngleCalibrationDialog(QDialog):
    def __init__(self, parent=None, *, current_angle=0, angle_enabled=False,
                 angle_snapping=False, settings_verified=False):
        super().__init__(parent)
        self.suggested_angle = None
        self._result = None
        self.setWindowTitle("Angle aim alignment")
        self.resize(820, 520)
        self.setStyleSheet(STYLE)
        self.ready = (settings_verified is True and type(current_angle) is int
                      and -30 <= current_angle <= 30 and type(angle_enabled) is bool
                      and type(angle_snapping) is bool and not angle_snapping
                      and (not angle_enabled or current_angle == 0))
        body = QVBoxLayout(self)
        body.setContentsMargins(22, 20, 22, 20)
        body.setSpacing(12)
        body.addWidget(label("Find your natural angle", "title"))
        body.addWidget(label("Aim alignment from desktop pointer movement. Keep the same mouse and desktop settings throughout. Acceleration and scaling can affect the suggestion.", "muted", True))
        self.notice = label("Try three practice passes, or begin ten measured passes. The result is staged in your draft when you accept it.", "notice", True)
        if not settings_verified:
            self.notice.setText("Read the mouse and select its active profile before starting.")
        elif not self.ready:
            self.notice.setText("Turn off angle snapping and turn off angle tuning (or set it to 0°), apply those settings, and read the mouse again before starting.")
        body.addWidget(self.notice)
        self.canvas = AngleCanvas()
        body.addWidget(self.canvas, 1)
        self.progress_label = label("0 / 10 passes", "sectionTitle")
        body.addWidget(self.progress_label)
        controls = QHBoxLayout()
        self.practice_button = QPushButton("Practice 3 passes")
        self.start_button = QPushButton("Start 10 passes")
        self.accept_button = QPushButton("Use suggested angle")
        self.accept_button.setObjectName("primary")
        self.cancel_button = QPushButton("Cancel")
        self.practice_button.setEnabled(self.ready)
        self.start_button.setEnabled(self.ready)
        self.accept_button.setEnabled(False)
        for button in (self.practice_button, self.start_button, self.cancel_button, self.accept_button):
            button.setAutoDefault(False)
            controls.addWidget(button)
        body.addLayout(controls)
        self.practice_button.clicked.connect(lambda: self.start(practice=True))
        self.start_button.clicked.connect(self.start)
        self.cancel_button.clicked.connect(self.reject)
        self.accept_button.clicked.connect(self.accept)
        self.canvas.progress.connect(lambda count, total: self.progress_label.setText(f"{count} / {total} passes"))
        self.canvas.message.connect(self.notice.setText)
        self.canvas.completed.connect(self._completed)
        self.canvas.invalidated.connect(self._invalidated)

    def start(self, _checked=False, *, practice=False):
        if not self.ready:
            return
        self.suggested_angle = self._result = None
        self.accept_button.setEnabled(False)
        self.canvas.begin(practice=practice)
        self.notice.setText("Click the start circle. Move across the area to each highlighted border; alternate directions.")

    def _completed(self, result):
        self._result = result
        if result is None:
            self.notice.setText("Practice complete. Start ten measured passes when you are ready.")
        else:
            elapsed = time.monotonic()-self.canvas._started_at
            self.progress_label.setText(f"Suggested angle: {result.suggested_angle:+d}° · 10 passes · {elapsed:.1f} s")
            self.notice.setText("Use the suggestion to update your local draft, then Apply sensitivity when ready."
                                + (" The result is limited to the mouse's ±30° range." if result.unclamped_angle != result.suggested_angle else ""))
            self.accept_button.setEnabled(True)

    def _invalidated(self):
        self._result = self.suggested_angle = None
        self.accept_button.setEnabled(False)
        self.notice.setText("The pointer area changed size. Restart the passes so all samples use the same geometry.")

    def accept(self):
        if self.ready and self._result is not None:
            self.suggested_angle = self._result.suggested_angle
            self.canvas.stop()
            super().accept()

    def reject(self):
        self.suggested_angle = None
        self.canvas.stop()
        super().reject()


class DpiCanvas(_AimCanvas):
    # Original native layout: successive stationary targets, with no imitation
    # of the vendor's timed target animation or its visual assets.
    TARGETS = ((0.82, 0.27), (0.20, 0.76), (0.78, 0.73), (0.23, 0.24), (0.80, 0.48))

    def __init__(self, current_dpi, parent=None):
        super().__init__(parent)
        self.setAccessibleName("DPI aim target area")
        self.current_dpi = validate_current_dpi(current_dpi)
        self.samples = []
        self._first_point = None

    def begin(self):
        self.samples.clear()
        self._first_point = None
        self._begin()
        self.progress.emit(0, DPI_SAMPLES)

    def target(self):
        x, y = self.TARGETS[min(len(self.samples), DPI_SAMPLES-1)]
        return round(x*self.width()), round(y*self.height())

    def mouseMoveEvent(self, event):
        point = self._point(event)
        if self._started and point is not None:
            if self._first_point is None:
                self._first_point = point
            self._trace.append(point)
        self.update()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        point = self._point(event)
        if event.button() != Qt.MouseButton.LeftButton or point is None or not self._armed:
            return super().mousePressEvent(event)
        if not self._started:
            if self._is_origin(point):
                self._started = True
                self._started_at = time.monotonic()
                self.message.emit("Move toward the target and click where you aim. Five attempts will produce a DPI suggestion.")
        elif self._first_point is None:
            self.message.emit("Move the pointer toward the target before clicking.")
        else:
            sample = DpiSample(self._first_point, self.target(), point)
            try:
                dpi_sample_value(self.current_dpi, sample)
            except ValueError as error:
                self.message.emit(str(error))
                self._first_point = None
            else:
                self.samples.append(sample)
                self._first_point = None
                self._trace.clear()
                self.progress.emit(len(self.samples), DPI_SAMPLES)
                if len(self.samples) == DPI_SAMPLES:
                    self._armed = self._started = False
                    self.completed.emit(suggest_dpi(self.current_dpi, self.samples))
        self.update()
        super().mousePressEvent(event)

    def paintEvent(self, _event):
        painter = self._paint_base()
        if self._started:
            painter.setPen(QPen(QColor("#c6b3ff"), 2))
            painter.setBrush(QColor("#493760"))
            painter.drawEllipse(QPoint(*self.target()), self.RADIUS, self.RADIUS)
            painter.drawText(self.rect().adjusted(14, 10, -14, -10), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight,
                             f"Target {len(self.samples)+1} / 5")
        painter.end()


class DpiCalibrationDialog(QDialog):
    def __init__(self, parent=None, *, current_dpi, settings_verified=False):
        super().__init__(parent)
        self.current_dpi = validate_current_dpi(current_dpi)
        self.ready = settings_verified is True
        self.suggested_dpi = None
        self._result = None
        self.setWindowTitle("DPI aim recommendation")
        self.resize(820, 510)
        self.setStyleSheet(STYLE)
        body = QVBoxLayout(self)
        body.setContentsMargins(22, 20, 22, 20)
        body.setSpacing(12)
        body.addWidget(label("Find a comfortable DPI", "title"))
        body.addWidget(label(f"Current DPI: {current_dpi}. Aim at five targets to get a DPI suggestion. Keep the mouse, DPI and desktop settings unchanged. This measures desktop aim, not physical sensor resolution.", "muted", True))
        self.notice = label("Begin, then click the start circle and aim at each successive target." if self.ready else
                            "Read the mouse and select its active profile and active DPI stage before starting.", "notice", True)
        body.addWidget(self.notice)
        self.canvas = DpiCanvas(current_dpi)
        body.addWidget(self.canvas, 1)
        self.progress_label = label("0 / 5 targets", "sectionTitle")
        body.addWidget(self.progress_label)
        controls = QHBoxLayout()
        self.start_button = QPushButton("Start 5 targets")
        self.cancel_button = QPushButton("Cancel")
        self.accept_button = QPushButton("Use suggested DPI")
        self.accept_button.setObjectName("primary")
        self.start_button.setEnabled(self.ready)
        self.accept_button.setEnabled(False)
        for button in (self.start_button, self.cancel_button, self.accept_button):
            button.setAutoDefault(False)
            controls.addWidget(button)
        body.addLayout(controls)
        self.start_button.clicked.connect(self.start)
        self.cancel_button.clicked.connect(self.reject)
        self.accept_button.clicked.connect(self.accept)
        self.canvas.progress.connect(lambda count, total: self.progress_label.setText(f"{count} / {total} targets"))
        self.canvas.message.connect(self.notice.setText)
        self.canvas.completed.connect(self._completed)
        self.canvas.invalidated.connect(self._invalidated)

    def start(self):
        if self.ready:
            self.suggested_dpi = self._result = None
            self.accept_button.setEnabled(False)
            self.canvas.begin()
            self.notice.setText("Click the start circle, then move toward and click each target. Desktop settings must stay unchanged.")

    def _completed(self, result):
        self._result = result
        self.progress_label.setText(f"Suggested DPI: {result.suggested_dpi} · target error: {result.accuracy_pixels} px · spread: {result.precision_pixels} px")
        self.notice.setText("Use the suggestion to update your local draft, then Apply sensitivity when ready.")
        self.accept_button.setEnabled(True)

    def _invalidated(self):
        self._result = self.suggested_dpi = None
        self.accept_button.setEnabled(False)
        self.notice.setText("The pointer area changed size. Restart so every attempt uses the same geometry.")

    def accept(self):
        if self.ready and self._result is not None:
            self.suggested_dpi = self._result.suggested_dpi
            self.canvas.stop()
            super().accept()

    def reject(self):
        self.suggested_dpi = None
        self.canvas.stop()
        super().reject()
