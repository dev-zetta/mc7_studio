"""Captured read vectors and transaction failure cases; no real device I/O."""

import unittest

from swarm2.hardware import transact
from swarm2.host_metrics import MetricsUnavailable
from swarm2.protocol import (ProtocolError, build_physical_dpi_report,
                             decode_sensor_response)
from swarm2.service import DeviceService
from swarm2.transport import DeviceError

# MC7 USB profile 1, captured 2026-09-15. GET_FEATURE returned 48 bytes.
CAPTURE = bytes.fromhex("10100000020200000004010700ff000001010f0000ff000101170000646401011f000000ff01013f00e320ca0185014b")


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.sent = []
        self.acknowledgements = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def send(self, report):
        self.sent.append(report)

    def get_sensor(self):
        return self.responses.pop(0)


def request():
    return {"operation": "apply", "device_id": "fixture", "profile_slot": 1,
            "baseline": {"device_id": "fixture", "profile_slot": 1, "raw": CAPTURE.hex()},
            "dpi": {"current_stage": 4, "dpi": [450, 800, 1200, 1600, 3200],
                    "colors": [[255, 0, 0], [0, 255, 0], [0, 100, 100], [0, 0, 255], [227, 32, 202]],
                    "enabled": [True]*5, "indicator_enabled": True}}


class SensorTests(unittest.TestCase):
    def test_captured_read_units_and_colors(self):
        state = decode_sensor_response(CAPTURE, 0)
        self.assertEqual(state.dpi, (400, 800, 1200, 1600, 3200))
        self.assertEqual(state.current_stage, 4)
        self.assertEqual(state.colors[-1], (227, 32, 202))
        self.assertEqual(state.enabled, (True,)*5)

    def test_rejects_wrong_identity_and_invalid_values(self):
        for offset, value in ((0, 0), (1, 0x14), (2, 1), (3, 1), (9, 5), (10, 2), (12, 255), (23, 0)):
            bad = bytearray(CAPTURE)
            bad[offset] = value
            with self.subTest(offset=offset), self.assertRaises(ProtocolError):
                decode_sensor_response(bad, 0)
        with self.assertRaises(ProtocolError):
            decode_sensor_response(CAPTURE[:47], 0)

    def test_physical_write_encoding(self):
        data = build_physical_dpi_report(profile_index=0, **request()["dpi"])
        self.assertEqual(data[:14].hex(), "1014049f08000f0017001f003f00")
        self.assertEqual(data[14:29].hex(), "ff000000ff000064640000ffe320ca")
        self.assertEqual(data[29:], bytes(35))

    def test_write_limits(self):
        for values in ([49]*5, [30100]*5, [801]*5, [True]*5):
            settings = request()["dpi"]
            settings["dpi"] = values
            with self.assertRaises(ProtocolError):
                build_physical_dpi_report(profile_index=0, **settings)

    def test_read_apply_and_readback(self):
        after = bytearray(CAPTURE)
        after[11] = 8
        transport = FakeTransport([CAPTURE, after])
        result = transact(request(), lambda _: transport)
        self.assertTrue(result["changed"])
        self.assertTrue(transport.closed)
        self.assertEqual([p[1] for p in transport.sent], [0x1c, 0x14, 0x1c])
        self.assertEqual(bytes.fromhex(result["raw"])[11], 8)

    def test_stale_baseline_never_writes(self):
        modified = bytearray(CAPTURE)
        modified[9] = 1
        transport = FakeTransport([modified])
        with self.assertRaisesRegex(DeviceError, "changed since"):
            transact(request(), lambda _: transport)
        self.assertEqual([p[1] for p in transport.sent], [0x1c])

    def test_wrong_device_or_profile_never_writes(self):
        for key, value in (("device_id", "different"), ("profile_slot", 2)):
            data = request()
            data["baseline"][key] = value
            transport = FakeTransport([CAPTURE])
            with self.assertRaises(DeviceError):
                transact(data, lambda _: transport)
            self.assertEqual(len(transport.sent), 1)

    def test_invalid_live_lcd_request_is_rejected_before_transport_open(self):
        cases = [
            {"operation": "host_lcd", "device_id": "fixture", "profile_slot": 1,
             "cpu_percent": None, "ram_percent": 20, "baseline": None},
            {"operation": "host_lcd", "device_id": "fixture", "profile_slot": 1,
             "cpu_percent": 10, "ram_percent": 20, "baseline": None},
        ]
        for data in cases:
            opened = []
            with self.subTest(data=data), self.assertRaises((MetricsUnavailable, DeviceError)):
                transact(data, lambda device_id: opened.append(device_id))
            self.assertEqual(opened, [])

    def test_macos_location_change_never_writes(self):
        data = request()
        data["baseline"]["transport_identity"] = "123"
        transport = FakeTransport([CAPTURE])
        transport.location_id = 456
        with self.assertRaisesRegex(DeviceError, "USB location changed"):
            transact(data, lambda _: transport)
        self.assertEqual(len(transport.sent), 1)

    def test_readback_mismatch_is_explicitly_uncertain(self):
        transport = FakeTransport([CAPTURE, CAPTURE])
        with self.assertRaisesRegex(DeviceError, "may have changed"):
            transact(request(), lambda _: transport)

    def test_unrelated_sensor_change_is_not_reported_as_success(self):
        after = bytearray(CAPTURE)
        after[11] = 8
        after[46] = 0
        transport = FakeTransport([CAPTURE, after])
        with self.assertRaisesRegex(DeviceError, "Unrelated sensor settings"):
            transact(request(), lambda _: transport)

    def test_noop_does_not_write(self):
        data = request()
        data["dpi"]["dpi"][0] = 400
        transport = FakeTransport([CAPTURE])
        result = transact(data, lambda _: transport)
        self.assertFalse(result["changed"])
        self.assertEqual(len(transport.sent), 1)

    def test_snapshot_marks_only_confirmed_fields(self):
        snapshot = DeviceService._snapshot("fixture", 1, {"raw": CAPTURE.hex(), "changed": False})
        self.assertEqual(snapshot["summary"]["dpi"], 3200)
        self.assertNotIn("sensor.polling_rate", snapshot["verified_fields"])
        self.assertEqual(snapshot["configuration"].to_dict()["source"], "local_draft")


if __name__ == "__main__":
    unittest.main()
