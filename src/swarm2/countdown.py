"""Persistent MC7 LCD-action helper and event-preserving USB pump.

The helper owns both vendor interfaces while enabled. Its first JSON line must
be an explicit start request; a later ``{"command":"stop"}`` line (or EOF)
ends the session. It changes transient LCD values only and never edits stored
mouse settings.
"""

from collections import deque
from dataclasses import dataclass
import json
import os
import platform
import re
import select
from .pipe_io import pipe_readable
import sys
import time

from .countdown_commands import COUNTDOWN_RECORD_COMMAND
from .countdown_runtime import (
    CountdownBinding,
    GeneralMediaBinding,
    LcdActionRuntime,
)
from .display_commands import decode_profile_response
from .general_media_commands import GENERAL_MEDIA_RECORD_COMMAND
from .lcd_commands import decode_lcd_response
from .host_actions import HostActionBinding
from .host_actions import encode_host_action_record, same_host_action_record
from .obs_actions import (
    ObsLaunchBinding,
    ObsScreenshotBinding,
    ObsStudioModeBinding,
)
from .obs_studio_mode_commands import OBS_STUDIO_MODE_RECORD_COMMAND
from .protocol import AckStatus, decode_acknowledgement
from .screen_key_commands import decode_screen_key_responses
from .settings import read_raw, stable
from .transport import DeviceError, HidrawTransport


# A full settings baseline can include bounded raw macro snapshots. Keep the
# helper protocol large enough for that existing snapshot shape while still
# placing a firm cap on pipe memory.
MAX_CONTROLLER_COMMAND_BYTES = 128 * 1024
MAX_CONTROLLER_JSON_DEPTH = 8
MAX_CONTROLLER_JSON_NODES = 512

# Interface 1 multiplexes the eight-byte vendor report with ordinary keyboard,
# consumer-control and system-control reports.  Linux hidraw and macOS hidapi
# include the report ID in these lengths.  Ignore only the exact descriptor
# shapes; an unknown or malformed frame remains terminal.
_UNRELATED_INPUT_REPORT_BYTES = {
    0x07: 9,
    0x08: 16,
    0x0C: 3,
    0x0D: 2,
}
_MAX_CONSECUTIVE_UNRELATED_INPUT_REPORTS = 128


@dataclass(frozen=True)
class PreparedCountdownRequest:
    device_id: str
    profile_index: int
    transport_identity: str
    expected_profile: object
    expected_lcd: object
    bindings: tuple[CountdownBinding, ...]
    host_action_bindings: tuple[HostActionBinding, ...] = ()
    general_media_bindings: tuple[GeneralMediaBinding, ...] = ()
    expected_screen_keys: object | None = None
    preferred_media_player: str | None = None
    obs_launch_bindings: tuple[ObsLaunchBinding, ...] = ()
    obs_screenshot_bindings: tuple[ObsScreenshotBinding, ...] = ()
    obs_studio_mode_bindings: tuple[ObsStudioModeBinding, ...] = ()


class _RequestValidationMediaProvider:
    """Structural runtime seam; request validation never dispatches host input."""

    def perform(self, _action):  # pragma: no cover - unreachable by construction
        raise AssertionError("Request validation cannot dispatch media actions")

    def read_state(self):  # pragma: no cover - unreachable by construction
        raise AssertionError("Request validation cannot read host media state")


_REQUEST_VALIDATION_MEDIA_PROVIDER = _RequestValidationMediaProvider()


