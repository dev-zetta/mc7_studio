"""Regular-file reads preserve binary data and reject indirect inputs."""

import os
from pathlib import Path
import tempfile
import unittest

from swarm2.file_io import open_regular_read


class RegularReadTests(unittest.TestCase):
    def test_binary_bytes_are_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.bin"
            payload = b"\x00\xff\r\n\x1a\n"
            path.write_bytes(payload)
            with os.fdopen(open_regular_read(path), "rb") as stream:
                self.assertEqual(stream.read(), payload)

    def test_symlink_and_directory_are_rejected_before_reading(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "data.bin"
            path.write_bytes(b"unchanged")
            link = root / "link.bin"
            link.symlink_to(path)
            for candidate in (root, link):
                with self.subTest(candidate=candidate), self.assertRaises(OSError):
                    open_regular_read(candidate)
            self.assertEqual(path.read_bytes(), b"unchanged")
