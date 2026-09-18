"""Bounded host playback controls, independent from MC7 input and USB.

Linux calls the standard MPRIS D-Bus interface through ``gdbus``.  macOS
uses the public Apple-event scripting interface exposed by the Music app.
The platform adapters do not listen for mouse events, infer LCD touch bytes,
or report that playback changed merely because a command was accepted.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
import os
import posixpath
import platform
import re
import selectors
from .pipe_io import pipe_selector
import shutil
import subprocess
import time
from typing import Protocol, runtime_checkable

from .media_player_ids import (
    APPLE_MUSIC_PLAYER_ID,
    MPRIS_PLAYER_PREFIX,
    is_mpris_player_id,
)
from .runtime import host_command_environment


COMMAND_TIMEOUT_SECONDS = 0.75
DISPATCH_TIMEOUT_SECONDS = 2.0
MAX_COMMAND_OUTPUT_BYTES = 8 * 1024
MAX_MPRIS_PLAYERS = 8

_MPRIS_OBJECT = "/org/mpris/MediaPlayer2"
_MPRIS_PLAYER = "org.mpris.MediaPlayer2.Player"
_DBUS_PROPERTIES = "org.freedesktop.DBus.Properties"

_PERMISSION_MARKERS = (
    "access denied",
    "accessdenied",
    "not authorized",
    "not authorised",
    "operation not permitted",
    "permission denied",
    "-1743",
)
_MISSING_SERVICE_MARKERS = (
    "application isn't running",
    "application isn\u2019t running",
    "namehasnoowner",
    "serviceunknown",
    "was not provided by any .service files",
    "(-600)",
    "(-609)",
)
_UNAVAILABLE_MARKERS = (
    "cannot autolaunch d-bus",
    "could not connect",
    "failed to connect",
    "no such file or directory",
)


class MediaAction(str, Enum):
    """Playback actions shared by MPRIS and the Music scripting dictionary."""

    PLAY_PAUSE = "play_pause"
    NEXT = "next"
    PREVIOUS = "previous"
    SHUFFLE = "shuffle"
    REPEAT = "repeat"
    STOP = "stop"


class MediaLoopStatus(str, Enum):
    """Portable form of the MPRIS and Apple Music repeat modes."""

    NONE = "None"
    TRACK = "Track"
    PLAYLIST = "Playlist"


@dataclass(frozen=True)
class MediaPlaybackState:
    """One state read from a specific, still-selected host player."""

    playing: bool
    shuffle_active: bool
    loop_status: MediaLoopStatus
    backend: str
    player_id: str

    def __post_init__(self) -> None:
        if type(self.playing) is not bool or type(self.shuffle_active) is not bool:
            raise TypeError("Media playback flags must be booleans")
        if not isinstance(self.loop_status, MediaLoopStatus):
            raise TypeError("Media loop status must be explicit")
        for label, value, maximum in (
            ("backend", self.backend, 64),
            ("player", self.player_id, 512),
        ):
            if (not isinstance(value, str) or not value
                    or len(value) > maximum or not value.isprintable()):
                raise TypeError(f"Media playback {label} must be bounded printable text")

    @property
    def repeat_active(self) -> bool:
        return self.loop_status is not MediaLoopStatus.NONE


@dataclass(frozen=True)
class MediaDispatch:
    """One accepted host command; it is not playback-state confirmation."""

    action: MediaAction
    backend: str
    player_id: str
    prior_status: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action, MediaAction):
            raise TypeError("Media dispatch action must be explicit")
        if not isinstance(self.backend, str) or not self.backend:
            raise TypeError("Media dispatch backend must be named")
        if not isinstance(self.player_id, str) or not self.player_id:
            raise TypeError("Media dispatch player must be named")
        if self.prior_status not in (None, "Playing", "Paused", "Stopped"):
            raise TypeError("Media dispatch status is invalid")


@dataclass(frozen=True)
class MediaPlayerInfo:
    """One supported player found in the current desktop session."""

    player_id: str
    label: str
    backend: str

    def __post_init__(self) -> None:
        from .media_player_ids import validate_media_player_id
        validate_media_player_id(self.player_id)
        if (not isinstance(self.label, str) or not self.label
                or len(self.label) > 160 or not self.label.isprintable()):
            raise TypeError("Media player label must be bounded printable text")
        if self.backend not in ("mpris", "apple-music"):
            raise TypeError("Media player backend is unsupported")


class MediaControlError(RuntimeError):
    """A host media command was not safely dispatched."""


class MediaProviderUnavailable(MediaControlError):
    """The current platform has no usable supported provider."""


class MediaPermissionDenied(MediaControlError):
    """The desktop session refused media-control access."""


class NoMediaPlayer(MediaControlError):
    """No supported running player can receive the requested action."""


class AmbiguousMediaPlayers(MediaControlError):
    """More than one equally eligible player is present."""


class MediaActionUnsupported(MediaControlError):
    """The selected player reports that it cannot perform this action."""


class MediaCommandFailed(MediaControlError):
    """A supported provider returned an error or malformed response."""


class MediaCommandTimedOut(MediaCommandFailed):
    """A bounded provider call did not finish in time."""


@dataclass(frozen=True)
class MediaCommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class _CommandOutputLimit(RuntimeError):
    pass


class _PlayerVanished(RuntimeError):
    pass


CommandRunner = Callable[[Sequence[str], float, int], MediaCommandResult]


@runtime_checkable
class HostMediaProvider(Protocol):
    """Small seam used by a future, independently verified touch listener."""

    backend: str

    def perform(self, action: MediaAction) -> MediaDispatch:
        """Dispatch one validated action or raise ``MediaControlError``."""

    def read_state(self) -> MediaPlaybackState:
        """Read current playback flags or raise ``MediaControlError``."""


def _action(value: MediaAction) -> MediaAction:
    if not isinstance(value, MediaAction):
        raise TypeError("Media action must be a MediaAction")
    return value


def _run_bounded_command(
    arguments: Sequence[str], timeout: float, maximum: int
) -> MediaCommandResult:
    """Run one argv vector with hard time and per-stream byte limits."""

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
        raise ValueError("A bounded media command needs valid arguments and limits")
    environment = host_command_environment({"LC_ALL": "C", "LANG": "C"})
    process = subprocess.Popen(
        list(arguments),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        close_fds=True,
        shell=False,
    )
    output, errors = bytearray(), bytearray()
    deadline = time.monotonic() + float(timeout)
    try:
        if process.stdout is None or process.stderr is None:
            raise OSError("Host media command pipes are unavailable")
        with pipe_selector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ, output)
            ready.register(process.stderr, selectors.EVENT_READ, errors)
            while ready.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(arguments, timeout)
                for key, _ in ready.select(min(remaining, 0.05)):
                    buffer = key.data
                    chunk = os.read(
                        key.fileobj.fileno(), min(4096, maximum - len(buffer) + 1)
                    )
                    if not chunk:
                        ready.unregister(key.fileobj)
                        continue
                    buffer.extend(chunk)
                    if len(buffer) > maximum:
                        raise _CommandOutputLimit
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(arguments, timeout)
        returncode = process.wait(timeout=remaining)
        return MediaCommandResult(returncode, bytes(output), bytes(errors))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


class _BoundedProvider:
    def __init__(self, executable: str | None, runner: CommandRunner):
        self.executable = executable
        self._runner = runner
        self._dispatch_deadline: float | None = None

    @contextmanager
    def _bounded_dispatch(self):
        if self._dispatch_deadline is not None:
            raise MediaCommandFailed(
                f"{self.backend} media control is already handling an action")
        self._dispatch_deadline = time.monotonic() + DISPATCH_TIMEOUT_SECONDS
        try:
            yield
        finally:
            self._dispatch_deadline = None

    def _invoke(self, arguments: Sequence[str]) -> MediaCommandResult:
        if not isinstance(self.executable, str) or not posixpath.isabs(self.executable):
            raise MediaProviderUnavailable(
                f"{self.backend} media control is unavailable on this host"
            )
        timeout = COMMAND_TIMEOUT_SECONDS
        if self._dispatch_deadline is not None:
            remaining = self._dispatch_deadline - time.monotonic()
            if remaining <= 0:
                raise MediaCommandTimedOut(
                    f"{self.backend} media control timed out")
            timeout = min(timeout, remaining)
        try:
            result = self._runner(
                tuple(arguments), timeout, MAX_COMMAND_OUTPUT_BYTES
            )
        except (subprocess.TimeoutExpired, MediaCommandTimedOut) as error:
            raise MediaCommandTimedOut(
                f"{self.backend} media control timed out"
            ) from error
        except _CommandOutputLimit as error:
            raise MediaCommandFailed(
                f"{self.backend} media control returned too much data"
            ) from error
        except (OSError, subprocess.SubprocessError) as error:
            raise MediaProviderUnavailable(
                f"{self.backend} media control could not be started"
            ) from error
        if (self._dispatch_deadline is not None
                and time.monotonic() >= self._dispatch_deadline):
            raise MediaCommandTimedOut(
                f"{self.backend} media control timed out")
        if (
            type(result) is not MediaCommandResult
            or type(result.returncode) is not int
            or not isinstance(result.stdout, bytes)
            or not isinstance(result.stderr, bytes)
            or len(result.stdout) > MAX_COMMAND_OUTPUT_BYTES
            or len(result.stderr) > MAX_COMMAND_OUTPUT_BYTES
        ):
            raise MediaCommandFailed(
                f"{self.backend} media control returned an invalid response"
            )
        if result.returncode:
            details = (result.stdout + b"\n" + result.stderr).decode(
                "utf-8", errors="replace"
            ).casefold()
            if any(marker in details for marker in _PERMISSION_MARKERS):
                raise MediaPermissionDenied(
                    f"Permission to control {self.backend} media was denied"
                )
            if any(marker in details for marker in _UNAVAILABLE_MARKERS):
                raise MediaProviderUnavailable(
                    f"{self.backend} media control is unavailable in this session"
                )
            if any(marker in details for marker in _MISSING_SERVICE_MARKERS):
                raise _PlayerVanished
            raise MediaCommandFailed(f"{self.backend} media control failed")
        return result


class MprisMediaProvider(_BoundedProvider):
    """Control one unambiguous MPRIS player in the Linux user session."""

    backend = "Linux MPRIS"

    _METHODS = {
        MediaAction.PLAY_PAUSE: "PlayPause",
        MediaAction.NEXT: "Next",
        MediaAction.PREVIOUS: "Previous",
        MediaAction.STOP: "Stop",
    }

    _NEXT_LOOP_STATUS = {
        MediaLoopStatus.NONE: MediaLoopStatus.TRACK,
        MediaLoopStatus.TRACK: MediaLoopStatus.PLAYLIST,
        MediaLoopStatus.PLAYLIST: MediaLoopStatus.NONE,
    }

    def __init__(
        self,
        *,
        preferred_player: str | None = None,
        find_executable: Callable[[str], str | None] = shutil.which,
        runner: CommandRunner = _run_bounded_command,
    ):
        if preferred_player is not None and not is_mpris_player_id(preferred_player):
            raise ValueError("Preferred MPRIS player must be a complete well-known bus name")
        try:
            executable = find_executable("gdbus")
        except OSError:
            executable = None
        super().__init__(executable, runner)
        self.preferred_player = preferred_player

    def _gdbus(self, *arguments: str) -> MediaCommandResult:
        return self._invoke((self.executable or "gdbus", "call", "--session", *arguments))

    def _list_players(self) -> tuple[str, ...]:
        try:
            result = self._gdbus(
                "--dest", "org.freedesktop.DBus",
                "--object-path", "/org/freedesktop/DBus",
                "--method", "org.freedesktop.DBus.ListNames",
            )
        except _PlayerVanished as error:
            raise MediaProviderUnavailable(
                "The Linux session D-Bus service is unavailable"
            ) from error
        try:
            text = result.stdout.decode("ascii").strip()
            if not (text.startswith("(") and text.endswith(",)")):
                raise ValueError
            names = ast.literal_eval(text[1:-2])
        except (UnicodeError, ValueError, SyntaxError, MemoryError, RecursionError) as error:
            raise MediaCommandFailed(
                "Linux MPRIS discovery returned an invalid response"
            ) from error
        if (
            not isinstance(names, list)
            or len(names) > 256
            or any(not isinstance(name, str) for name in names)
        ):
            raise MediaCommandFailed("Linux MPRIS discovery returned an invalid response")
        players = tuple(sorted(
            name for name in set(names)
            if name.startswith(MPRIS_PLAYER_PREFIX) and is_mpris_player_id(name)
        ))
        if len(players) > MAX_MPRIS_PLAYERS:
            raise AmbiguousMediaPlayers(
                f"More than {MAX_MPRIS_PLAYERS} MPRIS players are registered"
            )
        return players

    def available_players(self) -> tuple[MediaPlayerInfo, ...]:
        """List bounded MPRIS names without reading playback or sending input."""

        with self._bounded_dispatch():
            players = self._list_players()
        return tuple(
            MediaPlayerInfo(
                player_id=player,
                label=player[len(MPRIS_PLAYER_PREFIX):][:160],
                backend="mpris",
            )
            for player in players
        )

    def _property(self, player: str, name: str) -> MediaCommandResult:
        return self._gdbus(
            "--dest", player,
            "--object-path", _MPRIS_OBJECT,
            "--method", f"{_DBUS_PROPERTIES}.Get",
            _MPRIS_PLAYER, name,
        )

    def _owner(self, player: str) -> str:
        """Resolve one well-known name to a non-activatable unique bus name."""

        result = self._gdbus(
            "--dest", "org.freedesktop.DBus",
            "--object-path", "/org/freedesktop/DBus",
            "--method", "org.freedesktop.DBus.GetNameOwner",
            player,
        )
        try:
            text = result.stdout.decode("ascii").strip()
            parsed = ast.literal_eval(text)
        except (UnicodeError, ValueError, SyntaxError, MemoryError,
                RecursionError) as error:
            raise MediaCommandFailed(
                "Linux MPRIS owner lookup returned an invalid response") from error
        if (not isinstance(parsed, tuple) or len(parsed) != 1
                or not isinstance(parsed[0], str)
                or len(parsed[0]) > 255
                or re.fullmatch(
                    r":[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+",
                    parsed[0]) is None):
            raise MediaCommandFailed(
                "Linux MPRIS owner lookup returned an invalid response")
        return parsed[0]

    def _status(self, player: str) -> str:
        raw = self._property(player, "PlaybackStatus").stdout
        try:
            text = raw.decode("ascii").strip()
        except UnicodeError as error:
            raise MediaCommandFailed(
                "An MPRIS player returned an invalid playback status"
            ) from error
        match = re.fullmatch(r"\(\s*<\s*'(Playing|Paused|Stopped)'\s*>\s*,?\s*\)", text)
        if match is None:
            raise MediaCommandFailed(
                "An MPRIS player returned an invalid playback status"
            )
        return match.group(1)

    def _boolean_property(self, player: str, name: str) -> bool:
        raw = self._property(player, name).stdout
        try:
            text = raw.decode("ascii").strip()
        except UnicodeError as error:
            raise MediaCommandFailed(
                f"An MPRIS player returned an invalid {name} capability"
            ) from error
        match = re.fullmatch(r"\(\s*<\s*(true|false)\s*>\s*,?\s*\)", text)
        if match is None:
            raise MediaCommandFailed(
                f"An MPRIS player returned an invalid {name} capability"
            )
        return match.group(1) == "true"

    def _loop_status(self, player: str) -> MediaLoopStatus:
        raw = self._property(player, "LoopStatus").stdout
        try:
            text = raw.decode("ascii").strip()
        except UnicodeError as error:
            raise MediaCommandFailed(
                "An MPRIS player returned an invalid LoopStatus") from error
        match = re.fullmatch(
            r"\(\s*<\s*'(None|Track|Playlist)'\s*>\s*,?\s*\)", text)
        if match is None:
            raise MediaCommandFailed(
                "An MPRIS player returned an invalid LoopStatus")
        return MediaLoopStatus(match.group(1))

    def _set_property(self, player: str, name: str, variant: str) -> None:
        result = self._gdbus(
            "--dest", player,
            "--object-path", _MPRIS_OBJECT,
            "--method", f"{_DBUS_PROPERTIES}.Set",
            _MPRIS_PLAYER, name, variant,
        )
        try:
            response = result.stdout.decode("ascii").strip()
        except UnicodeError as error:
            raise MediaCommandFailed(
                f"The MPRIS {name} update returned an invalid response") from error
        if response != "()":
            raise MediaCommandFailed(
                f"The MPRIS {name} update returned an invalid response")

    def _select_player(self) -> tuple[str, str, str]:
        players = self._list_players()
        if self.preferred_player is not None:
            if self.preferred_player not in players:
                raise NoMediaPlayer("The selected MPRIS player is not running")
            try:
                owner = self._owner(self.preferred_player)
                return self.preferred_player, owner, self._status(owner)
            except _PlayerVanished as error:
                raise NoMediaPlayer("The selected MPRIS player closed") from error

        statuses = []
        for player in players:
            try:
                owner = self._owner(player)
                statuses.append((player, owner, self._status(owner)))
            except _PlayerVanished as error:
                raise NoMediaPlayer(
                    "An MPRIS player closed during selection") from error
        if not statuses:
            raise NoMediaPlayer("No MPRIS player is running")
        playing = [item for item in statuses if item[2] == "Playing"]
        if len(playing) == 1:
            return playing[0]
        if len(playing) > 1:
            raise AmbiguousMediaPlayers(
                "More than one MPRIS player reports active playback"
            )
        if len(statuses) == 1:
            return statuses[0]
        raise AmbiguousMediaPlayers(
            "Choose a preferred MPRIS player before controlling multiple idle players"
        )

    def perform(self, action: MediaAction) -> MediaDispatch:
        action = _action(action)
        with self._bounded_dispatch():
            return self._perform(action)

    def _perform(self, action: MediaAction) -> MediaDispatch:
        player, owner, status = self._select_player()
        try:
            if not self._boolean_property(owner, "CanControl"):
                raise MediaActionUnsupported("The selected MPRIS player cannot be controlled")
            capabilities = {
                MediaAction.NEXT: ("CanGoNext",),
                MediaAction.PREVIOUS: ("CanGoPrevious",),
                MediaAction.PLAY_PAUSE: (
                    ("CanPause",) if status == "Playing"
                    else ("CanPlay",)
                ),
                MediaAction.STOP: (),
                MediaAction.SHUFFLE: (),
                MediaAction.REPEAT: (),
            }[action]
            for capability in capabilities:
                if not self._boolean_property(owner, capability):
                    raise MediaActionUnsupported(
                        f"The selected MPRIS player reports {capability}=false"
                    )
            if action is MediaAction.SHUFFLE:
                enabled = self._boolean_property(owner, "Shuffle")
                self._set_property(
                    owner, "Shuffle", "<false>" if enabled else "<true>")
                result = None
            elif action is MediaAction.REPEAT:
                next_status = self._NEXT_LOOP_STATUS[self._loop_status(owner)]
                self._set_property(
                    owner, "LoopStatus", f"<'{next_status.value}'>")
                result = None
            else:
                result = self._gdbus(
                    "--dest", owner,
                    "--object-path", _MPRIS_OBJECT,
                    "--method", f"{_MPRIS_PLAYER}.{self._METHODS[action]}",
                )
        except _PlayerVanished as error:
            raise NoMediaPlayer("The selected MPRIS player closed") from error
        if result is not None:
            try:
                response = result.stdout.decode("ascii").strip()
            except UnicodeError as error:
                raise MediaCommandFailed(
                    "The MPRIS action returned an invalid response") from error
            if response != "()":
                raise MediaCommandFailed(
                    "The MPRIS action returned an invalid response")
        return MediaDispatch(action, "mpris", player, status)

    def read_state(self) -> MediaPlaybackState:
        """Read the three app-1024 flags from one unambiguous player."""

        with self._bounded_dispatch():
            player, owner, status = self._select_player()
            try:
                shuffle = self._boolean_property(owner, "Shuffle")
                loop_status = self._loop_status(owner)
            except _PlayerVanished as error:
                raise NoMediaPlayer(
                    "The selected MPRIS player closed") from error
        return MediaPlaybackState(
            playing=status == "Playing",
            shuffle_active=shuffle,
            loop_status=loop_status,
            backend="mpris",
            player_id=player,
        )


_APPLE_MUSIC_COMMANDS = {
    MediaAction.PLAY_PAUSE: "playpause",
    MediaAction.NEXT: "next track",
    MediaAction.PREVIOUS: "previous track",
    MediaAction.STOP: "stop",
}


class AppleMusicMediaProvider(_BoundedProvider):
    """Control an already-running Apple Music app through AppleScript."""

    backend = "macOS Apple Music"
    player_id = APPLE_MUSIC_PLAYER_ID

    def __init__(
        self,
        *,
        preferred_player: str | None = None,
        find_executable: Callable[[str], str | None] = shutil.which,
        runner: CommandRunner = _run_bounded_command,
    ):
        if preferred_player not in (None, self.player_id):
            raise ValueError("The macOS media provider supports Apple Music only")
        try:
            executable = find_executable("osascript")
        except OSError:
            executable = None
        super().__init__(executable, runner)

    @staticmethod
    def _discovery_script() -> str:
        return (
            'if application id "com.apple.Music" is running then\n'
            '    return "swarm2:running"\n'
            'end if\n'
            'return "swarm2:no-player"'
        )

    def available_players(self) -> tuple[MediaPlayerInfo, ...]:
        """Report Apple Music only when it is already running."""

        with self._bounded_dispatch():
            try:
                result = self._invoke((
                    self.executable or "osascript", "-e",
                    self._discovery_script(),
                ))
            except _PlayerVanished:
                return ()
        try:
            response = result.stdout.decode("utf-8").strip()
        except UnicodeError as error:
            raise MediaCommandFailed(
                "Apple Music discovery returned an invalid response") from error
        if response == "swarm2:no-player":
            return ()
        if response != "swarm2:running":
            raise MediaCommandFailed(
                "Apple Music discovery returned an invalid response")
        return (MediaPlayerInfo(
            self.player_id, "Apple Music", "apple-music"),)

    @staticmethod
    def _script(action: MediaAction) -> str:
        if action in _APPLE_MUSIC_COMMANDS:
            return (
                'if application id "com.apple.Music" is running then\n'
                '    tell application id "com.apple.Music" to '
                + _APPLE_MUSIC_COMMANDS[action] + '\n'
                '    return "swarm2:ok"\n'
                'end if\n'
                'return "swarm2:no-player"'
            )
        if action is MediaAction.SHUFFLE:
            command = "set shuffle enabled to not (shuffle enabled)"
        else:
            command = (
                "if song repeat is off then\n"
                "            set song repeat to one\n"
                "        else if song repeat is one then\n"
                "            set song repeat to all\n"
                "        else\n"
                "            set song repeat to off\n"
                "        end if"
            )
        return (
            'if application id "com.apple.Music" is running then\n'
            '    tell application id "com.apple.Music"\n'
            '        ' + command + '\n'
            '    end tell\n'
            '    return "swarm2:ok"\n'
            'end if\n'
            'return "swarm2:no-player"'
        )

    @staticmethod
    def _state_script() -> str:
        return (
            'if application id "com.apple.Music" is running then\n'
            '    tell application id "com.apple.Music"\n'
            '        set swarm2State to player state as text\n'
            '        set swarm2Shuffle to shuffle enabled as text\n'
            '        set swarm2Repeat to song repeat as text\n'
            '    end tell\n'
            '    return "swarm2:state|" & swarm2State & "|" & '
            'swarm2Shuffle & "|" & swarm2Repeat\n'
            'end if\n'
            'return "swarm2:no-player"'
        )

    def perform(self, action: MediaAction) -> MediaDispatch:
        action = _action(action)
        with self._bounded_dispatch():
            return self._perform(action)

    def _perform(self, action: MediaAction) -> MediaDispatch:
        try:
            result = self._invoke((
                self.executable or "osascript", "-e", self._script(action)
            ))
        except _PlayerVanished as error:
            raise NoMediaPlayer("Apple Music closed before the action") from error
        try:
            response = result.stdout.decode("utf-8").strip()
        except UnicodeError as error:
            raise MediaCommandFailed(
                "Apple Music returned an invalid response"
            ) from error
        if response == "swarm2:no-player":
            raise NoMediaPlayer("Apple Music is not running")
        if response != "swarm2:ok":
            raise MediaCommandFailed("Apple Music returned an invalid response")
        return MediaDispatch(action, "apple-music", self.player_id)

    def read_state(self) -> MediaPlaybackState:
        """Read Apple Music state without launching or activating the app."""

        with self._bounded_dispatch():
            try:
                result = self._invoke((
                    self.executable or "osascript", "-e", self._state_script()
                ))
            except _PlayerVanished as error:
                raise NoMediaPlayer(
                    "Apple Music closed before its state was read") from error
        try:
            response = result.stdout.decode("utf-8").strip()
        except UnicodeError as error:
            raise MediaCommandFailed(
                "Apple Music returned an invalid state") from error
        if response == "swarm2:no-player":
            raise NoMediaPlayer("Apple Music is not running")
        parts = response.split("|")
        if (len(parts) != 4 or parts[0] != "swarm2:state"
                or parts[1] not in (
                    "playing", "paused", "stopped",
                    "fast forwarding", "rewinding")
                or parts[2] not in ("true", "false")
                or parts[3] not in ("off", "one", "all")):
            raise MediaCommandFailed("Apple Music returned an invalid state")
        loop_status = {
            "off": MediaLoopStatus.NONE,
            "one": MediaLoopStatus.TRACK,
            "all": MediaLoopStatus.PLAYLIST,
        }[parts[3]]
        return MediaPlaybackState(
            playing=parts[1] == "playing",
            shuffle_active=parts[2] == "true",
            loop_status=loop_status,
            backend="apple-music",
            player_id=self.player_id,
        )


class UnavailableMediaProvider:
    """Explicit provider for unsupported host platforms."""

    backend = "Host"

    def __init__(self, system: str):
        self.system = system

    def perform(self, action: MediaAction) -> MediaDispatch:
        _action(action)
        raise MediaProviderUnavailable(
            f"Host media control is unavailable on {self.system or 'this platform'}"
        )

    def available_players(self) -> tuple[MediaPlayerInfo, ...]:
        raise MediaProviderUnavailable(
            f"Host media control is unavailable on {self.system or 'this platform'}"
        )

    def read_state(self) -> MediaPlaybackState:
        raise MediaProviderUnavailable(
            f"Host media control is unavailable on {self.system or 'this platform'}"
        )


def create_host_media_provider(
    *,
    system: str | None = None,
    preferred_player: str | None = None,
    find_executable: Callable[[str], str | None] = shutil.which,
    runner: CommandRunner = _run_bounded_command,
) -> HostMediaProvider:
    """Build a platform provider without running a command or opening a device."""

    host_system = platform.system() if system is None else system
    if host_system == "Linux":
        return MprisMediaProvider(
            preferred_player=preferred_player,
            find_executable=find_executable,
            runner=runner,
        )
    if host_system == "Darwin":
        return AppleMusicMediaProvider(
            preferred_player=preferred_player,
            find_executable=find_executable,
            runner=runner,
        )
    if preferred_player is not None:
        raise ValueError("Preferred MPRIS players apply only on Linux")
    return UnavailableMediaProvider(host_system)


def available_media_players(
    *,
    system: str | None = None,
    find_executable: Callable[[str], str | None] = shutil.which,
    runner: CommandRunner = _run_bounded_command,
) -> tuple[MediaPlayerInfo, ...]:
    """Discover supported running players without opening an MC7 device."""

    provider = create_host_media_provider(
        system=system, find_executable=find_executable, runner=runner)
    return provider.available_players()
