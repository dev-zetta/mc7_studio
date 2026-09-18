"""Countdown helper framing, guards and raw event methods without USB access."""

import json
import os
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch

from swarm2.countdown import (
    MAX_CONTROLLER_COMMAND_BYTES,
    CountdownCommandReader,
    CountdownEventTransport,
    CountdownGuard,
    validate_countdown_request,
)
from swarm2.countdown_commands import CountdownDisplayUpdate, build_countdown_reports
from swarm2.countdown_runtime import CountdownBinding, CountdownRuntime
from swarm2.display_commands import decode_profile_response
from swarm2.general_media_commands import (
    GeneralMediaDisplayState,
    build_general_media_report,
)
from swarm2.lcd_commands import decode_lcd_response
from swarm2.macos import MacOSTransport
from swarm2.transport import DeviceError, HidrawTransport
from tests.test_settings import CAPTURES, with_lcd_page


TOUCH_1 = bytes.fromhex("1033134600000100")
TOUCH_2 = bytes.fromhex("1033214600aa01bb")
ACK_A3 = bytes.fromhex("1000f2a300000000")
OLD_ACK = bytes.fromhex("1000f21c00000000")


def a3_report():
    return build_countdown_reports([
        CountdownDisplayUpdate.sync(
            0, 0, state=0, total_seconds=60, remaining_seconds=60)
    ])[0]


class EventRawTransport:
    def __init__(self):
        self.before = [OLD_ACK, TOUCH_1, b""]
        self.after = [TOUCH_2, ACK_A3]
        self.written = []

    def read_event(self, _timeout=0):
        queue = self.after if self.written else self.before
        return queue.pop(0) if queue else b""

    def write_feature(self, report):
        self.written.append(report)
        return len(report)


class CountdownEventTransportTests(unittest.TestCase):
    def test_exact_general_media_state_report_is_allowed_and_mutations_fail(self):
        report = build_general_media_report(
            GeneralMediaDisplayState(True, False, True),
            page_index=2, logical_slot=1)
        CountdownEventTransport._validate_report(report)
        for offset, value in (
            (3, 2), (4, 0), (5, 4), (7, 2), (8, 1),
            (9, 2), (10, 1), (11, 1), (12, 2), (13, 1),
        ):
            with self.subTest(offset=offset):
                malformed = bytearray(report)
                malformed[offset] = value
                with self.assertRaisesRegex(DeviceError, "invalid A3"):
                    CountdownEventTransport._validate_report(bytes(malformed))

    def test_touches_before_and_during_ack_wait_are_preserved_in_order(self):
        raw = EventRawTransport()
        transport = CountdownEventTransport(raw)

        transport.send(a3_report())

        self.assertEqual(raw.written, [a3_report()])
        self.assertEqual(transport.read_event(), TOUCH_1)
        self.assertEqual(transport.read_event(), TOUCH_2)
        self.assertEqual(transport.read_event(), b"")
        self.assertEqual(list(transport.acknowledgements), [ACK_A3.hex()])

    def test_invalid_raw_event_or_short_feature_write_is_terminal(self):
        raw = EventRawTransport()
        raw.before = [bytes(7)]
        with self.assertRaisesRegex(DeviceError, "unexpected HID input"):
            CountdownEventTransport(raw).send(a3_report())
        self.assertEqual(raw.written, [])

        raw = EventRawTransport()
        raw.before = [b""]
        raw.write_feature = lambda report: 63
        with self.assertRaisesRegex(DeviceError, "incomplete"):
            CountdownEventTransport(raw).send(a3_report())

    def test_descriptor_known_standard_reports_are_ignored_without_payload_logging(self):
        raw = EventRawTransport()
        raw.before = [
            bytes((0x07,)) + bytes(8),
            bytes((0x08,)) + bytes(15),
            bytes((0x0C,)) + bytes(2),
            bytes((0x0D,)) + bytes(1),
            TOUCH_1,
        ]
        transport = CountdownEventTransport(raw)

        self.assertEqual(transport.read_event(), TOUCH_1)
        self.assertEqual(list(transport.ignored_input_reports), [
            {"report_id": "0x07", "report_bytes": 9},
            {"report_id": "0x08", "report_bytes": 16},
            {"report_id": "0x0c", "report_bytes": 3},
            {"report_id": "0x0d", "report_bytes": 2},
        ])

    def test_malformed_vendor_or_unrelated_reports_remain_terminal(self):
        for event, message in (
            (bytes((0x10,)) + bytes(8), "invalid vendor input"),
            (bytes((0x07,)) + bytes(7), "unexpected HID input"),
            (bytes((0x09,)) + bytes(7), "unexpected HID input"),
        ):
            raw = EventRawTransport()
            raw.before = [event]
            with self.subTest(event=event.hex()), self.assertRaisesRegex(
                    DeviceError, message):
                CountdownEventTransport(raw).read_event()

    def test_unrelated_report_filter_is_bounded(self):
        raw = EventRawTransport()
        raw.before = [bytes((0x07,)) + bytes(8)] * 128
        transport = CountdownEventTransport(raw)

        with self.assertRaisesRegex(DeviceError, "too many unrelated HID reports"):
            transport.read_event()
        self.assertEqual(len(transport.ignored_input_reports), 128)


