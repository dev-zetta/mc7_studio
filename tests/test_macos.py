"""macOS transport contract tests using fake native APIs; no USB I/O."""

from contextlib import ExitStack
import ctypes
import types
import sys
import unittest
from unittest.mock import patch

from swarm2.macos import MacOSTransport, _DarwinHidapi, _NativeHandle
from swarm2.protocol import build_sensor_read_request
from swarm2.transport import DeviceError


def descriptor(kind="feature", report_id=0x10, length=64, usage_page=0xFF01):
    return (bytes((0x06, usage_page & 255, usage_page >> 8, 0x09, 1, 0xA1, 1,
                   0x85, report_id, 0x75, 8, 0x95, length - 1,
                   0xB1 if kind == "feature" else 0x81, 2, 0xC0)))


def record(path, interface, **overrides):
    return {"path": path, "interface_number": interface, "vendor_id": 0x10F5,
            "product_id": 0x502C, "bus_type": 1, "serial_number": "000000000000",
            "usage_page": 0xFF01, "usage": 1, **overrides}


class FakeHandle:
    def __init__(self, backend, interface):
        self.backend = backend
        self.interface = interface
        self.description = descriptor() if interface == 2 else descriptor("input", length=8)
        self.location = 0x123456
        self.closed = False
        self.nonblocking = False
        self.stale = []

    def get_report_descriptor(self):
        return self.description

    def get_location_id(self):
        return self.location

    def set_nonblocking(self, value):
        self.nonblocking = value

    def close(self):
        self.closed = True

    def send_feature_report(self, report):
        self.backend.sent.append(report)
        return self.backend.write_count

    def read(self, length, timeout_ms=0):
        if timeout_ms == 0:
            return self.stale.pop(0) if self.stale else b""
        return self.backend.replies.pop(0) if self.backend.replies else b""

    def get_feature_report(self, report_id, length, selector):
        self.backend.gets.append((report_id, length, selector))
        return self.backend.response


class FakeBackend:
    def __init__(self):
        self.records = [record(b"events", 1), record(b"control", 2)]
        self.handles = {b"events": FakeHandle(self, 1), b"control": FakeHandle(self, 2)}
        self.opened = []
        self.sent = []
        self.gets = []
        self.write_count = 64
        self.response = b"\x10\x10" + bytes(46)
        self.replies = [bytes.fromhex("1000f21c02000000"), bytes.fromhex("1000f21c00000000")]

    def enumerate(self):
        return self.records

    def open_path(self, path):
        self.opened.append(path)
        return self.handles[path]


