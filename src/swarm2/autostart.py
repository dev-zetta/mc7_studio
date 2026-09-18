"""Explicit, per-user start-at-login integration for Linux and macOS.

Nothing is installed while checking host state.  Callers must invoke
``install`` or ``remove`` in response to an explicit user action.  Linux uses
the XDG autostart directory; macOS uses a per-user LaunchAgent.  Both formats
start the application with an argv vector and never invoke a shell.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import errno
import os
from pathlib import Path
import plistlib
import stat
import sys
import tempfile
from typing import Mapping, Sequence


LINUX_AUTOSTART_NAME = "swarm2-mc7.desktop"
MACOS_LAUNCH_AGENT_LABEL = "local.swarm2-mc7.mc7-studio"
MACOS_LAUNCH_AGENT_NAME = f"{MACOS_LAUNCH_AGENT_LABEL}.plist"
LINUX_MANAGED_KEY = "X-Swarm2-MC7-Autostart=true"

_MAX_ARGUMENTS = 128
_MAX_ARGUMENT_LENGTH = 4096
_MAX_INVOCATION_LENGTH = 32 * 1024
_MAX_AUTOSTART_BYTES = 128 * 1024


class AutostartState(str, Enum):
    """Stable state names for the desktop integration UI."""

    NOT_APPLICABLE = "not_applicable"
    MISSING = "missing"
    CURRENT = "current"
    NEEDS_UPDATE = "needs_update"
    ERROR = "error"


@dataclass(frozen=True)
class AutostartStatus:
    state: AutostartState
    detail: str
    path: str | None
    installed: bool
    current: bool
    can_install: bool
    can_remove: bool


class AutostartError(RuntimeError):
    """A requested start-at-login operation could not be completed."""


@dataclass(frozen=True)
class _TargetSnapshot:
    present: bool
    regular: bool
    content: bytes | None
    identity: tuple[int, int] | None


class AutostartManager:
    """Check and explicitly manage the current user's login entry.

    Constructor overrides are dependency-injection seams for tests and
    packagers. The default command uses the outer AppImage path while running
    from an AppImage and otherwise uses ``sys.executable -m swarm2.gui``.
    """

    def __init__(
        self,
        *,
        platform: str | None = None,
        environ: Mapping[str, str] | None = None,
        home: str | Path | None = None,
        config_home: str | Path | None = None,
        executable: str | Path | None = None,
        arguments: Sequence[str] | None = None,
    ):
        self.platform = sys.platform if platform is None else platform
        self.environ = dict(os.environ if environ is None else environ)
        self.home = None if home is None else Path(home)
        self.config_home = None if config_home is None else Path(config_home)
        appimage = self.environ.get("APPIMAGE", "")
        default_appimage = (
            executable is None
            and arguments is None
            and self.platform.startswith("linux")
            and bool(appimage)
            and Path(appimage).is_absolute()
        )
        self.executable = appimage if default_appimage else (
            sys.executable if executable is None else os.fspath(executable)
        )
        if default_appimage:
            self.arguments = ()
        elif arguments is None:
            self.arguments = ("-m", "swarm2.gui")
        elif isinstance(arguments, str):
            self.arguments = (arguments,)
        else:
            self.arguments = tuple(arguments)

    def _kind(self) -> str | None:
        if self.platform.startswith("linux"):
            return "linux"
        if self.platform == "darwin":
            return "macos"
        return None

    def _home(self) -> Path:
        if self.home is not None:
            home = self.home
        elif self.environ.get("HOME"):
            home = Path(self.environ["HOME"])
        else:
            home = Path.home()
        if not home.is_absolute():
            raise AutostartError("The user home directory must be an absolute path.")
        return home

    def path(self) -> Path:
        """Return the platform's per-user login-entry path."""

        kind = self._kind()
        if kind is None:
            raise AutostartError(
                "Start at login is available only on Linux and macOS."
            )
        home = self._home()
        if kind == "macos":
            return home / "Library" / "LaunchAgents" / MACOS_LAUNCH_AGENT_NAME

        if self.config_home is not None:
            config_home = self.config_home
            if not config_home.is_absolute():
                raise AutostartError("XDG_CONFIG_HOME must be an absolute path.")
        else:
            configured = self.environ.get("XDG_CONFIG_HOME", "")
            candidate = Path(configured) if configured else None
            # The XDG Base Directory specification says relative values are
            # invalid and must be ignored.
            config_home = (
                candidate
                if candidate is not None and candidate.is_absolute()
                else home / ".config"
            )
        return config_home / "autostart" / LINUX_AUTOSTART_NAME

    def program_arguments(self) -> tuple[str, ...]:
        """Return the validated argv stored in the login entry."""

        executable = self.executable
        if not isinstance(executable, str) or not executable:
            raise AutostartError("The MC7 Studio executable path is unavailable.")
        if not Path(executable).is_absolute():
            raise AutostartError("The MC7 Studio executable path must be absolute.")
        values = (executable, *self.arguments)
        if len(values) > _MAX_ARGUMENTS:
            raise AutostartError("The MC7 Studio login command has too many arguments.")
        total = 0
        for value in values:
            if not isinstance(value, str) or not value:
                raise AutostartError(
                    "Every MC7 Studio login-command argument must be non-empty text."
                )
            if len(value) > _MAX_ARGUMENT_LENGTH:
                raise AutostartError("An MC7 Studio login-command argument is too long.")
            if any(ord(character) < 32 or ord(character) == 127 for character in value):
                raise AutostartError(
                    "MC7 Studio login-command arguments cannot contain control characters."
                )
            try:
                value.encode("utf-8", errors="strict")
            except UnicodeError as error:
                raise AutostartError(
                    "MC7 Studio login-command arguments must be valid UTF-8 text."
                ) from error
            total += len(value)
        if total > _MAX_INVOCATION_LENGTH:
            raise AutostartError("The MC7 Studio login command is too long.")
        return values

    def expected_bytes(self) -> bytes:
        """Build the deterministic platform-native login entry."""

        kind = self._kind()
        if kind is None:
            raise AutostartError(
                "Start at login is available only on Linux and macOS."
            )
        arguments = self.program_arguments()
        if kind == "linux":
            command = " ".join(_desktop_exec_argument(value) for value in arguments)
            return (
                "[Desktop Entry]\n"
                "Type=Application\n"
                "Version=1.0\n"
                "Name=MC7 Studio\n"
                "Comment=Configure the Turtle Beach Command Series MC7\n"
                f"Exec={command}\n"
                "Terminal=false\n"
                "StartupNotify=false\n"
                "X-GNOME-Autostart-enabled=true\n"
                f"{LINUX_MANAGED_KEY}\n"
            ).encode("utf-8")

        document = {
            "Label": MACOS_LAUNCH_AGENT_LABEL,
            "ProcessType": "Interactive",
            "ProgramArguments": list(arguments),
            "RunAtLoad": True,
        }
        return plistlib.dumps(document, fmt=plistlib.FMT_XML, sort_keys=True)

    def check(self) -> AutostartStatus:
        """Check the login entry without changing the filesystem."""

        if self._kind() is None:
            return AutostartStatus(
                AutostartState.NOT_APPLICABLE,
                "Start at login is available only on Linux and macOS.",
                None,
                False,
                False,
                False,
                False,
            )
        try:
            path = self.path()
            expected = self.expected_bytes()
            snapshot = _read_target(path)
        except (AutostartError, OSError, UnicodeError) as error:
            return AutostartStatus(
                AutostartState.ERROR,
                str(error) or "The start-at-login entry could not be checked safely.",
                None,
                False,
                False,
                False,
                False,
            )

        path_text = str(path)
        if not snapshot.present:
            return AutostartStatus(
                AutostartState.MISSING,
                "MC7 Studio is not configured to start at login.",
                path_text,
                False,
                False,
                True,
                False,
            )
        if not snapshot.regular:
            return AutostartStatus(
                AutostartState.ERROR,
                f"The start-at-login path is a link or is not a regular file: {path}",
                path_text,
                True,
                False,
                False,
                False,
            )
        current = snapshot.content == expected
        if current:
            return AutostartStatus(
                AutostartState.CURRENT,
                f"The MC7 Studio start-at-login entry is installed and current: {path}",
                path_text,
                True,
                True,
                True,
                True,
            )
        if _is_managed_entry(self._kind(), snapshot.content):
            return AutostartStatus(
                AutostartState.NEEDS_UPDATE,
                f"The MC7 Studio start-at-login entry differs from this installation: {path}",
                path_text,
                True,
                False,
                True,
                True,
            )
        return AutostartStatus(
            AutostartState.ERROR,
            f"An unrecognized file occupies the MC7 Studio start-at-login path: {path}",
            path_text,
            True,
            False,
            False,
            False,
        )

    def install(self) -> AutostartStatus:
        """Safely install or update the current user's complete login entry."""

        path = self.path()
        expected = self.expected_bytes()
        try:
            snapshot = _read_target(path)
            if snapshot.present and not snapshot.regular:
                raise AutostartError(
                    f"Refusing to replace a link or non-regular login entry: {path}"
                )
            if (
                snapshot.present
                and snapshot.content != expected
                and not _is_managed_entry(self._kind(), snapshot.content)
            ):
                raise AutostartError(
                    f"Refusing to replace an unrecognized login entry: {path}"
                )
            if snapshot.content != expected:
                _install_entry(path, expected, self._kind(), snapshot)
        except AutostartError:
            raise
        except OSError as error:
            raise AutostartError(
                f"MC7 Studio could not install the start-at-login entry in {path}."
            ) from error
        status = self.check()
        if not status.current:
            raise AutostartError(
                "The installed start-at-login entry could not be verified."
            )
        return status

    def remove(self) -> AutostartStatus:
        """Remove MC7 Studio's per-user login entry if it is a regular file."""

        path = self.path()
        try:
            snapshot = _read_target(path)
            if not snapshot.present:
                return self.check()
            if not snapshot.regular:
                raise AutostartError(
                    f"Refusing to remove a link or non-regular login entry: {path}"
                )
            if not _is_managed_entry(self._kind(), snapshot.content):
                raise AutostartError(
                    f"Refusing to remove an unrecognized login entry: {path}"
                )
            _remove_managed_entry(path, snapshot, self._kind())
        except AutostartError:
            raise
        except OSError as error:
            raise AutostartError(
                f"MC7 Studio could not remove the start-at-login entry at {path}."
            ) from error
        status = self.check()
        if status.state is not AutostartState.MISSING:
            raise AutostartError("The start-at-login entry could not be removed safely.")
        return status


