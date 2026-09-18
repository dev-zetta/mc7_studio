"""Battery telemetry never becomes a settings-write concurrency requirement."""

import unittest
from unittest.mock import patch

from swarm2.hardware import transact
from swarm2.service import DeviceService
from tests.test_settings import SettingsTransport, with_checksum


class ChangingTelemetryTransport(SettingsTransport):
    def get_feature(self, selector):
        if selector == 0x09:
            raw = bytearray(self.records["status"])
            raw[9] -= 1
            raw[10] ^= 1
            self.records["status"] = with_checksum(raw)
        return super().get_feature(selector)


class SettingsTelemetryTests(unittest.TestCase):
    def run_apply(self, transport, *, missing_before=False, missing_after=False):
        service = DeviceService()
        if missing_before:
            transport.read_errors["status"] = "Battery query unavailable"
        with patch.object(DeviceService, "_call", side_effect=lambda request: transact(request, lambda _: transport)):
            snapshot = service.read("fixture-mc7", 1)
            before = dict(transport.records)
            if missing_after:
                transport.fail_reads_after_write.add("status")
            snapshot["configuration"].lighting.brightness = 50
            result = service.apply_section("fixture-mc7", snapshot["configuration"], "lighting", snapshot["baseline"])
        self.assertTrue(result["summary"]["changed"])
        self.assertEqual([report[1] for report in transport.writes], [0x2A])
        for name in before.keys() - {"lighting", "status"}:
            self.assertEqual(transport.records[name], before[name])
        return snapshot, result

    def test_changing_battery_and_charge_between_all_reads_do_not_fail_apply(self):
        snapshot, result = self.run_apply(ChangingTelemetryTransport())
        self.assertNotEqual(snapshot["baseline"]["settings"]["status"], result["baseline"]["settings"]["status"])
        self.assertNotIn("status", result["errors"])

    def test_absent_status_during_initial_and_final_reads_does_not_fail_apply(self):
        snapshot, result = self.run_apply(SettingsTransport(), missing_before=True)
        self.assertNotIn("status", snapshot["baseline"]["settings"])
        self.assertNotIn("status", result["baseline"]["settings"])
        self.assertIn("status", result["errors"])

    def test_status_becoming_unavailable_after_write_does_not_fail_readback(self):
        snapshot, result = self.run_apply(SettingsTransport(), missing_after=True)
        self.assertIn("status", snapshot["baseline"]["settings"])
        self.assertNotIn("status", result["baseline"]["settings"])
        self.assertIn("status", result["errors"])


if __name__ == "__main__":
    unittest.main()
