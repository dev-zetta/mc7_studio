import subprocess
import unittest
from unittest.mock import patch

from swarm2.host_metrics import GpuSource, HostMetrics
from swarm2.host_media import MediaPlayerInfo
from swarm2.service import DeviceService
from swarm2.transport import DeviceError


class ServiceTests(unittest.TestCase):
    def test_apply_timeout_and_helper_crash_report_uncertain_state(self):
        for result in ("invalid JSON", "null", "[]", "{}"):
            with self.subTest(result=result), patch("swarm2.service.platform.system", return_value="Linux"), patch("swarm2.service.subprocess.run", return_value=subprocess.CompletedProcess([], 1, result, "")):
                with self.assertRaisesRegex(DeviceError, "may have changed"):
                    DeviceService._call({"operation": "apply"})
        with patch("swarm2.service.platform.system", return_value="Darwin"), patch("swarm2.service.subprocess.run", side_effect=subprocess.TimeoutExpired("helper", 12)):
            with self.assertRaisesRegex(DeviceError, "may have changed"):
                DeviceService._call({"operation": "apply"})

    def test_read_timeout_does_not_claim_configuration_changed(self):
        for operation in ("read", "read_active_profile"):
            with self.subTest(operation=operation), patch("swarm2.service.platform.system", return_value="Linux"), patch("swarm2.service.subprocess.run", side_effect=subprocess.TimeoutExpired("helper", 12)):
                with self.assertRaises(DeviceError) as raised:
                    DeviceService._call({"operation": operation})
                self.assertNotIn("may have changed", str(raised.exception))

    def test_active_profile_service_rejects_inconsistent_helper_state(self):
        valid = {"raw": "1012000005fb", "active_profile": 1,
                 "profile_count": 5, "energy_saving": False, "changed": False}
        for change in ({}, {"active_profile": 2}, {"changed": True},
                       {"raw": "1012000105fa"}):
            result = dict(valid)
            result.update(change)
            if not change:
                result.pop("raw")
            with self.subTest(change=change), patch.object(DeviceService, "_call", return_value=result):
                with self.assertRaisesRegex(DeviceError, "invalid active-profile state"):
                    DeviceService().read_active_profile("mouse")

    def test_live_lcd_service_passes_every_sampled_metric(self):
        metrics = HostMetrics(12.9, 34.8, 56.7, 67.6, 78.5)
        with patch("swarm2.host_metrics.sample_metrics", return_value=metrics), \
                patch.object(DeviceService, "_call", side_effect=lambda request: request):
            request = DeviceService().update_host_lcd("mouse", 3, {"baseline": True})
        self.assertEqual(request, {
            "operation": "host_lcd", "device_id": "mouse", "profile_slot": 3,
            "baseline": {"baseline": True}, "cpu_percent": 12.9,
            "ram_percent": 34.8, "gpu_percent": 56.7,
            "cpu_temperature_c": 67.6, "gpu_temperature_c": 78.5,
        })

    def test_live_lcd_service_forwards_explicit_gpu_source_only_to_sampler(self):
        metrics = HostMetrics(12, 34, 56, 67, 78)
        with patch("swarm2.host_metrics.sample_metrics", return_value=metrics) as sample, \
                patch.object(DeviceService, "_call", side_effect=lambda request: request):
            request = DeviceService().update_host_lcd(
                "mouse", 2, {"baseline": True}, gpu_source="pci:0000:08:00.0")
        sample.assert_called_once_with(gpu_source="pci:0000:08:00.0")
        self.assertNotIn("gpu_source", request)
        self.assertEqual(request["gpu_percent"], 56)

    def test_gpu_source_inventory_is_serializable_and_does_not_call_usb(self):
        sources = (GpuSource("pci:0000:03:00.0", "PCI 0000:03:00.0",
                             True, True, False),)
        with patch("swarm2.host_metrics.available_gpu_sources",
                   return_value=sources), \
                patch.object(DeviceService, "_call") as call:
            result = DeviceService.gpu_sources()
        call.assert_not_called()
        self.assertEqual(result, [{
            "id": "pci:0000:03:00.0", "label": "PCI 0000:03:00.0",
            "primary": True, "load_available": True,
            "temperature_available": False,
        }])

    def test_media_player_inventory_is_serializable_and_does_not_call_usb(self):
        players = (MediaPlayerInfo(
            "org.mpris.MediaPlayer2.vlc", "VLC", "mpris"),)
        with patch("swarm2.host_media.available_media_players",
                   return_value=players), \
                patch.object(DeviceService, "_call") as call:
            result = DeviceService.media_players()
        call.assert_not_called()
        self.assertEqual(result, [{
            "id": "org.mpris.MediaPlayer2.vlc",
            "label": "VLC", "backend": "mpris",
        }])


if __name__ == "__main__":
    unittest.main()
