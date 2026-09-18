#!/usr/bin/env python3
"""Collect and verify the exact source archives for an MC7 Studio AppImage."""

from __future__ import annotations

import argparse
from email.parser import Parser
import gzip
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_CONFIGURATION = (
    PROJECT_ROOT / "packaging" / "appimage" / "corresponding-sources.json"
)
DEFAULT_CACHE = PROJECT_ROOT / "tmp" / "corresponding-sources" / "cache"
SCHEMA = "mc7-studio.corresponding-source-archive.v1"
CONFIGURATION_SCHEMA = "mc7-studio.corresponding-sources.v1"
MAX_SOURCE_BYTES = 1024 * 1024 * 1024
REQUIRED_FIXED_COMPONENTS = {
    ("AppImage type-2 runtime", "20251108"),
    ("7-Zip", "26.03"),
    ("PyInstaller", "6.22.3"),
    ("hidapi", "0.15.0"),
    ("PySide6", "6.11.2"),
    ("Qt", "6.11.2"),
    ("Shiboken6", "6.11.2"),
}
REQUIRED_SOURCE_ARCHIVES = {
    "pyside-setup-everywhere-src-6.11.2.tar.xz",
    "qtbase-everywhere-src-6.11.2.tar.xz",
    "qtsvg-everywhere-src-6.11.2.tar.xz",
    "qtwayland-everywhere-src-6.11.2.tar.xz",
    "pyinstaller-6.22.3.tar.gz",
    "hidapi-0.15.0.tar.gz",
    "7z2603-src.tar.xz",
    "type2-runtime-20251108.tar.gz",
    "fuse-3.15.0.tar.xz",
    "squashfuse-0.5.2.tar.gz",
}


