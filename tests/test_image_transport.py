"""Background SET/GET contracts with mock ioctl and Darwin native handles."""

from contextlib import ExitStack
import sys
import unittest
from unittest.mock import Mock, patch

from swarm2.macos import MacOSTransport
from swarm2.protocol import ProtocolError
from swarm2.transport import DeviceError, HidrawTransport


def packet(command=0xF1):
    return bytes((0x10, 0xA5, command)) + bytes(61)


@unittest.skipIf(sys.platform == "win32", "Exercises POSIX desktop or USB APIs")
class ImageTransportTests(unittest.TestCase):
    def backend(self, platform, *, reply=b"\x10\xa5\xf1", write_count=64, get_count=None):
        """No session discovery, device opens or actual I/O occur in these tests."""
        stack = ExitStack()
        self.addCleanup(stack.close)
        events = []
        if platform == "linux":
            transport = HidrawTransport("fixture")
            transport.control, transport.events = 12, 11

            def ioctl(fd, request, buffer, mutate):
                self.assertEqual(fd, 12)
                self.assertTrue(mutate)
                self.assertEqual((request >> 16) & 0x3FFF, 64)
                if request & 255 == 6:
                    events.append(("set", bytes(buffer)))
                    return write_count
                self.assertEqual(request & 255, 7)
                self.assertEqual(buffer[:2], b"\x10\xa2")
                events.append(("get", bytes(buffer[:2])))
                buffer[:len(reply)] = reply
                return len(reply) if get_count is None else get_count

            stack.enter_context(patch("fcntl.ioctl", side_effect=ioctl))
            stack.enter_context(patch("swarm2.transport.os.read", side_effect=AssertionError("No event read during image transfer")))
            module = "swarm2.transport"
        else:
            transport = MacOSTransport()
            transport._opened = True
            transport.events = Mock()
            transport.events.read.side_effect = AssertionError("No event read during image transfer")
            transport.control = Mock()

            def send(report):
                events.append(("set", bytes(report)))
                return write_count

            def get(report_id, size, selector):
                self.assertEqual((report_id, size, selector), (0x10, 64, 0xA2))
                events.append(("get", report_id))
                return reply

            transport.control.send_feature_report.side_effect = send
            transport.control.get_feature_report.side_effect = get
            module = "swarm2.macos"
        stack.enter_context(patch(module + ".time.sleep", side_effect=lambda seconds: events.append(("sleep", seconds))))
        return transport, events

    def test_linux_and_mac_preserve_report_and_do_one_ordered_set_get_pair(self):
        for platform in ("linux", "mac"):
            with self.subTest(platform=platform):
                reply = b"\x10\xa5\x03\x7f"  # Byte3 is not a proven status field.
                transport, events = self.backend(platform, reply=reply)
                outgoing = bytes.fromhex("10a503deadbe") + bytes(58)
                self.assertEqual(transport.exchange_image(outgoing, delay_ms=30), reply)
                self.assertEqual(events[0], ("set", outgoing))
                self.assertEqual([event[0] for event in events], ["set", "sleep", "get", "sleep"])
                self.assertEqual([event[1] for event in events if event[0] == "sleep"], [0.03, 0.03])
                self.assertEqual(transport.acknowledgements, [])

    def test_finish_waits_two_and_half_seconds_before_get(self):
        for platform in ("linux", "mac"):
            with self.subTest(platform=platform):
                transport, events = self.backend(platform, reply=b"\x10\xa2\xff")
                self.assertEqual(transport.exchange_image(packet(0xFF), delay_ms=2500), b"\x10\xa2\xff")
                self.assertEqual(events[1], ("sleep", 2.5))
                self.assertEqual([event[0] for event in events].count("get"), 1)

    def test_incomplete_set_aborts_without_get_sleep_or_retry(self):
        for platform in ("linux", "mac"):
            for count in (-1, 0, 63, 65):
                with self.subTest(platform=platform, count=count):
                    transport, events = self.backend(platform, write_count=count)
                    with self.assertRaisesRegex(DeviceError, "Incomplete background transfer"):
                        transport.exchange_image(packet(), delay_ms=30)
                    self.assertEqual(events, [("set", packet())])

    def test_wrong_f1_reply_or_short_read_aborts_without_retransmitting(self):
        for platform in ("linux", "mac"):
            for reply in (b"", b"\x10\xa5", b"\x10\xa5\xf2",
                          b"\x04\xa5\xf1"):
                with self.subTest(platform=platform, reply=reply):
                    transport, events = self.backend(platform, reply=reply)
                    with self.assertRaises((ProtocolError, DeviceError)):
                        transport.exchange_image(packet(), delay_ms=30)
                    self.assertEqual([event[0] for event in events].count("set"), 1)
                    self.assertEqual([event[0] for event in events].count("get"), 1)

    def test_firmware_504_ff_filled_data_response_is_accepted_on_linux_and_mac(self):
        for platform in ("linux", "mac"):
            with self.subTest(platform=platform):
                reply = bytes((0x10,)) + bytes((0xFF,)) * 63
                transport, events = self.backend(platform, reply=reply)
                self.assertEqual(transport.exchange_image(packet(0x3D), delay_ms=30),
                                 reply)
                self.assertEqual([event[0] for event in events].count("set"), 1)
                self.assertEqual([event[0] for event in events].count("get"), 1)

    def test_control_phase_mismatch_is_not_accepted_as_packet_completion(self):
        for platform in ("linux", "mac"):
            with self.subTest(platform=platform):
                transport, events = self.backend(platform, reply=b"\x10\xff\xf1")
                with self.assertRaisesRegex(ProtocolError, "command echo"):
                    transport.exchange_image(packet(0xF2), delay_ms=30)
                self.assertEqual([event[0] for event in events].count("set"), 1)
                self.assertEqual([event[0] for event in events].count("get"), 1)

    def test_unknown_selector_is_polled_without_replaying_the_packet(self):
        for platform in ("linux", "mac"):
            with self.subTest(platform=platform):
                transport, events = self.backend(platform)
                replies = iter((b"\x10\x00\x00", b"\x10\xa2\xf1"))
                def get_feature(selector):
                    self.assertEqual(selector, 0xA2)
                    events.append(("get", selector))
                    return next(replies)
                with patch.object(transport, "_get_feature_now", side_effect=get_feature):
                    self.assertEqual(transport.exchange_image(packet(), delay_ms=30),
                                     b"\x10\xa2\xf1")
                self.assertEqual([event[0] for event in events].count("set"), 1)
                self.assertEqual([event[0] for event in events].count("get"), 2)

    def test_sixteen_unknown_selectors_fail_without_replaying_the_packet(self):
        for platform in ("linux", "mac"):
            with self.subTest(platform=platform):
                transport, events = self.backend(platform)
                def get_feature(selector):
                    self.assertEqual(selector, 0xA2)
                    events.append(("get", selector))
                    return b"\x10\x00\x00"
                with patch.object(transport, "_get_feature_now", side_effect=get_feature):
                    with self.assertRaisesRegex(ProtocolError, "selector"):
                        transport.exchange_image(packet(), delay_ms=30)
                self.assertEqual([event[0] for event in events].count("set"), 1)
                self.assertEqual([event[0] for event in events].count("get"), 16)

    def test_linux_rejects_ioctl_get_count_outside_buffer(self):
        for count in (-1, 0, 65):
            with self.subTest(count=count):
                transport, events = self.backend("linux", get_count=count)
                with self.assertRaisesRegex(DeviceError, "Invalid feature response length"):
                    transport.exchange_image(packet(), delay_ms=30)
                self.assertEqual([event[0] for event in events].count("set"), 1)

    def test_invalid_command_size_and_delay_never_reach_backend(self):
        cases = [(bytes(64), 30), (packet()[:-1], 30), (packet() + b"\0", 30),
                 (packet(62), 30), (packet(104), 30), (packet(125), 30),
                 (packet(), True),
                 (packet(), -30), (packet(), 0), (packet(), 2.5),
                 (packet(0xFF), 30), (packet(), 2500)]
        for platform in ("linux", "mac"):
            for report, delay in cases:
                with self.subTest(platform=platform, report=report[:3], delay=delay):
                    transport, events = self.backend(platform)
                    with self.assertRaisesRegex(DeviceError, "Unsupported background transfer"):
                        transport.exchange_image(report, delay_ms=delay)
                    self.assertEqual(events, [])

    def test_custom_icon_selection_uses_the_same_single_set_get_exchange(self):
        for platform in ("linux", "mac"):
            for selector in (105, 124):
                with self.subTest(platform=platform, selector=selector):
                    transport, events = self.backend(
                        platform, reply=bytes((0x10, 0xA5, selector)))
                    response = transport.exchange_image(
                        packet(selector), delay_ms=30)
                    self.assertEqual(response, bytes((0x10, 0xA5, selector)))
                    self.assertEqual(
                        [event[0] for event in events],
                        ["set", "sleep", "get", "sleep"])

    def test_mac_closed_session_never_touches_native_handle(self):
        transport = MacOSTransport()
        transport.control = Mock()
        with self.assertRaisesRegex(DeviceError, "Open an MC7 session"):
            transport.exchange_image(packet(), delay_ms=30)
        self.assertEqual(transport.control.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
