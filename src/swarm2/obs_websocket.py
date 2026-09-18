"""Bounded local OBS WebSocket client for source-mapped MC7 actions."""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
import errno
import hashlib
import importlib
import json
import ntpath
import os
from .file_io import open_regular_read
from pathlib import Path
import platform
import stat
import time
from types import MappingProxyType
import uuid

from .transport import DeviceError


OBS_WEBSOCKET_DEFAULT_PORT = 4455
OBS_WEBSOCKET_RPC_VERSION = 1
OBS_WEBSOCKET_ACTION_TIMEOUT_SECONDS = 2.0
OBS_WEBSOCKET_MAX_CONFIG_BYTES = 64 * 1024
OBS_WEBSOCKET_MAX_MESSAGE_BYTES = 64 * 1024
OBS_WEBSOCKET_MAX_JSON_DEPTH = 8
OBS_WEBSOCKET_MAX_JSON_NODES = 256
OBS_SCREENSHOT_HOTKEY = "OBSBasic.Screenshot"
OBS_STUDIO_MODE_VERIFY_TIMEOUT_SECONDS = 1.0


@dataclass(frozen=True)
class ObsWebSocketConfiguration:
    """One enabled local OBS server; its password must never be logged."""

    port: int = OBS_WEBSOCKET_DEFAULT_PORT
    password: str = field(default="", repr=False)
    source_path: Path | None = None

    def __post_init__(self) -> None:
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("OBS WebSocket port must be 1..65535")
        if not isinstance(self.password, str) or len(self.password) > 1024:
            raise ValueError("OBS WebSocket password is invalid")
        if any(0xD800 <= ord(character) <= 0xDFFF for character in self.password):
            raise ValueError("OBS WebSocket password is invalid")
        if self.source_path is not None and not isinstance(self.source_path, Path):
            raise ValueError("OBS WebSocket configuration path is invalid")


@dataclass(frozen=True)
class ObsWebSocketRequestResult:
    request_type: str
    status_code: int
    response_data: Mapping[str, object] = field(
        default_factory=dict,
        repr=False,
    )

    def __post_init__(self) -> None:
        if (
            not isinstance(self.request_type, str)
            or not self.request_type
            or len(self.request_type) > 128
            or not self.request_type.isascii()
            or type(self.status_code) is not int
        ):
            raise ValueError("OBS WebSocket request result is invalid")
        if type(self.response_data) is not dict:
            raise ValueError("OBS WebSocket responseData must be a JSON object")
        try:
            encoded = json.dumps(
                self.response_data,
                ensure_ascii=True,
                separators=(",", ":"),
            )
            copied = _bounded_json_object(encoded, "OBS WebSocket responseData")
        except DeviceError as error:
            raise ValueError(str(error)) from error
        except (TypeError, ValueError, UnicodeError, RecursionError) as error:
            raise ValueError(
                "OBS WebSocket responseData must be bounded JSON"
            ) from error
        object.__setattr__(self, "response_data", _freeze_json(copied))


@dataclass(frozen=True)
class ObsStudioModeToggleResult:
    old_enabled: bool
    new_enabled: bool

    def __post_init__(self) -> None:
        if (
            type(self.old_enabled) is not bool
            or type(self.new_enabled) is not bool
            or self.old_enabled == self.new_enabled
        ):
            raise ValueError(
                "OBS Studio mode result must contain one verified state change"
            )


def obs_websocket_config_paths(
    *,
    system: str | None = None,
    home: str | Path | None = None,
    environ: Mapping[str, str] = os.environ,
) -> tuple[Path, ...]:
    """Return deterministic native/Flatpak OBS configuration candidates."""

    detected = platform.system() if system is None else system
    if not isinstance(detected, str):
        raise DeviceError("Host platform name is invalid")
    root = Path.home() if home is None else Path(home)
    if not root.is_absolute():
        raise DeviceError("The user home directory must be absolute")
    relative = Path("plugin_config") / "obs-websocket" / "config.json"
    if detected == "Linux":
        configured = environ.get("XDG_CONFIG_HOME", "")
        native_root = (
            Path(configured) if configured and Path(configured).is_absolute()
            else root / ".config"
        )
        candidates = (
            native_root / "obs-studio" / relative,
            root / ".var" / "app" / "com.obsproject.Studio" / "config"
            / "obs-studio" / relative,
        )
    elif detected == "Darwin":
        candidates = (
            root / "Library" / "Application Support" / "obs-studio" / relative,
        )
    elif detected == "Windows":
        configured = environ.get("APPDATA", "")
        native_root = (
            Path(configured) if configured and ntpath.isabs(configured)
            else root / "AppData" / "Roaming"
        )
        candidates = (native_root / "obs-studio" / relative,)
    else:
        raise DeviceError("OBS controls require Linux, macOS or Windows")
    return tuple(dict.fromkeys(candidates))


