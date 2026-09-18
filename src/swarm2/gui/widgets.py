"""Small original widgets, drawn locally without vendor artwork."""

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QColorDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QSpinBox, QStyle,
    QStyleOptionSpinBox, QVBoxLayout, QWidget,
)


STYLE = """
QWidget { background: #15171c; color: #e8eaf0; font-size: 13px; }
QMainWindow, QScrollArea, QScrollArea > QWidget > QWidget { background: #15171c; }
QWidget#sidebar { background: #101216; border-right: 1px solid #292c35; }
QLabel { background: transparent; }
QLabel#brand { font-size: 23px; font-weight: 750; letter-spacing: 2px; }
QLabel#eyebrow { color: #ac9cff; font-size: 11px; font-weight: 650; letter-spacing: 2px; }
QLabel#title { font-size: 30px; font-weight: 700; }
QLabel#subtitle, QLabel#muted { color: #969dab; }
QLabel#sectionTitle { font-size: 16px; font-weight: 650; }
QLabel#heroTitle { font-size: 39px; font-weight: 700; }
QLabel#largeValue { font-size: 24px; font-weight: 650; }
QLabel#statusBadge { background: #28223e; color: #c0b2ff; border: 1px solid #4c416a; border-radius: 12px; padding: 5px 11px; }
QLabel#notice { background: #20232b; border: 1px solid #353946; border-radius: 8px; padding: 11px 14px; color: #bfc5d2; }
QLabel#error { background: #39272b; border: 1px solid #674149; border-radius: 8px; padding: 11px 14px; color: #ffb6bb; }
QFrame#card { background: #1c1f26; border: 1px solid #30343f; border-radius: 12px; }
QFrame#footer { background: #191c22; border-top: 1px solid #30343f; }
QListWidget#navigation { background: transparent; border: none; outline: 0; font-size: 14px; }
QListWidget#navigation::item { padding: 13px 16px; margin: 2px 0px; border-radius: 7px; color: #9ca3b3; }
QListWidget#navigation::item:selected { background: #2e2743; color: #d1c6ff; }
QListWidget#navigation::item:hover:!selected { background: #1f222a; }
QPushButton { background: #2a2e39; border: 1px solid #414755; border-radius: 7px; padding: 9px 15px; font-weight: 550; }
QPushButton:hover { background: #363b49; border-color: #697389; }
QPushButton:pressed { background: #242830; }
QPushButton#primary { background: #ad96ff; color: #191426; border-color: #ad96ff; font-weight: 700; }
QPushButton#primary:hover { background: #bca9ff; }
QPushButton:disabled { background: #24262e; color: #707784; border-color: #353945; }
QPushButton#primary:disabled { background: #363044; color: #8a829a; border-color: #4a405d; }
QPushButton:focus, QComboBox:focus, QSpinBox:focus, QLineEdit:focus, QPlainTextEdit:focus, QListWidget:focus { border: 1px solid #b29aff; }
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit { background: #14171d; border: 1px solid #3c4250; border-radius: 6px; padding: 8px 10px; selection-background-color: #655195; }
QFrame#card QLineEdit, QFrame#card QSpinBox, QFrame#card QComboBox { background: #14171d; }
QSpinBox { min-height: 19px; padding-right: 20px; }
QSpinBox::up-button, QSpinBox::down-button { width: 18px; background: #2d3240; border: none; }
QComboBox { min-height: 19px; padding-right: 23px; }
QComboBox QAbstractItemView { background: #20242d; selection-background-color: #504069; outline: none; }
QComboBox:disabled, QSpinBox:disabled { color: #858c9b; background: #20232b; border-color: #303644; }
QTabWidget::pane { border: 1px solid #353c49; border-radius: 6px; background: #191d24; }
QTabBar::tab { background: #222731; border: 1px solid #353c49; color: #a7afbe; padding: 10px 23px; margin-right: 3px; border-top-left-radius: 6px; border-top-right-radius: 6px; }
QTabBar::tab:selected { background: #3a304f; color: #d4c7ff; border-color: #7a679b; }
QTabBar::tab:disabled { color: #69717f; }
QCheckBox { spacing: 8px; background: transparent; }
QCheckBox::indicator { width: 17px; height: 17px; border: 1px solid #666e80; border-radius: 4px; background: #151820; }
QCheckBox::indicator:checked { background: #ab94fa; border-color: #ccbdff; }
QCheckBox::indicator:disabled { background: #292d36; border-color: #424958; }
QSlider { background: transparent; }
QSlider::groove:horizontal { height: 5px; background: #3c4250; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #aa94ff; border-radius: 2px; }
QSlider::handle:horizontal { width: 15px; margin: -5px 0; background: #c4b4ff; border-radius: 7px; }
QScrollBar:vertical { background: #191c22; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #414855; min-height: 28px; border-radius: 5px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: #191c22; height: 10px; margin: 0; }
QScrollBar::handle:horizontal { background: #414855; min-width: 28px; border-radius: 5px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QTableWidget { background: #161920; border: 1px solid #363c49; gridline-color: #2e3440; border-radius: 6px; selection-background-color: #403652; }
QTableWidget::item { padding: 8px; }
QHeaderView::section { background: #252a34; color: #aab3c4; border: none; padding: 10px; font-weight: 600; }
QListWidget { background: #171a21; border: 1px solid #363c49; border-radius: 6px; }
QListWidget::item { padding: 11px; }
QListWidget::item:selected { background: #3e3254; }
QToolTip { background: #292d37; color: #eee; border: 1px solid #656071; padding: 6px; }
"""


