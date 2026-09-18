"""Protocol, local WebSocket and shared-listener tests for OBS Screenshot."""

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock

from swarm2.countdown import validate_countdown_request
from swarm2.countdown_runtime import LcdActionRuntime
from swarm2.lcd_commands import (
    LCD_WIDGETS,
    build_lcd_report,
    decode_lcd_response,
    edited_lcd_signature,
)
from swarm2.obs_actions import ObsScreenshotBinding, execute_obs_screenshot
from swarm2.obs_commands import decode_obs_screenshot_touch
from swarm2.obs_websocket import (
    OBS_SCREENSHOT_HOTKEY,
    OBS_WEBSOCKET_ACTION_TIMEOUT_SECONDS,
    ObsWebSocketConfiguration,
    compute_obs_websocket_authentication,
    discover_obs_websocket_configuration,
    execute_obs_screenshot_request,
    obs_websocket_config_paths,
)
from swarm2.protocol import ProtocolError
from swarm2.transport import DeviceError
from tests.test_settings import CAPTURES, with_lcd_page


OBS_SCREENSHOT_PRESS = bytes.fromhex("1033134307010100")
OBS_SCREENSHOT_RELEASE = bytes.fromhex("1033134307010000")


def screenshot_lcd(slot=0):
    signatures = [b"\xfe\x00"] * 4
    signatures[slot] = b"\x43\x07"
    return with_lcd_page(
        bytes.fromhex(CAPTURES["lcd"]), 0, tuple(signatures)
    )


class FakeConnection:
    def __init__(self, responses):
        self.responses = list(responses)
        self.sent = []
        self.timeouts = []
        self.closed = False

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def recv(self):
        return self.responses.pop(0)

    def send(self, payload):
        self.sent.append(payload)
        return len(payload)

    def close(self):
        self.closed = True


class InvalidConnection:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class ObsScreenshotCodecTests(unittest.TestCase):
    def test_source_mapped_tile_and_exact_press_release(self):
        self.assertEqual(LCD_WIDGETS["obs_screenshot"].signature, (0x43, 0x07))
        self.assertEqual(LCD_WIDGETS["obs_screenshot"].width, 1)
        state = decode_lcd_response(bytes.fromhex(CAPTURES["lcd"]), 0)
        edits = {0: ["obs_screenshot", "empty", "empty", "empty"]}
        report = build_lcd_report(state, pages=edits)
        self.assertEqual(report[5:16].hex(), "010000fe00fe00fe004307")
        response = bytearray(report[:61])
        response[2] = 0
        decoded = decode_lcd_response(response, 0)
        self.assertEqual(decoded.signature, edited_lcd_signature(state, pages=edits))
        self.assertEqual(
            [item.key for item in decoded.pages[0].slots],
            ["obs_screenshot", "empty", "empty", "empty"],
        )
        press = decode_obs_screenshot_touch(OBS_SCREENSHOT_PRESS)
        release = decode_obs_screenshot_touch(OBS_SCREENSHOT_RELEASE)
        self.assertEqual(
            (press.page_index, press.slot_index, press.pressed), (0, 0, True)
        )
        self.assertEqual(
            (release.page_index, release.slot_index, release.pressed),
            (0, 0, False),
        )
        other = decode_obs_screenshot_touch(
            bytes.fromhex("1033214307010100")
        )
        self.assertEqual((other.page_index, other.slot_index), (1, 2))

    def test_decoder_rejects_coordinates_and_ignores_other_events(self):
        for malformed in (
            bytes(7),
            bytes.fromhex("1033034307010100"),
            bytes.fromhex("1033144307010100"),
        ):
            with self.subTest(malformed=malformed), self.assertRaises(ProtocolError):
                decode_obs_screenshot_touch(malformed)
        for unrelated in (
            bytes.fromhex("1033134306010100"),
            bytes.fromhex("1033134307020100"),
            bytes.fromhex("1033134307010200"),
            bytes.fromhex("1033134307010101"),
            bytes.fromhex("1033134b00010100"),
        ):
            with self.subTest(unrelated=unrelated):
                self.assertIsNone(decode_obs_screenshot_touch(unrelated))


