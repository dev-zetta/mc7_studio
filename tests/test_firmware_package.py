"""Pinned archive identity and bounded parsing; no firmware or USB required."""

from dataclasses import replace
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from swarm2.firmware_catalog import FirmwareError, known_releases, release_by_key
from swarm2.firmware_package import (FirmwarePackageError, _copy_verified_archive,
    _list_entries, _run_7z, inspect_firmware_package)


def members(release):
    prefix = f"data/Firmware/COMMAND_MC7/10F5_{release.product_id:04X}"
    directory = f"{prefix}/{release.firmware_version}"
    name = release.archive_stem
    part = 1 if release.role == "mouse" else 0
    minor = release.firmware_version % 100
    packed_version = part | (7 << 4) | (5 << 12) | (minor << 27)
    offer = bytes.fromhex("00000f00") + packed_version.to_bytes(4, "little") + bytes.fromhex("0000000004000000")
    payload = (bytes.fromhex("0000000001a9") + bytes(51)
               + bytes.fromhex("010000000137") + bytes(51))
    files = {
        f"{prefix}/version.ini": (f"[General]\nversion={release.package_version}\n"
            f"fw_version={release.firmware_version}\n"
            f"fw_auto_reset_version={release.auto_reset_version}\n").encode(),
        f"{directory}/Info.ini": f"offer_file={name}.offer.bin\npayload_file={name}.payload.bin\n".encode(),
        f"{directory}/{name}.offer.bin": offer,
        f"{directory}/{name}.payload.bin": payload,
    }
    for alias in release.equivalent_archive_stems:
        files[f"{directory}/{alias}.offer.bin"] = offer
        files[f"{directory}/{alias}.payload.bin"] = payload
    return files


def listing(files):
    return b"\n".join(f"Path = {name}\nSize = {len(raw)}\nAttributes = A\nEncrypted = -\n".encode()
                      for name, raw in files.items())


