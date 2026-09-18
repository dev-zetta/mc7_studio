"""Bounded fake-network firmware downloads; never contact a mouse or the internet."""

from dataclasses import replace
import hashlib
import itertools
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from swarm2.firmware_catalog import (FirmwareError, current_release, known_releases,
    release_by_key, validate_release)
from swarm2.firmware_download import download_release


class Response(BytesIO):
    def __init__(self, data, url, *, status=200, headers=None, fail_after=None):
        super().__init__(data)
        self.url = url
        self.status = status
        self.headers = headers or {}
        self.fail_after = fail_after

    def geturl(self):
        return self.url

    def read1(self, size):
        if self.fail_after is not None and self.tell() >= self.fail_after:
            raise OSError("Connection interrupted")
        # Exercise partial reads even when the caller requests a whole chunk.
        return self.read(min(size, 17))


class Opener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request.full_url, timeout, request.header_items()))
        if not self.responses:
            raise AssertionError("Unexpected network request")
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FirmwareCatalogTests(unittest.TestCase):
    def test_release_identities_layouts_sizes_hashes_and_date(self):
        releases = known_releases()
        self.assertEqual([
            (r.key, r.role, r.vendor_id, r.product_id, r.device_id,
             r.package_version, r.firmware_version, r.auto_reset_version, r.size,
             r.archive_stem, r.equivalent_archive_stems, r.installation_supported)
            for r in releases
        ], [
            ("mouse:5.9.0.0", "mouse", 0x10F5, 0x502C, 454, "5.9.0.0", 509,
             506, 1864401, "RTL8772GWP_ImgPacketFile_App_UI", (), True),
            ("mouse:5.8.0.0", "mouse", 0x10F5, 0x502C, 454, "5.8.0.0", 508,
             506, 1847660, "RTL8772GWP_ImgPacketFile_App_UI", (), False),
            ("mouse:5.5.0.0", "mouse", 0x10F5, 0x502C, 454, "5.5.0.0", 505,
             505, 1837979, "RTL8772GWP_ImgPacketFile_APPUI", (), False),
            ("mouse:5.4.0.0", "mouse", 0x10F5, 0x502C, 454, "5.4.0.0", 504,
             504, 1829502, "RTL8772GWP_ImgPacketFile_App_0702",
             ("RTL8772GWP_ImgPacketFile_App_UI",), False),
            ("transmitter:5.4.0.0", "transmitter", 0x10F5, 0x502E, 454,
             "5.4.0.0", 504, 504, 88694, "RTL8772GWP_ImgPacketFile_Dongle", (), False),
            ("transmitter:5.3.0.0", "transmitter", 0x10F5, 0x502E, 454,
             "5.3.0.0", 503, 503, 88672, "RTL8772GWP_ImgPacketFile_Dongle", (), False),
            ("transmitter:5.2.0.0", "transmitter", 0x10F5, 0x502E, 454,
             "5.2.0.0", 502, 502, 88712, "RTL8772GWP_ImgPacketFile_Dongle_0702",
             ("RTL8772GWP_ImgPacketFile_Dongle",), False),
        ])
        self.assertEqual([
            (r.key, r.md5, r.sha256, r.resolver_url, r.cdn_url) for r in releases
        ], [
            ("mouse:5.9.0.0", "0e636a3886bfd1d97f89fdc2e5a04184",
             "0f89ba9a62fbcf0484adc939b7ff23d3ad6a224500f54e1205e53a37c2b1cf00",
             "https://acpr.prod.turtlebeach.com/swarm2/download/2895/1258/firmware.7z",
             "https://cdn.turtlebeach.com/device/driver-firmware/command-series-mc7/command-series-mc7_454-5.9.0.0-8260-v1.7z"),
            ("mouse:5.8.0.0", "a45d41a0a311feac93cc10054bf4eb83",
             "714ea09bc1b1860d8b8e3e12054e02c2f1e4a162b7396e5e41c905264a3984e3",
             "https://acpr.prod.turtlebeach.com/swarm2/download/1/1250/firmware.7z",
             "https://cdn.turtlebeach.com/device/driver-firmware/command-series-mc7/command-series-mc7_454-5.8.0.0-2572-v1.7z"),
            ("mouse:5.5.0.0", "e4d5997040407b380c45612ba9414414",
             "b1f58b3063bb1634f77e51a0b92086c544dd1efcbaa5e1109ee81e00e70f283c",
             "https://acpr.prod.turtlebeach.com/swarm2/download/1/1215/firmware.7z",
             "https://cdn.turtlebeach.com/device/driver-firmware/command-series-mc7/command-series-mc7_454-5.5.0.0-2645-v1.7z"),
            ("mouse:5.4.0.0", "9767a1a69f5d4c46936348ea93c0626c",
             "494a047a8414deef763e47f79ed0fb91a0359e91ecf690045d70be39d7749910",
             "https://acpr.prod.turtlebeach.com/swarm2/download/1/1198/firmware.7z",
             "https://cdn.turtlebeach.com/device/driver-firmware/command-series-mc7/command-series-mc7_454-5.4.0.0-0974-v1.7z"),
            ("transmitter:5.4.0.0", "5e7b4167b1b54dcb26bcdc39ecc0fe8d",
             "3434bb800455bed281cd989132c02868d35f4f2ba36f296b3288037c22a2557f",
             "https://acpr.prod.turtlebeach.com/swarm2/download/2896/1259/firmware.7z",
             "https://cdn.turtlebeach.com/device/driver-firmware/command-series-mc7/command-series-mc7_454-5.4.0.0-4561-v1.7z"),
            ("transmitter:5.3.0.0", "a778cb7b08f9330e9c79125a3b9572c0",
             "735859317c75f149beb1dfb17431b28bd5229e44a673eceaec8a46c3b71599ef",
             "https://acpr.prod.turtlebeach.com/swarm2/download/1/1216/firmware.7z",
             "https://cdn.turtlebeach.com/device/driver-firmware/command-series-mc7/command-series-mc7_454-5.3.0.0-7735-v1.7z"),
            ("transmitter:5.2.0.0", "cea2b55e1cfac409e084ead9b8b1a0da",
             "fc6f79d9d98ba5ae13568a44a6359fd42bc0ddebde59bfb011bcdf39a8351ea8",
             "https://acpr.prod.turtlebeach.com/swarm2/download/1/1199/firmware.7z",
             "https://cdn.turtlebeach.com/device/driver-firmware/command-series-mc7/command-series-mc7_454-5.2.0.0-2290-v1.7z"),
        ])
        for release in releases:
            self.assertIs(validate_release(release), release)
            self.assertIs(release_by_key(release.key), release)
            self.assertEqual(release.catalog_date, "2026-09-16")
            self.assertEqual(len(bytes.fromhex(release.md5)), 16)
            self.assertEqual(len(bytes.fromhex(release.sha256)), 32)
            self.assertIn(release.role, release.filename)
        self.assertIs(current_release("mouse"), releases[0])
        self.assertIs(current_release("transmitter"), releases[4])
        self.assertIsNone(release_by_key("mouse:9.9.0.0"))

    def test_caller_cannot_change_identity_url_digest_or_version(self):
        release = known_releases()[0]
        for changes in ({"role": "transmitter"}, {"product_id": 0x502E},
                        {"cdn_url": "https://example.com/update.7z"},
                        {"sha256": "0" * 64}, {"package_version": "9.9.0.0"}):
            with self.subTest(changes=changes), self.assertRaises(FirmwareError):
                validate_release(replace(release, **changes))
        with self.assertRaises(FirmwareError):
            validate_release({"role": "mouse"})


class FirmwareDownloadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "chosen-firmware.7z"
        self.data = b"7z\xbc\xaf\x27\x1c" + bytes(range(100))
        self.release = replace(known_releases()[0], size=len(self.data),
                               md5=hashlib.md5(self.data).hexdigest(),
                               sha256=hashlib.sha256(self.data).hexdigest())
        # Tiny synthetic package registered only inside this isolated test.
        self.catalog = patch("swarm2.firmware_catalog._KNOWN_RELEASES", (self.release,))
        self.catalog.start()
        self.addCleanup(self.catalog.stop)

    def responses(self, *, data=None, headers=None):
        return [Response((self.release.cdn_url + "\n").encode(), self.release.resolver_url),
                Response(self.data if data is None else data, self.release.cdn_url, headers=headers)]

    def download(self, responses=None):
        opener = Opener(self.responses() if responses is None else responses)
        with patch("swarm2.firmware_download.build_opener", return_value=opener):
            result = download_release(self.release, self.path)
        return result, opener

    def assert_no_partial_file(self):
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.glob(".mc7-firmware-*.part")), [])

    def test_full_archive_verified_and_installed_without_partial_files(self):
        result, opener = self.download()
        self.assertEqual(result.path, self.path)
        self.assertEqual(result.release, self.release)
        self.assertFalse(result.reused_existing)
        self.assertEqual(self.path.read_bytes(), self.data)
        self.assertEqual([call[0] for call in opener.calls],
                         [self.release.resolver_url, self.release.cdn_url])
        self.assertTrue(all(0 < call[1] <= 20 for call in opener.calls))
        self.assertEqual(list(self.path.parent.glob("*.part")), [])
        for _, _, headers in opener.calls:
            self.assertFalse(any("key" in key.lower() or "iv" in key.lower() for key, _ in headers))

    def test_identical_existing_archive_is_rechecked_without_network(self):
        self.path.write_bytes(self.data)
        result, opener = self.download([])
        self.assertTrue(result.reused_existing)
        self.assertEqual(opener.calls, [])

    def test_other_existing_file_and_symlink_are_preserved(self):
        self.path.write_bytes(b"a user file")
        with self.assertRaises(FirmwareError):
            self.download([])
        self.assertEqual(self.path.read_bytes(), b"a user file")
        self.path.unlink()
        target = self.path.parent / "target.7z"
        target.write_bytes(self.data)
        self.path.symlink_to(target)
        with self.assertRaises(FirmwareError):
            self.download([])
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(target.read_bytes(), self.data)

    def test_same_size_corrupt_existing_archive_is_not_reused(self):
        self.path.write_bytes(self.data[:-1] + b"x")
        with self.assertRaises(FirmwareError):
            self.download([])
        self.assertEqual(self.path.read_bytes(), self.data[:-1] + b"x")

    def test_corrupt_short_and_oversized_downloads_leave_no_archive(self):
        for data in (self.data[:-1] + b"x", self.data[:-1], self.data + b"x"):
            with self.subTest(size=len(data)), self.assertRaises(FirmwareError):
                self.download(self.responses(data=data))
            self.assert_no_partial_file()

    def test_content_length_and_encoding_must_match(self):
        for headers in ({"Content-Length": "999"}, {"Content-Length": "not a number"},
                        {"Content-Encoding": "gzip"}):
            with self.subTest(headers=headers), self.assertRaises(FirmwareError):
                self.download(self.responses(headers=headers))
            self.assert_no_partial_file()

    def test_public_resolver_must_return_exact_known_cdn_url(self):
        targets = ("http://cdn.turtlebeach.com/file.7z", "https://example.com/file.7z",
                   self.release.cdn_url + "?different=true", "x" * 8193, "\u2603")
        for target in targets:
            opener = Opener([Response(target.encode(), self.release.resolver_url)])
            with self.subTest(target=target[:80]), patch(
                    "swarm2.firmware_download.build_opener", return_value=opener), self.assertRaises(FirmwareError):
                download_release(self.release, self.path)
            self.assertEqual(len(opener.calls), 1)
            self.assert_no_partial_file()

    def test_external_insecure_and_credential_redirects_are_not_followed(self):
        for target in ("https://example.com/file.7z", "file:///tmp/update.7z",
                       self.release.cdn_url.replace("https:", "http:"),
                       self.release.cdn_url.replace("https://", "https://user:pass@")):
            error = HTTPError(self.release.resolver_url, 302, "Found", {"Location": target}, BytesIO())
            opener = Opener([error])
            with self.subTest(target=target), patch(
                    "swarm2.firmware_download.build_opener", return_value=opener), self.assertRaises(FirmwareError):
                download_release(self.release, self.path)
            self.assertEqual(len(opener.calls), 1)
            self.assert_no_partial_file()

    def test_known_https_redirect_to_cdn_is_accepted(self):
        error = HTTPError(self.release.resolver_url, 302, "Found",
                          {"Location": self.release.cdn_url}, BytesIO())
        result, opener = self.download([error, Response(self.data, self.release.cdn_url),
                                       Response(self.data, self.release.cdn_url)])
        self.assertEqual(len(opener.calls), 3)
        self.assertEqual(result.path.read_bytes(), self.data)

    def test_http_failure_disconnect_and_redirect_loop_are_bounded(self):
        errors = ([HTTPError(self.release.resolver_url, 404, "Missing", {}, BytesIO())],
                  [URLError("Offline")],
                  [HTTPError(self.release.resolver_url, 302, "Found",
                             {"Location": self.release.resolver_url}, BytesIO()) for _ in range(4)],
                  [self.responses()[0], Response(self.data, self.release.cdn_url, fail_after=17)])
        for responses in errors:
            with self.subTest(responses=responses), self.assertRaises(FirmwareError):
                self.download(responses)
            self.assert_no_partial_file()

    def test_total_deadline_failure_removes_temporary_file(self):
        opener = Opener(self.responses())
        real_reader = __import__("swarm2.firmware_download", fromlist=["_read_chunk"])._read_chunk
        def fail_during_archive(response, size, deadline):
            if response.geturl() == self.release.cdn_url:
                raise FirmwareError("Firmware download exceeded its time limit")
            return real_reader(response, size, deadline)
        with patch("swarm2.firmware_download.build_opener", return_value=opener), patch(
                "swarm2.firmware_download._read_chunk", side_effect=fail_during_archive), self.assertRaises(FirmwareError):
            download_release(self.release, self.path)
        self.assert_no_partial_file()

    def test_slow_fragments_cannot_reset_total_deadline(self):
        clock = itertools.count(0, 10)
        opener = Opener(self.responses())
        with patch("swarm2.firmware_download.build_opener", return_value=opener), patch(
                "swarm2.firmware_download.time.monotonic", side_effect=lambda: next(clock)), self.assertRaisesRegex(
                    FirmwareError, "time limit"):
            download_release(self.release, self.path)
        self.assertEqual(len(opener.calls), 1)
        self.assert_no_partial_file()

    def test_concurrent_destination_creation_is_never_overwritten(self):
        import os
        real_link = os.link
        def concurrent_file(source, destination, **kwargs):
            Path(destination).write_bytes(b"concurrent user file")
            return real_link(source, destination, **kwargs)
        with patch("swarm2.firmware_download.os.link", side_effect=concurrent_file), self.assertRaises(FirmwareError):
            self.download()
        self.assertEqual(self.path.read_bytes(), b"concurrent user file")
        self.assertEqual(list(self.path.parent.glob("*.part")), [])

    def test_destination_validation_precedes_network(self):
        with patch("swarm2.firmware_download.build_opener") as opened:
            for path in (self.path.with_suffix(".exe"), self.path.parent / "missing" / "file.7z"):
                with self.subTest(path=path), self.assertRaises(FirmwareError):
                    download_release(self.release, path)
            opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()
