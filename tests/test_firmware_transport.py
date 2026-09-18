"""CFU endpoint and native API contracts, without opening any USB handles."""

from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch

from swarm2.firmware_transport import FirmwareTransport, validate_cfu_descriptor
from swarm2.transport import DeviceError
from tests.test_descriptor import MC7_DESCRIPTOR
from tests.test_firmware_commands import OFFER, VERSION
from swarm2.firmware_commands import build_offer_report


@unittest.skipIf(sys.platform == "win32", "Exercises POSIX desktop or USB APIs")
class FirmwareTransportTests(unittest.TestCase):
    def setUp(self):
        self.transport = FirmwareTransport("fixture-port")
        self.normal = SimpleNamespace(control=12, events=11,
                                      _request=lambda number, length: (number, length))
        self.transport.normal = self.normal

    def test_descriptor_requires_cfu_collection_and_exact_envelopes(self):
        validate_cfu_descriptor(MC7_DESCRIPTOR)
        for malformed in (MC7_DESCRIPTOR.replace(b"\x06\x0b\xff", b"\x06\x0c\xff", 1),
                          MC7_DESCRIPTOR.replace(b"\x85\x2d", b"\x85\x2e", 1),
                          MC7_DESCRIPTOR.replace(b"\x95\x3c", b"\x95\x3b", 1), b""):
            with self.subTest(descriptor=malformed), self.assertRaises((ValueError, DeviceError)):
                validate_cfu_descriptor(malformed)

    def test_normal_session_lock_is_retained_and_closed_after_descriptor_failure(self):
        ordinary = Mock()
        ordinary.__enter__ = Mock(return_value=ordinary)
        ordinary.__exit__ = Mock()
        with patch("swarm2.firmware_transport.platform.system", return_value="Linux"), \
             patch("swarm2.firmware_transport.HidrawTransport", return_value=ordinary) as factory, \
             patch.object(FirmwareTransport, "_descriptor", return_value=MC7_DESCRIPTOR):
            with FirmwareTransport("fixture-port") as transport:
                self.assertIs(transport.normal, ordinary)
            factory.assert_called_once_with("fixture-port")
            ordinary.__enter__.assert_called_once_with()
            ordinary.__exit__.assert_called_once()
            self.assertIsNone(transport.normal)
        ordinary.__exit__.reset_mock()
        with patch("swarm2.firmware_transport.platform.system", return_value="Linux"), \
             patch("swarm2.firmware_transport.HidrawTransport", return_value=ordinary), \
             patch.object(FirmwareTransport, "_descriptor", return_value=b""):
            with self.assertRaises(ValueError):
                with FirmwareTransport("fixture-port"):
                    self.fail("An invalid descriptor must not open a firmware session")
            ordinary.__exit__.assert_called_once()

    def test_linux_version_get_uses_control_handle_and_max_feature_length(self):
        for reply in (VERSION, VERSION + bytes(3)):
            calls = []

            def ioctl(fd, request, buffer, mutate):
                calls.append((fd, request, bytes(buffer), mutate))
                buffer[:len(reply)] = reply
                return len(reply)

            with self.subTest(length=len(reply)), patch("fcntl.ioctl", side_effect=ioctl):
                self.assertEqual(self.transport.get_feature(), reply)
            self.assertEqual(calls, [(12, (7, 64), b"\x2a" + bytes(63), True)])

    def test_partial_version_get_is_rejected(self):
        for size in (-1, 0, 12, 60, 62, 63, 65):
            with self.subTest(size=size), patch("fcntl.ioctl", return_value=size):
                with self.assertRaisesRegex(DeviceError, "Incomplete CFU version"):
                    self.transport.get_feature()

    def test_cfu_events_come_from_interface_two_not_normal_ack_interface(self):
        event = bytes.fromhex("2d00000000000000000000000001000000")
        with patch("swarm2.firmware_transport.select.select", return_value=([12], [], [])) as ready, \
             patch("swarm2.firmware_transport.os.read", return_value=event) as read:
            self.assertEqual(self.transport.read_event(.2), event)
        ready.assert_called_once_with([12], [], [], .2)
        read.assert_called_once_with(12, 64)
        with patch("swarm2.firmware_transport.select.select", return_value=([], [], [])), \
             patch("swarm2.firmware_transport.os.read") as read:
            self.assertEqual(self.transport.read_input(0), b"")
            read.assert_not_called()

    def test_disconnect_and_invalid_timeouts_never_fabricate_a_reply(self):
        with patch("swarm2.firmware_transport.select.select", return_value=([12], [], [])), \
             patch("swarm2.firmware_transport.os.read", return_value=b""):
            with self.assertRaisesRegex(DeviceError, "disconnected"):
                self.transport.read_event(.1)
        for timeout in (-1, 6, True, "1", None, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), patch("swarm2.firmware_transport.select.select") as ready:
                with self.assertRaises(DeviceError):
                    self.transport.read_event(timeout)
                ready.assert_not_called()

    def test_linux_output_requires_usb_session_and_never_uses_hidraw_output(self):
        report = build_offer_report(OFFER)
        with patch("fcntl.ioctl") as ioctl, \
             patch("swarm2.firmware_transport.os.write") as interrupt_write:
            with self.assertRaisesRegex(DeviceError, "Begin"):
                self.transport.set_output(report)
            usb = Mock()
            self.transport._usb = usb
            self.transport.set_output(report)
        usb.set_output.assert_called_once_with(report)
        ioctl.assert_not_called()
        interrupt_write.assert_not_called()

    def test_prepare_access_check_never_claims_or_sends(self):
        usb = Mock()
        with patch.object(self.transport, '_open_usb', return_value=usb):
            self.transport.check_update_access()
        usb.close.assert_called_once_with()
        usb.claim.assert_not_called()
        usb.set_output.assert_not_called()
        self.assertIsNone(self.transport._usb)

    def test_begin_routes_version_input_and_output_to_claimed_usb(self):
        usb = Mock()
        usb.get_feature.return_value = VERSION
        usb.read_input.return_value = b'\x2d' + bytes(16)
        with patch.object(self.transport, '_open_usb', return_value=usb) as open_usb, \
             patch('fcntl.ioctl') as ioctl:
            self.transport.begin_update()
            self.transport.begin_update()
            self.assertEqual(self.transport.get_feature(), VERSION)
            self.assertEqual(self.transport.read_input(.2), b'\x2d' + bytes(16))
            self.transport.set_output(build_offer_report(OFFER))
        open_usb.assert_called_once_with()
        usb.claim.assert_called_once_with()
        usb.get_feature.assert_called_once_with(0x2A, 64)
        usb.read_input.assert_called_once_with(.2)
        ioctl.assert_not_called()

    def test_usb_cleanup_precedes_normal_lock_release_even_on_claim_failure(self):
        events = []
        self.normal.__exit__ = lambda *args: events.append('unlock')
        usb = Mock()
        usb.claim.side_effect = DeviceError('claim failed')
        usb.close.side_effect = lambda: events.append('rebind')
        with patch.object(self.transport, '_open_usb', return_value=usb):
            with self.assertRaisesRegex(DeviceError, 'claim failed'):
                self.transport.begin_update()
        self.transport.__exit__(None, None, None)
        self.assertEqual(events, ['rebind', 'unlock'])
        self.assertIsNone(self.transport.normal)
        self.assertIsNone(self.transport._usb)

    def test_cleanup_failure_preserves_original_error_and_releases_lock(self):
        self.normal.__exit__ = Mock()
        self.transport._usb = Mock()
        self.transport._usb.close.side_effect = DeviceError('rebind failed')
        error = DeviceError('offer timed out')
        self.transport.__exit__(DeviceError, error, None)
        self.assertIn('offer timed out', str(error))
        self.assertIn('rebind failed', str(error))
        self.normal.__exit__.assert_called_once()
        self.assertIsNone(self.transport.normal)

    def test_cleanup_failure_without_prior_error_is_reported(self):
        self.normal.__exit__ = Mock()
        self.transport._usb = Mock()
        self.transport._usb.close.side_effect = DeviceError('rebind failed')
        with self.assertRaisesRegex(DeviceError, 'rebind failed'):
            self.transport.__exit__(None, None, None)
        self.normal.__exit__.assert_called_once()

    def test_invalid_commands_fail_before_native_io(self):
        with patch("fcntl.ioctl") as ioctl:
            for report in (bytes(61), b"\x2d" + bytes(16), b"\x2d" + bytes(63),
                           b"\x2d" + bytes(16) + b"x" + bytes(43), bytearray(61)):
                with self.subTest(report=report), self.assertRaises(DeviceError):
                    self.transport.set_output(report)
            for report, length in ((0x2B, 64), (0x2A, 61), (True, 64), (0x2A, 64.0)):
                with self.subTest(report=report, length=length), self.assertRaises(DeviceError):
                    self.transport.get_feature(report, length)
            ioctl.assert_not_called()

    def test_macos_uses_native_feature_output_and_control_collection_reads(self):
        library = SimpleNamespace(hid_send_output_report=Mock(return_value=17),
                                  hid_write=Mock(), hid_send_feature_report=Mock())
        get_calls = []

        def get_feature(pointer, data, length):
            get_calls.append((pointer, bytes(data), length))
            data[:len(VERSION)] = VERSION
            return len(VERSION)

        library.hid_get_feature_report = get_feature
        control = SimpleNamespace(library=library, _pointer=lambda: 123,
                                  read=Mock(return_value=b"\x2c" + bytes(16)))
        self.normal.control = control
        self.normal.events = SimpleNamespace(read=Mock())
        self.assertEqual(self.transport.get_feature(), VERSION)
        self.assertEqual(get_calls, [(123, b"\x2a" + bytes(63), 64)])
        self.assertEqual(self.transport.read_input(.2), b"\x2c" + bytes(16))
        control.read.assert_called_once_with(64, 200)
        self.normal.events.read.assert_not_called()
        report = build_offer_report(OFFER)
        self.transport.set_output(report)
        pointer, buffer, length = library.hid_send_output_report.call_args.args
        self.assertEqual((pointer, bytes(buffer), length),
                         (123, bytes.fromhex('2d00400f00715000480000000004000000'), 17))
        library.hid_send_output_report.return_value = 16
        with self.assertRaisesRegex(DeviceError, '16/17'):
            self.transport.set_output(report)
        content = b'\x2a' + bytes(range(60))
        library.hid_send_output_report.return_value = 61
        self.transport.set_output(content)
        pointer, buffer, length = library.hid_send_output_report.call_args.args
        self.assertEqual((pointer, bytes(buffer), length), (123, content, 61))
        library.hid_write.assert_not_called()
        library.hid_send_feature_report.assert_not_called()

    def test_no_operation_uses_a_closed_session(self):
        self.transport.normal = None
        for operation in (self.transport.get_feature, lambda: self.transport.read_event(0),
                          lambda: self.transport.set_output(build_offer_report(OFFER))):
            with self.subTest(operation=operation), self.assertRaisesRegex(DeviceError, "Open"):
                operation()


if __name__ == "__main__":
    unittest.main()
