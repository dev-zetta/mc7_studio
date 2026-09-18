"""Bounded PipeWire playback-stream volume model for Swarm II app 1025.

Linux exposes active playback streams through PipeWire.  This module reads
the machine-readable ``pw-dump`` graph and changes one explicitly selected
stream, labelled by a stable application identity, with ``wpctl``.  Transient
PipeWire node IDs are never persisted and are revalidated immediately before
and after a write.  ``wpctl`` still accepts only the numeric node ID, so an
unavoidable close-and-ID-reuse race remains between validation and execution;
the app-1025 product path stays gated while its device mapping is unknown.

The built-in macOS APIs do not expose general per-application output volume,
so non-Linux platforms return an explicit unavailable provider for now.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
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

from .media_volume_commands import MediaVolumeAction, apply_media_volume_action
from .runtime import host_command_environment


COMMAND_TIMEOUT_SECONDS = 1.5
DISPATCH_TIMEOUT_SECONDS = 8.0
MAX_GRAPH_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_CONTROL_OUTPUT_BYTES = 8 * 1024
MAX_PIPEWIRE_OBJECTS = 4096
MAX_AUDIO_SESSIONS = 32
MAX_JSON_DEPTH = 16

_NODE_TYPE = "PipeWire:Interface:Node"
_PLAYBACK_CLASS = "Stream/Output/Audio"
_SESSION_ID = re.compile(
    r"pipewire:(application-id|process-binary):"
    r"([A-Za-z0-9][A-Za-z0-9._+@-]{0,127})"
)
_VOLUME_OUTPUT = re.compile(
    r"Volume:\s*([0-9]+(?:\.[0-9]{1,6})?)(?:\s+\[MUTED\])?\s*"
)

_PERMISSION_MARKERS = (
    "access denied",
    "operation not permitted",
    "permission denied",
)
_UNAVAILABLE_MARKERS = (
    "could not connect",
    "failed to connect",
    "host is down",
    "no such file or directory",
)


class HostAudioError(RuntimeError):
    """A host audio-session operation did not complete safely."""


class AudioProviderUnavailable(HostAudioError):
    """This host has no usable supported playback-stream audio provider."""


class AudioPermissionDenied(HostAudioError):
    """The desktop session denied access to its audio graph."""


class NoAudioSession(HostAudioError):
    """No matching active playback stream exists."""


class AmbiguousAudioSessions(HostAudioError):
    """More than one stream is eligible for the requested action."""


class AudioCommandFailed(HostAudioError):
    """A provider command failed or returned malformed data."""


class AudioCommandTimedOut(AudioCommandFailed):
    """A bounded provider operation exceeded its deadline."""


class AudioSessionChanged(AudioCommandFailed):
    """The selected PipeWire object changed before it could be written."""


class AudioVolumeWriteFailed(AudioCommandFailed):
    """The requested volume was rejected or could not be verified."""


@dataclass(frozen=True)
class AudioCommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True)
class AudioSessionInfo:
    """One selectable application identity, without a transient node ID."""

    session_id: str
    label: str
    backend: str = "pipewire"

    def __post_init__(self) -> None:
        validate_audio_session_id(self.session_id)
        if (
            not isinstance(self.label, str)
            or not self.label
            or len(self.label) > 160
            or not self.label.isprintable()
        ):
            raise TypeError("Audio-session label must be bounded printable text")
        if self.backend != "pipewire":
            raise TypeError("Audio-session backend is unsupported")


@dataclass(frozen=True)
class AudioVolumeState:
    """A verified scalar volume read from one selected playback stream."""

    volume: int
    backend: str
    session_id: str

    def __post_init__(self) -> None:
        if type(self.volume) is not int or not 0 <= self.volume <= 100:
            raise TypeError("Audio volume must be an integer in 0..100")
        validate_audio_session_id(self.session_id)
        if self.backend != "pipewire":
            raise TypeError("Audio-volume backend is unsupported")


@dataclass(frozen=True)
class AudioVolumeDispatch:
    """One app-1025 action whose absolute result was read back."""

    action: MediaVolumeAction
    previous_volume: int
    volume: int
    backend: str
    session_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.action, MediaVolumeAction):
            raise TypeError("Audio-volume action must be explicit")
        for value in (self.previous_volume, self.volume):
            if type(value) is not int or not 0 <= value <= 100:
                raise TypeError("Audio volume must be an integer in 0..100")
        validate_audio_session_id(self.session_id)
        if self.backend != "pipewire":
            raise TypeError("Audio-volume backend is unsupported")


@dataclass(frozen=True)
class _PipeWireNode:
    node_id: int
    serial: int
    session_id: str
    label: str


class _CommandOutputLimit(RuntimeError):
    pass


class _DuplicateJsonKey(ValueError):
    pass


CommandRunner = Callable[[Sequence[str], float, int], AudioCommandResult]


@runtime_checkable
class HostAudioProvider(Protocol):
    backend: str

    def available_sessions(self) -> tuple[AudioSessionInfo, ...]:
        """Return stable active application identities."""

    def read_volume(self) -> AudioVolumeState:
        """Read the selected session's current scalar volume."""

    def perform(self, action: MediaVolumeAction) -> AudioVolumeDispatch:
        """Apply one source-exact app-1025 action and verify its result."""


