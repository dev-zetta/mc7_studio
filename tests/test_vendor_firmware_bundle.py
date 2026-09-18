import json
from pathlib import Path
import subprocess
import sys
import unittest

from swarm2.firmware_catalog import known_releases


REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_DIR = REPO_ROOT / "vendor/firmware/turtle-beach/command-series-mc7"


class VendorFirmwareBundleTests(unittest.TestCase):
    def test_offline_verifier(self):
        if not list(BUNDLE_DIR.glob("*.7z")):
            self.skipTest("private firmware backup is intentionally absent")
        result = subprocess.run(
            [sys.executable, str(BUNDLE_DIR / "verify.py")],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "Verified 7 MC7 firmware archives (7645620 bytes).\n",
        )

    def test_manifest_matches_application_catalog(self):
        manifest = json.loads(
            (BUNDLE_DIR / "manifest.json").read_text(encoding="utf-8")
        )
        records = {record["key"]: record for record in manifest["releases"]}
        catalog = {release.key: release for release in known_releases()}
        self.assertEqual(records.keys(), catalog.keys())

        for key, release in catalog.items():
            record = records[key]
            with self.subTest(key=key):
                self.assertEqual(record["role"], release.role)
                self.assertEqual(
                    record["usb_vid_pid"],
                    f"{release.vendor_id:04X}:{release.product_id:04X}",
                )
                self.assertEqual(record["package_version"], release.package_version)
                self.assertEqual(record["firmware_version"], release.firmware_version)
                self.assertEqual(record["auto_reset_version"], release.auto_reset_version)
                self.assertEqual(record["bytes"], release.size)
                self.assertEqual(record["md5"], release.md5)
                self.assertEqual(record["sha256"], release.sha256)
                self.assertEqual(record["resolver_url"], release.resolver_url)
                self.assertEqual(record["cdn_url"], release.cdn_url)
                self.assertEqual(record["filename"], release.cdn_url.rsplit("/", 1)[1])
                self.assertIs(record["product_inspection_passed"], True)


if __name__ == "__main__":
    unittest.main()
