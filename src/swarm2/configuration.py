"""Portable local MC7 drafts, with bounded JSON import and atomic persistence.

These are editor defaults, not settings read from the mouse. This module never
opens a device, starts a program, registers shortcuts, or executes macro events.
Accepting a local value does not establish that the device protocol supports it.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from .file_io import sync_directory
from pathlib import Path
import re
import stat
import sys
import tempfile
import uuid
import zlib
from typing import Any


SCHEMA = "swarm2.mc7.local-preset"
SCHEMA_VERSION = 1
MAX_PRESET_BYTES = 1024 * 1024
MAX_MACROS = 64
MAX_MACRO_EVENTS = 1000
MAX_COUNTDOWN_TIMERS = 64
MAX_PROFILE_IMAGE_BYTES = 256 * 1024
MAX_PROFILE_IMAGE_DIMENSION = 512
PROFILE_IMAGE_PREFIX = "data:image/png;base64,"
HOST_ACTION_ICON_WIDTH = 62
HOST_ACTION_ICON_HEIGHT = 64
HOST_ACTION_ICON_RGBA_BYTES = (
    HOST_ACTION_ICON_WIDTH * HOST_ACTION_ICON_HEIGHT * 4)
HOST_ACTION_ICON_PREFIX = "data:application/x-swarm2-rgba;base64,"
DEFAULT_PROFILE_COLOR = "#7A679B"
DPI_MIN, DPI_MAX, DPI_STEP = 50, 30000, 50
CUSTOM_PRECISION_PREFIX = "precision_custom_"
POLLING_RATES = (125, 250, 500, 1000, 2000, 4000, 8000)
LIFT_OFF_DISTANCES = ("very_low", "low", "custom")
# The shared vendor lighting editor contains this vocabulary. MC7 support for
# each effect and its wire value still requires verification.
LIGHTING_EFFECTS = ("off", "static", "blink", "breathing", "heartbeat", "photon", "snake", "aimo", "wave", "custom", "fade", "ripple", "battery")
DISPLAY_WIDGETS = ("dpi", "polling_rate", "led_brightness", "cpu", "cpu_temperature", "gpu", "gpu_temperature", "ram", "microphone_mute", "media", "countdown", "hotkeys", "macro")
HAPTIC_INTENSITIES = ("off", "low", "medium", "high")
MACRO_PLAYBACK_MODES = ("once", "repeat", "while_held", "toggle")
MACRO_EVENT_KINDS = ("key_down", "key_up", "mouse_down", "mouse_up", "delay")
ACTION_VALUES = {
    "mouse": ("left", "right", "middle", "back", "forward", "scroll_up", "scroll_down", "tilt_left", "tilt_right", "double_click"),
    "dpi": ("cycle", "up", "down", "precision", "precision_stage_1", "precision_stage_2", "precision_stage_3", "precision_stage_4", "precision_stage_5"),
    "profile": ("cycle", "next", "previous", "1", "2", "3", "4", "5"),
    "media": ("play_pause", "next", "previous", "mute", "volume_up", "volume_down", "stop"),
    "launch": ("browser", "calculator"),
    "easy_wheel": ("dpi", "volume", "alt_tab", "desktop"),
    "easy_shift": ("hold", "toggle"),
    "keyboard": (),
    "macro": (),
    "device": (),  # Opaque action read from hardware; may only be preserved.
    "disabled": ("",),
}
ACTION_KINDS = tuple(ACTION_VALUES)
_MOUSE_EVENT_BUTTONS = ("left", "right", "middle", "back", "forward")
_KEY_NAMES = {
    "ctrl", "control", "alt", "shift", "meta", "super", "cmd", "command", "option",
    "enter", "return", "escape", "esc", "space", "tab", "backspace", "delete", "insert",
    "home", "end", "pageup", "pagedown", "up", "down", "left", "right", "capslock",
    "numlock", "scrolllock", "printscreen", "pause", "minus", "equal", "plus", "comma",
    "period", "slash", "backslash", "semicolon", "quote", "bracketleft", "bracketright",
    "grave", "menu",
}
_MODIFIERS = {"ctrl", "control", "alt", "shift", "meta", "super", "cmd", "command", "option"}


class ConfigurationError(ValueError):
    """A local draft is malformed or a value needs correction in the editor."""


@dataclass
class DPIStage:
    value: int = 800
    enabled: bool = True
    color: str = "#00D7A7"


def _default_stages() -> list[DPIStage]:
    return [DPIStage(value, True, color) for value, color in zip(
        (400, 800, 1200, 1600, 3200),
        ("#FF5964", "#00D7A7", "#57A5FF", "#C084FC", "#FFCE54"),
    )]


@dataclass
class SensorSettings:
    stages: list[DPIStage] = field(default_factory=_default_stages)
    current_stage: int = 1
    polling_rate: int = 1000
    angle_snapping: bool = False
    lift_off_distance: str = "low"
    debounce_ms: int = 10
    motion_sync: bool = False
    angle_tuning: int = 0
    angle_tuning_enabled: bool = False
    dpi_indicator_enabled: bool = True


@dataclass
class Action:
    kind: str = "disabled"
    value: str = ""


def parse_custom_precision_dpi(value: Any) -> int | None:
    """Return the DPI encoded by a canonical custom Easy-Aim action value."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(rf"{re.escape(CUSTOM_PRECISION_PREFIX)}([1-9][0-9]*)", value)
    if match is None:
        return None
    digits = match.group(1)
    if len(digits) > len(str(DPI_MAX)):
        return None
    dpi = int(digits)
    if DPI_MIN <= dpi <= DPI_MAX and dpi % DPI_STEP == 0:
        return dpi
    return None