def _desktop_exec_argument(value: str) -> str:
    """Quote one Desktop Entry Exec argument without invoking a shell.

    The desktop-entry specification applies value-string unescaping before its
    Exec quoting rules.  Escape the Exec metacharacters first, then double each
    resulting backslash for the outer value-string layer.  ``%%`` represents a
    literal percent sign rather than a desktop field code.
    """

    value = value.replace("%", "%%")
    quoted = "".join(
        "\\" + character if character in {'"', "`", "$", "\\"} else character
        for character in value
    )
    quoted = quoted.replace("\\", "\\\\")
    return f'"{quoted}"'


def _is_managed_entry(kind: str | None, content: bytes | None) -> bool:
    """Recognize only stale entries that MC7 Studio owns."""

    if content is None:
        return False
    if kind == "linux":
        try:
            lines = content.decode("utf-8", errors="strict").splitlines()
        except UnicodeError:
            return False
        return bool(lines and lines[0] == "[Desktop Entry]" and LINUX_MANAGED_KEY in lines)
    if kind == "macos":
        try:
            document = plistlib.loads(content)
        except (plistlib.InvalidFileException, ValueError, TypeError, OverflowError):
            return False
        return (
            isinstance(document, dict)
            and document.get("Label") == MACOS_LAUNCH_AGENT_LABEL
        )
    return False


