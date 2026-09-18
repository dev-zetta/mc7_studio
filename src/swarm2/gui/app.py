"""Native desktop editing with explicit local drafts and bounded device jobs."""

from __future__ import annotations

import copy
import json
import os
import sys
from datetime import datetime
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QProcess, QSize, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QCloseEvent, QIcon, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QPushButton, QScrollArea, QSlider,
    QStackedWidget, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from ..configuration import (
    ACTION_VALUES, CUSTOM_PRECISION_PREFIX, HAPTIC_INTENSITIES,
    LIFT_OFF_DISTANCES, MAX_MACROS, MAX_MACRO_EVENTS, Action, Configuration,
    CountdownTimer, Macro, MacroEvent, PresetStore,
    encode_host_action_icon_rgba, host_action_icon_rgba,
    parse_custom_precision_dpi,
)
from ..automatic_profiles import (
    AutomaticProfileSettings, AutomaticProfileStore, ProfileSelection,
    resolve_profile,
)
from ..automatic_profile_monitor import (
    ForegroundApplicationMonitor, ForegroundApplicationSnapshot,
    ForegroundApplicationStatus,
)
from ..button_commands import BUTTON_LABELS
from ..host_metrics import GpuSourcePreferenceError, GpuSourcePreferenceStore
from ..lighting_commands import LIGHTING_EFFECT_IDS
from ..lcd_commands import (
    LCD_HOST_ACTION_WIDGETS, LCD_KEY_WIDGETS, LCD_MACRO_WIDGETS,
    LCD_TIMER_WIDGETS, LCD_WIDGETS,
)
from ..lcd_layout import move_page
from ..runtime import helper_command
from ..sensor_commands import SCREEN_BRIGHTNESS_LEVELS, SCREEN_TIMEOUT_VALUES
from .widgets import STYLE, ColorButton, MouseIllustration, SpinBox as QSpinBox, card, label
from .recording import RecordingDialog
from .background import BackgroundDialog
from .calibration import AngleCalibrationDialog, DpiCalibrationDialog
from .custom_icon import (
    prepare_application_icon, prepare_custom_icon, preview_from_wire_rgba,
)
from .dcu import DcuCalibrationDialog
from .profile_appearance import decode_profile_image, prepare_profile_image, profile_icon
from .tray import BatteryTray, STATUS_INTERVAL_MS, normalized_status


PAGES = [
    ("Overview", "Your mouse, your setup.", "An independent configurator for the Command Series MC7."),
    ("Sensitivity", "Precision, on your terms.", "Set five sensitivity stages and tune your sensor."),
    ("Buttons", "Make every click count.", "Assign actions to the standard and Easy-Shift layers."),
    ("Lighting", "Find your signature.", "Choose an effect, color and brightness for your setup."),
    ("Display", "A glance is enough.", "Choose the information you want on the mouse display."),
    ("Macros", "Build your sequence.", "Create and edit reusable input sequences in your local preset."),
    ("Profiles", "A setup for every session.", "Save, duplicate and exchange presets on this computer."),
    ("Device", "Keep your setup connected.", "Connection details and the features available to your mouse."),
]

PAGE_SECTIONS = {1: "sensor", 2: "buttons", 3: "lighting", 4: "display", 7: "power"}
SECTION_LABELS = {"sensor": "sensitivity", "buttons": "buttons", "lighting": "lighting",
                  "display": "display settings", "power": "power settings"}

# Browser launch and calculator are handled by the desktop's consumer-key
# bindings. Application, website, file and folder targets stay in the local
# preset and use the companion listener. Existing Windows-only and unknown
# tiles are retained, but are not offered as new assignments. Polling does not
# render on the tested firmware despite its entry in the vendor's widget catalog.
LCD_CHOICES = ("empty", "remap_key", "hotkey", "macro", "countdown", "dpi", "led_brightness", "general_media", "launch_obs", "obs_screenshot", "obs_studio_mode", "system_media", "play_pause",
               "next_track", "previous_track", "stop", "speaker_mute", "shuffle",
               "repeat", "volume_mute", "cut", "copy", "paste", "undo", "redo",
               "launch_browser", "browser_back", "browser_forward", "calculator",
               "open_application", "open_website", "open_file", "open_folder",
               "cpu_load", "cpu_temperature", "gpu_load", "gpu_temperature",
               "ram_usage")
HOST_LCD_WIDGETS = frozenset(("cpu_load", "cpu_temperature", "gpu_load",
                              "gpu_temperature", "ram_usage"))
LCD_KEY_DEFAULTS = {"remap_key": "A", "hotkey": "Ctrl+S"}
CUSTOM_EASY_AIM_PICKER = ("dpi", "precision_custom")

def title_case(value: str) -> str:
    words = value.replace("_", " ").split()
    acronyms = {"aimo", "dpi", "eco", "cpu", "gpu", "ram", "lcd", "rgb"}
    return " ".join(word.upper() if word.lower() in acronyms else word.title() for word in words)


def custom_easy_aim_dpi(action: Action) -> int | None:
    """Return the bounded DPI carried by a custom Easy-Aim action."""

    return parse_custom_precision_dpi(action.value) if action.kind == "dpi" else None


class DeviceJob(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, operation: Callable, parent=None):
        super().__init__(parent)
        self.operation = operation

    def run(self):
        try:
            self.completed.emit(self.operation())
        except Exception as error:
            self.failed.emit(str(error))


class ApplicationScanJob(QThread):
    """Collect one foreground application without occupying the USB worker."""

    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, operation: Callable, parent=None):
        super().__init__(parent)
        self.operation = operation

    def run(self):
        try:
            self.completed.emit(self.operation())
        except Exception as error:
            self.failed.emit(str(error))