def label(text: str, name: str = "", wrap: bool = False) -> QLabel:
    widget = QLabel(text)
    widget.setObjectName(name)
    widget.setWordWrap(wrap)
    if wrap:
        widget.setMinimumWidth(1)
    return widget


def card(title: str = "", description: str = "") -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(22, 20, 22, 22)
    layout.setSpacing(16)
    if title:
        layout.addWidget(label(title, "sectionTitle"))
    if description:
        layout.addWidget(label(description, "muted", True))
    return frame, layout


class ColorButton(QPushButton):
    colorChanged = Signal(str)

    def __init__(self, color: str = "#ad96ff", parent=None):
        super().__init__(parent)
        self._color = color
        self.clicked.connect(self.choose)
        self.set_color(color)

    def color(self) -> str:
        return self._color

    def set_color(self, color: str):
        self._color = color
        self.setText(color.upper())
        self.setAccessibleName(f"Choose color, {color}")
        self.setStyleSheet(f"QPushButton {{ border-left: 8px solid {color}; }}")

    def choose(self):
        color = QColorDialog.getColor(QColor(self._color), self, "Choose a color")
        if color.isValid():
            self.set_color(color.name())
            self.colorChanged.emit(color.name())


class SpinBox(QSpinBox):
    """Native spin-button behavior with visible chevrons in the dark theme."""

    def paintEvent(self, event):
        super().paintEvent(event)
        option = QStyleOptionSpinBox()
        self.initStyleOption(option)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#c1c6d1" if self.isEnabled() else "#626976"), 1.3))
        for control, direction in ((QStyle.SubControl.SC_SpinBoxUp, -1), (QStyle.SubControl.SC_SpinBoxDown, 1)):
            rectangle = self.style().subControlRect(QStyle.ComplexControl.CC_SpinBox, option, control, self)
            center = rectangle.center()
            path = QPainterPath()
            path.moveTo(QPointF(center.x() - 3, center.y() - direction))
            path.lineTo(QPointF(center.x(), center.y() + direction * 2))
            path.lineTo(QPointF(center.x() + 3, center.y() - direction))
            painter.drawPath(path)
        painter.end()


class MouseIllustration(QWidget):
    """An intentionally schematic mouse silhouette, not a product rendering."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(240, 250)
        self.setAccessibleName("Schematic top view of a gaming mouse")

    def sizeHint(self):
        return QSize(300, 320)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        side = min(self.width() / 300, self.height() / 320)
        painter.translate(self.width() / 2, self.height() / 2)
        painter.scale(side, side)
        painter.translate(-150, -160)
        painter.setPen(QPen(QColor("#2c283e"), 1))
        for radius in (115, 135, 155):
            painter.drawEllipse(QRectF(150 - radius, 160 - radius, radius * 2, radius * 2))
        path = QPainterPath()
        path.moveTo(150, 27)
        path.cubicTo(219, 27, 227, 88, 238, 172)
        path.cubicTo(253, 266, 210, 299, 150, 300)
        path.cubicTo(87, 300, 52, 266, 64, 178)
        path.cubicTo(76, 101, 80, 27, 150, 27)
        gradient = QLinearGradient(70, 40, 215, 290)
        gradient.setColorAt(0, QColor("#414350"))
        gradient.setColorAt(0.4, QColor("#282b36"))
        gradient.setColorAt(1, QColor("#191b23"))
        painter.setBrush(gradient)
        painter.setPen(QPen(QColor("#737080"), 1.5))
        painter.drawPath(path)
        painter.setPen(QPen(QColor("#14151d"), 3))
        painter.drawLine(150, 29, 150, 148)
        split = QPainterPath()
        split.moveTo(73, 142)
        split.quadTo(150, 166, 229, 141)
        painter.drawPath(split)
        painter.setBrush(QColor("#a58bf6"))
        painter.setPen(QPen(QColor("#c5b4ff"), 1))
        painter.drawRoundedRect(QRectF(140, 63, 20, 47), 7, 7)
        painter.setPen(QPen(QColor("#6a558f"), 1))
        for y in range(72, 104, 6):
            painter.drawLine(144, y, 156, y)
        painter.setBrush(QColor("#181b23"))
        painter.setPen(QPen(QColor("#8871ba"), 1))
        painter.drawRoundedRect(QRectF(59, 136, 10, 32), 4, 4)
        painter.drawRoundedRect(QRectF(54, 177, 10, 33), 4, 4)
        painter.setPen(QPen(QColor("#b39bf4"), 2))
        painter.drawLine(130, 252, 140, 264)
        painter.drawLine(140, 264, 151, 246)
        painter.drawLine(151, 246, 161, 264)
        painter.drawLine(161, 264, 172, 252)
        painter.end()