class CountdownGuardTests(unittest.TestCase):
    def test_layout_drift_between_split_reports_stops_before_second_write(self):
        bindings = [
            CountdownBinding("shared", page, slot, 60)
            for page in range(3) for slot in range(4)
        ]
        sent = []
        guard_calls = 0

        def guard():
            nonlocal guard_calls
            guard_calls += 1
            if guard_calls == 2:
                raise DeviceError("LCD layout changed")

        runtime = CountdownRuntime(
            bindings, SimpleNamespace(send=sent.append), guard=guard)

        with self.assertRaisesRegex(DeviceError, "LCD layout changed"):
            runtime.start()

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][3], 6)
        self.assertFalse(runtime.started)
        self.assertTrue(runtime.display_may_be_stale)

    def test_guard_failure_before_first_write_is_not_reported_as_stale(self):
        runtime = CountdownRuntime(
            [CountdownBinding("one", 0, 0, 1)],
            SimpleNamespace(send=Mock()),
            guard=Mock(side_effect=DeviceError("profile drift")),
        )
        with self.assertRaisesRegex(DeviceError, "profile drift"):
            runtime.start()
        self.assertFalse(runtime.display_may_be_stale)

    def test_guard_checks_fresh_active_profile_and_full_lcd_signature(self):
        profile_raw = bytes.fromhex(CAPTURES["profile"])
        lcd_raw = bytes.fromhex(CAPTURES["lcd"])
        prepared = SimpleNamespace(
            profile_index=0,
            expected_profile=decode_profile_response(profile_raw),
            expected_lcd=decode_lcd_response(lcd_raw, 0),
        )
        guard = CountdownGuard(prepared, object())
        with patch("swarm2.countdown.read_raw", side_effect=[profile_raw, lcd_raw]):
            guard()

        moved_lcd = with_lcd_page(
            lcd_raw, 0,
            (b"\x46\x00", b"\x64\x00", b"\xfe\x00", b"\xfe\x00"),
        )
        with patch("swarm2.countdown.read_raw", side_effect=[profile_raw, moved_lcd]):
            with self.assertRaisesRegex(DeviceError, "LCD layout changed"):
                guard()


class CountdownCommandReaderTests(unittest.TestCase):
    def read_line(self, payload):
        reader_fd, writer_fd = os.pipe()
        self.addCleanup(os.close, reader_fd)
        os.write(writer_fd, payload)
        os.close(writer_fd)
        return CountdownCommandReader(reader_fd).read(0.1)

    def test_accepts_one_bounded_object_and_rejects_duplicate_or_nonfinite_json(self):
        self.assertEqual(self.read_line(b'{"command":"stop"}\n'),
                         {"command": "stop"})
        for payload in (
            b'{"command":"start","command":"stop"}\n',
            b'{"command":"stop","value":NaN}\n',
            b'\xff\n',
        ):
            with self.subTest(payload=payload), self.assertRaises(DeviceError):
                self.read_line(payload)

    def test_partial_pipe_reads_are_collected_until_the_line_is_complete(self):
        reader = CountdownCommandReader(7)
        with patch("swarm2.countdown.pipe_readable",
                   side_effect=[True, True]), \
             patch("swarm2.countdown.os.read",
                   side_effect=[b'{"command":', b'"stop"}\n']):
            self.assertEqual(reader.read(0.1), {"command": "stop"})

    def test_size_depth_and_node_count_are_bounded_before_helper_actions(self):
        reader = CountdownCommandReader(7)
        with patch("swarm2.countdown.pipe_readable", return_value=True), \
             patch("swarm2.countdown.os.read",
                   return_value=b"x" * (MAX_CONTROLLER_COMMAND_BYTES + 1)):
            with self.assertRaisesRegex(DeviceError, "too large"):
                reader.read()

        nested = b'{"command":' + b"[" * 9 + b"0" + b"]" * 9 + b"}\n"
        with self.assertRaisesRegex(DeviceError, "deeply nested"):
            self.read_line(nested)
        many = json.dumps({"command": "stop", "values": list(range(512))}).encode() + b"\n"
        with self.assertRaisesRegex(DeviceError, "deeply nested"):
            self.read_line(many)


