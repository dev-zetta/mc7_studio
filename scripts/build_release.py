#!/usr/bin/env python3
"""Build and verify the public MC7 Studio release artifacts.

Vendor firmware payloads are excluded.  A separate provenance artifact records
the official URLs, sizes, and hashes so users can verify their own authorized
downloads without treating Turtle Beach firmware as project code.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIRMWARE_RELATIVE = Path("vendor/firmware/turtle-beach/command-series-mc7")
FIRMWARE_PROVENANCE_FILES = ("README.md", "manifest.json", "SHA256SUMS", "verify.py")
DEFAULT_SOURCE_DATE_EPOCH = 1_789_516_800  # 2026-09-16 00:00:00 UTC
CHECKSUM_NAME = "SHA256SUMS"
RELEASE_MANIFEST_NAME = "release-manifest.json"
LOCAL_ONLY_PATHS = frozenset({
    ".research",
    ".tools",
    ".update",
    "artifacts",
    "docs-local",
    "scripts-local",
    "tests-local",
    "tmp",
})
FORBIDDEN_RELEASE_SUFFIXES = frozenset({".7z", ".dll", ".exe", ".msi"})
PROVENANCE_README = """# Turtle Beach Command Series MC7 firmware provenance

This artifact contains no Turtle Beach firmware payloads. `manifest.json` records the official public download URLs, sizes, MD5 values, and SHA-256 values recovered on 2026-09-16. `SHA256SUMS` is the corresponding portable checksum list.

After obtaining firmware under terms that authorize your download, place the seven official `.7z` files beside these metadata files and run:

```sh
python3 verify.py
```

Firmware and associated copyrights remain with Turtle Beach and their respective owners. The project's license does not cover those payloads.
"""


class ReleaseError(RuntimeError):
    """A release input or generated artifact failed validation."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _quoted_project_value(project: str, key: str) -> str:
    match = re.search(r"(?ms)^\[project\]\s*(.*?)(?=^\[|\Z)", project)
    if not match:
        raise ReleaseError("pyproject.toml has no [project] table")
    value = re.search(rf'(?m)^{re.escape(key)}\s*=\s*["\']([^"\']+)["\']\s*$',
                      match.group(1))
    if not value:
        raise ReleaseError(f"pyproject.toml has no simple quoted project {key}")
    return value.group(1)


def project_identity(root: Path = PROJECT_ROOT) -> tuple[str, str]:
    project = (root / "pyproject.toml").read_text(encoding="utf-8")
    name = _quoted_project_value(project, "name")
    version = _quoted_project_value(project, "version")
    package = (root / "src/swarm2/__init__.py").read_text(encoding="utf-8")
    package_version = re.search(
        r'(?m)^__version__\s*=\s*["\']([^"\']+)["\']\s*$', package)
    if not package_version or package_version.group(1) != version:
        raise ReleaseError("pyproject.toml and swarm2.__version__ disagree")
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){2}", version):
        raise ReleaseError(f"Unsupported release version {version!r}")
    return name, version


