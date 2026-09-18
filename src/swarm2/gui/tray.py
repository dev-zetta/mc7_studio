"""Optional battery tray presentation; no device I/O or persistent settings."""

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

LOW_BATTERY_PERCENT = 20
REARM_BATTERY_PERCENT = 25
STATUS_INTERVAL_MS = 60_000


def normalized_status(status):
    status = status if isinstance(status, dict) else {}
    battery = status.get("battery_percent")
    charging = status.get("charging")
    firmware = status.get("firmware_version")
    return {
        "firmware_version": firmware if isinstance(firmware, str) and firmware else None,
        "battery_percent": battery if type(battery) is int and 0 <= battery <= 100 else None,
        "charging": charging if type(charging) is bool else None,
    }


class BatteryAlertPolicy:
    """Alert once per low-battery period; unknown readings do not rearm it."""

    def __init__(self):
        self.device_id = None
        self.alerted = False

    def update(self, device_id, status, *, enabled):
        if device_id != self.device_id:
            self.device_id = device_id
            self.alerted = False
        status = normalized_status(status)
        level = status["battery_percent"]
        if level is not None and level >= REARM_BATTERY_PERCENT:
            self.alerted = False
        if (enabled and device_id is not None and level is not None
                and level <= LOW_BATTERY_PERCENT and status["charging"] is False
                and not self.alerted):
            self.alerted = True
            return True
        return False


def battery_icon(status) -> QIcon:
    status = normalized_status(status)
    level, charging = status["battery_percent"], status["charging"]
    accent = QColor("#59c897" if charging is True else "#ef9b58" if level is not None and level <= LOW_BATTERY_PERCENT else "#9d82e5")
    icon = QIcon()
    for size in (16, 22, 32, 64):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(size / 32, size / 32)
        painter.setPen(QPen(accent, 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(QRectF(3, 8, 24, 16), 3, 3)
        painter.fillRect(QRectF(28, 13, 3, 6), accent)
        if level is not None:
            painter.fillRect(QRectF(6, 11, 18 * level / 100, 10), accent)
        else:
            painter.setFont(QFont("Sans Serif", 11, QFont.Weight.Bold))
            painter.drawText(QRectF(4, 7, 22, 18), Qt.AlignmentFlag.AlignCenter, "?")
        if charging is True:
            bolt = QPainterPath(QPointF(17, 6))
            for point in ((10, 17), (15, 17), (13, 26), (21, 14), (16, 14)):
                bolt.lineTo(QPointF(*point))
            bolt.closeSubpath()
            painter.setPen(QPen(QColor("#15171c"), 1))
            painter.setBrush(QColor("#f0fff7"))
            painter.drawPath(bolt)
        painter.end()
        icon.addPixmap(pixmap)
    return icon


class BatteryTray(QObject):
    show_requested = Signal()
    read_requested = Signal()
    quit_requested = Signal()

    def __init__(self, parent=None, *, icon=None, available=None, messages_supported=None):
        super().__init__(parent)
        self.icon = icon if icon is not None else QSystemTrayIcon(self)
        self._available = available or QSystemTrayIcon.isSystemTrayAvailable
        self._messages_supported = messages_supported or QSystemTrayIcon.supportsMessages
        self.enabled = False
        self.notifications = False
        self.device_id = None
        self.status = normalized_status(None)
        self.policy = BatteryAlertPolicy()
        self.menu = QMenu(parent)
        self.status_action = self.menu.addAction("Battery: not reported")
        self.status_action.setEnabled(False)
        self.menu.addSeparator()
        self.show_action = self.menu.addAction("Show MC7 Studio")
        self.show_action.triggered.connect(lambda _checked=False: self.show_requested.emit())
        self.read_action = self.menu.addAction("Read status now")
        self.read_action.triggered.connect(lambda _checked=False: self.read_requested.emit())
        self.menu.addSeparator()
        self.quit_action = self.menu.addAction("Quit MC7 Studio")
        self.quit_action.triggered.connect(lambda _checked=False: self.quit_requested.emit())
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._activated)
        self.icon.messageClicked.connect(self.show_requested.emit)
        self.update_status(None, None)

    def available(self):
        return bool(self._available())

    def messages_supported(self):
        return bool(self._messages_supported())

    def set_enabled(self, enabled):
        enabled = bool(enabled and self.available())
        if enabled != self.enabled:
            self.icon.show() if enabled else self.icon.hide()
        self.enabled = enabled
        return enabled

    def set_notifications(self, enabled):
        self.notifications = bool(enabled)
        self.update_status(self.device_id, self.status)

    def update_status(self, device_id, status):
        self.device_id = device_id
        self.status = normalized_status(status)
        level, charging = self.status["battery_percent"], self.status["charging"]
        caption = f"Battery: {level}%" if level is not None else "Battery: not reported"
        if charging is True:
            caption += " · charging"
        elif charging is False:
            caption += " · not charging"
        self.status_action.setText(caption)
        self.icon.setToolTip(f"MC7 Studio\n{caption}")
        self.icon.setIcon(battery_icon(self.status))
        notify = self.enabled and self.notifications and self.messages_supported()
        if self.policy.update(device_id, self.status, enabled=notify):
            self.icon.showMessage("MC7 battery is low", f"Your mouse battery is at {level}%. Connect it to charge.",
                                  QSystemTrayIcon.MessageIcon.Warning, 10_000)

    def set_read_enabled(self, enabled):
        self.read_action.setEnabled(bool(enabled))

    def _activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_requested.emit()