class MainWindow(QMainWindow):
    """A local configuration editor; device I/O is supplied by the service."""

    def __init__(self, service=None, store: PresetStore | None = None, *,
                 auto_discover: bool = True, automatic_store=None,
                 application_monitor=None, host_integration_manager=None,
                 countdown_process_factory=None, gpu_source_store=None):
        super().__init__()
        if service is None:
            from ..service import DeviceService
            service = DeviceService()
        self.service = service
        self.store = store if store is not None else PresetStore()
        self.gpu_source_store = (
            gpu_source_store if gpu_source_store is not None
            else GpuSourcePreferenceStore(
                self.store.directory / "host-metrics" / "gpu-source.json"))
        self._gpu_source_error = ""
        try:
            self._gpu_source = self.gpu_source_store.load()
        except (OSError, ValueError) as error:
            self._gpu_source = None
            self._gpu_source_error = str(error)
        self.automatic_store = (automatic_store if automatic_store is not None
                                else AutomaticProfileStore())
        self.application_monitor = (application_monitor if application_monitor is not None
                                    else ForegroundApplicationMonitor())
        self.host_integration_manager = host_integration_manager
        self._automatic_settings_error = ""
        try:
            loaded_automatic_settings = self.automatic_store.load()
            if type(loaded_automatic_settings) is not AutomaticProfileSettings:
                raise ValueError("Automatic profile storage returned invalid settings.")
            self._automatic_settings = loaded_automatic_settings.normalized()
        except (OSError, ValueError) as error:
            self._automatic_settings = AutomaticProfileSettings()
            self._automatic_settings_error = str(error)
        self.draft = Configuration()
        self.dirty = False
        self._saved = False
        self.snapshot = None
        self.devices = []
        self.selected_device_id = None
        self._job: DeviceJob | None = None
        self._loading = False
        self._macro_loading = False
        self._timer_loading = False
        self._macro_merge_warning = ""
        self._last_status = normalized_status(None)
        self._status_refresh_due = False
        self._host_lcd_context = None
        self._gpu_source_job = None
        self._gpu_source_loading = False
        self._gpu_sources = ()
        self._media_player_job = None
        self._media_player_loading = False
        self._media_players = ()
        self._countdown_process_factory = countdown_process_factory or QProcess
        self._countdown_process = None
        self._countdown_request = None
        self._countdown_stdout = bytearray()
        self._countdown_stderr = bytearray()
        self._countdown_ready_seen = False
        self._countdown_stopped_seen = False
        self._countdown_running_ids = set()
        self._countdown_remaining = {}
        self._countdown_result = None
        self._countdown_local_abort = False
        self._countdown_stop_requested = False
        self._countdown_stop_written = False
        self._countdown_stop_reason = ""
        self._countdown_close_pending = False
        self._countdown_quit_pending = False
        self._dcu_dialog = None
        self._firmware_dialog = None
        self._restore_dialog = None
        self._automatic_profile_dialog = None
        self._host_integrations_dialog = None
        self._automatic_scan_job: ApplicationScanJob | None = None
        self._automatic_revision = 0
        self._automatic_candidate = None
        self._automatic_candidate_count = 0
        self._automatic_handled_candidate = None
        self._automatic_pending_switch = None
        self._automatic_active_action = None
        self._hidden_to_tray = False
        self._tray_quit_requested = False
        self._initial_quit_policy = QApplication.instance().quitOnLastWindowClosed()
        self._field_loaders: list[Callable] = []
        self._capability_labels: dict[str, QLabel] = {}
        self.setWindowTitle("MC7 Studio — Local draft")
        self.resize(1240, 860)
        self.setMinimumSize(900, 680)
        self.setStyleSheet(STYLE)
        self._build_shell()
        self._build_overview()
        self._build_sensitivity()
        self._build_buttons()
        self._build_lighting()
        self._build_display()
        self._build_macros()
        self._build_profiles()
        self._build_device()
        self.battery_tray = BatteryTray(self)
        self.battery_tray.show_requested.connect(self.show_from_tray)
        self.battery_tray.read_requested.connect(self.refresh_status)
        self.battery_tray.quit_requested.connect(self.quit_from_tray)
        self.status_timer = QTimer(self)
        self.status_timer.setInterval(STATUS_INTERVAL_MS)
        self.status_timer.timeout.connect(self._poll_status)
        self.host_lcd_timer = QTimer(self)
        self.host_lcd_timer.setInterval(2000)
        self.host_lcd_timer.timeout.connect(self._poll_host_lcd)
        self.automatic_profile_timer = QTimer(self)
        self.automatic_profile_timer.setInterval(1800)
        self.automatic_profile_timer.timeout.connect(self._poll_automatic_profile)
        self._sync_automatic_profile_monitor(initial=True)
        self.navigation.setCurrentRow(0)
        self._load_draft()
        self._refresh_presets()
        self._update_capabilities()
        self._shortcuts()
        if callable(getattr(self.service, "gpu_sources", None)):
            QTimer.singleShot(0, self._refresh_gpu_sources)
        if callable(getattr(self.service, "media_players", None)):
            QTimer.singleShot(0, self._refresh_media_players)
        if auto_discover and service is not None:
            QTimer.singleShot(0, self.refresh_device)

    def _build_shell(self):
        root = QWidget()
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(210)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(20, 30, 20, 22)
        side.setSpacing(8)
        side.addWidget(label("MC7 STUDIO", "brand"))
        side.addWidget(label("COMMAND YOUR MOUSE", "eyebrow"))
        side.addSpacing(26)
        self.navigation = QListWidget()
        self.navigation.setObjectName("navigation")
        self.navigation.setAccessibleName("Settings pages")
        self.navigation.addItems([page[0] for page in PAGES])
        self.navigation.currentRowChanged.connect(self._navigate)
        side.addWidget(self.navigation, 1)
        self.connection_badge = label("○  No device read", "muted", True)
        side.addWidget(self.connection_badge)
        side.addSpacing(10)
        side.addWidget(label("Independent software\nLinux · macOS · Windows", "muted", True))
        layout.addWidget(sidebar)
        main = QWidget()
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        header = QWidget()
        heading = QVBoxLayout(header)
        heading.setContentsMargins(34, 28, 34, 20)
        top = QHBoxLayout()
        self.breadcrumb = label("COMMAND SERIES  /  OVERVIEW", "eyebrow")
        top.addWidget(self.breadcrumb, 1)
        self.draft_badge = label("LOCAL DRAFT", "statusBadge")
        top.addWidget(self.draft_badge)
        heading.addLayout(top)
        heading.addSpacing(9)
        self.page_title = label("", "title")
        self.page_subtitle = label("", "subtitle", True)
        heading.addWidget(self.page_title)
        heading.addWidget(self.page_subtitle)
        main_layout.addWidget(header)
        self.stack = QStackedWidget()
        self.page_layouts = []
        for _ in PAGES:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            body = QWidget()
            page_layout = QVBoxLayout(body)
            page_layout.setContentsMargins(34, 8, 34, 24)
            page_layout.setSpacing(18)
            self.page_layouts.append(page_layout)
            scroll.setWidget(body)
            self.stack.addWidget(scroll)
        main_layout.addWidget(self.stack, 1)
        footer = QFrame()
        footer.setObjectName("footer")
        foot = QHBoxLayout(footer)
        foot.setContentsMargins(28, 15, 28, 15)
        self.footer_status = label("Local draft · Not saved yet", "muted", True)
        foot.addWidget(self.footer_status, 1)
        self.read_button = QPushButton("Read mouse")
        self.read_button.clicked.connect(self.read_mouse)
        self.save_button = QPushButton("Save preset")
        self.save_button.clicked.connect(self.save_preset)
        self.apply_button = QPushButton("Apply sensitivity")
        self.apply_button.setObjectName("primary")
        self.apply_button.clicked.connect(self.apply_to_mouse)
        foot.addWidget(self.read_button)
        foot.addWidget(self.save_button)
        foot.addWidget(self.apply_button)
        main_layout.addWidget(footer)
        self.message = label("", "notice", True)
        self.message.setVisible(False)
        main_layout.insertWidget(1, self.message)
        layout.addWidget(main, 1)
        self.setCentralWidget(root)

    def _navigate(self, index: int):
        if index < 0:
            return
        page, title, subtitle = PAGES[index]
        self.stack.setCurrentIndex(index)
        self.breadcrumb.setText(f"COMMAND SERIES  /  {page.upper()}")
        self.page_title.setText(title)
        self.page_subtitle.setText(subtitle)
        if hasattr(self, "capability_summary"):
            self._update_capabilities()

    def _build_overview(self):
        page = self.page_layouts[0]
        hero, body = card()
        row = QHBoxLayout()
        text = QVBoxLayout()
        text.addWidget(label("TURTLE BEACH", "eyebrow"))
        text.addWidget(label("Command Series\nMC7", "heroTitle", True))
        text.addWidget(label("One place for your sensitivity, buttons,\nlighting and local presets.", "muted", True))
        text.addSpacing(20)
        self.overview_connection = label("No device read", "statusBadge")
        text.addWidget(self.overview_connection, 0, Qt.AlignmentFlag.AlignLeft)
        self.overview_battery = label("Battery: not read", "muted", True)
        text.addWidget(self.overview_battery)
        self.overview_firmware = label("Mouse firmware: not read", "muted", True)
        self.overview_firmware.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        text.addWidget(self.overview_firmware)
        text.addSpacing(15)
        configure = QPushButton("Edit sensitivity  →")
        configure.setObjectName("primary")
        configure.clicked.connect(lambda: self.navigation.setCurrentRow(1))
        text.addWidget(configure, 0, Qt.AlignmentFlag.AlignLeft)
        text.addStretch()
        row.addLayout(text, 3)
        row.addWidget(MouseIllustration(), 2)
        body.addLayout(row)
        page.addWidget(hero)
        cards = QHBoxLayout()
        preset, preset_layout = card("Local preset")
        self.overview_preset = label(self.draft.name, "largeValue", True)
        preset_layout.addWidget(self.overview_preset)
        preset_layout.addWidget(label("Save and share a complete setup.", "muted", True))
        sensor, sensor_layout = card("Mouse sensitivity")
        self.overview_dpi = label("Not read", "largeValue")
        sensor_layout.addWidget(self.overview_dpi)
        self.overview_read_status = label("Read the mouse to see its current settings.", "muted", True)
        sensor_layout.addWidget(self.overview_read_status)
        cards.addWidget(preset)
        cards.addWidget(sensor)
        page.addLayout(cards)
        page.addWidget(label("Read your mouse, edit a page, then apply that page's settings. Save preset keeps a copy of your complete setup on this computer.", "notice", True))
        page.addStretch()

    def _notice(self, page: QVBoxLayout, capability: str):
        notice = label("Local preset editing available. Applying this feature to the mouse is not available yet.", "notice", True)
        self._capability_labels[capability] = notice
        page.addWidget(notice)

    def _form(self, page: QVBoxLayout, title: str, description: str = ""):
        frame, body = card(title, description)
        form = QFormLayout()
        form.setHorizontalSpacing(24)
        form.setVerticalSpacing(16)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        body.addLayout(form)
        page.addWidget(frame)
        return form

    def _set(self, section: str, attr: str, value):
        if self._loading:
            return
        setattr(getattr(self.draft, section), attr, value)
        self._changed()

    def _spin(self, form, caption, section, attr, minimum, maximum, suffix="", step=1):
        control = QSpinBox()
        control.setRange(minimum, maximum)
        control.setSingleStep(step)
        control.setSuffix(suffix)
        control.setAccessibleName(caption)
        control.valueChanged.connect(lambda value: self._set(section, attr, value))
        self._field_loaders.append(lambda: control.setValue(getattr(getattr(self.draft, section), attr)))
        form.addRow(caption, control)
        return control

    def _combo(self, form, caption, section, attr, values):
        control = QComboBox()
        for value in values:
            control.addItem(title_case(str(value)), value)
        control.setAccessibleName(caption)
        control.currentIndexChanged.connect(lambda index: self._set(section, attr, control.itemData(index)))
        def load():
            value = getattr(getattr(self.draft, section), attr)
            index = control.findData(value)
            if index < 0:
                control.addItem(f"Saved choice: {title_case(str(value))} (unavailable)", value)
                index = control.count() - 1
            control.setCurrentIndex(index)
        self._field_loaders.append(load)
        form.addRow(caption, control)
        return control

    def _check(self, form, caption, section, attr):
        control = QCheckBox("Enabled")
        control.setAccessibleName(caption)
        control.toggled.connect(lambda checked: self._set(section, attr, checked))
        self._field_loaders.append(lambda: control.setChecked(getattr(getattr(self.draft, section), attr)))
        form.addRow(caption, control)
        return control

    def _build_sensitivity(self):
        page = self.page_layouts[1]
        self._notice(page, "sensitivity")
        frame, body = card("DPI stages", "Read mouse loads the supported settings into this draft. Apply sensitivity sends the DPI stages and supported sensor tuning options.")
        self.dpi_controls = []
        for index in range(5):
            row = QHBoxLayout()
            enabled = QCheckBox(f"Stage {index + 1}")
            enabled.setMinimumWidth(95)
            value = QSpinBox()
            value.setRange(50, 30000)
            value.setSingleStep(50)
            value.setKeyboardTracking(False)
            value.setSuffix(" DPI")
            value.setAccessibleName(f"Stage {index + 1} sensitivity")
            value.setMinimumWidth(130)
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(50, 30000)
            slider.setSingleStep(50)
            slider.setAccessibleName(f"Stage {index + 1} sensitivity slider")
            color = ColorButton()
            value.valueChanged.connect(slider.setValue)
            slider.valueChanged.connect(lambda number, target=value: target.setValue(((number + 25) // 50) * 50))
            value.valueChanged.connect(lambda number, target=value: target.setValue(((number + 25) // 50) * 50))
            value.valueChanged.connect(lambda n, i=index: self._set_stage(i, "value", n))
            enabled.toggled.connect(lambda checked, i=index: self._set_stage(i, "enabled", checked))
            color.colorChanged.connect(lambda name, i=index: self._set_stage(i, "color", name))
            row.addWidget(enabled)
            row.addWidget(slider, 1)
            row.addWidget(value)
            row.addWidget(color)
            body.addLayout(row)
            self.dpi_controls.append((enabled, value, color))
        current = QFormLayout()
        self.current_stage = self._combo(current, "Active stage", "sensor", "current_stage", range(5))
        for index in range(5):
            self.current_stage.setItemText(index, f"Stage {index + 1}")
        body.addLayout(current)
        self._check(current, "DPI indicator", "sensor", "dpi_indicator_enabled")
        page.addWidget(frame)
        tuning = self._form(page, "Sensor tuning", "Adjust tracking and response alongside your DPI stages.")
        self._combo(tuning, "Polling rate (Hz)", "sensor", "polling_rate", [125, 250, 500, 1000, 2000, 4000, 8000])
        self._combo(tuning, "Lift-off distance", "sensor", "lift_off_distance", LIFT_OFF_DISTANCES)
        self.dcu_calibration_button = QPushButton("Calibrate my surface…")
        self.dcu_calibration_button.clicked.connect(self.calibrate_lift_off)
        tuning.addRow("Custom lift-off", self.dcu_calibration_button)
        self._spin(tuning, "Debounce time", "sensor", "debounce_ms", 0, 10, " ms")
        self._check(tuning, "Angle snapping", "sensor", "angle_snapping")
        self._check(tuning, "Angle tuning", "sensor", "angle_tuning_enabled")
        self._spin(tuning, "Angle adjustment", "sensor", "angle_tuning", -30, 30, "°")
        self._check(tuning, "Motion sync", "sensor", "motion_sync")
        frame, body = card("Aim calibration", "Guided pointer exercises suggest DPI and angle settings. Read the active mouse profile first; accepted suggestions stay in your draft until you apply sensitivity.")
        row = QHBoxLayout()
        self.dpi_calibration_button = QPushButton("Find a comfortable DPI…")
        self.dpi_calibration_button.clicked.connect(self.calibrate_dpi)
        self.angle_calibration_button = QPushButton("Align my angle…")
        self.angle_calibration_button.clicked.connect(self.calibrate_angle)
        row.addWidget(self.dpi_calibration_button)
        row.addWidget(self.angle_calibration_button)
        body.addLayout(row)
        body.addWidget(label("These exercises use desktop pointer movement. They do not measure physical sensor resolution. Use Calibrate my surface above for the mouse's custom lift-off workflow.", "muted", True))
        page.addWidget(frame)
        self.current_stage.currentIndexChanged.connect(lambda _index: self._update_capabilities())
        page.addStretch()

    def calibrate_lift_off(self):
        if not self.dcu_calibration_button.isEnabled() or self._device_or_dialog_busy():
            return
        dialog = self._dcu_dialog = DcuCalibrationDialog(self.selected_device_id, self)
        self._update_capabilities()
        try:
            dialog.exec()
            outcome = dialog.outcome or {}
            changed = dialog.device_may_have_changed
        finally:
            self._dcu_dialog = None
            dialog.deleteLater()
            self._update_capabilities()
        if not changed:
            return
        self.host_lcd_checkbox.setChecked(False)
        self.snapshot = None
        self._update_capabilities()
        if outcome.get("verified") and outcome.get("outcome") in ("committed", "cancelled"):
            if outcome["outcome"] == "committed":
                self.draft.sensor.lift_off_distance = "custom"
                self._load_draft()
                self._changed()
            device_id, slot = self.selected_device_id, self.draft.profile_slot
            self._run_job(lambda: self.service.read(device_id, slot), self._dcu_refreshed,
                          "Refreshing the mouse after surface calibration…")
        else:
            self._notify(outcome.get("error", "Calibration could not be verified. Read the mouse again before applying settings."), error=True)

    def _dcu_refreshed(self, snapshot):
        # Refresh the device baseline only; retain all pending local edits.
        self.snapshot = snapshot
        self._notify("Surface calibration finished and the mouse was read again. Your other draft edits are preserved.")

    def _calibration_sensor(self, kind):
        snapshot = self.snapshot or {}
        if (snapshot.get("device_id") != self.selected_device_id
                or snapshot.get("profile_slot") != self.draft.profile_slot
                or snapshot.get("summary", {}).get("active_profile") != self.draft.profile_slot):
            return None
        needed = {"sensor.stages", "sensor.current_stage"} if kind == "dpi" else {
            "sensor.angle_tuning", "sensor.angle_tuning_enabled", "sensor.angle_snapping"}
        if not needed.issubset(snapshot.get("verified_fields", [])):
            return None
        received = snapshot.get("configuration")
        if isinstance(received, dict):
            received = Configuration.from_dict(received)
        if received is None:
            return None
        sensor = received.sensor
        if kind == "dpi" and self.draft.sensor.current_stage != sensor.current_stage:
            return None
        return sensor

    def calibrate_dpi(self):
        sensor = self._calibration_sensor("dpi")
        if self._job is not None or sensor is None:
            self._notify("Read the active mouse profile and select its active DPI stage before calibration.", error=True)
            return
        stage = sensor.current_stage
        dialog = DpiCalibrationDialog(self, current_dpi=sensor.stages[stage].value, settings_verified=True)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        suggested = dialog.suggested_dpi
        dialog.deleteLater()
        if accepted and suggested is not None:
            self.draft.sensor.stages[stage].value = suggested
            self._load_draft()
            self._changed()
            self._notify(f"Suggested {suggested} DPI staged for stage {stage+1}. Apply sensitivity to send it to the mouse.")

    def calibrate_angle(self):
        sensor = self._calibration_sensor("angle")
        if self._job is not None or sensor is None:
            self._notify("Read the active mouse profile before angle calibration.", error=True)
            return
        dialog = AngleCalibrationDialog(self, current_angle=sensor.angle_tuning,
            angle_enabled=sensor.angle_tuning_enabled, angle_snapping=sensor.angle_snapping,
            settings_verified=True)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        suggested = dialog.suggested_angle
        dialog.deleteLater()
        if accepted and suggested is not None:
            self.draft.sensor.angle_tuning = suggested
            self.draft.sensor.angle_tuning_enabled = True
            self._load_draft()
            self._changed()
            self._notify(f"Suggested angle {suggested:+d}° staged in your draft. Apply sensitivity to send it to the mouse.")

    def _set_stage(self, index, attribute, value):
        if self._loading:
            return
        if attribute == "value":
            value = max(50, min(30000, ((value + 25) // 50) * 50))
        stage = self.draft.sensor.stages[index]
        if attribute == "enabled" and not value:
            if sum(item.enabled for item in self.draft.sensor.stages) == 1:
                self.dpi_controls[index][0].setChecked(True)
                self._notify("Keep at least one DPI stage enabled.")
                return
            if self.draft.sensor.current_stage == index:
                self.current_stage.setCurrentIndex(next(i for i, item in enumerate(self.draft.sensor.stages) if i != index and item.enabled))
        setattr(stage, attribute, value)
        self.current_stage.model().item(index).setEnabled(stage.enabled)
        self._changed()

    def _build_buttons(self):
        page = self.page_layouts[2]
        self._notice(page, "buttons")
        frame, body = card("Button assignments", "Choose an action on either layer. Existing actions that are not editable are preserved. Assign supported keyboard and mouse macros here, then Apply buttons.")
        self.button_table = QTableWidget(0, 3)
        self.button_table.setHorizontalHeaderLabels(["Mouse control", "Standard action", "Easy-Shift action"])
        self.button_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.button_table.verticalHeader().setVisible(False)
        self.button_table.setMinimumHeight(500)
        body.addWidget(self.button_table)
        page.addWidget(frame)
        page.addStretch()

    def _action_picker(self, binding, attr):
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(4, 4, 4, 4)
        combo = QComboBox()
        action = getattr(binding, attr)
        for kind, values in ACTION_VALUES.items():
            if kind in ("keyboard", "macro"):
                continue
            for value in values:
                caption = "Disabled" if kind == "disabled" else f"{title_case(kind)} · {title_case(value)}"
                if (kind, value) == ("dpi", "precision"):
                    caption = "DPI · Easy-Aim 200 DPI"
                combo.addItem(caption, (kind, value))
            if kind == "dpi":
                custom_dpi = custom_easy_aim_dpi(action)
                if custom_dpi is not None:
                    combo.addItem(
                        f"DPI · Easy-Aim {custom_dpi:,} DPI",
                        ("dpi", action.value),
                    )
                combo.addItem("DPI · Easy-Aim custom…", CUSTOM_EASY_AIM_PICKER)
        combo.addItem("Keyboard shortcut…", ("keyboard", ""))
        for macro in self.draft.macros:
            combo.addItem(f"Macro · {macro.name}", ("macro", macro.id))
        # QVariant cannot compare the Python tuple values stored in these
        # entries through findData(). Compare their Python values explicitly.
        selected = next((index for index in range(combo.count())
                         if combo.itemData(index) == (action.kind, action.value)), -1)
        if action.kind == "device":
            combo.addItem("On-device action (preserved)", (action.kind, action.value))
            selected = combo.count() - 1
        if selected < 0 and action.kind == "keyboard":
            combo.addItem(f"Keyboard · {action.value}", (action.kind, action.value))
            selected = combo.count() - 1
        elif selected < 0:
            combo.addItem("Saved action (unavailable)", (action.kind, action.value))
            selected = combo.count() - 1
        combo.setCurrentIndex(selected)
        combo.setAccessibleName(f"Button {binding.button_id} {attr.replace('_', ' ')} action")

        def choose(index):
            kind, value = combo.itemData(index)
            if (kind, value) == CUSTOM_EASY_AIM_PICKER:
                current = custom_easy_aim_dpi(getattr(binding, attr)) or 1600
                dpi, accepted = QInputDialog.getInt(
                    self, "Custom Easy-Aim", "DPI (50–30,000, in steps of 50):",
                    current, 50, 30000, 50,
                )
                if not accepted:
                    self._load_buttons()
                    return
                dpi = max(50, min(30000, ((dpi + 25) // 50) * 50))
                value = f"{CUSTOM_PRECISION_PREFIX}{dpi}"
            if kind == "keyboard" and not value:
                value, accepted = QInputDialog.getText(self, "Keyboard shortcut", "Shortcut, for example Ctrl+S or F5:")
                if not accepted or not value.strip():
                    self._load_buttons()
                    return
            setattr(binding, attr, Action(kind=kind, value=value.strip()))
            self._changed()
            if kind == "keyboard" or value.startswith(CUSTOM_PRECISION_PREFIX):
                self._load_buttons()

        combo.currentIndexChanged.connect(choose)
        row.addWidget(combo)
        return container

    def _load_buttons(self):
        self.button_table.setRowCount(len(self.draft.buttons))
        for row, binding in enumerate(self.draft.buttons):
            index = binding.button_id - 1
            name = BUTTON_LABELS[index] if 0 <= index < len(BUTTON_LABELS) else f"Button {binding.button_id:02d}"
            item = QTableWidgetItem(name)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.button_table.setItem(row, 0, item)
            self.button_table.setCellWidget(row, 1, self._action_picker(binding, "primary"))
            self.button_table.setCellWidget(row, 2, self._action_picker(binding, "easy_shift"))
            self.button_table.setRowHeight(row, 52)

    def _build_lighting(self):
        page = self.page_layouts[3]
        self._notice(page, "lighting")
        form = self._form(page, "Lighting style")
        self._combo(form, "Effect", "lighting", "effect", tuple(LIGHTING_EFFECT_IDS))
        self.light_color = ColorButton()
        self.light_color.colorChanged.connect(lambda color: self._set("lighting", "color", color))
        self._field_loaders.append(lambda: self.light_color.set_color(self.draft.lighting.color))
        form.addRow("Accent color", self.light_color)
        self._spin(form, "Brightness", "lighting", "brightness", 0, 100, "%")
        self._spin(form, "Effect speed", "lighting", "speed", 10, 100, "%", step=10)
        page.addStretch()

    def _build_display(self):
        page = self.page_layouts[4]
        self._notice(page, "display")
        form = self._form(page, "Mouse display")
        self._combo(form, "Brightness (%)", "display", "brightness", SCREEN_BRIGHTNESS_LEVELS)
        self.screen_timeout = self._combo(
            form, "Screen timeout (minutes)", "display", "timeout_value",
            SCREEN_TIMEOUT_VALUES,
        )
        for index, value in enumerate(SCREEN_TIMEOUT_VALUES):
            self.screen_timeout.setItemText(index, f"{value} min")
        self.screen_timeout.setToolTip(
            "Swarm II sends these minute values unchanged. The physical meaning "
            "of the vendor's 0-minute value still needs a timed device test."
        )
        self._combo(form, "Haptic feedback", "display", "haptic_intensity", HAPTIC_INTENSITIES)
        self.background_selection = self._combo(form, "Active background (all profiles)", "display", "background_index", (None, 0, 1))
        for index, text in enumerate(("Keep current background", "Built-in background", "Stored custom background")):
            self.background_selection.setItemText(index, text)
        self.background_selection.setToolTip("Selects an existing background when you Apply display settings. This does not upload or erase image data; a custom image must already be stored on the mouse.")
        self.activate_display_button = QPushButton("Set up LCD")
        self.activate_display_button.clicked.connect(self.setup_display)
        self.activate_display_button.setToolTip("Replace the Download Swarm II tile with useful onboard controls. Other tiles and pages are preserved.")
        form.addRow("Download Swarm II prompt", self.activate_display_button)
        frame, body = card("LCD pages", "Choose the four slots on each onboard page. Key tiles also need a key or shortcut. Wide controls occupy three slots. Apply display settings writes this layout to the mouse.")
        self.lcd_status = label("Read the mouse to load its LCD pages, or import a preset with a layout.", "muted", True)
        body.addWidget(self.lcd_status)
        self.lcd_tabs = QTabWidget()
        self.lcd_tabs.setAccessibleName("Mouse LCD pages")
        self.lcd_controls = []
        self.lcd_key_controls = []
        self.lcd_macro_controls = []
        self.lcd_timer_controls = []
        self.lcd_host_action_editors = []
        self.lcd_host_action_browse_buttons = []
        self.lcd_host_action_icon_previews = []
        self.lcd_host_action_icon_states = []
        self.lcd_host_action_icon_auto_buttons = []
        self.lcd_host_action_icon_browse_buttons = []
        self.lcd_host_action_icon_clear_buttons = []
        for page_index in range(3):
            tab = QWidget()
            rows = QFormLayout(tab)
            rows.setContentsMargins(18, 22, 18, 20)
            rows.setVerticalSpacing(14)
            rows.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            controls = []
            key_controls = []
            macro_controls = []
            timer_controls = []
            host_action_editors = []
            host_action_browse_buttons = []
            host_action_icon_previews = []
            host_action_icon_states = []
            host_action_icon_auto_buttons = []
            host_action_icon_browse_buttons = []
            host_action_icon_clear_buttons = []
            for slot in range(4):
                field = QWidget()
                field_layout = QVBoxLayout(field)
                field_layout.setContentsMargins(0, 0, 0, 0)
                field_layout.setSpacing(6)
                control_layout = QHBoxLayout()
                control_layout.setContentsMargins(0, 0, 0, 0)
                control_layout.setSpacing(10)
                icon_layout = QHBoxLayout()
                icon_layout.setContentsMargins(0, 0, 0, 0)
                icon_layout.setSpacing(10)
                control = QComboBox()
                control.setAccessibleName(f"LCD page {page_index + 1}, slot {slot + 1}")
                control.currentIndexChanged.connect(lambda index, p=page_index, s=slot, c=control: self._lcd_slot_changed(p, s, c.itemData(index)))
                key_control = QLineEdit()
                key_control.setMaxLength(128)
                key_control.setAccessibleName(f"LCD page {page_index + 1}, slot {slot + 1} key binding")
                key_control.textChanged.connect(lambda text, p=page_index, s=slot: self._lcd_key_changed(p, s, text))
                macro_control = QComboBox()
                macro_control.setAccessibleName(f"LCD page {page_index + 1}, slot {slot + 1} macro")
                macro_control.currentIndexChanged.connect(
                    lambda index, p=page_index, s=slot, c=macro_control:
                    self._lcd_macro_changed(p, s, c.itemData(index)))
                timer_control = QComboBox()
                timer_control.setAccessibleName(
                    f"LCD page {page_index + 1}, slot {slot + 1} count down timer")
                timer_control.currentIndexChanged.connect(
                    lambda index, p=page_index, s=slot, c=timer_control:
                    self._lcd_timer_changed(p, s, c.itemData(index)))
                host_action_editor = QLineEdit()
                host_action_editor.setMaxLength(4096)
                host_action_editor.setAccessibleName(
                    f"LCD page {page_index + 1}, slot {slot + 1} launch target")
                host_action_editor.textChanged.connect(
                    lambda text, p=page_index, s=slot:
                    self._lcd_host_action_changed(p, s, text))
                host_action_browse = QPushButton("Choose…")
                host_action_browse.setAccessibleName(
                    f"Choose LCD page {page_index + 1}, slot {slot + 1} launch target")
                host_action_browse.clicked.connect(
                    lambda checked=False, p=page_index, s=slot:
                    self._choose_lcd_host_action_target(p, s))
                host_action_icon_preview = QLabel("No icon")
                host_action_icon_preview.setFixedSize(QSize(64, 62))
                host_action_icon_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
                host_action_icon_preview.setAccessibleName(
                    f"LCD page {page_index + 1}, slot {slot + 1} application icon preview")
                host_action_icon_state = QLabel("Choose an application")
                host_action_icon_state.setAccessibleName(
                    f"LCD page {page_index + 1}, slot {slot + 1} application icon status")
                host_action_icon_auto = QPushButton("Use app icon")
                host_action_icon_auto.setAccessibleName(
                    f"Use LCD page {page_index + 1}, slot {slot + 1} application icon")
                host_action_icon_auto.clicked.connect(
                    lambda checked=False, p=page_index, s=slot:
                    self._use_lcd_application_icon(p, s))
                host_action_icon_browse = QPushButton("Custom icon…")
                host_action_icon_browse.setAccessibleName(
                    f"Choose LCD page {page_index + 1}, slot {slot + 1} custom icon")
                host_action_icon_browse.clicked.connect(
                    lambda checked=False, p=page_index, s=slot:
                    self._choose_lcd_application_icon(p, s))
                host_action_icon_clear = QPushButton("Clear icon")
                host_action_icon_clear.setAccessibleName(
                    f"Clear LCD page {page_index + 1}, slot {slot + 1} application icon")
                host_action_icon_clear.clicked.connect(
                    lambda checked=False, p=page_index, s=slot:
                    self._clear_lcd_application_icon(p, s))
                control_layout.addWidget(control, 2)
                control_layout.addWidget(key_control, 1)
                control_layout.addWidget(macro_control, 1)
                control_layout.addWidget(timer_control, 1)
                control_layout.addWidget(host_action_editor, 1)
                control_layout.addWidget(host_action_browse)
                icon_layout.addWidget(host_action_icon_preview)
                icon_layout.addWidget(host_action_icon_state)
                icon_layout.addStretch()
                icon_layout.addWidget(host_action_icon_auto)
                icon_layout.addWidget(host_action_icon_browse)
                icon_layout.addWidget(host_action_icon_clear)
                field_layout.addLayout(control_layout)
                field_layout.addLayout(icon_layout)
                rows.addRow(f"Slot {slot + 1}", field)
                controls.append(control)
                key_controls.append(key_control)
                macro_controls.append(macro_control)
                timer_controls.append(timer_control)
                host_action_editors.append(host_action_editor)
                host_action_browse_buttons.append(host_action_browse)
                host_action_icon_previews.append(host_action_icon_preview)
                host_action_icon_states.append(host_action_icon_state)
                host_action_icon_auto_buttons.append(host_action_icon_auto)
                host_action_icon_browse_buttons.append(host_action_icon_browse)
                host_action_icon_clear_buttons.append(host_action_icon_clear)
            self.lcd_controls.append(controls)
            self.lcd_key_controls.append(key_controls)
            self.lcd_macro_controls.append(macro_controls)
            self.lcd_timer_controls.append(timer_controls)
            self.lcd_host_action_editors.append(host_action_editors)
            self.lcd_host_action_browse_buttons.append(host_action_browse_buttons)
            self.lcd_host_action_icon_previews.append(host_action_icon_previews)
            self.lcd_host_action_icon_states.append(host_action_icon_states)
            self.lcd_host_action_icon_auto_buttons.append(
                host_action_icon_auto_buttons)
            self.lcd_host_action_icon_browse_buttons.append(
                host_action_icon_browse_buttons)
            self.lcd_host_action_icon_clear_buttons.append(
                host_action_icon_clear_buttons)
            self.lcd_tabs.addTab(tab, f"Page {page_index + 1}")
        body.addWidget(self.lcd_tabs)
        order = QHBoxLayout()
        self.lcd_move_left = QPushButton("Move page left")
        self.lcd_move_right = QPushButton("Move page right")
        self.lcd_move_left.clicked.connect(lambda: self._move_lcd_page(-1))
        self.lcd_move_right.clicked.connect(lambda: self._move_lcd_page(1))
        order.addWidget(self.lcd_move_left)
        order.addWidget(self.lcd_move_right)
        order.addStretch()
        body.addLayout(order)
        self.lcd_tabs.currentChanged.connect(self._update_lcd_moves)
        body.addWidget(label("Key, media, editing and Macro tiles send input to the computer; their behavior depends on the keyboard layout and focused application. General Media, Launch OBS, OBS Screenshot, OBS Studio Mode, and application, website, file and folder tiles need the LCD action listener while MC7 Studio is running. Apply display settings to store the selected tiles and application icons on the mouse.", "muted", True))
        page.addWidget(frame)
        frame, body = card(
            "Count down timer library",
            "Create timers from 1 to 600 seconds, then assign one to each Count down timer LCD tile. Timer definitions and assignments are saved with this preset.")
        toolbar = QHBoxLayout()
        self.countdown_timer_selector = QComboBox()
        self.countdown_timer_selector.setPlaceholderText("No count down timers yet")
        self.countdown_timer_selector.setAccessibleName("Select a count down timer")
        self.countdown_timer_selector.currentIndexChanged.connect(
            self._load_countdown_timer)
        toolbar.addWidget(self.countdown_timer_selector, 1)
        self.new_countdown_timer_button = QPushButton("New")
        self.new_countdown_timer_button.clicked.connect(self._new_countdown_timer)
        toolbar.addWidget(self.new_countdown_timer_button)
        self.delete_countdown_timer_button = QPushButton("Delete")
        self.delete_countdown_timer_button.clicked.connect(self._delete_countdown_timer)
        toolbar.addWidget(self.delete_countdown_timer_button)
        body.addLayout(toolbar)
        timer_form = QFormLayout()
        self.countdown_timer_name = QLineEdit()
        self.countdown_timer_name.setMaxLength(80)
        self.countdown_timer_name.setAccessibleName("Count down timer name")
        self.countdown_timer_name.editingFinished.connect(self._edit_countdown_timer)
        self.countdown_timer_duration = QSpinBox()
        self.countdown_timer_duration.setRange(1, 600)
        self.countdown_timer_duration.setSuffix(" sec")
        self.countdown_timer_duration.setAccessibleName("Count down timer duration in seconds")
        self.countdown_timer_duration.valueChanged.connect(self._edit_countdown_timer)
        timer_form.addRow("Name", self.countdown_timer_name)
        timer_form.addRow("Duration", self.countdown_timer_duration)
        body.addLayout(timer_form)
        page.addWidget(frame)
        frame, body = card(
            "Live LCD actions",
            "Apply Count down timer, General Media, Open application, Open website, Open file or Open folder tiles to the active mouse profile, then keep MC7 Studio running to handle their taps.")
        media_player_row = QHBoxLayout()
        self.media_player_selector = QComboBox()
        self.media_player_selector.setAccessibleName(
            "Preferred General Media player for this profile")
        self.media_player_selector.addItem(
            "Automatic (only unambiguous player)", None)
        self.media_player_selector.currentIndexChanged.connect(
            self._media_player_changed)
        media_player_row.addWidget(self.media_player_selector, 1)
        self.media_player_refresh_button = QPushButton("Refresh players")
        self.media_player_refresh_button.clicked.connect(
            self._refresh_media_players)
        self.media_player_refresh_button.setEnabled(
            callable(getattr(self.service, "media_players", None)))
        media_player_row.addWidget(self.media_player_refresh_button)
        body.addWidget(label(
            "Preferred General Media player for this profile. Automatic refuses ambiguous idle players.",
            "muted", True))
        body.addLayout(media_player_row)
        self.media_player_status = label(
            "Media player discovery has not run yet.", "muted", True)
        self.media_player_status.setAccessibleName(
            "General Media player selection status")
        body.addWidget(self.media_player_status)
        self.countdown_checkbox = QCheckBox(
            "Listen for timer, media and launch taps on the mouse")
        self.countdown_checkbox.toggled.connect(self._toggle_countdown)
        body.addWidget(self.countdown_checkbox)
        self.countdown_status = label(
            "Stopped. Read the active profile after applying every timer, media or launch tile.",
            "muted", True)
        self.countdown_status.setAccessibleName("LCD action listener status")
        body.addWidget(self.countdown_status)
        body.addWidget(label(
            "The listener owns the mouse connection while enabled, so settings, firmware, live system values and automatic profile changes pause until it stops.",
            "muted", True))
        page.addWidget(frame)
        frame, body = card("Live system monitoring", "Add a CPU, GPU, temperature or RAM widget to an LCD slot and Apply display settings. Live updates run while MC7 Studio is open and this mouse profile is active.")
        gpu_source_row = QHBoxLayout()
        self.gpu_source_selector = QComboBox()
        self.gpu_source_selector.setAccessibleName("GPU telemetry source")
        self.gpu_source_selector.addItem("Automatic (system primary)", None)
        if self._gpu_source is not None:
            self.gpu_source_selector.addItem(
                f"Saved GPU · {self._gpu_source} · checking…", self._gpu_source)
            self.gpu_source_selector.setCurrentIndex(1)
        self.gpu_source_selector.currentIndexChanged.connect(
            self._gpu_source_changed)
        gpu_source_row.addWidget(self.gpu_source_selector, 1)
        self.gpu_source_refresh_button = QPushButton("Refresh GPUs")
        self.gpu_source_refresh_button.clicked.connect(self._refresh_gpu_sources)
        self.gpu_source_refresh_button.setEnabled(
            callable(getattr(self.service, "gpu_sources", None)))
        gpu_source_row.addWidget(self.gpu_source_refresh_button)
        body.addWidget(label(
            "GPU source affects GPU load and temperature widgets. Automatic uses a unique system primary adapter.",
            "muted", True))
        body.addLayout(gpu_source_row)
        self.gpu_source_status = label(
            "GPU source discovery has not run yet.", "muted", True)
        self.gpu_source_status.setAccessibleName("GPU telemetry source status")
        body.addWidget(self.gpu_source_status)
        self.host_lcd_checkbox = QCheckBox("Send live system values to the mouse every 2 seconds")
        self.host_lcd_checkbox.toggled.connect(self._toggle_host_lcd)
        body.addWidget(self.host_lcd_checkbox)
        self.host_lcd_status = label("Stopped. Enable after applying a system monitoring widget to the active profile.", "muted", True)
        body.addWidget(self.host_lcd_status)
        body.addWidget(label("Updates pause during other mouse operations and dialogs. Values may remain on the mouse after stopping. This option resets when you quit.", "muted", True))
        page.addWidget(frame)
        frame, body = card("Background image", "Preview a PNG or JPEG fitted to the mouse display, then choose Upload background to send it. Background images are separate from saved presets.")
        self.background_button = QPushButton("Choose background…")
        self.background_button.clicked.connect(self.choose_background)
        body.addWidget(self.background_button, 0, Qt.AlignmentFlag.AlignLeft)
        page.addWidget(frame)
        page.addStretch()

    def _countdown_start_request(self):
        device = next(
            (item for item in self.devices if item["id"] == self.selected_device_id),
            {},
        )
        snapshot = self.snapshot
        if ("countdown" not in device.get("capabilities", [])
                or not snapshot
                or snapshot.get("device_id") != self.selected_device_id
                or snapshot.get("profile_slot") != self.draft.profile_slot
                or snapshot.get("summary", {}).get("active_profile") != self.draft.profile_slot):
            return None, "Read the active mouse profile first."
        baseline = snapshot.get("baseline")
        if not isinstance(baseline, dict):
            return None, "Read the active mouse profile first."
        try:
            from ..lcd_commands import decode_lcd_response
            baseline_settings = baseline["settings"]
            lcd = decode_lcd_response(
                bytes.fromhex(baseline_settings["lcd"]),
                self.draft.profile_slot - 1,
            )
        except (KeyError, TypeError, ValueError):
            return None, "Read the active profile and LCD layout first."
        hardware_pages = [
            [widget.key if widget is not None else None for widget in page.slots]
            for page in lcd.pages[:min(3, lcd.page_count)]
        ]
        if self.draft.display.pages != hardware_pages:
            return None, "Apply the current LCD layout, then read this profile again."
        timers = {timer.id: timer for timer in self.draft.countdown_timers}
        bindings = []
        host_action_bindings = []
        general_media_bindings = []
        obs_launch_bindings = []
        obs_screenshot_bindings = []
        obs_studio_mode_bindings = []
        for page_index, page in enumerate(hardware_pages):
            binding_row = (self.draft.display.timer_bindings[page_index]
                           if page_index < len(self.draft.display.timer_bindings)
                           else ())
            host_action_row = (
                self.draft.display.host_action_bindings[page_index]
                if page_index < len(self.draft.display.host_action_bindings)
                else ())
            host_action_icon_row = (
                self.draft.display.host_action_icon_bindings[page_index]
                if page_index < len(
                    self.draft.display.host_action_icon_bindings)
                else ())
            for slot_index, widget in enumerate(page):
                if widget == "countdown":
                    timer_id = (binding_row[slot_index]
                                if slot_index < len(binding_row) else None)
                    timer = timers.get(timer_id)
                    if timer is None:
                        return None, "Assign every Count down timer tile to a timer."
                    bindings.append({
                        "timer_id": timer.id,
                        "page_index": page_index,
                        "slot_index": slot_index,
                        "duration_seconds": timer.duration_seconds,
                    })
                elif widget in LCD_HOST_ACTION_WIDGETS:
                    target = (host_action_row[slot_index]
                              if slot_index < len(host_action_row) else None)
                    if target is None:
                        return None, (
                            "Assign a target to every application, website, file and folder tile.")
                    item = {
                        "widget_key": widget,
                        "page_index": page_index,
                        "slot_index": slot_index,
                        "target": target,
                    }
                    if (widget == "open_application"
                            and (slot_index >= len(host_action_icon_row)
                                 or host_action_icon_row[slot_index] is None)):
                        return None, (
                            "Choose an icon for every Open Application tile.")
                    host_action_bindings.append(item)
                elif widget == "general_media":
                    general_media_bindings.append({
                        "page_index": page_index,
                        "slot_index": slot_index,
                    })
                elif widget == "launch_obs":
                    obs_launch_bindings.append({
                        "page_index": page_index,
                        "slot_index": slot_index,
                    })
                elif widget == "obs_screenshot":
                    obs_screenshot_bindings.append({
                        "page_index": page_index,
                        "slot_index": slot_index,
                    })
                elif widget == "obs_studio_mode":
                    obs_studio_mode_bindings.append({
                        "page_index": page_index,
                        "slot_index": slot_index,
                    })
        if (not bindings and not host_action_bindings
                and not general_media_bindings and not obs_launch_bindings
                and not obs_screenshot_bindings
                and not obs_studio_mode_bindings):
            return None, (
                "Add and apply a timer, General Media or host action tile first."
            )
        listener_settings = {
            "profile": baseline_settings.get("profile"),
            "lcd": baseline_settings["lcd"],
        }
        if host_action_bindings:
            screen_keys = baseline_settings.get("screen_keys")
            if not isinstance(screen_keys, str) or not screen_keys:
                return None, (
                    "Read the active profile and its LCD action records first.")
            try:
                from ..custom_icons import custom_icon_reference
                from ..screen_key_commands import decode_screen_key_responses
                screen_key_state = decode_screen_key_responses(
                    bytes.fromhex(screen_keys), self.draft.profile_slot - 1)
                for binding in host_action_bindings:
                    if binding["widget_key"] != "open_application":
                        continue
                    icon_index = custom_icon_reference(
                        screen_key_state.pages[
                            binding["page_index"]].records[
                                binding["slot_index"]])
                    if icon_index is None:
                        return None, (
                            "Apply the Open Application icon, then read this profile again.")
                    binding["icon_index"] = icon_index
            except (IndexError, TypeError, ValueError):
                return None, (
                    "Read the active profile and its LCD action records first.")
            listener_settings["screen_keys"] = screen_keys
        countdown_baseline = {
            "device_id": baseline.get("device_id"),
            "profile_slot": baseline.get("profile_slot"),
            "transport_identity": baseline.get(
                "transport_identity", self.selected_device_id),
            "settings": listener_settings,
        }
        request = {
            "command": "start",
            "device_id": self.selected_device_id,
            "profile_slot": self.draft.profile_slot,
            "baseline": countdown_baseline,
            "bindings": bindings,
            "host_action_bindings": host_action_bindings,
            "general_media_bindings": general_media_bindings,
        }
        if obs_launch_bindings:
            request["obs_launch_bindings"] = obs_launch_bindings
        if obs_screenshot_bindings:
            request["obs_screenshot_bindings"] = obs_screenshot_bindings
        if obs_studio_mode_bindings:
            request["obs_studio_mode_bindings"] = obs_studio_mode_bindings
        if (general_media_bindings
                and self.draft.display.preferred_media_player is not None):
            request["preferred_media_player"] = (
                self.draft.display.preferred_media_player)
        try:
            from ..countdown import validate_countdown_request
            validate_countdown_request(request)
        except (TypeError, ValueError, RuntimeError) as error:
            return None, str(error)
        return request, ""

    def _countdown_ready(self):
        request, _ = self._countdown_start_request()
        return request

    def _toggle_countdown(self, enabled):
        if enabled:
            if self._device_or_dialog_busy():
                self._countdown_unavailable("Wait for the current mouse operation to finish.")
                return
            request, error = self._countdown_start_request()
            if request is None:
                self._countdown_unavailable(error)
                return
            if self.host_lcd_checkbox.isChecked():
                self.host_lcd_checkbox.setChecked(False)
            self._start_countdown_process(request)
        elif self._countdown_process is not None:
            self._request_countdown_stop("Stopped by the user.")
        else:
            self.countdown_status.setText(
                "Stopped. Read the active profile after applying every timer, media or launch tile.")

    def _countdown_unavailable(self, message):
        self.countdown_checkbox.blockSignals(True)
        self.countdown_checkbox.setChecked(False)
        self.countdown_checkbox.blockSignals(False)
        self.countdown_status.setText("LCD actions unavailable: " + message)

    def _start_countdown_process(self, request):
        try:
            process = self._countdown_process_factory(self)
            process.setProcessChannelMode(
                QProcess.ProcessChannelMode.SeparateChannels)
            process.started.connect(
                lambda current=process: self._countdown_process_started(current))
            process.readyReadStandardOutput.connect(
                lambda current=process: self._countdown_output_ready(current))
            process.readyReadStandardError.connect(
                lambda current=process: self._countdown_error_ready(current))
            process.errorOccurred.connect(
                lambda error, current=process:
                self._countdown_process_error(current, error))
            process.finished.connect(
                lambda exit_code, exit_status, current=process:
                self._countdown_process_finished(current, exit_code, exit_status))
        except Exception as error:
            self._countdown_unavailable(f"The helper could not be created: {error}")
            return
        self._countdown_process = process
        self._countdown_request = request
        self._countdown_stdout.clear()
        self._countdown_stderr.clear()
        self._countdown_ready_seen = False
        self._countdown_stopped_seen = False
        self._countdown_running_ids.clear()
        self._countdown_remaining.clear()
        self._countdown_result = None
        self._countdown_local_abort = False
        self._countdown_stop_requested = False
        self._countdown_stop_written = False
        self._countdown_stop_reason = ""
        self.countdown_status.setText("Starting LCD actions…")
        self._update_capabilities()
        try:
            command = helper_command("swarm2.countdown")
            process.start(command[0], list(command[1:]))
        except Exception as error:
            self._countdown_abort(
                f"The helper could not be started: {error}", process)
            self._countdown_process_finished(
                process, -1, QProcess.ExitStatus.CrashExit)

    def _countdown_process_started(self, process):
        if (self._countdown_process is not process
                or self._countdown_request is None):
            return
        payload = (json.dumps(self._countdown_request, separators=(",", ":"))
                   + "\n").encode("utf-8")
        if process.write(payload) != len(payload):
            self._countdown_abort(
                "The helper input pipe could not be written completely.", process)
            return
        if self._countdown_stop_requested:
            self._write_countdown_stop(process)

    def _request_countdown_stop(self, reason=""):
        process = self._countdown_process
        if process is None:
            return
        if reason:
            self._countdown_stop_reason = reason
        if self._countdown_stop_requested:
            return
        self._countdown_stop_requested = True
        self.countdown_checkbox.blockSignals(True)
        self.countdown_checkbox.setChecked(False)
        self.countdown_checkbox.blockSignals(False)
        self.countdown_status.setText("Stopping LCD actions…")
        if process.state() == QProcess.ProcessState.Running:
            self._write_countdown_stop(process)
        QTimer.singleShot(3000, lambda current=process:
                          self._kill_stuck_countdown(current))

    def _write_countdown_stop(self, process):
        if (self._countdown_process is not process
                or self._countdown_stop_written):
            return
        payload = b'{"command":"stop"}\n'
        self._countdown_stop_written = True
        if process.write(payload) != len(payload):
            self._countdown_abort(
                "The helper stop command could not be written completely.",
                process)
            return
        process.closeWriteChannel()

    def _kill_stuck_countdown(self, process):
        if (self._countdown_process is process
                and process.state() != QProcess.ProcessState.NotRunning):
            process.kill()

    def _countdown_output_ready(self, process):
        if self._countdown_process is not process:
            return
        self._countdown_stdout.extend(bytes(process.readAllStandardOutput()))
        if len(self._countdown_stdout) > 65536:
            self._countdown_abort("The helper returned too much output.", process)
            return
        while b"\n" in self._countdown_stdout:
            line, _, rest = self._countdown_stdout.partition(b"\n")
            self._countdown_stdout = bytearray(rest)
            if len(line) > 16384:
                self._countdown_abort(
                    "The helper returned an oversized message.", process)
                return
            try:
                def object_pairs(pairs):
                    value = {}
                    for key, item in pairs:
                        if key in value:
                            raise ValueError("duplicate field")
                        value[key] = item
                    return value

                def invalid_constant(_value):
                    raise ValueError("invalid number")

                message = json.loads(
                    line.decode("utf-8"), object_pairs_hook=object_pairs,
                    parse_constant=invalid_constant)
            except (UnicodeError, ValueError, RecursionError):
                self._countdown_abort(
                    "The helper returned an invalid message.", process)
                return
            if not isinstance(message, dict):
                self._countdown_abort(
                    "The helper returned an invalid message.", process)
                return
            if not self._countdown_message(message, process):
                return
            if self._countdown_process is not process:
                return

    def _countdown_error_ready(self, process):
        if self._countdown_process is not process:
            return
        self._countdown_stderr.extend(bytes(process.readAllStandardError()))
        if len(self._countdown_stderr) > 16384:
            del self._countdown_stderr[16384:]

    def _countdown_timer_name(self, timer_id):
        timer = next(
            (item for item in self.draft.countdown_timers if item.id == timer_id),
            None,
        )
        return timer.name if timer is not None else "Timer"

    def _countdown_expected_timers(self):
        durations = {}
        request = self._countdown_request or {}
        for binding in request.get("bindings", ()):
            if isinstance(binding, dict):
                durations[binding.get("timer_id")] = binding.get(
                    "duration_seconds")
        return durations

    def _countdown_expected_host_actions(self):
        actions = {}
        request = self._countdown_request or {}
        for binding in request.get("host_action_bindings", ()):
            if isinstance(binding, dict):
                actions[(binding.get("page_index"), binding.get("slot_index"))] = (
                    binding.get("widget_key"))
        for binding in request.get("obs_launch_bindings", ()):
            if isinstance(binding, dict):
                actions[(binding.get("page_index"), binding.get("slot_index"))] = (
                    "launch_obs")
        for binding in request.get("obs_screenshot_bindings", ()):
            if isinstance(binding, dict):
                actions[(binding.get("page_index"), binding.get("slot_index"))] = (
                    "obs_screenshot")
        for binding in request.get("obs_studio_mode_bindings", ()):
            if isinstance(binding, dict):
                actions[(binding.get("page_index"), binding.get("slot_index"))] = (
                    "obs_studio_mode")
        return actions

    def _countdown_expected_media_actions(self):
        positions = set()
        request = self._countdown_request or {}
        for binding in request.get("general_media_bindings", ()):
            if isinstance(binding, dict):
                positions.add((
                    binding.get("page_index"), binding.get("slot_index")))
        return positions

    def _countdown_message(self, message, process):
        if self._countdown_result is not None:
            self._countdown_abort(
                "The helper returned a message after its final result.", process)
            return False
        kind = message.get("type")
        if kind == "ready":
            expected = self._countdown_expected_timers()
            expected_host_actions = self._countdown_expected_host_actions()
            expected_media_actions = self._countdown_expected_media_actions()
            positions = len((self._countdown_request or {}).get("bindings", ()))
            expected_fields = {"type", "timers", "positions"}
            if expected_host_actions:
                expected_fields.add("host_actions")
            if expected_media_actions:
                expected_fields.add("media_panels")
            if (self._countdown_ready_seen
                    or set(message) != expected_fields
                    or type(message.get("timers")) is not int
                    or type(message.get("positions")) is not int
                    or message["timers"] != len(expected)
                    or message["positions"] != positions
                    or (expected_host_actions
                        and (type(message.get("host_actions")) is not int
                             or message["host_actions"] != len(
                                 expected_host_actions)))
                    or (expected_media_actions
                        and (type(message.get("media_panels")) is not int
                             or message["media_panels"] != len(
                                 expected_media_actions)))):
                self._countdown_abort(
                    "The helper returned an invalid ready message.", process)
                return False
            self._countdown_ready_seen = True
            descriptions = []
            if message["timers"]:
                descriptions.append(
                    f"{message['timers']} timer(s) in {message['positions']} tile(s)")
            if expected_host_actions:
                descriptions.append(
                    f"{message['host_actions']} host action tile(s)")
            if expected_media_actions:
                descriptions.append(
                    f"{message['media_panels']} General Media tile(s)")
            self.countdown_status.setText(
                "Active for " + " and ".join(descriptions)
                + ". Tap a tile to run its action.")
            return True
        if kind == "timer":
            timer_id = message.get("timer_id")
            event = message.get("event")
            remaining = message.get("remaining_seconds")
            durations = self._countdown_expected_timers()
            duration = durations.get(timer_id)
            if (not self._countdown_ready_seen
                    or self._countdown_stopped_seen
                    or set(message) != {
                        "type", "event", "timer_id", "remaining_seconds"}
                    or not isinstance(timer_id, str)
                    or event not in ("started", "tick", "stopped", "completed")
                    or type(remaining) is not int
                    or type(duration) is not int
                    or not 0 <= remaining <= duration):
                self._countdown_abort(
                    "The helper returned an invalid timer message.", process)
                return False
            running = timer_id in self._countdown_running_ids
            if (event == "started" and (running or remaining != duration)):
                self._countdown_abort(
                    "The helper returned an invalid timer transition.", process)
                return False
            if (event == "tick"
                    and (not running or not 0 < remaining
                         < self._countdown_remaining[timer_id])):
                self._countdown_abort(
                    "The helper returned an invalid timer transition.", process)
                return False
            if (event in ("stopped", "completed")
                    and (not running or remaining != 0)):
                self._countdown_abort(
                    "The helper returned an invalid timer transition.", process)
                return False
            if event == "started":
                self._countdown_running_ids.add(timer_id)
                self._countdown_remaining[timer_id] = remaining
            elif event == "tick":
                self._countdown_remaining[timer_id] = remaining
            elif event in ("stopped", "completed"):
                self._countdown_running_ids.remove(timer_id)
                self._countdown_remaining.pop(timer_id, None)
            name = self._countdown_timer_name(timer_id)
            if event == "started":
                text = f"{name} started: {remaining} seconds remaining."
            elif event == "tick":
                text = f"{name}: {remaining} seconds remaining."
            elif event == "completed":
                text = f"{name} finished. Tap its tile to start again."
            else:
                text = f"{name} stopped. Tap its tile to start again."
            self.countdown_status.setText(text)
            return True
        if kind == "host_action":
            event = message.get("event")
            widget = message.get("widget")
            page_index = message.get("page_index")
            slot_index = message.get("slot_index")
            expected = self._countdown_expected_host_actions()
            common_fields = {
                "type", "event", "widget", "page_index", "slot_index"}
            valid_common = (
                self._countdown_ready_seen
                and not self._countdown_stopped_seen
                and isinstance(widget, str)
                and type(page_index) is int
                and type(slot_index) is int
                and expected.get((page_index, slot_index)) == widget)
            if event == "opened":
                valid = (
                    valid_common
                    and widget not in ("obs_screenshot", "obs_studio_mode")
                    and set(message) == common_fields
                )
            elif event == "triggered":
                valid = (
                    valid_common
                    and widget == "obs_screenshot"
                    and set(message) == common_fields
                )
            elif event == "toggled":
                old_enabled = message.get("old_enabled")
                new_enabled = message.get("new_enabled")
                valid = (
                    valid_common
                    and widget == "obs_studio_mode"
                    and set(message) == common_fields | {
                        "old_enabled", "new_enabled"}
                    and type(old_enabled) is bool
                    and type(new_enabled) is bool
                    and old_enabled != new_enabled
                )
            elif event == "failed":
                error = message.get("error")
                valid = (
                    valid_common
                    and widget in (
                        "launch_obs", "obs_screenshot", "obs_studio_mode")
                    and set(message) == common_fields | {"error"}
                    and isinstance(error, str) and 0 < len(error) <= 4096
                    and error.isprintable())
            else:
                valid = False
            if not valid:
                self._countdown_abort(
                    "The helper returned an invalid host-action message.",
                    process)
                return False
            if event == "failed":
                self.countdown_status.setText(
                    f"{LCD_WIDGETS[widget].label} failed: {message['error']}")
            elif event == "triggered":
                self.countdown_status.setText(
                    f"{LCD_WIDGETS[widget].label} triggered from page "
                    f"{page_index + 1}, slot {slot_index + 1}.")
            elif event == "toggled":
                state = "enabled" if message["new_enabled"] else "disabled"
                self.countdown_status.setText(
                    f"{LCD_WIDGETS[widget].label} {state} from page "
                    f"{page_index + 1}, slot {slot_index + 1}.")
            else:
                self.countdown_status.setText(
                    f"{LCD_WIDGETS[widget].label} opened from page "
                    f"{page_index + 1}, slot {slot_index + 1}.")
            return True
        if kind == "media_action":
            event = message.get("event")
            action = message.get("action")
            page_index = message.get("page_index")
            slot_index = message.get("slot_index")
            common_fields = {
                "type", "event", "action", "page_index", "slot_index"}
            expected = self._countdown_expected_media_actions()
            valid_common = (
                self._countdown_ready_seen
                and not self._countdown_stopped_seen
                and isinstance(action, str)
                and action in {
                    "shuffle", "next", "play_pause", "previous", "repeat"}
                and type(page_index) is int
                and type(slot_index) is int
                and (page_index, slot_index) in expected)
            if event == "dispatched":
                valid = (
                    valid_common
                    and set(message) == common_fields
                    and action in {
                        "shuffle", "next", "play_pause", "previous", "repeat"})
            elif event == "failed":
                error = message.get("error")
                valid = (
                    valid_common
                    and set(message) == common_fields | {"error"}
                    and action in {
                        "shuffle", "next", "play_pause", "previous", "repeat"}
                    and isinstance(error, str) and 0 < len(error) <= 4096
                    and error.isprintable())
            else:
                valid = False
            if not valid:
                self._countdown_abort(
                    "The helper returned an invalid media-action message.",
                    process)
                return False
            action_label = action.replace("_", " ").capitalize()
            if event == "dispatched":
                self.countdown_status.setText(
                    f"{action_label} requested from page "
                    f"{page_index + 1}, slot {slot_index + 1}.")
            elif event == "failed":
                self.countdown_status.setText(
                    f"{action_label} failed: {message['error']}")
            return True
        if kind == "stopped":
            if (set(message) != {"type"}
                    or not self._countdown_stop_requested
                    or not self._countdown_ready_seen
                    or self._countdown_stopped_seen):
                self._countdown_abort("The helper stopped unexpectedly.", process)
                return False
            self._countdown_stopped_seen = True
            self._countdown_running_ids.clear()
            self._countdown_remaining.clear()
            return True
        if kind == "result":
            outcome = message.get("outcome")
            if outcome == "stopped":
                valid = (set(message) == {
                    "type", "outcome", "verified", "display_may_be_stale"}
                    and self._countdown_stop_requested
                    and self._countdown_stopped_seen
                    and message.get("verified") is True
                    and message.get("display_may_be_stale") is False)
            elif outcome == "error":
                error = message.get("error")
                valid = (set(message) == {
                    "type", "outcome", "verified", "display_may_be_stale",
                    "error"}
                    and message.get("verified") is False
                    and type(message.get("display_may_be_stale")) is bool
                    and isinstance(error, str) and 0 < len(error) <= 4096)
            else:
                valid = False
            if not valid:
                self._countdown_abort(
                    "The helper returned an invalid result.", process)
                return False
            self._countdown_result = message
            if message["verified"] is True and message["outcome"] == "stopped":
                detail = self._countdown_stop_reason or "Stopped by the user."
                self.countdown_status.setText("LCD actions stopped. " + detail)
            else:
                detail = str(message.get("error") or "The LCD action helper failed.")
                self.countdown_status.setText("LCD actions stopped: " + detail)
                self.snapshot = None
            return True
        self._countdown_abort("The helper returned an unknown message.", process)
        return False

    def _countdown_abort(self, message, process=None):
        process = self._countdown_process if process is None else process
        if (self._countdown_process is not process
                or self._countdown_local_abort):
            return
        self._countdown_local_abort = True
        self._countdown_result = {
            "type": "result", "outcome": "error", "verified": False,
            "display_may_be_stale": True, "error": message,
        }
        self.countdown_status.setText("LCD actions stopped: " + message)
        self.snapshot = None
        if process is not None and process.state() != QProcess.ProcessState.NotRunning:
            process.kill()

    def _countdown_process_error(self, process, _error):
        if self._countdown_process is not process:
            return
        detail = process.errorString().strip() or "The helper process failed."
        self._countdown_abort(detail, process)
        if process.state() == QProcess.ProcessState.NotRunning:
            QTimer.singleShot(0, lambda current=process:
                              self._countdown_process_finished(
                                  current, -1,
                                  QProcess.ExitStatus.CrashExit))

    def _countdown_process_finished(self, process, exit_code=None,
                                    exit_status=None):
        if self._countdown_process is not process:
            return
        self._countdown_output_ready(process)
        if self._countdown_process is not process:
            return
        self._countdown_error_ready(process)
        if self._countdown_stdout.strip() and not self._countdown_local_abort:
            self._countdown_result = None
            self.countdown_status.setText(
                "LCD actions stopped: The helper returned an incomplete message.")
            self.snapshot = None
        if self._countdown_result is None:
            detail = self._countdown_stderr.decode("utf-8", "replace").strip()
            self.countdown_status.setText(
                "LCD actions stopped: "
                + (detail or "The helper exited without a verified result."))
            self.snapshot = None
        elif (self._countdown_result.get("outcome") == "stopped"
              and (exit_code != 0
                   or exit_status != QProcess.ExitStatus.NormalExit)):
            self._countdown_result = None
            self.countdown_status.setText(
                "LCD actions stopped: The helper did not exit cleanly after verification.")
            self.snapshot = None
        self.countdown_checkbox.blockSignals(True)
        self.countdown_checkbox.setChecked(False)
        self.countdown_checkbox.blockSignals(False)
        self._countdown_process = None
        self._countdown_request = None
        self._countdown_stop_requested = False
        self._countdown_stop_written = False
        self._countdown_running_ids.clear()
        self._countdown_remaining.clear()
        process.deleteLater()
        self._update_capabilities()
        if self._countdown_close_pending:
            QTimer.singleShot(0, self.close)

    @staticmethod
    def _validated_media_players(value):
        if not isinstance(value, (list, tuple)) or len(value) > 8:
            raise ValueError(
                "Media player discovery returned an invalid player list.")
        from ..media_player_ids import validate_media_player_id
        result = []
        identities = set()
        for record in value:
            if not isinstance(record, dict) or set(record) != {
                    "id", "label", "backend"}:
                raise ValueError(
                    "Media player discovery returned an invalid player.")
            identity = record["id"]
            try:
                validate_media_player_id(identity)
            except ValueError as error:
                raise ValueError(
                    "Media player discovery returned an invalid player.") from error
            player_label, backend = record["label"], record["backend"]
            if (identity in identities
                    or not isinstance(player_label, str)
                    or not 1 <= len(player_label) <= 160
                    or not player_label.isprintable()
                    or backend not in ("mpris", "apple-music")):
                raise ValueError(
                    "Media player discovery returned an invalid player.")
            identities.add(identity)
            result.append({
                "id": identity, "label": player_label, "backend": backend,
            })
        return tuple(result)

    def _refresh_media_players(self):
        provider = getattr(self.service, "media_players", None)
        if self._media_player_job is not None or not callable(provider):
            if not callable(provider):
                self.media_player_status.setText(
                    "Media player discovery is unavailable in this installation.")
            return
        self.media_player_refresh_button.setEnabled(False)
        self.media_player_status.setText("Checking host media players…")
        job = ApplicationScanJob(provider, self)
        self._media_player_job = job
        job.completed.connect(
            lambda result, current=job:
            self._media_players_received(current, result))
        job.failed.connect(
            lambda message, current=job:
            self._media_players_failed(current, message))
        job.finished.connect(
            lambda current=job: self._media_player_job_finished(current))
        job.start()

    def _media_players_received(self, job, value):
        if self._media_player_job is not job:
            return
        try:
            self._media_players = self._validated_media_players(value)
        except ValueError as error:
            self._media_players_failed(job, str(error))
            return
        self._populate_media_players()

    def _media_players_failed(self, job, message):
        if self._media_player_job is not job:
            return
        self._media_players = ()
        self._populate_media_players()
        detail = str(message).strip() or "the host media players could not be listed"
        self.media_player_status.setText(
            "Media player discovery failed: " + detail
            + (" The saved player remains selected and will not fall back."
               if self.draft.display.preferred_media_player is not None else ""))

    def _media_player_job_finished(self, job):
        if self._media_player_job is job:
            self._media_player_job = None
            self.media_player_refresh_button.setEnabled(
                callable(getattr(self.service, "media_players", None)))
        job.deleteLater()

    def _populate_media_players(self):
        selected = self.draft.display.preferred_media_player
        self._media_player_loading = True
        self.media_player_selector.blockSignals(True)
        try:
            self.media_player_selector.clear()
            self.media_player_selector.addItem(
                "Automatic (only unambiguous player)", None)
            selected_index = 0
            for player in self._media_players:
                self.media_player_selector.addItem(
                    f"{player['label']} · {player['backend']}", player["id"])
                if player["id"] == selected:
                    selected_index = self.media_player_selector.count() - 1
            if selected is not None and selected_index == 0:
                self.media_player_selector.addItem(
                    f"Saved player · {selected} · unavailable", selected)
                selected_index = self.media_player_selector.count() - 1
            self.media_player_selector.setCurrentIndex(selected_index)
        finally:
            self.media_player_selector.blockSignals(False)
            self._media_player_loading = False
        self._update_media_player_status()

    def _update_media_player_status(self):
        selected_id = self.draft.display.preferred_media_player
        selected = next(
            (player for player in self._media_players
             if player["id"] == selected_id), None)
        if selected_id is not None and selected is None:
            self.media_player_status.setText(
                "The saved player is unavailable. General Media will wait for it "
                "and will not control another player.")
        elif selected is not None:
            self.media_player_status.setText(
                f"General Media will control {selected['label']} for this profile.")
        elif not self._media_players:
            self.media_player_status.setText(
                "No supported running media player was detected.")
        elif len(self._media_players) == 1:
            self.media_player_status.setText(
                f"Automatic will use {self._media_players[0]['label']}.")
        else:
            self.media_player_status.setText(
                "Automatic uses the only playing player. Choose a player to "
                "resolve multiple playing or idle players.")

    def _media_player_changed(self, index):
        if self._media_player_loading or self._loading or index < 0:
            return
        selected = self.media_player_selector.itemData(index)
        if selected == self.draft.display.preferred_media_player:
            return
        if selected is not None:
            try:
                from ..media_player_ids import validate_media_player_id
                selected = validate_media_player_id(selected)
            except ValueError as error:
                self._populate_media_players()
                self._notify(str(error), error=True)
                return
        self.draft.display.preferred_media_player = selected
        self._changed()
        self._update_media_player_status()

    @staticmethod
    def _validated_gpu_sources(value):
        if not isinstance(value, (list, tuple)) or len(value) > 64:
            raise ValueError("GPU source discovery returned an invalid adapter list.")
        result = []
        identities = set()
        for record in value:
            if not isinstance(record, dict):
                raise ValueError("GPU source discovery returned an invalid adapter.")
            identity, source_label = record.get("id"), record.get("label")
            if (not isinstance(identity, str) or not 1 <= len(identity) <= 4096
                    or not identity.isprintable() or identity in identities
                    or not isinstance(source_label, str)
                    or not 1 <= len(source_label) <= 160
                    or not source_label.isprintable()
                    or type(record.get("primary")) is not bool
                    or type(record.get("load_available")) is not bool
                    or type(record.get("temperature_available")) is not bool):
                raise ValueError("GPU source discovery returned an invalid adapter.")
            identities.add(identity)
            result.append({
                "id": identity,
                "label": source_label,
                "primary": record["primary"],
                "load_available": record["load_available"],
                "temperature_available": record["temperature_available"],
            })
        return tuple(result)

    @staticmethod
    def _gpu_source_choice_text(source):
        readings = []
        if source["load_available"]:
            readings.append("load")
        if source["temperature_available"]:
            readings.append("temperature")
        availability = " + ".join(readings) if readings else "no readable values"
        primary = " · system primary" if source["primary"] else ""
        return f"{source['label']}{primary} · {availability}"

    def _refresh_gpu_sources(self):
        provider = getattr(self.service, "gpu_sources", None)
        if self._gpu_source_job is not None or not callable(provider):
            if not callable(provider):
                self.gpu_source_status.setText(
                    "GPU source discovery is unavailable in this installation.")
            return
        self.gpu_source_refresh_button.setEnabled(False)
        self.gpu_source_status.setText("Checking host GPU adapters…")
        job = ApplicationScanJob(provider, self)
        self._gpu_source_job = job
        job.completed.connect(
            lambda result, current=job: self._gpu_sources_received(current, result))
        job.failed.connect(
            lambda message, current=job: self._gpu_sources_failed(current, message))
        job.finished.connect(
            lambda current=job: self._gpu_source_job_finished(current))
        job.start()

    def _gpu_sources_received(self, job, value):
        if self._gpu_source_job is not job:
            return
        try:
            self._gpu_sources = self._validated_gpu_sources(value)
        except ValueError as error:
            self._gpu_sources_failed(job, str(error))
            return
        self._populate_gpu_sources()

    def _gpu_sources_failed(self, job, message):
        if self._gpu_source_job is not job:
            return
        self._gpu_sources = ()
        self._populate_gpu_sources()
        detail = str(message).strip() or "the host GPU adapters could not be listed"
        self.gpu_source_status.setText(
            "GPU source discovery failed: " + detail
            + (" The saved source remains selected and will not fall back."
               if self._gpu_source is not None else ""))

    def _gpu_source_job_finished(self, job):
        if self._gpu_source_job is job:
            self._gpu_source_job = None
            self.gpu_source_refresh_button.setEnabled(
                callable(getattr(self.service, "gpu_sources", None)))
        job.deleteLater()

    def _populate_gpu_sources(self):
        selected = self._gpu_source
        self._gpu_source_loading = True
        self.gpu_source_selector.blockSignals(True)
        try:
            self.gpu_source_selector.clear()
            self.gpu_source_selector.addItem("Automatic (system primary)", None)
            selected_index = 0
            for source in self._gpu_sources:
                self.gpu_source_selector.addItem(
                    self._gpu_source_choice_text(source), source["id"])
                if source["id"] == selected:
                    selected_index = self.gpu_source_selector.count() - 1
            if selected is not None and selected_index == 0:
                self.gpu_source_selector.addItem(
                    f"Saved GPU · {selected} · unavailable", selected)
                selected_index = self.gpu_source_selector.count() - 1
            self.gpu_source_selector.setCurrentIndex(selected_index)
        finally:
            self.gpu_source_selector.blockSignals(False)
            self._gpu_source_loading = False
        self._update_gpu_source_status()

    def _update_gpu_source_status(self):
        if self._gpu_source_error:
            self.gpu_source_status.setText(
                "The saved GPU source could not be read; Automatic is selected: "
                + self._gpu_source_error)
            return
        selected = next(
            (source for source in self._gpu_sources
             if source["id"] == self._gpu_source), None)
        if self._gpu_source is not None and selected is None:
            self.gpu_source_status.setText(
                "The saved GPU source is unavailable. GPU values will be skipped; "
                "choose Automatic or an available GPU to change it.")
            return
        if selected is not None:
            readings = []
            if selected["load_available"]:
                readings.append("load")
            if selected["temperature_available"]:
                readings.append("temperature")
            self.gpu_source_status.setText(
                f"Using {selected['label']}. Available readings: "
                + (" and ".join(readings) if readings else "none") + ".")
            return
        if not self._gpu_sources:
            self.gpu_source_status.setText(
                "No GPU telemetry adapter was detected. GPU widgets will be unavailable.")
            return
        primary = [source for source in self._gpu_sources if source["primary"]]
        if len(primary) == 1:
            self.gpu_source_status.setText(
                f"Automatic will use the system primary GPU, {primary[0]['label']}.")
        elif len(self._gpu_sources) == 1:
            self.gpu_source_status.setText(
                f"Automatic will use the only detected GPU, {self._gpu_sources[0]['label']}.")
        else:
            self.gpu_source_status.setText(
                "Automatic has no unique system primary GPU. Choose a specific GPU "
                "to enable its widgets.")

    def _gpu_source_changed(self, index):
        if self._gpu_source_loading or index < 0:
            return
        selected = self.gpu_source_selector.itemData(index)
        previous = self._gpu_source
        if selected == previous:
            return
        try:
            self.gpu_source_store.save(selected)
        except (OSError, ValueError) as error:
            self._gpu_source = previous
            self._populate_gpu_sources()
            self._notify(f"Could not save GPU source: {error}", error=True)
            return
        self._gpu_source = selected
        self._gpu_source_error = ""
        if self.host_lcd_checkbox.isChecked():
            self.host_lcd_checkbox.setChecked(False)
            self.host_lcd_status.setText(
                "Live updates stopped because the GPU source changed. Enable them to restart.")
        self._update_gpu_source_status()

    def _host_lcd_ready(self):
        device = next((item for item in self.devices if item["id"] == self.selected_device_id), {})
        snapshot = self.snapshot
        if ("host_lcd" not in device.get("capabilities", [])
                or not callable(getattr(self.service, "update_host_lcd", None))
                or not snapshot or snapshot.get("device_id") != self.selected_device_id
                or snapshot.get("profile_slot") != self.draft.profile_slot
                or snapshot.get("summary", {}).get("active_profile") != self.draft.profile_slot):
            return None
        baseline = snapshot.get("baseline", {})
        try:
            from ..lcd_commands import decode_lcd_response
            raw = baseline["settings"]["lcd"]
            state = decode_lcd_response(bytes.fromhex(raw), self.draft.profile_slot - 1)
        except (KeyError, TypeError, ValueError):
            return None
        if not any(widget and widget.key in HOST_LCD_WIDGETS
                   for page in state.pages[:min(3, state.page_count)]
                   for widget in page.slots or ()):
            return None
        return (self.selected_device_id, self.draft.profile_slot, raw,
                self._gpu_source)

    def _toggle_host_lcd(self, enabled):
        if enabled:
            if self._countdown_process is not None:
                self._host_lcd_failed("Stop live LCD actions first.")
                return
            self._host_lcd_context = self._host_lcd_ready()
            if self._host_lcd_context is None:
                self._host_lcd_failed("Read the active profile and apply a system monitoring widget first.")
                return
            self.host_lcd_timer.start()
            self._poll_host_lcd()
        else:
            self.host_lcd_timer.stop()
            self._host_lcd_context = None
            self.host_lcd_status.setText("Stopped. Any current update will finish; values may remain on the mouse.")

    def _poll_host_lcd(self):
        if not self.host_lcd_checkbox.isChecked():
            return
        if self._job is not None or QApplication.activeModalWidget() is not None:
            return
        if self._host_lcd_context != self._host_lcd_ready():
            self._host_lcd_failed("The device, profile or LCD layout changed. Read it again before restarting.")
            return
        device_id, slot, _, gpu_source = self._host_lcd_context
        baseline = copy.deepcopy(self.snapshot["baseline"])
        operation = (
            (lambda: self.service.update_host_lcd(device_id, slot, baseline))
            if gpu_source is None else
            (lambda: self.service.update_host_lcd(
                device_id, slot, baseline, gpu_source=gpu_source)))
        self._run_job(operation,
                      self._host_lcd_received, "Updating live LCD widgets…", telemetry=True,
                      failed=self._host_lcd_failed)

    def _host_lcd_received(self, result):
        if not self.host_lcd_checkbox.isChecked():
            return
        if not result.get("acknowledged"):
            self._host_lcd_failed("The mouse did not acknowledge the live update.")
            return
        labels = {
            "cpu_load": ("CPU", "%"), "ram_usage": ("RAM", "%"),
            "gpu_load": ("GPU", "%"), "cpu_temperature": ("CPU temp", "°C"),
            "gpu_temperature": ("GPU temp", "°C"),
        }
        values = result.get("updated_values")
        if not isinstance(values, dict):
            values = {"cpu_load": result.get("cpu_percent"),
                      "ram_usage": result.get("ram_percent")}
        rendered = [f"{labels[key][0]} {value}{labels[key][1]}"
                    for key, value in values.items()
                    if key in labels and value is not None]
        unavailable = [labels[key][0] + " unavailable"
                       for key in result.get("unavailable_widgets", ()) if key in labels]
        summary = " · ".join(rendered + unavailable) or "live values"
        source = self.gpu_source_selector.currentText()
        self.host_lcd_status.setText(
            f"Sent {summary} to {result['widgets']} LCD widget(s) at "
            f"{datetime.now().strftime('%H:%M:%S')}. GPU source: {source}.")

    def _host_lcd_failed(self, message):
        self.host_lcd_checkbox.setChecked(False)
        self.host_lcd_status.setText("Live updates stopped: " + message)

    def choose_background(self):
        if self._device_or_dialog_busy():
            return
        device = next((item for item in self.devices if item["id"] == self.selected_device_id), {})
        can_upload = "upload_background" in device.get("capabilities", [])
        dialog = BackgroundDialog(self, can_upload=can_upload)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        prepared = dialog.prepared
        dialog.deleteLater()
        if not accepted or prepared is None:
            return
        device_id, rgba = self.selected_device_id, prepared.rgba
        self._run_job(lambda: self.service.upload_background(device_id, rgba), self._background_uploaded,
                      "Uploading LCD background (up to two minutes)…")

    def _background_uploaded(self, result):
        if result.get("acknowledged"):
            # A completed upload selects the custom background globally. Keep
            # that verified baseline without replacing unrelated draft edits.
            from ..image_commands import decode_background_selection_response
            try:
                state = decode_background_selection_response(bytes.fromhex(result["selection_after"]))
                if not result.get("selection_verified") or state.background_index != 1:
                    raise ValueError("Custom selection was not verified")
            except (KeyError, TypeError, ValueError):
                self.snapshot = None
                self._update_capabilities()
            else:
                self.draft.display.background_index = 1
                if self.snapshot:
                    self.snapshot = copy.deepcopy(self.snapshot)
                    self.snapshot["baseline"].setdefault("settings", {})["background"] = state.raw.hex()
                    received = self.snapshot["configuration"]
                    if isinstance(received, dict):
                        received["display"]["background_index"] = 1
                    else:
                        received.display.background_index = 1
                self._load_draft()
                self._changed()
            self._notify("Background upload acknowledged by the mouse. Check the physical LCD to confirm the image; pixels cannot be read back yet.")
        else:
            self.snapshot = None
            self._update_capabilities()
            self._notify("The mouse did not confirm the background upload. Check the connection before trying again.", error=True)

    @staticmethod
    def _lcd_width(key):
        if key in LCD_WIDGETS:
            return LCD_WIDGETS[key].width
        if isinstance(key, str) and key.startswith("unknown_"):
            return 3 if key.split("_")[1] in ("01", "45", "65") else 1
        return 1

    def _load_lcd_pages(self):
        previous_loading, self._loading = self._loading, True
        try:
            pages = self.draft.display.pages
            first_layout = bool(pages) and not getattr(self, "_lcd_had_pages", False)
            self.lcd_status.setText("Slots run from left to right on the mouse. Changes stay in this draft until you apply them." if pages else "Read the mouse to load its LCD pages, or import a preset with a layout.")
            for page_index, controls in enumerate(self.lcd_controls):
                present = page_index < len(pages)
                self.lcd_tabs.setTabEnabled(page_index, present)
                for slot, control in enumerate(controls):
                    key_control = self.lcd_key_controls[page_index][slot]
                    macro_control = self.lcd_macro_controls[page_index][slot]
                    timer_control = self.lcd_timer_controls[page_index][slot]
                    host_action_editor = self.lcd_host_action_editors[page_index][slot]
                    host_action_browse = self.lcd_host_action_browse_buttons[page_index][slot]
                    host_action_icon_preview = (
                        self.lcd_host_action_icon_previews[page_index][slot])
                    host_action_icon_state = (
                        self.lcd_host_action_icon_states[page_index][slot])
                    host_action_icon_auto = (
                        self.lcd_host_action_icon_auto_buttons[page_index][slot])
                    host_action_icon_browse = (
                        self.lcd_host_action_icon_browse_buttons[page_index][slot])
                    host_action_icon_clear = (
                        self.lcd_host_action_icon_clear_buttons[page_index][slot])
                    control.clear()
                    value = pages[page_index][slot] if present else None
                    control.setEnabled(present and value is not None)
                    if not present:
                        control.addItem("No layout loaded", None)
                    elif value is None:
                        anchor = slot - 1
                        while anchor > 0 and pages[page_index][anchor] is None:
                            anchor -= 1
                        control.addItem(f"Covered by slot {anchor + 1}", None)
                    else:
                        for key in LCD_CHOICES:
                            widget = LCD_WIDGETS[key]
                            if slot + widget.width <= 4:
                                control.addItem(widget.label + (" · 3 slots" if widget.width == 3 else ""), key)
                        if control.findData(value) < 0:
                            widget = LCD_WIDGETS.get(value)
                            caption = widget.label if widget else "On-device widget"
                            if value == "polling_rate":
                                caption += " · unavailable on tested firmware"
                            control.addItem(caption + " (preserved)", value)
                        control.setCurrentIndex(control.findData(value))
                    binding = None
                    if (page_index < len(self.draft.display.key_bindings)
                            and slot < len(self.draft.display.key_bindings[page_index])):
                        binding = self.draft.display.key_bindings[page_index][slot]
                    key_control.setEnabled(present and value in LCD_KEY_WIDGETS)
                    key_control.setVisible(present and value in LCD_KEY_WIDGETS)
                    opaque = isinstance(binding, str) and binding.startswith("device:")
                    key_control.setText("" if opaque else binding or "")
                    if opaque:
                        key_control.setPlaceholderText("On-device key data (preserved)")
                        key_control.setToolTip("This unrecognized device value is preserved. Enter a supported key or shortcut to replace it.")
                    elif value == "remap_key":
                        key_control.setPlaceholderText("Key, for example A or F5")
                        key_control.setToolTip("Enter one keyboard key.")
                    elif value == "hotkey":
                        key_control.setPlaceholderText("Shortcut, for example Ctrl+S")
                        key_control.setToolTip("Enter modifiers followed by one key.")
                    else:
                        key_control.setPlaceholderText("Available for key tiles")
                        key_control.setToolTip("Choose Remap key or Keyboard shortcut to edit this field.")
                    macro_binding = None
                    if (page_index < len(self.draft.display.macro_bindings)
                            and slot < len(self.draft.display.macro_bindings[page_index])):
                        macro_binding = self.draft.display.macro_bindings[page_index][slot]
                    macro_control.clear()
                    macro_control.addItem("Choose a macro…", None)
                    for macro in self.draft.macros:
                        if macro.playback != "while_held":
                            macro_control.addItem(macro.name, macro.id)
                    if macro_binding is not None and macro_control.findData(macro_binding) < 0:
                        bound_macro = next(
                            (macro for macro in self.draft.macros
                             if macro.id == macro_binding), None)
                        if bound_macro is not None and bound_macro.playback == "while_held":
                            macro_control.addItem(
                                f"{bound_macro.name} · While held unavailable for LCD",
                                macro_binding)
                            item = macro_control.model().item(macro_control.count() - 1)
                            if item is not None:
                                item.setEnabled(False)
                        else:
                            macro_control.addItem("Missing macro (fix before saving)", macro_binding)
                    macro_control.setCurrentIndex(max(0, macro_control.findData(macro_binding)))
                    is_macro = present and value in LCD_MACRO_WIDGETS
                    macro_control.setEnabled(is_macro)
                    macro_control.setVisible(is_macro)
                    macro_control.setToolTip(
                        "Choose a Once, Repeat, or Toggle macro from this preset, then apply display settings to store it on the mouse.")
                    timer_binding = None
                    if (page_index < len(self.draft.display.timer_bindings)
                            and slot < len(self.draft.display.timer_bindings[page_index])):
                        timer_binding = self.draft.display.timer_bindings[page_index][slot]
                    timer_control.clear()
                    timer_control.addItem("Choose a timer…", None)
                    for timer in self.draft.countdown_timers:
                        timer_control.addItem(
                            f"{timer.name} · {timer.duration_seconds} sec", timer.id)
                    timer_control.setCurrentIndex(
                        max(0, timer_control.findData(timer_binding)))
                    is_timer = present and value in LCD_TIMER_WIDGETS
                    timer_control.setEnabled(is_timer)
                    timer_control.setVisible(is_timer)
                    timer_control.setToolTip(
                        "Choose a timer from this preset for this Count down timer tile.")
                    host_action_target = None
                    if (page_index < len(self.draft.display.host_action_bindings)
                            and slot < len(self.draft.display.host_action_bindings[page_index])):
                        host_action_target = (
                            self.draft.display.host_action_bindings[page_index][slot])
                    is_host_action = present and value in LCD_HOST_ACTION_WIDGETS
                    host_action_editor.setEnabled(is_host_action)
                    host_action_editor.setVisible(is_host_action)
                    if value == "open_application":
                        host_action_editor.setMaxLength(4096)
                        host_action_editor.setPlaceholderText(
                            "Choose an application or enter its absolute path")
                        host_action_editor.setToolTip(
                            "Choose an executable application or macOS .app bundle. "
                            "Changing this path clears the saved icon until you choose one again.")
                    elif value == "open_website":
                        host_action_editor.setMaxLength(2048)
                        host_action_editor.setPlaceholderText("https://example.com")
                        host_action_editor.setToolTip(
                            "Enter a complete http or https URL for this tile.")
                    elif value == "open_file":
                        host_action_editor.setMaxLength(4096)
                        host_action_editor.setPlaceholderText("Choose a file or enter its absolute path")
                        host_action_editor.setToolTip(
                            "Choose the file this tile should open, or enter its absolute path.")
                    elif value == "open_folder":
                        host_action_editor.setMaxLength(4096)
                        host_action_editor.setPlaceholderText("Choose a folder or enter its absolute path")
                        host_action_editor.setToolTip(
                            "Choose the folder this tile should open, or enter its absolute path.")
                    else:
                        host_action_editor.setMaxLength(4096)
                        host_action_editor.setPlaceholderText(
                            "Available for application, website, file and folder tiles")
                        host_action_editor.setToolTip(
                            "Choose Open application, Open website, Open file or Open folder to edit this field.")
                    host_action_editor.setText(host_action_target or "")
                    can_browse = present and value in (
                        "open_application", "open_file", "open_folder")
                    host_action_browse.setEnabled(can_browse)
                    host_action_browse.setVisible(can_browse)
                    if value == "open_application":
                        host_action_browse.setText("Choose app…")
                        host_action_browse.setToolTip(
                            "Choose the executable application this LCD tile should open.")
                    elif value == "open_folder":
                        host_action_browse.setText("Choose folder…")
                        host_action_browse.setToolTip("Choose the folder this LCD tile should open.")
                    else:
                        host_action_browse.setText("Choose file…")
                        host_action_browse.setToolTip("Choose the file this LCD tile should open.")

                    icon_value = None
                    if (page_index < len(
                            self.draft.display.host_action_icon_bindings)
                            and slot < len(
                                self.draft.display.host_action_icon_bindings[page_index])):
                        icon_value = (
                            self.draft.display.host_action_icon_bindings[page_index][slot])
                    is_application = present and value == "open_application"
                    has_target = is_application and bool(host_action_target)
                    host_action_icon_preview.setPixmap(QPixmap())
                    host_action_icon_preview.setText("No icon")
                    if icon_value is not None:
                        try:
                            preview = preview_from_wire_rgba(
                                host_action_icon_rgba(icon_value))
                        except ValueError:
                            host_action_icon_state.setText("Invalid icon")
                            host_action_icon_preview.setText("Invalid")
                        else:
                            host_action_icon_preview.setPixmap(
                                QPixmap.fromImage(preview))
                            host_action_icon_state.setText("Icon ready")
                    elif has_target:
                        host_action_icon_state.setText("Icon required")
                    else:
                        host_action_icon_state.setText("Choose an application")
                    for icon_control in (
                            host_action_icon_preview, host_action_icon_state,
                            host_action_icon_auto, host_action_icon_browse,
                            host_action_icon_clear):
                        icon_control.setVisible(is_application)
                    host_action_icon_auto.setEnabled(has_target)
                    host_action_icon_auto.setToolTip(
                        "Extract the operating system icon from the selected application.")
                    host_action_icon_browse.setEnabled(has_target)
                    host_action_icon_browse.setToolTip(
                        "Replace the application icon with a PNG or JPEG image.")
                    host_action_icon_clear.setEnabled(
                        is_application and icon_value is not None)
                    host_action_icon_clear.setToolTip(
                        "Remove this icon. A resolved Open Application tile requires an icon before it can be saved or applied.")
            if first_layout:
                self.lcd_tabs.setCurrentIndex(0)
            self._lcd_had_pages = bool(pages)
            self._update_lcd_moves()
        finally:
            self._loading = previous_loading

    def _update_lcd_moves(self, *_):
        source = self.lcd_tabs.currentIndex()
        for button, offset in ((self.lcd_move_left, -1), (self.lcd_move_right, 1)):
            enabled = self._job is None and 0 <= source+offset < len(self.draft.display.pages)
            tooltip = "Move this entire page in the local draft. Apply display settings to update the mouse."
            if enabled:
                try:
                    move_page(self.draft.display.pages, source, source+offset,
                              key_bindings=self.draft.display.key_bindings,
                              macro_bindings=self.draft.display.macro_bindings,
                              macros=self.draft.macros,
                              timer_bindings=self.draft.display.timer_bindings,
                              countdown_timers=self.draft.countdown_timers,
                              host_action_bindings=(
                                  self.draft.display.host_action_bindings),
                              host_action_icon_bindings=(
                                  self.draft.display.host_action_icon_bindings))
                except ValueError as error:
                    enabled, tooltip = False, str(error)
            button.setEnabled(enabled)
            button.setToolTip(tooltip)

    def _move_lcd_page(self, offset):
        if self._job is not None:
            return
        source = self.lcd_tabs.currentIndex()
        try:
            pages = move_page(self.draft.display.pages, source, source+offset,
                              key_bindings=self.draft.display.key_bindings,
                              macro_bindings=self.draft.display.macro_bindings,
                              macros=self.draft.macros,
                              timer_bindings=self.draft.display.timer_bindings,
                              countdown_timers=self.draft.countdown_timers,
                              host_action_bindings=(
                                  self.draft.display.host_action_bindings),
                              host_action_icon_bindings=(
                                  self.draft.display.host_action_icon_bindings))
        except ValueError as error:
            self._notify(str(error), error=True)
            return
        bindings = copy.deepcopy(self.draft.display.key_bindings)
        macro_bindings = copy.deepcopy(self.draft.display.macro_bindings)
        timer_bindings = copy.deepcopy(self.draft.display.timer_bindings)
        host_action_bindings = copy.deepcopy(
            self.draft.display.host_action_bindings)
        host_action_icon_bindings = copy.deepcopy(
            self.draft.display.host_action_icon_bindings)
        if bindings:
            bindings.insert(source + offset, bindings.pop(source))
        if macro_bindings:
            macro_bindings.insert(source + offset, macro_bindings.pop(source))
        if timer_bindings:
            timer_bindings.insert(source + offset, timer_bindings.pop(source))
        if host_action_bindings:
            host_action_bindings.insert(
                source + offset, host_action_bindings.pop(source))
        if host_action_icon_bindings:
            host_action_icon_bindings.insert(
                source + offset, host_action_icon_bindings.pop(source))
        self.draft.display.pages = pages
        self.draft.display.key_bindings = bindings
        self.draft.display.macro_bindings = macro_bindings
        self.draft.display.timer_bindings = timer_bindings
        self.draft.display.host_action_bindings = host_action_bindings
        self.draft.display.host_action_icon_bindings = (
            host_action_icon_bindings)
        self._load_lcd_pages()
        self.lcd_tabs.setCurrentIndex(source+offset)
        self._changed()

    def _lcd_slot_changed(self, page_index, slot, key):
        if self._loading or key is None or page_index >= len(self.draft.display.pages):
            return
        cells = self.draft.display.pages[page_index]
        if cells[slot] == key:
            return
        width = self._lcd_width(key)
        if slot + width > 4:
            return
        bindings = self._ensure_lcd_key_bindings()
        macro_bindings = self._ensure_lcd_macro_bindings()
        timer_bindings = self._ensure_lcd_timer_bindings()
        host_action_bindings = self._ensure_lcd_host_action_bindings()
        host_action_icon_bindings = (
            self._ensure_lcd_host_action_icon_bindings())
        # Clear every old wide widget touched by the replacement, including its
        # continuation cells, so moving or shrinking a panel stays valid.
        for anchor, existing in enumerate(cells[:]):
            if existing is None:
                continue
            old_width = self._lcd_width(existing)
            if anchor < slot + width and anchor + old_width > slot:
                cells[anchor:anchor + old_width] = ["empty"] * old_width
                bindings[page_index][anchor:anchor + old_width] = [None] * old_width
                macro_bindings[page_index][anchor:anchor + old_width] = [None] * old_width
                timer_bindings[page_index][anchor:anchor + old_width] = [None] * old_width
                host_action_bindings[page_index][anchor:anchor + old_width] = [None] * old_width
                host_action_icon_bindings[
                    page_index][anchor:anchor + old_width] = [None] * old_width
        cells[slot:slot + width] = [key] + [None] * (width - 1)
        bindings[page_index][slot:slot + width] = [LCD_KEY_DEFAULTS.get(key)] + [None] * (width - 1)
        macro_bindings[page_index][slot:slot + width] = [None] * width
        timer_bindings[page_index][slot:slot + width] = [None] * width
        host_action_bindings[page_index][slot:slot + width] = [None] * width
        host_action_icon_bindings[page_index][slot:slot + width] = [None] * width
        self._load_lcd_pages()
        self._changed()

    def _ensure_lcd_key_bindings(self):
        pages = self.draft.display.pages
        bindings = self.draft.display.key_bindings
        if len(bindings) == len(pages) and all(len(row) == 4 for row in bindings):
            return bindings
        normalized = [[None] * 4 for _ in pages]
        for page_index, row in enumerate(bindings[:len(normalized)]):
            if not isinstance(row, list):
                continue
            for slot, binding in enumerate(row[:4]):
                if pages[page_index][slot] in LCD_KEY_WIDGETS:
                    normalized[page_index][slot] = binding
        self.draft.display.key_bindings = normalized
        return normalized

    def _lcd_key_changed(self, page_index, slot, value):
        if self._loading or page_index >= len(self.draft.display.pages):
            return
        if self.draft.display.pages[page_index][slot] not in LCD_KEY_WIDGETS:
            return
        bindings = self._ensure_lcd_key_bindings()
        value = value or None
        if bindings[page_index][slot] == value:
            return
        bindings[page_index][slot] = value
        self._changed()

    def _ensure_lcd_macro_bindings(self):
        pages = self.draft.display.pages
        bindings = self.draft.display.macro_bindings
        if len(bindings) == len(pages) and all(len(row) == 4 for row in bindings):
            return bindings
        normalized = [[None] * 4 for _ in pages]
        for page_index, row in enumerate(bindings[:len(normalized)]):
            if not isinstance(row, list):
                continue
            for slot, binding in enumerate(row[:4]):
                if pages[page_index][slot] in LCD_MACRO_WIDGETS:
                    normalized[page_index][slot] = binding
        self.draft.display.macro_bindings = normalized
        return normalized

    def _lcd_macro_changed(self, page_index, slot, macro_id):
        if self._loading or page_index >= len(self.draft.display.pages):
            return
        if self.draft.display.pages[page_index][slot] not in LCD_MACRO_WIDGETS:
            return
        bindings = self._ensure_lcd_macro_bindings()
        if bindings[page_index][slot] == macro_id:
            return
        bindings[page_index][slot] = macro_id
        self._changed()

    def _ensure_lcd_timer_bindings(self):
        pages = self.draft.display.pages
        bindings = self.draft.display.timer_bindings
        if len(bindings) == len(pages) and all(len(row) == 4 for row in bindings):
            return bindings
        normalized = [[None] * 4 for _ in pages]
        for page_index, row in enumerate(bindings[:len(normalized)]):
            if not isinstance(row, list):
                continue
            for slot, binding in enumerate(row[:4]):
                if pages[page_index][slot] in LCD_TIMER_WIDGETS:
                    normalized[page_index][slot] = binding
        self.draft.display.timer_bindings = normalized
        return normalized

    def _lcd_timer_changed(self, page_index, slot, timer_id):
        if self._loading or page_index >= len(self.draft.display.pages):
            return
        if self.draft.display.pages[page_index][slot] not in LCD_TIMER_WIDGETS:
            return
        bindings = self._ensure_lcd_timer_bindings()
        if bindings[page_index][slot] == timer_id:
            return
        bindings[page_index][slot] = timer_id
        self._changed()

    def _ensure_lcd_host_action_bindings(self):
        pages = self.draft.display.pages
        bindings = self.draft.display.host_action_bindings
        if (len(bindings) == len(pages)
                and all(isinstance(row, list) and len(row) == 4
                        for row in bindings)):
            return bindings
        normalized = [[None] * 4 for _ in pages]
        for page_index, row in enumerate(bindings[:len(normalized)]):
            if not isinstance(row, list):
                continue
            for slot, binding in enumerate(row[:4]):
                if pages[page_index][slot] in LCD_HOST_ACTION_WIDGETS:
                    normalized[page_index][slot] = binding
        self.draft.display.host_action_bindings = normalized
        return normalized

    def _ensure_lcd_host_action_icon_bindings(self):
        pages = self.draft.display.pages
        bindings = self.draft.display.host_action_icon_bindings
        if (len(bindings) == len(pages)
                and all(isinstance(row, list) and len(row) == 4
                        for row in bindings)):
            return bindings
        normalized = [[None] * 4 for _ in pages]
        for page_index, row in enumerate(bindings[:len(normalized)]):
            if not isinstance(row, list):
                continue
            for slot, binding in enumerate(row[:4]):
                if pages[page_index][slot] == "open_application":
                    normalized[page_index][slot] = binding
        self.draft.display.host_action_icon_bindings = normalized
        return normalized

    def _lcd_host_action_changed(self, page_index, slot, target):
        if self._loading or page_index >= len(self.draft.display.pages):
            return
        if self.draft.display.pages[page_index][slot] not in LCD_HOST_ACTION_WIDGETS:
            return
        bindings = self._ensure_lcd_host_action_bindings()
        target = target or None
        if bindings[page_index][slot] == target:
            return
        if self.draft.display.pages[page_index][slot] == "open_application":
            icons = self._ensure_lcd_host_action_icon_bindings()
            icons[page_index][slot] = None
            preview = self.lcd_host_action_icon_previews[page_index][slot]
            preview.setPixmap(QPixmap())
            preview.setText("No icon")
            self.lcd_host_action_icon_states[page_index][slot].setText(
                "Icon required" if target else "Choose an application")
            self.lcd_host_action_icon_auto_buttons[page_index][slot].setEnabled(
                bool(target))
            self.lcd_host_action_icon_browse_buttons[
                page_index][slot].setEnabled(bool(target))
            self.lcd_host_action_icon_clear_buttons[
                page_index][slot].setEnabled(False)
        bindings[page_index][slot] = target
        self._update_lcd_moves()
        self._changed()

    @staticmethod
    def _validate_application_target(target):
        path = Path(target)
        try:
            path.stat()
        except OSError as error:
            raise ValueError("The selected application does not exist.") from error
        is_macos_bundle = (
            sys.platform == "darwin" and path.is_dir()
            and path.suffix.lower() == ".app")
        if not is_macos_bundle and not path.is_file():
            raise ValueError(
                "Choose an executable application file or a macOS .app bundle.")
        if not is_macos_bundle and not os.access(path, os.X_OK):
            raise ValueError("The selected application is not executable.")
        return path

    def _store_lcd_application_icon(self, page_index, slot, target, prepared):
        if (page_index >= len(self.draft.display.pages)
                or slot >= len(self.draft.display.pages[page_index])
                or self.draft.display.pages[page_index][slot]
                != "open_application"):
            return
        targets = self._ensure_lcd_host_action_bindings()
        icons = self._ensure_lcd_host_action_icon_bindings()
        encoded = encode_host_action_icon_rgba(prepared.rgba)
        if (targets[page_index][slot] == target
                and icons[page_index][slot] == encoded):
            return
        targets[page_index][slot] = target
        icons[page_index][slot] = encoded
        self._load_lcd_pages()
        self._changed()

    def _use_lcd_application_icon(self, page_index, slot):
        if (page_index >= len(self.draft.display.pages)
                or slot >= len(self.draft.display.pages[page_index])
                or self.draft.display.pages[page_index][slot]
                != "open_application"):
            return
        target = self.lcd_host_action_editors[page_index][slot].text()
        try:
            path = self._validate_application_target(target)
            prepared = prepare_application_icon(path)
        except (OSError, ValueError) as error:
            self._notify(
                f"Could not use the application icon: {error}", error=True)
            return
        self._store_lcd_application_icon(
            page_index, slot, os.fspath(path), prepared)

    def _choose_lcd_application_icon(self, page_index, slot):
        if (page_index >= len(self.draft.display.pages)
                or slot >= len(self.draft.display.pages[page_index])
                or self.draft.display.pages[page_index][slot]
                != "open_application"):
            return
        target = self.lcd_host_action_editors[page_index][slot].text()
        try:
            path = self._validate_application_target(target)
        except (OSError, ValueError) as error:
            self._notify(
                f"Choose a valid application before its icon: {error}",
                error=True)
            return
        filename, _ = QFileDialog.getOpenFileName(
            self, "Choose application icon", "",
            "Images (*.png *.jpg *.jpeg);;All files (*)")
        if not filename:
            return
        try:
            prepared = prepare_custom_icon(filename)
        except (OSError, ValueError) as error:
            self._notify(
                f"Could not use the custom application icon: {error}",
                error=True)
            return
        self._store_lcd_application_icon(
            page_index, slot, os.fspath(path), prepared)

    def _clear_lcd_application_icon(self, page_index, slot):
        if (page_index >= len(self.draft.display.pages)
                or slot >= len(self.draft.display.pages[page_index])
                or self.draft.display.pages[page_index][slot]
                != "open_application"):
            return
        icons = self._ensure_lcd_host_action_icon_bindings()
        if icons[page_index][slot] is None:
            return
        icons[page_index][slot] = None
        self._load_lcd_pages()
        self._changed()

    def _choose_lcd_host_action_target(self, page_index, slot):
        if (page_index >= len(self.draft.display.pages)
                or slot >= len(self.draft.display.pages[page_index])):
            return
        widget = self.draft.display.pages[page_index][slot]
        editor = self.lcd_host_action_editors[page_index][slot]
        current = editor.text()
        if widget == "open_application":
            target, _ = QFileDialog.getOpenFileName(
                self, "Choose application to open", current,
                "Applications (*)")
            if not target:
                return
            try:
                path = self._validate_application_target(target)
                prepared = prepare_application_icon(path)
            except (OSError, ValueError) as error:
                self._notify(
                    f"Could not use the selected application: {error}",
                    error=True)
                return
            self._store_lcd_application_icon(
                page_index, slot, os.fspath(path), prepared)
            return
        if widget == "open_file":
            target, _ = QFileDialog.getOpenFileName(
                self, "Choose file to open", current, "All files (*)")
        elif widget == "open_folder":
            target = QFileDialog.getExistingDirectory(
                self, "Choose folder to open", current)
        else:
            return
        if target:
            editor.setText(target)

    def _current_countdown_timer(self):
        index = self.countdown_timer_selector.currentIndex()
        return (self.draft.countdown_timers[index]
                if 0 <= index < len(self.draft.countdown_timers) else None)

    def _refresh_countdown_timers(self, select: int = 0):
        self._timer_loading = True
        self.countdown_timer_selector.clear()
        self.countdown_timer_selector.addItems(
            [timer.name for timer in self.draft.countdown_timers])
        if self.draft.countdown_timers:
            self.countdown_timer_selector.setCurrentIndex(
                min(select, len(self.draft.countdown_timers) - 1))
        self._timer_loading = False
        self._load_countdown_timer()
        self._load_lcd_pages()

    def _load_countdown_timer(self, *_):
        if self._timer_loading:
            return
        self._timer_loading = True
        timer = self._current_countdown_timer()
        self.countdown_timer_name.setEnabled(timer is not None)
        self.countdown_timer_duration.setEnabled(timer is not None)
        self.delete_countdown_timer_button.setEnabled(timer is not None)
        self.countdown_timer_name.setText(timer.name if timer else "")
        self.countdown_timer_duration.setValue(
            timer.duration_seconds if timer else 60)
        self._timer_loading = False

    def _new_countdown_timer(self):
        timer = CountdownTimer(name=f"Timer {len(self.draft.countdown_timers) + 1}")
        self.draft.countdown_timers.append(timer)
        self._refresh_countdown_timers(len(self.draft.countdown_timers) - 1)
        self._changed()

    def _delete_countdown_timer(self):
        timer = self._current_countdown_timer()
        if timer is None:
            return
        if any(timer.id == binding
               for row in self.draft.display.timer_bindings for binding in row):
            self._notify(
                "This timer is assigned to an LCD tile. Remove its assignments before deleting it.",
                error=True)
            return
        self.draft.countdown_timers.remove(timer)
        self._refresh_countdown_timers()
        self._changed()

    def _edit_countdown_timer(self, *_):
        timer = self._current_countdown_timer()
        if self._timer_loading or timer is None:
            return
        name = self.countdown_timer_name.text().strip()
        if not name:
            self._notify("Enter a name for this count down timer.", error=True)
            self._load_countdown_timer()
            return
        timer.name = name
        timer.duration_seconds = self.countdown_timer_duration.value()
        self.countdown_timer_selector.setItemText(
            self.countdown_timer_selector.currentIndex(), timer.name)
        self._load_lcd_pages()
        self._changed()

    def _build_macros(self):
        page = self.page_layouts[5]
        self._notice(page, "macros")
        frame, body = card("Macro library", "Add key presses, releases and delays. Sequences are saved locally and are never played by this editor.")
        toolbar = QHBoxLayout()
        self.macro_selector = QComboBox()
        self.macro_selector.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.macro_selector.setMinimumContentsLength(8)
        self.macro_selector.setPlaceholderText("No macros yet")
        self.macro_selector.setAccessibleName("Select a macro")
        self.macro_selector.currentIndexChanged.connect(self._load_macro)
        toolbar.addWidget(self.macro_selector, 1)
        for text, callback in [("New", self._new_macro), ("Duplicate", self._duplicate_macro), ("Delete", self._delete_macro)]:
            button = QPushButton(text)
            button.clicked.connect(callback)
            toolbar.addWidget(button)
        self.record_macro_button = QPushButton("Record…")
        self.record_macro_button.clicked.connect(self._record_macro)
        self.record_macro_button.setToolTip("Record key presses and mouse clicks in a focused window, then append the sequence to this macro.")
        toolbar.addWidget(self.record_macro_button)
        body.addLayout(toolbar)
        form = QFormLayout()
        self.macro_name = QLineEdit()
        self.macro_name.setAccessibleName("Macro name")
        self.macro_name.editingFinished.connect(self._edit_macro_meta)
        self.macro_repeat = QSpinBox()
        self.macro_repeat.setRange(1, 999)
        self.macro_repeat.valueChanged.connect(self._edit_macro_meta)
        self.macro_playback = QComboBox()
        for value in ("once", "repeat", "while_held", "toggle"):
            self.macro_playback.addItem(title_case(value), value)
        self.macro_playback.currentIndexChanged.connect(self._edit_macro_meta)
        self.macro_playback.setToolTip("Once runs one sequence; Repeat uses the count; While held repeats until release; Toggle repeats until the assigned button is pressed again. Supports keyboard and left/right/middle/back/forward mouse events.")
        form.addRow("Name", self.macro_name)
        form.addRow("Playback", self.macro_playback)
        form.addRow("Repeat count", self.macro_repeat)
        body.addLayout(form)
        self.event_table = QTableWidget(0, 3)
        self.event_table.setHorizontalHeaderLabels(["Event", "Key / button", "Delay after (ms)"])
        self.event_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.event_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.event_table.setMinimumHeight(230)
        self.event_table.itemChanged.connect(self._edit_macro_event)
        body.addWidget(self.event_table)
        eventbar = QHBoxLayout()
        for text, callback in [("Add key press", self._add_key_press), ("Add delay", self._add_delay), ("Move up", lambda: self._move_event(-1)), ("Move down", lambda: self._move_event(1)), ("Remove", self._remove_event)]:
            button = QPushButton(text)
            button.clicked.connect(callback)
            eventbar.addWidget(button)
        body.addLayout(eventbar)
        page.addWidget(frame)
        page.addStretch()

    def _current_macro(self):
        index = self.macro_selector.currentIndex()
        return self.draft.macros[index] if 0 <= index < len(self.draft.macros) else None

    def _refresh_macros(self, select: int = 0):
        self._macro_loading = True
        self.macro_selector.clear()
        self.macro_selector.addItems([macro.name for macro in self.draft.macros])
        self.macro_selector.setCurrentIndex(min(select, len(self.draft.macros) - 1))
        self._macro_loading = False
        self._load_macro()
        self._load_lcd_pages()

    def _load_macro(self, *_):
        if self._macro_loading:
            return
        self._macro_loading = True
        macro = self._current_macro()
        self.macro_name.setEnabled(macro is not None)
        self.macro_repeat.setEnabled(macro is not None and macro.playback == "repeat")
        self.macro_playback.setEnabled(macro is not None)
        self.macro_name.setText(macro.name if macro else "")
        self.macro_repeat.setValue(macro.repeat if macro else 1)
        self.macro_playback.setCurrentIndex(self.macro_playback.findData(macro.playback) if macro else 0)
        self.event_table.setRowCount(len(macro.events) if macro else 0)
        for row, event in enumerate(macro.events if macro else []):
            kind = QComboBox()
            for value in ("key_down", "key_up", "mouse_down", "mouse_up", "delay"):
                kind.addItem(title_case(value), value)
            kind.setCurrentIndex(kind.findData(event.kind))
            kind.currentIndexChanged.connect(lambda index, r=row, c=kind: self._set_event_kind(r, c.itemData(index)))
            self.event_table.setCellWidget(row, 0, kind)
            self.event_table.setItem(row, 1, QTableWidgetItem(event.value))
            self.event_table.setItem(row, 2, QTableWidgetItem(str(event.delay_ms)))
            self.event_table.setRowHeight(row, 44)
        self._macro_loading = False

    def _new_macro(self):
        self.draft.macros.append(Macro(name=f"Macro {len(self.draft.macros) + 1}"))
        self._refresh_macros(len(self.draft.macros) - 1)
        self._load_buttons()
        self._changed()

    def _duplicate_macro(self):
        macro = self._current_macro()
        if macro:
            from uuid import uuid4
            duplicate = copy.deepcopy(macro)
            duplicate.id = str(uuid4())
            duplicate.name += " copy"
            self.draft.macros.append(duplicate)
            self._refresh_macros(len(self.draft.macros) - 1)
            self._load_buttons()
            self._changed()

    def _delete_macro(self):
        macro = self._current_macro()
        if not macro:
            return
        references = [binding for binding in self.draft.buttons if any(getattr(binding, field).kind == "macro" and getattr(binding, field).value == macro.id for field in ("primary", "easy_shift"))]
        lcd_reference = any(value == macro.id for row in self.draft.display.macro_bindings for value in row)
        if references or lcd_reference:
            targets = "a button or LCD tile" if references and lcd_reference else ("a button" if references else "an LCD tile")
            self._notify(f"This macro is assigned to {targets}. Remove its assignments before deleting it.", error=True)
            return
        self.draft.macros.remove(macro)
        self._refresh_macros()
        self._load_buttons()
        self._changed()

    def _edit_macro_meta(self, *_):
        macro = self._current_macro()
        if self._macro_loading or macro is None:
            return
        macro.name = self.macro_name.text().strip()
        macro.repeat = self.macro_repeat.value()
        macro.playback = self.macro_playback.currentData()
        self.macro_repeat.setEnabled(macro.playback == "repeat")
        self.macro_selector.setItemText(self.macro_selector.currentIndex(), macro.name)
        self._load_buttons()
        self._load_lcd_pages()
        self._changed()

    def _set_event_kind(self, row, kind):
        macro = self._current_macro()
        if macro and not self._macro_loading:
            macro.events[row].kind = kind
            if kind == "delay":
                macro.events[row].value = ""
                self._load_macro()
            self._changed()

    def _edit_macro_event(self, item):
        macro = self._current_macro()
        if self._macro_loading or macro is None:
            return
        event = macro.events[item.row()]
        if item.column() == 1:
            event.value = item.text().strip()
        elif item.column() == 2:
            try:
                number = int(item.text())
                if not 0 <= number <= 60000:
                    raise ValueError()
            except ValueError:
                self._notify("Event delays must be whole numbers between 0 and 60,000 ms.", error=True)
                self._load_macro()
                return
            event.delay_ms = number
        self._changed()

    def _add_key_press(self):
        macro = self._current_macro()
        if macro is None:
            self._new_macro()
            macro = self._current_macro()
        value, accepted = QInputDialog.getText(self, "Add key press", "Key name, for example A, Space or F5:")
        if accepted and value.strip():
            macro.events.extend([MacroEvent(kind="key_down", value=value.strip(), delay_ms=50), MacroEvent(kind="key_up", value=value.strip(), delay_ms=0)])
            self._load_macro()
            self._changed()

    def _record_macro(self):
        macro = self._current_macro()
        if macro is not None:
            try:
                Configuration(macros=[copy.deepcopy(macro)]).validate()
            except ValueError as error:
                self._notify(f"Correct this macro before recording: {error}", error=True)
                return
        if macro is None and len(self.draft.macros) >= MAX_MACROS:
            self._notify("A preset can contain at most 64 macros. Select an existing macro or remove one before recording.", error=True)
            return
        remaining = MAX_MACRO_EVENTS - (len(macro.events) if macro else 0)
        if remaining < 2:
            self._notify("This macro has no room for another key press. Remove events or create a new macro before recording.", error=True)
            return
        dialog = RecordingDialog(self, max_events=remaining)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        recorded_events = copy.deepcopy(dialog.recorded_events) if accepted else []
        dialog.deleteLater()
        if not accepted:
            return
        candidate = copy.deepcopy(macro) if macro else Macro(name=f"Macro {len(self.draft.macros) + 1}")
        candidate.events.extend(recorded_events)
        try:
            Configuration(macros=[candidate]).validate()
        except ValueError as error:
            self._notify(f"Correct this macro before appending a recording: {error}", error=True)
            return
        if macro:
            macro.events = candidate.events
            self._load_macro()
        else:
            self.draft.macros.append(candidate)
            self._refresh_macros(len(self.draft.macros) - 1)
            self._load_buttons()
        self._changed()
        self._notify(f"Added {len(recorded_events)} recorded events to the local macro. Save preset to keep the sequence.")

    def _add_delay(self):
        if self._current_macro() is None:
            self._new_macro()
        self._current_macro().events.append(MacroEvent(kind="delay", value="", delay_ms=100))
        self._load_macro()
        self._changed()

    def _move_event(self, direction):
        macro = self._current_macro()
        row = self.event_table.currentRow()
        other = row + direction
        if macro and 0 <= row < len(macro.events) and 0 <= other < len(macro.events):
            macro.events[row], macro.events[other] = macro.events[other], macro.events[row]
            self._load_macro()
            self.event_table.selectRow(other)
            self._changed()

    def _remove_event(self):
        macro = self._current_macro()
        row = self.event_table.currentRow()
        if macro and 0 <= row < len(macro.events):
            del macro.events[row]
            self._load_macro()
            self._changed()

    def _build_profiles(self):
        page = self.page_layouts[6]
        frame, body = card(
            "Current draft",
            "Preset names, colors and images identify files in this local library. "
            "The mouse profile slot is saved as part of the draft.",
        )
        form = QFormLayout()
        self.preset_name = QLineEdit(self.draft.name)
        self.preset_name.setAccessibleName("Preset name")
        self.preset_name.textEdited.connect(self._name_changed)
        self.profile_color = ColorButton(self.draft.appearance.color)
        self.profile_color.setAccessibleName("Choose profile library color")
        self.profile_color.colorChanged.connect(self._profile_color_changed)
        self.profile_image_preview = QLabel("No image")
        self.profile_image_preview.setAccessibleName("Profile library image preview")
        self.profile_image_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.profile_image_preview.setFixedSize(104, 104)
        self.choose_profile_image_button = QPushButton("Choose image…")
        self.choose_profile_image_button.clicked.connect(self.choose_profile_image)
        self.clear_profile_image_button = QPushButton("Remove image")
        self.clear_profile_image_button.clicked.connect(self.clear_profile_image)
        image_buttons = QVBoxLayout()
        image_buttons.setContentsMargins(0, 0, 0, 0)
        image_buttons.addWidget(self.choose_profile_image_button)
        image_buttons.addWidget(self.clear_profile_image_button)
        image_buttons.addStretch()
        image_row = QWidget()
        image_layout = QHBoxLayout(image_row)
        image_layout.setContentsMargins(0, 0, 0, 0)
        image_layout.addWidget(self.profile_image_preview)
        image_layout.addLayout(image_buttons)
        image_layout.addStretch()
        self.profile_slot = QSpinBox()
        self.profile_slot.setRange(1, 5)
        self.profile_slot.setAccessibleName("Mouse profile slot")
        self.profile_slot.valueChanged.connect(self._slot_changed)
        form.addRow("Preset name", self.preset_name)
        form.addRow("Library color", self.profile_color)
        form.addRow("Library image", image_row)
        form.addRow("Mouse profile slot", self.profile_slot)
        self.activate_profile_button = QPushButton("Activate this profile")
        self.activate_profile_button.clicked.connect(self.activate_profile)
        form.addRow("Onboard profile", self.activate_profile_button)
        self.active_profile_status = label("Active mouse profile: not read", "muted", True)
        form.addRow("", self.active_profile_status)
        body.addLayout(form)
        body.addWidget(label(
            "Library appearance is embedded in saved and exported JSON presets. "
            "It stays on this computer and is never written to the mouse.",
            "muted", True,
        ))
        actions = QHBoxLayout()
        for text, callback in [("New draft", self.new_draft), ("Duplicate draft", self.duplicate_draft), ("Import…", self.import_preset), ("Export…", self.export_preset)]:
            button = QPushButton(text)
            button.clicked.connect(callback)
            actions.addWidget(button)
        body.addLayout(actions)
        page.addWidget(frame)
        frame, body = card("Saved presets")
        self.preset_list = QListWidget()
        self.preset_list.setAccessibleName("Saved presets")
        self.preset_list.setIconSize(QSize(40, 40))
        self.preset_list.itemDoubleClicked.connect(lambda _: self.load_preset())
        self.preset_list.setMinimumHeight(170)
        body.addWidget(self.preset_list)
        actions = QHBoxLayout()
        load = QPushButton("Load selected")
        load.clicked.connect(self.load_preset)
        delete = QPushButton("Delete selected")
        delete.clicked.connect(self.delete_preset)
        actions.addWidget(load)
        actions.addWidget(delete)
        actions.addStretch()
        body.addLayout(actions)
        page.addWidget(frame)
        frame, body = card(
            "Automatic profiles",
            "Choose an onboard profile from the application currently in the foreground. "
            "Exact paths and bundle identifiers stay on this computer.",
        )
        self.automatic_profile_summary = label("", "muted", True)
        body.addWidget(self.automatic_profile_summary)
        self.automatic_profile_status = label("", "muted", True)
        self.automatic_profile_status.setAccessibleName("Automatic profile monitor status")
        body.addWidget(self.automatic_profile_status)
        self.automatic_profiles_button = QPushButton("Manage automatic profiles…")
        self.automatic_profiles_button.clicked.connect(self.manage_automatic_profiles)
        body.addWidget(self.automatic_profiles_button, 0, Qt.AlignmentFlag.AlignLeft)
        page.addWidget(frame)
        page.addStretch()

    def manage_automatic_profiles(self):
        if self._device_or_dialog_busy():
            return
        from .automatic_profiles import AutomaticProfilesDialog

        dialog = self._automatic_profile_dialog = AutomaticProfilesDialog(
            self._automatic_settings, self
        )
        self._update_capabilities()
        try:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            settings = dialog.result_settings
        finally:
            self._automatic_profile_dialog = None
            dialog.deleteLater()
            self._update_capabilities()
        if not accepted or settings is None:
            return
        try:
            self.automatic_store.save(settings)
        except (OSError, ValueError) as error:
            self._notify(f"Automatic profile rules could not be saved: {error}", error=True)
            return
        self._automatic_settings = settings
        self._automatic_settings_error = ""
        self._invalidate_automatic_profile_context()
        self._sync_automatic_profile_monitor()
        state = "enabled" if settings.enabled else "disabled"
        self._notify(f"Automatic profile switching {state}. Rules were saved on this computer.")

    def _name_changed(self, name):
        if not self._loading:
            self.draft.name = name
            self._changed()

    def _profile_color_changed(self, color):
        if self._loading:
            return
        self.draft.appearance.color = color.upper()
        self._load_profile_appearance()
        self._changed()

    def choose_profile_image(self):
        filename, _ = QFileDialog.getOpenFileName(
            self, "Choose profile image", "", "PNG or JPEG images (*.png *.jpg *.jpeg)")
        if not filename:
            return
        try:
            prepared = prepare_profile_image(filename)
        except (OSError, ValueError) as error:
            self._notify(f"Could not use profile image: {error}", error=True)
            return
        self.draft.appearance.image = prepared.data_url
        self._load_profile_appearance()
        self._changed()
        self._notify(
            "Profile image embedded in this draft. Save or export the preset to keep it.")

    def clear_profile_image(self):
        if self.draft.appearance.image is None:
            return
        self.draft.appearance.image = None
        self._load_profile_appearance()
        self._changed()
        self._notify("Profile image removed from this draft.")

    def _load_profile_appearance(self):
        appearance = self.draft.appearance
        self.profile_color.set_color(appearance.color)
        self.profile_image_preview.setStyleSheet(
            f"background: #101216; border: 3px solid {appearance.color}; "
            "border-radius: 9px; padding: 1px;")
        image = decode_profile_image(appearance.image)
        if image.isNull():
            self.profile_image_preview.setPixmap(QPixmap())
            self.profile_image_preview.setText("No image")
        else:
            self.profile_image_preview.setText("")
            self.profile_image_preview.setPixmap(QPixmap.fromImage(image))
        self.clear_profile_image_button.setEnabled(appearance.image is not None)

    def _slot_changed(self, value):
        if not self._loading:
            self.draft.profile_slot = value
            self._changed()
            self._update_capabilities()

    def _refresh_presets(self):
        self.preset_list.clear()
        try:
            self._presets = self.store.list()
            for preset in self._presets:
                item = QListWidgetItem(profile_icon(preset.appearance), preset.name)
                image = "custom image embedded" if preset.appearance.image else "no image"
                item.setToolTip(f"Color {preset.appearance.color.upper()} · {image}")
                self.preset_list.addItem(item)
        except (OSError, ValueError) as error:
            self._presets = []
            self._notify(f"Could not read saved presets: {error}", error=True)

    def save_preset(self) -> bool:
        try:
            self.store.save(self.draft)
        except (OSError, ValueError) as error:
            self._notify(f"Preset could not be saved: {error}", error=True)
            return False
        self.dirty = False
        self._saved = True
        self._update_draft_status()
        self._refresh_presets()
        self._notify(f"Saved “{self.draft.name}” on this computer.")
        return True

    def _can_replace_draft(self) -> bool:
        if not self.dirty:
            return True
        choice = QMessageBox.question(self, "Unsaved draft", "Save your draft before continuing?", QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Save)
        if choice == QMessageBox.StandardButton.Save:
            return self.save_preset()
        return choice == QMessageBox.StandardButton.Discard

    def new_draft(self):
        if self._can_replace_draft():
            if self._countdown_process is not None:
                self._request_countdown_stop("The local draft changed.")
            self.draft = Configuration()
            self.dirty = False
            self._saved = False
            self._load_draft()
            self._notify("New local draft. Its values have not been read from the mouse.")

    def duplicate_draft(self):
        if self._countdown_process is not None:
            self._request_countdown_stop("The local draft changed.")
        self.draft = copy.deepcopy(self.draft)
        self.draft.name += " copy"
        self.dirty = True
        self._saved = False
        self._load_draft()

    def load_preset(self):
        index = self.preset_list.currentRow()
        if 0 <= index < len(self._presets) and self._can_replace_draft():
            if self._countdown_process is not None:
                self._request_countdown_stop("The local draft changed.")
            self.draft = copy.deepcopy(self._presets[index])
            self.dirty = False
            self._saved = True
            self._load_draft()
            self._notify(f"Loaded “{self.draft.name}” into the editor. Mouse settings have not changed.")

    def delete_preset(self):
        item = self.preset_list.currentItem()
        if item is None:
            return
        name = item.text()
        choice = QMessageBox.question(self, "Delete preset", f"Delete the saved preset “{name}” from this computer?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if choice == QMessageBox.StandardButton.Yes:
            try:
                self.store.delete(name)
                self._refresh_presets()
                self._notify(f"Deleted saved preset “{name}”.")
            except (OSError, ValueError) as error:
                self._notify(str(error), error=True)

    def import_preset(self):
        if not self._can_replace_draft():
            return
        filename, _ = QFileDialog.getOpenFileName(self, "Import preset", "", "MC7 preset (*.json)")
        if filename:
            try:
                imported = self.store.import_file(Path(filename), save=False)
                if self._countdown_process is not None:
                    self._request_countdown_stop("The local draft changed.")
                self.draft = imported
                self.dirty = True
                self._saved = False
                self._load_draft()
                self._notify("Imported into the local draft. Save preset to keep a copy in your library.")
            except (OSError, ValueError) as error:
                self._notify(f"Could not import preset: {error}", error=True)

    def export_preset(self):
        filename, _ = QFileDialog.getSaveFileName(self, "Export preset", "mc7-preset.json", "MC7 preset (*.json)")
        if filename:
            try:
                self.store.export(self.draft, Path(filename))
                self._notify(f"Exported preset to {filename}.")
            except (OSError, ValueError) as error:
                self._notify(f"Could not export preset: {error}", error=True)

    def _build_device(self):
        page = self.page_layouts[7]
        frame, body = card("Connection")
        self.device_selector = QComboBox()
        self.device_selector.setAccessibleName("Connected mouse")
        self.device_selector.currentIndexChanged.connect(self._select_device)
        body.addWidget(self.device_selector)
        self.device_summary = label("No device read yet.", "muted", True)
        self.device_summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.addWidget(self.device_summary)
        self.device_status = label("Firmware, battery and charging status: not read", "muted", True)
        body.addWidget(self.device_status)
        self.status_read_note = label("Battery status has not been read.", "muted", True)
        body.addWidget(self.status_read_note)
        self.refresh_button = QPushButton("Refresh devices")
        self.refresh_button.clicked.connect(self.refresh_device)
        self.status_read_button = QPushButton("Read firmware and battery")
        self.status_read_button.clicked.connect(self.refresh_status)
        self.firmware_button = QPushButton("Firmware…")
        self.firmware_button.clicked.connect(self.manage_firmware)
        self.restore_button = QPushButton("Restore backup…")
        self.restore_button.clicked.connect(lambda: self.manage_restore())
        status_actions = QHBoxLayout()
        status_actions.addWidget(self.refresh_button)
        status_actions.addWidget(self.status_read_button)
        status_actions.addWidget(self.firmware_button)
        status_actions.addWidget(self.restore_button)
        status_actions.addStretch()
        body.addLayout(status_actions)
        page.addWidget(frame)
        frame, body = card(
            "Host integration",
            "Manage per-user startup, the GNOME Wayland automatic-profile companion and Linux MC7 USB access.",
        )
        body.addWidget(label(
            "Startup and GNOME companion installation apply per user. Installing the udev rule requests administrator authorization and requires reconnecting the mouse.",
            "muted", True,
        ))
        self.host_integrations_button = QPushButton("Check and install host integration…")
        self.host_integrations_button.clicked.connect(self.manage_host_integrations)
        body.addWidget(self.host_integrations_button, 0, Qt.AlignmentFlag.AlignLeft)
        page.addWidget(frame)
        frame, body = card("Battery monitoring", "These options apply to this session. Status refreshes leave your preset edits and configuration read untouched.")
        self.battery_monitor_checkbox = QCheckBox("Monitor battery every minute and show a tray icon")
        self.battery_monitor_checkbox.toggled.connect(self._monitor_toggled)
        self.battery_notifications_checkbox = QCheckBox("Notify when battery reaches 20%")
        self.battery_notifications_checkbox.toggled.connect(self._sync_tray_options)
        self.keep_running_checkbox = QCheckBox("Keep running in the tray when the window is closed")
        self.keep_running_checkbox.toggled.connect(self._sync_tray_options)
        for checkbox in (self.battery_monitor_checkbox, self.battery_notifications_checkbox, self.keep_running_checkbox):
            body.addWidget(checkbox)
        self.tray_availability = label("", "muted", True)
        body.addWidget(self.tray_availability)
        page.addWidget(frame)
        frame, body = card("Feature availability")
        self.capability_summary = label("", "muted", True)
        body.addWidget(self.capability_summary)
        page.addWidget(frame)
        form = self._form(
            page, "Power preferences",
            "Read the mouse before applying power preferences. Delays use the "
            "same minute values as Swarm II.",
        )
        self.standby_timeout = self._spin(
            form, "Standby delay (minutes)", "power", "standby_value", 1, 30,
            " min",
        )
        self.standby_timeout.setToolTip(
            "Swarm II offers standby delays from 1 to 30 minutes."
        )
        self.led_timeout = self._spin(
            form, "Lighting timeout (minutes)", "power", "led_timeout_value",
            0, 30, " min",
        )
        self.led_timeout.setToolTip(
            "This per-profile value is stored as minutes. The physical meaning "
            "of the vendor's 0-minute value still needs a timed device test."
        )
        self._check(form, "ECO mode", "power", "eco_mode")
        self._check(form, "Energy saving", "power", "energy_saving")
        page.addWidget(label("Open Firmware to download, prepare and install mouse firmware. Updates that require a settings reset explain it before installation.", "notice", True))
        page.addStretch()

    def manage_host_integrations(self):
        if self._device_or_dialog_busy():
            return
        from .host_integrations import HostIntegrationsDialog

        dialog = self._host_integrations_dialog = HostIntegrationsDialog(
            self.host_integration_manager, self
        )
        self._update_capabilities()
        try:
            dialog.exec()
        finally:
            self._host_integrations_dialog = None
            dialog.deleteLater()
            self._update_capabilities()

    def manage_firmware(self):
        if self._device_or_dialog_busy():
            return
        from .firmware import FirmwareDialog
        from ..firmware_service import FirmwareService
        device = next((item for item in self.devices if item['id'] == self.selected_device_id), {})
        device_id = self.selected_device_id if "read_status" in device.get('capabilities', []) else None
        dialog = self._firmware_dialog = FirmwareDialog(FirmwareService(self.service), device_id, self)
        self._update_capabilities()
        try:
            dialog.exec()
            changed = dialog.device_may_have_changed
            outcome = dialog.outcome
            restore_requested = dialog.restore_requested
            restore_path = dialog.restore_backup_path
        finally:
            self._firmware_dialog = None
            dialog.deleteLater()
            self._update_capabilities()
        if changed:
            self.host_lcd_checkbox.setChecked(False)
            self.snapshot = None
            self._set_device_status(None, note="Read the mouse after firmware installation.")
            self._update_capabilities()
            self._notify("Firmware operation finished. Refresh devices and read the mouse again; your local draft is preserved." if outcome and outcome.get('verified') else "Firmware installation could not be verified. Check the mouse connection and read its firmware before continuing.", error=not bool(outcome and outcome.get('verified')))
        if restore_requested is True:
            QTimer.singleShot(0, lambda: self.manage_restore(restore_path))

    def manage_restore(self, initial_path=None):
        if self._device_or_dialog_busy():
            return
        from .restore import RestoreDialog
        from ..restore_service import RestoreService
        device = next((item for item in self.devices if item['id'] == self.selected_device_id), {})
        device_id = self.selected_device_id if 'read_settings' in device.get('capabilities', []) else None
        dialog = self._restore_dialog = RestoreDialog(RestoreService(), device_id, self, initial_path=initial_path)
        self._update_capabilities()
        try:
            dialog.exec()
            changed, outcome = dialog.device_may_have_changed, dialog.outcome
        finally:
            self._restore_dialog = None
            dialog.deleteLater()
            self._update_capabilities()
        if changed:
            self.host_lcd_checkbox.setChecked(False)
            self.snapshot = None
            self._set_device_status(None, note='Read the mouse after restoring settings.')
            self._update_capabilities()
            self._notify('Supported settings restored and verified. Read the mouse to load them; your local draft is preserved.'
                         if outcome and outcome.get('verified') else
                         'Restoration could not be verified. Some settings may have changed; read the mouse before continuing.',
                         error=not bool(outcome and outcome.get('verified')))

    def _load_draft(self):
        self._loading = True
        try:
            for load in self._field_loaders:
                load()
            self.preset_name.setText(self.draft.name)
            self.profile_slot.setValue(self.draft.profile_slot)
            self._load_profile_appearance()
            for index, (enabled, value, color) in enumerate(self.dpi_controls):
                stage = self.draft.sensor.stages[index]
                enabled.setChecked(stage.enabled)
                value.setValue(stage.value)
                color.set_color(stage.color)
                self.current_stage.model().item(index).setEnabled(stage.enabled)
            self._load_lcd_pages()
            self._load_buttons()
            self._refresh_macros()
            self._refresh_countdown_timers()
            self._populate_media_players()
        finally:
            self._loading = False
        self._update_draft_status()
        self._update_capabilities()

    def _device_or_dialog_busy(self):
        return (self._job is not None or self._dcu_dialog is not None
                or self._firmware_dialog is not None or self._restore_dialog is not None
                or self._automatic_profile_dialog is not None
                or self._host_integrations_dialog is not None
                or self._countdown_process is not None
                or self._countdown_close_pending)

    def _invalidate_automatic_profile_context(self):
        self._automatic_revision += 1
        self._automatic_candidate = None
        self._automatic_candidate_count = 0
        self._automatic_handled_candidate = None
        self._automatic_pending_switch = None
        self._automatic_active_action = None

    def _update_automatic_profile_card(self):
        settings = self._automatic_settings
        enabled_rules = sum(rule.enabled for rule in settings.rules)
        state = "On" if settings.enabled else "Off"
        noun = "rule" if len(settings.rules) == 1 else "rules"
        self.automatic_profile_summary.setText(
            f"{state} · {enabled_rules} of {len(settings.rules)} {noun} enabled · "
            f"default profile {settings.default_profile_slot}"
        )
        if self._automatic_settings_error:
            self.automatic_profile_status.setText(
                "Automatic switching is off because its rules could not be read: "
                + self._automatic_settings_error
            )
        elif not settings.enabled:
            self.automatic_profile_status.setText("Automatic profile switching is off.")

    def _sync_automatic_profile_monitor(self, *, initial=False):
        self._update_automatic_profile_card()
        if self._automatic_settings.enabled and not self._automatic_settings_error:
            self.automatic_profile_timer.start()
            if not initial:
                QTimer.singleShot(0, self._poll_automatic_profile)
        else:
            self.automatic_profile_timer.stop()
            self._invalidate_automatic_profile_context()
        self._sync_tray_options()

    def _automatic_device(self):
        device = next(
            (item for item in self.devices if item["id"] == self.selected_device_id),
            None,
        )
        capabilities = device.get("capabilities", ()) if device else ()
        if (device is None or "read_active_profile" not in capabilities
                or "switch_profile" not in capabilities
                or not callable(getattr(self.service, "read_active_profile", None))
                or not callable(getattr(self.service, "switch_profile", None))):
            return None
        return device

    def _automatic_pause_reason(self):
        if self._device_or_dialog_busy():
            return "another mouse operation or dialog is active"
        if QApplication.activeModalWidget() is not None:
            return "a dialog is active"
        if self._automatic_device() is None:
            return "select a directly connected MC7 with profile access"
        return None

    def _poll_automatic_profile(self):
        if not self._automatic_settings.enabled or self._automatic_settings_error:
            return
        reason = self._automatic_pause_reason()
        if reason is not None:
            self._automatic_candidate = None
            self._automatic_candidate_count = 0
            self.automatic_profile_status.setText(
                f"Automatic switching paused: {reason}."
            )
            return
        if self._automatic_scan_job is not None:
            return
        revision, device_id = self._automatic_revision, self.selected_device_id
        job = ApplicationScanJob(self.application_monitor.poll, self)
        self._automatic_scan_job = job
        job.completed.connect(
            lambda result, current=job, rev=revision, device=device_id:
            self._automatic_profile_sampled(current, rev, device, result)
        )
        job.failed.connect(
            lambda message, current=job, rev=revision, device=device_id:
            self._automatic_profile_scan_failed(current, rev, device, message)
        )
        job.finished.connect(lambda current=job: self._automatic_scan_finished(current))
        job.start()

    def _automatic_scan_finished(self, job):
        if self._automatic_scan_job is job:
            self._automatic_scan_job = None
        job.deleteLater()

    def _automatic_sample_is_current(self, job, revision, device_id):
        return (self._automatic_scan_job is job
                and revision == self._automatic_revision
                and device_id == self.selected_device_id
                and self._automatic_settings.enabled)

    def _automatic_profile_scan_failed(self, job, revision, device_id, message):
        if not self._automatic_sample_is_current(job, revision, device_id):
            return
        self._automatic_candidate = None
        self._automatic_candidate_count = 0
        self._automatic_handled_candidate = None
        detail = str(message).strip() or "the foreground application could not be read"
        self.automatic_profile_status.setText(
            f"Automatic switching paused after a foreground check failed: {detail}."
        )

    def _automatic_profile_sampled(self, job, revision, device_id, sample):
        if not self._automatic_sample_is_current(job, revision, device_id):
            return
        if self._automatic_pause_reason() is not None:
            self._automatic_candidate = None
            self._automatic_candidate_count = 0
            return
        if type(sample) is not ForegroundApplicationSnapshot:
            self._automatic_profile_scan_failed(
                job, revision, device_id, "the monitor returned an invalid result"
            )
            return
        if sample.status not in (
            ForegroundApplicationStatus.AVAILABLE,
            ForegroundApplicationStatus.NO_APPLICATION,
        ):
            self._automatic_candidate = None
            self._automatic_candidate_count = 0
            self._automatic_handled_candidate = None
            message = sample.message.strip() or sample.status.value.replace("_", " ")
            self.automatic_profile_status.setText(
                f"Automatic switching paused: {message}"
                + ("" if message.endswith(".") else ".")
            )
            return
        try:
            selection = resolve_profile(self._automatic_settings, sample.applications)
        except ValueError as error:
            self._automatic_candidate = None
            self._automatic_candidate_count = 0
            self._automatic_handled_candidate = None
            self.automatic_profile_status.setText(
                f"Automatic switching paused: {error}"
            )
            return
        if selection is None:
            return
        application = sample.application
        observed = (
            sample.status.value,
            application.executable_path if application else None,
            application.bundle_id if application else None,
        )
        key = (
            device_id,
            selection.profile_slot,
            selection.source,
            selection.rule.rule_id if selection.rule else None,
            observed,
        )
        if key == self._automatic_handled_candidate:
            return
        if self._automatic_handled_candidate is not None:
            self._automatic_handled_candidate = None
        if key == self._automatic_candidate:
            self._automatic_candidate_count += 1
        else:
            self._automatic_candidate = key
            self._automatic_candidate_count = 1
        description = self._automatic_selection_description(selection)
        if self._automatic_candidate_count < 2:
            self.automatic_profile_status.setText(
                f"Confirming {description} for profile {selection.profile_slot}…"
            )
            return
        context = {
            "revision": revision,
            "device_id": device_id,
            "selection": selection,
            "candidate": key,
        }
        self._automatic_candidate = None
        self._automatic_candidate_count = 0
        self._automatic_active_action = context
        self.automatic_profile_status.setText(
            f"Checking the active mouse profile for {description}…"
        )
        started = self._run_job(
            lambda: self.service.read_active_profile(device_id),
            lambda result, current=context: self._automatic_profile_read(current, result),
            "",
            telemetry=True,
            failed=lambda message, current=context:
            self._automatic_profile_operation_failed(current, message),
        )
        if not started:
            self._automatic_active_action = None
            self.automatic_profile_status.setText(
                "Automatic switching paused before checking the mouse."
            )

    @staticmethod
    def _automatic_selection_description(selection: ProfileSelection):
        if selection.source == "application" and selection.rule is not None:
            return f"“{selection.rule.name}”"
        return "the default rule"

    def _automatic_action_is_current(self, context):
        return (self._automatic_active_action is context
                and context["revision"] == self._automatic_revision
                and context["device_id"] == self.selected_device_id
                and self._automatic_settings.enabled)

    def _automatic_profile_read(self, context, result):
        if not self._automatic_action_is_current(context):
            return
        active = result.get("active_profile") if isinstance(result, dict) else None
        if type(active) is not int or not 1 <= active <= 5:
            self._automatic_profile_operation_failed(
                context, "the mouse returned an invalid active profile"
            )
            return
        self._patch_cached_active_profile(context["device_id"], active)
        target = context["selection"].profile_slot
        if active == target:
            description = self._automatic_selection_description(context["selection"])
            self.automatic_profile_status.setText(
                f"Profile {target} is already active for {description}."
            )
            self._automatic_handled_candidate = context["candidate"]
            self._automatic_active_action = None
            return
        context["active_before"] = active
        self._automatic_pending_switch = context
        if self.host_lcd_checkbox.isChecked():
            self.host_lcd_checkbox.setChecked(False)
        description = self._automatic_selection_description(context["selection"])
        self.automatic_profile_status.setText(
            f"Switching from profile {active} to {target} for {description}…"
        )

    def _start_automatic_profile_switch(self, context):
        if not self._automatic_action_is_current(context):
            self._automatic_active_action = None
            return
        if self._automatic_pause_reason() is not None:
            self._automatic_active_action = None
            self.automatic_profile_status.setText(
                "Automatic switching paused before changing the mouse profile."
            )
            return
        device_id = context["device_id"]
        target = context["selection"].profile_slot
        started = self._run_job(
            lambda: self.service.switch_profile(device_id, target),
            lambda result, current=context:
            self._automatic_profile_switched(current, result),
            "",
            telemetry=True,
            failed=lambda message, current=context:
            self._automatic_profile_operation_failed(current, message),
        )
        if not started:
            self._automatic_active_action = None
            self.automatic_profile_status.setText(
                "Automatic switching paused before changing the mouse profile."
            )

    def _automatic_profile_switched(self, context, result):
        if not self._automatic_action_is_current(context):
            return
        target = context["selection"].profile_slot
        summary = result.get("summary", {}) if isinstance(result, dict) else {}
        active = summary.get("active_profile")
        if type(active) is not int or active != target:
            self._automatic_profile_operation_failed(
                context, "the profile change was not verified by the mouse"
            )
            return
        # The fresh read immediately before this job reported another slot.
        # Even if a simultaneous onboard change lets the service skip its own
        # write, the active-profile state changed and the settings baseline is
        # no longer safe for Apply.
        changed = context.get("active_before") != target
        self._patch_cached_active_profile(context["device_id"], target)
        if changed:
            self.snapshot = None
            self.overview_read_status.setText(
                "Mouse profile changed automatically · Read settings before applying"
            )
            self._update_capabilities()
        description = self._automatic_selection_description(context["selection"])
        verb = "Switched to" if changed else "Verified"
        self.automatic_profile_status.setText(
            f"{verb} profile {target} for {description}."
        )
        self._automatic_handled_candidate = context["candidate"]
        self._automatic_active_action = None

    def _automatic_profile_operation_failed(self, context, message):
        if not self._automatic_action_is_current(context):
            return
        self._automatic_pending_switch = None
        self._automatic_active_action = None
        self._automatic_candidate = None
        self._automatic_candidate_count = 0
        self._automatic_handled_candidate = None
        detail = str(message).strip() or "the mouse operation failed"
        self.automatic_profile_status.setText(
            f"Automatic switching failed: {detail}. Waiting for fresh foreground checks."
        )

    def _patch_cached_active_profile(self, device_id, profile_slot):
        if (self.snapshot is not None
                and self.snapshot.get("device_id") == device_id):
            summary = dict(self.snapshot.get("summary", {}))
            summary["active_profile"] = profile_slot
            self.snapshot["summary"] = summary
        if device_id == self.selected_device_id:
            now = datetime.now().astimezone().strftime("%H:%M:%S")
            self.active_profile_status.setText(
                f"Active mouse profile: {profile_slot} · Verified {now}"
            )

    def _sync_tray_options(self, *_):
        automatic = bool(self._automatic_settings.enabled
                         and not self._automatic_settings_error)
        monitoring = self.battery_monitor_checkbox.isChecked() or automatic
        tray_active = self.battery_tray.set_enabled(monitoring)
        battery_monitoring = self.battery_monitor_checkbox.isChecked()
        self.battery_notifications_checkbox.setEnabled(
            tray_active and battery_monitoring and self.battery_tray.messages_supported()
        )
        self.keep_running_checkbox.setEnabled(tray_active)
        if not tray_active and self.keep_running_checkbox.isChecked():
            self.keep_running_checkbox.blockSignals(True)
            self.keep_running_checkbox.setChecked(False)
            self.keep_running_checkbox.blockSignals(False)
        self.battery_tray.set_notifications(
            tray_active and battery_monitoring
            and self.battery_notifications_checkbox.isChecked()
        )
        keep_running = tray_active and self.keep_running_checkbox.isChecked()
        QApplication.instance().setQuitOnLastWindowClosed(False if keep_running else self._initial_quit_policy)
        if not tray_active and self._hidden_to_tray:
            self.show_from_tray()
        if not self.battery_tray.available():
            self.tray_availability.setText("This desktop has no system tray. Monitoring still updates this window; keeping it hidden and tray notifications are unavailable.")
        elif not self.battery_tray.messages_supported():
            self.tray_availability.setText("The tray icon is available. This desktop does not provide tray notifications.")
        else:
            self.tray_availability.setText("Low-battery alerts are suppressed while charging. Another alert is allowed after the battery rises to 25%. Desktop settings may suppress notifications.")

    def _monitor_toggled(self, enabled):
        self._status_refresh_due = bool(enabled)
        if enabled:
            self.status_timer.start()
        else:
            self.status_timer.stop()
        self._sync_tray_options()
        if enabled:
            self.refresh_status()

    def _poll_status(self):
        if not self.battery_monitor_checkbox.isChecked():
            return
        self._sync_tray_options()
        if not self._device_or_dialog_busy() and QApplication.activeModalWidget() is None:
            self.refresh_status()

    def refresh_status(self, *_):
        device_id = self.selected_device_id
        device = next((item for item in self.devices if item["id"] == device_id), {})
        if (self._device_or_dialog_busy()
                or "read_status" not in device.get("capabilities", [])
                or not callable(getattr(self.service, "read_status", None))):
            return
        self._status_refresh_due = False
        self.status_read_note.setText("Reading battery status…")
        self._run_job(lambda: self.service.read_status(device_id),
                      lambda status: self._status_received(device_id, status), "",
                      telemetry=True, failed=lambda message: self._status_failed(device_id, message))

    def _status_received(self, device_id, status):
        if device_id == self.selected_device_id:
            self._set_device_status(status, note=f"Battery status updated at {datetime.now().astimezone().strftime('%H:%M:%S')}.")

    def _status_failed(self, device_id, message):
        if device_id == self.selected_device_id:
            self._set_device_status(None, note=f"Battery status unavailable: {message}")

    def _set_device_status(self, status, *, note=None):
        self._last_status = normalized_status(status)
        battery = self._last_status["battery_percent"]
        charging = self._last_status["charging"]
        battery_text = f"{battery}%" if battery is not None else "not reported"
        charging_text = "charging" if charging is True else "not charging"
        self.overview_battery.setText(f"Battery: {battery_text}" + (f" · {charging_text}" if charging is not None else ""))
        self.overview_firmware.setText(f"Mouse firmware: {self._last_status['firmware_version'] or 'not reported'}")
        self.device_status.setText(f"Firmware: {self._last_status['firmware_version'] or 'not reported'}\nBattery: {battery_text}\nCharging: {'yes' if charging is True else 'no' if charging is False else 'not reported'}")
        if note is not None:
            self.status_read_note.setText(note)
        self.battery_tray.update_status(self.selected_device_id, self._last_status)

    def show_from_tray(self):
        self._hidden_to_tray = False
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit_from_tray(self):
        self.show_from_tray()
        self._tray_quit_requested = True
        try:
            if self.close():
                QApplication.instance().quit()
        finally:
            self._tray_quit_requested = False

    def _changed(self):
        if self._loading:
            return
        if self._countdown_process is not None:
            self._request_countdown_stop("The local draft changed.")
        self.dirty = True
        self._update_draft_status()

    def _update_draft_status(self):
        self.setWindowTitle(f"MC7 Studio — {self.draft.name}{' *' if self.dirty else ''}")
        self.draft_badge.setText("LOCAL DRAFT · UNSAVED" if self.dirty else "LOCAL DRAFT")
        self.overview_preset.setText(self.draft.name or "Unnamed draft")
        self.footer_status.setText("Unsaved local changes" if self.dirty else "Preset saved on this computer" if self._saved else "Local draft · Not saved yet")

    def _notify(self, text, *, error=False):
        self.message.setText(text)
        self.message.setObjectName("error" if error else "notice")
        self.message.style().unpolish(self.message)
        self.message.style().polish(self.message)
        self.message.setVisible(True)

    def _shortcuts(self):
        self._shortcut_actions = []
        for shortcut, callback, title in [(QKeySequence.StandardKey.Save, self.save_preset, "Save preset"), (QKeySequence.StandardKey.Open, self.import_preset, "Import preset"), (QKeySequence.StandardKey.New, self.new_draft, "New draft")]:
            action = QAction(title, self)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(callback)
            self.addAction(action)
            self._shortcut_actions.append(action)

    def _update_capabilities(self):
        device = next((item for item in self.devices if item["id"] == self.selected_device_id), None)
        capabilities = device.get("capabilities", []) if device else []
        busy = self._device_or_dialog_busy()
        self.firmware_button.setEnabled(not busy)
        self.restore_button.setEnabled(not busy)
        self.automatic_profiles_button.setEnabled(not busy)
        self.host_integrations_button.setEnabled(not busy)
        self.background_button.setEnabled(not busy)
        self.media_player_refresh_button.setEnabled(
            self._media_player_job is None
            and callable(getattr(self.service, "media_players", None)))
        self._update_lcd_moves()
        self.dpi_calibration_button.setEnabled(not busy and self._calibration_sensor("dpi") is not None)
        self.angle_calibration_button.setEnabled(not busy and self._calibration_sensor("angle") is not None)
        self.dpi_calibration_button.setToolTip("Read the active profile and keep its current DPI stage selected to use the target exercise.")
        self.angle_calibration_button.setToolTip("Read the active profile, then follow the angle alignment exercise.")
        matching_read = bool(self.snapshot and self.snapshot.get("device_id") == self.selected_device_id and self.snapshot.get("profile_slot") == self.draft.profile_slot)
        self.dcu_calibration_button.setEnabled(not busy and matching_read and "calibrate_lift_off" in capabilities)
        self.host_lcd_checkbox.setEnabled(self.host_lcd_checkbox.isChecked() or (not busy and self._host_lcd_ready() is not None))
        self.countdown_checkbox.setEnabled(
            self.countdown_checkbox.isChecked()
            or (not busy and self._countdown_ready() is not None))
        can_read = "read_settings" in capabilities or "read_sensor" in capabilities
        self.read_button.setEnabled(not busy and can_read)
        self.read_button.setToolTip("Load supported settings from the selected mouse profile into this local draft.")
        section = PAGE_SECTIONS.get(self.navigation.currentRow())
        can_apply = not busy and section is not None and f"apply_{section}" in capabilities and matching_read
        if section in ("buttons", "display") and self._macro_merge_warning:
            can_apply = False
        self.apply_button.setText(f"Apply {SECTION_LABELS[section]}" if section else "Apply to mouse")
        self.apply_button.setEnabled(can_apply)
        self.apply_button.setToolTip(f"Apply this page's supported {SECTION_LABELS[section]} to the mouse." if can_apply else "Choose a settings page and read this mouse profile before applying.")
        self.device_selector.setEnabled(not busy)
        can_read_status = "read_status" in capabilities and callable(getattr(self.service, "read_status", None))
        self.status_read_button.setEnabled(not busy and can_read_status)
        self.battery_tray.set_read_enabled(not busy and can_read_status)
        for page, label_widget in self._capability_labels.items():
            key = "sensor" if page == "sensitivity" else page
            if key == "macros" and "apply_buttons" in capabilities:
                message = "Assign a macro on Buttons, then Apply buttons to upload keyboard and left/right/middle/back/forward mouse events. Once, Repeat, While held and Toggle playback are supported."
            elif f"apply_{key}" in capabilities:
                message = "Read this mouse profile, edit your settings, then apply this page to the mouse."
                if key == "display":
                    message = "Read and apply LCD pages, brightness, timeout and haptic feedback. Set up LCD replaces the Download Swarm II prompt."
                elif key == "buttons":
                    message = "Apply buttons uploads assigned keyboard and left/right/middle/back/forward mouse macros with Once, Repeat, While held or Toggle playback, then updates the assignments."
            else:
                message = "Local preset editing available. Applying this feature is unavailable for the selected connection."
            label_widget.setText(message)
        self.activate_display_button.setEnabled(not busy and ("setup_display" in capabilities or "activate_display" in capabilities))
        self.activate_profile_button.setEnabled(not busy and ("switch_profile" in capabilities or "activate_profile" in capabilities))
        rows = ["Local preset editing and import / export: available"]
        rows.append(f"Read mouse settings: {'available' if can_read else 'unavailable'}")
        for key, caption in SECTION_LABELS.items():
            rows.append(f"Apply {caption}: {'available after reading this profile' if f'apply_{key}' in capabilities else 'unavailable'}")
        rows.append(f"LCD background upload: {'available' if 'upload_background' in capabilities else 'unavailable'}")
        rows.append(f"Keyboard and left/right/middle/back/forward mouse macro upload through Apply buttons: {'available (Once / Repeat / While held / Toggle)' if 'apply_buttons' in capabilities else 'unavailable'}")
        rows.append(f"Live CPU/GPU/temperature/RAM LCD widgets: {'available' if 'host_lcd' in capabilities else 'unavailable'}")
        rows.append(f"Host-controlled LCD countdown timers: {'available' if 'countdown' in capabilities else 'unavailable'}")
        automatic_available = ("read_active_profile" in capabilities
                               and "switch_profile" in capabilities)
        rows.append(f"Foreground application profile switching: {'available' if automatic_available else 'unavailable'}")
        self.capability_summary.setText("\n\n".join(rows))

    def _run_job(self, operation, completed, status, *, telemetry=False, failed=None):
        if self._device_or_dialog_busy():
            return False
        if not telemetry:
            self.footer_status.setText(status)
        self.refresh_button.setEnabled(False)
        self.read_button.setEnabled(False)
        self.apply_button.setEnabled(False)
        self.device_selector.setEnabled(False)
        if not telemetry:
            self.stack.setEnabled(False)
            self.save_button.setEnabled(False)
            for action in self._shortcut_actions:
                action.setEnabled(False)
        self._job = DeviceJob(operation, self)
        self._job.completed.connect(completed)
        self._job.failed.connect(failed or self._job_failed)
        self._job.finished.connect(self._job_finished)
        self._update_capabilities()
        self._job.start()
        return True

    def _job_finished(self):
        job = self._job
        self._job = None
        if job:
            job.deleteLater()
        self.refresh_button.setEnabled(True)
        self.stack.setEnabled(True)
        self.save_button.setEnabled(True)
        for action in self._shortcut_actions:
            action.setEnabled(True)
        self._update_draft_status()
        self._update_capabilities()
        pending = self._automatic_pending_switch
        self._automatic_pending_switch = None
        if pending is not None:
            self._start_automatic_profile_switch(pending)
            return
        if self._status_refresh_due and self.battery_monitor_checkbox.isChecked():
            self._status_refresh_due = False
            QTimer.singleShot(0, self._poll_status)

    def _job_failed(self, message):
        self.snapshot = None
        self._set_device_status(None, note="Battery status unavailable; check the mouse connection.")
        self.connection_badge.setText("○  Refresh connection")
        self._notify(message, error=True)

    def refresh_device(self):
        if self.service is None:
            self.overview_connection.setText("No device service")
            self._notify("Device discovery is unavailable. Local presets can still be edited.")
            return
        self._run_job(self.service.discover, self._device_discovered, "Looking for your MC7…")

    def _device_discovered(self, snapshot):
        previous = self.selected_device_id
        self.devices = snapshot
        self.device_selector.blockSignals(True)
        self.device_selector.clear()
        for device in self.devices:
            self.device_selector.addItem(device["label"], device["id"])
        index = self.device_selector.findData(previous)
        self.device_selector.setCurrentIndex(max(0, index) if self.devices else -1)
        self.device_selector.blockSignals(False)
        self._select_device()
        if not self.devices:
            self._notify("No MC7 was found. Connect the mouse by USB, then refresh devices. You can keep editing local presets.")

    def _select_device(self, *_):
        selected = self.device_selector.currentData()
        changed = selected != self.selected_device_id
        if selected != self.selected_device_id:
            self.snapshot = None
            self._macro_merge_warning = ""
            self.overview_dpi.setText("Not read")
            self.active_profile_status.setText("Active mouse profile: not read")
            self.overview_battery.setText("Battery: not read")
            self.device_status.setText("Firmware, battery and charging status: not read")
        self.selected_device_id = selected
        self._invalidate_automatic_profile_context()
        if changed:
            self._set_device_status(None, note="Battery status has not been read for this connection.")
            self._status_refresh_due = self.battery_monitor_checkbox.isChecked()
        device = next((item for item in self.devices if item["id"] == selected), None)
        if device:
            self.connection_badge.setText("●  MC7 connected")
            self.overview_connection.setText("USB device connected")
            detail = device.get("detail", "")
            availability = "Read mouse to retrieve the supported settings." if any(key in device.get("capabilities", []) for key in ("read_settings", "read_sensor")) else "Settings access is unavailable for this connection."
            self.device_summary.setText("\n\n".join(part for part in (device["label"], detail, availability) if part))
        else:
            self.connection_badge.setText("○  MC7 not connected")
            self.overview_connection.setText("No mouse connected")
            self.device_summary.setText("No supported device is connected.")
        self._update_capabilities()
        if self._automatic_settings.enabled:
            QTimer.singleShot(0, self._poll_automatic_profile)
        if self._status_refresh_due and self._job is None:
            QTimer.singleShot(0, self._poll_status)

    def read_mouse(self):
        if not self.read_button.isEnabled():
            return
        device_id, slot = self.selected_device_id, self.draft.profile_slot
        draft = copy.deepcopy(self.draft)
        self._run_job(lambda: self.service.read(device_id, slot, draft), self._mouse_read, "Reading mouse settings…")

    def _merge_verified(self, snapshot, *, only_sections=None):
        received = snapshot["configuration"]
        if isinstance(received, dict):
            received = Configuration.from_dict(received)
        merged = False
        local_timer_pages = copy.deepcopy(self.draft.display.pages)
        local_timer_bindings = copy.deepcopy(self.draft.display.timer_bindings)
        local_host_action_pages = copy.deepcopy(self.draft.display.pages)
        local_host_action_bindings = copy.deepcopy(
            self.draft.display.host_action_bindings)
        local_host_action_icon_bindings = copy.deepcopy(
            self.draft.display.host_action_icon_bindings)
        received_host_action_bindings = copy.deepcopy(
            received.display.host_action_bindings)
        pages_merged = False
        fields = snapshot.get("verified_fields", [])
        if only_sections is None and "buttons" in fields and "macros" not in fields:
            self._macro_merge_warning = ""
        merge_macros = "macros" in fields and (only_sections is None or "macros" in only_sections or "buttons" in only_sections)
        skip_buttons = False
        skip_lcd_macro_state = False
        if merge_macros:
            macros = copy.deepcopy(self.draft.macros)
            positions = {macro.id: index for index, macro in enumerate(macros)}
            for macro in received.macros:
                if macro.id in positions:
                    macros[positions[macro.id]] = copy.deepcopy(macro)
                else:
                    positions[macro.id] = len(macros)
                    macros.append(copy.deepcopy(macro))
            if len(macros) > MAX_MACROS:
                self._macro_merge_warning = "The combined local and mouse macro libraries exceed 64 entries. Remove unused local macros, then read again to load mouse macros and button or LCD assignments."
                skip_buttons = True
                skip_lcd_macro_state = any(
                    widget == "macro"
                    for page in received.display.pages for widget in page)
            else:
                self.draft.macros = macros
                self._macro_merge_warning = ""
                merged = True
        for field in snapshot.get("verified_fields", []):
            # Only service-declared fields are read from hardware. Every other
            # page keeps its independent local draft values.
            parts = field.split(".")
            if only_sections is not None and parts[0] not in only_sections:
                continue
            if parts == ["buttons"]:
                if skip_buttons:
                    continue
                self.draft.buttons = copy.deepcopy(received.buttons)
                merged = True
            elif len(parts) == 2 and parts[0] in ("sensor", "lighting", "display", "power"):
                if (skip_lcd_macro_state and parts[0] == "display"
                        and parts[1] in ("pages", "key_bindings", "macro_bindings")):
                    continue
                # Countdown definitions, launch targets and application-icon
                # pixels are host-only and cannot be verified by an ordinary
                # settings read.
                if (parts[0] == "display"
                        and parts[1] in (
                            "timer_bindings", "host_action_bindings",
                            "host_action_icon_bindings")):
                    continue
                destination, source = getattr(self.draft, parts[0]), getattr(received, parts[0])
                if parts[1] in destination.__dataclass_fields__:
                    setattr(destination, parts[1], copy.deepcopy(getattr(source, parts[1])))
                    if parts[0] == "display" and parts[1] == "pages":
                        pages_merged = True
                    merged = True
        if pages_merged:
            timer_ids = {timer.id for timer in self.draft.countdown_timers}
            timer_bindings = [[None] * 4 for _ in self.draft.display.pages]
            for page_index, page in enumerate(self.draft.display.pages):
                for cell, widget in enumerate(page):
                    if (widget == "countdown"
                            and page_index < len(local_timer_pages)
                            and cell < len(local_timer_pages[page_index])
                            and local_timer_pages[page_index][cell] == "countdown"
                            and page_index < len(local_timer_bindings)
                            and cell < len(local_timer_bindings[page_index])
                            and local_timer_bindings[page_index][cell] in timer_ids):
                        timer_bindings[page_index][cell] = (
                            local_timer_bindings[page_index][cell])
            self.draft.display.timer_bindings = (
                timer_bindings
                if any(widget == "countdown" for page in self.draft.display.pages
                       for widget in page)
                else [])
            host_action_bindings = [[None] * 4 for _ in self.draft.display.pages]
            host_action_icon_bindings = [
                [None] * 4 for _ in self.draft.display.pages]
            for page_index, page in enumerate(self.draft.display.pages):
                for cell, widget in enumerate(page):
                    if (widget in LCD_HOST_ACTION_WIDGETS
                            and page_index < len(local_host_action_pages)
                            and cell < len(local_host_action_pages[page_index])
                            and local_host_action_pages[page_index][cell] == widget
                            and page_index < len(local_host_action_bindings)
                            and cell < len(local_host_action_bindings[page_index])
                            and page_index < len(received_host_action_bindings)
                            and cell < len(received_host_action_bindings[page_index])
                            and received_host_action_bindings[page_index][cell]
                            == local_host_action_bindings[page_index][cell]):
                        if widget == "open_application":
                            if (page_index >= len(
                                    local_host_action_icon_bindings)
                                    or cell >= len(
                                        local_host_action_icon_bindings[page_index])
                                    or local_host_action_icon_bindings[
                                        page_index][cell] is None):
                                continue
                            host_action_icon_bindings[page_index][cell] = (
                                local_host_action_icon_bindings[
                                    page_index][cell])
                        host_action_bindings[page_index][cell] = (
                            local_host_action_bindings[page_index][cell])
            self.draft.display.host_action_bindings = (
                host_action_bindings
                if any(widget in LCD_HOST_ACTION_WIDGETS
                       for page in self.draft.display.pages for widget in page)
                else [])
            self.draft.display.host_action_icon_bindings = (
                host_action_icon_bindings
                if any(
                    widget == "open_application"
                    and host_action_bindings[page_index][cell] is not None
                    for page_index, page in enumerate(
                        self.draft.display.pages)
                    for cell, widget in enumerate(page))
                else [])
        self.snapshot = snapshot
        self.dirty = self.dirty or merged
        self._load_draft()
        summary = snapshot.get("summary", {})
        dpi = summary.get("dpi")
        self.overview_dpi.setText(f"{dpi:,} DPI" if isinstance(dpi, int) else "Read complete")
        read_at = summary.get("read_at", "Unknown")
        active_profile = summary.get("active_profile")
        self.active_profile_status.setText(f"Active mouse profile: {active_profile} · Last read {read_at}" if type(active_profile) is int else "Active mouse profile: not reported by this read")
        self._set_device_status(summary.get("status"), note=f"Battery status last read: {read_at}")
        self.overview_read_status.setText(f"Profile {snapshot.get('profile_slot', self.draft.profile_slot)} · Last read {read_at}")
        device = next((item for item in self.devices if item["id"] == self.selected_device_id), {})
        detail = device.get("detail", "")
        self.device_summary.setText(f"Profile {snapshot.get('profile_slot', self.draft.profile_slot)}\nSensitivity: {dpi if dpi is not None else 'unknown'} DPI\nStage: {summary.get('stage', 'unknown')}\nLast read: {read_at}\nConnection: {summary.get('transport', 'USB')}" + (f"\n\n{detail}" if detail else ""))

    def _mouse_read(self, snapshot):
        self._merge_verified(snapshot)
        if self._macro_merge_warning:
            self._notify(self._macro_merge_warning, error=True)
            return
        errors = snapshot.get("errors", {})
        if errors:
            names = {"primary": "standard buttons", "easy_shift": "Easy-Shift buttons", "lcd": "LCD pages", "eco": "ECO mode"}
            missing = ", ".join(dict.fromkeys("mouse macros" if name.startswith("macro") else names.get(name, title_case(name)) for name in errors))
            self._notify(f"Mouse settings loaded. Could not read: {missing}. Those controls retain their local draft values; read again before applying them.", error=True)
        else:
            self._notify("Supported mouse settings loaded into the local draft. Unrelated local macros and preset settings are preserved.")

    def apply_to_mouse(self):
        if not self.apply_button.isEnabled():
            return
        try:
            configuration = copy.deepcopy(self.draft)
            configuration.validate()
        except ValueError as error:
            self._notify(f"Correct the draft before applying settings: {error}", error=True)
            return
        device_id = self.selected_device_id
        baseline = copy.deepcopy(self.snapshot.get("baseline"))
        section = PAGE_SECTIONS.get(self.navigation.currentRow())
        if section is None:
            return
        if section == "sensor":
            operation = lambda: self.service.apply(device_id, configuration, baseline=baseline)
        else:
            operation = lambda: self.service.apply_section(device_id, configuration, section, baseline=baseline)
        self._run_job(operation, lambda snapshot: self._mouse_applied(snapshot, section), f"Applying {SECTION_LABELS[section]} and reading it back…")

    def _mouse_applied(self, snapshot, section="sensor"):
        self._merge_verified(snapshot, only_sections=(section,))
        if self._macro_merge_warning and section == "buttons":
            self._notify("Button changes were sent and read back. " + self._macro_merge_warning, error=True)
            return
        adjustments = snapshot.get("macro_timing_adjustments") or snapshot.get("summary", {}).get("macro_timing_adjustments")
        timing = " Some macro delays were adjusted to the mouse's timing resolution." if adjustments else ""
        self._notify(f"{SECTION_LABELS[section].capitalize()} applied to the mouse and read back successfully. Your edits on other pages are preserved." + timing)

    def setup_display(self):
        if not self.activate_display_button.isEnabled():
            return
        device_id, slot = self.selected_device_id, self.draft.profile_slot
        self._run_job(lambda: self.service.setup_display(device_id, slot), self._display_set_up, "Setting up LCD pages and reading them back…")

    def _display_set_up(self, snapshot):
        # Setup changes only the LCD pages. Keep pending brightness, haptic,
        # timeout and other page edits intact while accepting the new baseline.
        layout_snapshot = dict(snapshot, verified_fields=[field for field in snapshot.get("verified_fields", []) if field == "display.pages"])
        self._merge_verified(layout_snapshot, only_sections=("display",))
        self.snapshot = snapshot
        summary = snapshot.get("summary", {})
        if summary.get("lcd_setup_verified"):
            if summary.get("changed"):
                self._notify("LCD layout updated and read back successfully. Check the LCD on the mouse to see the new controls.")
            else:
                self._notify("LCD layout is already set up: no Download Swarm II prompt is stored in this profile.")
        else:
            self._notify("LCD setup could not be verified. Read the mouse again before applying changes.", error=True)

    def activate_profile(self):
        if not self.activate_profile_button.isEnabled():
            return
        device_id, slot = self.selected_device_id, self.draft.profile_slot
        draft = copy.deepcopy(self.draft)
        self._run_job(lambda: self.service.switch_profile(device_id, slot, draft), self._profile_activated, f"Activating mouse profile {slot}…")

    def _profile_activated(self, snapshot):
        self.draft.profile_slot = snapshot["profile_slot"]
        self._merge_verified(snapshot)
        self._notify(f"Mouse profile {snapshot['profile_slot']} activated and its supported settings read back.")

    def closeEvent(self, event: QCloseEvent):
        if (self._dcu_dialog is not None or self._firmware_dialog is not None
                or self._restore_dialog is not None
                or self._automatic_profile_dialog is not None
                or self._host_integrations_dialog is not None):
            event.ignore()
            return
        self._sync_tray_options()
        if (not self._tray_quit_requested and not self._countdown_close_pending
                and self.battery_tray.enabled
                and self.keep_running_checkbox.isChecked()):
            self._hidden_to_tray = True
            self.hide()
            event.ignore()
            return
        if self._job is not None:
            self._notify("A mouse operation is finishing. Close the window after it completes.")
            event.ignore()
            return
        if self._countdown_process is not None:
            if not self._countdown_close_pending and not self._can_replace_draft():
                event.ignore()
                return
            self._countdown_close_pending = True
            self._countdown_quit_pending = (
                self._countdown_quit_pending or self._tray_quit_requested)
            self.centralWidget().setEnabled(False)
            self._request_countdown_stop("MC7 Studio is closing.")
            event.ignore()
            return
        close_approved = (self._countdown_close_pending
                          or self._can_replace_draft())
        if close_approved:
            quit_pending = self._countdown_quit_pending
            self._automatic_revision += 1
            scan_job = self._automatic_scan_job
            if scan_job is not None and scan_job.isRunning() and not scan_job.wait(2000):
                self._notify("The foreground application check is finishing. Close the window again when it completes.")
                if self._automatic_settings.enabled:
                    self.automatic_profile_timer.start()
                if self._countdown_close_pending:
                    QTimer.singleShot(500, self.close)
                event.ignore()
                return
            self._automatic_scan_job = None
            gpu_job = self._gpu_source_job
            if gpu_job is not None and gpu_job.isRunning() and not gpu_job.wait(2000):
                self._notify(
                    "GPU source discovery is finishing. Close the window again when it completes.")
                event.ignore()
                return
            self._gpu_source_job = None
            media_job = self._media_player_job
            if (media_job is not None and media_job.isRunning()
                    and not media_job.wait(2000)):
                self._notify(
                    "Media player discovery is finishing. Close the window again when it completes.")
                event.ignore()
                return
            self._media_player_job = None
            # Keep active session timers running if a bounded worker wait above
            # leaves the window open. Stop them only once close can complete.
            self.status_timer.stop()
            self.host_lcd_timer.stop()
            self.automatic_profile_timer.stop()
            self._countdown_close_pending = False
            self._countdown_quit_pending = False
            self.battery_tray.set_enabled(False)
            QApplication.instance().setQuitOnLastWindowClosed(self._initial_quit_policy)
            event.accept()
            if quit_pending:
                QTimer.singleShot(0, QApplication.instance().quit)
        else:
            event.ignore()


def run(argv=None) -> int:
    app = QApplication.instance() or QApplication(sys.argv if argv is None else argv)
    app.setApplicationName("MC7 Studio")
    app.setDesktopFileName("mc7-studio")
    app.setOrganizationName("swarm2-mc7")
    app.setWindowIcon(QIcon.fromTheme("mc7-studio"))
    app.setStyle("Fusion")
    from ..service import DeviceService
    window = MainWindow(service=DeviceService())
    window.show()
    return app.exec()