def validate_countdown_request(request) -> PreparedCountdownRequest:
    if not isinstance(request, dict) or request.get("command") != "start":
        raise DeviceError("An explicit countdown start request is required")
    allowed = {"command", "device_id", "profile_slot", "baseline", "bindings",
               "host_action_bindings", "general_media_bindings",
               "obs_launch_bindings", "obs_screenshot_bindings",
               "obs_studio_mode_bindings",
               "preferred_media_player"}
    if set(request) - allowed:
        raise DeviceError("Countdown start request contains unknown fields")
    device_id = request.get("device_id")
    if not isinstance(device_id, str) or not device_id or len(device_id) > 1024:
        raise DeviceError("Choose one connected USB mouse for countdown timers")
    slot = request.get("profile_slot")
    if type(slot) is not int or not 1 <= slot <= 5:
        raise DeviceError("Countdown profile slot must be 1..5")
    baseline = request.get("baseline")
    if (not isinstance(baseline, dict)
            or baseline.get("device_id") != device_id
            or baseline.get("profile_slot") != slot):
        raise DeviceError("Read this mouse profile before starting countdown timers")
    transport_identity = baseline.get("transport_identity", device_id)
    if (isinstance(transport_identity, str)
            and (not transport_identity or len(transport_identity) > 1024)):
        raise DeviceError("Countdown baseline has an invalid USB identity")
    if not isinstance(transport_identity, str) and not (
            type(transport_identity) is int
            and 0 < transport_identity <= 0xFFFFFFFF):
        raise DeviceError("Countdown baseline has an invalid USB identity")
    try:
        settings = baseline["settings"]
        expected_profile = decode_profile_response(bytes.fromhex(settings["profile"]))
        expected_lcd = decode_lcd_response(bytes.fromhex(settings["lcd"]), slot - 1)
    except (KeyError, TypeError, ValueError) as error:
        raise DeviceError("Read the active profile and LCD layout before starting countdown timers") from error
    if expected_profile.current_profile != slot - 1:
        raise DeviceError("Countdown timers require the active mouse profile")
    raw_bindings = request.get("bindings", [])
    raw_host_bindings = request.get("host_action_bindings", [])
    raw_media_bindings = request.get("general_media_bindings", [])
    raw_obs_bindings = request.get("obs_launch_bindings", [])
    raw_obs_screenshot_bindings = request.get("obs_screenshot_bindings", [])
    raw_obs_studio_mode_bindings = request.get(
        "obs_studio_mode_bindings", [])
    preferred_media_player = request.get("preferred_media_player")
    if (not isinstance(raw_bindings, list)
            or not isinstance(raw_host_bindings, list)
            or not isinstance(raw_media_bindings, list)
            or not isinstance(raw_obs_bindings, list)
            or not isinstance(raw_obs_screenshot_bindings, list)
            or not isinstance(raw_obs_studio_mode_bindings, list)
            or not 1 <= (len(raw_bindings) + len(raw_host_bindings)
                         + len(raw_media_bindings)
                         + len(raw_obs_bindings)
                         + len(raw_obs_screenshot_bindings)
                         + len(raw_obs_studio_mode_bindings)) <= 12):
        raise DeviceError("Configure between one and twelve LCD action positions")
    if preferred_media_player is not None:
        try:
            from .media_player_ids import validate_media_player_id
            preferred_media_player = validate_media_player_id(
                preferred_media_player)
        except ValueError as error:
            raise DeviceError(str(error)) from error
        if not raw_media_bindings:
            raise DeviceError(
                "A preferred media player requires a General Media binding")
    bindings = []
    host_bindings = []
    media_bindings = []
    obs_bindings = []
    obs_screenshot_bindings = []
    obs_studio_mode_bindings = []
    expected_screen_keys = None
    try:
        for item in raw_bindings:
            if not isinstance(item, dict) or set(item) != {
                    "timer_id", "page_index", "slot_index", "duration_seconds"}:
                raise ValueError("Countdown binding has missing or unknown fields")
            binding = CountdownBinding(**item)
            if binding.page_index >= min(3, expected_lcd.page_count):
                raise ValueError("Countdown binding addresses a disabled LCD page")
            widget = expected_lcd.pages[binding.page_index].slots[binding.slot_index]
            if widget is None or widget.signature != (0x46, 0):
                raise ValueError("Countdown binding does not match the stored LCD tile")
            bindings.append(binding)
        for item in raw_host_bindings:
            required_host_fields = {
                "widget_key", "page_index", "slot_index", "target"}
            if (not isinstance(item, dict)
                    or set(item) not in (
                        required_host_fields,
                        required_host_fields | {"icon_index"})):
                raise ValueError("Host-action binding has missing or unknown fields")
            binding = HostActionBinding(**item)
            if binding.page_index >= min(3, expected_lcd.page_count):
                raise ValueError("Host-action binding addresses a disabled LCD page")
            widget = expected_lcd.pages[binding.page_index].slots[binding.slot_index]
            from .lcd_commands import LCD_WIDGETS
            if (widget is None
                    or widget.signature != LCD_WIDGETS[binding.widget_key].signature):
                raise ValueError("Host-action binding does not match the stored LCD tile")
            host_bindings.append(binding)
        for item in raw_media_bindings:
            if (not isinstance(item, dict)
                    or set(item) != {"page_index", "slot_index"}):
                raise ValueError(
                    "General Media binding has missing or unknown fields")
            binding = GeneralMediaBinding(**item)
            if binding.page_index >= min(3, expected_lcd.page_count):
                raise ValueError(
                    "General Media binding addresses a disabled LCD page")
            widget = expected_lcd.pages[binding.page_index].slots[
                binding.slot_index]
            if widget is None or widget.signature != (0x45, 0):
                raise ValueError(
                    "General Media binding does not match the stored LCD tile")
            media_bindings.append(binding)
        for item in raw_obs_bindings:
            if (
                not isinstance(item, dict)
                or set(item) != {"page_index", "slot_index"}
            ):
                raise ValueError(
                    "Launch OBS binding has missing or unknown fields"
                )
            binding = ObsLaunchBinding(**item)
            if binding.page_index >= min(3, expected_lcd.page_count):
                raise ValueError(
                    "Launch OBS binding addresses a disabled LCD page"
                )
            widget = expected_lcd.pages[binding.page_index].slots[
                binding.slot_index
            ]
            if widget is None or widget.signature != (0x4B, 0):
                raise ValueError(
                    "Launch OBS binding does not match the stored LCD tile"
                )
            obs_bindings.append(binding)
        for item in raw_obs_screenshot_bindings:
            if (
                not isinstance(item, dict)
                or set(item) != {"page_index", "slot_index"}
            ):
                raise ValueError(
                    "OBS Screenshot binding has missing or unknown fields"
                )
            binding = ObsScreenshotBinding(**item)
            if binding.page_index >= min(3, expected_lcd.page_count):
                raise ValueError(
                    "OBS Screenshot binding addresses a disabled LCD page"
                )
            widget = expected_lcd.pages[binding.page_index].slots[
                binding.slot_index
            ]
            if widget is None or widget.signature != (0x43, 0x07):
                raise ValueError(
                    "OBS Screenshot binding does not match the stored LCD tile"
                )
            obs_screenshot_bindings.append(binding)
        for item in raw_obs_studio_mode_bindings:
            if (
                not isinstance(item, dict)
                or set(item) != {"page_index", "slot_index"}
            ):
                raise ValueError(
                    "OBS Studio Mode binding has missing or unknown fields"
                )
            binding = ObsStudioModeBinding(**item)
            if binding.page_index >= min(3, expected_lcd.page_count):
                raise ValueError(
                    "OBS Studio Mode binding addresses a disabled LCD page"
                )
            widget = expected_lcd.pages[binding.page_index].slots[
                binding.slot_index
            ]
            if widget is None or widget.signature != (0x43, 0x09):
                raise ValueError(
                    "OBS Studio Mode binding does not match the stored LCD tile"
                )
            obs_studio_mode_bindings.append(binding)
        if host_bindings:
            try:
                expected_screen_keys = decode_screen_key_responses(
                    bytes.fromhex(settings["screen_keys"]), slot - 1)
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(
                    "Read the LCD key definitions before starting host actions") from error
            for binding in host_bindings:
                record = expected_screen_keys.pages[binding.page_index].records[
                    binding.slot_index]
                requested = encode_host_action_record(
                    binding.widget_key, binding.target,
                    icon_index=binding.icon_index)
                if not same_host_action_record(
                        record, requested, binding.widget_key):
                    raise ValueError(
                        "Host-action binding does not match the stored LCD key definition")
        # Reuse the runtime's identity, coordinate, and cross-binding checks.
        LcdActionRuntime(
            bindings, host_bindings, object(),
            general_media_bindings=media_bindings,
            obs_launch_bindings=obs_bindings,
            obs_screenshot_bindings=obs_screenshot_bindings,
            obs_studio_mode_bindings=obs_studio_mode_bindings,
            media_provider=_REQUEST_VALIDATION_MEDIA_PROVIDER)
    except (TypeError, ValueError) as error:
        raise DeviceError(str(error)) from error
    return PreparedCountdownRequest(
        device_id=device_id,
        profile_index=slot - 1,
        transport_identity=str(transport_identity),
        expected_profile=expected_profile,
        expected_lcd=expected_lcd,
        bindings=tuple(bindings),
        host_action_bindings=tuple(host_bindings),
        general_media_bindings=tuple(media_bindings),
        obs_launch_bindings=tuple(obs_bindings),
        expected_screen_keys=expected_screen_keys,
        preferred_media_player=preferred_media_player,
        obs_screenshot_bindings=tuple(obs_screenshot_bindings),
        obs_studio_mode_bindings=tuple(obs_studio_mode_bindings),
    )