def verify_firmware_source(firmware_root: Path) -> list[Path]:
    verifier = firmware_root / "verify.py"
    manifest = firmware_root / "manifest.json"
    checksums = firmware_root / CHECKSUM_NAME
    required = [firmware_root / name for name in FIRMWARE_PROVENANCE_FILES]
    for required_path in required:
        if not required_path.is_file() or required_path.is_symlink():
            raise ReleaseError(f"Firmware provenance is missing {required_path.name}")
    try:
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReleaseError("Firmware provenance manifest is not valid JSON") from error
    releases = metadata.get("releases")
    if metadata.get("schema") != "swarm2.vendor-firmware-bundle.v1":
        raise ReleaseError("Firmware provenance manifest has an unexpected schema")
    if not isinstance(releases, list) or not releases:
        raise ReleaseError("Firmware provenance manifest has no releases")
    entries: list[tuple[str, str]] = []
    keys: set[str] = set()
    filenames: set[str] = set()
    total_bytes = 0
    for release in releases:
        if not isinstance(release, dict):
            raise ReleaseError("Firmware provenance release entry is invalid")
        filename = release.get("filename")
        key = release.get("key")
        size = release.get("bytes")
        md5 = release.get("md5")
        sha256 = release.get("sha256")
        resolver_url = release.get("resolver_url")
        cdn_url = release.get("cdn_url")
        if (not isinstance(key, str) or key in keys
                or not isinstance(filename, str) or filename in filenames
                or Path(filename).name != filename
                or not filename.endswith(".7z")
                or not isinstance(size, int) or isinstance(size, bool) or size <= 0
                or not isinstance(md5, str) or re.fullmatch(r"[0-9a-f]{32}", md5) is None
                or not isinstance(sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
                or not isinstance(resolver_url, str)
                or not resolver_url.startswith("https://acpr.prod.turtlebeach.com/")
                or not isinstance(cdn_url, str)
                or not cdn_url.startswith("https://cdn.turtlebeach.com/")):
            raise ReleaseError("Firmware provenance release identity is invalid")
        keys.add(key)
        filenames.add(filename)
        total_bytes += size
        entries.append((filename, sha256))
    scope = metadata.get("scope")
    if (not isinstance(scope, dict) or scope.get("archive_count") != len(entries)
            or scope.get("archive_bytes") != total_bytes):
        raise ReleaseError("Firmware provenance scope differs from its releases")
    expected_checksums = "".join(
        f"{digest}  {filename}\n" for filename, digest in sorted(entries))
    if checksums.read_text(encoding="ascii") != expected_checksums:
        raise ReleaseError("Firmware provenance checksums differ from the manifest")
    expected_archives = {filename for filename, _ in entries}
    archives = list(firmware_root.glob("*.7z"))
    actual_archives = {path.name for path in archives}
    if actual_archives and actual_archives != expected_archives:
        raise ReleaseError("Local firmware backup is incomplete")
    if actual_archives:
        for archive in archives:
            if not stat.S_ISREG(archive.lstat().st_mode):
                raise ReleaseError(f"Local firmware backup contains a link: {archive.name}")
        result = subprocess.run(
            [sys.executable, str(verifier)], cwd=firmware_root, check=False)
        if result.returncode:
            raise ReleaseError(f"Firmware backup verifier exited with {result.returncode}")
    return required


def _tar_info(name: str, *, epoch: int, size: int = 0, directory: bool = False) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name + ("/" if directory else ""))
    info.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
    info.size = 0 if directory else size
    info.mode = 0o755 if directory else 0o644
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = epoch
    return info