class ObsWebSocketConfigurationTests(unittest.TestCase):
    def write(self, root, name, value):
        path = Path(root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_cross_platform_configuration_paths_are_local_and_deterministic(self):
        home = Path.cwd() / "test-home"
        config = Path.cwd() / "test-config"
        linux = obs_websocket_config_paths(
            system="Linux", home=home, environ={"XDG_CONFIG_HOME": str(config)}
        )
        self.assertEqual(
            linux,
            (
                config / "obs-studio/plugin_config/obs-websocket/config.json",
                home / ".var/app/com.obsproject.Studio/config/obs-studio/plugin_config/obs-websocket/config.json",
            ),
        )
        self.assertEqual(
            obs_websocket_config_paths(system="Darwin", home=home),
            (home / "Library/Application Support/obs-studio/plugin_config/obs-websocket/config.json",),
        )
        self.assertEqual(
            obs_websocket_config_paths(
                system="Windows", home=home,
                environ={"APPDATA": "C:/Users/test/AppData/Roaming"}),
            (Path("C:/Users/test/AppData/Roaming/obs-studio/plugin_config/obs-websocket/config.json"),),
        )

    def test_enabled_configuration_loads_without_leaking_password(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, "obs.json", {
                "server_enabled": True,
                "auth_required": True,
                "server_port": 4456,
                "server_password": "fixture-secret",
                "future_field": "is allowed",
            })
            value = discover_obs_websocket_configuration(paths=[path])
            self.assertEqual(value.port, 4456)
            self.assertEqual(value.password, "fixture-secret")
            self.assertEqual(value.source_path, path)
            self.assertNotIn("fixture-secret", repr(value))

    def test_disabled_missing_ambiguous_and_malformed_configurations_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            disabled = self.write(root, "disabled.json", {
                "server_enabled": False,
                "auth_required": True,
                "server_port": 4455,
                "server_password": "secret",
            })
            with self.assertRaisesRegex(DeviceError, "Enable the WebSocket server"):
                discover_obs_websocket_configuration(paths=[disabled])
            with self.assertRaisesRegex(DeviceError, "Open OBS once"):
                discover_obs_websocket_configuration(paths=[root / "missing.json"])
            other = self.write(root, "other.json", {
                "server_enabled": True,
                "auth_required": False,
                "server_port": 4456,
            })
            enabled = self.write(root, "enabled.json", {
                "server_enabled": True,
                "auth_required": False,
                "server_port": 4455,
            })
            with self.assertRaisesRegex(DeviceError, "More than one enabled"):
                discover_obs_websocket_configuration(paths=[enabled, other])
            duplicate = root / "duplicate.json"
            duplicate.write_text(
                '{"server_enabled":true,"server_enabled":true,'
                '"auth_required":false,"server_port":4455}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(DeviceError, "bounded JSON"):
                discover_obs_websocket_configuration(paths=[duplicate])
            link = root / "link.json"
            link.symlink_to(enabled)
            with self.assertRaisesRegex(DeviceError, "bounded regular file"):
                discover_obs_websocket_configuration(paths=[link])


class ObsWebSocketProtocolTests(unittest.TestCase):
    def responses(self, *, authenticated=False, result=True, code=100):
        hello = {"op": 0, "d": {"rpcVersion": 1}}
        if authenticated:
            hello["d"]["authentication"] = {
                "challenge": "+IxH4CnCiqpX1rM9scsNynZzbOe4KhDeYcTNS3PDaeY=",
                "salt": "lM1GncleQOaCu9lT1yeUZhFYnqhsLLP1G5lAGo3ixaI=",
            }
        return [
            json.dumps(hello),
            json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}}),
            json.dumps({
                "op": 7,
                "d": {
                    "requestType": "TriggerHotkeyByName",
                    "requestId": "fixture-request",
                    "requestStatus": {
                        "result": result,
                        "code": code,
                        **({"comment": "No hotkeys were found"} if not result else {}),
                    },
                },
            }),
        ]

    def test_authentication_vector_uses_the_official_example_fields(self):
        self.assertEqual(
            compute_obs_websocket_authentication(
                "supersecretpassword",
                "lM1GncleQOaCu9lT1yeUZhFYnqhsLLP1G5lAGo3ixaI=",
                "+IxH4CnCiqpX1rM9scsNynZzbOe4KhDeYcTNS3PDaeY=",
            ),
            "1Ct943GAT+6YQUUX47Ia/ncufilbe6+oD6lY+5kaCu4=",
        )
        with self.assertRaisesRegex(ValueError, "valid Unicode"):
            compute_obs_websocket_authentication("secret", chr(0xD800), "challenge")

    def test_exact_authenticated_screenshot_request_and_response(self):
        connection = FakeConnection(self.responses(authenticated=True))
        factory = Mock(return_value=connection)
        result = execute_obs_screenshot_request(
            configuration=ObsWebSocketConfiguration(
                port=4455, password="supersecretpassword"
            ),
            connection_factory=factory,
            request_id_factory=lambda: "fixture-request",
        )
        self.assertEqual(result.request_type, "TriggerHotkeyByName")
        self.assertEqual(result.status_code, 100)
        factory.assert_called_once()
        self.assertEqual(factory.call_args.args[0], "ws://127.0.0.1:4455")
        self.assertLessEqual(
            factory.call_args.args[1], OBS_WEBSOCKET_ACTION_TIMEOUT_SECONDS
        )
        sent = [json.loads(item) for item in connection.sent]
        self.assertEqual(sent[0], {
            "op": 1,
            "d": {
                "rpcVersion": 1,
                "eventSubscriptions": 0,
                "authentication": "1Ct943GAT+6YQUUX47Ia/ncufilbe6+oD6lY+5kaCu4=",
            },
        })
        self.assertEqual(sent[1], {
            "op": 6,
            "d": {
                "requestType": "TriggerHotkeyByName",
                "requestId": "fixture-request",
                "requestData": {"hotkeyName": OBS_SCREENSHOT_HOTKEY},
            },
        })
        self.assertEqual(len(connection.timeouts), 3)
        self.assertTrue(connection.closed)

    def test_protocol_rejects_failed_or_mismatched_responses_and_closes(self):
        failures = []
        failed = FakeConnection(self.responses(result=False, code=600))
        failures.append((failed, "No hotkeys were found"))
        mismatched_messages = self.responses()
        mismatched = json.loads(mismatched_messages[-1])
        mismatched["d"]["requestId"] = "other"
        mismatch = FakeConnection([*mismatched_messages[:-1], json.dumps(mismatched)])
        failures.append((mismatch, "mismatched"))
        binary = FakeConnection([b"not a text frame"])
        failures.append((binary, "JSON text frame"))
        for connection, message in failures:
            with self.subTest(message=message), self.assertRaisesRegex(
                DeviceError, message
            ):
                execute_obs_screenshot_request(
                    configuration=ObsWebSocketConfiguration(),
                    connection_factory=lambda *_args, item=connection: item,
                    request_id_factory=lambda: "fixture-request",
                )
            self.assertTrue(connection.closed)

    def test_protocol_maps_malformed_frames_and_factory_failures(self):
        surrogate = FakeConnection([
            '{"op":0,"d":{"rpcVersion":"\\ud800"}}'
        ])
        with self.assertRaisesRegex(DeviceError, "valid bounded JSON"):
            execute_obs_screenshot_request(
                configuration=ObsWebSocketConfiguration(),
                connection_factory=lambda *_args: surrogate,
            )
        self.assertTrue(surrogate.closed)

        invalid = InvalidConnection()
        with self.assertRaisesRegex(DeviceError, "factory returned invalid"):
            execute_obs_screenshot_request(
                configuration=ObsWebSocketConfiguration(),
                connection_factory=lambda *_args: invalid,
            )
        self.assertTrue(invalid.closed)

        with self.assertRaisesRegex(DeviceError, "Cannot connect") as caught:
            execute_obs_screenshot_request(
                configuration=ObsWebSocketConfiguration(),
                connection_factory=lambda *_args: (_ for _ in ()).throw(
                    OSError("fixture-secret must not escape")
                ),
            )
        self.assertNotIn("fixture-secret", str(caught.exception))


