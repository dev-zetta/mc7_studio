"""Protocol and provider tests for the MC7 OBS Studio Mode tile."""

import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from swarm2.countdown import CountdownEventTransport, validate_countdown_request
from swarm2.countdown_runtime import LcdActionRuntime
from swarm2.lcd_commands import (
    LCD_WIDGETS,
    build_lcd_report,
    decode_lcd_response,
    edited_lcd_signature,
)
from swarm2.obs_actions import (
    ObsStudioModeBinding,
    execute_obs_studio_mode,
)
from swarm2.obs_commands import decode_obs_studio_mode_touch
from swarm2.obs_studio_mode_commands import (
    ObsStudioModeDisplayUpdate,
    build_obs_studio_mode_reports,
)
from swarm2.obs_websocket import (
    OBS_STUDIO_MODE_VERIFY_TIMEOUT_SECONDS,
    OBS_WEBSOCKET_ACTION_TIMEOUT_SECONDS,
    ObsStudioModeToggleResult,
    ObsWebSocketConfiguration,
    ObsWebSocketRequestResult,
    execute_obs_studio_mode_toggle_request,
    execute_obs_websocket_request,
    get_obs_studio_mode_enabled,
    set_obs_studio_mode_enabled,
)
from swarm2.protocol import ProtocolError
from swarm2.transport import DeviceError
from tests.test_settings import CAPTURES, with_lcd_page


OBS_STUDIO_MODE_PRESS = bytes.fromhex("1033134309010100")
OBS_STUDIO_MODE_RELEASE = bytes.fromhex("1033134309010000")


def studio_mode_lcd(slot=0):
    signatures = [b"\xfe\x00"] * 4
    signatures[slot] = b"\x43\x09"
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


def websocket_responses(request_type, response_data=None):
    response = {
        "op": 7,
        "d": {
            "requestType": request_type,
            "requestId": "fixture-request",
            "requestStatus": {"result": True, "code": 100},
        },
    }
    if response_data is not None:
        response["d"]["responseData"] = response_data
    return [
        json.dumps({"op": 0, "d": {"rpcVersion": 1}}),
        json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}}),
        json.dumps(response),
    ]