def create_firmware_provenance_archive(
        firmware_root: Path, destination: Path, version: str, epoch: int) -> None:
    files = verify_firmware_source(firmware_root)
    prefix = Path(f"swarm2-mc7-firmware-provenance-{version}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        with temporary.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw,
                               compresslevel=9, mtime=epoch) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                    archive.addfile(_tar_info(prefix.as_posix(), epoch=epoch, directory=True))
                    readme = PROVENANCE_README.encode("utf-8")
                    archive.addfile(
                        _tar_info((prefix / "README.md").as_posix(), epoch=epoch,
                                  size=len(readme)), BytesIO(readme))
                    for source in files:
                        if source.name == "README.md":
                            continue
                        target = (prefix / source.name).as_posix()
                        with source.open("rb") as handle:
                            archive.addfile(
                                _tar_info(target, epoch=epoch, size=source.stat().st_size), handle)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def normalize_source_distribution(path: Path, epoch: int) -> None:
    """Rewrite a generated sdist with stable gzip and tar metadata."""
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with tarfile.open(path, "r:gz") as source:
            members = sorted(source.getmembers(), key=lambda member: member.name)
            names: set[str] = set()
            with temporary.open("wb") as raw:
                with gzip.GzipFile(filename="", mode="wb", fileobj=raw,
                                   compresslevel=9, mtime=epoch) as compressed:
                    with tarfile.open(
                            fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as output:
                        for member in members:
                            name = member.name.rstrip("/")
                            parsed = PurePosixPath(name)
                            if (not name or parsed.is_absolute() or ".." in parsed.parts
                                    or name in names):
                                raise ReleaseError("Python source distribution has an unsafe path")
                            names.add(name)
                            if member.isdir():
                                output.addfile(_tar_info(name, epoch=epoch, directory=True))
                                continue
                            if not member.isfile():
                                raise ReleaseError(
                                    "Python source distribution contains a link or special file")
                            extracted = source.extractfile(member)
                            if extracted is None:
                                raise ReleaseError("Could not read Python source distribution member")
                            info = _tar_info(name, epoch=epoch, size=member.size)
                            info.mode = 0o755 if member.mode & 0o111 else 0o644
                            with extracted:
                                output.addfile(info, extracted)
        os.replace(temporary, path)
    except (OSError, tarfile.TarError) as error:
        raise ReleaseError("Could not normalize Python source distribution") from error
    finally:
        temporary.unlink(missing_ok=True)


def _require_release_tag(root: Path, version: str, provenance_files: list[Path]) -> None:
    expected = f"v{version}"
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, check=False,
        capture_output=True, text=True)
    if status.returncode:
        raise ReleaseError(f"Could not inspect release checkout: {status.stderr.strip()}")
    if status.stdout:
        raise ReleaseError("A tagged release must be built from a clean worktree")
    tag = subprocess.run(
        ["git", "describe", "--tags", "--exact-match", "HEAD"], cwd=root,
        check=False, capture_output=True, text=True)
    if tag.returncode or tag.stdout.strip() != expected:
        raise ReleaseError(f"HEAD must have the exact tag {expected}")
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--", FIRMWARE_RELATIVE.as_posix()], cwd=root,
        check=True, capture_output=True).stdout.split(b"\0")
    tracked_names = {item.decode("utf-8") for item in tracked if item}
    source_names = {path.relative_to(root).as_posix() for path in provenance_files}
    if tracked_names != source_names:
        raise ReleaseError(
            "Only the firmware provenance metadata may be tracked in a public release")


def _build_python_distributions(root: Path, output: Path, epoch: int) -> list[Path]:
    environment = dict(os.environ)
    environment["SOURCE_DATE_EPOCH"] = str(epoch)
    result = subprocess.run(
        [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(output)],
        cwd=root, env=environment, check=False)
    if result.returncode:
        raise ReleaseError(f"Python distribution build exited with {result.returncode}")
    wheels = sorted(output.glob("*.whl"))
    source_archives = sorted(output.glob("*.tar.gz"))
    if len(wheels) != 1 or len(source_archives) != 1:
        raise ReleaseError("Expected exactly one wheel and one Python source distribution")
    normalize_source_distribution(source_archives[0], epoch)
    return wheels + source_archives


def _artifact_record(path: Path) -> dict[str, object]:
    return {"filename": path.name, "size": path.stat().st_size, "sha256": _sha256(path)}


def write_release_metadata(
        output: Path, *, project: str, version: str, epoch: int,
        firmware_manifest: Path, artifacts: list[Path]) -> None:
    records = [_artifact_record(path) for path in sorted(artifacts, key=lambda item: item.name)]
    manifest = {
        "schema_version": 1,
        "project": project,
        "version": version,
        "source_date_epoch": epoch,
        "firmware": {
            "source_directory": FIRMWARE_RELATIVE.as_posix(),
            "source_manifest_sha256": _sha256(firmware_manifest),
            "payloads_included": False,
        },
        "artifacts": records,
    }
    manifest_path = output / RELEASE_MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    checksummed = sorted(artifacts + [manifest_path], key=lambda item: item.name)
    (output / CHECKSUM_NAME).write_text(
        "".join(f"{_sha256(path)}  {path.name}\n" for path in checksummed),
        encoding="ascii")


