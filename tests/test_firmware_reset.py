"""Post-update reset ordering with synthetic events, never physical hardware."""

from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch

from swarm2.firmware_reset import (FirmwareResetTransport, RESET_REPORT,
                                   decode_reset_completion, perform_factory_reset)
from swarm2.protocol import ProtocolError
from swarm2.transport import DeviceError

ACK = bytes.fromhex("1000f21300000000")
B1 = bytes.fromhex("1000b10011223344")
F5 = bytes.fromhex("1000f500aabbccdd")


class FakeResetTransport:
    def __init__(self, events=()):
        self.now = 10.0
        self.pending = list(events)
        self.events = []
        self.writes = []
        self.drains = 0
        self.disconnect = False

    def drain(self):
        self.drains += 1
        self.events.clear()

    def write(self, packet):
        self.writes.append(packet)
        self.events.extend(self.pending)

    def read_event(self, timeout):
        self.now += timeout
        if self.disconnect:
            raise DeviceError("Disconnected")
        return self.events.pop(0) if self.events else b""

    def reset(self):
        return perform_factory_reset(self, timeout_seconds=3, clock=lambda: self.now)


class FirmwareResetTests(unittest.TestCase):
    def test_source_reset_packet_and_both_success_event_forms(self):
        self.assertEqual(RESET_REPORT, bytes.fromhex("10135a00") + bytes(60))
        for event in (B1, F5):
            with self.subTest(event=event.hex()):
                transport = FakeResetTransport([ACK, event])
                result = transport.reset()
                self.assertEqual(result, {"acknowledged": True, "reset_completed": True,
                                          "acknowledgement": ACK.hex(), "completion_event": event.hex()})
                self.assertEqual(transport.writes, [RESET_REPORT])
                self.assertEqual(transport.drains, 1)

    def test_completion_before_ack_is_retained_without_another_drain(self):
        transport = FakeResetTransport([B1, ACK])
        self.assertEqual(transport.reset()["completion_event"], B1.hex())
        self.assertEqual(transport.drains, 1)
        self.assertEqual(transport.writes, [RESET_REPORT])

    def test_generic_ack_never_establishes_reset_completion(self):
        transport = FakeResetTransport([ACK])
        with self.assertRaisesRegex(DeviceError, "did not report reset completion"):
            transport.reset()
        self.assertLessEqual(transport.now, 13.001)
        self.assertEqual(transport.writes, [RESET_REPORT])

    def test_completion_without_ack_is_uncertain_and_never_replayed(self):
        transport = FakeResetTransport([B1])
        with self.assertRaisesRegex(DeviceError, "did not acknowledge"):
            transport.reset()
        self.assertLessEqual(transport.now, 12.001)
        self.assertEqual(transport.writes, [RESET_REPORT])

    def test_stale_completion_and_ack_before_this_attempt_are_drained(self):
        transport = FakeResetTransport([ACK])
        transport.events = [ACK, B1]
        with self.assertRaisesRegex(DeviceError, "did not report reset completion"):
            transport.reset()
        self.assertEqual(transport.writes, [RESET_REPORT])

    def test_busy_ack_is_not_success_and_can_precede_accepted_ack(self):
        transport = FakeResetTransport([bytes.fromhex("1000f21301000000"), F5, ACK])
        self.assertTrue(transport.reset()["reset_completed"])
        transport = FakeResetTransport([bytes.fromhex("1000f21302000000"), F5])
        with self.assertRaisesRegex(DeviceError, "did not acknowledge"):
            transport.reset()
        self.assertEqual(transport.writes, [RESET_REPORT])

    def test_rejected_ack_and_disconnect_report_uncertainty_once(self):
        for events, disconnect, message in (([bytes.fromhex("1000f21303000000")], False, "rejected reset"),
                                            ([ACK, B1], True, "Disconnected")):
            with self.subTest(message=message):
                transport = FakeResetTransport(events)
                transport.disconnect = disconnect
                with self.assertRaisesRegex(DeviceError, message):
                    transport.reset()
                self.assertEqual(transport.writes, [RESET_REPORT])

    def test_wrong_event_identity_status_and_command_are_ignored(self):
        invalid = [b"", B1[:-1], B1 + b"\0", bytes.fromhex("1100b10000000000"),
                   bytes.fromhex("1033b10000000000"), bytes.fromhex("1000b10100000000"),
                   bytes.fromhex("1000f50100000000"), bytes.fromhex("1000d10000000000"),
                   bytes.fromhex("1000f21900000000"), bytes.fromhex("1000061300000000")]
        for event in invalid:
            with self.subTest(event=event), self.assertRaises(ProtocolError):
                decode_reset_completion(event)
        transport = FakeResetTransport([*invalid, ACK, F5])
        self.assertEqual(transport.reset()["completion_event"], F5.hex())

    def test_bad_timeout_and_failed_drain_never_send_a_reset(self):
        for timeout in (None, True, 0, 1.9, 31, float("inf"), float("nan")):
            transport = FakeResetTransport([ACK, B1])
            with self.subTest(timeout=timeout), self.assertRaises(DeviceError):
                perform_factory_reset(transport, timeout_seconds=timeout, clock=lambda: transport.now)
            self.assertEqual(transport.writes, [])
            self.assertEqual(transport.drains, 0)
        transport = FakeResetTransport([ACK, B1])
        transport.drain = Mock(side_effect=DeviceError("Unsettled event queue"))
        with self.assertRaisesRegex(DeviceError, "Unsettled event queue"):
            transport.reset()
        self.assertEqual(transport.writes, [])

    def test_adapter_rejects_other_reports_before_native_write(self):
        handle = Mock()
        transport = FirmwareResetTransport(SimpleNamespace(control=handle))
        for report in (bytes(64), RESET_REPORT[:-1], bytearray(RESET_REPORT),
                       RESET_REPORT[:3] + b"\x01" + RESET_REPORT[4:]):
            with self.subTest(report=report), self.assertRaises(DeviceError):
                transport.write(report)
        handle.send_feature_report.assert_not_called()
        handle.send_feature_report.return_value = 64
        transport.write(RESET_REPORT)
        handle.send_feature_report.assert_called_once_with(RESET_REPORT)
        handle.send_feature_report.return_value = 63
        with self.assertRaisesRegex(DeviceError, "incomplete"):
            transport.write(RESET_REPORT)

    @unittest.skipIf(sys.platform == "win32", "Exercises POSIX ioctl APIs")
    def test_linux_adapter_sends_one_exact_feature_ioctl(self):
        ordinary = SimpleNamespace(control=17, _request=lambda number, length: (number, length))
        transport = FirmwareResetTransport(ordinary)
        with patch("fcntl.ioctl", return_value=64) as ioctl:
            transport.write(RESET_REPORT)
        ioctl.assert_called_once_with(17, (6, 64), bytearray(RESET_REPORT), True)


if __name__ == "__main__":
    unittest.main()