class FirmwarePackageTests(unittest.TestCase):
    def setUp(self):
        self.release = known_releases()[0]
        self.files = members(self.release)
        self.arguments = []
        self.listing_override = None
        self.extract_overrides = {}
        self.copier = patch("swarm2.firmware_package._copy_verified_archive")
        self.copier.start()
        self.addCleanup(self.copier.stop)
        runner = patch("swarm2.firmware_package._run_7z", side_effect=self.run_7z)
        runner.start()
        self.addCleanup(runner.stop)
        executable = patch("swarm2.firmware_package._executable", return_value="fixture-7z")
        executable.start()
        self.addCleanup(executable.stop)

    def run_7z(self, executable, arguments, maximum):
        self.arguments.append((arguments, maximum))
        self.assertEqual(executable, "fixture-7z")
        if arguments[0] == "l":
            return self.listing_override if self.listing_override is not None else listing(self.files)
        self.assertEqual(arguments[:6], ["x", "-so", "-y", "-bd", "-bb0", "-spd"])
        self.assertEqual(arguments[6], "--")
        name = arguments[-1]
        self.assertEqual(maximum, len(self.files[name]))
        return self.extract_overrides.get(name, self.files[name])

    def inspect(self):
        return inspect_firmware_package("downloaded.7z", self.release)

    def by_suffix(self, suffix):
        return next(name for name in self.files if name.endswith(suffix))

    def test_exact_four_members_and_manifest_mapping_return_inert_bytes(self):
        package = self.inspect()
        self.assertIs(package.release, self.release)
        self.assertEqual((package.package_version, package.fw_version, package.auto_reset_version),
                         ("5.9.0.0", 509, 506))
        self.assertEqual(package.component_id, 15)
        self.assertEqual(package.offer, self.files[self.by_suffix(".offer.bin")])
        self.assertEqual(package.payload, self.files[self.by_suffix(".payload.bin")])
        self.assertTrue(package.requires_reset(504))
        self.assertTrue(package.requires_reset(505))
        self.assertFalse(package.requires_reset(506))
        self.assertFalse(package.requires_reset(509))
        self.assertEqual([args[0] for args, _ in self.arguments], ["l", "x", "x", "x", "x"])
        # Temporary archive paths are removed before the bytes are returned.
        self.assertFalse(Path(self.arguments[0][0][-1]).exists())
        for invalid in (0, -1, True, 5.04, "504", 10000):
            with self.subTest(invalid=invalid), self.assertRaises(FirmwarePackageError):
                package.requires_reset(invalid)

    def test_transmitter_keeps_its_separate_role_directory_and_reset_threshold(self):
        self.release = release_by_key("transmitter:5.4.0.0")
        self.files = members(self.release)
        package = self.inspect()
        self.assertEqual(package.release.role, "transmitter")
        self.assertEqual((package.fw_version, package.auto_reset_version), (504, 504))
        self.assertTrue(package.requires_reset(502))
        self.assertFalse(package.requires_reset(504))

    def test_historical_duplicate_aliases_are_accepted_only_when_identical(self):
        for key in ("mouse:5.4.0.0", "transmitter:5.2.0.0"):
            with self.subTest(key=key):
                self.release = release_by_key(key)
                self.files = members(self.release)
                self.arguments = []
                self.listing_override = None
                self.extract_overrides = {}
                package = self.inspect()
                directory = (f"data/Firmware/COMMAND_MC7/10F5_{self.release.product_id:04X}/"
                             f"{self.release.firmware_version}")
                alias = self.release.equivalent_archive_stems[0]
                self.assertEqual(package.offer, self.files[f"{directory}/{alias}.offer.bin"])
                self.assertEqual(package.payload, self.files[f"{directory}/{alias}.payload.bin"])
                self.assertEqual([args[0] for args, _ in self.arguments],
                                 ["l", "x", "x", "x", "x", "x", "x"])

    def test_historical_duplicate_alias_mismatch_is_rejected(self):
        for key in ("mouse:5.4.0.0", "transmitter:5.2.0.0"):
            for suffix in (".offer.bin", ".payload.bin"):
                with self.subTest(key=key, suffix=suffix):
                    self.release = release_by_key(key)
                    self.files = members(self.release)
                    self.arguments = []
                    self.listing_override = None
                    self.extract_overrides = {}
                    directory = (f"data/Firmware/COMMAND_MC7/10F5_{self.release.product_id:04X}/"
                                 f"{self.release.firmware_version}")
                    alias = self.release.equivalent_archive_stems[0]
                    path = f"{directory}/{alias}{suffix}"
                    changed = bytearray(self.files[path])
                    changed[-1] ^= 1
                    self.extract_overrides[path] = bytes(changed)
                    with self.assertRaisesRegex(FirmwarePackageError,
                                                "Equivalent firmware members do not match"):
                        self.inspect()

    def test_historical_duplicate_alias_short_read_is_rejected(self):
        self.release = release_by_key("mouse:5.4.0.0")
        self.files = members(self.release)
        directory = "data/Firmware/COMMAND_MC7/10F5_502C/504"
        alias = self.release.equivalent_archive_stems[0]
        path = f"{directory}/{alias}.payload.bin"
        self.extract_overrides[path] = self.files[path][:-1]
        with self.assertRaisesRegex(FirmwarePackageError, "member length"):
            self.inspect()

    def test_arbitrary_release_hashes_or_identity_are_rejected_before_subprocess(self):
        for release in (replace(self.release, sha256="0" * 64),
                        replace(self.release, role="transmitter"),
                        replace(self.release, package_version="6.0.0.0"), None):
            with self.subTest(release=release), self.assertRaises(FirmwareError):
                inspect_firmware_package("downloaded.7z", release)
        self.assertEqual(self.arguments, [])

    def test_unexpected_members_and_missing_member_are_rejected_before_extraction(self):
        original = dict(self.files)
        for mutation in (lambda: self.files.update({"installer.exe": b"unused"}),
                         lambda: self.files.pop(self.by_suffix(".payload.bin")),
                         lambda: self.files.update({"data/Firmware/COMMAND_MC7/10F5_502E/version.ini": b"wrong role"})):
            self.files = dict(original)
            mutation()
            with self.assertRaisesRegex(FirmwarePackageError, "unexpected files"):
                self.inspect()
            self.assertTrue(all(args[0] == "l" for args, _ in self.arguments))

    def test_oversize_and_invalid_entry_lengths_rejected_before_extraction(self):
        original = listing(self.files)
        version_size = len(self.files[self.by_suffix("version.ini")])
        for old, new in ((b"Size = 16", b"Size = 15"),
                         (f"Size = {version_size}".encode(), b"Size = 0"),
                         (f"Size = {version_size}".encode(), b"Size = 67108865")):
            with self.subTest(new=new):
                self.listing_override = original.replace(old, new)
                with self.assertRaises(FirmwarePackageError):
                    self.inspect()
                self.assertTrue(all(args[0] == "l" for args, _ in self.arguments))

    def test_manifest_versions_reset_threshold_and_ini_structure_fail_closed(self):
        name = self.by_suffix("version.ini")
        original = self.files[name]
        replacements = [original.replace(b"5.9.0.0", b"5.4.0.0"),
                        original.replace(b"fw_version=509", b"fw_version=504"),
                        original.replace(b"506", b"510"), original.replace(b"506", b"0506"),
                        original.replace(b"506", b"-1"), original + b"fw_version=509\n",
                        original + b"[Other]\nx=y\n", original + b"unknown=1\n",
                        original + b"\0", original + b"\xff"]
        for invalid in replacements:
            with self.subTest(invalid=invalid):
                self.files[name] = invalid
                with self.assertRaises(FirmwarePackageError):
                    self.inspect()

    def test_info_cannot_redirect_to_another_file_or_path(self):
        name = self.by_suffix("Info.ini")
        original = self.files[name]
        for invalid in (original.replace(b"offer_file=", b"offer_file=../"),
                        original.replace(b"App_UI", b"Dongle"),
                        original + b"payload_file=other.bin\n"):
            with self.subTest(invalid=invalid):
                self.files[name] = invalid
                with self.assertRaises(FirmwarePackageError):
                    self.inspect()

    def test_member_short_read_and_wrong_component_are_rejected(self):
        name = self.by_suffix(".offer.bin")
        self.extract_overrides[name] = bytes(15)
        with self.assertRaisesRegex(FirmwarePackageError, "member length"):
            self.inspect()
        self.extract_overrides.clear()
        self.files[name] = bytes(16)
        with self.assertRaisesRegex(FirmwarePackageError, "component"):
            self.inspect()

    def test_offer_role_version_token_and_bank_must_match_catalog(self):
        name = self.by_suffix(".offer.bin")
        original = self.files[name]
        mutations = []
        for offset, value in ((3, 1), (10, 1)):
            changed = bytearray(original)
            changed[offset] = value
            mutations.append(bytes(changed))
        for packed in (0x48005070, 0x40005071):
            changed = bytearray(original)
            changed[4:8] = packed.to_bytes(4, "little")
            mutations.append(bytes(changed))
        for changed in mutations:
            with self.subTest(offer=changed.hex()):
                self.files[name] = changed
                with self.assertRaisesRegex(FirmwarePackageError, "offer identity"):
                    self.inspect()

    def test_payload_structure_is_validated_before_returning_a_package(self):
        name = self.by_suffix(".payload.bin")
        original = self.files[name]
        for payload in (original[:57], original[:-1],
                        original[:57] + bytes.fromhex("02000000") + original[61:]):
            with self.subTest(payload=payload.hex()):
                self.files[name] = payload
                with self.assertRaises(FirmwarePackageError):
                    self.inspect()

    def test_links_duplicate_paths_and_encrypted_names_are_rejected(self):
        original = listing(self.files)
        malformed = [original + original,
                     original.replace(b"Encrypted = -", b"Encrypted = +", 1),
                     original.replace(b"Attributes = A", b"Attributes = A_ lrwxrwxrwx", 1),
                     original.replace(b"Attributes = A", b"Attributes = A\nSymbolic Link = target", 1),
                     original.replace(b"Path = data/", b"Path = ../data/", 1),
                     original.replace(b"Path = data/", b"Path = /data/", 1),
                     original.replace(b"Path = data/", b"Path = C:/data/", 1),
                     original.replace(b"Path = data/", b"Path = data\\", 1),
                     original.replace(b"Size = 16", b"Size = 16\nSize = 16")]
        for raw in malformed:
            with self.subTest(raw=raw[:100]), self.assertRaises(FirmwarePackageError):
                _list_entries(raw)

    def test_directory_records_are_allowed_only_for_expected_ancestors(self):
        self.listing_override = (b"Path = data\nSize = 0\nAttributes = D\nEncrypted = -\n\n"
                                 + listing(self.files))
        self.inspect()
        self.listing_override = self.listing_override.replace(b"Path = data\n", b"Path = other\n", 1)
        with self.assertRaisesRegex(FirmwarePackageError, "unexpected files"):
            self.inspect()