def _verify_provenance_archive(path: Path, version: str) -> None:
    prefix = f"swarm2-mc7-firmware-provenance-{version}"
    expected = {
        prefix,
        f"{prefix}/README.md",
        f"{prefix}/manifest.json",
        f"{prefix}/SHA256SUMS",
        f"{prefix}/verify.py",
    }
    try:
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
            names = [member.name.rstrip("/") for member in members]
            if len(names) != len(set(names)) or set(names) != expected:
                raise ReleaseError("Firmware provenance archive inventory is invalid")
            for member in members:
                if member.name.lower().endswith(".7z"):
                    raise ReleaseError("Public release contains a firmware payload")
                if not (member.isdir() or member.isfile()) or member.size > 1024 * 1024:
                    raise ReleaseError("Firmware provenance archive contains an unsafe entry")
    except (OSError, tarfile.TarError) as error:
        raise ReleaseError("Could not inspect firmware provenance archive") from error


def _reject_private_release_material(paths: list[Path]) -> None:
    """Reject vendor binaries and local research paths from public containers."""
    try:
        for path in paths:
            if path.suffix == ".whl":
                with zipfile.ZipFile(path) as archive:
                    names = archive.namelist()
            elif path.name.endswith(".tar.gz"):
                with tarfile.open(path, "r:gz") as archive:
                    names = archive.getnames()
            else:
                continue
            for name in names:
                member = PurePosixPath(name)
                if member.is_absolute() or ".." in member.parts:
                    raise ReleaseError(
                        f"Public release contains an unsafe path: {path.name}")
                if any(part in LOCAL_ONLY_PATHS for part in member.parts):
                    raise ReleaseError(
                        f"Public release contains local-only material: {path.name}")
                if member.suffix.lower() in FORBIDDEN_RELEASE_SUFFIXES:
                    raise ReleaseError(
                        f"Public release contains a vendor binary: {path.name}")
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as error:
        raise ReleaseError("Could not inspect a public release container") from error


