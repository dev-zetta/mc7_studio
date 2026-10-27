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

    def test_receiver_uses_its_own_matching_vendor_interfaces(self):
        control, events = interface(2, product=0x502E), interface(1, product=0x502E)
        self.assertEqual(self.select([control, events]), (control, events))

    def test_refuses_ambiguous_missing_mixed_or_wrong_descriptor_pairs(self):
        cases = [
            [interface(2)],
            [interface(2), interface(1, port="usb-other")],
            [interface(2), interface(1), interface(1)],
            [interface(2), interface(2), interface(1)],
            [interface(2, product=0x502E), interface(1)],
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

    def test_receiver_polls_past_previous_selector_and_empty_cache_without_sending(self):
        self.transport.wireless = True
        current = bytes.fromhex("10100000020200000002010700ff000001010f0000ff0001017a000064640101f9000000ff01015702e320ca018501f6")
        replies = [bytes.fromhex("1012000005fb"), bytes.fromhex("100000000000"), current]
        with patch.object(self.transport, '_get_feature_now', side_effect=replies) as get, \
             patch.object(self.transport, 'send') as send, \
             patch('swarm2.transport.time.sleep'):
            self.assertEqual(self.transport.get_sensor(), current)
        self.assertEqual(get.call_count, 3)
        send.assert_not_called()

    def test_receiver_wait_has_a_deadline_and_a_poll_count_limit(self):
        self.transport.wireless = True
        for times, expected in (([0, 3], 1), (None, 128)):
            with self.subTest(times=times), \
                 patch.object(self.transport, '_get_feature_now', return_value=b'\x10\0') as get, \
                 patch('swarm2.transport.time.monotonic', side_effect=times, return_value=0), \
                 patch('swarm2.transport.time.sleep'):
                with self.assertRaisesRegex(DeviceError, 'wireless mouse'):
                    self.transport.get_feature(0x10)
            self.assertEqual(get.call_count, expected)

    def test_receiver_polls_past_same_selector_from_a_previous_profile_or_page(self):
        self.transport.wireless = True
        for selector in (0x10, 0x15, 0x16, 0x25, 0x2A, 0x29):
            with self.subTest(selector=selector):
                self.transport._feature_read = bytes((0x10, 0x1C, 0, selector, 4, 2, 0)) + bytes(57)
                length = {0x25: 61, 0x29: 54}.get(selector, 12)
                current = bytearray((0x10, selector, 0, 4)) + bytearray(length - 4)
                if selector == 0x29:
                    current[6:8] = bytes((4, 2))
                    wrong_profile, wrong_page = bytearray(current), bytearray(current)
                    wrong_profile[6], wrong_page[7] = 3, 1
                    stale = [bytes(wrong_profile), bytes(wrong_page)]
                else:
                    previous = bytearray(current)
                    previous[3] = 3
                    stale = [bytes(previous)]
                with patch.object(self.transport, '_get_feature_now', side_effect=[*stale, bytes(current)]) as get, \
                     patch.object(self.transport, 'send') as send, patch('swarm2.transport.time.sleep'):
                    self.assertEqual(self.transport.get_feature(selector), bytes(current))
                self.assertEqual(get.call_count, len(stale) + 1)
                send.assert_not_called()

    def test_acknowledged_page_read_retains_identity_until_its_reply_arrives(self):
        from swarm2.screen_key_commands import build_screen_key_read_request
        self.transport.wireless = True
        report = build_screen_key_read_request(4, 2)
        previous = bytes((0x10, 0x29, 0, 0x29, 0x32, 0, 4, 2)) + bytes(46)
        current = bytes((0x10, 0x29, 0, 0x29, 0x32, 0, 4, 3)) + bytes(46)
        with patch('swarm2.transport.select.select', side_effect=[([], [], []), ([11], [], [])]), \
             patch('swarm2.transport.os.read', return_value=acknowledgement()), \
             patch('fcntl.ioctl', return_value=64) as ioctl, \
             patch.object(self.transport, '_get_feature_now', side_effect=[previous, current]), \
             patch('swarm2.transport.time.sleep'):
            self.transport.send(report)
            self.assertEqual(self.transport.get_feature(0x29), current)
        ioctl.assert_called_once()

    def test_receiver_rejects_a_permanently_stale_page_without_replaying_selector(self):
        self.transport.wireless = True
        self.transport._feature_read = bytes((0x10, 0x1C, 0, 0x29, 4, 3, 0)) + bytes(57)
        previous = bytes((0x10, 0x29, 0, 0x29, 0x32, 0, 4, 2)) + bytes(46)
        with patch.object(self.transport, '_get_feature_now', return_value=previous) as get, \
             patch.object(self.transport, 'send') as send, \
             patch('swarm2.transport.time.sleep'), patch('swarm2.transport.time.monotonic', return_value=0):
            with self.assertRaisesRegex(DeviceError, 'No matching reply'):
                self.transport.get_feature(0x29)
        self.assertEqual(get.call_count, 128)
        send.assert_not_called()

    def test_receiver_does_not_hide_a_matching_but_corrupt_reply(self):
        self.transport.wireless = True
        with patch.object(self.transport, '_get_feature_now', return_value=b'\x10\x10\x01') as get, \
             patch('swarm2.transport.time.sleep'):
            self.assertEqual(self.transport.get_feature(0x10), b'\x10\x10\x01')
        get.assert_called_once()

    def test_receiver_discards_only_zero_lcd_padding(self):
        self.transport.wireless = True
        for selector, length in ((0x25, 61), (0x29, 54)):
            record = bytes((0x10, selector)) + bytes(length - 2)
            for tail, accepted in ((bytes(64 - length), True), (bytes(63 - length) + b'\x01', False)):
                with self.subTest(selector=selector, accepted=accepted), \
                     patch.object(self.transport, '_get_feature_now', return_value=record + tail), \
                     patch('swarm2.transport.time.sleep'):
                    if accepted:
                        self.assertEqual(self.transport.get_feature(selector), record)
                    else:
                        with self.assertRaisesRegex(DeviceError, 'Unexpected data'):
                            self.transport.get_feature(selector)

    def test_receiver_reporting_pause_and_resume_match_ack_and_rate_event(self):
        self.transport.wireless = True
        for enabled in (False, True):
            incoming = [b'', acknowledgement(2, command=0x0F, discriminator=6),
                        acknowledgement(command=0x0F, discriminator=6)]
            if enabled:
                incoming.append(bytes.fromhex('1000050500000000'))
            with self.subTest(enabled=enabled), \
                 patch.object(self.transport, 'write_feature', return_value=64) as write, \
                 patch.object(self.transport, 'read_event', side_effect=incoming), \
                 patch('swarm2.transport.time.sleep'):
                self.transport._set_receiver_reporting(enabled)
            write.assert_called_once_with(bytes((0x10,0x0F,7 if enabled else 0))+bytes(61))

    def test_receiver_reporting_rejects_partial_send_and_missing_rate_event_without_replay(self):
        self.transport.wireless = True
        with patch.object(self.transport, 'write_feature', return_value=63) as write, \
             patch.object(self.transport, 'read_event', return_value=b''):
            with self.assertRaisesRegex(DeviceError, 'Incomplete receiver'):
                self.transport._set_receiver_reporting(False)
            write.assert_called_once()
        for enabled, wait_for_rate in ((True, False), (False, True)):
            with self.subTest(enabled=enabled, wait_for_rate=wait_for_rate), \
                 patch.object(self.transport, 'write_feature', return_value=64) as write, \
                 patch.object(self.transport, 'read_event', side_effect=[b'']+[acknowledgement(command=0x0F,discriminator=6)]*128), \
                 patch('swarm2.transport.time.monotonic', return_value=0):
                with self.assertRaisesRegex(DeviceError, 'rate event timed out'):
                    self.transport._set_receiver_reporting(enabled, wait_for_rate=wait_for_rate)
                write.assert_called_once()

    def test_reporting_forwards_vendor_notifications_during_drain_and_rate_wait(self):
        self.transport.wireless = True
        before = bytes.fromhex("1000d10000000000")
        during = bytes.fromhex("1000d10100000000")
        keyboard = bytes.fromhex("0100000400000000")
        incoming = [before, keyboard, b"", acknowledgement(command=0x0F, discriminator=6),
                    during, keyboard, bytes.fromhex("1000050500000000")]
        observed = []
        with patch.object(self.transport, "write_feature", return_value=64) as write, \
             patch.object(self.transport, "read_event", side_effect=incoming), \
             patch("swarm2.transport.time.sleep"):
            self.transport._set_receiver_reporting(True, on_event=observed.append)
        self.assertEqual(observed, [before, during])
        write.assert_called_once()

    def test_receiver_readback_pause_waits_for_rate_transition_after_ack(self):
        self.transport.wireless = True
        incoming = [b'', acknowledgement(command=0x0F, discriminator=6),
                    bytes.fromhex('1000050501e80300')]
        with patch.object(self.transport, 'write_feature', return_value=64) as write, \
             patch.object(self.transport, 'read_event', side_effect=incoming) as read, \
             patch('swarm2.transport.time.sleep'):
            self.transport.prepare_settings_readback()
        self.assertEqual(read.call_count, 3)
        write.assert_called_once_with(bytes((0x10, 0x0F, 0)) + bytes(61))

    def test_receiver_configuration_restores_reporting_after_start_or_operation_failure(self):
        self.transport.wireless = True
        for start_failure in (False, True):
            def set_reporting(enabled):
                if not enabled and start_failure:
                    raise DeviceError('Pause failed')
            with self.subTest(start_failure=start_failure), \
                 patch.object(self.transport, '_set_receiver_reporting', side_effect=set_reporting) as reporting:
                with self.assertRaises(DeviceError):
                    with self.transport.configuration_session():
                        raise DeviceError('Operation failed')
            self.assertEqual([call.args[0] for call in reporting.call_args_list],[False,True])

    def test_usb_configuration_never_sends_receiver_reporting_commands(self):
        self.transport.wireless = False
        with patch.object(self.transport, '_set_receiver_reporting') as reporting:
            with self.transport.configuration_session():
                pass
        reporting.assert_not_called()


if __name__ == "__main__":
    unittest.main()
