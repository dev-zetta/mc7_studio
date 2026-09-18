"""Host-owned actions for MC7 LCD tiles.

The mouse stores a small command-0x29 trigger record and emits an LCD touch
event.  The full website or filesystem target remains in the local preset and
is opened only by the explicit listener process.  This module never uses a
shell and importing or validating a preset never launches anything.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import ntpath
import posixpath
import platform
import shutil
import subprocess
from types import MappingProxyType
from urllib.parse import SplitResult, urlsplit

from .lcd_commands import LCD_HOST_ACTION_WIDGETS
from .protocol import ProtocolError
from .runtime import host_command_environment
from .transport import DeviceError


MAX_HOST_ACTION_TARGET_BYTES = 4096
MAX_WEBSITE_TARGET_BYTES = 2048

# Command-0x29 records store Data0, Data1, FunctionID, FunctionType, icon and
# a six-byte qstrncpy label. Open Application uses the one-based custom-icon
# reference in that fifth byte; its public icon index is zero based.
HOST_ACTION_FUNCTION_CODES = MappingProxyType({
    "open_application": (0x0B, 0x01),
    "open_website": (0x0B, 0x07),
    "open_file": (0x0B, 0x05),
    "open_folder": (0x0B, 0x06),
})


def _text(value: object, *, maximum: int, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a nonempty string of at most {maximum} UTF-8 bytes")
    if any(ord(character) < 0x20 or ord(character) == 0x7F
           or 0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ValueError(f"{label} must not contain control characters or invalid Unicode")
    if len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{label} must be a nonempty string of at most {maximum} UTF-8 bytes")
    return value


def _website(value: str) -> SplitResult:
    if any(character.isspace() for character in value):
        raise ValueError("Website target must not contain whitespace")
    try:
        parsed = urlsplit(value)
        # Accessing these fields performs bracket and port validation.
        host, port = parsed.hostname, parsed.port
    except ValueError as error:
        raise ValueError("Website target must contain a valid host and optional port") from error
    if parsed.scheme.lower() not in ("http", "https") or not host:
        raise ValueError("Website target must use http or https and contain a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Website target must not contain embedded credentials")
    del port
    return parsed


def validate_host_action_target(widget_key: str, target: object) -> str:
    """Validate a persisted target without touching the filesystem or opening it."""
    if not isinstance(widget_key, str) or widget_key not in LCD_HOST_ACTION_WIDGETS:
        raise ValueError("Unsupported host-action LCD widget")
    maximum = (MAX_WEBSITE_TARGET_BYTES if widget_key == "open_website"
               else MAX_HOST_ACTION_TARGET_BYTES)
    value = _text(target, maximum=maximum, label="Host-action target")
    if widget_key == "open_website":
        _website(value)
        return value
    # Presets can move between operating systems; validate their stored path
    # syntax independently of this host. Execution still requires a local path.
    if PureWindowsPath(value).is_absolute():
        path = PureWindowsPath(ntpath.normpath(value))
    elif posixpath.isabs(value):
        path = PurePosixPath(posixpath.normpath(value))
    else:
        raise ValueError("Application, file and folder targets must use an absolute path")
    if not path.name:
        raise ValueError("Application, file and folder targets must not name a filesystem root")
    return value


def _display_label(widget_key: str, target: str) -> str:
    if widget_key == "open_website":
        host = _website(target).hostname or ""
        if host.lower().startswith("www."):
            host = host[4:]
        try:
            return host.encode("idna").decode("ascii")
        except UnicodeError:
            return host
    path = PureWindowsPath(target) if PureWindowsPath(target).is_absolute() else PurePosixPath(target)
    return path.stem if widget_key == "open_application" else path.name


def encode_host_action_record(widget_key: str, target: object, *,
                              icon_index: int | None = None) -> bytes:
    """Encode the known command-0x29 record; the full target is not embedded."""
    try:
        value = validate_host_action_target(widget_key, target)
    except ValueError as error:
        raise ProtocolError(str(error)) from error
    function_type, function_id = HOST_ACTION_FUNCTION_CODES[widget_key]
    if widget_key == "open_application":
        if type(icon_index) is not int or not 0 <= icon_index <= 19:
            raise ProtocolError(
                "Open Application requires a custom-icon index from 0 to 19")
        icon_reference = icon_index + 1
    else:
        if icon_index is not None:
            raise ProtocolError(
                "Only Open Application accepts a custom-icon index")
        icon_reference = 0
    label = _display_label(widget_key, value).encode("ascii", "replace")[:5]
    label = bytes(byte if 0x20 <= byte <= 0x7E else ord("?") for byte in label)
    if not label:
        raise ProtocolError("Host-action display label must not be empty")
    return bytes((0, 0, function_id, function_type, icon_reference)) + label.ljust(6, b"\0")


def decode_host_action_record(record: bytes, widget_key: str) -> str:
    """Return the five-byte display label from one supported trigger record."""
    if (not isinstance(record, (bytes, bytearray)) or len(record) != 11
            or widget_key not in HOST_ACTION_FUNCTION_CODES):
        raise ProtocolError("Host-action record or widget is invalid")
    raw = bytes(record)
    function_type, function_id = HOST_ACTION_FUNCTION_CODES[widget_key]
    if raw[:4] != bytes((0, 0, function_id, function_type)):
        raise ProtocolError("Host-action record function does not match its LCD widget")
    if widget_key == "open_application" and not 1 <= raw[4] <= 20:
        raise ProtocolError(
            "Open Application record contains an invalid custom-icon reference")
    label, separator, _padding = raw[5:11].partition(b"\0")
    if (not separator or raw[10] != 0 or not label
            or any(not 0x20 <= byte <= 0x7E for byte in label)):
        raise ProtocolError("Host-action record contains an invalid display label")
    return label.decode("ascii")


def same_host_action_record(existing: bytes, requested: bytes, widget_key: str) -> bool:
    """Compare display semantics and the Open Application icon reference."""
    try:
        same_label = (decode_host_action_record(existing, widget_key)
                      == decode_host_action_record(requested, widget_key))
        return (same_label and (widget_key != "open_application"
                                or bytes(existing)[4] == bytes(requested)[4]))
    except ProtocolError:
        return False


@dataclass(frozen=True)
class HostActionBinding:
    widget_key: str
    page_index: int
    slot_index: int
    target: str
    icon_index: int | None = None

    def __post_init__(self) -> None:
        if type(self.page_index) is not int or not 0 <= self.page_index <= 2:
            raise ValueError("Host-action page must be 0..2")
        if type(self.slot_index) is not int or not 0 <= self.slot_index <= 3:
            raise ValueError("Host-action slot must be 0..3")
        validate_host_action_target(self.widget_key, self.target)
        if self.widget_key == "open_application":
            if type(self.icon_index) is not int or not 0 <= self.icon_index <= 19:
                raise ValueError(
                    "Open Application requires a custom-icon index from 0 to 19")
        elif self.icon_index is not None:
            raise ValueError("Only Open Application accepts a custom-icon index")


def _resolved_target(binding: HostActionBinding, *, system: str) -> str:
    value = validate_host_action_target(binding.widget_key, binding.target)
    if binding.widget_key == "open_website":
        return value
    if not Path(value).is_absolute():
        raise DeviceError("Choose a host-action target on this operating system")
    try:
        path = Path(value).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise DeviceError("The selected host-action target no longer exists") from error
    if binding.widget_key == "open_application":
        is_macos_bundle = (
            system == "Darwin" and path.is_dir()
            and path.suffix.lower() == ".app")
        if not is_macos_bundle and not path.is_file():
            raise DeviceError(
                "The selected Open Application target is not an executable file or macOS app bundle")
        if not is_macos_bundle and not os.access(path, os.X_OK):
            raise DeviceError(
                "The selected Open Application target is not executable")
    elif binding.widget_key == "open_file" and not path.is_file():
        raise DeviceError("The selected Open File target is not a regular file")
    if binding.widget_key == "open_folder" and not path.is_dir():
        raise DeviceError("The selected Open Folder target is not a directory")
    return os.fspath(path)


def build_host_action_argv(binding: HostActionBinding, *, system: str | None = None,
                           which=shutil.which) -> tuple[str, ...]:
    """Build one fixed argv vector without executing it or invoking a shell."""
    if not isinstance(binding, HostActionBinding):
        raise DeviceError("Host action requires a validated binding")
    binding.__post_init__()
    host_system = platform.system() if system is None else system
    target = _resolved_target(binding, system=host_system)
    if binding.widget_key == "open_application":
        if host_system == "Darwin" and Path(target).is_dir():
            return "/usr/bin/open", target
        if host_system in ("Darwin", "Linux", "Windows"):
            return (target,)
        raise DeviceError("Host LCD actions require Linux, macOS or Windows")
    if host_system == "Darwin":
        opener = "/usr/bin/open"
    elif host_system == "Linux":
        opener = which("xdg-open")
        if not isinstance(opener, str) or not posixpath.isabs(opener):
            raise DeviceError("xdg-open is required for host LCD actions")
    elif host_system == "Windows":
        opener = which("explorer.exe")
        if not isinstance(opener, str) or not PureWindowsPath(opener).is_absolute():
            raise DeviceError("Windows Explorer is required for host LCD actions")
    else:
        raise DeviceError("Host LCD actions require Linux, macOS or Windows")
    return opener, target


def execute_host_action(binding: HostActionBinding, *, system: str | None = None,
                        which=shutil.which, process_factory=subprocess.Popen):
    """Open one validated target through a fixed argv vector and no shell."""
    try:
        argv = build_host_action_argv(binding, system=system, which=which)
        return process_factory(
            argv, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True,
            env=host_command_environment())
    except DeviceError:
        raise
    except (OSError, ValueError) as error:
        raise DeviceError("The host LCD action could not be opened") from error
