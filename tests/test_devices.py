import contextlib
import io
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from swarm2.cli import main
from swarm2.devices import BackendUnavailable, Device, Enumeration, enumerate_devices, enumerate_hidapi, enumerate_sysfs, is_mc7


class DiscoveryTests(unittest.TestCase):
    def test_ids_are_exact_and_names_do_not_control_selection(self):
        self.assertTrue(is_mc7(0x10F5, 0x502C))
        self.assertTrue(is_mc7(0x10F5, 0x502E))
        self.assertFalse(is_mc7(0x10F5, 0x502D))
        self.assertFalse(is_mc7(0x1E7D, 0x502C))

    def test_sysfs_discovery_without_device_nodes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sysfs = root / "sysfs"
            for index, (vendor, product) in enumerate(((0x10F5, 0x502C), (0x10F5, 0x502E), (0x9999, 0x502C))):
                device = sysfs / f"hidraw{index}" / "device"
                device.mkdir(parents=True)
                (device / "uevent").write_text(
                    f"HID_ID=0003:{vendor:08X}:{product:08X}\nHID_NAME=Example\nHID_PHYS=usb/example/input2\n"
                )
                (device / "bInterfaceNumber").write_text("02\n")
                (device / "report_descriptor").write_bytes(bytes.fromhex("750895018102"))
            result = enumerate_sysfs(sysfs, root / "missing-dev")
            self.assertEqual(result.issues, [])
            self.assertEqual(len(result.devices), 2)
            self.assertTrue(all(device.interface_number == 2 for device in result.devices))
            self.assertTrue(all(device.device_node_exists is False for device in result.devices))
            self.assertTrue(all(device.descriptor["reports"][0]["report_bytes"] == 1 for device in result.devices))

    def test_unreadable_descriptor_does_not_hide_supported_mouse(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            device = root / "hidraw0" / "device"
            device.mkdir(parents=True)
            (device / "uevent").write_text("HID_ID=0003:000010F5:0000502C\n")
            result = enumerate_sysfs(root)
            self.assertEqual(len(result.devices), 1)
            self.assertIsNone(result.devices[0].descriptor)
            self.assertIn("Cannot summarize", result.devices[0].issues[0])

    def test_hidapi_enumerates_both_variants_without_opening_handles(self):
        calls = []

        def enumerate_fake(vendor, product):
            calls.append((vendor, product))
            return [{"path": f"test-{product:04x}".encode(), "vendor_id": vendor,
                     "product_id": product, "interface_number": 2, "usage_page": 0xFF0B, "usage": 0x104},
                    {"path": b"unrelated", "vendor_id": 123, "product_id": 456}]

        def forbidden_open():
            self.fail("Discovery must not open a HID handle")

        with patch("swarm2.devices.importlib.import_module", return_value=types.SimpleNamespace(enumerate=enumerate_fake, device=forbidden_open)):
            result = enumerate_hidapi()
        self.assertEqual(calls, [(0x10F5, 0x502C), (0x10F5, 0x502E)])
        self.assertEqual(len(result.devices), 2)
        self.assertEqual(result.devices[0].usage_page, 0xFF0B)

    def test_macos_selects_optional_hidapi(self):
        with patch("swarm2.devices.platform.system", return_value="Darwin"), patch("swarm2.devices.enumerate_hidapi") as backend:
            self.assertIs(enumerate_devices(), backend.return_value)
            backend.assert_called_once_with()

    def test_missing_optional_backend_error_is_actionable(self):
        with patch("swarm2.devices.importlib.import_module", side_effect=ImportError("hid")):
            with self.assertRaisesRegex(BackendUnavailable, "install the USB extra"):
                enumerate_hidapi()

    def test_doctor_remains_available_without_macos_optional_dependency(self):
        output = io.StringIO()
        with patch("swarm2.devices.platform.system", return_value="Darwin"), patch(
            "swarm2.devices.importlib.import_module", side_effect=ImportError("hid")
        ), contextlib.redirect_stdout(output):
            status = main(["doctor", "--json"])
        self.assertEqual(status, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["platform"], "Darwin")
        self.assertFalse(report["hidapi_available"])
        self.assertFalse(report["configuration_supported"])
        self.assertIn("USB extra", report["issues"][0])

    def test_doctor_counts_shared_path_collections_once(self):
        devices = [Device("/dev/hidraw9", 0x10F5, 0x502C, "MC7", "hidapi", usage_page=page)
                   for page in (0xFF00, 0xFF01, 0xFF02, 0xFF0B)]
        output = io.StringIO()
        with patch("swarm2.cli.enumerate_devices", return_value=Enumeration("hidapi", devices)), patch(
            "swarm2.cli.load_hidapi"
        ), contextlib.redirect_stdout(output):
            status = main(["doctor", "--backend", "hidapi", "--json"])
        self.assertEqual(status, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["recognized_interfaces"], 1)
        self.assertEqual(report["enumeration_records"], 4)

    def test_descriptor_cli_json_contains_lengths(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = main(["descriptor", "--hex", "750895018102", "--json"])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())["reports"][0]["report_bytes"], 1)

    def test_bad_descriptor_cli_returns_structured_error(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = main(["descriptor", "--hex", "27ff", "--json"])
        self.assertEqual(status, 2)
        self.assertIn("truncated", json.loads(output.getvalue())["error"])


if __name__ == "__main__":
    unittest.main()
