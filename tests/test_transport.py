"""Bounded Linux HID transaction tests with no real device handles or writes."""

from contextlib import ExitStack
import struct
import sys
import unittest
from unittest.mock import patch

from swarm2.devices import Device, Enumeration
from swarm2.protocol import build_sensor_read_request
from swarm2.transport import DeviceError, HidrawTransport, physical_id, select_interfaces


def interface(number, *, port="usb-fixture-1", product=0x502C, length=None, usage=1):
    return Device(
        path=f"/dev/mock{number}", vendor_id=0x10F5, product_id=product,
        product_name="Fixture", backend="sysfs", interface_number=number,
        physical_path=f"{port}/input{number}",
        descriptor={"reports": [{
            "kind": "feature" if number == 2 else "input", "report_id": 0x10,
            "report_bytes": length if length is not None else (64 if number == 2 else 8),
            "application_usages": [{"page": 0xFF01, "usage": usage}],
        }]},
    )


def acknowledgement(status=0, *, command=0x1C, discriminator=0xF2, report_id=0x10):
    return bytes((report_id, 0, discriminator, command, status, 0, 0, 0))


class InterfaceSelectionTests(unittest.TestCase):
    def select(self, devices, device_id="usb-fixture-1"):
        with patch("swarm2.transport.enumerate_sysfs", return_value=Enumeration("sysfs", devices)):
            return select_interfaces(device_id)

    def test_pairs_control_and_events_only_on_same_physical_mouse(self):
        control, events = interface(2), interface(1)
        other_events = interface(1, port="usb-fixture-2")
        self.assertEqual(physical_id(control), "usb-fixture-1")
        self.assertEqual(self.select([other_events, events, control]), (control, events))

    def test_refuses_ambiguous_missing_transmitter_or_wrong_descriptor_pairs(self):
        cases = [
            [interface(2)],
            [interface(2), interface(1, port="usb-other")],
            [interface(2), interface(1), interface(1)],
            [interface(2), interface(2), interface(1)],
            [interface(2, product=0x502E), interface(1, product=0x502E)],
            [interface(2), interface(1, length=64)],
            [interface(2, usage=2), interface(1)],
        ]
        for devices in cases:
            with self.subTest(devices=devices), self.assertRaises(DeviceError):
                self.select(devices)
        with self.assertRaises(DeviceError):
            self.select([interface(2), interface(1)], "")


@unittest.skipIf(sys.platform == "win32", "Exercises POSIX desktop or USB APIs")
class SessionLifetimeTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch("swarm2.transport.tempfile.gettempdir", return_value="/tmp"))

    def test_context_releases_handles_and_lock_in_reverse_order(self):
        with patch("swarm2.transport.select_interfaces", return_value=(interface(2), interface(1))), \
             patch("swarm2.transport.os.open", side_effect=[10, 11, 12]) as opened, \
             patch("swarm2.transport.os.close") as closed, patch("fcntl.flock") as locked, \
             patch.object(HidrawTransport, "_verify_handle") as verified:
            with HidrawTransport("usb-fixture-1") as transport:
                self.assertEqual((transport.events, transport.control), (11, 12))
            self.assertEqual([call.args[0] for call in closed.call_args_list], [12, 11, 10])
            self.assertEqual(opened.call_args_list[1].args[0], "/dev/mock1")
            self.assertEqual(opened.call_args_list[2].args[0], "/dev/mock2")
            locked.assert_called_once()
            self.assertEqual([call.args[0] for call in verified.call_args_list], [12, 11])

    def test_permission_failure_releases_lock(self):
        with patch("swarm2.transport.select_interfaces", return_value=(interface(2), interface(1))), \
             patch("swarm2.transport.os.open", side_effect=[10, PermissionError("denied")]), \
             patch("swarm2.transport.os.close") as closed, patch("fcntl.flock"):
            with self.assertRaisesRegex(DeviceError, "access denied"):
                with HidrawTransport("usb-fixture-1"):
                    self.fail("A denied session must not enter its body")
            closed.assert_called_once_with(10)

    def test_changed_device_identity_closes_all_handles_before_any_send(self):
        with patch("swarm2.transport.select_interfaces", return_value=(interface(2), interface(1))), \
             patch("swarm2.transport.os.open", side_effect=[10, 11, 12]), \
             patch("swarm2.transport.os.close") as closed, patch("fcntl.flock"), \
             patch.object(HidrawTransport, "_verify_handle", side_effect=DeviceError("USB mouse changed")):
            with self.assertRaisesRegex(DeviceError, "USB mouse changed"):
                with HidrawTransport("usb-fixture-1"):
                    self.fail("A replaced device must not enter its session")
            self.assertEqual([call.args[0] for call in closed.call_args_list], [12, 11, 10])

    def test_busy_session_does_not_open_hid_handles(self):
        with patch("swarm2.transport.select_interfaces", return_value=(interface(2), interface(1))), \
             patch("swarm2.transport.os.open", return_value=10) as opened, \
             patch("swarm2.transport.os.close") as closed, \
             patch("fcntl.flock", side_effect=BlockingIOError("locked")):
            with self.assertRaisesRegex(DeviceError, "Another swarm2 process"):
                with HidrawTransport("usb-fixture-1"):
                    self.fail("A busy session must not enter its body")
            opened.assert_called_once()
            closed.assert_called_once_with(10)

    def test_opened_handle_requires_matching_bus_vendor_product_and_full_interface_path(self):
        matching = (3, 0x10F5, 0x502C, "usb-fixture-1/input2")
        cases = [matching, (5, *matching[1:]), (3, 0x1234, *matching[2:]),
                 (3, 0x10F5, 0x502E, matching[3]),
                 (*matching[:3], "usb-fixture-2/input2"),
                 (*matching[:3], "usb-fixture-1/input1")]
        for values in cases:
            def ioctl(_fd, request, buffer, _mutate):
                if request & 0xFF == 3:
                    buffer[:] = struct.pack("=IHH", *values[:3])
                else:
                    encoded = values[3].encode("utf-8") + b"\0"
                    buffer[:len(encoded)] = encoded
                return 0

            with self.subTest(values=values), patch("fcntl.ioctl", side_effect=ioctl):
                if values == matching:
                    HidrawTransport._verify_handle(12, interface(2))
                else:
                    with self.assertRaisesRegex(DeviceError, "changed during connection"):
                        HidrawTransport._verify_handle(12, interface(2))


