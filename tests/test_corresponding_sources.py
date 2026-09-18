"""Offline checks for the AppImage corresponding-source release artifact."""

import importlib.util
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
import tarfile
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/build_corresponding_sources.py"
SPEC = importlib.util.spec_from_file_location("swarm2_corresponding_sources", SCRIPT)
sources = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(sources)


def component_manifest(*, opaque=False):
    components = [
        {"kind": "appimage-runtime", "name": "AppImage type-2 runtime", "version": "20251108"},
        {"kind": "bundled-tool", "name": "7-Zip", "version": "26.03"},
        {"kind": "build-tool-runtime", "name": "PyInstaller", "version": "6.22.3"},
        {"kind": "python-distribution", "name": "hidapi", "version": "0.15.0"},
        {"kind": "python-wheel-component", "name": "PySide6", "version": "6.11.2"},
        {"kind": "python-wheel-component", "name": "Qt", "version": "6.11.2"},
        {"kind": "python-wheel-component", "name": "Shiboken6", "version": "6.11.2"},
        {"kind": "debian-package", "name": "libfixture:amd64", "version": "1.2-3"},
    ]
    if opaque:
        components.append({
            "kind": "python-wheel-vendored-library",
            "name": "libunknown",
            "version": "unrecorded",
        })
    return {
        "schema_version": 1,
        "application": "MC7-Studio",
        "application_version": "0.1.0",
        "components": components,
    }


class CorrespondingSourceTests(unittest.TestCase):
    def setUp(self):
        local_tmp = Path.cwd() / "tmp"
        local_tmp.mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=local_tmp)
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_component_inventory_rejects_opaque_wheel_native_library(self):
        path = self.root / "components.json"
        path.write_text(json.dumps(component_manifest(opaque=True)))
        with self.assertRaisesRegex(sources.SourceError, "Opaque wheel-vendored"):
            sources._component_inventory(path)

    def test_component_inventory_accepts_fixed_and_debian_components(self):
        path = self.root / "components.json"
        path.write_text(json.dumps(component_manifest()))
        version, components = sources._component_inventory(path)
        self.assertEqual(version, "0.1.0")
        self.assertEqual(components[-1]["name"], "libfixture:amd64")

    def test_debian_source_identity_uses_explicit_source_version(self):
        completed = SimpleNamespace(
            returncode=0,
            stdout=(
                "Package: libfixture\n"
                "Source: fixture-source (1:1.2-3)\n"
                "Version: 1.2-3+b1\n\n"
            ),
        )
        with patch.object(sources.subprocess, "run", return_value=completed):
            self.assertEqual(
                sources._debian_source_identity("libfixture:amd64", "1.2-3+b1"),
                ("fixture-source", "1:1.2-3"),
            )

    def test_dsc_inventory_checks_every_referenced_file(self):
        payload = self.root / "fixture_1.2.orig.tar.gz"
        payload.write_bytes(b"exact source")
        digest = sources.sha256(payload)
        control = self.root / "fixture_1.2-3.dsc"
        control.write_text(
            "Format: 3.0 (quilt)\n"
            "Source: fixture\n"
            "Version: 1.2-3\n"
            "Checksums-Sha256:\n"
            f" {digest} {payload.stat().st_size} {payload.name}\n"
        )
        inventory = sources._dsc_inventory(control, "fixture", "1.2-3")
        self.assertEqual(inventory[0]["sha256"], digest)
        payload.write_bytes(b"changed")
        with self.assertRaisesRegex(sources.SourceError, "failed its .dsc checksum"):
            sources._dsc_inventory(control, "fixture", "1.2-3")

    def test_debian_source_filenames_allow_version_tildes_but_no_paths(self):
        self.assertEqual(
            sources._safe_filename("gcc-12_12.3.0-1ubuntu1~22.04.3.tar.xz", "Debian source"),
            "gcc-12_12.3.0-1ubuntu1~22.04.3.tar.xz",
        )
        with self.assertRaisesRegex(sources.SourceError, "Invalid Debian source"):
            sources._safe_filename("../source.tar.xz", "Debian source")

    def test_archive_verifier_detects_changed_payload(self):
        staging = self.root / "MC7-Studio-0.1.0-corresponding-sources"
        staging.mkdir()
        payload = staging / "source.tar.xz"
        payload.write_bytes(b"source")
        manifest = {
            "schema": sources.SCHEMA,
            "payload_files": [{
                "path": payload.name,
                "bytes": payload.stat().st_size,
                "sha256": sources.sha256(payload),
            }],
        }
        (staging / "SOURCES.json").write_text(json.dumps(manifest))
        valid = self.root / "valid.tar.gz"
        sources._write_archive(staging, valid, 1_789_516_800)
        self.assertEqual(sources.verify_archive(valid)["schema"], sources.SCHEMA)

        invalid = self.root / "invalid.tar.gz"
        with tarfile.open(invalid, "w:gz") as archive:
            for path in (staging / "SOURCES.json", payload):
                data = path.read_bytes()
                if path == payload:
                    data = b"tampered"
                info = tarfile.TarInfo(f"{staging.name}/{path.name}")
                info.size = len(data)
                archive.addfile(info, BytesIO(data))
        with self.assertRaisesRegex(sources.SourceError, "differs from SOURCES.json"):
            sources.verify_archive(invalid)

    def test_pinned_source_configuration_is_complete(self):
        configured = sources._source_configuration(sources.SOURCE_CONFIGURATION)
        self.assertEqual(
            {item["filename"] for item in configured},
            sources.REQUIRED_SOURCE_ARCHIVES,
        )


if __name__ == "__main__":
    unittest.main()