@dataclass
class ButtonBinding:
    button_id: int = 1
    primary: Action = field(default_factory=Action)
    easy_shift: Action = field(default_factory=Action)


def _default_buttons() -> list[ButtonBinding]:
    actions = (
        ("mouse", "left"), ("mouse", "right"), ("mouse", "middle"),
        ("mouse", "scroll_up"), ("mouse", "scroll_down"), ("mouse", "forward"), ("mouse", "back"),
        ("dpi", "cycle"), ("easy_shift", "hold"), ("mouse", "tilt_right"), ("mouse", "tilt_left"),
    )
    return [ButtonBinding(index, Action(*action)) for index, action in enumerate(actions, 1)]


@dataclass
class LightingSettings:
    effect: str = "static"
    color: str = "#00D7A7"
    brightness: int = 100
    speed: int = 50


@dataclass
class DisplaySettings:
    brightness: int = 100
    timeout_seconds: int = 60
    widgets: list[str] = field(default_factory=lambda: ["dpi", "polling_rate", "led_brightness"])
    haptic_intensity: str = "medium"
    timeout_value: int = 10
    pages: list[list[str | None]] = field(default_factory=list)
    # Rows align with pages and columns with slots. An empty list is the
    # version-1 representation for layouts that contain no key widgets.
    key_bindings: list[list[str | None]] = field(default_factory=list)
    # Local macro IDs aligned with LCD pages and slots. Active images use the
    # coordinate-bound LCD macro namespace and command-0x29 trigger records.
    macro_bindings: list[list[str | None]] = field(default_factory=list)
    # Host-only countdown timer IDs aligned with LCD pages and slots. The
    # mouse stores only the 46/00 tile in its layout, so unresolved hardware
    # tiles are represented by null bindings.
    timer_bindings: list[list[str | None]] = field(default_factory=list)
    # Full application/website/file/folder targets remain on the host. The mouse stores
    # only the matching layout tile and its small command-0x29 trigger record.
    # Null keeps a raw hardware tile loadable until the user assigns a target.
    host_action_bindings: list[list[str | None]] = field(default_factory=list)
    # Canonical 62x64 RGBA previews aligned with host-action targets. Only a
    # resolved Open Application tile carries an icon; raw hardware tiles remain
    # loadable with both host-only matrices empty.
    host_action_icon_bindings: list[list[str | None]] = field(default_factory=list)
    # The original app keeps its General Media player choice on the host for
    # each profile. None asks the provider to choose only when unambiguous.
    preferred_media_player: str | None = None
    # None keeps the mouse's current background, including older presets.
    background_index: int | None = None


@dataclass
class PowerSettings:
    """Local power draft using the vendor's minute-count menu values."""

    standby_value: int = 5
    led_timeout_value: int = 1
    eco_mode: bool = False
    energy_saving: bool = False


@dataclass
class MacroEvent:
    kind: str = "delay"
    value: str = ""
    delay_ms: int = 100


@dataclass
class Macro:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = "New macro"
    events: list[MacroEvent] = field(default_factory=list)
    repeat: int = 1
    playback: str = "once"


@dataclass
class CountdownTimer:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = "New timer"
    duration_seconds: int = 1


@dataclass
class ProfileAppearance:
    """Portable library-only presentation; never written to the mouse."""

    color: str = DEFAULT_PROFILE_COLOR
    # A normalized, bounded PNG data URL keeps exported presets portable.
    image: str | None = None