@unittest.skipIf(sys.platform == "win32", "Exercises POSIX desktop or USB APIs")
class MacOSTransportTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.backend = FakeBackend()
        self.stack.enter_context(patch("swarm2.macos.platform.system", return_value="Darwin"))
        self.loader = self.stack.enter_context(patch("swarm2.macos._load_backend", return_value=self.backend))

    def test_pair_duplicate_collections_and_acknowledge(self):
        self.backend.records += [record(b"events", 1, usage_page=1, usage=6), record(b"pointer", 0)]
        self.backend.handles[b"events"].stale = [bytes.fromhex("1000f21c00000000")]
        self.backend.replies.insert(0, bytes.fromhex("1000f21400000000"))
        with MacOSTransport() as transport:
            transport.send(build_sensor_read_request(0))
            self.assertEqual(transport.get_sensor(), self.backend.response)
            self.assertEqual(transport.location_id, 0x123456)
            self.assertEqual(len(transport.acknowledgements), 2)
            self.assertEqual(self.backend.gets, [(0x10, 64, 0x10)])
            self.assertTrue(self.backend.handles[b"events"].nonblocking)
        self.assertEqual(self.backend.opened, [b"events", b"control"])
        self.assertTrue(all(h.closed for h in self.backend.handles.values()))

    def test_wrong_platform_and_selection_never_open(self):
        with patch("swarm2.macos.platform.system", return_value="Linux"):
            with self.assertRaisesRegex(DeviceError, "requires macOS"):
                with MacOSTransport():
                    pass
        with self.assertRaisesRegex(DeviceError, "Unknown"):
            with MacOSTransport("some-other-mouse"):
                pass
        self.loader.assert_not_called()

    def test_multiple_mice_never_guess_by_zero_serial(self):
        self.backend.records += [record(b"events2", 1), record(b"control2", 2)]
        with self.assertRaisesRegex(DeviceError, "exactly one"):
            with MacOSTransport():
                pass
        self.assertFalse(self.backend.opened)

    def test_pair_requires_identical_nonzero_locations(self):
        for location in (0, 0xFFFFFF):
            with self.subTest(location=location):
                self.backend.handles[b"control"].location = location
                with self.assertRaisesRegex(DeviceError, "physical USB location"):
                    with MacOSTransport():
                        pass
                self.assertTrue(all(h.closed for h in self.backend.handles.values()))

    def test_refuse_wrong_descriptor_identity_and_size(self):
        for desc in (descriptor(report_id=0x11), descriptor(length=63),
                     descriptor(usage_page=1), b"\x06\x01", b""):
            with self.subTest(desc=desc):
                self.backend.handles[b"control"].description = desc
                with self.assertRaises(DeviceError):
                    with MacOSTransport():
                        pass
                self.assertTrue(all(h.closed for h in self.backend.handles.values()))

    def test_unknown_interface_and_foreign_devices_do_not_open(self):
        for changes in ({"interface_number": -1}, {"vendor_id": 0x1234},
                        {"product_id": 0x502E}, {"bus_type": 2}, {"path": b""}):
            with self.subTest(changes=changes):
                self.backend.records = [record(b"events", 1), {**record(b"control", 2), **changes}]
                with self.assertRaises(DeviceError):
                    with MacOSTransport():
                        pass
                self.assertFalse(self.backend.opened)

    def test_conflicting_collection_metadata_fails(self):
        self.backend.records.append(record(b"events", 2))
        with self.assertRaisesRegex(DeviceError, "Ambiguous"):
            with MacOSTransport():
                pass
        self.assertFalse(self.backend.opened)

    def test_busy_forever_has_deadline_and_no_retry_write(self):
        self.backend.replies = [bytes.fromhex("1000f21c02000000")]
        with MacOSTransport() as transport:
            with patch("swarm2.macos.time.monotonic", side_effect=[0, 0, 0, 3]):
                with self.assertRaisesRegex(DeviceError, "timed out"):
                    transport.send(build_sensor_read_request(0))
        self.assertEqual(len(self.backend.sent), 1)

    def test_rejection_and_short_write(self):
        with MacOSTransport() as transport:
            self.backend.replies = [bytes.fromhex("1000f21c03000000")]
            with self.assertRaisesRegex(DeviceError, "rejected"):
                transport.send(build_sensor_read_request(0))
            self.backend.write_count = 63
            with self.assertRaisesRegex(DeviceError, "Incomplete feature transfer"):
                transport.send(build_sensor_read_request(0))

    def test_unsettled_queue_never_writes(self):
        self.backend.handles[b"events"].stale = [b"x"] * 128
        with MacOSTransport() as transport:
            with self.assertRaisesRegex(DeviceError, "did not settle"):
                transport.send(build_sensor_read_request(0))
        self.assertFalse(self.backend.sent)

    def test_sensor_length_and_command_allowlist(self):
        with MacOSTransport() as transport:
            for response in (bytes(47), bytes(49), bytes(65)):
                self.backend.response = response
                with self.assertRaisesRegex(DeviceError, "Incomplete sensor"):
                    transport.get_sensor()
            for report in (bytes(64), b"\x10\xff" + bytes(62), bytes(63)):
                with self.assertRaisesRegex(DeviceError, "Unsupported"):
                    transport.send(report)
        self.assertFalse(self.backend.sent)
        with self.assertRaisesRegex(DeviceError, "Open an MC7"):
            transport.send(build_sensor_read_request(0))