class CountdownEventTransport:
    """Preserve LCD touches while correlating serialized feature ACKs."""

    def __init__(self, transport, *, clock=time.monotonic):
        self.transport, self.clock = transport, clock
        self.pending = deque()
        self.acknowledgements = deque(maxlen=128)
        # Retain metadata only.  Standard keyboard payloads can contain typed
        # keys and must never be copied into diagnostics.
        self.ignored_input_reports = deque(maxlen=128)

    def _read_raw_event(self, timeout=0):
        deadline = self.clock() + timeout
        wait = timeout
        for _ in range(_MAX_CONSECUTIVE_UNRELATED_INPUT_REPORTS):
            event = bytes(self.transport.read_event(wait))
            if not event:
                return event
            report_id = event[0]
            if report_id == 0x10:
                if len(event) != 8:
                    raise DeviceError(
                        f"MC7 returned an invalid vendor input report "
                        f"(ID 0x10, {len(event)} bytes)")
                return event
            expected = _UNRELATED_INPUT_REPORT_BYTES.get(report_id)
            if expected is None or len(event) != expected:
                raise DeviceError(
                    f"MC7 returned an unexpected HID input report "
                    f"(ID 0x{report_id:02x}, {len(event)} bytes)")
            self.ignored_input_reports.append({
                "report_id": f"0x{report_id:02x}",
                "report_bytes": len(event),
            })
            wait = max(0, deadline - self.clock())
        raise DeviceError("MC7 input queue contained too many unrelated HID reports")

    def _queue(self, event):
        # Old acknowledgements cannot describe the feature write that has not
        # happened yet. Preserve every non-ACK event for the runtime.
        if event[0] == 0x10 and event[2] == 0xF2:
            return
        if len(self.pending) >= 128:
            raise DeviceError("Countdown input queue did not settle")
        self.pending.append(event)

    @staticmethod
    def _validate_report(report):
        if not isinstance(report, bytes) or len(report) != 64 or report[0] != 0x10:
            raise DeviceError("Countdown helper requires a complete MC7 feature report")
        if report[1] == 0x1C:
            selector = report[3]
            common_invalid = report[2] != 0 or any(report[7:])
            screen_key_invalid = (selector == 0x29 and (
                not 0 <= report[4] <= 4
                or not 1 <= report[5] <= 3
                or report[6] != 0))
            ordinary_invalid = (
                (selector == 0x12 and any(report[4:7]))
                or (selector == 0x25 and (
                    not 0 <= report[4] <= 4 or any(report[5:7]))))
            if (common_invalid or selector not in (0x12, 0x25, 0x29)
                    or screen_key_invalid or ordinary_invalid):
                raise DeviceError("Countdown helper rejected an unrelated settings read")
        elif report[1] == 0xA3:
            count = report[3]
            used = 4 + count * 9
            commands = {
                report[4 + index * 9 + 2]
                for index in range(count)
            } if 1 <= count <= 6 else set()
            countdown_batch = commands == {COUNTDOWN_RECORD_COMMAND}
            studio_mode_batch = (
                1 <= count <= 6
                and commands == {OBS_STUDIO_MODE_RECORD_COMMAND}
                and all(
                    1 <= report[4 + index * 9] <= 3
                    and 0 <= report[5 + index * 9] <= 3
                    and report[7 + index * 9] in (0, 1)
                    and report[8 + index * 9:13 + index * 9] == bytes(5)
                    for index in range(count)
                )
            )
            general_media_update = (
                count == 1
                and commands == {GENERAL_MEDIA_RECORD_COMMAND}
                and 1 <= report[4] <= 3
                and 0 <= report[5] <= 3
                and report[7] in (0, 1)
                and report[8] == 0
                and report[9] in (0, 1)
                and report[10:12] == bytes(2)
                and report[12] in (0, 1)
            )
            if (report[2] != 0 or not 1 <= count <= 6 or used > 64
                    or not (
                        countdown_batch
                        or general_media_update
                        or studio_mode_batch
                    )
                    or any(report[used:])):
                raise DeviceError("Countdown helper rejected an invalid A3 batch")
        else:
            raise DeviceError("Countdown helper rejected an unrelated feature command")

    def send(self, report):
        self._validate_report(report)
        for _ in range(128):
            event = self._read_raw_event(0)
            if not event:
                break
            self._queue(event)
        else:
            raise DeviceError("Countdown input queue did not settle before a feature write")
        count = self.transport.write_feature(report)
        if count != 64:
            raise DeviceError("Countdown feature transfer was incomplete")
        deadline = self.clock() + 2
        while self.clock() < deadline:
            event = self._read_raw_event(min(0.05, max(0, deadline - self.clock())))
            if not event:
                continue
            if event[0] == 0x10 and event[2:4] == bytes((0xF2, report[1])):
                ack = decode_acknowledgement(event, target="mouse")
                self.acknowledgements.append(event.hex())
                if ack.status == AckStatus.ACCEPTED:
                    return
                if ack.status != AckStatus.BUSY:
                    raise DeviceError(
                        f"Mouse rejected countdown command with status {ack.status_code}")
            else:
                self._queue(event)
        raise DeviceError("Countdown command acknowledgement timed out")

    def get_feature(self, selector):
        return self.transport.get_feature(selector)

    def read_event(self, timeout=0):
        if self.pending:
            return self.pending.popleft()
        return self._read_raw_event(timeout)