def _json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate field {key!r}")
        value[key] = item
    return value


def _invalid_json_constant(value):
    raise ValueError(f"invalid number {value}")


def _freeze_json(value):
    """Return a deeply immutable copy of one already-bounded JSON value."""

    if isinstance(value, dict):
        return MappingProxyType({
            key: _freeze_json(item) for key, item in value.items()
        })
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _bounded_json_object(raw: str, label: str) -> dict:
    if not isinstance(raw, str):
        raise DeviceError(f"{label} is too large")
    try:
        encoded_size = len(raw.encode("utf-8"))
    except UnicodeError as error:
        raise DeviceError(f"{label} is not valid bounded JSON") from error
    if encoded_size > OBS_WEBSOCKET_MAX_MESSAGE_BYTES:
        raise DeviceError(f"{label} is too large")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_json_object,
            parse_constant=_invalid_json_constant,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError, RecursionError) as error:
        raise DeviceError(f"{label} is not valid bounded JSON") from error
    stack = [(value, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if depth > OBS_WEBSOCKET_MAX_JSON_DEPTH or nodes > OBS_WEBSOCKET_MAX_JSON_NODES:
            raise DeviceError(f"{label} is too deeply nested")
        if isinstance(item, dict):
            for key, child in item.items():
                try:
                    key.encode("utf-8")
                except UnicodeError as error:
                    raise DeviceError(
                        f"{label} is not valid bounded JSON"
                    ) from error
                stack.append((child, depth + 1))
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            try:
                item.encode("utf-8")
            except UnicodeError as error:
                raise DeviceError(
                    f"{label} is not valid bounded JSON"
                ) from error
    if not isinstance(value, dict):
        raise DeviceError(f"{label} must be a JSON object")
    return value


def _read_obs_configuration(path: Path) -> tuple[bool, ObsWebSocketConfiguration] | None:
    descriptor = None
    try:
        descriptor = open_regular_read(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.EMLINK):
            raise DeviceError(
                "The local OBS WebSocket configuration is not a bounded regular file"
            ) from error
        raise DeviceError(
            "The local OBS WebSocket configuration could not be opened"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > OBS_WEBSOCKET_MAX_CONFIG_BYTES
        ):
            raise DeviceError(
                "The local OBS WebSocket configuration is not a bounded regular file"
            )
        try:
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = None
                raw = stream.read(OBS_WEBSOCKET_MAX_CONFIG_BYTES + 1)
        except OSError as error:
            raise DeviceError(
                "The local OBS WebSocket configuration could not be read"
            ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(raw) > OBS_WEBSOCKET_MAX_CONFIG_BYTES:
        raise DeviceError("The local OBS WebSocket configuration is too large")
    try:
        text = raw.decode("utf-8")
    except UnicodeError as error:
        raise DeviceError("The local OBS WebSocket configuration is not UTF-8") from error
    value = _bounded_json_object(text, "The local OBS WebSocket configuration")
    enabled = value.get("server_enabled")
    auth_required = value.get("auth_required")
    port = value.get("server_port", OBS_WEBSOCKET_DEFAULT_PORT)
    password = value.get("server_password", "")
    if type(enabled) is not bool or type(auth_required) is not bool:
        raise DeviceError("The local OBS WebSocket configuration has invalid server flags")
    if type(port) is not int or not 1 <= port <= 65535:
        raise DeviceError("The local OBS WebSocket configuration has an invalid port")
    if not isinstance(password, str) or len(password) > 1024:
        raise DeviceError("The local OBS WebSocket configuration has an invalid password")
    if auth_required and not password:
        raise DeviceError("OBS WebSocket authentication is enabled without a password")
    try:
        configuration = ObsWebSocketConfiguration(
            port=port,
            password=password if auth_required else "",
            source_path=path,
        )
    except ValueError as error:
        raise DeviceError(str(error)) from error
    return enabled, configuration


def discover_obs_websocket_configuration(
    *,
    paths: Iterable[str | Path] | None = None,
    system: str | None = None,
    home: str | Path | None = None,
    environ: Mapping[str, str] = os.environ,
) -> ObsWebSocketConfiguration:
    """Load one enabled same-user OBS server without exposing its password."""

    candidates = (
        tuple(Path(item) for item in paths)
        if paths is not None
        else obs_websocket_config_paths(
            system=system, home=home, environ=environ
        )
    )
    if not candidates or len(candidates) > 8:
        raise DeviceError("OBS WebSocket configuration locations are invalid")
    enabled = []
    saw_disabled = False
    errors = []
    for path in candidates:
        if not path.is_absolute():
            errors.append(DeviceError("An OBS WebSocket configuration path is not absolute"))
            continue
        try:
            result = _read_obs_configuration(path)
        except DeviceError as error:
            errors.append(error)
            continue
        if result is None:
            continue
        is_enabled, configuration = result
        if is_enabled:
            enabled.append(configuration)
        else:
            saw_disabled = True
    if not enabled:
        if errors:
            raise errors[0]
        if saw_disabled:
            raise DeviceError(
                "Enable the WebSocket server in OBS: Tools → WebSocket Server Settings"
            )
        raise DeviceError(
            "Open OBS once, then enable Tools → WebSocket Server Settings"
        )
    selected = enabled[0]
    if any(
        item.port != selected.port or item.password != selected.password
        for item in enabled[1:]
    ):
        raise DeviceError("More than one enabled local OBS WebSocket configuration was found")
    return selected


def compute_obs_websocket_authentication(
    password: str, salt: str, challenge: str
) -> str:
    """Create the obs-websocket 5.x challenge response."""

    if not all(isinstance(item, str) for item in (password, salt, challenge)):
        raise ValueError("OBS WebSocket authentication fields must be strings")
    try:
        secret = base64.b64encode(
            hashlib.sha256((password + salt).encode("utf-8")).digest()
        ).decode("ascii")
        return base64.b64encode(
            hashlib.sha256((secret + challenge).encode("utf-8")).digest()
        ).decode("ascii")
    except UnicodeError as error:
        raise ValueError(
            "OBS WebSocket authentication fields must be valid Unicode"
        ) from error


class _WebSocketClientAdapter:
    """Map third-party client failures to stable, printable device errors."""

    def __init__(self, connection):
        self.connection = connection

    def settimeout(self, timeout):
        try:
            self.connection.settimeout(timeout)
        except Exception as error:
            raise DeviceError("OBS WebSocket timeout could not be set") from error

    def recv(self):
        try:
            return self.connection.recv()
        except Exception as error:
            raise DeviceError("OBS WebSocket did not return a response in time") from error

    def send(self, payload):
        try:
            return self.connection.send(payload)
        except Exception as error:
            raise DeviceError("OBS WebSocket request could not be sent") from error

    def close(self):
        try:
            self.connection.close()
        except Exception:
            pass


def _open_obs_websocket(endpoint: str, timeout: float):
    try:
        websocket = importlib.import_module("websocket")
    except ModuleNotFoundError as error:
        raise DeviceError(
            "OBS controls require the MC7 Studio GUI dependencies"
        ) from error
    try:
        connection = websocket.create_connection(
            endpoint,
            timeout=timeout,
            subprotocols=["obswebsocket.json"],
            origin="http://127.0.0.1",
            http_no_proxy=["127.0.0.1", "localhost", "::1"],
        )
    except Exception as error:
        raise DeviceError(
            "Cannot connect to the local OBS WebSocket server"
        ) from error
    return _WebSocketClientAdapter(connection)


def _remaining(deadline: float, clock: Callable[[], float]) -> float:
    value = deadline - clock()
    if value <= 0:
        raise DeviceError("OBS WebSocket action timed out")
    return value


def _receive(connection, deadline, clock, label):
    connection.settimeout(_remaining(deadline, clock))
    raw = connection.recv()
    if not isinstance(raw, str):
        raise DeviceError(f"{label} must use a JSON text frame")
    return _bounded_json_object(raw, label)


def _send(connection, value):
    try:
        payload = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise DeviceError("OBS WebSocket request could not be encoded") from error
    if len(payload.encode("ascii")) > OBS_WEBSOCKET_MAX_MESSAGE_BYTES:
        raise DeviceError("OBS WebSocket request is too large")
    connection.send(payload)


def execute_obs_websocket_request(
    request_type: str,
    request_data: Mapping[str, object],
    *,
    configuration: ObsWebSocketConfiguration | None = None,
    configuration_loader: Callable[[], ObsWebSocketConfiguration] = (
        discover_obs_websocket_configuration
    ),
    connection_factory: Callable[[str, float], object] = _open_obs_websocket,
    request_id_factory: Callable[[], object] = uuid.uuid4,
    clock: Callable[[], float] = time.monotonic,
    timeout_seconds: float = OBS_WEBSOCKET_ACTION_TIMEOUT_SECONDS,
) -> ObsWebSocketRequestResult:
    """Authenticate and execute one fixed local obs-websocket 5.x request."""

    if (
        not isinstance(request_type, str)
        or not request_type
        or len(request_type) > 128
        or not request_type.isascii()
        or type(request_data) is not dict
    ):
        raise ValueError("OBS WebSocket request is invalid")
    if (
        type(timeout_seconds) not in (int, float)
        or isinstance(timeout_seconds, bool)
        or not 0 < timeout_seconds <= OBS_WEBSOCKET_ACTION_TIMEOUT_SECONDS
    ):
        raise ValueError(
            "OBS WebSocket timeout must be positive and within the action limit"
        )
    config = configuration_loader() if configuration is None else configuration
    if not isinstance(config, ObsWebSocketConfiguration):
        raise DeviceError("OBS WebSocket configuration loader returned invalid data")
    try:
        config.__post_init__()
    except ValueError as error:
        raise DeviceError(str(error)) from error
    endpoint = f"ws://127.0.0.1:{config.port}"
    deadline = clock() + timeout_seconds
    try:
        connection = connection_factory(endpoint, _remaining(deadline, clock))
    except DeviceError:
        raise
    except Exception as error:
        raise DeviceError("Cannot connect to the local OBS WebSocket server") from error
    if not all(callable(getattr(connection, method, None))
               for method in ("settimeout", "recv", "send", "close")):
        close = getattr(connection, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        raise DeviceError("OBS WebSocket connection factory returned invalid data")
    try:
        hello = _receive(connection, deadline, clock, "OBS WebSocket Hello")
        hello_data = hello.get("d")
        if hello.get("op") != 0 or not isinstance(hello_data, dict):
            raise DeviceError("OBS WebSocket did not send a valid Hello message")
        server_rpc = hello_data.get("rpcVersion")
        if type(server_rpc) is not int or server_rpc < OBS_WEBSOCKET_RPC_VERSION:
            raise DeviceError("OBS WebSocket does not support RPC version 1")
        identify_data = {
            "rpcVersion": OBS_WEBSOCKET_RPC_VERSION,
            "eventSubscriptions": 0,
        }
        authentication = hello_data.get("authentication")
        if authentication is not None:
            if not isinstance(authentication, dict):
                raise DeviceError("OBS WebSocket sent an invalid authentication challenge")
            salt = authentication.get("salt")
            challenge = authentication.get("challenge")
            if (
                not config.password
                or not isinstance(salt, str)
                or not isinstance(challenge, str)
                or not salt
                or not challenge
                or len(salt) > 1024
                or len(challenge) > 1024
            ):
                raise DeviceError("OBS WebSocket authentication configuration is invalid")
            identify_data["authentication"] = compute_obs_websocket_authentication(
                config.password, salt, challenge
            )
        _send(connection, {"op": 1, "d": identify_data})
        identified = _receive(
            connection, deadline, clock, "OBS WebSocket Identified response"
        )
        identified_data = identified.get("d")
        if (
            identified.get("op") != 2
            or not isinstance(identified_data, dict)
            or identified_data.get("negotiatedRpcVersion")
            != OBS_WEBSOCKET_RPC_VERSION
        ):
            raise DeviceError("OBS WebSocket authentication or identification failed")
        request_id = str(request_id_factory())
        if (
            not request_id
            or len(request_id) > 128
            or not request_id.isascii()
            or not request_id.isprintable()
        ):
            raise DeviceError("OBS WebSocket request ID is invalid")
        _send(connection, {
            "op": 6,
            "d": {
                "requestType": request_type,
                "requestId": request_id,
                "requestData": dict(request_data),
            },
        })
        response = _receive(
            connection, deadline, clock, "OBS WebSocket request response"
        )
        response_data = response.get("d")
        if (
            response.get("op") != 7
            or not isinstance(response_data, dict)
            or response_data.get("requestType") != request_type
            or response_data.get("requestId") != request_id
        ):
            raise DeviceError("OBS WebSocket returned a mismatched request response")
        status = response_data.get("requestStatus")
        if not isinstance(status, dict):
            raise DeviceError("OBS WebSocket returned an invalid request status")
        result, code = status.get("result"), status.get("code")
        if type(result) is not bool or type(code) is not int:
            raise DeviceError("OBS WebSocket returned an invalid request status")
        if not result or code != 100:
            comment = status.get("comment")
            detail = (
                comment
                if isinstance(comment, str)
                and 0 < len(comment) <= 512
                and comment.isprintable()
                else "OBS rejected the request"
            )
            raise DeviceError(f"OBS WebSocket request failed: {detail}")
        raw_result_data = response_data.get("responseData", {})
        if type(raw_result_data) is not dict:
            raise DeviceError(
                "OBS WebSocket returned invalid responseData"
            )
        try:
            return ObsWebSocketRequestResult(
                request_type,
                code,
                raw_result_data,
            )
        except ValueError as error:  # defensive; the enclosing frame is bounded
            raise DeviceError(
                "OBS WebSocket returned invalid responseData"
            ) from error
    finally:
        connection.close()


def execute_obs_screenshot_request(**kwargs) -> ObsWebSocketRequestResult:
    """Trigger the original app-3407 OBS screenshot hotkey."""

    return execute_obs_websocket_request(
        "TriggerHotkeyByName",
        {"hotkeyName": OBS_SCREENSHOT_HOTKEY},
        **kwargs,
    )


def get_obs_studio_mode_enabled(
    *, request_executor=None, **kwargs
) -> bool:
    """Return OBS's exact boolean Studio Mode state."""

    executor = execute_obs_websocket_request if request_executor is None else request_executor
    if not callable(executor):
        raise DeviceError("OBS Studio mode request provider is invalid")
    result = executor("GetStudioModeEnabled", {}, **kwargs)
    if (
        not isinstance(result, ObsWebSocketRequestResult)
        or result.request_type != "GetStudioModeEnabled"
        or result.status_code != 100
        or set(result.response_data) != {"studioModeEnabled"}
        or type(result.response_data.get("studioModeEnabled")) is not bool
    ):
        raise DeviceError(
            "OBS WebSocket returned an invalid Studio Mode state"
        )
    return result.response_data["studioModeEnabled"]


def set_obs_studio_mode_enabled(
    enabled: bool, *, request_executor=None, **kwargs
) -> ObsWebSocketRequestResult:
    """Request one exact Studio Mode state."""

    if type(enabled) is not bool:
        raise ValueError("OBS Studio mode state must be true or false")
    executor = execute_obs_websocket_request if request_executor is None else request_executor
    if not callable(executor):
        raise DeviceError("OBS Studio mode request provider is invalid")
    result = executor(
        "SetStudioModeEnabled",
        {"studioModeEnabled": enabled},
        **kwargs,
    )
    if (
        not isinstance(result, ObsWebSocketRequestResult)
        or result.request_type != "SetStudioModeEnabled"
        or result.status_code != 100
        or result.response_data
    ):
        raise DeviceError(
            "OBS WebSocket returned an invalid Studio Mode update result"
        )
    return result


def execute_obs_studio_mode_toggle_request(
    *,
    request_executor=None,
    **kwargs,
) -> ObsStudioModeToggleResult:
    """Toggle Studio Mode and verify it with exactly one follow-up read."""

    executor = execute_obs_websocket_request if request_executor is None else request_executor
    if not callable(executor):
        raise DeviceError("OBS Studio mode request provider is invalid")
    if "timeout_seconds" in kwargs:
        raise ValueError("OBS Studio mode toggle manages its request timeout")
    old_enabled = get_obs_studio_mode_enabled(
        request_executor=executor,
        **kwargs,
    )
    requested = not old_enabled
    set_obs_studio_mode_enabled(
        requested,
        request_executor=executor,
        **kwargs,
    )
    # obs-websocket waits for its queued OBS UI task before acknowledging Set,
    # so one separately bounded Get is the complete verification step.
    observed = get_obs_studio_mode_enabled(
        request_executor=executor,
        timeout_seconds=OBS_STUDIO_MODE_VERIFY_TIMEOUT_SECONDS,
        **kwargs,
    )
    if observed != requested:
        raise DeviceError(
            "OBS Studio mode did not reach the requested state"
        )
    return ObsStudioModeToggleResult(old_enabled, observed)
