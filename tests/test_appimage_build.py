"""Focused tests for AppImage release compatibility gates."""

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_appimage.py"
SPEC = importlib.util.spec_from_file_location("swarm2_build_appimage", SCRIPT)
build_appimage = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(build_appimage)


class AppImageCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.appdir = Path(self.directory.name)
        (self.appdir / "program").write_bytes(b"\x7fELFfixture")

    def tearDown(self):
        self.directory.cleanup()

    def test_release_baseline_accepts_matching_glibc_requirement(self):
        completed = SimpleNamespace(
            returncode=0,
            stdout="Version: 1  File: libc.so.6  Name: GLIBC_2.35\n",
            stderr="",
        )
        with patch.object(build_appimage.shutil, "which", return_value="/usr/bin/readelf"), \
             patch.object(build_appimage.subprocess, "run", return_value=completed):
            build_appimage._verify_glibc_baseline(self.appdir, "2.35")

    def test_release_baseline_rejects_newer_glibc_requirement(self):
        completed = SimpleNamespace(
            returncode=0,
            stdout="Version: 1  File: libc.so.6  Name: GLIBC_2.36\n",
            stderr="",
        )
        with patch.object(build_appimage.shutil, "which", return_value="/usr/bin/readelf"), \
             patch.object(build_appimage.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(
                build_appimage.AppImageError, "needs GLIBC_2.36"
            ):
                build_appimage._verify_glibc_baseline(self.appdir, "2.35")

    def test_invalid_release_baseline_is_rejected(self):
        with self.assertRaisesRegex(
            build_appimage.AppImageError, "must look like 2.35"
        ):
            build_appimage._parse_glibc_version("latest")


class AppImageProvenanceTests(unittest.TestCase):
    def setUp(self):
        local_tmp = Path.cwd() / "tmp"
        local_tmp.mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=local_tmp)
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def _fixture_debian_archive(self):
        dpkg_deb = shutil.which("dpkg-deb")
        if dpkg_deb is None:
            self.skipTest("dpkg-deb is required for Debian archive provenance")
        package_root = self.root / "package"
        control = package_root / "DEBIAN" / "control"
        control.parent.mkdir(parents=True)
        control.write_text(
            "Package: libfixture\n"
            "Version: 1.2-3\n"
            "Architecture: amd64\n"
            "Maintainer: MC7 Studio tests <tests@example.invalid>\n"
            "Description: AppImage provenance fixture\n"
        )
        library_root = package_root / "usr/lib/x86_64-linux-gnu"
        library_root.mkdir(parents=True)
        (library_root / "libfixture.so.1.2").write_bytes(b"fixture library bytes")
        (library_root / "libfixture.so.1").symlink_to("libfixture.so.1.2")
        copyright_file = package_root / "usr/share/doc/libfixture/copyright"
        copyright_file.parent.mkdir(parents=True)
        copyright_file.write_bytes(b"exact fixture copyright\n")
        archive = self.root / "libfixture_1.2-3_amd64.deb"
        subprocess.run(
            [dpkg_deb, "--build", str(package_root), str(archive)],
            check=True,
            capture_output=True,
        )
        return archive, dpkg_deb

    def test_native_toc_rejects_parent_traversal(self):
        source = self.root / "library.so"
        source.write_bytes(b"native")
        toc = self.root / "COLLECT-00.toc"
        toc.write_text(repr(([("../library.so", str(source), "BINARY")],)))
        with self.assertRaisesRegex(build_appimage.AppImageError, "Unsafe or duplicate"):
            build_appimage._read_native_toc(toc)

    def test_hidapi_wheel_library_must_have_component_mapping(self):
        self.assertEqual(
            build_appimage._hidapi_wheel_component(
                "hidapi.libs/libusb-example.so.0"
            ),
            ("libusb", "LGPL-2.1-or-later"),
        )
        with self.assertRaisesRegex(build_appimage.AppImageError, "Unattributed library"):
            build_appimage._hidapi_wheel_component(
                "hidapi.libs/libunexpected-example.so.0"
            )

    def test_qt_virtual_keyboard_is_rejected(self):
        with self.assertRaisesRegex(build_appimage.AppImageError, "Forbidden unused Qt"):
            build_appimage._python_provenance(
                self.root / "libQt6VirtualKeyboard.so.6",
                (
                    "PySide6_Addons",
                    "6.11.2",
                    "PySide6/Qt/lib/libQt6VirtualKeyboard.so.6",
                ),
                {},
            )

    def test_qt_license_mapping_includes_each_declared_choice(self):
        for package, relative in (
            ("PySide6_Essentials", "PySide6/Qt/lib/libQt6Core.so.6"),
            ("shiboken6", "shiboken6/Shiboken.abi3.so"),
        ):
            provenance = build_appimage._python_provenance(
                self.root / Path(relative).name,
                (package, "6.11.2", relative),
                {},
            )
            self.assertEqual(
                provenance["license_expression"],
                "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only",
            )
            self.assertTrue(
                {"LGPL-3.0.txt", "GPL-2.0.txt", "GPL-3.0.txt"}
                <= set(provenance["license_files"])
            )

    def test_debian_provenance_copies_package_copyright(self):
        source = self.root / "library.so"
        source.write_bytes(b"native")
        docs = self.root / "docs"
        notice = docs / "libfixture" / "copyright"
        notice.parent.mkdir(parents=True)
        notice.write_text("fixture license\n")
        def run(argv, **_kwargs):
            if argv[1] == "-S":
                return SimpleNamespace(returncode=0, stdout=f"libfixture:amd64: {source}\n")
            return SimpleNamespace(returncode=0, stdout="libfixture:amd64\t1.2-3\n")

        licenses = self.root / "licenses"
        with patch.object(build_appimage.subprocess, "run", side_effect=run):
            result = build_appimage._debian_package_provenance(
                source,
                licenses,
                documentation_root=docs,
                dpkg_query="/usr/bin/dpkg-query",
            )
        self.assertEqual(result["name"], "libfixture:amd64")
        self.assertEqual(result["version"], "1.2-3")
        self.assertEqual(
            (licenses / "debian/libfixture-amd64.copyright").read_text(),
            "fixture license\n",
        )

    def test_debian_archive_fallback_matches_name_bytes_and_exact_notice(self):
        archive_path, dpkg_deb = self._fixture_debian_archive()
        archive = build_appimage._inspect_debian_package_archive(
            archive_path, dpkg_deb=dpkg_deb,
        )
        extracted_root = self.root / "extracted/usr/lib/x86_64-linux-gnu"
        extracted_root.mkdir(parents=True)
        (extracted_root / "libfixture.so.1.2").write_bytes(b"fixture library bytes")
        source = extracted_root / "libfixture.so.1"
        source.symlink_to("libfixture.so.1.2")
        licenses = self.root / "licenses"

        provenance = build_appimage._debian_archive_provenance(
            source, licenses, [archive],
        )

        self.assertEqual(provenance["name"], "libfixture:amd64")
        self.assertEqual(provenance["version"], "1.2-3")
        self.assertEqual(
            provenance["package_payload_path"],
            "usr/lib/x86_64-linux-gnu/libfixture.so.1",
        )
        self.assertEqual(
            provenance["package_archive_sha256"], build_appimage._sha256(archive_path)
        )
        notice = licenses / provenance["license_files"][0]
        self.assertEqual(notice.read_bytes(), b"exact fixture copyright\n")

    def test_debian_archive_fallback_rejects_same_name_with_different_bytes(self):
        archive_path, dpkg_deb = self._fixture_debian_archive()
        archive = build_appimage._inspect_debian_package_archive(
            archive_path, dpkg_deb=dpkg_deb,
        )
        source = self.root / "libfixture.so.1"
        source.write_bytes(b"different library bytes")
        with self.assertRaisesRegex(
            build_appimage.DebianPackageNotOwnedError, "supplied package archive"
        ):
            build_appimage._debian_archive_provenance(
                source, self.root / "licenses", [archive],
            )

    def test_installed_debian_package_mapping_takes_priority_over_archive(self):
        installed = {
            "kind": "debian-package",
            "name": "libfixture:amd64",
            "version": "1.2-3",
            "license_files": ["debian/libfixture-amd64.copyright"],
        }
        with patch.object(
            build_appimage, "_debian_package_provenance", return_value=installed,
        ) as installed_lookup, patch.object(
            build_appimage, "_debian_archive_provenance"
        ) as archive_lookup:
            result = build_appimage._debian_system_provenance(
                self.root / "library.so", self.root / "licenses", [],
            )
        self.assertIs(result, installed)
        installed_lookup.assert_called_once()
        archive_lookup.assert_not_called()

    def test_manifest_records_native_digest_and_provenance(self):
        source = self.root / "library.so"
        source.write_bytes(b"source native")
        appdir = self.root / "AppDir"
        frozen = appdir / "usr/lib/mc7-studio"
        (frozen / "_internal").mkdir(parents=True)
        (frozen / "_internal/library.so").write_bytes(b"bundled native")
        toc = self.root / "COLLECT-00.toc"
        toc.write_text(repr(([('library.so', str(source), 'BINARY')],)))
        license_dir = appdir / "usr/share/licenses/mc7-studio"
        license_dir.mkdir(parents=True)
        package_licenses = {
            "Python": ["Python-COPYRIGHT.txt"],
            "PyInstaller": ["PyInstaller-COPYING.txt"],
            "certifi": ["certifi-LICENSE"],
            "hidapi": ["hidapi-LICENSE"],
            "psutil": ["psutil-LICENSE"],
            "websocket-client": ["websocket-client-LICENSE"],
        }
        for name in {
            *(item for values in package_licenses.values() for item in values),
            "7-Zip-License.txt",
            "GPL-2.0.txt",
            "debian/libfixture-amd64.copyright",
        }:
            notice = license_dir / name
            notice.parent.mkdir(parents=True, exist_ok=True)
            notice.write_text("license\n")
        package = {
            "kind": "debian-package",
            "name": "libfixture:amd64",
            "version": "1.2-3",
            "source_path": str(source),
            "license_files": ["debian/libfixture-amd64.copyright"],
        }
        with patch.object(build_appimage, "_distribution_file_index", return_value={}), \
             patch.object(build_appimage, "_debian_package_provenance", return_value=package), \
             patch.object(build_appimage.metadata, "version", return_value="fixture"), \
             patch.object(build_appimage.metadata, "distribution") as distribution:
            distribution.return_value.metadata = {}
            manifest_path = build_appimage._write_bundled_component_manifest(
                appdir, frozen, toc, package_licenses, {},
            )
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(len(manifest["native_files"]), 1)
        native = manifest["native_files"][0]
        self.assertEqual(native["path"], "usr/lib/mc7-studio/_internal/library.so")
        self.assertEqual(native["provenance"]["name"], "libfixture:amd64")
        self.assertEqual(
            native["sha256"], build_appimage._sha256(frozen / "_internal/library.so")
        )


if __name__ == "__main__":
    unittest.main()