@unittest.skipIf(sys.platform == "win32", "Exercises POSIX desktop or USB APIs")
class AcknowledgementTests(unittest.TestCase):
    def setUp(self):
        self.transport = HidrawTransport("fixture")
        self.transport.events = 11
        self.transport.control = 12
        self.report = build_sensor_read_request(0)

    def test_drops_old_events_ignores_other_commands_and_waits_through_busy(self):
        old = acknowledgement()
        incoming = [b"\x01\x02", acknowledgement(report_id=1),
                    acknowledgement(discriminator=6), acknowledgement(command=0x14),
                    acknowledgement(1), acknowledgement(2), acknowledgement()]
        ready = [([11], [], []), ([], [], [])] + [([11], [], [])] * len(incoming)
        with patch("swarm2.transport.select.select", side_effect=ready), \
             patch("swarm2.transport.os.read", side_effect=[old, *incoming]), \
             patch("fcntl.ioctl", return_value=64) as ioctl:
            self.transport.send(self.report)
        ioctl.assert_called_once()
        self.assertEqual(self.transport.acknowledgements,
                         [acknowledgement(status).hex() for status in (1, 2, 0)])

    def test_busy_then_timeout_never_retransmits_write(self):
        ready = [( [], [], []), ([11], [], []), ([], [], [])]
        with patch("swarm2.transport.select.select", side_effect=ready) as selected, \
             patch("swarm2.transport.os.read", return_value=acknowledgement(1)), \
             patch("fcntl.ioctl", return_value=64) as ioctl:
            with self.assertRaisesRegex(DeviceError, "timed out.*read settings again"):
                self.transport.send(self.report)
        ioctl.assert_called_once()
        self.assertGreater(selected.call_args.args[3], 0)
        self.assertLessEqual(selected.call_args.args[3], 2)

    def test_unknown_ack_status_fails_immediately(self):
        with patch("swarm2.transport.select.select", side_effect=[([], [], []), ([11], [], [])]), \
             patch("swarm2.transport.os.read", return_value=acknowledgement(9)), \
             patch("fcntl.ioctl", return_value=64) as ioctl:
            with self.assertRaisesRegex(DeviceError, "status 9"):
                self.transport.send(self.report)
        ioctl.assert_called_once()

    def test_incomplete_feature_send_does_not_wait_for_ack(self):
        with patch("swarm2.transport.select.select", return_value=([], [], [])) as selected, \
             patch("swarm2.transport.os.read") as read, patch("fcntl.ioctl", return_value=63):
            with self.assertRaisesRegex(DeviceError, "Incomplete feature transfer"):
                self.transport.send(self.report)
        selected.assert_called_once()
        read.assert_not_called()

    def test_unsettled_event_queue_is_bounded_and_never_sends(self):
        with patch("swarm2.transport.select.select", return_value=([11], [], [])), \
             patch("swarm2.transport.os.read", return_value=acknowledgement()) as read, \
             patch("fcntl.ioctl") as ioctl:
            with self.assertRaisesRegex(DeviceError, "queue did not settle"):
                self.transport.send(self.report)
        self.assertEqual(read.call_count, 128)
        ioctl.assert_not_called()

    def test_unapproved_feature_commands_never_reach_ioctl(self):
        for report in (bytes(64), bytes((0x10, 0x20)) + bytes(62), self.report[:63]):
            with self.subTest(report=report), patch("fcntl.ioctl") as ioctl:
                with self.assertRaisesRegex(DeviceError, "Unsupported feature command"):
                    self.transport.send(report)
                ioctl.assert_not_called()

    def test_sensor_get_preserves_exact_returned_length_and_rejects_short_data(self):
        for count in (48, 64, 0, 47, 65):
            payload = bytes(range(64))

            def ioctl(_fd, _request, buffer, _mutate):
                self.assertEqual(buffer[:2], b"\x10\x10")
                buffer[:] = payload
                return count

            with self.subTest(count=count), patch("fcntl.ioctl", side_effect=ioctl), \
                 patch("swarm2.transport.time.sleep"):
                if count in (48, 64):
                    self.assertEqual(self.transport.get_sensor(), payload[:count])
                else:
                    with self.assertRaisesRegex(DeviceError, "Incomplete sensor response|Invalid feature response length"):
                        self.transport.get_sensor()


if __name__ == "__main__":
    unittest.main()