@dataclass
class Configuration:
    name: str = "Local draft"
    profile_slot: int = 1
    sensor: SensorSettings = field(default_factory=SensorSettings)
    buttons: list[ButtonBinding] = field(default_factory=_default_buttons)
    lighting: LightingSettings = field(default_factory=LightingSettings)
    display: DisplaySettings = field(default_factory=DisplaySettings)
    power: PowerSettings = field(default_factory=PowerSettings)
    macros: list[Macro] = field(default_factory=list)
    countdown_timers: list[CountdownTimer] = field(default_factory=list)
    # Keep new host-only metadata last so older positional construction remains
    # compatible; production callers use keyword arguments.
    appearance: ProfileAppearance = field(default_factory=ProfileAppearance)

    def _raw_dict(self) -> dict[str, Any]:
        try:
            return {"schema": SCHEMA, "version": SCHEMA_VERSION, "source": "local_draft", **asdict(self)}
        except (TypeError, RecursionError) as exc:
            raise ConfigurationError("The draft contains an invalid nested value.") from exc

    def validate(self) -> None:
        """Validate a mutable editor draft before saving or exporting it."""
        self.from_dict(self._raw_dict())

    def to_dict(self) -> dict[str, Any]:
        data = self._raw_dict()
        self.from_dict(data)
        return data

    @classmethod
    def from_dict(cls, data: Any) -> Configuration:
        if isinstance(data, dict):
            # Host-only additions retain version-1 compatibility. Missing
            # means an older preset, while unknown fields remain rejected.
            data = {
                "appearance": {"color": DEFAULT_PROFILE_COLOR, "image": None},
                "countdown_timers": [],
                **data,
            }
        data = _object(data, "Preset", (
            "schema", "version", "source", "name", "profile_slot", "appearance",
            "sensor", "buttons", "lighting", "display", "power", "macros",
            "countdown_timers",
        ))
        if data["schema"] != SCHEMA:
            raise ConfigurationError("This file is not a Swarm2 MC7 local preset.")
        if type(data["version"]) is not int or data["version"] != SCHEMA_VERSION:
            raise ConfigurationError(f"Unsupported preset version; this application supports version {SCHEMA_VERSION}.")
        if data["source"] != "local_draft":
            raise ConfigurationError("A preset must be marked as a local draft, not verified device settings.")
        name = _text(data["name"], "Preset name", 80)
        profile_slot = _integer(data["profile_slot"], "Profile slot", 1, 5)
        appearance = _object(data["appearance"], "Profile appearance", ("color", "image"))
        sensor = _parse_sensor(data["sensor"])
        macros = [_parse_macro(value, index) for index, value in enumerate(
            _items(data["macros"], "Macros", MAX_MACROS), 1
        )]
        macro_ids = {macro.id for macro in macros}
        if len(macro_ids) != len(macros):
            raise ConfigurationError("Every macro must have a unique ID.")
        countdown_timers = [
            _parse_countdown_timer(value, index)
            for index, value in enumerate(
                _items(data["countdown_timers"], "Countdown timers", MAX_COUNTDOWN_TIMERS), 1)
        ]
        timer_ids = {timer.id for timer in countdown_timers}
        if len(timer_ids) != len(countdown_timers):
            raise ConfigurationError("Every countdown timer must have a unique ID.")
        buttons = []
        for index, value in enumerate(_items(data["buttons"], "Buttons", 11, exact=11), 1):
            value = _object(value, f"Button {index}", ("button_id", "primary", "easy_shift"))
            buttons.append(ButtonBinding(
                _integer(value["button_id"], f"Button {index} ID", 1, 11),
                _parse_action(value["primary"], f"Button {index} primary action", macro_ids),
                _parse_action(value["easy_shift"], f"Button {index} Easy-Shift action", macro_ids),
            ))
        if {button.button_id for button in buttons} != set(range(1, 12)):
            raise ConfigurationError("Buttons must contain each ID from 1 to 11 exactly once.")
        lighting = _object(data["lighting"], "Lighting", ("effect", "color", "brightness", "speed"))
        display_data = data["display"]
        if isinstance(display_data, dict):
            # Older version-1 drafts stored only a hypothetical seconds value.
            display_data = {"timeout_value": 10, "pages": [], "key_bindings": [],
                            "macro_bindings": [], "timer_bindings": [],
                            "host_action_bindings": [],
                            "host_action_icon_bindings": [],
                            "preferred_media_player": None,
                            "background_index": None, **display_data}
        display = _object(display_data, "Display", (
            "brightness", "timeout_seconds", "widgets", "haptic_intensity",
            "timeout_value", "pages", "key_bindings", "macro_bindings", "timer_bindings",
            "host_action_bindings", "host_action_icon_bindings",
            "preferred_media_player", "background_index",
        ))
        power = _object(data["power"], "Power", ("standby_value", "led_timeout_value", "eco_mode", "energy_saving"))
        widgets = [_choice(widget, "Display widget", DISPLAY_WIDGETS)
                   for widget in _items(display["widgets"], "Display widgets", 16)]
        if len(set(widgets)) != len(widgets):
            raise ConfigurationError("A display widget can appear only once in its layout.")
        pages = _parse_lcd_pages(display["pages"])
        key_bindings = _parse_lcd_key_bindings(display["key_bindings"], pages)
        macro_bindings = _parse_lcd_macro_bindings(display["macro_bindings"], pages, macro_ids)
        timer_bindings = _parse_lcd_timer_bindings(display["timer_bindings"], pages, timer_ids)
        host_action_bindings = _parse_lcd_host_action_bindings(
            display["host_action_bindings"], pages)
        host_action_icon_bindings = _parse_lcd_host_action_icon_bindings(
            display["host_action_icon_bindings"], pages, host_action_bindings)
        return cls(
            name=name, profile_slot=profile_slot,
            appearance=ProfileAppearance(
                color=_color(appearance["color"], "Profile color"),
                image=_profile_image(appearance["image"]),
            ),
            sensor=sensor, buttons=buttons, macros=macros,
            countdown_timers=countdown_timers,
            lighting=LightingSettings(
                _choice(lighting["effect"], "Lighting effect", LIGHTING_EFFECTS),
                _color(lighting["color"], "Lighting color"),
                _integer(lighting["brightness"], "Lighting brightness", 0, 100),
                _integer(lighting["speed"], "Lighting speed", 0, 100),
            ),
            display=DisplaySettings(
                brightness=_integer(display["brightness"], "Display brightness", 0, 100),
                timeout_seconds=_integer(display["timeout_seconds"], "Display timeout", 0, 3600),
                widgets=widgets,
                haptic_intensity=_choice(display["haptic_intensity"], "Haptic intensity", HAPTIC_INTENSITIES),
                timeout_value=_integer(display["timeout_value"], "Screen timeout setting", 0, 30),
                pages=pages,
                key_bindings=key_bindings,
                macro_bindings=macro_bindings,
                timer_bindings=timer_bindings,
                host_action_bindings=host_action_bindings,
                host_action_icon_bindings=host_action_icon_bindings,
                preferred_media_player=_parse_media_player_id(
                    display["preferred_media_player"]),
                background_index=(None if display["background_index"] is None else
                                  _integer(display["background_index"], "Background selection", 0, 255)),
            ),
            power=PowerSettings(
                _integer(power["standby_value"], "Standby delay value", 1, 30),
                _integer(power["led_timeout_value"], "LED timeout value", 0, 30),
                _boolean(power["eco_mode"], "ECO mode"),
                _boolean(power["energy_saving"], "Energy saving"),
            ),
        )