def validate_audio_session_id(value: str) -> str:
    """Validate and return a persistable PipeWire application selector."""

    if not isinstance(value, str) or _SESSION_ID.fullmatch(value) is None:
        raise ValueError("Audio session ID is invalid")
    return value


def is_audio_session_id(value: object) -> bool:
    try:
        validate_audio_session_id(value)  # type: ignore[arg-type]
    except ValueError:
        return False
    return True


def _run_bounded_command(
    arguments: Sequence[str], timeout: float, maximum: int
) -> AudioCommandResult:
    """Run one fixed argv vector with time and per-stream byte limits."""

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
        raise ValueError("A bounded audio command needs valid arguments and limits")
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
            raise OSError("Host audio command pipes are unavailable")
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
        return AudioCommandResult(returncode, bytes(output), bytes(errors))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _check_json_depth(value: object) -> None:
    stack: list[tuple[object, int]] = [(value, 1)]
    containers = 0
    while stack:
        current, depth = stack.pop()
        if isinstance(current, dict):
            containers += 1
            if depth > MAX_JSON_DEPTH:
                raise ValueError("PipeWire graph is too deeply nested")
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            containers += 1
            if depth > MAX_JSON_DEPTH:
                raise ValueError("PipeWire graph is too deeply nested")
            stack.extend((item, depth + 1) for item in current)
        if containers > MAX_PIPEWIRE_OBJECTS * 8:
            raise ValueError("PipeWire graph contains too many containers")


def _stable_identity(properties: dict[str, object]) -> str | None:
    for key, kind in (
        ("application.id", "application-id"),
        ("application.process.binary", "process-binary"),
    ):
        value = properties.get(key)
        if (
            isinstance(value, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+@-]{0,127}", value)
        ):
            return f"pipewire:{kind}:{value}"
    return None


def _node_label(properties: dict[str, object], fallback: str) -> str:
    for key in (
        "application.name",
        "node.description",
        "media.name",
        "application.id",
        "application.process.binary",
    ):
        value = properties.get(key)
        if isinstance(value, str):
            value = value.strip()
            if value and len(value) <= 160 and value.isprintable():
                return value
    return fallback.rsplit(":", 1)[-1]


