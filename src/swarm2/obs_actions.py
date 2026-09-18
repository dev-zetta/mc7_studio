"""Bounded host actions for source-mapped MC7 OBS tiles."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os
import posixpath
import ntpath
import platform
import shutil
import subprocess

from .runtime import host_command_environment
from .transport import DeviceError


OBS_FLATPAK_APP_ID = "com.obsproject.Studio"
OBS_FLATPAK_PROBE_TIMEOUT_SECONDS = 2.0
OBS_FLATPAK_LAUNCH_GRACE_SECONDS = 1.0
_OBS_UNAVAILABLE = (
    "OBS Studio is unavailable: no native obs executable or installed "
    f"Flatpak {OBS_FLATPAK_APP_ID} was found"
)
_OBS_FLATPAK_CHECK_FAILED = (
    "Could not check the OBS Studio Flatpak installation"
)


@dataclass(frozen=True)
class ObsLaunchBinding:
    """One stored ``4B/00`` tile that may launch OBS on its host."""

    page_index: int
    slot_index: int

    def __post_init__(self) -> None:
        if type(self.page_index) is not int or not 0 <= self.page_index <= 2:
            raise ValueError("Launch OBS page must be 0..2")
        if type(self.slot_index) is not int or not 0 <= self.slot_index <= 3:
            raise ValueError("Launch OBS slot must be 0..3")


@dataclass(frozen=True)
class ObsScreenshotBinding:
    """One stored ``43/07`` tile that may request an OBS screenshot."""

    page_index: int
    slot_index: int

    def __post_init__(self) -> None:
        if type(self.page_index) is not int or not 0 <= self.page_index <= 2:
            raise ValueError("OBS Screenshot page must be 0..2")
        if type(self.slot_index) is not int or not 0 <= self.slot_index <= 3:
            raise ValueError("OBS Screenshot slot must be 0..3")


@dataclass(frozen=True)
class ObsStudioModeBinding:
    """One stored ``43/09`` tile that may toggle OBS Studio Mode."""

    page_index: int
    slot_index: int

    def __post_init__(self) -> None:
        if type(self.page_index) is not int or not 0 <= self.page_index <= 2:
            raise ValueError("OBS Studio Mode page must be 0..2")
        if type(self.slot_index) is not int or not 0 <= self.slot_index <= 3:
            raise ValueError("OBS Studio Mode slot must be 0..3")


def _absolute_executable(
    name: str, find_executable: Callable[[str], str | None]
) -> str | None:
    try:
        executable = find_executable(name)
    except (OSError, ValueError):
        return None
    if (
        not isinstance(executable, str)
        or not executable
        or not posixpath.isabs(executable)
        or len(os.fsencode(executable)) > 4096
        or any(ord(character) < 0x20 for character in executable)
    ):
        return None
    return executable


def _absolute_windows_executable(
    name: str, find_executable: Callable[[str], str | None]
) -> str | None:
    try:
        executable = find_executable(name)
    except (OSError, ValueError):
        return None
    if (
        not isinstance(executable, str)
        or not executable
        or not ntpath.isabs(executable)
        or len(os.fsencode(executable)) > 4096
        or any(ord(character) < 0x20 for character in executable)
    ):
        return None
    return executable


def _flatpak_obs_is_installed(
    flatpak: str, probe_runner: Callable[..., object]
) -> bool:
    """Check the exact official app ID without buffering command output."""

    try:
        result = probe_runner(
            (flatpak, "info", "--show-ref", OBS_FLATPAK_APP_ID),
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            timeout=OBS_FLATPAK_PROBE_TIMEOUT_SECONDS,
            check=False,
            env=host_command_environment(),
        )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise DeviceError(_OBS_FLATPAK_CHECK_FAILED) from error
    returncode = getattr(result, "returncode", None)
    if type(returncode) is not int:
        raise DeviceError(_OBS_FLATPAK_CHECK_FAILED)
    return returncode == 0


def build_obs_launch_argv(
    *,
    system: str | None = None,
    find_executable: Callable[[str], str | None] = shutil.which,
    probe_runner: Callable[..., object] = subprocess.run,
) -> tuple[str, ...]:
    """Resolve a fixed argument vector without starting OBS or using a shell."""

    detected = platform.system() if system is None else system
    if not isinstance(detected, str):
        raise DeviceError("Host platform name is invalid")
    if detected == "Darwin":
        return "/usr/bin/open", "-a", "OBS"
    if detected == "Windows":
        executable = _absolute_windows_executable("obs64.exe", find_executable)
        if executable is None:
            executable = _absolute_windows_executable("obs32.exe", find_executable)
        if executable is not None:
            return (executable,)
        candidates = []
        for variable in ("ProgramFiles", "ProgramFiles(x86)"):
            root = os.environ.get(variable)
            if root:
                candidates.append(os.path.join(root, "obs-studio", "bin", "64bit", "obs64.exe"))
        for candidate in candidates:
            if os.path.isfile(candidate):
                return (os.path.abspath(candidate),)
        raise DeviceError(_OBS_UNAVAILABLE)
    if detected != "Linux":
        raise DeviceError("Launch OBS requires Linux, macOS or Windows")
    executable = _absolute_executable("obs", find_executable)
    if executable is not None:
        return (executable,)
    flatpak = _absolute_executable("flatpak", find_executable)
    if flatpak is not None and _flatpak_obs_is_installed(flatpak, probe_runner):
        return flatpak, "run", OBS_FLATPAK_APP_ID
    raise DeviceError(_OBS_UNAVAILABLE)


def execute_obs_launch(
    binding: ObsLaunchBinding,
    *,
    system: str | None = None,
    find_executable: Callable[[str], str | None] = shutil.which,
    probe_runner: Callable[..., object] = subprocess.run,
    process_factory=subprocess.Popen,
):
    """Start OBS through one fixed argv vector and report process creation."""

    if not isinstance(binding, ObsLaunchBinding):
        raise DeviceError("Launch OBS requires a validated LCD binding")
    binding.__post_init__()
    argv = build_obs_launch_argv(
        system=system,
        find_executable=find_executable,
        probe_runner=probe_runner,
    )
    try:
        process = process_factory(
            argv,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
            env=host_command_environment(),
        )
    except (OSError, ValueError) as error:
        raise DeviceError("OBS Studio could not be opened") from error
    if len(argv) != 3 or argv[1:] != ("run", OBS_FLATPAK_APP_ID):
        return process
    try:
        returncode = process.wait(timeout=OBS_FLATPAK_LAUNCH_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        return process
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise DeviceError("OBS Studio launch status could not be checked") from error
    if type(returncode) is not int or returncode != 0:
        raise DeviceError("OBS Studio could not be opened through Flatpak")
    return process


def execute_obs_screenshot(
    binding: ObsScreenshotBinding,
    *,
    request_executor=None,
):
    """Trigger the source-mapped screenshot request for one validated tile."""

    if not isinstance(binding, ObsScreenshotBinding):
        raise DeviceError("OBS Screenshot requires a validated LCD binding")
    binding.__post_init__()
    if request_executor is None:
        from .obs_websocket import execute_obs_screenshot_request

        request_executor = execute_obs_screenshot_request
    if not callable(request_executor):
        raise DeviceError("OBS Screenshot request provider is invalid")
    return request_executor()


def execute_obs_studio_mode(
    binding: ObsStudioModeBinding,
    *,
    request_executor=None,
):
    """Toggle and verify Studio Mode for one validated tile."""

    if not isinstance(binding, ObsStudioModeBinding):
        raise DeviceError("OBS Studio Mode requires a validated LCD binding")
    binding.__post_init__()
    if request_executor is None:
        from .obs_websocket import execute_obs_studio_mode_toggle_request

        request_executor = execute_obs_studio_mode_toggle_request
    if not callable(request_executor):
        raise DeviceError("OBS Studio Mode request provider is invalid")
    result = request_executor()
    from .obs_websocket import ObsStudioModeToggleResult

    if not isinstance(result, ObsStudioModeToggleResult):
        raise DeviceError("OBS Studio Mode request provider returned invalid data")
    try:
        result.__post_init__()
    except ValueError as error:
        raise DeviceError(
            "OBS Studio Mode request provider returned invalid data"
        ) from error
    return result