def _read_target(path: Path) -> _TargetSnapshot:
    """Read a bounded regular file without following a final symlink."""

    try:
        info = path.lstat()
    except FileNotFoundError:
        return _TargetSnapshot(False, False, None, None)
    if not stat.S_ISREG(info.st_mode):
        return _TargetSnapshot(True, False, None, (info.st_dev, info.st_ino))

    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
        ):
            raise AutostartError(
                f"The start-at-login entry changed while it was being checked: {path}"
            )
        content = stream.read(_MAX_AUTOSTART_BYTES + 1)
    if len(content) > _MAX_AUTOSTART_BYTES:
        content = None
    return _TargetSnapshot(True, True, content, (info.st_dev, info.st_ino))


def _install_entry(
    path: Path,
    content: bytes,
    kind: str | None,
    original: _TargetSnapshot,
) -> None:
    """Publish one complete entry without overwriting a concurrent file."""

    if len(content) > _MAX_AUTOSTART_BYTES:
        raise AutostartError("The generated start-at-login entry is too large.")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.parent.is_dir():
        raise AutostartError(
            f"The start-at-login directory is not a directory: {path.parent}"
        )
    temporary: Path | None = None
    descriptor = -1
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if original.present:
            _replace_managed_entry(path, temporary, original, kind)
        else:
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError as error:
                current = _read_target(path)
                if not (
                    current.regular
                    and current.content == content
                ):
                    raise AutostartError(
                        f"The start-at-login entry changed before it could be installed: {path}"
                    ) from error
        _fsync_directory(path.parent)
    finally:
        active_exception = sys.exc_info()[0] is not None
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as error:
                if not active_exception:
                    raise AutostartError(
                        f"The start-at-login temporary file could not be removed: "
                        f"{temporary}."
                    ) from error


