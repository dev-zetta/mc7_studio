"""Checks and explicit installers for optional desktop integration.

The application never installs an integration during discovery or device
access.  Start-at-login and GNOME assets are per-user.  Installing the udev
rule is a separate, explicit operation using fixed executable paths and argv;
no command is interpreted by a shell.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from importlib import resources
import locale
import os
from pathlib import Path
import selectors
from .pipe_io import pipe_selector
import signal
import subprocess
import sys
import tempfile
import time
from typing import Callable, Mapping, Sequence

from .autostart import AutostartManager, AutostartStatus
from .gnome_extension import (
    GNOME_EXTENSION_UUID,
    GnomeExtensionInstallError,
    bundled_extension_assets,
    extension_destination,
    install_extension,
)
from .runtime import host_command_environment


UDEV_RULE_NAME = "70-swarm2-mc7.rules"
UDEV_RULE_DESTINATION = Path("/etc/udev/rules.d") / UDEV_RULE_NAME

_UDEV_RULE = b"""# Allow the active local desktop user to access MC7 HID interfaces.
# Install before 73-seat-late.rules, then reconnect the mouse and receiver.
SUBSYSTEM==\"hidraw\", ATTRS{idVendor}==\"10f5\", ATTRS{idProduct}==\"502c\", TAG+=\"uaccess\"
SUBSYSTEM==\"hidraw\", ATTRS{idVendor}==\"10f5\", ATTRS{idProduct}==\"502e\", TAG+=\"uaccess\"
# Firmware control transfers require USB access to the directly wired mouse.
SUBSYSTEM==\"usb\", ENV{DEVTYPE}==\"usb_device\", ATTR{idVendor}==\"10f5\", ATTR{idProduct}==\"502c\", TAG+=\"uaccess\"
"""

_COMMAND_CANDIDATES = {
    "gnome-extensions": (
        "/usr/bin/gnome-extensions",
        "/bin/gnome-extensions",
    ),
    "pkexec": ("/usr/bin/pkexec", "/bin/pkexec"),
    "install": ("/usr/bin/install", "/bin/install"),
    "udevadm": (
        "/usr/bin/udevadm",
        "/bin/udevadm",
        "/usr/sbin/udevadm",
        "/sbin/udevadm",
    ),
}
_MAX_COMMAND_OUTPUT = 16 * 1024


class _CommandOutputLimit(RuntimeError):
    pass


class IntegrationState(str, Enum):
    """Stable state names consumed by the GUI and tests."""

    NOT_APPLICABLE = "not_applicable"
    MISSING = "missing"
    CURRENT = "current"
    NEEDS_UPDATE = "needs_update"
    ERROR = "error"


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class GnomeExtensionStatus:
    state: IntegrationState
    detail: str
    destination: str | None
    installed: bool
    current: bool
    known_to_shell: bool | None
    enabled: bool | None
    can_install: bool
    can_enable: bool


@dataclass(frozen=True)
class UdevRuleStatus:
    state: IntegrationState
    detail: str
    path: str | None
    installed: bool
    current: bool
    can_install: bool


@dataclass(frozen=True)
class HostIntegrationSnapshot:
    autostart: AutostartStatus
    gnome: GnomeExtensionStatus
    udev: UdevRuleStatus


class HostIntegrationError(RuntimeError):
    """An explicit host integration operation could not be completed."""


CommandRunner = Callable[[Sequence[str], float], CommandResult]


def bundled_udev_rule() -> bytes:
    """Return the fixed wheel-contained MC7 access rule after validation."""

    try:
        value = (
            resources.files("swarm2")
            .joinpath("data")
            .joinpath("udev")
            .joinpath(UDEV_RULE_NAME)
            .read_bytes()
        )
    except (OSError, FileNotFoundError) as exc:
        raise HostIntegrationError(
            "The bundled MC7 udev rule is missing from this installation."
        ) from exc
    if value != _UDEV_RULE:
        raise HostIntegrationError(
            "The bundled MC7 udev rule does not match the application."
        )
    return value


def _default_runner(argv: Sequence[str], timeout: float) -> CommandResult:
    command_name = Path(argv[0]).name
    try:
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=host_command_environment(),
            close_fds=True,
            shell=False,
            start_new_session=os.name == "posix",
        )
    except OSError as exc:
        raise HostIntegrationError(
            f"Could not start {command_name}."
        ) from exc

    output = bytearray()
    errors = bytearray()
    deadline = time.monotonic() + timeout
    completed = False
    try:
        if process.stdout is None or process.stderr is None:
            raise OSError("Host integration command pipes are unavailable.")
        with pipe_selector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ, output)
            ready.register(process.stderr, selectors.EVENT_READ, errors)
            while ready.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)
                for key, _ in ready.select(min(remaining, 0.05)):
                    buffer = key.data
                    available = _MAX_COMMAND_OUTPUT - len(buffer)
                    chunk = os.read(key.fileobj.fileno(), min(4096, available + 1))
                    if not chunk:
                        ready.unregister(key.fileobj)
                        continue
                    if len(chunk) > available:
                        raise _CommandOutputLimit
                    buffer.extend(chunk)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(argv, timeout)
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise subprocess.TimeoutExpired(argv, timeout) from exc
        completed = True
    except subprocess.TimeoutExpired as exc:
        raise HostIntegrationError(
            f"{command_name} did not finish within {timeout:g} seconds."
        ) from exc
    except _CommandOutputLimit as exc:
        raise HostIntegrationError(
            f"{command_name} produced more than {_MAX_COMMAND_OUTPUT} bytes "
            "on one output stream."
        ) from exc
    except OSError as exc:
        raise HostIntegrationError(
            f"Could not run {command_name}."
        ) from exc
    finally:
        if not completed:
            _stop_command(process)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()

    encoding = locale.getpreferredencoding(False)
    return CommandResult(
        returncode,
        bytes(output).decode(encoding, errors="replace"),
        bytes(errors).decode(encoding, errors="replace"),
    )


def _stop_command(process: subprocess.Popen[bytes]) -> None:
    """Stop a failed bounded command and any descendants sharing its group."""

    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=1.0)
    except (OSError, subprocess.TimeoutExpired):
        pass


class HostIntegrationManager:
    """Read host state and perform only user-requested installation steps.

    Constructor overrides are dependency-injection seams for offline tests and
    packagers.  The desktop application uses the fixed defaults.
    """

    def __init__(
        self,
        *,
        platform: str | None = None,
        environ: Mapping[str, str] | None = None,
        data_home: str | Path | None = None,
        udev_paths: Sequence[str | Path] | None = None,
        commands: Mapping[str, str | Path | None] | None = None,
        runner: CommandRunner | None = None,
        temp_directory: str | Path | None = None,
        autostart_manager: AutostartManager | None = None,
    ):
        self.platform = sys.platform if platform is None else platform
        self.environ = dict(os.environ if environ is None else environ)
        self.data_home = data_home
        self.udev_paths = tuple(
            Path(path)
            for path in (
                udev_paths
                if udev_paths is not None
                else (
                    UDEV_RULE_DESTINATION,
                    Path("/run/udev/rules.d") / UDEV_RULE_NAME,
                    Path("/usr/local/lib/udev/rules.d") / UDEV_RULE_NAME,
                    Path("/usr/lib/udev/rules.d") / UDEV_RULE_NAME,
                    Path("/lib/udev/rules.d") / UDEV_RULE_NAME,
                )
            )
        )
        self.commands = dict(commands or {})
        self.runner = runner or _default_runner
        self.temp_directory = (
            None if temp_directory is None else Path(temp_directory)
        )
        self.autostart_manager = (
            autostart_manager
            if autostart_manager is not None
            else AutostartManager(platform=self.platform, environ=self.environ)
        )

    def check_all(self) -> HostIntegrationSnapshot:
        return HostIntegrationSnapshot(
            autostart=self.check_autostart(),
            gnome=self.check_gnome_extension(),
            udev=self.check_udev_rule(),
        )

    def check_autostart(self) -> AutostartStatus:
        return self.autostart_manager.check()

    def install_autostart(self) -> HostIntegrationSnapshot:
        self.autostart_manager.install()
        return self.check_all()

    def remove_autostart(self) -> HostIntegrationSnapshot:
        self.autostart_manager.remove()
        return self.check_all()

    def _is_linux(self) -> bool:
        return self.platform.startswith("linux")

    def _is_gnome_wayland(self) -> bool:
        desktop = ":".join(
            self.environ.get(name, "")
            for name in ("XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP")
        )
        desktops = tuple(
            token.strip().upper()
            for token in desktop.replace(";", ":").replace(",", ":").split(":")
            if token.strip()
        )
        wayland = (
            self.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
            or bool(self.environ.get("WAYLAND_DISPLAY", ""))
        )
        return (
            self._is_linux()
            and wayland
            and any(value == "GNOME" or value.startswith("GNOME-") for value in desktops)
        )

    def _command(self, name: str) -> str | None:
        if name in self.commands:
            configured = self.commands[name]
            return None if configured is None else str(Path(configured))
        for candidate in _COMMAND_CANDIDATES[name]:
            path = Path(candidate)
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
        return None

    def _run(self, argv: Sequence[str], timeout: float) -> CommandResult:
        if not argv or not Path(argv[0]).is_absolute():
            raise HostIntegrationError("Host integration commands require an absolute path.")
        return self.runner(tuple(argv), timeout)

    def check_gnome_extension(self) -> GnomeExtensionStatus:
        if not self._is_gnome_wayland():
            return GnomeExtensionStatus(
                IntegrationState.NOT_APPLICABLE,
                "The companion is needed only for automatic profiles in a GNOME Wayland session.",
                None,
                False,
                False,
                None,
                None,
                False,
                False,
            )

        try:
            destination = extension_destination(self.data_home)
            assets = bundled_extension_assets()
        except (OSError, ValueError, GnomeExtensionInstallError) as exc:
            return GnomeExtensionStatus(
                IntegrationState.ERROR,
                str(exc),
                None,
                False,
                False,
                None,
                None,
                False,
                False,
            )

        installed = destination.exists() or destination.is_symlink()
        current = False
        unsafe = destination.is_symlink() or (installed and not destination.is_dir())
        if installed and not unsafe and destination.is_dir():
            try:
                current = all(
                    not (destination / name).is_symlink()
                    and (destination / name).is_file()
                    and (destination / name).read_bytes() == value
                    for name, value in assets.items()
                )
            except OSError:
                current = False

        known: bool | None = None
        enabled: bool | None = None
        command = self._command("gnome-extensions")
        if command is not None:
            try:
                listed = self._run((command, "list"), 3.0)
                enabled_list = self._run((command, "list", "--enabled"), 3.0)
                if listed.returncode == 0 and enabled_list.returncode == 0:
                    known = GNOME_EXTENSION_UUID in set(listed.stdout.splitlines())
                    enabled = GNOME_EXTENSION_UUID in set(
                        enabled_list.stdout.splitlines()
                    )
            except HostIntegrationError:
                known = enabled = None

        destination_text = str(destination)
        if unsafe:
            state = IntegrationState.ERROR
            detail = "The extension path is a link or is not a directory; remove it manually before installing."
        elif not installed:
            state = IntegrationState.MISSING
            detail = "The GNOME Wayland companion is not installed for this user."
        elif not current:
            state = IntegrationState.NEEDS_UPDATE
            detail = "The installed GNOME Wayland companion differs from this application."
        elif known is False:
            state = IntegrationState.CURRENT
            detail = "Installed and current. Log out and back in so GNOME Shell can load it."
        elif enabled is False:
            state = IntegrationState.CURRENT
            detail = "Installed and current. Enable it to use automatic profiles on GNOME Wayland."
        elif enabled is True:
            state = IntegrationState.CURRENT
            detail = "Installed, current and enabled for this GNOME session."
        else:
            state = IntegrationState.CURRENT
            detail = "Installed and current. GNOME Shell enablement could not be checked."

        return GnomeExtensionStatus(
            state,
            detail,
            destination_text,
            installed,
            current,
            known,
            enabled,
            state is not IntegrationState.ERROR,
            bool(current and known and enabled is False and command),
        )

    def install_gnome_extension(self) -> HostIntegrationSnapshot:
        if not self._is_gnome_wayland():
            raise HostIntegrationError(
                "The GNOME companion can be installed only from a GNOME Wayland session."
            )
        try:
            install_extension(self.data_home)
        except (OSError, ValueError, GnomeExtensionInstallError) as exc:
            raise HostIntegrationError(str(exc)) from exc
        return self.check_all()

    def enable_gnome_extension(self) -> HostIntegrationSnapshot:
        status = self.check_gnome_extension()
        if not status.current:
            raise HostIntegrationError(
                "Install the current GNOME companion before enabling it."
            )
        if status.known_to_shell is not True:
            raise HostIntegrationError(
                "Log out and back in before enabling the newly installed companion."
            )
        command = self._command("gnome-extensions")
        if command is None:
            raise HostIntegrationError("gnome-extensions is unavailable on this system.")
        result = self._run((command, "enable", GNOME_EXTENSION_UUID), 10.0)
        if result.returncode != 0:
            raise HostIntegrationError(
                _command_failure("GNOME Shell could not enable the companion", result)
            )
        refreshed = self.check_all()
        if refreshed.gnome.enabled is not True:
            raise HostIntegrationError(
                "GNOME Shell did not report the companion as enabled."
            )
        return refreshed

    def check_udev_rule(self) -> UdevRuleStatus:
        if not self._is_linux():
            return UdevRuleStatus(
                IntegrationState.NOT_APPLICABLE,
                "udev device-access rules apply only on Linux.",
                None,
                False,
                False,
                False,
            )
        try:
            expected = bundled_udev_rule()
        except HostIntegrationError as exc:
            return UdevRuleStatus(
                IntegrationState.ERROR,
                str(exc),
                None,
                False,
                False,
                False,
            )

        # Paths are ordered by udev precedence.  The first existing same-name
        # rule is the effective one.
        effective = next(
            (path for path in self.udev_paths if path.exists() or path.is_symlink()),
            None,
        )
        if effective is None:
            return UdevRuleStatus(
                IntegrationState.MISSING,
                "The MC7 udev access rule is not installed.",
                None,
                False,
                False,
                True,
            )
        if effective.is_symlink():
            return UdevRuleStatus(
                IntegrationState.ERROR,
                f"The effective MC7 udev rule is a symbolic link: {effective}",
                str(effective),
                True,
                False,
                False,
            )
        try:
            current = effective.is_file() and effective.read_bytes() == expected
        except OSError:
            current = False
        if current:
            return UdevRuleStatus(
                IntegrationState.CURRENT,
                f"Installed and current: {effective}",
                str(effective),
                True,
                True,
                True,
            )
        return UdevRuleStatus(
            IntegrationState.NEEDS_UPDATE,
            f"The effective MC7 udev rule differs from this application: {effective}",
            str(effective),
            True,
            False,
            True,
        )

    def install_udev_rule(self) -> HostIntegrationSnapshot:
        if not self._is_linux():
            raise HostIntegrationError("The MC7 udev rule can be installed only on Linux.")
        expected = bundled_udev_rule()
        target = self.udev_paths[0] if self.udev_paths else UDEV_RULE_DESTINATION
        if not target.is_absolute():
            raise HostIntegrationError("The udev rule destination must be absolute.")
        if target.is_symlink():
            raise HostIntegrationError(
                f"Refusing to replace the symbolic-link udev rule {target}."
            )
        pkexec = self._command("pkexec")
        install = self._command("install")
        udevadm = self._command("udevadm")
        missing = [
            name
            for name, value in (
                ("pkexec", pkexec),
                ("install", install),
                ("udevadm", udevadm),
            )
            if value is None
        ]
        if missing:
            raise HostIntegrationError(
                "Required system command unavailable: " + ", ".join(missing) + "."
            )

        temporary_root = tempfile.mkdtemp(
            prefix="swarm2-udev-", dir=self.temp_directory
        )
        temporary = Path(temporary_root) / UDEV_RULE_NAME
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o400,
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    descriptor = -1
                    stream.write(expected)
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                if descriptor >= 0:
                    os.close(descriptor)

            install_result = self._run(
                (
                    pkexec,
                    install,
                    "-D",
                    "-o",
                    "root",
                    "-g",
                    "root",
                    "-m",
                    "0644",
                    "--",
                    str(temporary),
                    str(target),
                ),
                120.0,
            )
            if install_result.returncode != 0:
                raise HostIntegrationError(
                    _command_failure(
                        "Administrator authorization or udev rule installation failed",
                        install_result,
                    )
                )
            reload_result = self._run(
                (pkexec, udevadm, "control", "--reload-rules"),
                120.0,
            )
            if reload_result.returncode != 0:
                raise HostIntegrationError(
                    _command_failure("The udev rule was installed but could not be reloaded", reload_result)
                )
        finally:
            temporary.unlink(missing_ok=True)
            try:
                Path(temporary_root).rmdir()
            except OSError:
                pass

        refreshed = self.check_all()
        if not refreshed.udev.current:
            raise HostIntegrationError(
                "The installed udev rule did not match the bundled rule."
            )
        return refreshed


def _command_failure(prefix: str, result: CommandResult) -> str:
    message = " ".join(
        (result.stderr or result.stdout).replace("\0", "").split()
    )
    if len(message) > 500:
        message = message[:497] + "..."
    return f"{prefix} (exit {result.returncode})" + (f": {message}" if message else ".")