class ObsStudioModeCodecTests(unittest.TestCase):
    def test_static_tile_storage_and_exact_press_release(self):
        self.assertEqual(
            LCD_WIDGETS["obs_studio_mode"].signature,
            (0x43, 0x09),
        )
        self.assertEqual(LCD_WIDGETS["obs_studio_mode"].width, 1)
        state = decode_lcd_response(bytes.fromhex(CAPTURES["lcd"]), 0)
        edits = {0: ["obs_studio_mode", "empty", "empty", "empty"]}
        report = build_lcd_report(state, pages=edits)
        self.assertEqual(report[5:16].hex(), "010000fe00fe00fe004309")
        response = bytearray(report[:61])
        response[2] = 0
        decoded = decode_lcd_response(response, 0)
        self.assertEqual(
            decoded.signature,
            edited_lcd_signature(state, pages=edits),
        )
        self.assertEqual(
            [item.key for item in decoded.pages[0].slots],
            ["obs_studio_mode", "empty", "empty", "empty"],
        )
        press = decode_obs_studio_mode_touch(OBS_STUDIO_MODE_PRESS)
        release = decode_obs_studio_mode_touch(OBS_STUDIO_MODE_RELEASE)
        self.assertEqual(
            (press.page_index, press.slot_index, press.pressed),
            (0, 0, True),
        )
        self.assertEqual(
            (release.page_index, release.slot_index, release.pressed),
            (0, 0, False),
        )
        other = decode_obs_studio_mode_touch(
            bytes.fromhex("1033214309010100")
        )
        self.assertEqual((other.page_index, other.slot_index), (1, 2))

    def test_decoder_rejects_bad_coordinates_and_ignores_mismatches(self):
        for malformed in (
            bytes(7),
            bytes.fromhex("1033034309010100"),
            bytes.fromhex("1033144309010100"),
        ):
            with self.subTest(malformed=malformed), self.assertRaises(
                ProtocolError
            ):
                decode_obs_studio_mode_touch(malformed)
        for unrelated in (
            bytes.fromhex("1033134308010100"),
            bytes.fromhex("1033134309020100"),
            bytes.fromhex("1033134309010200"),
            bytes.fromhex("1033134309010101"),
            bytes.fromhex("1033134307010100"),
        ):
            with self.subTest(unrelated=unrelated):
                self.assertIsNone(decode_obs_studio_mode_touch(unrelated))

    def test_exact_a3_state_records_sort_and_batch_six_at_a_time(self):
        updates = [
            ObsStudioModeDisplayUpdate(page, slot, (page + slot) % 2 == 0)
            for page in reversed(range(3))
            for slot in reversed(range(4))
        ]
        reports = build_obs_studio_mode_reports(updates)
        self.assertEqual(len(reports), 2)
        self.assertTrue(all(len(report) == 64 for report in reports))
        self.assertEqual(reports[0][:4], bytes.fromhex("10a30006"))
        self.assertEqual(reports[1][:4], bytes.fromhex("10a30006"))
        records = b"".join(
            report[4:4 + report[3] * 9] for report in reports
        )
        expected = b"".join(
            bytes((page + 1, 3 - slot, 0x04))
            + int((page + slot) % 2 == 0).to_bytes(2, "little")
            + bytes(4)
            for page in range(3)
            for slot in range(4)
        )
        self.assertEqual(records, expected)
        self.assertTrue(all(
            report[4 + report[3] * 9:] == bytes(60 - report[3] * 9)
            for report in reports
        ))

    def test_a3_encoder_rejects_bad_types_coordinates_states_and_duplicates(self):
        for value in (None, b"", "", object(), [object()], [None] * 13):
            with self.subTest(value=value), self.assertRaises(ProtocolError):
                build_obs_studio_mode_reports(value)
        for values in (
            (-1, 0, False),
            (3, 0, False),
            (0, -1, False),
            (0, 4, False),
            (0, 0, 0),
        ):
            with self.subTest(values=values), self.assertRaises(ProtocolError):
                ObsStudioModeDisplayUpdate(*values)
        update = ObsStudioModeDisplayUpdate(0, 0, False)
        with self.assertRaisesRegex(ProtocolError, "only once"):
            build_obs_studio_mode_reports([update, update])
        self.assertEqual(build_obs_studio_mode_reports([]), ())


