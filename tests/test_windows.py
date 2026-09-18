"""Windows HID transport contract tests with no physical USB access."""

import unittest

from swarm2.protocol import build_sensor_read_request
from swarm2.transport import DeviceError
from swarm2.windows import WindowsTransport, _WindowsControlHandle, _candidate_paths


def record(path, interface, **overrides):
    return {"path": path, "interface_number": interface, "vendor_id": 0x10F5,
            "product_id": 0x502C, "bus_type": 1, **overrides}


class FakeHandle:
    def __init__(self):
        self.sent = []
        self.reads = []
        self.gets = []

    def send_feature_report(self, report):
        self.sent.append(report)
        return len(report)

    def read(self, _length, _timeout=0):
        return self.reads.pop(0) if _timeout and self.reads else b""

    def get_feature_report(self, report_id, length, selector):
        self.gets.append((report_id, length, selector))
        return b"\x10\x10" + bytes(46)


class WindowsTransportTests(unittest.TestCase):
    def transport(self):
        value = WindowsTransport()
        value.control = FakeHandle()
        value.events = FakeHandle()
        value._opened = True
        return value

    def test_requires_exactly_one_control_and_event_interface(self):
        paths = _candidate_paths([record(b"event", 1), record(b"control", 2)])
        self.assertEqual(paths, {b"event": 1, b"control": 2})
        for records in ([record(b"control", 2)],
                        [record(b"event", 1), record(b"control", 2), record(b"other", 2)],
                        [record(b"event", -1), record(b"control", 2)]):
            with self.subTest(records=records), self.assertRaises(DeviceError):
                _candidate_paths(records)

    def test_feature_selector_and_acknowledgement_match_existing_protocol(self):
        transport = self.transport()
        transport.events.reads = [bytes.fromhex("1000f21c00000000")]
        report = build_sensor_read_request(0)
        transport.send(report)
        self.assertEqual(transport.control.sent, [report])
        self.assertEqual(transport.get_sensor(), b"\x10\x10" + bytes(46))
        self.assertEqual(transport.control.gets, [(0x10, 64, 0x10)])

    def test_rejects_wrong_acknowledgement_without_rewriting(self):
        transport = self.transport()
        transport.events.reads = [bytes.fromhex("1000f21c03000000")]
        with self.assertRaisesRegex(DeviceError, "rejected"):
            transport.send(build_sensor_read_request(0))
        self.assertEqual(len(transport.control.sent), 1)

    def test_native_windows_feature_read_preserves_selector_seed(self):
        class Hid:
            def __init__(self):
                self.write = None
                self.read = None

            def HidD_SetFeature(self, _handle, buffer, length):
                self.write = bytes(buffer[:length])
                return 1

            def HidD_GetFeature(self, _handle, buffer, length):
                self.read = bytes(buffer[:2])
                buffer[2] = 0xAA
                return 1

        class Kernel:
            def CloseHandle(self, _handle):
                return 1

        hid = Hid()
        handle = _WindowsControlHandle(hid, Kernel(), 123, b"descriptor")
        report = build_sensor_read_request(0)
        self.assertEqual(handle.send_feature_report(report), 64)
        self.assertEqual(hid.write, report)
        response = handle.get_feature_report(0x10, 64, 0x25)
        self.assertEqual(hid.read, b"\x10\x25")
        self.assertEqual(response[:3], b"\x10\x25\xaa")


if __name__ == "__main__":
    unittest.main()