def _replace_managed_entry(
    path: Path,
    staged: Path,
    original: _TargetSnapshot,
    kind: str | None,
) -> None:
    recovery = Path(
        tempfile.mkdtemp(prefix=f".{path.name}.recovery-", dir=path.parent)
    )
    displaced = recovery / path.name
    try:
        os.rename(path, displaced)
        try:
            moved = _read_target(displaced)
        except (AutostartError, OSError) as error:
            suffix = _recover_displaced(
                displaced, path, recovery, "The displaced entry"
            )
            raise AutostartError(
                f"The start-at-login entry could not be verified after it was "
                f"moved for an update: {path}." + suffix
            ) from error
        if not _same_target(moved, original) or not _is_managed_entry(
            kind, moved.content
        ):
            suffix = _recover_displaced(
                displaced, path, recovery, "The substituted entry"
            )
            raise AutostartError(
                f"The start-at-login entry changed before it could be updated: {path}."
                + suffix
            )
        try:
            os.link(staged, path, follow_symlinks=False)
        except FileExistsError as error:
            raise AutostartError(
                f"A new start-at-login entry appeared during the update. "
                f"The previous managed entry was preserved at {displaced}."
            ) from error
        except OSError as error:
            suffix = _recover_displaced(
                displaced, path, recovery, "The previous managed entry"
            )
            raise AutostartError(
                f"The updated start-at-login entry could not be published at {path}."
                + suffix
            ) from error
        try:
            displaced.unlink()
        except OSError as error:
            raise AutostartError(
                f"The updated start-at-login entry was published at {path}, but "
                f"the previous managed entry could not be removed and remains at "
                f"{displaced}."
            ) from error
        try:
            recovery.rmdir()
        except OSError as error:
            raise AutostartError(
                f"The updated start-at-login entry was published at {path}, but "
                f"its empty recovery directory could not be removed: {recovery}."
            ) from error
    except Exception:
        if recovery.exists():
            try:
                recovery.rmdir()
            except OSError:
                pass
        raise


def _remove_managed_entry(
    path: Path,
    original: _TargetSnapshot,
    kind: str | None,
) -> None:
    recovery = Path(
        tempfile.mkdtemp(prefix=f".{path.name}.recovery-", dir=path.parent)
    )
    displaced = recovery / path.name
    try:
        os.rename(path, displaced)
        try:
            moved = _read_target(displaced)
        except (AutostartError, OSError) as error:
            suffix = _recover_displaced(
                displaced, path, recovery, "The displaced entry"
            )
            raise AutostartError(
                f"The start-at-login entry could not be verified after it was "
                f"moved for removal: {path}." + suffix
            ) from error
        if not _same_target(moved, original) or not _is_managed_entry(
            kind, moved.content
        ):
            suffix = _recover_displaced(
                displaced, path, recovery, "The substituted entry"
            )
            raise AutostartError(
                f"The start-at-login entry changed before it could be removed: {path}."
                + suffix
            )
        try:
            displaced.unlink()
        except OSError as error:
            suffix = _recover_displaced(
                displaced, path, recovery, "The managed entry"
            )
            raise AutostartError(
                f"The start-at-login entry could not be deleted: {path}." + suffix
            ) from error
        try:
            recovery.rmdir()
        except OSError as error:
            raise AutostartError(
                f"The start-at-login entry was removed, but its empty recovery "
                f"directory could not be removed: {recovery}."
            ) from error
        try:
            _fsync_directory(path.parent)
        except OSError as error:
            raise AutostartError(
                f"The start-at-login entry was removed, but the login directory "
                f"could not be synchronized: {path.parent}."
            ) from error
    except Exception:
        if recovery.exists():
            try:
                recovery.rmdir()
            except OSError:
                pass
        raise


def _same_target(current: _TargetSnapshot, expected: _TargetSnapshot) -> bool:
    return bool(
        current.present
        and current.regular
        and current.identity is not None
        and current.identity == expected.identity
        and current.content == expected.content
    )


def _recover_displaced(
    displaced: Path,
    path: Path,
    recovery: Path,
    description: str,
) -> str:
    """Restore one displaced file without overwriting a concurrent entry."""

    if _restore_displaced(displaced, path):
        try:
            recovery.rmdir()
        except OSError:
            pass
        return f" {description} was restored."
    return f" {description} was preserved at {displaced}."


def _restore_displaced(displaced: Path, path: Path) -> bool:
    """Restore a raced regular entry without replacing another directory entry."""

    try:
        os.link(displaced, path, follow_symlinks=False)
    except OSError:
        return False
    try:
        displaced.unlink()
    except OSError:
        # Both names still reference the same inode, so no content was lost.
        return False
    try:
        _fsync_directory(path.parent)
    except OSError:
        # The public name is restored already. Recovery must not be obscured by
        # a later directory-durability error.
        pass
    return True


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        try:
            os.fsync(descriptor)
        except OSError as error:
            # Some macOS filesystems reject fsync on an open directory.  The
            # file itself was fsynced before rename; keep directory fsync as a
            # durability improvement where the host supports it.
            unsupported = {errno.EBADF, errno.EINVAL}
            if hasattr(errno, "ENOTSUP"):
                unsupported.add(errno.ENOTSUP)
            if error.errno not in unsupported:
                raise
    finally:
        os.close(descriptor)