class CountdownGuard:
    def __init__(self, prepared, transport):
        self.prepared, self.transport = prepared, transport

    def __call__(self):
        profile = read_raw(self.transport, "profile", self.prepared.profile_index)
        current_profile = decode_profile_response(profile)
        if current_profile.current_profile != self.prepared.profile_index:
            raise DeviceError("The active mouse profile changed; countdown timers stopped")
        if stable("profile", profile) != stable("profile", self.prepared.expected_profile.raw):
            raise DeviceError("The mouse profile state changed; read it again before restarting countdown timers")
        lcd = read_raw(self.transport, "lcd", self.prepared.profile_index)
        if decode_lcd_response(lcd, self.prepared.profile_index).signature != self.prepared.expected_lcd.signature:
            raise DeviceError("The LCD layout changed; read it again before restarting countdown timers")
        expected_screen_keys = getattr(
            self.prepared, "expected_screen_keys", None)
        if expected_screen_keys is not None:
            screen_keys = read_raw(
                self.transport, "screen_keys", self.prepared.profile_index)
            if (stable("screen_keys", screen_keys)
                    != stable("screen_keys", expected_screen_keys.raw)):
                raise DeviceError(
                    "The LCD key definitions changed; read them again before restarting host actions")


class CountdownCommandReader:
    def __init__(self, fd):
        self.fd, self.buffer, self.eof = fd, bytearray(), False

    def read(self, timeout=0):
        if (type(timeout) not in (int, float) or isinstance(timeout, bool)
                or not 0 <= timeout <= 5):
            raise DeviceError("Countdown controller timeout must be between 0 and 5 seconds")
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer and not self.eof:
            wait = 0 if timeout == 0 else max(0, deadline - time.monotonic())
            if not pipe_readable(self.fd, wait):
                break
            chunk = os.read(self.fd, MAX_CONTROLLER_COMMAND_BYTES + 1)
            if not chunk:
                self.eof = True
            self.buffer.extend(chunk)
            if len(self.buffer) > MAX_CONTROLLER_COMMAND_BYTES:
                raise DeviceError("Countdown controller command is too large")
        if b"\n" not in self.buffer:
            if self.eof and self.buffer:
                raise DeviceError("Countdown controller sent an incomplete command")
            return None
        line, _, rest = self.buffer.partition(b"\n")
        self.buffer = bytearray(rest)
        def object_pairs(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise DeviceError(
                        f"Countdown controller command contains duplicate field {key!r}")
                value[key] = item
            return value

        def invalid_constant(value):
            raise DeviceError(
                f"Countdown controller command contains invalid number {value}")

        try:
            value = json.loads(
                line.decode("utf-8"), object_pairs_hook=object_pairs,
                parse_constant=invalid_constant)
        except DeviceError:
            raise
        except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
            raise DeviceError("Countdown controller command is not valid bounded JSON") from error
        stack = [(value, 0)]
        nodes = 0
        while stack:
            item, depth = stack.pop()
            nodes += 1
            if (depth > MAX_CONTROLLER_JSON_DEPTH
                    or nodes > MAX_CONTROLLER_JSON_NODES):
                raise DeviceError("Countdown controller command is too deeply nested")
            if isinstance(item, dict):
                stack.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                stack.extend((child, depth + 1) for child in item)
        if not isinstance(value, dict):
            raise DeviceError("Countdown controller command must be an object")
        return value


def _transport_factory(device_id):
    if platform.system() == "Linux":
        return HidrawTransport(device_id)
    if platform.system() == "Darwin":
        from .macos import MacOSTransport
        return MacOSTransport(device_id)
    if platform.system() == "Windows":
        from .windows import WindowsTransport
        return WindowsTransport(device_id)
    raise DeviceError("Countdown timers require a USB-connected mouse on Linux, macOS or Windows")


def main():
    def emit(value):
        print(json.dumps(value), flush=True)

    runtime = None
    try:
        reader = CountdownCommandReader(sys.stdin.fileno())
        initial = reader.read(5)
        prepared = validate_countdown_request(initial)
        media_provider = None
        if prepared.general_media_bindings:
            from .host_media import create_host_media_provider
            media_provider = create_host_media_provider(
                preferred_player=prepared.preferred_media_player)
        with _transport_factory(prepared.device_id) as raw_transport:
            identity = str(getattr(raw_transport, "location_id", prepared.device_id))
            if identity != prepared.transport_identity:
                raise DeviceError("The mouse USB location changed; read it again")
            transport = CountdownEventTransport(raw_transport)
            runtime = LcdActionRuntime(
                prepared.bindings, prepared.host_action_bindings, transport, emit,
                general_media_bindings=prepared.general_media_bindings,
                obs_launch_bindings=prepared.obs_launch_bindings,
                obs_screenshot_bindings=prepared.obs_screenshot_bindings,
                obs_studio_mode_bindings=prepared.obs_studio_mode_bindings,
                media_provider=media_provider,
                guard=CountdownGuard(prepared, transport))
            runtime.start()
            while True:
                command = reader.read()
                if command is not None:
                    if command != {"command": "stop"}:
                        raise DeviceError("Only Stop is accepted by a running countdown helper")
                    break
                if reader.eof:
                    break
                event = transport.read_event(0.05)
                if event:
                    runtime.observe(event)
                runtime.advance()
            runtime.stop()
        emit({"type": "result", "outcome": "stopped", "verified": True,
              "display_may_be_stale": False})
        return 0
    except (OSError, ValueError, DeviceError, KeyError, TypeError) as error:
        # Never issue a second A3 write after an ambiguous transfer failure.
        emit({"type": "result", "outcome": "error", "verified": False,
              "display_may_be_stale": bool(
                  runtime and runtime.display_may_be_stale),
              "error": str(error)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
