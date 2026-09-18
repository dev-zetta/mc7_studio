import unittest
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from swarm2.host_metrics import (
    GpuMetrics, GpuSourcePreferenceError, GpuSourcePreferenceStore,
    MetricsUnavailable, _GpuRecord, _linux_gpu_metrics, _nvidia_gpu_records,
    _sample_gpu, available_gpu_sources, sample_metrics,
)


class HostMetricsTests(unittest.TestCase):
    def test_first_sample_uses_positive_interval_and_available_based_percent(self):
        provider = Mock()
        provider.cpu_percent.return_value = 12.5
        provider.virtual_memory.return_value = SimpleNamespace(percent=37.2, used=999999, total=1000000)
        result = sample_metrics(provider=provider)
        self.assertEqual((result.cpu_percent, result.ram_percent), (12.5, 37.2))
        self.assertEqual((result.gpu_percent, result.cpu_temperature_c,
                          result.gpu_temperature_c), (None, None, None))
        provider.cpu_percent.assert_called_once_with(interval=0.1)
        provider.virtual_memory.assert_called_once_with()

    def test_missing_or_invalid_metrics_never_produce_zero_success(self):
        for field in ('cpu', 'ram'):
            for value in (None, True, '20', -1, 101, float('nan'), float('inf')):
                provider = Mock()
                provider.cpu_percent.return_value = value if field == 'cpu' else 20
                provider.virtual_memory.return_value = SimpleNamespace(percent=value if field == 'ram' else 30)
                with self.subTest(field=field, value=value), self.assertRaises(MetricsUnavailable):
                    sample_metrics(provider=provider)

    def test_os_failure_is_explicit(self):
        for error in (OSError("Unavailable"), AttributeError("missing"),
                      TypeError("bad"), ValueError("bad")):
            provider = Mock()
            provider.cpu_percent.side_effect = error
            with self.subTest(error=error), self.assertRaisesRegex(
                    MetricsUnavailable, "unavailable"):
                sample_metrics(provider=provider)

    def test_cpu_temperature_and_injected_gpu_are_optional(self):
        provider = Mock()
        provider.cpu_percent.return_value = 12.5
        provider.virtual_memory.return_value = SimpleNamespace(percent=37.2)
        provider.sensors_temperatures.return_value = {
            "nvme": [SimpleNamespace(current=91)],
            "k10temp": [SimpleNamespace(current=54.9), SimpleNamespace(current=67.25)],
            "amdgpu": [SimpleNamespace(current=88)],
        }
        gpu_provider = Mock(return_value={"percent": 43.5, "temperature_c": 61.75})
        result = sample_metrics(provider=provider, gpu_provider=gpu_provider)
        self.assertEqual(result.cpu_temperature_c, 67.25)
        self.assertEqual((result.gpu_percent, result.gpu_temperature_c), (43.5, 61.75))
        gpu_provider.assert_called_once_with()

    def test_bad_optional_sources_remain_unavailable_instead_of_zero(self):
        provider = Mock()
        provider.cpu_percent.return_value = 20
        provider.virtual_memory.return_value = SimpleNamespace(percent=30)
        provider.sensors_temperatures.return_value = {
            "k10temp": [SimpleNamespace(current=float("nan")), object()],
        }
        result = sample_metrics(provider=provider,
                                gpu_provider=lambda: (-1, 55))
        self.assertIsNone(result.cpu_temperature_c)
        self.assertIsNone(result.gpu_percent)
        self.assertEqual(result.gpu_temperature_c, 55)

    @staticmethod
    def _gpu_card(root: Path, index: int, *, load: int, temperature: int,
                  boot: bool | None = None):
        device = root / f"card{index}" / "device"
        hwmon = device / "hwmon" / f"hwmon{index}"
        hwmon.mkdir(parents=True)
        (device / "gpu_busy_percent").write_text(str(load), encoding="ascii")
        (hwmon / "temp1_input").write_text(str(temperature * 1000), encoding="ascii")
        if boot is not None:
            (device / "boot_vga").write_text("1" if boot else "0", encoding="ascii")

    def test_linux_gpu_keeps_load_and_temperature_on_unique_primary_device(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._gpu_card(root, 0, load=99, temperature=91, boot=False)
            self._gpu_card(root, 1, load=17, temperature=63, boot=True)
            result = _linux_gpu_metrics(root)
            self.assertEqual((result.percent, result.temperature_c), (17, 63))
            self.assertEqual(result.source, "sysfs:" + str((root / "card1" / "device").resolve()))

    def test_linux_gpu_does_not_combine_ambiguous_adapters(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._gpu_card(root, 0, load=99, temperature=91)
            self._gpu_card(root, 1, load=17, temperature=63)
            self.assertEqual(_linux_gpu_metrics(root), GpuMetrics())

    def test_unreadable_primary_does_not_promote_readable_secondary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._gpu_card(root, 0, load=99, temperature=91, boot=False)
            self._gpu_card(root, 1, load=17, temperature=63, boot=True)
            (root / "card1" / "device" / "gpu_busy_percent").unlink()
            for path in (root / "card1" / "device" / "hwmon").glob("*/temp1_input"):
                path.unlink()
            result = _linux_gpu_metrics(root)
            self.assertEqual((result.percent, result.temperature_c), (None, None))
            self.assertIn("card1", result.source)

    def test_drm_and_nvidia_fields_merge_only_for_the_same_pci_identity(self):
        drm = GpuMetrics(None, 61, "pci:0000:03:00.0")
        same = GpuMetrics(47, 62, "pci:0000:03:00.0")
        other = GpuMetrics(93, 72, "pci:0000:08:00.0")
        records = {drm.source: _GpuRecord(drm, True)}
        with patch("swarm2.host_metrics._linux_gpu_records", return_value=records), \
                patch("swarm2.host_metrics._nvidia_gpu_records",
                      return_value={same.source: same}):
            self.assertEqual(_sample_gpu(), GpuMetrics(47, 61, drm.source))
        with patch("swarm2.host_metrics._linux_gpu_records", return_value=records), \
                patch("swarm2.host_metrics._nvidia_gpu_records",
                      return_value={other.source: other}):
            self.assertEqual(_sample_gpu(), drm)

    def test_automatic_never_promotes_one_vendor_gpu_over_ambiguous_drm_inventory(self):
        first = GpuMetrics(17, 61, "pci:0000:03:00.0")
        second = GpuMetrics(93, 72, "pci:0000:08:00.0")
        records = {
            first.source: _GpuRecord(first, False),
            second.source: _GpuRecord(second, False),
        }
        with patch("swarm2.host_metrics._linux_gpu_records", return_value=records), \
                patch("swarm2.host_metrics._nvidia_gpu_records",
                      return_value={first.source: first}):
            self.assertEqual(_sample_gpu(), GpuMetrics())
        # A vendor-only adapter also participates in the complete inventory;
        # one DRM record does not make it the only detected GPU.
        with patch("swarm2.host_metrics._linux_gpu_records",
                   return_value={first.source: _GpuRecord(first, False)}), \
                patch("swarm2.host_metrics._nvidia_gpu_records",
                      return_value={second.source: second}):
            self.assertEqual(_sample_gpu(), GpuMetrics())

    def test_nvidia_query_is_bounded_and_keeps_partial_rows_by_pci_identity(self):
        completed = SimpleNamespace(
            returncode=0,
            stdout="00000000:03:00.0, 17, 61\n00000000:08:00.0, N/A, 72\nbad\n",
        )
        with patch("swarm2.host_metrics.shutil.which", return_value="/usr/bin/nvidia-smi"), \
                patch("swarm2.host_metrics.subprocess.run", return_value=completed) as run:
            records = _nvidia_gpu_records()
        self.assertEqual(records, {
            "pci:0000:03:00.0": GpuMetrics(17, 61, "pci:0000:03:00.0"),
            "pci:0000:08:00.0": GpuMetrics(None, 72, "pci:0000:08:00.0"),
        })
        self.assertEqual(run.call_args.kwargs["timeout"], 1)

    def test_gpu_inventory_lists_every_identity_and_merges_same_adapter(self):
        linux = {
            "pci:0000:03:00.0": _GpuRecord(
                GpuMetrics(None, 61, "pci:0000:03:00.0"), True),
            "pci:0000:08:00.0": _GpuRecord(
                GpuMetrics(23, None, "pci:0000:08:00.0"), False),
        }
        nvidia = {
            "pci:0000:03:00.0": GpuMetrics(47, 62, "pci:0000:03:00.0"),
            "pci:0000:0a:00.0": GpuMetrics(9, 50, "pci:0000:0a:00.0"),
        }
        with patch("swarm2.host_metrics._linux_gpu_records", return_value=linux), \
                patch("swarm2.host_metrics._nvidia_gpu_records", return_value=nvidia):
            sources = available_gpu_sources()
        self.assertEqual([source.identity for source in sources], [
            "pci:0000:03:00.0", "pci:0000:08:00.0", "pci:0000:0a:00.0"])
        self.assertTrue(sources[0].primary)
        self.assertTrue(sources[0].load_available)
        self.assertTrue(sources[0].temperature_available)
        self.assertEqual(sources[0].label, "PCI 0000:03:00.0")
        self.assertFalse(sources[1].temperature_available)

    def test_explicit_gpu_source_never_falls_back_to_another_adapter(self):
        first = GpuMetrics(17, 61, "pci:0000:03:00.0")
        second = GpuMetrics(93, 72, "pci:0000:08:00.0")
        records = {
            first.source: _GpuRecord(first, True),
            second.source: _GpuRecord(second, False),
        }
        with patch("swarm2.host_metrics._linux_gpu_records", return_value=records), \
                patch("swarm2.host_metrics._nvidia_gpu_records", return_value={}):
            self.assertEqual(_sample_gpu(source=second.source), second)
            stale = _sample_gpu(source="pci:0000:0a:00.0")
        self.assertEqual(stale, GpuMetrics(source="pci:0000:0a:00.0"))

    def test_injected_gpu_provider_must_match_an_explicit_source(self):
        provider = lambda: GpuMetrics(12, 55, "pci:0000:03:00.0")
        self.assertEqual(
            _sample_gpu(provider, source="pci:0000:03:00.0"),
            GpuMetrics(12, 55, "pci:0000:03:00.0"))
        self.assertEqual(
            _sample_gpu(provider, source="pci:0000:08:00.0"),
            GpuMetrics(source="pci:0000:08:00.0"))

    def test_injected_system_provider_never_reads_real_gpu_for_explicit_source(self):
        provider = Mock()
        provider.cpu_percent.return_value = 20
        provider.virtual_memory.return_value = SimpleNamespace(percent=30)
        source = "pci:0000:08:00.0"
        with patch("swarm2.host_metrics._sample_gpu") as sample:
            result = sample_metrics(provider=provider, gpu_source=source)
        sample.assert_not_called()
        self.assertIsNone(result.gpu_percent)
        self.assertIsNone(result.gpu_temperature_c)

    def test_gpu_source_preference_round_trip_and_automatic_value(self):
        with tempfile.TemporaryDirectory() as directory:
            store = GpuSourcePreferenceStore(Path(directory) / "gpu.json")
            self.assertIsNone(store.load())
            store.save("PCI:0000:03:00.0")
            self.assertEqual(store.load(), "pci:0000:03:00.0")
            store.save(None)
            self.assertIsNone(store.load())

    def test_gpu_source_preference_rejects_malformed_or_unsafe_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gpu.json"
            store = GpuSourcePreferenceStore(path)
            invalid = (
                b"not json",
                b'{"schema":"swarm2.mc7.host-metrics","version":1,"gpu_source":"card0"}',
                b'{"schema":"swarm2.mc7.host-metrics","schema":"duplicate","version":1,"gpu_source":null}',
            )
            for raw in invalid:
                path.write_bytes(raw)
                with self.subTest(raw=raw), self.assertRaises(GpuSourcePreferenceError):
                    store.load()

    def test_gpu_source_preference_never_saves_more_than_it_can_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gpu.json"
            store = GpuSourcePreferenceStore(path)
            source = "sysfs:/" + "a" * 4080
            with self.assertRaisesRegex(GpuSourcePreferenceError, "too large"):
                store.save(source)
            self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
