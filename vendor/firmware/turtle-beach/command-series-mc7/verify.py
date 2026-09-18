#!/usr/bin/env python3
"""Verify the tracked MC7 firmware bundle without network or device access."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sys


BUNDLE_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = BUNDLE_DIR / "manifest.json"
CHECKSUMS_PATH = BUNDLE_DIR / "SHA256SUMS"
SCHEMA = "swarm2.vendor-firmware-bundle.v1"
HEX_MD5 = re.compile(r"[0-9a-f]{32}\Z")
HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class VerificationError(ValueError):
    """The bundle differs from its pinned manifest."""


def _digest(path: Path, algorithm: str) -> str:
    try:
        digest = hashlib.new(algorithm, usedforsecurity=False)
    except TypeError:  # Python implementations without usedforsecurity.
        digest = hashlib.new(algorithm)
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify() -> tuple[int, int]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if manifest.get("schema") != SCHEMA:
        raise VerificationError("unexpected manifest schema")

    releases = manifest.get("releases")
    if not isinstance(releases, list) or not releases:
        raise VerificationError("manifest has no releases")

    filenames: set[str] = set()
    keys: set[str] = set()
    expected_checksums: list[str] = []
    total_bytes = 0

    for release in releases:
        filename = release.get("filename")
        key = release.get("key")
        md5 = release.get("md5")
        sha256 = release.get("sha256")
        size = release.get("bytes")
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise VerificationError(f"invalid archive filename for {key!r}")
        if filename in filenames or not isinstance(key, str) or key in keys:
            raise VerificationError(f"duplicate release identity: {key!r}")
        if not isinstance(md5, str) or HEX_MD5.fullmatch(md5) is None:
            raise VerificationError(f"invalid MD5 for {key}")
        if not isinstance(sha256, str) or HEX_SHA256.fullmatch(sha256) is None:
            raise VerificationError(f"invalid SHA-256 for {key}")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise VerificationError(f"invalid size for {key}")

        archive = BUNDLE_DIR / filename
        if not archive.is_file():
            raise VerificationError(f"missing archive: {filename}")
        if archive.stat().st_size != size:
            raise VerificationError(f"size mismatch: {filename}")
        if _digest(archive, "md5") != md5:
            raise VerificationError(f"MD5 mismatch: {filename}")
        if _digest(archive, "sha256") != sha256:
            raise VerificationError(f"SHA-256 mismatch: {filename}")

        filenames.add(filename)
        keys.add(key)
        expected_checksums.append(f"{sha256}  {filename}\n")
        total_bytes += size

    actual_filenames = {path.name for path in BUNDLE_DIR.glob("*.7z")}
    if actual_filenames != filenames:
        missing = sorted(filenames - actual_filenames)
        unexpected = sorted(actual_filenames - filenames)
        raise VerificationError(
            f"archive set mismatch; missing={missing}, unexpected={unexpected}"
        )

    scope = manifest.get("scope", {})
    if scope.get("archive_count") != len(releases):
        raise VerificationError("manifest archive count mismatch")
    if scope.get("archive_bytes") != total_bytes:
        raise VerificationError("manifest total byte count mismatch")

    expected_text = "".join(sorted(expected_checksums, key=lambda line: line[66:]))
    if CHECKSUMS_PATH.read_text(encoding="ascii") != expected_text:
        raise VerificationError("SHA256SUMS differs from manifest")

    return len(releases), total_bytes


def main() -> int:
    try:
        count, total_bytes = verify()
    except (OSError, json.JSONDecodeError, VerificationError) as exc:
        print(f"MC7 firmware bundle verification failed: {exc}", file=sys.stderr)
        return 1
    print(f"Verified {count} MC7 firmware archives ({total_bytes} bytes).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