class NativeFunction:
    def __init__(self, implementation):
        self.implementation = implementation

    def __call__(self, *args):
        return self.implementation(*args)


class FakeLibrary:
    def __init__(self):
        self.calls = []
        self.exclusive = 1
        self.handle_exclusive = 0
        for name in ("hid_close", "hid_set_nonblocking", "hid_read_timeout",
                     "hid_get_feature_report", "hid_send_feature_report", "hid_get_report_descriptor"):
            setattr(self, name, NativeFunction(lambda *args, name=name: self._call(name, args)))
        self.hid_init = NativeFunction(lambda: self._call("init", ()))
        self.hid_darwin_set_open_exclusive = NativeFunction(self._set_exclusive)
        self.hid_darwin_get_open_exclusive = NativeFunction(lambda: self.exclusive)
        self.hid_darwin_is_device_open_exclusive = NativeFunction(lambda ptr: self.handle_exclusive)
        self.hid_open_path = NativeFunction(lambda path: self._call("open", (path,)) or 123)
        self.hid_darwin_get_location_id = NativeFunction(self._location)

    def _call(self, name, args):
        self.calls.append((name, args))
        return 0

    def _set_exclusive(self, value):
        self.calls.append(("exclusive", (value,)))
        self.exclusive = value

    @staticmethod
    def _location(pointer, location):
        ctypes.cast(location, ctypes.POINTER(ctypes.c_uint32))[0] = 0x123456
        return 0


class NativeDarwinApiTests(unittest.TestCase):
    def test_initializes_and_disables_exclusive_before_open(self):
        library = FakeLibrary()
        module = types.SimpleNamespace(enumerate=lambda vendor, product: [(vendor, product)])
        backend = _DarwinHidapi(module, library)
        self.assertEqual(backend.enumerate(), [(0x10F5, 0x502C)])
        handle = backend.open_path(b"vendor-interface")
        self.assertEqual(library.calls[:3], [("init", ()), ("exclusive", (0,)), ("open", (b"vendor-interface",))])
        self.assertEqual(handle.get_location_id(), 0x123456)
        self.assertEqual(library.hid_open_path.restype, ctypes.c_void_p)
        handle.close()
        handle.close()
        self.assertEqual(sum(name == "hid_close" for name, _ in library.calls), 1)

    def test_missing_api_fails_before_initialization_or_open(self):
        library = FakeLibrary()
        del library.hid_darwin_get_location_id
        with self.assertRaisesRegex(DeviceError, "lacks"):
            _DarwinHidapi(types.SimpleNamespace(), library)
        self.assertFalse(library.calls)

    def test_changed_global_mode_never_opens(self):
        library = FakeLibrary()
        backend = _DarwinHidapi(types.SimpleNamespace(), library)
        library.exclusive = 1
        with self.assertRaisesRegex(DeviceError, "refusing to seize"):
            backend.open_path(b"vendor")
        self.assertNotIn("open", [name for name, _ in library.calls])

    def test_exclusive_handle_is_closed_immediately(self):
        library = FakeLibrary()
        backend = _DarwinHidapi(types.SimpleNamespace(), library)
        library.handle_exclusive = 1
        with self.assertRaisesRegex(DeviceError, "non-exclusive"):
            backend.open_path(b"vendor")
        self.assertEqual(library.calls[-1], ("hid_close", (123,)))

    def test_native_feature_buffer_and_short_result(self):
        library = FakeLibrary()
        seen = []
        def feature(pointer, buffer, length):
            seen.append(bytes(buffer))
            buffer[3] = 4
            return 48
        library.hid_get_feature_report = NativeFunction(feature)
        _DarwinHidapi(types.SimpleNamespace(), library)
        handle = _NativeHandle(library, 123)
        self.assertEqual(len(handle.get_feature_report(0x10, 64, 0xA2)), 48)
        self.assertEqual(seen, [b"\x10\xa2" + bytes(62)])


if __name__ == "__main__":
    unittest.main()
