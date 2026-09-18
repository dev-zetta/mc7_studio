"""Bounded foreground-application observation for automatic profiles.

The policy is deliberately narrow: one poll reports only the application that
owns the current foreground window.  It never enumerates all processes, keeps
no application history, opens no device, and never executes a path stored in
an automatic-profile rule.

Linux uses the X11/EWMH ``_NET_ACTIVE_WINDOW`` and ``_NET_WM_PID`` properties,
then resolves that numeric PID through ``/proc``.  Wayland uses the narrow
focused-window interface supplied by GNOME Shell, Sway, or Hyprland.  Other
Wayland compositors are reported as unavailable even when XWayland exposes a
``DISPLAY``: XWayland cannot see native Wayland windows, so using its answer
could select the wrong profile.  macOS asks the fixed NSWorkspace API for its one
``frontmostApplication`` through a bounded ``osascript`` invocation.

``ForegroundApplicationMonitor.poll`` is synchronous and has no Qt
dependency, so a coordinator can call it from a worker scheduled by QTimer.
At most two half-second commands run during a Linux poll and one during a
macOS poll.  Expected host failures are returned as explicit states instead of
escaping through an event loop.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
import json
import os
import platform
import re
import selectors
from .pipe_io import pipe_selector
import shutil
import subprocess
import time

from .automatic_profiles import ApplicationIdentity, AutomaticProfileError
from .gnome_extension import (
    GNOME_DBUS_INTERFACE,
    GNOME_DBUS_NAME,
    GNOME_DBUS_OBJECT_PATH,
    GNOME_EXTENSION_UUID,
)
from .runtime import host_command_environment


COMMAND_TIMEOUT_SECONDS = 0.5
MAX_COMMAND_OUTPUT_BYTES = 8 * 1024
MAX_SWAY_TREE_OUTPUT_BYTES = 1024 * 1024
MAX_SWAY_TREE_NODES = 4096

_ACTIVE_WINDOW = re.compile(
    r"_NET_ACTIVE_WINDOW\(WINDOW\): window id # (0x[0-9A-Fa-f]+)\s*\Z"
)
_WINDOW_PID = re.compile(r"_NET_WM_PID\(CARDINAL\) = ([1-9][0-9]{0,9})\s*\Z")
_GNOME_FOCUSED_PID = re.compile(
    r"\(\s*(true|false)\s*,\s*uint32\s+([0-9]{1,10})\s*\)\s*\Z"
)
_MAX_PID = 2_147_483_647
_PERMISSION_MARKERS = (
    "accessdenied",
    "accessibility",
    "assistive access",
    "authorization required",
    "not authorized",
    "not authorised",
    "operation not permitted",
    "permission denied",
    "privacy",
    "-1743",
)
_UNAVAILABLE_MARKERS = (
    "can't open display",
    "cannot open display",
    "unable to open display",
)
_GNOME_EXTENSION_ABSENT_MARKERS = (
    "namehasnoowner",
    "serviceunknown",
    "was not provided by any .service files",
)
_GNOME_EXTENSION_INCOMPATIBLE_MARKERS = (
    "unknownmethod",
    "unknownobject",
    "unknowninterface",
)
_WAYLAND_IPC_UNAVAILABLE_MARKERS = (
    "failed to connect",
    "hyprland instance",
    "instance signature",
    "ipc socket",
    "unable to connect",
)

_MACOS_FRONTMOST_APPLICATION_SCRIPT = r"""
ObjC.import('AppKit');