class ObsStudioModeProviderTests(unittest.TestCase):
    def test_response_data_is_bounded_copied_and_deeply_immutable(self):
        source = {"state": {"history": [False, True]}}
        result = ObsWebSocketRequestResult("Fixture", 100, source)
        source["state"]["history"].append(False)
        self.assertEqual(result.response_data["state"]["history"], (False, True))
        with self.assertRaises(TypeError):
            result.response_data["state"] = {}
        with self.assertRaises(TypeError):
            result.response_data["state"]["other"] = True
        self.assertNotIn("history", repr(result))

    def test_request_carries_bounded_response_data_and_keeps_loopback_endpoint(self):
        connection = FakeConnection(websocket_responses(
            "GetStudioModeEnabled", {"studioModeEnabled": False}
        ))
        factory = Mock(return_value=connection)
        result = execute_obs_websocket_request(
            "GetStudioModeEnabled",
            {},
            configuration=ObsWebSocketConfiguration(port=4455),
            connection_factory=factory,
            request_id_factory=lambda: "fixture-request",
        )
        self.assertEqual(result.response_data["studioModeEnabled"], False)
        self.assertEqual(factory.call_args.args[0], "ws://127.0.0.1:4455")
        self.assertTrue(connection.closed)
        sent = json.loads(connection.sent[-1])
        self.assertEqual(sent["d"]["requestType"], "GetStudioModeEnabled")
        self.assertEqual(sent["d"]["requestData"], {})

    def test_request_rejects_non_object_response_data(self):
        responses = websocket_responses("GetStudioModeEnabled")
        response = json.loads(responses[-1])
        response["d"]["responseData"] = []
        connection = FakeConnection([*responses[:-1], json.dumps(response)])
        with self.assertRaisesRegex(DeviceError, "invalid responseData"):
            execute_obs_websocket_request(
                "GetStudioModeEnabled",
                {},
                configuration=ObsWebSocketConfiguration(),
                connection_factory=lambda *_args: connection,
                request_id_factory=lambda: "fixture-request",
            )
        self.assertTrue(connection.closed)

    def test_generic_request_timeout_is_validated_and_reaches_connection(self):
        for invalid in (
            True,
            0,
            -1,
            OBS_WEBSOCKET_ACTION_TIMEOUT_SECONDS + 0.01,
        ):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValueError, "within the action limit"
            ):
                execute_obs_websocket_request(
                    "Fixture",
                    {},
                    configuration=ObsWebSocketConfiguration(),
                    timeout_seconds=invalid,
                )
        connection = FakeConnection(websocket_responses("Fixture"))
        execute_obs_websocket_request(
            "Fixture",
            {},
            configuration=ObsWebSocketConfiguration(),
            connection_factory=lambda *_args: connection,
            request_id_factory=lambda: "fixture-request",
            clock=lambda: 0.0,
            timeout_seconds=0.25,
        )
        self.assertTrue(connection.timeouts)
        self.assertTrue(all(0 < value <= 0.25 for value in connection.timeouts))

    def test_typed_get_rejects_missing_non_boolean_and_extra_data(self):
        invalid = (
            {},
            {"studioModeEnabled": 1},
            {"studioModeEnabled": False, "unexpected": True},
        )
        for data in invalid:
            with self.subTest(data=data), self.assertRaisesRegex(
                DeviceError, "invalid Studio Mode state"
            ):
                get_obs_studio_mode_enabled(
                    request_executor=lambda *_args, value=data, **_kwargs:
                    ObsWebSocketRequestResult(
                        "GetStudioModeEnabled", 100, value
                    )
                )

    def test_typed_set_uses_exact_boolean_request(self):
        calls = []

        def execute(request_type, request_data, **kwargs):
            calls.append((request_type, request_data, kwargs))
            return ObsWebSocketRequestResult(request_type, 100)

        result = set_obs_studio_mode_enabled(
            True,
            request_executor=execute,
            configuration=ObsWebSocketConfiguration(),
        )
        self.assertEqual(result.request_type, "SetStudioModeEnabled")
        self.assertEqual(calls[0][0:2], (
            "SetStudioModeEnabled", {"studioModeEnabled": True}
        ))
        with self.assertRaisesRegex(ValueError, "true or false"):
            set_obs_studio_mode_enabled(1, request_executor=execute)
        with self.assertRaisesRegex(DeviceError, "invalid Studio Mode update"):
            set_obs_studio_mode_enabled(
                False,
                request_executor=lambda *_args, **_kwargs:
                ObsWebSocketRequestResult(
                    "SetStudioModeEnabled",
                    100,
                    {"unexpected": True},
                ),
            )

    def test_toggle_gets_sets_inverse_then_verifies_once(self):
        states = iter((False, True))
        calls = []

        def execute(request_type, request_data, **kwargs):
            calls.append((request_type, request_data, kwargs))
            if request_type == "GetStudioModeEnabled":
                return ObsWebSocketRequestResult(
                    request_type,
                    100,
                    {"studioModeEnabled": next(states)},
                )
            return ObsWebSocketRequestResult(request_type, 100)

        result = execute_obs_studio_mode_toggle_request(
            request_executor=execute,
        )
        self.assertEqual(result, ObsStudioModeToggleResult(False, True))
        self.assertEqual(calls, [
            ("GetStudioModeEnabled", {}, {}),
            ("SetStudioModeEnabled", {"studioModeEnabled": True}, {}),
            ("GetStudioModeEnabled", {}, {
                "timeout_seconds": OBS_STUDIO_MODE_VERIFY_TIMEOUT_SECONDS,
            }),
        ])

    def test_no_op_update_fails_after_one_bounded_verification(self):
        calls = []

        def execute(request_type, request_data, **kwargs):
            calls.append((request_type, request_data, kwargs))
            if request_type == "GetStudioModeEnabled":
                return ObsWebSocketRequestResult(
                    request_type, 100, {"studioModeEnabled": False}
                )
            return ObsWebSocketRequestResult(request_type, 100)

        with self.assertRaisesRegex(DeviceError, "did not reach"):
            execute_obs_studio_mode_toggle_request(
                request_executor=execute,
            )
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[-1], (
            "GetStudioModeEnabled",
            {},
            {"timeout_seconds": OBS_STUDIO_MODE_VERIFY_TIMEOUT_SECONDS},
        ))