def _parse_media_player_id(value: Any) -> str | None:
    if value is None:
        return None
    from .media_player_ids import validate_media_player_id
    try:
        return validate_media_player_id(value)
    except ValueError as error:
        raise ConfigurationError(str(error) + ".") from error


def _parse_lcd_pages(value: Any) -> list:
    from .lcd_commands import LCD_WIDGETS
    pages = _items(value, "LCD pages", 3)
    result = []
    for page in pages:
        slots = _items(page, "LCD page slots", 4, exact=4)
        remaining = 0
        for key in slots:
            if remaining:
                if key is not None:
                    raise ConfigurationError("Wide LCD widgets occupy three slots.")
                remaining -= 1
            elif isinstance(key, str) and key in LCD_WIDGETS:
                remaining = LCD_WIDGETS[key].width - 1
            elif isinstance(key, str) and re.fullmatch(r"unknown_[0-9a-f]{2}_[0-9a-f]{2}", key):
                remaining = 2 if int(key[8:10], 16) in (1, 0x45, 0x65) else 0
            else:
                raise ConfigurationError("Unsupported LCD widget or misplaced continuation.")
        if remaining:
            raise ConfigurationError("A wide LCD widget extends beyond its page.")
        result.append(list(slots))
    return result


def _parse_lcd_key_bindings(value: Any, pages: list[list[str | None]]) -> list:
    from .lcd_commands import LCD_KEY_WIDGETS
    bindings = _items(value, "LCD key bindings", 3)
    if not bindings:
        if any(key in LCD_KEY_WIDGETS for page in pages for key in page):
            raise ConfigurationError("LCD key widgets require a key binding for every page slot.")
        return []
    if len(bindings) != len(pages):
        raise ConfigurationError("LCD key bindings must contain one row for every LCD page.")
    result = []
    for page_index, (page, row) in enumerate(zip(pages, bindings), 1):
        values = _items(row, f"LCD page {page_index} key bindings", 4, exact=4)
        parsed = []
        for slot, (widget, binding) in enumerate(zip(page, values), 1):
            label = f"LCD page {page_index} slot {slot} key binding"
            if widget not in LCD_KEY_WIDGETS:
                if binding is not None:
                    raise ConfigurationError(f"{label} must be null for a non-key widget.")
                parsed.append(None)
                continue
            if binding is None:
                parsed.append(None)
                continue
            if not isinstance(binding, str):
                raise ConfigurationError(f"{label} must contain a keyboard key or shortcut.")
            if re.fullmatch(r"device:[0-9a-f]{22}", binding):
                parsed.append(binding)
                continue
            binding = _text(binding, label, 128)
            if widget == "remap_key":
                _key(binding, label)
            else:
                _shortcut(binding, label)
            parsed.append(binding)
        result.append(parsed)
    return result


def _parse_lcd_macro_bindings(value: Any, pages: list[list[str | None]],
                              macro_ids: set[str]) -> list:
    from .lcd_commands import LCD_MACRO_WIDGETS
    bindings = _items(value, "LCD macro bindings", 3)
    if not bindings:
        # A hardware read can identify the 05/02 tile before its separate
        # selector record is understood. Keep that layout loadable without
        # inventing a local macro assignment.
        return []
    if len(bindings) != len(pages):
        raise ConfigurationError("LCD macro bindings must contain one row for every LCD page.")
    result = []
    for page_index, (page, row) in enumerate(zip(pages, bindings), 1):
        values = _items(row, f"LCD page {page_index} macro bindings", 4, exact=4)
        parsed = []
        for slot, (widget, binding) in enumerate(zip(page, values), 1):
            label = f"LCD page {page_index} slot {slot} macro binding"
            if widget not in LCD_MACRO_WIDGETS:
                if binding is not None:
                    raise ConfigurationError(f"{label} must be null for a non-macro widget.")
                parsed.append(None)
                continue
            if binding is not None and binding not in macro_ids:
                raise ConfigurationError(
                    f"{label} must refer to a macro that is included in this preset.")
            parsed.append(binding)
        result.append(parsed)
    return result


def _parse_lcd_timer_bindings(value: Any, pages: list[list[str | None]],
                              timer_ids: set[str]) -> list:
    bindings = _items(value, "LCD countdown timer bindings", 3)
    if not bindings:
        # Older presets and raw hardware reads contain no host-only TimerID.
        # Keep countdown tiles loadable as unresolved until the user assigns
        # one from the local timer library.
        return []
    if len(bindings) != len(pages):
        raise ConfigurationError(
            "LCD countdown timer bindings must contain one row for every LCD page.")
    result = []
    for page_index, (page, row) in enumerate(zip(pages, bindings), 1):
        values = _items(row, f"LCD page {page_index} countdown timer bindings", 4, exact=4)
        parsed = []
        for slot, (widget, binding) in enumerate(zip(page, values), 1):
            label = f"LCD page {page_index} slot {slot} countdown timer binding"
            if widget != "countdown":
                if binding is not None:
                    raise ConfigurationError(f"{label} must be null for a non-countdown widget.")
                parsed.append(None)
                continue
            if binding is not None and binding not in timer_ids:
                raise ConfigurationError(
                    f"{label} must refer to a countdown timer that is included in this preset.")
            parsed.append(binding)
        result.append(parsed)
    return result