class PipeWireAudioProvider:
    """Select and control one unambiguous PipeWire playback stream."""

    backend = "pipewire"

    def __init__(
        self,
        *,
        preferred_session: str | None = None,
        find_executable: Callable[[str], str | None] = shutil.which,
        runner: CommandRunner = _run_bounded_command,
    ):
        if preferred_session is not None:
            validate_audio_session_id(preferred_session)
        try:
            pw_dump = find_executable("pw-dump")
            wpctl = find_executable("wpctl")
        except OSError:
            pw_dump = wpctl = None
        self.pw_dump = pw_dump
        self.wpctl = wpctl
        self.preferred_session = preferred_session
        self._runner = runner
        self._dispatch_deadline: float | None = None

    @contextmanager
    def _bounded_dispatch(self):
        if self._dispatch_deadline is not None:
            raise AudioCommandFailed("PipeWire audio is already handling an action")
        self._dispatch_deadline = time.monotonic() + DISPATCH_TIMEOUT_SECONDS
        try:
            yield
        finally:
            self._dispatch_deadline = None

    def _invoke(
        self, executable: str | None, arguments: Sequence[str], maximum: int,
        *, write: bool = False,
    ) -> AudioCommandResult:
        if not isinstance(executable, str) or not posixpath.isabs(executable):
            raise AudioProviderUnavailable(
                "Linux PipeWire playback-stream audio is unavailable on this host"
            )
        timeout = COMMAND_TIMEOUT_SECONDS
        if self._dispatch_deadline is not None:
            remaining = self._dispatch_deadline - time.monotonic()
            if remaining <= 0:
                raise AudioCommandTimedOut("PipeWire audio control timed out")
            timeout = min(timeout, remaining)
        try:
            result = self._runner(tuple(arguments), timeout, maximum)
        except (subprocess.TimeoutExpired, AudioCommandTimedOut) as error:
            raise AudioCommandTimedOut("PipeWire audio control timed out") from error
        except _CommandOutputLimit as error:
            failure = AudioVolumeWriteFailed if write else AudioCommandFailed
            raise failure(
                "PipeWire audio control returned too much data") from error
        except (OSError, subprocess.SubprocessError) as error:
            raise AudioProviderUnavailable(
                "PipeWire audio control could not be started") from error
        if (
            self._dispatch_deadline is not None
            and time.monotonic() >= self._dispatch_deadline
        ):
            raise AudioCommandTimedOut("PipeWire audio control timed out")
        if (
            type(result) is not AudioCommandResult
            or type(result.returncode) is not int
            or not isinstance(result.stdout, bytes)
            or not isinstance(result.stderr, bytes)
            or len(result.stdout) > maximum
            or len(result.stderr) > maximum
        ):
            failure = AudioVolumeWriteFailed if write else AudioCommandFailed
            raise failure(
                "PipeWire audio control returned an invalid response")
        if result.returncode:
            details = (result.stdout + b"\n" + result.stderr).decode(
                "utf-8", errors="replace").casefold()
            if any(marker in details for marker in _PERMISSION_MARKERS):
                raise AudioPermissionDenied(
                    "Permission to control PipeWire audio was denied")
            if any(marker in details for marker in _UNAVAILABLE_MARKERS):
                raise AudioProviderUnavailable(
                    "PipeWire audio is unavailable in this desktop session")
            if write:
                raise AudioVolumeWriteFailed(
                    "PipeWire rejected the requested application volume")
            raise AudioCommandFailed("PipeWire audio control failed")
        return result

    def _graph(self) -> tuple[_PipeWireNode, ...]:
        result = self._invoke(
            self.pw_dump,
            (self.pw_dump or "pw-dump",),
            MAX_GRAPH_OUTPUT_BYTES,
        )
        try:
            graph = json.loads(
                result.stdout.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            )
            _check_json_depth(graph)
        except (
            UnicodeError, json.JSONDecodeError, ValueError, TypeError,
            MemoryError, RecursionError,
        ) as error:
            raise AudioCommandFailed(
                "PipeWire discovery returned an invalid graph") from error
        if not isinstance(graph, list) or len(graph) > MAX_PIPEWIRE_OBJECTS:
            raise AudioCommandFailed("PipeWire discovery returned an invalid graph")

        nodes: list[_PipeWireNode] = []
        for item in graph:
            if not isinstance(item, dict):
                raise AudioCommandFailed(
                    "PipeWire discovery returned an invalid graph")
            if item.get("type") != _NODE_TYPE:
                continue
            info = item.get("info")
            if not isinstance(info, dict):
                raise AudioCommandFailed(
                    "A PipeWire node has an invalid description")
            properties = info.get("props")
            if not isinstance(properties, dict):
                raise AudioCommandFailed(
                    "A PipeWire node has invalid properties")
            if properties.get("media.class") != _PLAYBACK_CLASS:
                continue
            node_id = item.get("id")
            serial = properties.get("object.serial")
            if (
                type(node_id) is not int
                or not 0 <= node_id <= 0xFFFFFFFF
                or type(serial) is not int
                or not 0 <= serial <= 0xFFFFFFFFFFFFFFFF
            ):
                raise AudioCommandFailed(
                    "A PipeWire playback stream has an invalid identity")
            session_id = _stable_identity(properties)
            if session_id is None:
                raise AudioCommandFailed(
                    "A PipeWire playback stream has no stable application identity")
            nodes.append(_PipeWireNode(
                node_id=node_id,
                serial=serial,
                session_id=session_id,
                label=_node_label(properties, session_id),
            ))
            if len(nodes) > MAX_AUDIO_SESSIONS:
                raise AmbiguousAudioSessions(
                    f"More than {MAX_AUDIO_SESSIONS} playback streams are active")

        nodes.sort(key=lambda node: (node.session_id, node.node_id, node.serial))
        return tuple(nodes)

    @staticmethod
    def _duplicate_session_ids(
        nodes: tuple[_PipeWireNode, ...],
    ) -> set[str]:
        return {
            node.session_id for index, node in enumerate(nodes[1:], 1)
            if node.session_id == nodes[index - 1].session_id
        }

    def available_sessions(self) -> tuple[AudioSessionInfo, ...]:
        with self._bounded_dispatch():
            nodes = self._graph()
            if self._duplicate_session_ids(nodes):
                raise AmbiguousAudioSessions(
                    "More than one active stream has the same stable "
                    "application identity"
                )
        return tuple(
            AudioSessionInfo(node.session_id, node.label) for node in nodes
        )

    def _select(self, nodes: tuple[_PipeWireNode, ...]) -> _PipeWireNode:
        if self.preferred_session is not None:
            matching = tuple(
                node for node in nodes
                if node.session_id == self.preferred_session
            )
            if not matching:
                raise NoAudioSession(
                    "The selected application has no active playback stream")
            if len(matching) != 1:
                raise AmbiguousAudioSessions(
                    "The selected application has more than one active "
                    "playback stream"
                )
            return matching[0]
        if not nodes:
            raise NoAudioSession("No application playback stream is active")
        if len(nodes) != 1:
            raise AmbiguousAudioSessions(
                "Choose an application before controlling multiple playback streams")
        return nodes[0]

    def _volume(self, node_id: int) -> int:
        result = self._invoke(
            self.wpctl,
            (self.wpctl or "wpctl", "get-volume", str(node_id)),
            MAX_CONTROL_OUTPUT_BYTES,
        )
        try:
            text = result.stdout.decode("ascii")
            match = _VOLUME_OUTPUT.fullmatch(text)
            if match is None:
                raise ValueError
            scalar = Decimal(match.group(1))
            percent = scalar * 100
            rounded = int(percent.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        except (UnicodeError, ValueError, InvalidOperation) as error:
            raise AudioCommandFailed(
                "PipeWire returned an invalid application volume") from error
        if not Decimal(0) <= scalar <= Decimal(1):
            raise AudioCommandFailed(
                "PipeWire application volume is outside the supported 0..100 range")
        if abs(percent - Decimal(rounded)) > Decimal("0.01"):
            raise AudioCommandFailed(
                "PipeWire returned a non-integral application volume")
        return rounded

    def _revalidate(self, original: _PipeWireNode) -> _PipeWireNode:
        matching = tuple(
            node for node in self._graph()
            if node.session_id == original.session_id
        )
        if not matching:
            raise AudioSessionChanged(
                "The selected application stream closed before the volume write")
        if len(matching) != 1:
            raise AudioSessionChanged(
                "The selected application stream became ambiguous before the "
                "volume write"
            )
        current = matching[0]
        if (
            current.node_id != original.node_id
            or current.serial != original.serial
        ):
            raise AudioSessionChanged(
                "The selected application stream changed before the volume write")
        return current

    def read_volume(self) -> AudioVolumeState:
        with self._bounded_dispatch():
            node = self._select(self._graph())
            volume = self._volume(node.node_id)
        return AudioVolumeState(volume, self.backend, node.session_id)

    def perform(self, action: MediaVolumeAction) -> AudioVolumeDispatch:
        if not isinstance(action, MediaVolumeAction):
            raise TypeError("Audio-volume action must be a MediaVolumeAction")
        with self._bounded_dispatch():
            node = self._select(self._graph())
            previous = self._volume(node.node_id)
            target = apply_media_volume_action(previous, action)
            self._revalidate(node)
            self._invoke(
                self.wpctl,
                (
                    self.wpctl or "wpctl", "set-volume", "--limit", "1.0",
                    str(node.node_id), f"{target}%",
                ),
                MAX_CONTROL_OUTPUT_BYTES,
                write=True,
            )
            try:
                # A successful wpctl exit is only command acceptance.  Confirm
                # that its transient node ID still names the same object before
                # treating the following scalar read as this application's
                # volume.
                self._revalidate(node)
                confirmed = self._volume(node.node_id)
            except (AudioCommandTimedOut, AudioProviderUnavailable,
                    AudioPermissionDenied):
                raise
            except AudioCommandFailed as error:
                raise AudioVolumeWriteFailed(
                    "The application volume write could not be verified") from error
            if confirmed != target:
                raise AudioVolumeWriteFailed(
                    "The application volume did not match the requested value")
        return AudioVolumeDispatch(
            action, previous, confirmed, self.backend, node.session_id)


class UnavailableAudioProvider:
    """Explicit platform boundary for unsupported host audio APIs."""

    backend = "unavailable"

    @staticmethod
    def _unavailable():
        raise AudioProviderUnavailable(
            "Per-application audio volume is not supported on this platform")

    def available_sessions(self) -> tuple[AudioSessionInfo, ...]:
        return self._unavailable()

    def read_volume(self) -> AudioVolumeState:
        return self._unavailable()

    def perform(self, action: MediaVolumeAction) -> AudioVolumeDispatch:
        if not isinstance(action, MediaVolumeAction):
            raise TypeError("Audio-volume action must be a MediaVolumeAction")
        return self._unavailable()


def create_host_audio_provider(
    *,
    system: str | None = None,
    preferred_session: str | None = None,
    find_executable: Callable[[str], str | None] = shutil.which,
    runner: CommandRunner = _run_bounded_command,
) -> HostAudioProvider:
    """Construct a platform adapter without running a provider command."""

    detected = platform.system() if system is None else system
    if not isinstance(detected, str):
        raise TypeError("Host platform name must be text")
    if detected == "Linux":
        return PipeWireAudioProvider(
            preferred_session=preferred_session,
            find_executable=find_executable,
            runner=runner,
        )
    if preferred_session is not None:
        validate_audio_session_id(preferred_session)
    return UnavailableAudioProvider()


def available_audio_sessions(
    *,
    system: str | None = None,
    find_executable: Callable[[str], str | None] = shutil.which,
    runner: CommandRunner = _run_bounded_command,
) -> tuple[AudioSessionInfo, ...]:
    """Discover selectable sessions without touching USB or changing volume."""

    return create_host_audio_provider(
        system=system,
        find_executable=find_executable,
        runner=runner,
    ).available_sessions()
