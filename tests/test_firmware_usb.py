"""Linux CFU control transfer and interface ownership tests; no USB access."""
import ctypes
from pathlib import Path
import os
import stat
import tempfile
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch

from swarm2 import firmware_usb as usb
from swarm2.firmware_commands import build_offer_report
from swarm2.transport import DeviceError
from tests.test_firmware_commands import OFFER, VERSION


class FakeLibrary:
    def __init__(self):
        self.descriptor = usb._DeviceDescriptor()
        self.descriptor.idVendor = 0x10F5
        self.descriptor.idProduct = 0x502C
        self.descriptor.bNumConfigurations = 1
        self.endpoints = (usb._EndpointDescriptor * 2)()
        for endpoint, address in zip(self.endpoints, (3, 0x83)):
            endpoint.bEndpointAddress = address
            endpoint.bmAttributes = 3
            endpoint.wMaxPacketSize = 64
        self.alternates = (usb._InterfaceDescriptor * 3)()
        self.interfaces = (usb._Interface * 3)()
        for index in range(3):
            self.alternates[index].bInterfaceNumber = index
            self.alternates[index].bInterfaceClass = 3
            self.interfaces[index].num_altsetting = 1
            self.interfaces[index].altsetting = ctypes.pointer(self.alternates[index])
        self.alternates[2].bNumEndpoints = 2
        self.alternates[2].endpoint = self.endpoints
        self.config = usb._ConfigDescriptor()
        self.config.bNumInterfaces = 3
        self.config.bConfigurationValue = 1
        self.config.interface = self.interfaces

        def pointer(out, value):
            ctypes.cast(out, ctypes.POINTER(ctypes.c_void_p))[0] = value
            return 0

        def descriptor(device, out):
            ctypes.memmove(out, ctypes.byref(self.descriptor), ctypes.sizeof(self.descriptor))
            return 0

        def ports(device, out, maximum):
            out[0] = 2
            return 1

        def config(device, out):
            ctypes.cast(out, ctypes.POINTER(ctypes.POINTER(usb._ConfigDescriptor)))[0] = ctypes.pointer(self.config)
            return 0

        self.libusb_init = Mock(side_effect=lambda out: pointer(out, 301))
        self.libusb_exit = Mock()
        self.libusb_wrap_sys_device = Mock(side_effect=lambda context, fd, out: pointer(out, 401))
        self.libusb_close = Mock()
        self.libusb_get_device = Mock(return_value=501)
        self.libusb_get_device_descriptor = Mock(side_effect=descriptor)
        self.libusb_get_bus_number = Mock(return_value=1)
        self.libusb_get_device_address = Mock(return_value=3)
        self.libusb_get_port_numbers = Mock(side_effect=ports)
        self.libusb_get_active_config_descriptor = Mock(side_effect=config)
        self.libusb_free_config_descriptor = Mock()
        self.libusb_kernel_driver_active = Mock(return_value=1)
        self.libusb_detach_kernel_driver = Mock(return_value=0)
        self.libusb_attach_kernel_driver = Mock(return_value=0)
        self.libusb_claim_interface = Mock(return_value=0)
        self.libusb_release_interface = Mock(return_value=0)
        self.libusb_control_transfer = Mock(return_value=61)
        self.libusb_interrupt_transfer = Mock(return_value=-7)
        self.libusb_error_name = Mock(side_effect=lambda result: f"ERROR_{result}".encode())


class FirmwareLibraryLoadingTests(unittest.TestCase):
    def test_bundled_soname_is_used_when_system_lookup_finds_nothing(self):
        library = FakeLibrary()
        with patch.object(usb, "find_library", return_value=None), \
             patch.object(usb.ctypes, "CDLL", return_value=library) as load:
            self.assertIs(usb._load_library(), library)
        load.assert_called_once_with("libusb-1.0.so.0")

    def test_bundled_soname_is_fallback_when_discovered_library_fails(self):
        library = FakeLibrary()
        with patch.object(usb, "find_library", return_value="broken-libusb.so"), \
             patch.object(usb.ctypes, "CDLL", side_effect=(OSError("missing"), library)) as load:
            self.assertIs(usb._load_library(), library)
        self.assertEqual(
            [call.args[0] for call in load.call_args_list],
            ["broken-libusb.so", "libusb-1.0.so.0"],
        )