class ObsScreenshotRuntimeTests(unittest.TestCase):
    def test_binding_wrapper_and_runtime_dispatch_exactly_once_per_press(self):
        calls = Mock(return_value=SimpleNamespace(status_code=100))
        binding = ObsScreenshotBinding(0, 0)
        self.assertEqual(
            execute_obs_screenshot(binding, request_executor=calls).status_code,
            100,
        )
        calls.assert_called_once_with()

        guards = Mock()
        executor = Mock()
        messages = []
        runtime = LcdActionRuntime(
            [],
            [],
            SimpleNamespace(send=Mock()),
            messages.append,
            obs_screenshot_bindings=[binding],
            obs_screenshot_executor=executor,
            guard=guards,
        )
        runtime.start()
        self.assertEqual(messages[-1], {
            "type": "ready", "timers": 0, "positions": 0, "host_actions": 1,
        })
        self.assertTrue(runtime.observe(OBS_SCREENSHOT_PRESS))
        self.assertTrue(runtime.observe(OBS_SCREENSHOT_PRESS))
        self.assertEqual(executor.call_count, 1)
        self.assertTrue(runtime.observe(OBS_SCREENSHOT_RELEASE))
        self.assertTrue(runtime.observe(OBS_SCREENSHOT_PRESS))
        self.assertEqual(executor.call_count, 2)
        self.assertEqual(messages[-1], {
            "type": "host_action",
            "event": "triggered",
            "widget": "obs_screenshot",
            "page_index": 0,
            "slot_index": 0,
        })
        runtime.stop()
        self.assertEqual(guards.call_count, 4)

    def test_runtime_reports_recoverable_websocket_failure(self):
        messages = []
        executor = Mock(side_effect=DeviceError("Enable OBS WebSocket"))
        runtime = LcdActionRuntime(
            [], [], SimpleNamespace(send=Mock()), messages.append,
            obs_screenshot_bindings=[ObsScreenshotBinding(0, 0)],
            obs_screenshot_executor=executor,
        )
        runtime.start()
        self.assertTrue(runtime.observe(OBS_SCREENSHOT_PRESS))
        self.assertEqual(messages[-1], {
            "type": "host_action",
            "event": "failed",
            "widget": "obs_screenshot",
            "page_index": 0,
            "slot_index": 0,
            "error": "Enable OBS WebSocket",
        })
        self.assertTrue(runtime.observe(OBS_SCREENSHOT_PRESS))
        self.assertEqual(executor.call_count, 1)

    def test_request_requires_exact_stored_screenshot_tile(self):
        request = {
            "command": "start",
            "device_id": "fixture",
            "profile_slot": 1,
            "baseline": {
                "device_id": "fixture",
                "profile_slot": 1,
                "transport_identity": "fixture-port",
                "settings": {
                    "profile": CAPTURES["profile"],
                    "lcd": screenshot_lcd().hex(),
                },
            },
            "obs_screenshot_bindings": [
                {"page_index": 0, "slot_index": 0}
            ],
        }
        prepared = validate_countdown_request(request)
        self.assertEqual(
            prepared.obs_screenshot_bindings,
            (ObsScreenshotBinding(0, 0),),
        )
        self.assertIsNone(prepared.expected_screen_keys)
        request["obs_screenshot_bindings"][0]["slot_index"] = 1
        with self.assertRaisesRegex(DeviceError, "stored LCD tile"):
            validate_countdown_request(request)


if __name__ == "__main__":
    unittest.main()