def _parse_lcd_host_action_bindings(value: Any,
                                    pages: list[list[str | None]]) -> list:
    from .host_actions import validate_host_action_target
    from .lcd_commands import LCD_HOST_ACTION_WIDGETS

    bindings = _items(value, "LCD host-action bindings", 3)
    if not bindings:
        # Command 0x25 identifies the tile but no mouse read can recover the
        # full host target. Keep hardware-derived layouts explicitly unresolved.
        return []
    if len(bindings) != len(pages):
        raise ConfigurationError(
            "LCD host-action bindings must contain one row for every LCD page.")
    result = []
    for page_index, (page, row) in enumerate(zip(pages, bindings), 1):
        values = _items(row, f"LCD page {page_index} host-action bindings", 4, exact=4)
        parsed = []
        for slot, (widget, binding) in enumerate(zip(page, values), 1):
            label = f"LCD page {page_index} slot {slot} host-action binding"
            if widget not in LCD_HOST_ACTION_WIDGETS:
                if binding is not None:
                    raise ConfigurationError(
                        f"{label} must be null for a non-host-action widget.")
                parsed.append(None)
                continue
            if binding is None:
                parsed.append(None)
                continue
            try:
                parsed.append(validate_host_action_target(widget, binding))
            except ValueError as error:
                raise ConfigurationError(f"{label}: {error}") from error
        result.append(parsed)
    return result