@unittest.skipUnless(sys.platform.startswith("linux"), "Exercises Linux USB device numbers and sysfs")
class FirmwareUSBTests(unittest.TestCase):
    def setUp(self):
        self.library = FakeLibrary()
        self.identity = usb._USBIdentity(Path("/sys/devices/usb1/1-2"), 1, 3, (2,), 1)
        self.patches = [
            patch.object(usb, "_identity_from_hidraw", return_value=self.identity),
            patch.object(usb, "_load_library", return_value=self.library),
            patch.object(usb.os, "open", return_value=71),
            patch.object(usb.os, "fstat", return_value=SimpleNamespace(
                st_mode=stat.S_IFCHR | 0o660, st_rdev=os.makedev(189, 2))),
            patch.object(usb.os, "close"),
        ]
        self.identity_mock, _, self.open_mock, self.stat_mock, self.close_mock = [p.start() for p in self.patches]
        for active in reversed(self.patches):
            self.addCleanup(active.stop)
        self.channel = usb.LinuxFirmwareUSB("/dev/hidraw9")
        self.addCleanup(self.channel.close)

    def test_open_checks_bound_identity_without_reports_or_detachment(self):
        self.assertIs(self.channel.open(), self.channel)
        self.open_mock.assert_called_once_with("/dev/bus/usb/001/003", os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
        self.assertEqual(self.identity_mock.call_count, 2)
        self.library.libusb_control_transfer.assert_not_called()
        self.library.libusb_interrupt_transfer.assert_not_called()
        self.library.libusb_detach_kernel_driver.assert_not_called()
        self.library.libusb_claim_interface.assert_not_called()
        self.library.libusb_free_config_descriptor.assert_called_once()
        self.channel.open()
        self.library.libusb_wrap_sys_device.assert_called_once()

    def test_wrong_opened_target_fails_before_claim_and_closes_resources(self):
        self.library.descriptor.idProduct = 0x502E
        with self.assertRaisesRegex(DeviceError, "does not match"):
            self.channel.open()
        self.library.libusb_claim_interface.assert_not_called()
        self.library.libusb_detach_kernel_driver.assert_not_called()
        self.library.libusb_close.assert_called_once()
        self.library.libusb_exit.assert_called_once()
        self.close_mock.assert_called_once_with(71)

    def test_wrong_bus_address_or_ports_fail_before_claim(self):
        for name in ("libusb_get_bus_number", "libusb_get_device_address", "libusb_get_port_numbers"):
            with self.subTest(name=name):
                original = getattr(self.library, name)
                setattr(self.library, name, Mock(return_value=9))
                with self.assertRaisesRegex(DeviceError, "does not match"):
                    self.channel.open()
                setattr(self.library, name, original)
        self.library.libusb_detach_kernel_driver.assert_not_called()

    def test_wrong_configuration_and_endpoints_fail_before_claim(self):
        for entry, field, value in ((self.library.config, "bConfigurationValue", 2),
                                    (self.library.alternates[2], "bInterfaceClass", 255),
                                    (self.library.endpoints[1], "bEndpointAddress", 0x84),
                                    (self.library.endpoints[0], "wMaxPacketSize", 32)):
            old = getattr(entry, field)
            with self.subTest(field=field), self.assertRaises(DeviceError):
                setattr(entry, field, value)
                self.channel.open()
            setattr(entry, field, old)
        self.assertEqual(self.library.libusb_free_config_descriptor.call_count, 4)
        self.library.libusb_claim_interface.assert_not_called()

    def test_sysfs_identity_change_during_open_is_rejected(self):
        other = usb._USBIdentity(self.identity.usb_path, 1, 4, (2,), 1)
        self.identity_mock.side_effect = (self.identity, other)
        with self.assertRaisesRegex(DeviceError, "changed while opening"):
            self.channel.open()
        self.library.libusb_close.assert_called_once()

    def test_wrapped_handle_without_port_chain_uses_verified_sysfs_identity(self):
        self.library.libusb_get_port_numbers = Mock(return_value=0)
        self.channel.open()
        self.assertEqual(self.identity_mock.call_count, 2)
        self.library.libusb_detach_kernel_driver.assert_not_called()
        self.channel.close()
        self.identity_mock.side_effect = (self.identity,
            usb._USBIdentity(Path('/sys/devices/usb1/1-4'), 1, 3, (4,), 1))
        with self.assertRaisesRegex(DeviceError, 'changed while opening'):
            self.channel.open()
        self.library.libusb_detach_kernel_driver.assert_not_called()

    def test_wrong_usb_device_node_fails_before_libusb_open(self):
        for mode, device in ((stat.S_IFREG, os.makedev(189, 2)),
                             (stat.S_IFCHR, os.makedev(189, 3)),
                             (stat.S_IFCHR, os.makedev(188, 2))):
            self.stat_mock.return_value = SimpleNamespace(st_mode=mode, st_rdev=device)
            with self.subTest(mode=mode, device=device), self.assertRaisesRegex(DeviceError, "changed identity"):
                self.channel.open()
        self.library.libusb_init.assert_not_called()

    def test_access_failure_names_updated_udev_rule(self):
        self.open_mock.side_effect = PermissionError("denied")
        with self.assertRaisesRegex(DeviceError, "updated.*70-swarm2-mc7.rules"):
            self.channel.open()
        self.library.libusb_init.assert_not_called()
        self.close_mock.assert_not_called()

    def test_initialization_failure_closes_fd_without_wrapping_or_claiming(self):
        self.library.libusb_init.side_effect = None
        self.library.libusb_init.return_value = -11
        with self.assertRaisesRegex(DeviceError, "initialization failed"):
            self.channel.open()
        self.library.libusb_wrap_sys_device.assert_not_called()
        self.library.libusb_claim_interface.assert_not_called()
        self.library.libusb_exit.assert_not_called()
        self.close_mock.assert_called_once_with(71)

    def test_wrap_failure_releases_context_and_fd_without_detaching(self):
        self.library.libusb_wrap_sys_device.side_effect = None
        self.library.libusb_wrap_sys_device.return_value = -3
        with self.assertRaisesRegex(DeviceError, "open failed"):
            self.channel.open()
        self.library.libusb_close.assert_not_called()
        self.library.libusb_exit.assert_called_once()
        self.library.libusb_detach_kernel_driver.assert_not_called()
        self.close_mock.assert_called_once_with(71)

    def test_claim_detaches_only_vendor_interface_and_is_idempotent(self):
        self.channel.open().claim().claim()
        for name in ("libusb_kernel_driver_active", "libusb_detach_kernel_driver", "libusb_claim_interface"):
            function = getattr(self.library, name)
            function.assert_called_once()
            self.assertEqual(function.call_args.args[1], 2)
        self.channel.close()
        self.library.libusb_release_interface.assert_called_once()
        self.library.libusb_attach_kernel_driver.assert_called_once()
        self.assertEqual(self.library.libusb_attach_kernel_driver.call_args.args[1], 2)

    def test_already_unbound_interface_is_not_reattached_by_us(self):
        self.library.libusb_kernel_driver_active.return_value = 0
        self.channel.open().claim()
        self.channel.close()
        self.library.libusb_detach_kernel_driver.assert_not_called()
        self.library.libusb_attach_kernel_driver.assert_not_called()
        self.library.libusb_release_interface.assert_called_once()

    def test_claim_failure_reattaches_driver_and_preserves_original_error(self):
        self.library.libusb_claim_interface.return_value = -6
        self.channel.open()
        with self.assertRaisesRegex(DeviceError, "claim failed: ERROR_-6"):
            self.channel.claim()
        self.library.libusb_attach_kernel_driver.assert_called_once()
        self.library.libusb_release_interface.assert_not_called()
        self.library.libusb_close.assert_called_once()
        self.close_mock.assert_called_once_with(71)

    def test_detach_failure_does_not_claim_or_reattach(self):
        self.library.libusb_detach_kernel_driver.return_value = -3
        self.channel.open()
        with self.assertRaisesRegex(DeviceError, "detach failed"):
            self.channel.claim()
        self.library.libusb_claim_interface.assert_not_called()
        self.library.libusb_attach_kernel_driver.assert_not_called()
        self.library.libusb_close.assert_called_once()

    def test_failed_cleanup_is_reported_and_all_resources_are_closed(self):
        self.library.libusb_claim_interface.return_value = -6
        self.library.libusb_attach_kernel_driver.return_value = -1
        self.channel.open()
        with self.assertRaisesRegex(DeviceError, "claim failed.*cleanup also failed.*reattach failed"):
            self.channel.claim()
        self.library.libusb_close.assert_called_once()
        self.library.libusb_exit.assert_called_once()
        self.close_mock.assert_called_once_with(71)
        self.channel.close()

    def test_release_failure_still_attempts_reattach_and_closes(self):
        self.channel.open().claim()
        self.library.libusb_release_interface.return_value = -1
        with self.assertRaisesRegex(DeviceError, "release failed"):
            self.channel.close()
        self.library.libusb_attach_kernel_driver.assert_called_once()
        self.library.libusb_close.assert_called_once()

    def test_expected_usb_restart_during_cleanup_is_not_an_error(self):
        self.channel.open().claim()
        self.library.libusb_release_interface.return_value = -4
        self.library.libusb_attach_kernel_driver.return_value = -4
        self.channel.close()
        self.channel.close()
        self.library.libusb_close.assert_called_once()
        self.close_mock.assert_called_once_with(71)

    def test_feature_read_uses_explicit_control_setup_and_maximum_length(self):
        self.channel.open().claim()

        def transfer(handle, request_type, request, value, index, data, length, timeout):
            self.assertEqual((request_type, request, value, index, length, timeout),
                             (0xA1, 1, 0x032A, 2, 64, 2000))
            data[:len(VERSION)] = VERSION
            return len(VERSION)

        self.library.libusb_control_transfer.side_effect = transfer
        self.assertEqual(self.channel.get_feature(), VERSION)
        self.library.libusb_interrupt_transfer.assert_not_called()

    def test_output_uses_control_set_report_and_never_interrupt_out(self):
        self.channel.open().claim()
        for report, expected in ((build_offer_report(OFFER),
                                  bytes.fromhex('2d00400f00715000480000000004000000')),
                                 (b"\x2a" + bytes(range(60)), b"\x2a" + bytes(range(60)))):
            self.library.libusb_control_transfer.return_value = len(expected)
            self.channel.set_output(report)
            arguments = self.library.libusb_control_transfer.call_args.args
            self.assertEqual(arguments[1:5], (0x21, 9, 0x0200 | report[0], 2))
            self.assertEqual(bytes(arguments[5]), expected)
            self.assertEqual(arguments[6:], (len(expected), 2000))
        self.library.libusb_interrupt_transfer.assert_not_called()

    def test_incomplete_and_failed_control_transfer_are_errors_without_retry(self):
        self.channel.open().claim()
        self.library.libusb_control_transfer.return_value = 16
        with self.assertRaisesRegex(DeviceError, "uncertain"):
            self.channel.set_output(build_offer_report(OFFER))
        self.assertEqual(self.library.libusb_control_transfer.call_count, 1)
        self.library.libusb_control_transfer.return_value = 60
        with self.assertRaisesRegex(DeviceError, '60/61'):
            self.channel.set_output(b'\x2a' + bytes(60))
        with self.assertRaisesRegex(DeviceError, "Incomplete CFU version"):
            self.channel.get_feature()
        self.library.libusb_control_transfer.return_value = -7
        with self.assertRaisesRegex(DeviceError, "ERROR_-7"):
            self.channel.set_output(build_offer_report(OFFER))

    def test_reads_only_interrupt_in_with_finite_timeout_including_drain(self):
        self.channel.open().claim()
        for seconds, milliseconds in ((0, 1), (.0001, 1), (.25, 250), (5, 5000)):
            self.assertEqual(self.channel.read_input(seconds), b"")
            arguments = self.library.libusb_interrupt_transfer.call_args.args
            self.assertEqual((arguments[1], arguments[3], arguments[5]), (0x83, 64, milliseconds))

    def test_interrupt_data_and_errors_are_not_silently_discarded(self):
        self.channel.open().claim()
        reply = bytes.fromhex("2d00000000000000000000000001000000")

        def transfer(handle, endpoint, data, length, actual, timeout):
            data[:len(reply)] = reply
            ctypes.cast(actual, ctypes.POINTER(ctypes.c_int))[0] = len(reply)
            return -7

        self.library.libusb_interrupt_transfer.side_effect = transfer
        self.assertEqual(self.channel.read_input(.2), reply)
        self.library.libusb_interrupt_transfer.side_effect = None
        self.library.libusb_interrupt_transfer.return_value = -4
        with self.assertRaisesRegex(DeviceError, "ERROR_-4"):
            self.channel.read_input(.2)

    def test_invalid_report_and_timeout_do_not_reach_usb(self):
        self.channel.open().claim()
        for report in (bytes(61), b"\x2d" + bytes(16), bytearray(61),
                       b"\x2d" + bytes(16) + b"x" + bytes(43)):
            with self.subTest(report=report), self.assertRaises(DeviceError):
                self.channel.set_output(report)
        for timeout in (True, -1, 6, float("inf"), float("nan"), "1", None):
            with self.subTest(timeout=timeout), self.assertRaises(DeviceError):
                self.channel.read_input(timeout)
        for report_id, size in ((0x2B, 64), (0x2A, 61), (True, 64), (0x2A, 64.0)):
            with self.subTest(report_id=report_id, size=size), self.assertRaises(DeviceError):
                self.channel.get_feature(report_id, size)
        self.library.libusb_control_transfer.assert_not_called()
        self.library.libusb_interrupt_transfer.assert_not_called()

    def test_reports_require_claim_and_claim_requires_open(self):
        with self.assertRaisesRegex(DeviceError, "Open"):
            self.channel.claim()
        self.channel.open()
        for operation in (self.channel.get_feature, lambda: self.channel.read_input(0),
                          lambda: self.channel.set_output(build_offer_report(OFFER))):
            with self.assertRaisesRegex(DeviceError, "Claim"):
                operation()


@unittest.skipUnless(sys.platform.startswith("linux"), "Exercises Linux USB device numbers and sysfs")
class SysfsFirmwareIdentityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.usb = root / "devices" / "usb1" / "1-2"
        self.interface = self.usb / "1-2:1.2"
        self.hid = self.interface / "0003:10F5:502C.000C"
        self.hid.mkdir(parents=True)
        self.sysfs = root / "class" / "hidraw" / "hidraw9"
        self.sysfs.mkdir(parents=True)
        (self.sysfs / "device").symlink_to(self.hid)
        for directory, values in ((self.usb, {"idVendor": "10f5", "idProduct": "502c", "busnum": "1",
                                            "devnum": "3", "devpath": "2", "bConfigurationValue": "1"}),
                                  (self.interface, {"bInterfaceNumber": "02", "bAlternateSetting": "0",
                                                    "bInterfaceClass": "03"})):
            for name, value in values.items():
                (directory / name).write_text(value)
        for address in ("03", "83"):
            endpoint = self.interface / f"ep_{address}"
            endpoint.mkdir()
            for name, value in {"bEndpointAddress": address, "bmAttributes": "03", "wMaxPacketSize": "0040"}.items():
                (endpoint / name).write_text(value)
        self.patch = patch.object(usb, "_SYSFS_ROOT", self.sysfs.parent)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_identity_comes_from_selected_control_interface_and_usb_parent(self):
        identity = usb._identity_from_hidraw("/dev/hidraw9")
        self.assertEqual(identity, usb._USBIdentity(self.usb, 1, 3, (2,), 1))
        self.assertEqual(identity.node, "/dev/bus/usb/001/003")

    def test_wrong_interface_device_endpoint_and_port_are_rejected(self):
        for path, value in ((self.usb / "idProduct", "502e"),
                            (self.interface / "bInterfaceNumber", "01"),
                            (self.interface / "bAlternateSetting", "1"),
                            (self.interface / "ep_83" / "wMaxPacketSize", "0020"),
                            (self.usb / "devpath", "0")):
            original = path.read_text()
            path.write_text(value)
            with self.subTest(path=path), self.assertRaises(DeviceError):
                usb._identity_from_hidraw("/dev/hidraw9")
            path.write_text(original)

    def test_arbitrary_path_is_never_used_for_device_discovery(self):
        for path in ("hidraw9", "/tmp/hidraw9", "/dev/hidrawfoo", "/dev/hidraw9/../../other"):
            with self.subTest(path=path), self.assertRaises(DeviceError):
                usb._identity_from_hidraw(path)


if __name__ == "__main__":
    unittest.main()
