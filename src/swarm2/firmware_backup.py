"""Validate firmware backups and merge supported settings without raw replay.

All functions are offline. A RestorePlan still needs the ordinary settings
transaction's fresh baseline, acknowledgement and readback checks.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from .file_io import open_regular_read
from pathlib import Path
import re
import stat

from .button_commands import BUTTON_SLOTS
from .configuration import Action, Configuration
from .firmware_catalog import FirmwareError
from .firmware_commands import decode_realtek_version, decode_version_response
from .lcd_commands import (
    LCD_HOST_ACTION_WIDGETS, LCD_KEY_WIDGETS, LCD_TIMER_WIDGETS, LCD_WIDGETS,
)
from .screen_key_commands import (DEVICE_BINDING_PREFIX, SCREEN_KEY_RESPONSES_BYTES,
                                  decode_screen_key_responses, screen_key_bindings)
from .service import DeviceService
from .settings import PER_PROFILE, REQUIRED, SELECTORS, decode, stable
from .sensor_commands import SCREEN_BRIGHTNESS_LEVELS, SCREEN_TIMEOUT_VALUES
from .status_commands import decode_status_response

BACKUP_SCHEMA = "swarm2.mc7.firmware-backup.v1"
MAX_BACKUP_BYTES = 4 * 1024 * 1024
_ROOT_KEYS = {"schema", "device_id", "transport_identity", "archive_sha256",
              "status_raw", "cfu_raw", "profiles", "read_at", "limits"}
_OPTIONAL_ROOT_KEYS = {"release_key"}
_PROFILE_KEYS = {"raw", "settings", "errors", "macro_data", "changed"}
_OPTIONAL_PROFILE_SETTINGS = {"screen_keys"}
_GENERAL_WARNING = ("Restoration copies supported settings through the normal editor. "
                    "Unknown bytes, lift-off calibration, custom LCD background pixels, "
                    "Open Application icon pixels and background selection remain on "
                    "the current mouse.")


@dataclass(frozen=True)
class BackupProfile:
    profile_slot: int
    snapshot: dict
    warnings: tuple[str, ...]

    @property
    def configuration(self) -> Configuration:
        return self.snapshot["configuration"]

    @property
    def verified_fields(self) -> tuple[str, ...]:
        return tuple(self.snapshot["verified_fields"])

    @property
    def errors(self) -> dict:
        return self.snapshot["errors"]


@dataclass(frozen=True)
class FirmwareBackup:
    value: dict
    sha256: str
    profiles: tuple[BackupProfile, ...]
    warnings: tuple[str, ...]

    @property
    def device_id(self) -> str:
        return self.value["device_id"]

    @property
    def transport_identity(self) -> str:
        return self.value["transport_identity"]

    @property
    def firmware_version(self) -> str:
        return decode_status_response(bytes.fromhex(self.value["status_raw"])).firmware_version


@dataclass(frozen=True)
class RestorePlan:
    configuration: Configuration
    sections: tuple[str, ...]
    warnings: tuple[str, ...]


def _object(value, keys, label, optional=()):
    if (not isinstance(value, dict) or not set(keys).issubset(value)
            or set(value).difference(keys, optional)):
        raise FirmwareError(f"{label} contains missing or unsupported fields")
    return value


def _text(value, label, maximum=1024):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value)):
        raise FirmwareError(f"{label} must be bounded, nonempty text without control characters")
    return value


def _hex(value, label, maximum=64):
    if (not isinstance(value, str) or not value or len(value) > maximum * 2
            or len(value) % 2 or re.fullmatch(r"[0-9a-fA-F]+", value) is None):
        raise FirmwareError(f"{label} must contain bounded hexadecimal bytes")
    return bytes.fromhex(value)


def _digest(value, label):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise FirmwareError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise FirmwareError(f"Backup JSON contains duplicate field {key!r}")
        result[key] = value
    return result


def _invalid_constant(value):
    raise FirmwareError(f"Backup JSON contains invalid number {value}")


def _setting_records(value, label):
    """Accept v1 settings with or without the later screen-key record."""
    required = set(SELECTORS).difference(_OPTIONAL_PROFILE_SETTINGS)
    return _object(value, required, label, _OPTIONAL_PROFILE_SETTINGS)


def _decode_setting(name, raw, profile):
    if name == "screen_keys":
        return decode_screen_key_responses(raw, profile)
    return decode(name, raw, profile)


def _has_key_widget(page):
    return any(widget in LCD_KEY_WIDGETS for widget in page)


def _has_macro_widget(page):
    return any(widget == "macro" for widget in page)


def _has_countdown_widget(page):
    return any(widget in LCD_TIMER_WIDGETS for widget in page)


def _has_host_action_widget(page):
    return any(widget in LCD_HOST_ACTION_WIDGETS for widget in page)


def _has_opaque_binding(row):
    return any(isinstance(binding, str) and binding.startswith(DEVICE_BINDING_PREFIX)
               for binding in row)


def _clone_current_configuration(current):
    """Keep a readable LCD layout usable when its optional key read failed."""
    clone = deepcopy(current)
    if (not clone.display.key_bindings
            and any(_has_key_widget(page) for page in clone.display.pages)):
        # These placeholders are never treated as verified. They let the
        # restore plan retain the current layout while the page-level merge
        # below skips every key page whose raw definitions were not read.
        clone.display.key_bindings = [[None] * 4 for _ in clone.display.pages]
    return Configuration.from_dict(clone.to_dict())


def read_backup(path: str | Path, expected_sha256: str | None = None) -> FirmwareBackup:
    """Read a bounded regular file, validate every bundle, and decode drafts.

    The digest detects edits to an already reviewed file. It is not a signature
    or proof of device ownership. Unsupported macro formats remain opaque and
    are never used as executable write images.
    """
    if expected_sha256 is not None:
        _digest(expected_sha256, "Expected backup digest")
    descriptor = open_regular_read(path)
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise FirmwareError("Choose a regular firmware backup file")
        data = source.read(MAX_BACKUP_BYTES + 1)
    if len(data) > MAX_BACKUP_BYTES:
        raise FirmwareError("Firmware backups must be no larger than 4 MiB")
    digest = hashlib.sha256(data).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise FirmwareError("The firmware backup changed; inspect it again before restoring")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_invalid_constant)
        return _validate_backup(value, digest)
    except (UnicodeError, RecursionError, ValueError, KeyError, TypeError) as error:
        if isinstance(error, FirmwareError):
            raise
        raise FirmwareError(f"Invalid firmware backup: {error}") from error


def _validate_backup(value, digest):
    _object(value, _ROOT_KEYS, "Firmware backup", _OPTIONAL_ROOT_KEYS)
    if value["schema"] != BACKUP_SCHEMA:
        raise FirmwareError("This is not a supported MC7 firmware backup")
    if "release_key" in value:
        release_key = _text(value["release_key"], "Firmware release key", 100)
        if re.fullmatch(r"mouse:[0-9]+(?:\.[0-9]+){3}", release_key) is None:
            raise FirmwareError("Firmware release key must use mouse:x.y.z.w format")
    for key in ("device_id", "transport_identity", "limits"):
        _text(value[key], key)
    _digest(value["archive_sha256"], "Firmware archive digest")
    timestamp = datetime.fromisoformat(_text(value["read_at"], "Backup timestamp", 100))
    if timestamp.tzinfo is None:
        raise FirmwareError("Backup timestamp must include its timezone")
    status = decode_status_response(_hex(value["status_raw"], "Mouse status"))
    cfu = decode_version_response(_hex(value["cfu_raw"], "CFU version"))
    if (cfu.component_id != 0 or cfu.bank != 2
            or decode_realtek_version(cfu.version_raw) != (1, 7, status.firmware_major, status.firmware_minor)):
        raise FirmwareError("Backup CFU identity and mouse firmware version do not match")
    if not isinstance(value["profiles"], list) or len(value["profiles"]) != 5:
        raise FirmwareError("Firmware backup must contain all five profiles in slot order")
    profiles, globals_before = [], None
    for index, bundle in enumerate(value["profiles"]):
        label = f"Profile {index + 1}"
        _object(bundle, _PROFILE_KEYS, label)
        if bundle["errors"] != {} or bundle["changed"] is not False:
            raise FirmwareError(f"{label} is not a complete read-only settings backup")
        _setting_records(bundle["settings"], f"{label} settings")
        records = {name: _hex(raw, f"{label} {name}",
                              SCREEN_KEY_RESPONSES_BYTES if name == "screen_keys" else 64)
                   for name, raw in bundle["settings"].items()}
        if _hex(bundle["raw"], f"{label} sensor") != records["sensor"]:
            raise FirmwareError(f"{label} sensor data disagrees with its settings record")
        states = {name: _decode_setting(name, raw, index) for name, raw in records.items()}
        if any(state.raw != records[name] for name, state in states.items()):
            raise FirmwareError(f"{label} contains noncanonical setting records")
        if states["status"].firmware_numeric != status.firmware_numeric:
            raise FirmwareError(f"{label} contains a different mouse firmware version")
        globals_now = {name: stable(name, raw) for name, raw in records.items()
                       if name not in set(PER_PROFILE) | _OPTIONAL_PROFILE_SETTINGS and name != "status"}
        if globals_before is not None and globals_now != globals_before:
            raise FirmwareError("Global settings changed while the five profiles were backed up")
        globals_before = globals_now
        assignments = {f"{layer}:{logical}" for layer in ("primary", "easy_shift")
                       for logical in BUTTON_SLOTS if states[layer].records[logical][3] == 7}
        assignments.update(
            f"lcd:{page * 4 + cell}"
            for page, current in enumerate(states["lcd"].pages[:min(3, states["lcd"].page_count)])
            for cell, widget in enumerate(current.slots)
            if widget is not None and widget.key == "macro")
        _object(bundle["macro_data"], assignments, f"{label} assigned macros")
        for key, payload in bundle["macro_data"].items():
            if len(_hex(payload, f"{label} macro {key}", 1037)) != 1037:
                raise FirmwareError(f"{label} macro {key} must contain all seventeen read chunks")
        snapshot_bundle = deepcopy(bundle)
        snapshot = DeviceService._snapshot(value["device_id"], index + 1, snapshot_bundle)
        if "screen_keys" in records:
            snapshot["baseline"]["settings"]["screen_keys"] = records["screen_keys"].hex()
        pages = snapshot["configuration"].display.pages
        screen_key_warnings = []
        if "screen_keys" in states:
            decoded_bindings = screen_key_bindings(states["screen_keys"], states["lcd"])
            bindings, complete = [], True
            for page_index, page in enumerate(pages):
                row = []
                for slot, widget in enumerate(page):
                    binding = decoded_bindings[page_index][slot]
                    if widget not in LCD_KEY_WIDGETS and binding is not None:
                        complete = False
                        screen_key_warnings.append(
                            f"{label}: screen-key data for a non-key LCD tile is opaque and cannot be restored.")
                        binding = None
                    row.append(binding)
                bindings.append(row)
            snapshot["configuration"].display.key_bindings = (
                bindings if any(_has_key_widget(page) for page in pages) else [])
            if complete:
                if "display.key_bindings" not in snapshot["verified_fields"]:
                    snapshot["verified_fields"].append("display.key_bindings")
            elif "display.key_bindings" in snapshot["verified_fields"]:
                snapshot["verified_fields"].remove("display.key_bindings")
        elif any(_has_key_widget(page) for page in pages):
            # None is a valid portable placeholder, but it is deliberately not
            # marked verified: restore keeps each affected target page intact.
            snapshot["configuration"].display.key_bindings = [[None] * 4 for _ in pages]
            screen_key_warnings.append(
                f"{label}: screen-key data is absent; LCD pages containing key tiles cannot be restored.")
        snapshot["configuration"].name = f"Firmware backup profile {index + 1}"
        snapshot["configuration"].validate()
        warnings = [*screen_key_warnings,
                    *(f"{label}: {name}: {message}" for name, message in snapshot["errors"].items())]
        for page_index, row in enumerate(snapshot["configuration"].display.key_bindings, 1):
            if _has_opaque_binding(row):
                warnings.append(
                    f"{label}: LCD page {page_index} contains an opaque screen-key binding and cannot be restored.")
        for binding in snapshot["configuration"].buttons:
            for layer in ("primary", "easy_shift"):
                if getattr(binding, layer).kind == "device":
                    warnings.append(f"{label}: button {binding.button_id} {layer} is opaque and cannot be restored.")
        if states["lighting"].effect is None or not 1 <= states["lighting"].speed <= 10:
            warnings.append(f"{label}: unknown lighting effect or speed cannot be restored.")
        if states["sensor"].polling_rate_usb != states["sensor"].polling_rate_wireless:
            warnings.append(f"{label}: separate wireless polling is not represented by the editor.")
        if states["debounce"].debounce_ms != states["debounce"].secondary_debounce_ms:
            warnings.append(f"{label}: unequal debounce values cannot be restored separately.")
        if (states["screen"].brightness not in SCREEN_BRIGHTNESS_LEVELS
                or states["screen"].timeout_value not in SCREEN_TIMEOUT_VALUES):
            warnings.append(f"{label}: screen brightness or timeout is outside the supported write values.")
        if any(key is not None and key not in LCD_WIDGETS for page in snapshot["configuration"].display.pages for key in page):
            warnings.append(f"{label}: LCD pages containing unknown widgets cannot be restored.")
        if any(_has_macro_widget(page) for page in snapshot["configuration"].display.pages):
            warnings.append(
                f"{label}: restoring an LCD Macro tile overwrites its coordinate-bound raw slot; "
                "the mouse has no known per-slot erase and an interrupted upload cannot be rolled back automatically.")
        if any(_has_countdown_widget(page)
               for page in snapshot["configuration"].display.pages):
            warnings.append(
                f"{label}: Count down timer IDs and durations are host-only; "
                "the raw backup preserves the tile but contains no timer assignment.")
        if any(_has_host_action_widget(page)
               for page in snapshot["configuration"].display.pages):
            warnings.append(
                f"{label}: Website, application, file and folder launch targets are host-only, "
                "and Open Application icon pixels cannot be read back; the backup preserves "
                "their tiles and any captured trigger records but contains no targets or icon images.")
        profiles.append(BackupProfile(index + 1, snapshot, tuple(warnings)))
    warnings = (_GENERAL_WARNING, *(warning for profile in profiles for warning in profile.warnings))
    return FirmwareBackup(value, digest, tuple(profiles), warnings)


def merge_supported_profile(profile: BackupProfile, current_snapshot: dict) -> RestorePlan:
    """Copy only supported source fields onto a freshly read target profile.

    Opaque actions, private LCD pages and device-generated bytes stay with the
    target. Lift-off and background selection always stay unchanged. Global
    settings copied here must be coordinated across profiles by the caller.
    """
    if not isinstance(profile, BackupProfile):
        raise FirmwareError("Choose a validated firmware backup profile")
    if current_snapshot.get("profile_slot") != profile.profile_slot:
        raise FirmwareError("Read the matching target profile before restoring")
    current = current_snapshot["configuration"]
    if not isinstance(current, Configuration) or current.profile_slot != profile.profile_slot:
        raise FirmwareError("The target configuration does not match its profile slot")
    draft = _clone_current_configuration(current)
    source, fields = profile.configuration, set(profile.verified_fields)
    current_fields = set(current_snapshot.get("verified_fields", ()))
    raw = {name: bytes.fromhex(value) for name, value in current_snapshot["baseline"]["settings"].items()}
    states = {name: _decode_setting(name, value, profile.profile_slot - 1)
              for name, value in raw.items()}
    source_states = {name: _decode_setting(name, bytes.fromhex(value), profile.profile_slot - 1)
                     for name, value in profile.snapshot["baseline"]["settings"].items()}
    warnings = [_GENERAL_WARNING, *profile.warnings]
    sections = []
    excluded = {"sensor.lift_off_distance", "display.background_index",
                "display.pages", "display.key_bindings", "display.macro_bindings",
                "display.timer_bindings", "display.host_action_bindings",
                "display.host_action_icon_bindings"}
    if source_states["sensor"].polling_rate_usb != source_states["sensor"].polling_rate_wireless:
        excluded.update(("sensor.polling_rate", "sensor.motion_sync"))
    if source_states["debounce"].debounce_ms != source_states["debounce"].secondary_debounce_ms:
        excluded.add("sensor.debounce_ms")
    # Do not normalize an unrelated target's paired fields as a side effect.
    if "sensor" in states and states["sensor"].polling_rate_usb != states["sensor"].polling_rate_wireless:
        excluded.update(("sensor.polling_rate", "sensor.motion_sync"))
        warnings.append("Current USB/wireless polling values differ; both are preserved.")
    if "debounce" in states and states["debounce"].debounce_ms != states["debounce"].secondary_debounce_ms:
        excluded.add("sensor.debounce_ms")
        warnings.append("Current paired debounce values differ; both are preserved.")
    if any(state.brightness not in SCREEN_BRIGHTNESS_LEVELS or state.timeout_value not in SCREEN_TIMEOUT_VALUES
           for state in (source_states["screen"], states.get("screen")) if state is not None):
        excluded.update(("display.brightness", "display.timeout_value"))
        warnings.append("Screen brightness or timeout is outside supported write values; both current values are preserved.")
    for section in ("sensor", "lighting", "display", "power"):
        if any(name not in states for name in REQUIRED[section]):
            warnings.append(f"{section.capitalize()} settings were not read completely; this section is skipped.")
            continue
        if section == "lighting" and (states["lighting"].effect is None or not 1 <= states["lighting"].speed <= 10
                or not {"lighting.effect", "lighting.speed"}.issubset(fields)):
            warnings.append("Lighting uses an unknown effect or speed; current lighting is preserved.")
            continue
        copied = False
        for field in sorted(fields & current_fields):
            if field.startswith(section + ".") and field not in excluded:
                name = field.split(".", 1)[1]
                setattr(getattr(draft, section), name, deepcopy(getattr(getattr(source, section), name)))
                copied = True
        if copied:
            sections.append(section)
    source_library = {macro.id: macro for macro in source.macros}
    restored_macro_ids = {}

    def restored_macro_id(source_id):
        if source_id not in restored_macro_ids:
            macro = deepcopy(source_library[source_id])
            occupied = {item.id for item in draft.macros}
            name, suffix = macro.id, 2
            while macro.id in occupied:
                macro.id = f"{name[:50]}_restored{suffix}"
                suffix += 1
            draft.macros.append(macro)
            restored_macro_ids[source_id] = macro.id
        return restored_macro_ids[source_id]

    if "lcd" in states and "display.pages" in fields & current_fields:
        pages = deepcopy(current.display.pages)
        bindings = (deepcopy(current.display.key_bindings) if current.display.key_bindings
                    else [[None] * 4 for _ in pages])
        macro_bindings = (deepcopy(current.display.macro_bindings)
                          if current.display.macro_bindings
                          else [[None] * 4 for _ in pages])
        timer_bindings = (deepcopy(current.display.timer_bindings)
                          if current.display.timer_bindings
                          else [[None] * 4 for _ in pages])
        host_action_bindings = (
            deepcopy(current.display.host_action_bindings)
            if current.display.host_action_bindings
            else [[None] * 4 for _ in pages])
        host_action_icon_bindings = (
            deepcopy(current.display.host_action_icon_bindings)
            if current.display.host_action_icon_bindings
            else [[None] * 4 for _ in pages])
        source_bindings = (source.display.key_bindings if source.display.key_bindings
                           else [[None] * 4 for _ in source.display.pages])
        source_macro_bindings = (source.display.macro_bindings
                                 if source.display.macro_bindings
                                 else [[None] * 4 for _ in source.display.pages])
        source_key_support = ("display.key_bindings" in fields
                              and "screen_keys" in source_states)
        current_key_support = ("display.key_bindings" in current_fields
                               and "screen_keys" in states)
        source_macro_support = ("display.macro_bindings" in fields
                                and "screen_keys" in source_states)
        current_macro_support = ("display.macro_bindings" in current_fields
                                 and "screen_keys" in states)
        pages_copied = False
        for index, page in enumerate(source.display.pages):
            if index >= len(pages):
                warnings.append(f"LCD page {index + 1} is not currently enabled; it is skipped.")
            elif any(key is not None and key not in LCD_WIDGETS for key in (*page, *pages[index])):
                warnings.append(f"LCD page {index + 1} contains unknown widgets; the current page is preserved.")
            elif _has_countdown_widget(page) or _has_countdown_widget(pages[index]):
                # Firmware backups contain the 46/00 layout tile but never the
                # host TimerID/Second assignment. Keep the target page intact
                # instead of producing a countdown page whose meaning cannot
                # be recovered from either mouse.
                warnings.append(
                    f"LCD page {index + 1} contains a Count down timer with host-only data; "
                    "the current page is preserved.")
            elif (_has_host_action_widget(page)
                  or _has_host_action_widget(pages[index])):
                # A backup retains the visible 05/04..06 tile and its short
                # command-0x29 trigger, but never the full launch target or
                # Open Application icon pixels.
                # Restoring a changed page could therefore create a launch tile
                # with no meaning or carry the current target to another tile.
                # An identical page is already safely represented by the
                # current layout, targets and icons, so it needs no page write.
                detail = (
                    "is unchanged; its current launch targets and Open Application icons are preserved."
                    if page == pages[index]
                    else "has host-only launch targets or unreadable Open Application icons; "
                         "the current page is preserved.")
                warnings.append(f"LCD page {index + 1} {detail}")
            elif (_has_key_widget(page) or _has_key_widget(pages[index])
                  or _has_macro_widget(page) or _has_macro_widget(pages[index])):
                if not source_key_support or not current_key_support:
                    warnings.append(
                        f"LCD page {index + 1} needs complete source and current screen-key data; the current page is preserved.")
                elif index >= len(source_bindings) or index >= len(bindings):
                    warnings.append(
                        f"LCD page {index + 1} has incomplete screen-key rows; the current page is preserved.")
                elif _has_opaque_binding(source_bindings[index]) or _has_opaque_binding(bindings[index]):
                    warnings.append(
                        f"LCD page {index + 1} contains an opaque screen-key binding; the current page is preserved.")
                elif (_has_macro_widget(pages[index])
                      and (not current_macro_support
                           or index >= len(macro_bindings)
                           or any(macro_bindings[index][cell] is None
                                  for cell, widget in enumerate(pages[index])
                                  if widget == "macro"))):
                    warnings.append(
                        f"LCD page {index + 1} contains an opaque current macro tile; the current page is preserved.")
                elif (_has_macro_widget(page)
                      and (not source_macro_support
                           or index >= len(source_macro_bindings)
                           or any(source_macro_bindings[index][cell] is None
                                  for cell, widget in enumerate(page) if widget == "macro"))):
                    warnings.append(
                        f"LCD page {index + 1} contains an opaque macro tile; the current page is preserved.")
                else:
                    pages[index] = deepcopy(page)
                    bindings[index] = deepcopy(source_bindings[index])
                    macro_bindings[index] = [
                        (restored_macro_id(source_macro_bindings[index][cell])
                         if widget == "macro" else None)
                        for cell, widget in enumerate(page)]
                    pages_copied = True
            else:
                pages[index] = deepcopy(page)
                bindings[index] = [None] * 4
                macro_bindings[index] = [None] * 4
                timer_bindings[index] = [None] * 4
                host_action_bindings[index] = [None] * 4
                host_action_icon_bindings[index] = [None] * 4
                pages_copied = True
        draft.display.pages = pages
        draft.display.key_bindings = (bindings if any(_has_key_widget(page) for page in pages) else [])
        draft.display.macro_bindings = (
            macro_bindings if any(_has_macro_widget(page) for page in pages) else [])
        draft.display.timer_bindings = (
            timer_bindings if any(_has_countdown_widget(page) for page in pages) else [])
        draft.display.host_action_bindings = (
            host_action_bindings
            if any(_has_host_action_widget(page) for page in pages) else [])
        draft.display.host_action_icon_bindings = (
            host_action_icon_bindings
            if any(
                widget == "open_application"
                and host_action_bindings[page_index][cell] is not None
                for page_index, page in enumerate(pages)
                for cell, widget in enumerate(page))
            else [])
        if pages_copied and "display" not in sections:
            sections.append("display")
    if "buttons" in fields & current_fields and all(name in states for name in REQUIRED["buttons"]):
        macros_before_buttons = deepcopy(draft.macros)
        for incoming, existing in zip(sorted(source.buttons, key=lambda binding: binding.button_id),
                                      sorted(draft.buttons, key=lambda binding: binding.button_id)):
            for layer in ("primary", "easy_shift"):
                action, prior = getattr(incoming, layer), getattr(existing, layer)
                if action.kind == "device" or prior.kind == "device":
                    warnings.append(f"Button {existing.button_id} {layer} has an opaque action; the current assignment is preserved.")
                    continue
                if action.kind == "macro":
                    action = Action("macro", restored_macro_id(action.value))
                setattr(existing, layer, deepcopy(action))
        if all(any(binding.primary == needed for binding in draft.buttons)
               for needed in (Action("mouse", "left"), Action("mouse", "right"))):
            sections.append("buttons")
        else:
            draft.buttons = deepcopy(current.buttons)
            draft.macros = macros_before_buttons
            warnings.append("Restored buttons would not retain left and right click; current buttons are preserved.")
    draft.validate()
    order = ("sensor", "lighting", "buttons", "display", "power")
    return RestorePlan(draft, tuple(section for section in order if section in sections), tuple(dict.fromkeys(warnings)))