class CountdownRequestTests(unittest.TestCase):
    def request(self):
        lcd = with_lcd_page(
            bytes.fromhex(CAPTURES["lcd"]), 0,
            (b"\x46\x00", b"\x64\x00", b"\xfe\x00", b"\xfe\x00"),
        )
        return {
            "command": "start",
            "device_id": "fixture",
            "profile_slot": 1,
            "baseline": {
                "device_id": "fixture", "profile_slot": 1,
                "transport_identity": "fixture",
                "settings": {
                    "profile": CAPTURES["profile"], "lcd": lcd.hex(),
                },
            },
            "bindings": [{
                "timer_id": "timer-1", "page_index": 0, "slot_index": 0,
                "duration_seconds": 1,
            }],
        }

    def test_request_is_bound_to_active_profile_layout_and_safe_identity(self):
        prepared = validate_countdown_request(self.request())
        self.assertEqual((prepared.profile_index, prepared.transport_identity),
                         (0, "fixture"))
        self.assertEqual(prepared.bindings[0].duration_seconds, 1)

        for identity in (True, 0, 0x100000000, "", "x" * 1025):
            request = self.request()
            request["baseline"]["transport_identity"] = identity
            with self.subTest(identity=identity), self.assertRaises(DeviceError):
                validate_countdown_request(request)

    def test_request_rejects_noncanonical_timer_id_and_wrong_tile(self):
        request = self.request()
        request["bindings"][0]["timer_id"] = "ambiguous_id"
        with self.assertRaisesRegex(DeviceError, "letters, digits, or hyphens"):
            validate_countdown_request(request)

        request = self.request()
        request["bindings"][0]["slot_index"] = 1
        with self.assertRaisesRegex(DeviceError, "stored LCD tile"):
            validate_countdown_request(request)


class RawTransportMethodTests(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "Exercises POSIX ioctl APIs")
    def test_linux_raw_event_read_and_feature_write_are_bounded(self):
        transport = HidrawTransport("fixture")
        transport.events, transport.control = 11, 12
        with patch("swarm2.transport.select.select", return_value=([11], [], [])) as selected, \
             patch("swarm2.transport.os.read", return_value=TOUCH_1):
            self.assertEqual(transport.read_event(0.25), TOUCH_1)
        selected.assert_called_once_with([11], [], [], 0.25)

        with patch("fcntl.ioctl", return_value=64) as ioctl:
            self.assertEqual(transport.write_feature(a3_report()), 64)
        self.assertEqual(ioctl.call_args.args[0], 12)
        for timeout in (-1, 2.1, True, "0"):
            with self.subTest(timeout=timeout), self.assertRaises(DeviceError):
                transport.read_event(timeout)
        for report in (bytes(63), bytearray(64), "x"):
            with self.subTest(report=type(report)), self.assertRaises(DeviceError):
                transport.write_feature(report)

    def test_macos_raw_methods_require_open_session_and_preserve_timeout(self):
        transport = MacOSTransport()
        transport._opened = True
        transport.events = Mock()
        transport.control = Mock()
        transport.events.read.return_value = TOUCH_2
        transport.control.send_feature_report.return_value = 64

        self.assertEqual(transport.read_event(0.25), TOUCH_2)
        transport.events.read.assert_called_once_with(64, 250)
        self.assertEqual(transport.write_feature(a3_report()), 64)
        transport.control.send_feature_report.assert_called_once_with(a3_report())
        for timeout in (-1, 2.1, True, "0"):
            with self.subTest(timeout=timeout), self.assertRaises(DeviceError):
                transport.read_event(timeout)
        transport._opened = False
        with self.assertRaisesRegex(DeviceError, "Open an MC7"):
            transport.read_event()
        with self.assertRaisesRegex(DeviceError, "Open an MC7"):
            transport.write_feature(a3_report())


if __name__ == "__main__":
    unittest.main()