def host_action_icon_rgba(value: Any) -> bytes:
    """Decode one canonical, fixed-size Open Application icon data URL."""
    encoded_bytes = ((HOST_ACTION_ICON_RGBA_BYTES + 2) // 3) * 4
    if (not isinstance(value, str)
            or len(value) != len(HOST_ACTION_ICON_PREFIX) + encoded_bytes
            or not value.startswith(HOST_ACTION_ICON_PREFIX)):
        raise ConfigurationError(
            "Open Application icon must be canonical 62 by 64 RGBA data.")
    payload = value[len(HOST_ACTION_ICON_PREFIX):]
    try:
        rgba = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ConfigurationError(
            "Open Application icon contains invalid base64 data.") from error
    if (len(rgba) != HOST_ACTION_ICON_RGBA_BYTES
            or base64.b64encode(rgba).decode("ascii") != payload):
        raise ConfigurationError(
            "Open Application icon must be canonical 62 by 64 RGBA data.")
    return rgba


def encode_host_action_icon_rgba(rgba: Any) -> str:
    """Encode one fixed-size RGBA icon for portable preset storage."""
    if not isinstance(rgba, (bytes, bytearray)):
        raise ConfigurationError(
            "Open Application icon must be canonical 62 by 64 RGBA data.")
    raw = bytes(rgba)
    if len(raw) != HOST_ACTION_ICON_RGBA_BYTES:
        raise ConfigurationError(
            "Open Application icon must be canonical 62 by 64 RGBA data.")
    return HOST_ACTION_ICON_PREFIX + base64.b64encode(raw).decode("ascii")


# Keep the noun-oriented spelling available to callers that treat this value
# as a data URL rather than an encoder result.
host_action_icon_data_url = encode_host_action_icon_rgba


def _parse_lcd_host_action_icon_bindings(
        value: Any, pages: list[list[str | None]],
        host_action_bindings: list[list[str | None]]) -> list:
    icons = _items(value, "LCD host-action icon bindings", 3)
    resolved_applications = {
        (page_index, slot)
        for page_index, page in enumerate(pages)
        for slot, widget in enumerate(page)
        if (widget == "open_application"
            and page_index < len(host_action_bindings)
            and slot < len(host_action_bindings[page_index])
            and host_action_bindings[page_index][slot] is not None)
    }
    if not icons:
        if resolved_applications:
            raise ConfigurationError(
                "Every resolved Open Application tile requires a custom icon.")
        return []
    if len(icons) != len(pages):
        raise ConfigurationError(
            "LCD host-action icon bindings must contain one row for every LCD page.")
    result = []
    for page_index, row in enumerate(icons):
        values = _items(
            row, f"LCD page {page_index + 1} host-action icon bindings", 4,
            exact=4)
        parsed = []
        for slot, icon in enumerate(values):
            label = (
                f"LCD page {page_index + 1} slot {slot + 1} host-action icon binding")
            required = (page_index, slot) in resolved_applications
            if not required:
                if icon is not None:
                    raise ConfigurationError(
                        f"{label} must be null unless it belongs to a resolved Open Application tile.")
                parsed.append(None)
                continue
            if icon is None:
                raise ConfigurationError(
                    f"{label} is required for a resolved Open Application tile.")
            try:
                host_action_icon_rgba(icon)
            except ConfigurationError as error:
                raise ConfigurationError(f"{label}: {error}") from error
            parsed.append(icon)
        result.append(parsed)
    return result


def _object(value: Any, label: str, keys: tuple[str, ...]) -> dict:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{label} must be a JSON object.")
    if set(value) != set(keys):
        raise ConfigurationError(f"{label} has missing or unknown fields; expected: {', '.join(keys)}.")
    return value


def _text(value: Any, label: str, maximum: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise ConfigurationError(f"{label} must be {'a nonempty string of ' if not empty else 'a string of '}at most {maximum} characters.")
    if any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ConfigurationError(f"{label} must not contain control characters or invalid Unicode.")
    return value


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ConfigurationError(f"{label} must be an integer from {minimum} to {maximum}.")
    return value


def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise ConfigurationError(f"{label} must be true or false.")
    return value


def _choice(value: Any, label: str, choices: tuple) -> Any:
    if not isinstance(value, (str, int)) or isinstance(value, bool) or value not in choices:
        raise ConfigurationError(f"{label} must be one of: {', '.join(str(choice) for choice in choices)}.")
    return value


def _color(value: Any, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"#[0-9A-Fa-f]{6}", value) is None:
        raise ConfigurationError(f"{label} must be a color such as #00D7A7.")
    return value


def _png_row_sizes(width: int, height: int, bits_per_pixel: int,
                   interlace: int) -> list[int]:
    """Return filtered scanline sizes for a bounded PNG, including Adam7."""

    if interlace == 0:
        return [1 + (width * bits_per_pixel + 7) // 8] * height
    passes = (
        (0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
        (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2),
    )
    rows = []
    for start_x, start_y, step_x, step_y in passes:
        pass_width = 0 if width <= start_x else (width - start_x + step_x - 1) // step_x
        pass_height = 0 if height <= start_y else (height - start_y + step_y - 1) // step_y
        if pass_width:
            rows.extend(
                [1 + (pass_width * bits_per_pixel + 7) // 8] * pass_height
            )
    return rows


def _validate_png_pixels(compressed: bytes, width: int, height: int,
                         header: bytes) -> None:
    bit_depth, color_type, compression, filtering, interlace = header[8:13]
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
    valid_depths = {
        0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8),
        4: (8, 16), 6: (8, 16),
    }
    if (channels is None or bit_depth not in valid_depths[color_type]
            or compression != 0 or filtering != 0 or interlace not in (0, 1)):
        raise ConfigurationError("Profile image has unsupported PNG pixel settings.")
    row_sizes = _png_row_sizes(width, height, channels * bit_depth, interlace)
    expected = sum(row_sizes)
    try:
        decoder = zlib.decompressobj()
        pixels = decoder.decompress(compressed, expected + 1)
    except zlib.error as error:
        raise ConfigurationError("Profile image contains invalid compressed PNG data.") from error
    if (len(pixels) != expected or not decoder.eof or decoder.unconsumed_tail
            or decoder.unused_data):
        raise ConfigurationError("Profile image contains invalid compressed PNG data.")
    offset = 0
    for row_size in row_sizes:
        if pixels[offset] > 4:
            raise ConfigurationError("Profile image contains an invalid PNG row filter.")
        offset += row_size


def profile_image_bytes(value: Any) -> bytes:
    """Decode and validate a portable profile thumbnail without image I/O."""

    maximum_text = len(PROFILE_IMAGE_PREFIX) + ((MAX_PROFILE_IMAGE_BYTES + 2) // 3) * 4
    if not isinstance(value, str) or len(value) > maximum_text or not value.startswith(PROFILE_IMAGE_PREFIX):
        raise ConfigurationError("Profile image must be a bounded PNG embedded in the preset.")
    payload = value[len(PROFILE_IMAGE_PREFIX):]
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ConfigurationError("Profile image contains invalid base64 data.") from error
    if (not raw or len(raw) > MAX_PROFILE_IMAGE_BYTES
            or base64.b64encode(raw).decode("ascii") != payload):
        raise ConfigurationError("Profile image must be a canonical PNG no larger than 256 KiB.")
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise ConfigurationError("Profile image must contain PNG data.")

    offset = 8
    first = True
    saw_idat = False
    idat_closed = False
    saw_plte = False
    idat = bytearray()
    header = b""
    width = height = 0
    while offset + 12 <= len(raw):
        length = int.from_bytes(raw[offset:offset + 4], "big")
        end = offset + 12 + length
        if end > len(raw):
            break
        kind = raw[offset + 4:offset + 8]
        content = raw[offset + 8:offset + 8 + length]
        stored_crc = int.from_bytes(raw[offset + 8 + length:end], "big")
        if (any(not (65 <= byte <= 90 or 97 <= byte <= 122) for byte in kind)
                or kind[2] & 0x20):
            raise ConfigurationError("Profile image contains an invalid PNG chunk type.")
        if not kind[0] & 0x20 and kind not in (b"IHDR", b"PLTE", b"IDAT", b"IEND"):
            raise ConfigurationError("Profile image contains an unsupported critical PNG chunk.")
        if zlib.crc32(content, zlib.crc32(kind)) & 0xFFFFFFFF != stored_crc:
            raise ConfigurationError("Profile image contains a damaged PNG chunk.")
        if first:
            if kind != b"IHDR" or length != 13:
                raise ConfigurationError("Profile image has no valid PNG header.")
            width = int.from_bytes(content[:4], "big")
            height = int.from_bytes(content[4:8], "big")
            header = content
            first = False
        elif kind == b"IHDR":
            raise ConfigurationError("Profile image contains more than one PNG header.")
        if saw_idat and kind not in (b"IDAT", b"IEND"):
            idat_closed = True
        if kind == b"PLTE":
            color_type = header[9]
            entries = length // 3
            if (saw_plte or saw_idat or color_type in (0, 4) or not length
                    or length % 3 or entries > 256
                    or (color_type == 3 and entries > 1 << header[8])):
                raise ConfigurationError("Profile image contains an invalid PNG palette.")
            saw_plte = True
        if kind == b"IDAT":
            if idat_closed or (header[9] == 3 and not saw_plte):
                raise ConfigurationError("Profile image has invalid PNG pixel-data ordering.")
            saw_idat = True
            idat.extend(content)
        if kind == b"IEND":
            if length or end != len(raw) or not saw_idat:
                raise ConfigurationError("Profile image has an invalid PNG ending.")
            if not (1 <= width <= MAX_PROFILE_IMAGE_DIMENSION
                    and 1 <= height <= MAX_PROFILE_IMAGE_DIMENSION):
                raise ConfigurationError("Profile image dimensions must be from 1 to 512 pixels.")
            _validate_png_pixels(bytes(idat), width, height, header)
            return raw
        offset = end
    raise ConfigurationError("Profile image contains incomplete PNG data.")


def _profile_image(value: Any) -> str | None:
    if value is None:
        return None
    profile_image_bytes(value)
    return value


def _items(value: Any, label: str, maximum: int, *, exact: int | None = None) -> list:
    if not isinstance(value, list) or len(value) > maximum or (exact is not None and len(value) != exact):
        amount = f"exactly {exact}" if exact is not None else f"at most {maximum}"
        raise ConfigurationError(f"{label} must be a list containing {amount} items.")
    return value


def _parse_sensor(value: Any) -> SensorSettings:
    value = _object(value, "Sensor", (
        "stages", "current_stage", "polling_rate", "angle_snapping", "lift_off_distance", "debounce_ms", "motion_sync",
        "angle_tuning", "angle_tuning_enabled", "dpi_indicator_enabled",
    ))
    stages = []
    for index, stage in enumerate(_items(value["stages"], "DPI stages", 5, exact=5), 1):
        stage = _object(stage, f"DPI stage {index}", ("value", "enabled", "color"))
        dpi = _integer(stage["value"], f"DPI stage {index}", DPI_MIN, DPI_MAX)
        if dpi % DPI_STEP:
            raise ConfigurationError(f"DPI stage {index} must use increments of {DPI_STEP} DPI.")
        stages.append(DPIStage(dpi, _boolean(stage["enabled"], f"DPI stage {index} enabled"),
                               _color(stage["color"], f"DPI stage {index} color")))
    current_stage = _integer(value["current_stage"], "Current DPI stage index", 0, 4)
    if not stages[current_stage].enabled:
        raise ConfigurationError("The current DPI stage must be enabled.")
    return SensorSettings(
        stages=stages, current_stage=current_stage,
        polling_rate=_choice(value["polling_rate"], "Polling rate", POLLING_RATES),
        angle_snapping=_boolean(value["angle_snapping"], "Angle snapping"),
        lift_off_distance=_choice(value["lift_off_distance"], "Lift-off distance", LIFT_OFF_DISTANCES),
        debounce_ms=_integer(value["debounce_ms"], "Debounce", 0, 10),
        motion_sync=_boolean(value["motion_sync"], "Motion Sync"),
        angle_tuning=_integer(value["angle_tuning"], "Angle tuning", -30, 30),
        angle_tuning_enabled=_boolean(value["angle_tuning_enabled"], "Angle tuning enabled"),
        dpi_indicator_enabled=_boolean(value["dpi_indicator_enabled"], "DPI indicator enabled"),
    )


def _key(value: Any, label: str) -> str:
    value = _text(value, label, 24)
    if value.lower() not in _KEY_NAMES and re.fullmatch(r"[A-Za-z0-9]|[Ff](?:[1-9]|1[0-9]|2[0-4])|[Nn]um[0-9]", value) is None:
        raise ConfigurationError(f"{label} must be a key name such as A, Enter, Ctrl, or F5.")
    return value


def _shortcut(value: str, label: str) -> str:
    tokens = value.split("+")
    if not 1 <= len(tokens) <= 5:
        raise ConfigurationError(f"{label} must be a shortcut such as Ctrl+Shift+S.")
    for token in tokens:
        _key(token, label)
    if (any(token.lower() not in _MODIFIERS for token in tokens[:-1])
            or tokens[-1].lower() in _MODIFIERS
            or len(set(t.lower() for t in tokens)) != len(tokens)):
        raise ConfigurationError(f"{label} must contain modifiers followed by one key, such as Ctrl+S.")
    return value


def _parse_action(value: Any, label: str, macro_ids: set[str]) -> Action:
    value = _object(value, label, ("kind", "value"))
    kind = _choice(value["kind"], f"{label} type", ACTION_KINDS)
    action_value = _text(value["value"], label, 128, empty=kind == "disabled")
    if kind == "keyboard":
        _shortcut(action_value, label)
    elif kind == "macro":
        if action_value not in macro_ids:
            raise ConfigurationError(f"{label} refers to a macro that is not included in this preset.")
    elif kind == "device":
        if re.fullmatch(r"[0-9a-fA-F]{8}", action_value) is None:
            raise ConfigurationError(f"{label} must preserve a four-byte device action.")
    elif kind == "dpi" and action_value.startswith(CUSTOM_PRECISION_PREFIX):
        if parse_custom_precision_dpi(action_value) is None:
            raise ConfigurationError(
                f"{label} custom Easy-Aim DPI must be from {DPI_MIN} to {DPI_MAX} "
                f"in steps of {DPI_STEP}.")
    else:
        _choice(action_value, label, ACTION_VALUES[kind])
    return Action(kind, action_value)


def _parse_macro(value: Any, index: int) -> Macro:
    label = f"Macro {index}"
    value = _object(value, label, ("id", "name", "events", "repeat", "playback"))
    macro_id = _text(value["id"], f"{label} ID", 64)
    if re.fullmatch(r"[A-Za-z0-9_-]+", macro_id) is None:
        raise ConfigurationError(f"{label} ID may contain only letters, digits, underscores, and hyphens.")
    events = []
    pressed: set[tuple[str, str]] = set()
    duration = 0
    for event_index, event in enumerate(_items(value["events"], f"{label} events", MAX_MACRO_EVENTS), 1):
        event_label = f"{label}, event {event_index}"
        event = _object(event, event_label, ("kind", "value", "delay_ms"))
        kind = _choice(event["kind"], f"{event_label} type", MACRO_EVENT_KINDS)
        delay = _integer(event["delay_ms"], f"{event_label} delay", 0, 60000)
        duration += delay
        if duration > 3600000:
            raise ConfigurationError(f"{label} may contain at most one hour of delays per playback.")
        if kind == "delay":
            event_value = _choice(event["value"], f"{event_label} value", ("",))
        else:
            event_value = (_key(event["value"], event_label) if kind.startswith("key_")
                           else _choice(event["value"], event_label, _MOUSE_EVENT_BUTTONS))
            category, direction = kind.split("_")
            key = (category, event_value.lower())
            if direction == "down":
                if key in pressed:
                    raise ConfigurationError(f"{event_label} presses a key or button that is already held.")
                pressed.add(key)
            else:
                if key not in pressed:
                    raise ConfigurationError(f"{event_label} releases a key or button that has not been pressed.")
                pressed.remove(key)
        events.append(MacroEvent(kind, event_value, delay))
    if pressed:
        raise ConfigurationError(f"{label} must release every key and button before it ends.")
    return Macro(
        id=macro_id, name=_text(value["name"], f"{label} name", 80), events=events,
        repeat=_integer(value["repeat"], f"{label} repeat count", 1, 999),
        playback=_choice(value["playback"], f"{label} playback", MACRO_PLAYBACK_MODES),
    )


def _parse_countdown_timer(value: Any, index: int) -> CountdownTimer:
    label = f"Countdown timer {index}"
    value = _object(value, label, ("id", "name", "duration_seconds"))
    timer_id = _text(value["id"], f"{label} ID", 64)
    # Native timer state uses underscores as field separators. Restrict local
    # IDs now so a later runtime can forward them without ambiguous parsing.
    if re.fullmatch(r"[A-Za-z0-9-]+", timer_id) is None:
        raise ConfigurationError(
            f"{label} ID may contain only letters, digits, and hyphens.")
    return CountdownTimer(
        id=timer_id,
        name=_text(value["name"], f"{label} name", 80),
        duration_seconds=_integer(
            value["duration_seconds"], f"{label} duration", 1, 600),
    )


def default_configuration() -> Configuration:
    """Return independent editor defaults; these are not a device snapshot."""
    return Configuration()


def configuration_directory() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "swarm2"
    if sys.platform == "win32":
        root = os.environ.get("APPDATA")
        return (Path(root) if root and Path(root).is_absolute()
                else Path.home() / "AppData" / "Roaming") / "MC7 Studio"
    root = os.environ.get("XDG_CONFIG_HOME")
    # XDG specifies an absolute path; ignore a relative environment override.
    return (Path(root) if root and Path(root).is_absolute() else Path.home() / ".config") / "swarm2"


def _json_pairs(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationError(f"Preset JSON contains the duplicate field {key!r}.")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ConfigurationError(f"Preset JSON contains the invalid number {value}.")


def read_preset(path: str | Path) -> Configuration:
    """Read and validate bounded plain JSON; imported actions are never run."""
    path = Path(path)
    try:
        # Nonblocking open lets us reject a FIFO without waiting for a writer.
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ConfigurationError("Choose a regular JSON preset file.")
            raw = stream.read(MAX_PRESET_BYTES + 1)
        if len(raw) > MAX_PRESET_BYTES:
            raise ConfigurationError("Preset files must be no larger than 1 MiB.")
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_json_pairs, parse_constant=_invalid_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        if isinstance(exc, ConfigurationError):
            raise
        raise ConfigurationError("The preset must contain valid UTF-8 JSON with a bounded structure.") from exc
    return Configuration.from_dict(data)


def _write_atomic(path: Path, raw: bytes) -> Path:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        sync_directory(path.parent)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
    return path


class PresetStore:
    """Named local drafts stored under the platform's configuration directory."""

    def __init__(self, directory: str | Path | None = None):
        self.directory = Path(directory) if directory is not None else configuration_directory()

    def _path(self, name: str) -> Path:
        name = _text(name, "Preset name", 80)
        # Names remain user text, never filesystem paths or shell fragments.
        return self.directory / f"{hashlib.sha256(name.encode('utf-8')).hexdigest()}.json"

    def list(self) -> list[Configuration]:
        if not self.directory.exists():
            return []
        configurations = []
        for path in self.directory.glob("*.json"):
            try:
                configurations.append(read_preset(path))
            except ConfigurationError as exc:
                raise ConfigurationError(f"Cannot load preset {path.name}: {exc}") from exc
        return sorted(configurations, key=lambda configuration: configuration.name.casefold())

    def save(self, configuration: Configuration) -> Path:
        return self.export(configuration, self._path(configuration.name))

    def delete(self, name: str) -> bool:
        path = self._path(name)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True

    def export(self, configuration: Configuration, path: str | Path) -> Path:
        raw = (json.dumps(configuration.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if len(raw) > MAX_PRESET_BYTES:
            raise ConfigurationError("This preset exceeds the 1 MiB file size limit; reduce its macros.")
        return _write_atomic(Path(path), raw)

    def import_file(self, path: str | Path, *, save: bool = True) -> Configuration:
        configuration = read_preset(path)
        if save:
            # Importing a shared preset must not silently replace a local edit.
            original_name = configuration.name
            counter = 1
            while self._path(configuration.name).exists():
                try:
                    if read_preset(self._path(configuration.name)) == configuration:
                        return configuration
                except ConfigurationError:
                    pass
                suffix = " (imported)" if counter == 1 else f" (imported {counter})"
                configuration.name = original_name[:80 - len(suffix)] + suffix
                counter += 1
            self.save(configuration)
        return configuration