class ObsStudioModeRuntimeTests(unittest.TestCase):
    def runtime(self, *, initial=False, executor=None, transport=None,
                bindings=None, messages=None, guard=None):
        return LcdActionRuntime(
            [],
            [],
            transport or SimpleNamespace(send=Mock()),
            (messages if messages is not None else []).append,
            obs_studio_mode_bindings=(
                bindings
                if bindings is not None
                else [ObsStudioModeBinding(0, 0)]
            ),
            obs_studio_mode_state_reader=Mock(return_value=initial),
            obs_studio_mode_executor=(
                executor
                if executor is not None
                else Mock(return_value=ObsStudioModeToggleResult(False, True))
            ),
            guard=guard or Mock(),
        )

    def test_binding_wrapper_requires_and_returns_verified_toggle(self):
        binding = ObsStudioModeBinding(0, 0)
        result = ObsStudioModeToggleResult(False, True)
        executor = Mock(return_value=result)
        self.assertIs(
            execute_obs_studio_mode(binding, request_executor=executor),
            result,
        )
        executor.assert_called_once_with()
        with self.assertRaisesRegex(DeviceError, "validated LCD binding"):
            execute_obs_studio_mode(object(), request_executor=executor)
        with self.assertRaisesRegex(DeviceError, "invalid data"):
            execute_obs_studio_mode(binding, request_executor=lambda: object())

    def test_start_gets_state_and_writes_all_positions_in_sorted_batches(self):
        bindings = [
            ObsStudioModeBinding(page, slot)
            for page in reversed(range(3))
            for slot in reversed(range(4))
        ]
        transport = SimpleNamespace(send=Mock())
        guard = Mock()
        messages = []
        runtime = self.runtime(
            initial=True,
            transport=transport,
            bindings=bindings,
            messages=messages,
            guard=guard,
        )
        runtime.start()
        self.assertEqual(transport.send.call_count, 2)
        self.assertEqual(
            [call.args[0] for call in transport.send.call_args_list],
            list(build_obs_studio_mode_reports([
                ObsStudioModeDisplayUpdate(page, slot, True)
                for page in range(3)
                for slot in range(4)
            ])),
        )
        self.assertEqual(guard.call_count, 4)
        self.assertEqual(messages[-1], {
            "type": "ready",
            "timers": 0,
            "positions": 0,
            "host_actions": 12,
        })

    def test_toggle_updates_display_once_and_repeated_press_is_suppressed(self):
        transport = SimpleNamespace(send=Mock())
        guard = Mock()
        messages = []
        executor = Mock(return_value=ObsStudioModeToggleResult(False, True))
        runtime = self.runtime(
            transport=transport,
            executor=executor,
            messages=messages,
            guard=guard,
        )
        runtime.start()
        self.assertEqual(
            transport.send.call_args.args[0],
            bytes.fromhex("10a30001010304000000000000") + bytes(51),
        )
        transport.send.reset_mock()
        guard.reset_mock()

        self.assertTrue(runtime.observe(OBS_STUDIO_MODE_PRESS))
        self.assertTrue(runtime.observe(OBS_STUDIO_MODE_PRESS))
        executor.assert_called_once_with(ObsStudioModeBinding(0, 0))
        self.assertEqual(transport.send.call_count, 1)
        self.assertEqual(
            transport.send.call_args.args[0],
            bytes.fromhex("10a30001010304010000000000") + bytes(51),
        )
        self.assertEqual(guard.call_count, 3)
        self.assertEqual(messages[-1], {
            "type": "host_action",
            "event": "toggled",
            "widget": "obs_studio_mode",
            "page_index": 0,
            "slot_index": 0,
            "old_enabled": False,
            "new_enabled": True,
        })
        self.assertTrue(runtime.observe(OBS_STUDIO_MODE_RELEASE))

    def test_websocket_failure_is_recoverable_without_state_write(self):
        transport = SimpleNamespace(send=Mock())
        messages = []
        executor = Mock(side_effect=DeviceError("Enable OBS WebSocket"))
        runtime = self.runtime(
            transport=transport,
            executor=executor,
            messages=messages,
        )
        runtime.start()
        transport.send.reset_mock()
        self.assertTrue(runtime.observe(OBS_STUDIO_MODE_PRESS))
        self.assertEqual(transport.send.call_count, 0)
        self.assertEqual(messages[-1], {
            "type": "host_action",
            "event": "failed",
            "widget": "obs_studio_mode",
            "page_index": 0,
            "slot_index": 0,
            "error": "Enable OBS WebSocket",
        })
        self.assertTrue(runtime.observe(OBS_STUDIO_MODE_PRESS))
        self.assertEqual(executor.call_count, 1)

    def test_post_toggle_transport_failure_is_terminal_and_marks_stale(self):
        transport = SimpleNamespace(send=Mock())
        runtime = self.runtime(transport=transport)
        runtime.start()
        transport.send.side_effect = DeviceError("ambiguous A3 transfer")
        with self.assertRaisesRegex(DeviceError, "ambiguous A3"):
            runtime.observe(OBS_STUDIO_MODE_PRESS)
        self.assertTrue(runtime.display_may_be_stale)

    def test_runtime_rejects_duplicate_positions_and_invalid_provider_state(self):
        binding = ObsStudioModeBinding(0, 0)
        with self.assertRaisesRegex(ValueError, "configured once"):
            self.runtime(bindings=[binding, binding])
        runtime = self.runtime(initial=1)
        with self.assertRaisesRegex(DeviceError, "invalid state"):
            runtime.start()

    def test_request_requires_exact_stored_tile_without_screen_key_data(self):
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
                    "lcd": studio_mode_lcd().hex(),
                },
            },
            "obs_studio_mode_bindings": [
                {"page_index": 0, "slot_index": 0}
            ],
        }
        prepared = validate_countdown_request(request)
        self.assertEqual(
            prepared.obs_studio_mode_bindings,
            (ObsStudioModeBinding(0, 0),),
        )
        self.assertIsNone(prepared.expected_screen_keys)
        request["obs_studio_mode_bindings"][0]["slot_index"] = 1
        with self.assertRaisesRegex(DeviceError, "stored LCD tile"):
            validate_countdown_request(request)

    def test_helper_accepts_only_exact_studio_mode_a3_records(self):
        report = build_obs_studio_mode_reports([
            ObsStudioModeDisplayUpdate(2, 1, True),
        ])[0]
        CountdownEventTransport._validate_report(report)
        for offset, value in (
            (3, 2),
            (4, 0),
            (5, 4),
            (7, 2),
            (8, 1),
            (9, 1),
            (12, 1),
            (13, 1),
        ):
            with self.subTest(offset=offset):
                malformed = bytearray(report)
                malformed[offset] = value
                with self.assertRaisesRegex(DeviceError, "invalid A3"):
                    CountdownEventTransport._validate_report(bytes(malformed))

    def test_studio_mode_a3_waits_through_busy_for_exact_accepted_ack(self):
        report = build_obs_studio_mode_reports([
            ObsStudioModeDisplayUpdate(0, 0, True),
        ])[0]

        class RawTransport:
            def __init__(self):
                self.written = []
                self.events = [
                    bytes.fromhex("1000f2a301000000"),
                    bytes.fromhex("1000f2a302000000"),
                    bytes.fromhex("1000f2a300000000"),
                ]

            def read_event(self, _timeout=0):
                if not self.written:
                    return b""
                return self.events.pop(0) if self.events else b""

            def write_feature(self, value):
                self.written.append(value)
                return len(value)

        raw = RawTransport()
        transport = CountdownEventTransport(raw)
        transport.send(report)
        self.assertEqual(raw.written, [report])
        self.assertEqual(list(transport.acknowledgements), [
            "1000f2a301000000",
            "1000f2a302000000",
            "1000f2a300000000",
        ])


if __name__ == "__main__":
    unittest.main()