class FirmwareArchiveBoundsTests(unittest.TestCase):
    def test_archive_is_copied_and_both_hashes_checked_before_extraction(self):
        original = b"synthetic archive used only to exercise identity checks"
        release = replace(known_releases()[0], size=len(original),
                          md5=hashlib.md5(original).hexdigest(), sha256=hashlib.sha256(original).hexdigest())
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "source.7z", Path(directory) / "verified.7z"
            source.write_bytes(original)
            _copy_verified_archive(source, target, release)
            self.assertEqual(target.read_bytes(), original)
            target.unlink()
            for bad in (replace(release, md5="0" * 32), replace(release, sha256="0" * 64),
                        replace(release, size=len(original) - 1)):
                with self.subTest(bad=bad), self.assertRaises(FirmwarePackageError):
                    _copy_verified_archive(source, target, bad)
                target.unlink(missing_ok=True)

    def test_archive_symlink_and_directory_inputs_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.7z"
            source.write_bytes(b"x")
            link = root / "link.7z"
            link.symlink_to(source)
            release = replace(known_releases()[0], size=1)
            for unsafe in (link, root):
                with self.subTest(path=unsafe), self.assertRaises(FirmwarePackageError):
                    _copy_verified_archive(unsafe, root / "target", release)

    def test_subprocess_output_limit_and_nonzero_exit_are_explicit_errors(self):
        self.assertEqual(_run_7z(sys.executable, ["-c", "print('test', end='')"], 4), b"test")
        for program in ("print('too much')", "import sys; sys.exit(1)",
                        "import sys; sys.stderr.write('x'*65537)"):
            with self.subTest(program=program), self.assertRaises(FirmwarePackageError):
                _run_7z(sys.executable, ["-c", program], 4)

    def test_subprocess_timeout_kills_and_reaps_the_child(self):
        with patch("swarm2.firmware_package.EXTRACT_TIMEOUT_SECONDS", 0.1):
            with self.assertRaisesRegex(FirmwarePackageError, "timed out"):
                _run_7z(sys.executable, ["-c", "import time; time.sleep(20)"], 4)


if __name__ == "__main__":
    unittest.main()