def verify_release_output(output: Path) -> dict[str, object]:
    if not output.is_dir() or output.is_symlink():
        raise ReleaseError(f"Release output directory is missing: {output}")
    checksum_path = output / CHECKSUM_NAME
    if not checksum_path.is_file() or checksum_path.is_symlink():
        raise ReleaseError(f"Release output has no {CHECKSUM_NAME}")
    expected: dict[str, str] = {}
    for line in checksum_path.read_text(encoding="ascii").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._+-]*)", line)
        if not match or match.group(2) in expected:
            raise ReleaseError(f"Invalid release checksum line: {line!r}")
        expected[match.group(2)] = match.group(1)
    actual_paths: dict[str, Path] = {}
    for path in output.iterdir():
        if path.name == CHECKSUM_NAME:
            continue
        if not path.is_file() or path.is_symlink():
            raise ReleaseError(f"Unexpected release output entry: {path.name}")
        actual_paths[path.name] = path
    if set(expected) != set(actual_paths):
        raise ReleaseError("Release checksum inventory does not match the output directory")
    for name, digest in expected.items():
        if _sha256(actual_paths[name]) != digest:
            raise ReleaseError(f"Release artifact checksum mismatch: {name}")
    manifest_path = actual_paths.get(RELEASE_MANIFEST_NAME)
    if manifest_path is None:
        raise ReleaseError(f"Release output has no {RELEASE_MANIFEST_NAME}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReleaseError("Release manifest is not valid JSON") from error
    records = manifest.get("artifacts")
    if not isinstance(records, list):
        raise ReleaseError("Release manifest has no artifact inventory")
    recorded = {record.get("filename"): record for record in records if isinstance(record, dict)}
    artifact_names = set(actual_paths) - {RELEASE_MANIFEST_NAME}
    project = manifest.get("project")
    version = manifest.get("version")
    if not isinstance(project, str) or not isinstance(version, str):
        raise ReleaseError("Release manifest has no project identity")
    distribution = project.replace("-", "_")
    provenance_name = f"swarm2-mc7-firmware-provenance-{version}.tar.gz"
    expected_artifacts = {
        f"{distribution}-{version}-py3-none-any.whl",
        f"{distribution}-{version}.tar.gz",
        provenance_name,
    }
    if artifact_names != expected_artifacts:
        raise ReleaseError("Release output does not contain the three expected public artifacts")
    firmware = manifest.get("firmware")
    if not isinstance(firmware, dict) or firmware.get("payloads_included") is not False:
        raise ReleaseError("Release manifest does not exclude firmware payloads")
    if set(recorded) != artifact_names:
        raise ReleaseError("Release manifest artifact inventory is incomplete")
    for name in artifact_names:
        record = recorded[name]
        path = actual_paths[name]
        if record.get("size") != path.stat().st_size or record.get("sha256") != _sha256(path):
            raise ReleaseError(f"Release manifest metadata mismatch: {name}")
    _reject_private_release_material(
        [actual_paths[name] for name in sorted(artifact_names)])
    _verify_provenance_archive(actual_paths[provenance_name], version)
    return manifest


def build_release(output: Path, *, epoch: int, require_tag: bool = False) -> dict[str, object]:
    project, version = project_identity()
    firmware_root = PROJECT_ROOT / FIRMWARE_RELATIVE
    files = verify_firmware_source(firmware_root)
    if require_tag:
        _require_release_tag(PROJECT_ROOT, version, files)
    if output.exists():
        raise ReleaseError(f"Refusing to replace existing release output: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".swarm2-release-", dir=output.parent))
    try:
        python_artifacts = _build_python_distributions(PROJECT_ROOT, staging, epoch)
        provenance_archive = staging / f"swarm2-mc7-firmware-provenance-{version}.tar.gz"
        create_firmware_provenance_archive(firmware_root, provenance_archive, version, epoch)
        artifacts = python_artifacts + [provenance_archive]
        write_release_metadata(
            staging, project=project, version=version, epoch=epoch,
            firmware_manifest=firmware_root / "manifest.json", artifacts=artifacts)
        manifest = verify_release_output(staging)
        os.replace(staging, output)
        return manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _positive_epoch(value: str) -> int:
    try:
        epoch = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("epoch must be an integer") from error
    if epoch < 315_532_800:
        raise argparse.ArgumentTypeError("epoch must be 1980-01-01 or later")
    return epoch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", type=Path,
        help="new output directory (default: dist/release-<version>)")
    parser.add_argument(
        "--source-date-epoch", type=_positive_epoch,
        default=_positive_epoch(os.environ.get("SOURCE_DATE_EPOCH", str(DEFAULT_SOURCE_DATE_EPOCH))))
    parser.add_argument(
        "--require-tag", action="store_true",
        help="require a clean worktree, exact v<version> tag, and tracked firmware provenance")
    parser.add_argument(
        "--verify-only", type=Path, metavar="DIRECTORY",
        help="verify an existing release output instead of building")
    arguments = parser.parse_args(argv)
    try:
        if arguments.verify_only:
            manifest = verify_release_output(arguments.verify_only.resolve())
            print(f"Verified {len(manifest['artifacts'])} release artifacts for v{manifest['version']}")
            return 0
        _, version = project_identity()
        output = (arguments.output_dir or PROJECT_ROOT / "dist" / f"release-{version}").resolve()
        manifest = build_release(
            output, epoch=arguments.source_date_epoch, require_tag=arguments.require_tag)
        print(f"Built {len(manifest['artifacts'])} verified release artifacts in {output}")
        return 0
    except (OSError, ReleaseError, subprocess.SubprocessError) as error:
        print(f"release error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
