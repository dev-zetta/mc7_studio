"""Host LCD transactions against captured records; never access a real mouse."""

import copy
import unittest
from unittest.mock import patch

from swarm2.host_lcd import update_host_lcd
from swarm2.host_metrics import MetricsUnavailable
from swarm2.transport import DeviceError
from tests.test_settings import CAPTURES, SettingsTransport, with_checksum


class HostLcdTransport(SettingsTransport):
    """A3 acknowledges a transient value without changing stored settings."""

    def __init__(self):
        super().__init__()
        self.telemetry = []
        self.reads = []
        self.elapsed = 0
        self.send_times = []
        self.sleeps = []
        self.after_telemetry = None
        self.fail_telemetry_number = None

    def send(self, packet):
        if packet[1] != 0xA3:
            return super().send(packet)
        if not isinstance(packet, bytes) or len(packet) != 64 or packet[0] != 0x10:
            raise AssertionError("Expected a complete MC7 A3 feature report")
        self.sent.append(packet)
        self.writes.append(packet)
        self.telemetry.append(packet)
        self.send_times.append(self.elapsed)
        if len(self.telemetry) == self.fail_telemetry_number:
            raise DeviceError("Simulated telemetry acknowledgement timeout")
        self.acknowledgements.append("1000f2a300000000")
        if self.after_telemetry:
            self.after_telemetry(self)

    def get_feature(self, selector):
        self.reads.append(selector)
        return super().get_feature(selector)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.elapsed += seconds


def lcd_capture(pages, *, profile=0, page_count=3):
    """Write wire pairs directly, independent of the production LCD builder."""
    raw = bytearray.fromhex(CAPTURES["lcd"])
    raw[3:5] = bytes((profile, page_count))
    for page_index, slots in pages.items():
        if len(slots) != 4:
            raise AssertionError("A fixture needs four logical slots")
        pairs = [bytes((value, 0xFF)) if value is not None else bytes(2)
                 for value in reversed(slots)]
        start = 8 + 11 * page_index
        raw[start:start + 8] = b"".join(pairs)
    return with_checksum(raw)


def profile_capture(*, active=0, count=5, energy=0):
    return with_checksum(bytes((0x10, 0x12, 0, active, count | energy, 0)))