function run() {
    const application = $.NSWorkspace.sharedWorkspace.frontmostApplication;
    if (!application) {
        return JSON.stringify({bundle_id: null, executable_path: null});
    }
    const bundle = application.bundleIdentifier;
    const executable = application.executableURL;
    return JSON.stringify({
        bundle_id: bundle ? ObjC.unwrap(bundle) : null,
        executable_path: executable ? ObjC.unwrap(executable.path) : null
    });
}
""".strip()


class ForegroundApplicationStatus(str, Enum):
    """The complete set of outcomes a polling coordinator must handle."""

    AVAILABLE = "available"
    NO_APPLICATION = "no_application"
    PERMISSION_DENIED = "permission_denied"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


@dataclass(frozen=True)
class ForegroundApplicationSnapshot:
    """One foreground observation, with no timestamp or retained history."""

    status: ForegroundApplicationStatus
    application: ApplicationIdentity | None = None
    message: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.status, ForegroundApplicationStatus):
            raise TypeError("Foreground application status must be explicit.")
        if self.status is ForegroundApplicationStatus.AVAILABLE:
            if type(self.application) is not ApplicationIdentity:
                raise TypeError("An available foreground application needs an identity.")
            normalized = self.application.normalized()
            object.__setattr__(self, "application", normalized)
        elif self.application is not None:
            raise TypeError("Only an available observation can contain an identity.")
        if not isinstance(self.message, str):
            raise TypeError("Foreground application messages must be strings.")

    @property
    def succeeded(self) -> bool:
        """Whether this observation is safe to pass to the profile matcher."""

        return self.status in (
            ForegroundApplicationStatus.AVAILABLE,
            ForegroundApplicationStatus.NO_APPLICATION,
        )

    @property
    def applications(self) -> tuple[ApplicationIdentity, ...]:
        """Adapt the single-foreground policy to ``resolve_profile`` input."""

        return () if self.application is None else (self.application,)


@dataclass(frozen=True)
class MonitorCommandResult:
    """Small command result used by the injectable platform probes."""

    returncode: int
    stdout: bytes
    stderr: bytes


class _CommandTimedOut(RuntimeError):
    pass


class _CommandOutputLimit(RuntimeError):
    pass


CommandRunner = Callable[[Sequence[str], float, int], MonitorCommandResult]


def _windows_foreground_executable() -> str | None:
    """Return the executable owning the current foreground HWND."""
    import ctypes
    from ctypes import wintypes
    import psutil

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    window = user32.GetForegroundWindow()
    if not window:
        return None
    pid = wintypes.DWORD()
    if not user32.GetWindowThreadProcessId(window, ctypes.byref(pid)) or not pid.value:
        raise OSError(ctypes.get_last_error(), "Cannot identify the foreground window owner")
    return psutil.Process(pid.value).exe()


class ForegroundApplicationMonitor:
    """Collect one foreground identity without device or coordinator access.

    All host boundaries are injectable.  This keeps platform behavior fully
    deterministic in tests and lets a future coordinator decide how often to
    poll, whether to debounce a result, and when device work is allowed.
    """

    def __init__(
        self,
        *,
        system: str | None = None,
        environment: Mapping[str, str] | None = None,
        find_executable: Callable[[str], str | None] | None = None,
        run_command: CommandRunner | None = None,
        readlink: Callable[[str], str] | None = None,
        windows_foreground: Callable[[], str | None] | None = None,
    ) -> None:
        self._system = platform.system() if system is None else system
        self._environment = os.environ if environment is None else environment
        self._find_executable = shutil.which if find_executable is None else find_executable
        self._run_command = _run_bounded_command if run_command is None else run_command
        self._readlink = os.readlink if readlink is None else readlink
        self._windows_foreground = (
            _windows_foreground_executable
            if windows_foreground is None else windows_foreground
        )

    def poll(self) -> ForegroundApplicationSnapshot:
        """Return one bounded foreground observation for the current platform."""

        if self._system == "Linux":
            return self._poll_linux()
        if self._system == "Darwin":
            return self._poll_macos()
        if self._system == "Windows":
            return self._poll_windows()
        return _snapshot(
            ForegroundApplicationStatus.UNAVAILABLE,
            "Foreground application monitoring supports Linux, macOS and Windows.",
        )

    def _poll_windows(self) -> ForegroundApplicationSnapshot:
        try:
            executable_path = self._windows_foreground()
        except PermissionError:
            return _snapshot(
                ForegroundApplicationStatus.PERMISSION_DENIED,
                "Permission to inspect the foreground application was denied.",
            )
        except Exception:
            return _snapshot(
                ForegroundApplicationStatus.ERROR,
                "The foreground application identity could not be read.",
            )
        if executable_path is None:
            return _no_application()
        identity = _validated_identity(executable_path=executable_path)
        return identity if isinstance(identity, ForegroundApplicationSnapshot) else _available(identity)

    def _poll_linux(self) -> ForegroundApplicationSnapshot:
        session = self._environment.get("XDG_SESSION_TYPE", "").strip().casefold()
        if session == "wayland" or self._environment.get("WAYLAND_DISPLAY", ""):
            return self._poll_wayland()
        if not self._environment.get("DISPLAY", "").strip():
            return _snapshot(
                ForegroundApplicationStatus.UNAVAILABLE,
                "An X11 display is not available.",
            )
        try:
            executable = self._find_executable("xprop")
        except OSError:
            executable = None
        if not executable:
            return _snapshot(
                ForegroundApplicationStatus.UNAVAILABLE,
                "Install xprop to monitor the foreground X11 application.",
            )

        root = self._invoke((executable, "-root", "_NET_ACTIVE_WINDOW"))
        if isinstance(root, ForegroundApplicationSnapshot):
            return root
        if root.returncode:
            return _command_failure(root)
        root_text = _decode(root.stdout)
        if root_text is None:
            return _malformed_response()
        if "no such atom" in root_text.casefold():
            return _snapshot(
                ForegroundApplicationStatus.UNAVAILABLE,
                "The X11 window manager does not expose an active window.",
            )
        active = _ACTIVE_WINDOW.fullmatch(root_text)
        if active is None:
            return _malformed_response()
        window_id = active.group(1).casefold()
        if int(window_id, 16) == 0:
            return _no_application()

        owner = self._invoke((executable, "-id", window_id, "_NET_WM_PID"))
        if isinstance(owner, ForegroundApplicationSnapshot):
            return owner
        if owner.returncode:
            if (
                b"no such atom" in owner.stderr.lower()
                or b"not found" in owner.stderr.lower()
            ):
                return _snapshot(
                    ForegroundApplicationStatus.UNAVAILABLE,
                    "The foreground X11 window does not expose a process identity.",
                )
            return _command_failure(owner)
        owner_text = _decode(owner.stdout)
        if owner_text is None:
            return _malformed_response()
        if "no such atom" in owner_text.casefold():
            return _snapshot(
                ForegroundApplicationStatus.UNAVAILABLE,
                "The foreground X11 window does not expose a process identity.",
            )
        pid_match = _WINDOW_PID.fullmatch(owner_text)
        if pid_match is None:
            return _malformed_response()
        pid = int(pid_match.group(1))
        if pid > _MAX_PID:
            return _malformed_response()
        return self._resolve_linux_pid(pid)

    def _poll_wayland(self) -> ForegroundApplicationSnapshot:
        desktop_tokens = _desktop_tokens(self._environment)
        if _is_gnome_desktop(self._environment):
            return self._poll_gnome_wayland()
        if "sway" in desktop_tokens:
            return self._poll_sway_wayland()
        if "hyprland" in desktop_tokens:
            return self._poll_hyprland_wayland()
        if self._environment.get("SWAYSOCK", "").strip():
            return self._poll_sway_wayland()
        if self._environment.get("HYPRLAND_INSTANCE_SIGNATURE", "").strip():
            return self._poll_hyprland_wayland()
        return _snapshot(
            ForegroundApplicationStatus.UNAVAILABLE,
            "Foreground application monitoring is unavailable for this Wayland desktop.",
        )

    def _poll_gnome_wayland(self) -> ForegroundApplicationSnapshot:
        try:
            executable = self._find_executable("gdbus")
        except OSError:
            executable = None
        if not executable:
            return _snapshot(
                ForegroundApplicationStatus.UNAVAILABLE,
                "Install the GLib gdbus tool to use automatic profiles on GNOME Wayland.",
            )

        result = self._invoke(
            (
                executable,
                "call",
                "--session",
                "--dest",
                GNOME_DBUS_NAME,
                "--object-path",
                GNOME_DBUS_OBJECT_PATH,
                "--method",
                f"{GNOME_DBUS_INTERFACE}.GetFocusedWindowPid",
            )
        )
        if isinstance(result, ForegroundApplicationSnapshot):
            return result
        if result.returncode:
            details = (result.stdout + b"\n" + result.stderr).decode(
                "utf-8", errors="replace"
            ).casefold()
            if any(marker in details for marker in _GNOME_EXTENSION_ABSENT_MARKERS):
                return _gnome_extension_unavailable()
            if any(
                marker in details for marker in _GNOME_EXTENSION_INCOMPATIBLE_MARKERS
            ):
                return _snapshot(
                    ForegroundApplicationStatus.UNAVAILABLE,
                    "Reinstall and re-enable the Swarm 2 GNOME Shell extension; "
                    "its D-Bus interface is incompatible.",
                )
            if any(marker in details for marker in _PERMISSION_MARKERS):
                return _snapshot(
                    ForegroundApplicationStatus.PERMISSION_DENIED,
                    "Permission to query the Swarm 2 GNOME Shell extension was denied.",
                )
            return _snapshot(
                ForegroundApplicationStatus.ERROR,
                "The Swarm 2 GNOME Shell extension query failed.",
            )

        response = _decode(result.stdout)
        if response is None:
            return _malformed_response()
        match = _GNOME_FOCUSED_PID.fullmatch(response)
        if match is None:
            return _malformed_response()
        has_focused_window = match.group(1) == "true"
        pid = int(match.group(2))
        if not has_focused_window:
            return _no_application() if pid == 0 else _malformed_response()
        if pid == 0:
            return _snapshot(
                ForegroundApplicationStatus.ERROR,
                "The focused GNOME Wayland window has no process identity.",
            )
        if pid > _MAX_PID:
            return _malformed_response()
        return self._resolve_linux_pid(pid)

    def _poll_sway_wayland(self) -> ForegroundApplicationSnapshot:
        try:
            executable = self._find_executable("swaymsg")
        except OSError:
            executable = None
        if not executable:
            return _snapshot(
                ForegroundApplicationStatus.UNAVAILABLE,
                "Install swaymsg to use automatic profiles on Sway.",
            )

        result = self._invoke(
            (executable, "--raw", "-t", "get_tree"),
            maximum=MAX_SWAY_TREE_OUTPUT_BYTES,
        )
        if isinstance(result, ForegroundApplicationSnapshot):
            return result
        if result.returncode:
            return _wayland_command_failure(result, "Sway")
        value = _load_strict_json(result.stdout)
        if isinstance(value, ForegroundApplicationSnapshot):
            return value
        focused = _sway_focused_pid(value)
        if isinstance(focused, ForegroundApplicationSnapshot):
            return focused
        has_focused_window, pid = focused
        if not has_focused_window:
            return _no_application()
        if pid is None:
            return _snapshot(
                ForegroundApplicationStatus.ERROR,
                "The focused Sway window has no process identity.",
            )
        return self._resolve_linux_pid(pid)

    def _poll_hyprland_wayland(self) -> ForegroundApplicationSnapshot:
        try:
            executable = self._find_executable("hyprctl")
        except OSError:
            executable = None
        if not executable:
            return _snapshot(
                ForegroundApplicationStatus.UNAVAILABLE,
                "Install hyprctl to use automatic profiles on Hyprland.",
            )

        result = self._invoke((executable, "-j", "activewindow"))
        if isinstance(result, ForegroundApplicationSnapshot):
            return result
        if result.returncode:
            return _wayland_command_failure(result, "Hyprland")
        value = _load_strict_json(result.stdout)
        if isinstance(value, ForegroundApplicationSnapshot):
            return value
        if type(value) is not dict:
            return _malformed_response()
        if not value:
            return _no_application()
        if "pid" not in value:
            return _snapshot(
                ForegroundApplicationStatus.ERROR,
                "The focused Hyprland window has no process identity.",
            )
        pid = value["pid"]
        if type(pid) is not int or not 0 < pid <= _MAX_PID:
            return _malformed_response()
        return self._resolve_linux_pid(pid)

    def _resolve_linux_pid(self, pid: int) -> ForegroundApplicationSnapshot:
        try:
            executable_path = self._readlink(f"/proc/{pid}/exe")
        except (FileNotFoundError, ProcessLookupError):
            return _snapshot(
                ForegroundApplicationStatus.ERROR,
                "The foreground application closed before its identity was read.",
            )
        except PermissionError:
            return _snapshot(
                ForegroundApplicationStatus.PERMISSION_DENIED,
                "Permission to inspect the foreground application was denied.",
            )
        except OSError:
            return _snapshot(
                ForegroundApplicationStatus.ERROR,
                "The foreground application identity could not be read.",
            )
        identity = _validated_identity(executable_path=executable_path)
        return (
            identity
            if isinstance(identity, ForegroundApplicationSnapshot)
            else _available(identity)
        )

    def _poll_macos(self) -> ForegroundApplicationSnapshot:
        try:
            executable = self._find_executable("osascript")
        except OSError:
            executable = None
        if not executable:
            return _snapshot(
                ForegroundApplicationStatus.UNAVAILABLE,
                "The macOS application monitor is unavailable.",
            )
        result = self._invoke(
            (
                executable,
                "-l",
                "JavaScript",
                "-e",
                _MACOS_FRONTMOST_APPLICATION_SCRIPT,
            )
        )
        if isinstance(result, ForegroundApplicationSnapshot):
            return result
        if result.returncode:
            return _command_failure(result)
        try:
            value = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError, RecursionError):
            return _malformed_response()
        if not isinstance(value, dict) or set(value) != {"bundle_id", "executable_path"}:
            return _malformed_response()
        bundle_id = value["bundle_id"]
        executable_path = value["executable_path"]
        if bundle_id is None and executable_path is None:
            return _no_application()
        if bundle_id is not None and not isinstance(bundle_id, str):
            return _malformed_response()
        if executable_path is not None and not isinstance(executable_path, str):
            return _malformed_response()
        identity = _validated_identity(
            executable_path=executable_path,
            bundle_id=bundle_id,
        )
        return (
            identity
            if isinstance(identity, ForegroundApplicationSnapshot)
            else _available(identity)
        )

    def _invoke(
        self,
        arguments: Sequence[str],
        *,
        maximum: int = MAX_COMMAND_OUTPUT_BYTES,
    ) -> MonitorCommandResult | ForegroundApplicationSnapshot:
        try:
            result = self._run_command(arguments, COMMAND_TIMEOUT_SECONDS, maximum)
        except (_CommandTimedOut, subprocess.TimeoutExpired):
            return _snapshot(
                ForegroundApplicationStatus.ERROR,
                "Foreground application monitoring timed out.",
            )
        except _CommandOutputLimit:
            return _snapshot(
                ForegroundApplicationStatus.ERROR,
                "The foreground application response exceeded its size limit.",
            )
        except (OSError, subprocess.SubprocessError):
            return _snapshot(
                ForegroundApplicationStatus.UNAVAILABLE,
                "The foreground application monitor could not be started.",
            )
        if type(result) is not MonitorCommandResult:
            return _malformed_response()
        if (
            type(result.returncode) is not int
            or not isinstance(result.stdout, bytes)
            or not isinstance(result.stderr, bytes)
        ):
            return _malformed_response()
        if len(result.stdout) > maximum or len(result.stderr) > maximum:
            return _snapshot(
                ForegroundApplicationStatus.ERROR,
                "The foreground application response exceeded its size limit.",
            )
        return result


def _validated_identity(
    *, executable_path: str | None = None, bundle_id: str | None = None
) -> ApplicationIdentity | ForegroundApplicationSnapshot:
    try:
        return ApplicationIdentity(
            executable_path=executable_path,
            bundle_id=bundle_id,
        ).normalized()
    except AutomaticProfileError:
        return _malformed_response()


def _available(identity: ApplicationIdentity) -> ForegroundApplicationSnapshot:
    return ForegroundApplicationSnapshot(
        status=ForegroundApplicationStatus.AVAILABLE,
        application=identity,
    )


def _no_application() -> ForegroundApplicationSnapshot:
    return _snapshot(
        ForegroundApplicationStatus.NO_APPLICATION,
        "No identifiable foreground application is active.",
    )


def _gnome_extension_unavailable() -> ForegroundApplicationSnapshot:
    return _snapshot(
        ForegroundApplicationStatus.UNAVAILABLE,
        "Open Device > Host integration in MC7 Studio, or run "
        "'swarm2-gnome-extension install'; log out and back in, then enable "
        f"{GNOME_EXTENSION_UUID} to use automatic profiles on GNOME Wayland.",
    )


def _is_gnome_desktop(environment: Mapping[str, str]) -> bool:
    return any(
        token == "gnome" or token.startswith("gnome-")
        for token in _desktop_tokens(environment)
    )


def _desktop_tokens(environment: Mapping[str, str]) -> frozenset[str]:
    desktop = ":".join(
        environment.get(name, "")
        for name in ("XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP")
    )
    return frozenset(
        token.casefold()
        for token in re.split(r"[:;,\s]+", desktop)
        if token
    )


def _load_strict_json(
    raw: bytes,
) -> object | ForegroundApplicationSnapshot:
    def object_from_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate JSON object key.")
            value[key] = item
        return value

    def reject_constant(_value: str) -> object:
        raise ValueError("Non-finite JSON number.")

    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=object_from_pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError):
        return _malformed_response()


def _sway_focused_pid(
    tree: object,
) -> tuple[bool, int | None] | ForegroundApplicationSnapshot:
    if type(tree) is not dict:
        return _malformed_response()

    nodes: list[object] = [tree]
    focused_pid: int | None = None
    focused_count = 0
    visited = 0
    while nodes:
        node = nodes.pop()
        visited += 1
        if visited > MAX_SWAY_TREE_NODES or type(node) is not dict:
            return _malformed_response()

        focused = node.get("focused")
        if type(focused) is not bool:
            return _malformed_response()
        pid = node.get("pid")
        if pid is not None and (type(pid) is not int or not 0 < pid <= _MAX_PID):
            return _malformed_response()

        if focused:
            focused_count += 1
            if focused_count > 1:
                return _malformed_response()
            focused_pid = pid

        for key in ("nodes", "floating_nodes"):
            children = node.get(key, [])
            if type(children) is not list:
                return _malformed_response()
            nodes.extend(children)

    return focused_count == 1, focused_pid


def _malformed_response() -> ForegroundApplicationSnapshot:
    return _snapshot(
        ForegroundApplicationStatus.ERROR,
        "The foreground application monitor returned an invalid response.",
    )


def _command_failure(result: MonitorCommandResult) -> ForegroundApplicationSnapshot:
    details = (result.stdout + b"\n" + result.stderr).decode(
        "utf-8", errors="replace"
    ).casefold()
    if any(marker in details for marker in _PERMISSION_MARKERS):
        return _snapshot(
            ForegroundApplicationStatus.PERMISSION_DENIED,
            "Permission to inspect the foreground application was denied.",
        )
    if any(marker in details for marker in _UNAVAILABLE_MARKERS):
        return _snapshot(
            ForegroundApplicationStatus.UNAVAILABLE,
            "The configured display is unavailable.",
        )
    return _snapshot(
        ForegroundApplicationStatus.ERROR,
        "The foreground application monitor failed.",
    )


def _wayland_command_failure(
    result: MonitorCommandResult, compositor: str
) -> ForegroundApplicationSnapshot:
    details = (result.stdout + b"\n" + result.stderr).decode(
        "utf-8", errors="replace"
    ).casefold()
    if any(marker in details for marker in _PERMISSION_MARKERS):
        return _snapshot(
            ForegroundApplicationStatus.PERMISSION_DENIED,
            f"Permission to query {compositor} for the foreground application was denied.",
        )
    if any(marker in details for marker in _WAYLAND_IPC_UNAVAILABLE_MARKERS):
        return _snapshot(
            ForegroundApplicationStatus.UNAVAILABLE,
            f"The {compositor} compositor IPC service is unavailable.",
        )
    return _snapshot(
        ForegroundApplicationStatus.ERROR,
        f"The {compositor} foreground application query failed.",
    )


def _snapshot(
    status: ForegroundApplicationStatus, message: str
) -> ForegroundApplicationSnapshot:
    return ForegroundApplicationSnapshot(status=status, message=message)


def _decode(raw: bytes) -> str | None:
    try:
        return raw.decode("ascii")
    except UnicodeError:
        return None


def _run_bounded_command(
    arguments: Sequence[str], timeout: float, maximum: int
) -> MonitorCommandResult:
    """Run a fixed probe with hard time and per-stream byte limits."""

    if (
        not isinstance(arguments, (tuple, list))
        or not arguments
        or any(not isinstance(argument, str) or not argument for argument in arguments)
        or not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or timeout <= 0
        or type(maximum) is not int
        or maximum <= 0
    ):
        raise ValueError("A bounded command needs valid arguments and limits.")
    environment = host_command_environment({"LC_ALL": "C", "LANG": "C"})
    try:
        process = subprocess.Popen(
            list(arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            close_fds=True,
        )
    except OSError:
        raise
    output, errors = bytearray(), bytearray()
    deadline = time.monotonic() + float(timeout)
    try:
        if process.stdout is None or process.stderr is None:
            raise OSError("The foreground monitor pipes are unavailable.")
        with pipe_selector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ, (output, maximum))
            ready.register(process.stderr, selectors.EVENT_READ, (errors, maximum))
            while ready.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _CommandTimedOut
                for key, _ in ready.select(min(remaining, 0.05)):
                    buffer, limit = key.data
                    chunk = os.read(
                        key.fileobj.fileno(), min(4096, limit - len(buffer) + 1)
                    )
                    if not chunk:
                        ready.unregister(key.fileobj)
                    else:
                        buffer.extend(chunk)
                        if len(buffer) > limit:
                            raise _CommandOutputLimit
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _CommandTimedOut
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise _CommandTimedOut from exc
        return MonitorCommandResult(returncode, bytes(output), bytes(errors))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