class SourceError(RuntimeError):
    """A source input or generated corresponding-source archive is invalid."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_stream(handle) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := handle.read(1024 * 1024):
        size += len(chunk)
        digest.update(chunk)
    return digest.hexdigest(), size


def _safe_filename(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or Path(value).name != value:
        raise SourceError(f"Invalid {label} filename")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+~-]*", value) is None:
        raise SourceError(f"Unsafe {label} filename: {value!r}")
    return value


def _load_json(path: Path, label: str) -> dict[str, object]:
    if not path.is_file() or path.is_symlink():
        raise SourceError(f"{label} is not a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SourceError(f"{label} is not valid JSON") from error
    if not isinstance(value, dict):
        raise SourceError(f"{label} root must be an object")
    return value


def _component_inventory(path: Path) -> tuple[str, list[dict[str, object]]]:
    value = _load_json(path, "AppImage component manifest")
    version = value.get("application_version")
    components = value.get("components")
    if (
        value.get("schema_version") != 1
        or value.get("application") != "MC7-Studio"
        or not isinstance(version, str)
        or re.fullmatch(r"[0-9]+(?:\.[0-9]+){2}", version) is None
        or not isinstance(components, list)
    ):
        raise SourceError("AppImage component manifest has an unexpected identity")
    normalized: list[dict[str, object]] = []
    identities: set[tuple[str, str]] = set()
    for component in components:
        if not isinstance(component, dict):
            raise SourceError("AppImage component entry is invalid")
        name = component.get("name")
        component_version = component.get("version")
        kind = component.get("kind")
        if not all(isinstance(item, str) and item for item in (name, component_version, kind)):
            raise SourceError("AppImage component identity is incomplete")
        identities.add((name, component_version))
        normalized.append(component)
        if kind == "python-wheel-vendored-library" and name != "ICU":
            raise SourceError(
                f"Opaque wheel-vendored native library remains in the AppImage: {name}"
            )
    missing = sorted(REQUIRED_FIXED_COMPONENTS - identities)
    if missing:
        raise SourceError(f"AppImage component manifest lacks required source identities: {missing}")
    return version, normalized


def _source_configuration(path: Path) -> list[dict[str, str]]:
    value = _load_json(path, "Corresponding-source configuration")
    sources = value.get("sources")
    if value.get("schema") != CONFIGURATION_SCHEMA or not isinstance(sources, list):
        raise SourceError("Corresponding-source configuration has an unexpected schema")
    result: list[dict[str, str]] = []
    filenames: set[str] = set()
    for item in sources:
        if not isinstance(item, dict):
            raise SourceError("Corresponding-source entry is invalid")
        filename = _safe_filename(item.get("filename"), "source")
        component = item.get("component")
        version = item.get("version")
        url = item.get("url")
        digest = item.get("sha256")
        if (
            filename in filenames
            or not isinstance(component, str)
            or not component
            or not isinstance(version, str)
            or not version
            or not isinstance(url, str)
            or not url.startswith("https://")
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise SourceError(f"Invalid corresponding-source entry for {filename}")
        filenames.add(filename)
        result.append({
            "component": component,
            "filename": filename,
            "sha256": digest,
            "url": url,
            "version": version,
        })
    if filenames != REQUIRED_SOURCE_ARCHIVES:
        missing = sorted(REQUIRED_SOURCE_ARCHIVES - filenames)
        extra = sorted(filenames - REQUIRED_SOURCE_ARCHIVES)
        raise SourceError(
            f"Corresponding-source configuration differs from required archives; "
            f"missing={missing}, extra={extra}"
        )
    return result


def _download_source(entry: dict[str, str], cache: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / entry["filename"]
    if destination.is_file() and not destination.is_symlink():
        if sha256(destination) == entry["sha256"]:
            return destination
        destination.unlink()
    elif destination.exists():
        raise SourceError(f"Unsafe source cache path: {destination}")
    partial = destination.with_name(destination.name + ".part")
    partial.unlink(missing_ok=True)
    request = Request(entry["url"], headers={"User-Agent": "MC7-Studio-source-builder/0.1"})
    try:
        with urlopen(request, timeout=120) as response, partial.open("xb") as output:
            size = 0
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_SOURCE_BYTES:
                    raise SourceError(f"Source archive exceeds size limit: {entry['filename']}")
                output.write(chunk)
        actual = sha256(partial)
        if actual != entry["sha256"]:
            raise SourceError(
                f"Source archive hash mismatch for {entry['filename']}: {actual}"
            )
        os.replace(partial, destination)
    finally:
        partial.unlink(missing_ok=True)
    return destination


def _paragraph_fields(text: str) -> dict[str, str]:
    paragraphs = [paragraph for paragraph in text.split("\n\n") if paragraph.strip()]
    if not paragraphs:
        raise SourceError("apt-cache returned no package metadata")
    return dict(Parser().parsestr(paragraphs[0]).items())


def _debian_source_identity(binary: str, version: str) -> tuple[str, str]:
    result = subprocess.run(
        ["apt-cache", "show", "--no-all-versions", f"{binary}={version}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode or not result.stdout.strip():
        raise SourceError(f"Cannot resolve Debian source for {binary}={version}")
    fields = _paragraph_fields(result.stdout)
    if fields.get("Version") != version:
        raise SourceError(f"apt-cache returned the wrong version for {binary}")
    source = fields.get("Source", binary.split(":", 1)[0])
    match = re.fullmatch(r"([a-z0-9][a-z0-9+.-]*)(?: \(([^)]+)\))?", source)
    if match is None:
        raise SourceError(f"Invalid Debian Source field for {binary}: {source!r}")
    return match.group(1), match.group(2) or version


def _clear_signed_payload(text: str) -> str:
    if not text.startswith("-----BEGIN PGP SIGNED MESSAGE-----"):
        return text
    separator = text.find("\n\n")
    signature = text.find("\n-----BEGIN PGP SIGNATURE-----")
    if separator < 0 or signature < 0 or signature <= separator:
        raise SourceError("Debian source control file has malformed clear-signing")
    lines = text[separator + 2:signature].splitlines()
    return "\n".join(line[2:] if line.startswith("- ") else line for line in lines) + "\n"


def _dsc_inventory(path: Path, source: str, version: str) -> list[dict[str, object]]:
    try:
        payload = _clear_signed_payload(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as error:
        raise SourceError(f"Cannot read Debian source control file: {path.name}") from error
    fields = dict(Parser().parsestr(payload).items())
    if fields.get("Source") != source or fields.get("Version") != version:
        raise SourceError(f"Debian source identity mismatch in {path.name}")
    checksums = fields.get("Checksums-Sha256", "")
    files: list[dict[str, object]] = []
    names: set[str] = set()
    for line in checksums.splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 3:
            raise SourceError(f"Malformed Checksums-Sha256 entry in {path.name}")
        digest, size_text, filename = parts
        safe_name = _safe_filename(filename, "Debian source")
        if (
            safe_name in names
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or not size_text.isdecimal()
        ):
            raise SourceError(f"Invalid Debian source checksum in {path.name}")
        source_file = path.parent / safe_name
        if not source_file.is_file() or source_file.is_symlink():
            raise SourceError(f"Debian source file is missing: {safe_name}")
        if source_file.stat().st_size != int(size_text) or sha256(source_file) != digest:
            raise SourceError(f"Debian source file failed its .dsc checksum: {safe_name}")
        names.add(safe_name)
        files.append({"filename": safe_name, "bytes": int(size_text), "sha256": digest})
    if not files:
        raise SourceError(f"Debian source control file has no SHA-256 inventory: {path.name}")
    return files


def _download_debian_source(
    source: str,
    version: str,
    binaries: list[dict[str, str]],
    destination: Path,
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="mc7-source-", dir=destination.parent) as directory:
        temporary = Path(directory)
        result = subprocess.run(
            [
                "apt-get",
                "source",
                "--download-only",
                "--only-source",
                f"{source}={version}",
            ],
            cwd=temporary,
            check=False,
        )
        if result.returncode:
            raise SourceError(f"apt-get source failed for {source}={version}")
        controls = sorted(temporary.glob("*.dsc"))
        if len(controls) != 1:
            raise SourceError(f"Expected one .dsc for {source}={version}")
        payload_files = _dsc_inventory(controls[0], source, version)
        allowed = {controls[0].name, *(item["filename"] for item in payload_files)}
        actual = {path.name for path in temporary.iterdir() if path.is_file()}
        if actual != allowed:
            raise SourceError(f"Unexpected Debian source files for {source}={version}")
        destination.mkdir(parents=True)
        copied: list[dict[str, object]] = []
        for source_file in sorted(temporary.iterdir(), key=lambda item: item.name):
            target = destination / source_file.name
            shutil.copyfile(source_file, target)
            target.chmod(0o644)
            copied.append({
                "filename": target.name,
                "bytes": target.stat().st_size,
                "sha256": sha256(target),
            })
    return {
        "binary_packages": sorted(binaries, key=lambda item: item["name"]),
        "files": copied,
        "source": source,
        "version": version,
    }


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


def _write_archive(staging: Path, destination: Path, epoch: int) -> None:
    prefix = staging.name
    files = sorted(path for path in staging.rglob("*") if path.is_file())
    directories = sorted(
        (path for path in staging.rglob("*") if path.is_dir()),
        key=lambda path: (len(path.parts), path.as_posix()),
    )
    temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        with temporary.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=epoch) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                    archive.addfile(_tar_info(prefix, epoch=epoch, directory=True))
                    for directory in directories:
                        relative = directory.relative_to(staging)
                        archive.addfile(
                            _tar_info(f"{prefix}/{relative.as_posix()}", epoch=epoch, directory=True)
                        )
                    for path in files:
                        relative = path.relative_to(staging)
                        with path.open("rb") as handle:
                            archive.addfile(
                                _tar_info(
                                    f"{prefix}/{relative.as_posix()}",
                                    epoch=epoch,
                                    size=path.stat().st_size,
                                ),
                                handle,
                            )
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _payload_inventory(staging: Path) -> list[dict[str, object]]:
    return [
        {
            "path": path.relative_to(staging).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": sha256(path),
        }
        for path in sorted(staging.rglob("*"))
        if path.is_file() and path.name != "SOURCES.json"
    ]


def build(
    component_manifest: Path,
    output: Path,
    cache: Path,
    configuration: Path,
    epoch: int,
) -> list[Path]:
    version, components = _component_inventory(component_manifest)
    fixed_sources = _source_configuration(configuration)
    if output.exists():
        raise SourceError(f"Refusing to replace existing output directory: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="corresponding-sources-", dir=output.parent) as work:
        work_root = Path(work)
        staging = work_root / f"MC7-Studio-{version}-corresponding-sources"
        upstream = staging / "upstream"
        debian = staging / "debian"
        upstream.mkdir(parents=True)
        debian.mkdir()

        fixed_records: list[dict[str, object]] = []
        for entry in fixed_sources:
            source_path = _download_source(entry, cache)
            target = upstream / entry["filename"]
            shutil.copyfile(source_path, target)
            target.chmod(0o644)
            fixed_records.append({
                **entry,
                "bytes": target.stat().st_size,
                "path": target.relative_to(staging).as_posix(),
            })

        binary_sources: dict[tuple[str, str], list[dict[str, str]]] = {}
        for component in components:
            if component["kind"] != "debian-package":
                continue
            binary = str(component["name"])
            binary_version = str(component["version"])
            source_identity = _debian_source_identity(binary, binary_version)
            binary_sources.setdefault(source_identity, []).append({
                "name": binary,
                "version": binary_version,
            })

        debian_records: list[dict[str, object]] = []
        for (source, source_version), binaries in sorted(binary_sources.items()):
            safe_version = re.sub(r"[^A-Za-z0-9._+-]", "_", source_version)
            target = debian / f"{source}_{safe_version}"
            record = _download_debian_source(
                source, source_version, binaries, target,
            )
            for item in record["files"]:
                item["path"] = (target / str(item["filename"])).relative_to(staging).as_posix()
            debian_records.append(record)

        shutil.copyfile(component_manifest, staging / "BUNDLED-COMPONENTS.json")
        shutil.copyfile(configuration, staging / "corresponding-sources.json")
        readme = (
            f"# MC7 Studio {version} corresponding sources\n\n"
            "This archive accompanies the MC7 Studio AppImage. It retains the exact pinned upstream sources for the copyleft components and the authenticated Ubuntu source packages corresponding to every Debian native library copied into the AppImage.\n\n"
            "`BUNDLED-COMPONENTS.json` is the binary payload inventory emitted by the AppImage build. `SOURCES.json` maps that inventory to the retained source archives and records the SHA-256 digest of every file in this archive.\n\n"
            "Run `python3 scripts/build_corresponding_sources.py --verify-only ARCHIVE` from an MC7 Studio source checkout to verify paths, sizes and hashes.\n"
        )
        (staging / "README.md").write_text(readme, encoding="utf-8")
        source_manifest = {
            "schema": SCHEMA,
            "application": "MC7-Studio",
            "application_version": version,
            "debian_sources": debian_records,
            "fixed_sources": fixed_records,
            "payload_files": _payload_inventory(staging),
        }
        (staging / "SOURCES.json").write_text(
            json.dumps(source_manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        output.mkdir()
        archive = output / f"MC7-Studio-{version}-corresponding-sources.tar.gz"
        _write_archive(staging, archive, epoch)
        checksum = archive.with_name(archive.name + ".sha256")
        checksum.write_text(f"{sha256(archive)}  {archive.name}\n", encoding="ascii")
    verify_archive(archive)
    return [archive, checksum]


def verify_archive(path: Path) -> dict[str, object]:
    if not path.is_file() or path.is_symlink():
        raise SourceError(f"Source archive is not a regular file: {path}")
    try:
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
            regular = [member for member in members if member.isfile()]
            if not members or any(not (member.isfile() or member.isdir()) for member in members):
                raise SourceError("Source archive contains a link or special file")
            names = [PurePosixPath(member.name) for member in members]
            if any(name.is_absolute() or ".." in name.parts for name in names):
                raise SourceError("Source archive contains an unsafe path")
            if len({member.name for member in members}) != len(members):
                raise SourceError("Source archive contains duplicate paths")
            roots = {name.parts[0] for name in names if name.parts}
            if len(roots) != 1:
                raise SourceError("Source archive does not have one root directory")
            root = next(iter(roots))
            manifest_name = f"{root}/SOURCES.json"
            manifest_member = archive.getmember(manifest_name)
            manifest_handle = archive.extractfile(manifest_member)
            if manifest_handle is None or manifest_member.size > 16 * 1024 * 1024:
                raise SourceError("Source archive manifest is missing or too large")
            with manifest_handle:
                manifest = json.loads(manifest_handle.read().decode("utf-8"))
            if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
                raise SourceError("Source archive manifest has an unexpected schema")
            expected = manifest.get("payload_files")
            if not isinstance(expected, list):
                raise SourceError("Source archive has no payload inventory")
            actual: dict[str, tuple[int, str]] = {}
            for member in regular:
                relative = member.name.removeprefix(root + "/")
                if relative == "SOURCES.json":
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    raise SourceError(f"Cannot read source archive member: {relative}")
                with handle:
                    digest, size = _hash_stream(handle)
                actual[relative] = (size, digest)
            declared: dict[str, tuple[int, str]] = {}
            for item in expected:
                if not isinstance(item, dict):
                    raise SourceError("Source archive payload entry is invalid")
                relative = item.get("path")
                size = item.get("bytes")
                digest = item.get("sha256")
                parsed = PurePosixPath(relative) if isinstance(relative, str) else PurePosixPath()
                if (
                    not relative
                    or parsed.is_absolute()
                    or ".." in parsed.parts
                    or relative in declared
                    or not isinstance(size, int)
                    or isinstance(size, bool)
                    or size < 0
                    or not isinstance(digest, str)
                    or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                ):
                    raise SourceError("Source archive payload identity is invalid")
                declared[relative] = (size, digest)
            if actual != declared:
                raise SourceError("Source archive payload differs from SOURCES.json")
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, tarfile.TarError) as error:
        if isinstance(error, SourceError):
            raise
        raise SourceError(f"Cannot verify source archive: {path}") from error
    return manifest


def _path(value: str) -> Path:
    return Path(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--component-manifest", type=_path)
    parser.add_argument("--output-dir", type=_path, default=PROJECT_ROOT / "dist" / "sources")
    parser.add_argument("--cache-dir", type=_path, default=DEFAULT_CACHE)
    parser.add_argument("--configuration", type=_path, default=SOURCE_CONFIGURATION)
    parser.add_argument("--source-date-epoch", type=int)
    parser.add_argument("--verify-only", type=_path)
    arguments = parser.parse_args(argv)
    try:
        if arguments.verify_only is not None:
            verify_archive(arguments.verify_only.expanduser().resolve())
            print(f"Verified corresponding-source archive: {arguments.verify_only}")
            return 0
        if arguments.component_manifest is None:
            parser.error("--component-manifest is required unless --verify-only is used")
        epoch = arguments.source_date_epoch
        if epoch is None:
            value = os.environ.get("SOURCE_DATE_EPOCH", "")
            if not value.isdecimal():
                raise SourceError("Set SOURCE_DATE_EPOCH or pass --source-date-epoch")
            epoch = int(value)
        if epoch < 315_532_800:
            raise SourceError("Source date epoch must be at or after 1980-01-01")
        artifacts = build(
            arguments.component_manifest.expanduser().resolve(),
            arguments.output_dir.expanduser().resolve(),
            arguments.cache_dir.expanduser().resolve(),
            arguments.configuration.expanduser().resolve(),
            epoch,
        )
        print(f"Built {len(artifacts)} corresponding-source release files")
        return 0
    except (SourceError, OSError, subprocess.SubprocessError) as error:
        print(f"Corresponding-source build error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