class HostLcdTransactionTests(unittest.TestCase):
    def setUp(self):
        self.transport = HostLcdTransport()
        self.transport.records["lcd"] = lcd_capture({
            0: [0x3A, 0x41, 0x64, 0xFE],
            1: [0x65, None, None, 0x64],
            2: [0xFE, 0xFE, 0xFE, 0x41],
        })
        sleeper = patch("swarm2.host_lcd.time.sleep", side_effect=self.transport.sleep)
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def request(self, *, slot=1):
        return {
            "device_id": "fixture-mc7", "profile_slot": slot,
            "cpu_percent": 42.9, "ram_percent": 73.1,
            "baseline": {
                "device_id": "fixture-mc7", "profile_slot": slot,
                "transport_identity": self.transport.location_id,
                "settings": {name: self.transport.records[name].hex()
                             for name in ("lcd", "profile")},
            },
        }

    def run_update(self, request=None):
        return update_host_lcd(self.request() if request is None else request, self.transport)

    def assert_no_telemetry(self):
        self.assertEqual(self.transport.telemetry, [])
        self.assertEqual(self.transport.writes, [])

    def test_updates_only_known_active_widgets_and_returns_acknowledgement(self):
        request = self.request()
        original_request = copy.deepcopy(request)
        original_records = copy.deepcopy(self.transport.records)
        result = self.run_update(request)
        self.assertEqual(result, {
            "acknowledged": True, "widgets": 3, "reports": 2,
            "cpu_percent": 42, "ram_percent": 73,
            "gpu_percent": None, "cpu_temperature_c": None,
            "gpu_temperature_c": None,
            "updated_values": {"cpu_load": 42, "ram_usage": 73},
            "unavailable_widgets": [],
        })
        records = [packet[4 + index * 9:13 + index * 9]
                   for packet in self.transport.telemetry for index in range(packet[3])]
        self.assertEqual(records, [
            bytes.fromhex("010304020000000000"),  # CPU low icon, logical slot0.
            bytes.fromhex("0103052a0000000000"),  # CPU42, truncated once.
            bytes.fromhex("010204000000000000"),  # RAM high icon, logical slot1.
            bytes.fromhex("010205490000000000"),
            bytes.fromhex("030004000000000000"),  # RAM on page3, logical slot3.
            bytes.fromhex("030005490000000000"),
        ])
        self.assertEqual(self.transport.reads,
                         [0x12, 0x25, 0x12, 0x25, 0x12, 0x25])
        self.assertEqual(self.transport.records, original_records)
        self.assertEqual(request, original_request)
        # No configuration writes or application-presence toggles are involved.
        self.assertEqual({packet[1] for packet in self.transport.sent}, {0x1C, 0xA3})

    def test_full_layout_is_bounded_and_report_sends_are_spaced(self):
        self.transport.records["lcd"] = lcd_capture({
            page: [0x3A, 0x41, 0x3A, 0x41] for page in range(3)})
        request = self.request()
        request.update(cpu_percent=0, ram_percent=100)
        result = self.run_update(request)
        self.assertEqual((result["widgets"], result["reports"]), (12, 6))
        self.assertEqual([packet[3] for packet in self.transport.telemetry], [6, 2] * 3)
        self.assertTrue(all(len(packet) == 64 for packet in self.transport.telemetry))
        for earlier, later in zip(self.transport.send_times, self.transport.send_times[1:]):
            self.assertGreaterEqual(later - earlier + 1e-12, 0.03)
        self.assertTrue(all(delay >= 0.03 for delay in self.transport.sleeps))
        self.assertEqual(len(self.transport.acknowledgements), 6)

    def test_invalid_percentages_fail_before_any_io(self):
        for field in ("cpu_percent", "ram_percent"):
            for value in (-1, 100.01, True, False, "42", None, [], {},
                          float("nan"), float("inf"), float("-inf")):
                with self.subTest(field=field, value=value):
                    request = self.request()
                    request[field] = value
                    with self.assertRaises(MetricsUnavailable):
                        self.run_update(request)
                    self.assertEqual(self.transport.sent, [])
                    self.assertEqual(self.transport.reads, [])

    def test_gpu_and_temperature_widgets_use_supplied_measurements(self):
        self.transport.records["lcd"] = lcd_capture({
            0: [0x38, 0x39, 0x37, 0x3A],
            1: [0x41, 0xFE, 0xFE, 0xFE],
            2: [0xFE, 0xFE, 0xFE, 0xFE],
        })
        request = self.request()
        request.update(gpu_percent=49.9, cpu_temperature_c=50.8,
                       gpu_temperature_c=73.2)
        result = self.run_update(request)
        self.assertEqual(result["updated_values"], {
            "cpu_load": 42, "cpu_temperature": 50, "gpu_load": 49,
            "gpu_temperature": 73, "ram_usage": 73,
        })
        records = [packet[4 + index * 9:13 + index * 9]
                   for packet in self.transport.telemetry for index in range(packet[3])]
        self.assertIn(bytes.fromhex("010305310000000000"), records)
        self.assertIn(bytes.fromhex("010205320000000000"), records)
        self.assertIn(bytes.fromhex("010105490000000000"), records)

    def test_unavailable_optional_measurement_is_skipped_without_fake_zero(self):
        self.transport.records["lcd"] = lcd_capture({
            0: [0x38, 0x3A, 0xFE, 0xFE],
            1: [0xFE] * 4, 2: [0xFE] * 4,
        })
        result = self.run_update()
        self.assertEqual(result["updated_values"], {"cpu_load": 42})
        self.assertEqual(result["unavailable_widgets"], ["gpu_load"])
        values = [packet[4 + index * 9 + 3:4 + index * 9 + 5]
                  for packet in self.transport.telemetry for index in range(packet[3])
                  if packet[4 + index * 9 + 2] == 5]
        self.assertEqual(values, [bytes.fromhex("2a00")])

    def test_only_unavailable_optional_widgets_fail_before_io(self):
        self.transport.records["lcd"] = lcd_capture({
            0: [0x38, 0x39, 0x37, 0xFE],
            1: [0xFE] * 4, 2: [0xFE] * 4,
        })
        with self.assertRaisesRegex(DeviceError, "did not expose"):
            self.run_update()
        self.assert_no_telemetry()
        self.assertEqual(self.transport.reads, [])

    def test_invalid_optional_measurements_fail_before_io(self):
        for field, values in (
            ("gpu_percent", (-1, 101, True, "5")),
            ("cpu_temperature_c", (-1, 0x10000, True, "5")),
            ("gpu_temperature_c", (-1, 0x10000, True, "5")),
        ):
            for value in values:
                with self.subTest(field=field, value=value):
                    request = self.request()
                    request[field] = value
                    with self.assertRaises(MetricsUnavailable):
                        self.run_update(request)
                    self.assertEqual(self.transport.sent, [])
                    self.assertEqual(self.transport.reads, [])

    def test_invalid_profile_slots_fail_before_any_io(self):
        for slot in (0, 6, True, "1", 1.0, None):
            with self.subTest(slot=slot):
                with self.assertRaisesRegex(DeviceError, "Profile slot"):
                    self.run_update(self.request(slot=slot))
                self.assertEqual(self.transport.sent, [])

    def test_missing_or_mismatched_baseline_is_rejected_before_io(self):
        for mutation in (lambda request: request.update(baseline=None),
                         lambda request: request["baseline"].update(device_id="another-mouse"),
                         lambda request: request["baseline"].update(profile_slot=2),
                         lambda request: request["baseline"].update(transport_identity="another-port"),
                         lambda request: request["baseline"].pop("settings"),
                         lambda request: request["baseline"]["settings"].pop("lcd"),
                         lambda request: request["baseline"]["settings"].pop("profile"),
                         lambda request: request["baseline"]["settings"].update(lcd="not hex")):
            with self.subTest(mutation=mutation):
                request = self.request()
                mutation(request)
                with self.assertRaises(DeviceError):
                    self.run_update(request)
                self.assertEqual(self.transport.sent, [])

    def test_requested_inactive_profile_is_rejected_before_io(self):
        self.transport.records["lcd"] = lcd_capture({0: [0x3A] * 4}, profile=1)
        with self.assertRaisesRegex(DeviceError, "active mouse profile"):
            self.run_update(self.request(slot=2))
        self.assertEqual(self.transport.sent, [])

    def test_an_active_later_profile_uses_its_own_lcd_selector(self):
        self.transport.records["profile"] = profile_capture(active=4)
        self.transport.records["lcd"] = lcd_capture({0: [0x3A, 0xFE, 0xFE, 0xFE]}, profile=4)
        result = self.run_update(self.request(slot=5))
        self.assertTrue(result["acknowledged"])
        selectors = [packet for packet in self.transport.sent if packet[1] == 0x1C]
        self.assertEqual([(packet[3], packet[4]) for packet in selectors],
                         [(0x12, 0), (0x25, 4), (0x12, 0), (0x25, 4)])

    def test_stale_layout_including_reserved_and_hidden_pages_sends_no_values(self):
        for offset in (8, 6, 39):
            with self.subTest(offset=offset):
                original = self.transport.records["lcd"]
                request = self.request()
                raw = bytearray(original)
                raw[offset] ^= 1
                self.transport.records["lcd"] = with_checksum(raw)
                with self.assertRaisesRegex(DeviceError, "LCD layout changed"):
                    self.run_update(request)
                self.assert_no_telemetry()
                self.transport.records["lcd"] = original

    def test_changed_active_profile_or_metadata_sends_no_values(self):
        request = self.request()
        for raw in (profile_capture(active=1), profile_capture(count=4),
                    profile_capture(energy=0xF0)):
            with self.subTest(raw=raw.hex()):
                self.transport.records["profile"] = raw
                with self.assertRaisesRegex(DeviceError, "profile.*changed"):
                    self.run_update(request)
                self.assert_no_telemetry()

    def test_equivalent_energy_encodings_do_not_create_a_false_conflict(self):
        self.transport.records["profile"] = profile_capture(energy=0x10)
        request = self.request()
        self.transport.records["profile"] = profile_capture(energy=0xF0)
        self.assertTrue(self.run_update(request)["acknowledged"])

    def test_no_values_when_widgets_exist_only_on_inactive_or_hidden_pages(self):
        self.transport.records["lcd"] = lcd_capture({
            0: [0x64, 0xFE, 0xFE, 0xFE], 1: [0x3A] * 4,
            2: [0x41] * 4, 3: [0x3A] * 4, 4: [0x41] * 4,
        }, page_count=1)
        with self.assertRaisesRegex(DeviceError, "system monitoring widget"):
            self.run_update()
        self.assert_no_telemetry()

    def test_unknown_widget_subtype_is_preserved_and_not_targeted(self):
        raw = bytearray(lcd_capture({0: [0x3A, 0x3A, 0xFE, 0xFE],
                                     1: [0xFE] * 4, 2: [0xFE] * 4}))
        raw[13] = 0x11  # Logical slot1 has unknown CPU subtype, unlike slot0.
        self.transport.records["lcd"] = with_checksum(raw)
        before = self.transport.records["lcd"]
        result = self.run_update()
        self.assertEqual((result["widgets"], result["reports"]), (1, 1))
        self.assertEqual(self.transport.telemetry[0][3:7], bytes((2, 1, 3, 4)))
        self.assertEqual(self.transport.records["lcd"], before)

    def test_failed_fresh_read_sends_no_values(self):
        for name in ("profile", "lcd"):
            with self.subTest(name=name):
                self.transport.read_errors = {name: "Fixture read failed"}
                with self.assertRaisesRegex(DeviceError, "Fixture read failed"):
                    self.run_update()
                self.assert_no_telemetry()

    def test_profile_or_layout_change_after_send_does_not_report_success(self):
        original = copy.deepcopy(self.transport.records)
        for name, replacement in (
            ("profile", profile_capture(active=1)),
            ("profile", profile_capture(count=4)),
            ("profile", profile_capture(energy=0xF0)),
            ("lcd", lcd_capture({0: [0x64, 0x41, 0x64, 0xFE]})),
        ):
            with self.subTest(name=name, replacement=replacement.hex()):
                self.transport.records = copy.deepcopy(original)
                request = self.request()
                self.transport.after_telemetry = lambda transport: transport.records.update({name: replacement})
                before = len(self.transport.telemetry)
                with self.assertRaisesRegex(DeviceError, "changed"):
                    self.run_update(request)
                self.assertEqual(len(self.transport.telemetry) - before, 1)

    def test_telemetry_ack_failure_stops_without_remaining_reports(self):
        self.transport.fail_telemetry_number = 1
        with self.assertRaisesRegex(DeviceError, "acknowledgement timeout"):
            self.run_update()
        self.assertEqual(len(self.transport.telemetry), 1)
        self.assertEqual(self.transport.reads, [0x12, 0x25])

    def test_readback_failure_is_not_an_acknowledged_success(self):
        self.transport.fail_reads_after_write = {"lcd"}
        with self.assertRaisesRegex(DeviceError, "readback disconnect"):
            self.run_update()
        self.assertEqual(len(self.transport.telemetry), 1)
        self.assertEqual(self.transport.reads, [0x12, 0x25, 0x12, 0x25])


if __name__ == "__main__":
    unittest.main()
