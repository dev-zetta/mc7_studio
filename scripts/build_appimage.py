#!/usr/bin/env python3
"""Build the pinned x86_64 MC7 Studio AppImage in project-local storage."""

from __future__ import annotations

import argparse
import ast
import ctypes.util
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
try:
    import tomllib
except ImportError:  # Python 3.10 test environments
    import tomli as tomllib
from typing import NamedTuple
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGING_ROOT = PROJECT_ROOT / "packaging" / "appimage"
LOCAL_TMP_ROOT = PROJECT_ROOT / "tmp" / "appimage"
TOOLS_ROOT = LOCAL_TMP_ROOT / "tools"
BUILD_ROOT = LOCAL_TMP_ROOT / "build"

APPIMAGETOOL_VERSION = "1.9.1"
APPIMAGETOOL_URL = (
    "https://github.com/AppImage/appimagetool/releases/download/1.9.1/"
    "appimagetool-x86_64.AppImage"
)
APPIMAGETOOL_SHA256 = "ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"
RUNTIME_VERSION = "20251108"
RUNTIME_URL = (
    "https://github.com/AppImage/type2-runtime/releases/download/20251108/"
    "runtime-x86_64"
)
RUNTIME_SHA256 = "2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d"
SEVEN_ZIP_VERSION = "26.03"
SEVEN_ZIP_URL = (
    "https://github.com/ip7z/7zip/releases/download/26.03/"
    "7z2603-linux-x64.tar.xz"
)
SEVEN_ZIP_SHA256 = "dc99eff5008f1ab79bd7084c68513701547a808a89502bf4133683535ab3c695"

PINNED_PYTHON_PACKAGES = {
    "PyInstaller": "6.22.3",
    "PySide6": "6.11.2",
    "PySide6_Essentials": "6.11.2",
    "PySide6_Addons": "6.11.2",
    "shiboken6": "6.11.2",
    "hidapi": "0.15.0",
    "psutil": "7.2.2",
    "websocket-client": "1.9.2",
    "certifi": "2026.7.22",
}
PYTHON_NATIVE_DISTRIBUTIONS = (
    "PySide6",
    "PySide6_Essentials",
    "PySide6_Addons",
    "shiboken6",
    "hidapi",
    "psutil",
)
STATIC_LICENSE_FILES = (
    "GPL-2.0.txt",
    "GPL-3.0.txt",
    "LGPL-2.1.txt",
    "LGPL-3.0.txt",
    "ICU-73.2-LICENSE.txt",
    "QT-PYSIDE-SHIBOKEN-NOTICE.txt",
    "HIDAPI-WHEEL-THIRD-PARTY-NOTICES.txt",
    "PySide6-bufferprocs_py37.h",
    "PySide6-PSF-3.7.0.txt",
)
HIDAPI_WHEEL_LIBRARIES = {
    "libattr-": ("attr", "LGPL-2.1-or-later"),
    "libbz2-": ("bzip2", "bzip2-1.0.6"),
    "libcap-": ("libcap", "BSD-3-Clause OR GPL-2.0-only"),
    "libdw-": ("elfutils-libdw", "LGPL-3.0-or-later OR GPL-2.0-or-later"),
    "libelf-": ("elfutils-libelf", "LGPL-3.0-or-later OR GPL-2.0-or-later"),
    "liblzma-": ("XZ-Utils-liblzma", "public-domain"),
    "libudev-": ("systemd-libudev", "LGPL-2.1-or-later"),
    "libusb-": ("libusb", "LGPL-2.1-or-later"),
}
FORBIDDEN_QT_PAYLOADS = (
    "Qt6VirtualKeyboard", "qtvirtualkeyboard", "Qt6Pdf", "libqpdf", "Qt6Qml", "Qt6Quick",
)
NATIVE_TOC_KINDS = {"BINARY", "EXTENSION", "EXECUTABLE"}
APP_ID = "io.github.dev_zetta.MC7Studio"
APP_NAME = "MC7-Studio"
ARCHITECTURE = "x86_64"
MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024
MAX_DEBIAN_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_DEBIAN_PAYLOAD_BYTES = 512 * 1024 * 1024
MAX_DEBIAN_COPYRIGHT_BYTES = 4 * 1024 * 1024
_GLIBC_VERSION = re.compile(r"\bGLIBC_(\d+)\.(\d+)\b")


class AppImageError(RuntimeError):
    """An AppImage build input or output failed validation."""


class DebianPackageNotOwnedError(AppImageError):
    """An installed Debian package does not own a candidate system library."""


class DebianPackageArchive(NamedTuple):
    """Verified metadata and filesystem records from one Debian package archive."""

    path: Path
    sha256: str
    package: str
    version: str
    architecture: str
    file_sha256: dict[PurePosixPath, str]
    links: dict[PurePosixPath, PurePosixPath]
    copyright: bytes


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_hash(path: Path, expected: str, label: str) -> None:
    actual = _sha256(path)
    if actual != expected:
        raise AppImageError(f"{label} SHA-256 mismatch: expected {expected}, got {actual}")


def _download(url: str, expected: str, destination: Path, label: str) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        _verify_hash(destination, expected, label)
        return destination
    if destination.exists():
        raise AppImageError(f"{label} cache path is not a regular file: {destination}")
    partial = destination.with_name(destination.name + ".part")
    partial.unlink(missing_ok=True)
    request = Request(url, headers={"User-Agent": "MC7-Studio-AppImage-builder/0.1"})
    try:
        with urlopen(request, timeout=60) as response, partial.open("xb") as output:
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise AppImageError(f"{label} exceeds the download size limit")
                output.write(chunk)
        _verify_hash(partial, expected, label)
        os.replace(partial, destination)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    return destination


def _pinned_file(override: Path | None, *, url: str, sha256: str, name: str) -> Path:
    if override is None:
        path = _download(url, sha256, TOOLS_ROOT / name, name)
    else:
        path = override.expanduser().resolve()
        if not path.is_file() or path.is_symlink():
            raise AppImageError(f"Pinned input is not a regular file: {path}")
        _verify_hash(path, sha256, name)
    return path


def _project_version() -> str:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as handle:
        value = tomllib.load(handle).get("project", {}).get("version")
    if not isinstance(value, str) or re.fullmatch(r"[0-9]+(?:\.[0-9]+){2}", value) is None:
        raise AppImageError("pyproject.toml has no supported project version")
    return value


def _source_date_epoch() -> str:
    configured = os.environ.get("SOURCE_DATE_EPOCH")
    if configured is not None:
        if not configured.isdecimal() or int(configured) < 315_532_800:
            raise AppImageError("SOURCE_DATE_EPOCH must be an integer at or after 1980-01-01")
        return configured
    result = subprocess.run(
        ["git", "log", "-1", "--format=%ct"], cwd=PROJECT_ROOT,
        check=False, capture_output=True, text=True,
    )
    value = result.stdout.strip()
    if result.returncode or not value.isdecimal():
        raise AppImageError("Set SOURCE_DATE_EPOCH when git commit time is unavailable")
    return value


def _require_build_platform() -> None:
    machine = platform.machine().lower()
    if sys.platform != "linux" or machine not in {"x86_64", "amd64"}:
        raise AppImageError("This release builder supports Linux x86_64 only")
    if sys.version_info < (3, 11):
        raise AppImageError("The AppImage release builder needs Python 3.11 or newer")


def _require_python_packages() -> None:
    mismatches = []
    for package, expected in PINNED_PYTHON_PACKAGES.items():
        try:
            actual = metadata.version(package)
        except metadata.PackageNotFoundError:
            actual = "missing"
        if actual != expected:
            mismatches.append(f"{package}=={expected} (found {actual})")
    if mismatches:
        raise AppImageError("Install the pinned build packages: " + ", ".join(mismatches))


def _reset_build_root() -> None:
    BUILD_ROOT.parent.mkdir(parents=True, exist_ok=True)
    if BUILD_ROOT.exists():
        if BUILD_ROOT.is_symlink() or BUILD_ROOT.resolve().parent != LOCAL_TMP_ROOT.resolve():
            raise AppImageError(f"Refusing to clear unsafe build path: {BUILD_ROOT}")
        shutil.rmtree(BUILD_ROOT)
    BUILD_ROOT.mkdir(mode=0o755)


def _extract_seven_zip(archive: Path) -> tuple[Path, Path]:
    destination = TOOLS_ROOT / f"7zip-{SEVEN_ZIP_VERSION}"
    executable = destination / "7zz"
    license_path = destination / "License.txt"
    if destination.exists():
        if destination.is_symlink() or destination.resolve().parent != TOOLS_ROOT.resolve():
            raise AppImageError(f"Unsafe 7-Zip tool path: {destination}")
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    wanted = {"7zz": executable, "License.txt": license_path}
    found: set[str] = set()
    try:
        with tarfile.open(archive, "r:xz") as package:
            for member in package.getmembers():
                name = member.name.removeprefix("./")
                target = wanted.get(name)
                if target is None:
                    continue
                if not member.isfile() or member.size > 8 * 1024 * 1024:
                    raise AppImageError(f"Unsafe 7-Zip archive member: {member.name}")
                source = package.extractfile(member)
                if source is None:
                    raise AppImageError(f"Cannot read 7-Zip archive member: {member.name}")
                with source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
                found.add(name)
    except (OSError, tarfile.TarError) as error:
        raise AppImageError("Could not inspect the pinned 7-Zip archive") from error
    if found != set(wanted):
        raise AppImageError("Pinned 7-Zip archive does not contain 7zz and License.txt")
    executable.chmod(0o755)
    license_path.chmod(0o644)
    return executable, license_path


def _find_libusb(override: Path | None) -> Path:
    if override is not None:
        candidates = [override.expanduser()]
    else:
        candidates = [
            Path("/usr/lib/x86_64-linux-gnu/libusb-1.0.so.0"),
            Path("/lib/x86_64-linux-gnu/libusb-1.0.so.0"),
            Path("/usr/lib64/libusb-1.0.so.0"),
            Path("/lib64/libusb-1.0.so.0"),
        ]
        located = ctypes.util.find_library("usb-1.0")
        if located and "/" in located:
            candidates.insert(0, Path(located))
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file() and resolved.stat().st_size > 0:
            return candidate.absolute()
    raise AppImageError("Pass --libusb with the x86_64 libusb-1.0.so.0 build input")


def _copy_file(source: Path, destination: Path, mode: int = 0o644) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(mode)


def _copy_distribution_licenses(package: str, destination: Path) -> list[str]:
    distribution = metadata.distribution(package)
    copied: list[str] = []
    for relative in distribution.files or ():
        parts = tuple(part.lower() for part in relative.parts)
        basename = relative.name.lower()
        if not (
            basename.startswith(("license", "copying", "notice"))
            or "licenses" in parts
        ):
            continue
        source = Path(distribution.locate_file(relative))
        if not source.is_file():
            continue
        safe_name = re.sub(r"[^A-Za-z0-9._+-]", "-", relative.name)
        target = destination / f"{package}-{len(copied) + 1}-{safe_name}"
        _copy_file(source, target)
        copied.append(target.name)
    return copied


def _copy_first(candidates: list[Path], destination: Path, label: str) -> Path:
    for source in candidates:
        if source.is_file():
            _copy_file(source, destination)
            return source
    raise AppImageError(f"Could not locate the {label} license notice")


def _install_licenses(appdir: Path, seven_zip_license: Path) -> dict[str, list[str]]:
    destination = appdir / "usr" / "share" / "licenses" / "mc7-studio"
    destination.mkdir(parents=True)
    _copy_file(PROJECT_ROOT / "LICENSE", destination / "MC7-Studio-LICENSE.txt")
    _copy_file(PACKAGING_ROOT / "THIRD_PARTY_NOTICES.txt", destination / "THIRD_PARTY_NOTICES.txt")
    for name in STATIC_LICENSE_FILES:
        _copy_file(PACKAGING_ROOT / "licenses" / name, destination / name)
    _copy_file(seven_zip_license, destination / "7-Zip-License.txt")

    package_licenses: dict[str, list[str]] = {}
    for package in ("PyInstaller", "certifi", "hidapi", "psutil", "websocket-client"):
        copied = _copy_distribution_licenses(package, destination)
        if not copied:
            raise AppImageError(f"The installed {package} package has no license file")
        package_licenses[package] = copied

    python_version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    _copy_first(
        [
            Path(sys.base_prefix) / "LICENSE.txt",
            Path(sys.base_prefix) / "LICENSE",
            Path(sys.base_prefix) / "lib" / python_version / "LICENSE.txt",
        ],
        destination / "Python-COPYRIGHT.txt",
        "license shipped with this Python runtime",
    )
    package_licenses["Python"] = ["Python-COPYRIGHT.txt"]
    return package_licenses


def _distribution_file_index() -> dict[Path, tuple[str, str, str]]:
    index: dict[Path, tuple[str, str, str]] = {}
    for package in PYTHON_NATIVE_DISTRIBUTIONS:
        distribution = metadata.distribution(package)
        canonical_name = distribution.metadata.get("Name") or package
        for relative in distribution.files or ():
            source = Path(distribution.locate_file(relative))
            if not source.is_file():
                continue
            try:
                with source.open("rb") as handle:
                    if handle.read(4) != b"\x7fELF":
                        continue
            except OSError as error:
                raise AppImageError(f"Could not inspect Python distribution file: {source}") from error
            resolved = source.resolve()
            value = (canonical_name, distribution.version, relative.as_posix())
            previous = index.get(resolved)
            if previous is not None and previous != value:
                raise AppImageError(
                    f"Python distributions claim the same native source: {resolved}"
                )
            index[resolved] = value
    return index


def _read_native_toc(path: Path) -> list[tuple[str, Path, str]]:
    if not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise AppImageError(f"Missing or oversized PyInstaller TOC: {path}")
    try:
        value = ast.literal_eval(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError) as error:
        raise AppImageError(f"Could not parse PyInstaller TOC: {path}") from error
    if not isinstance(value, tuple) or len(value) != 1 or not isinstance(value[0], list):
        raise AppImageError(f"Unexpected PyInstaller TOC structure: {path}")

    records: list[tuple[str, Path, str]] = []
    destinations: set[str] = set()
    for record in value[0]:
        if not isinstance(record, tuple) or len(record) != 3:
            raise AppImageError(f"Invalid PyInstaller TOC record: {record!r}")
        destination, source, kind = record
        if kind not in NATIVE_TOC_KINDS:
            continue
        if not all(isinstance(item, str) for item in record):
            raise AppImageError(f"Invalid native PyInstaller TOC record: {record!r}")
        relative = PurePosixPath(destination)
        if relative.is_absolute() or ".." in relative.parts or destination in destinations:
            raise AppImageError(f"Unsafe or duplicate PyInstaller destination: {destination}")
        source_path = Path(source)
        if not source_path.is_absolute() or not source_path.is_file():
            raise AppImageError(f"Missing native PyInstaller source: {source}")
        destinations.add(destination)
        records.append((destination, source_path, kind))
    if not records:
        raise AppImageError("PyInstaller TOC contains no native files")
    return records


def _debian_archive_field(executable: str, archive: Path, field: str) -> str:
    result = subprocess.run(
        [executable, "-f", str(archive), field],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    value = result.stdout.rstrip("\n")
    if result.returncode or not value or any(character in value for character in "\r\n\t\0"):
        detail = result.stderr.strip()
        suffix = f": {detail}" if detail else ""
        raise AppImageError(f"Could not read Debian package {field}{suffix}")
    return value


def _normalise_debian_payload_path(
    value: str,
    *,
    base: PurePosixPath = PurePosixPath(),
    allow_parents: bool = False,
) -> PurePosixPath:
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or "\0" in value:
        raise AppImageError(f"Unsafe Debian package payload path: {value!r}")
    parts = list(base.parts)
    for part in candidate.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not allow_parents or not parts:
                raise AppImageError(f"Unsafe Debian package payload path: {value!r}")
            parts.pop()
            continue
        parts.append(part)
    if not parts:
        raise AppImageError(f"Empty Debian package payload path: {value!r}")
    return PurePosixPath(*parts)


def _debian_payload_tar(executable: str, archive: Path) -> Path:
    temporary_root = LOCAL_TMP_ROOT / "temp"
    temporary_root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix="debian-payload-", suffix=".tar", dir=temporary_root, delete=False,
    ) as output, tempfile.NamedTemporaryFile(
        prefix="dpkg-deb-", suffix=".stderr", dir=temporary_root, delete=False,
    ) as error_output:
        output_path = Path(output.name)
        error_path = Path(error_output.name)
        process = subprocess.Popen(
            [executable, "--fsys-tarfile", str(archive)],
            stdout=subprocess.PIPE,
            stderr=error_output,
        )
        try:
            if process.stdout is None:
                raise AppImageError("dpkg-deb did not expose the package payload")
            total = 0
            with process.stdout:
                while chunk := process.stdout.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_DEBIAN_PAYLOAD_BYTES:
                        process.kill()
                        raise AppImageError("Debian package payload exceeds the size limit")
                    output.write(chunk)
            result = process.wait(timeout=30)
        except Exception:
            process.kill()
            process.wait()
            output_path.unlink(missing_ok=True)
            error_path.unlink(missing_ok=True)
            raise
    try:
        detail = error_path.read_text(encoding="utf-8", errors="replace").strip()
    finally:
        error_path.unlink(missing_ok=True)
    if result:
        output_path.unlink(missing_ok=True)
        suffix = f": {detail}" if detail else ""
        raise AppImageError(f"Could not read Debian package payload{suffix}")
    return output_path


def _inspect_debian_package_archive(
    archive: Path,
    *,
    dpkg_deb: str | None = None,
) -> DebianPackageArchive:
    path = archive.expanduser().absolute()
    if path.is_symlink() or not path.is_file():
        raise AppImageError(f"Debian package archive is not a regular file: {path}")
    if path.stat().st_size > MAX_DEBIAN_ARCHIVE_BYTES:
        raise AppImageError(f"Debian package archive exceeds the size limit: {path}")
    executable = dpkg_deb or shutil.which("dpkg-deb")
    if executable is None:
        raise AppImageError(f"dpkg-deb is required to inspect package archive: {path}")
    archive_sha256 = _sha256(path)
    package = _debian_archive_field(executable, path, "Package")
    version = _debian_archive_field(executable, path, "Version")
    architecture = _debian_archive_field(executable, path, "Architecture")
    if re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", package) is None:
        raise AppImageError(f"Invalid Debian binary package name: {package!r}")
    if len(version) > 256 or re.fullmatch(r"[^\x00-\x20\x7f]+", version) is None:
        raise AppImageError(f"Invalid Debian package version: {version!r}")
    if re.fullmatch(r"[a-z0-9][a-z0-9-]*", architecture) is None:
        raise AppImageError(f"Invalid Debian package architecture: {architecture!r}")

    payload_tar = _debian_payload_tar(executable, path)
    file_sha256: dict[PurePosixPath, str] = {}
    links: dict[PurePosixPath, PurePosixPath] = {}
    members: set[PurePosixPath] = set()
    copyright_path = PurePosixPath("usr", "share", "doc", package, "copyright")
    copyright = b""
    declared_size = 0
    try:
        with tarfile.open(payload_tar, "r:") as payload:
            for member in payload:
                if member.isdir() and member.name.rstrip("/") in {"", "."}:
                    continue
                member_path = _normalise_debian_payload_path(member.name)
                if member_path in members:
                    raise AppImageError(
                        f"Duplicate Debian package payload path: {member_path}"
                    )
                members.add(member_path)
                if member.isdir():
                    continue
                if member.issym() or member.islnk():
                    base = member_path.parent if member.issym() else PurePosixPath()
                    links[member_path] = _normalise_debian_payload_path(
                        member.linkname, base=base, allow_parents=True,
                    )
                    continue
                if not member.isfile():
                    raise AppImageError(
                        f"Unsupported Debian package payload type: {member_path}"
                    )
                declared_size += member.size
                if declared_size > MAX_DEBIAN_PAYLOAD_BYTES:
                    raise AppImageError("Debian package payload exceeds the size limit")
                source = payload.extractfile(member)
                if source is None:
                    raise AppImageError(
                        f"Could not read Debian package payload file: {member_path}"
                    )
                digest = hashlib.sha256()
                captured = bytearray()
                with source:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                        if member_path == copyright_path:
                            if len(captured) + len(chunk) > MAX_DEBIAN_COPYRIGHT_BYTES:
                                raise AppImageError(
                                    f"Debian copyright file exceeds the size limit: {package}"
                                )
                            captured.extend(chunk)
                file_sha256[member_path] = digest.hexdigest()
                if member_path == copyright_path:
                    copyright = bytes(captured)
    except (OSError, tarfile.TarError) as error:
        raise AppImageError(f"Could not inspect Debian package payload: {path}") from error
    finally:
        payload_tar.unlink(missing_ok=True)
    if _sha256(path) != archive_sha256:
        raise AppImageError(f"Debian package archive changed while being inspected: {path}")
    if not copyright:
        raise AppImageError(f"Debian copyright file is missing or empty for {package}")
    return DebianPackageArchive(
        path=path,
        sha256=archive_sha256,
        package=package,
        version=version,
        architecture=architecture,
        file_sha256=file_sha256,
        links=links,
        copyright=copyright,
    )


def _resolve_debian_payload_hash(
    archive: DebianPackageArchive, path: PurePosixPath,
) -> str | None:
    seen: set[PurePosixPath] = set()
    current = path
    while current in archive.links:
        if current in seen:
            raise AppImageError(
                f"Debian package contains a payload link cycle: {archive.path}"
            )
        seen.add(current)
        current = archive.links[current]
    return archive.file_sha256.get(current)


def _match_debian_package_archive(
    source: Path, archive: DebianPackageArchive,
) -> PurePosixPath | None:
    source_hash = _sha256(source)
    candidates = [
        path for path in (*archive.file_sha256, *archive.links)
        if path.name == source.name
        and _resolve_debian_payload_hash(archive, path) == source_hash
    ]
    if not candidates:
        return None
    source_parts = source.absolute().parts
    suffix_matches = [
        path for path in candidates
        if len(path.parts) <= len(source_parts)
        and tuple(source_parts[-len(path.parts):]) == path.parts
    ]
    selected = suffix_matches or candidates
    if len(selected) != 1:
        names = ", ".join(path.as_posix() for path in sorted(selected))
        raise AppImageError(
            f"Debian package payload match is ambiguous for {source}: {names}"
        )
    return selected[0]


def _debian_archive_provenance(
    source: Path,
    license_directory: Path,
    archives: list[DebianPackageArchive],
) -> dict[str, object]:
    matches = [
        (archive, payload_path)
        for archive in archives
        if (payload_path := _match_debian_package_archive(source, archive)) is not None
    ]
    if not matches:
        raise DebianPackageNotOwnedError(
            f"No installed Debian package or supplied package archive owns bundled "
            f"system library: {source}"
        )
    if len(matches) != 1:
        names = ", ".join(str(archive.path) for archive, _path in matches)
        raise AppImageError(f"Multiple Debian package archives match {source}: {names}")
    archive, payload_path = matches[0]
    binary_package = f"{archive.package}:{archive.architecture}"
    safe_package = re.sub(r"[^A-Za-z0-9._+-]", "-", binary_package)
    safe_version = re.sub(r"[^A-Za-z0-9._+-]", "-", archive.version)
    relative_notice = (
        Path("debian-archives")
        / f"{safe_package}_{safe_version}_{archive.sha256[:12]}.copyright"
    )
    destination = license_directory / relative_notice
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.read_bytes() != archive.copyright:
            raise AppImageError(f"Conflicting Debian archive copyright: {destination}")
    else:
        destination.write_bytes(archive.copyright)
        destination.chmod(0o644)
    return {
        "kind": "debian-package-archive",
        "name": binary_package,
        "version": archive.version,
        "source_path": str(source.absolute()),
        "package_payload_path": payload_path.as_posix(),
        "package_archive": archive.path.name,
        "package_archive_sha256": archive.sha256,
        "license_files": [relative_notice.as_posix()],
    }


def _debian_package_provenance(
    source: Path,
    license_directory: Path,
    *,
    documentation_root: Path = Path("/usr/share/doc"),
    dpkg_query: str | None = None,
) -> dict[str, object]:
    executable = dpkg_query or shutil.which("dpkg-query")
    if executable is None:
        raise AppImageError(f"dpkg-query is required to attribute system library: {source}")

    owner = ""
    for candidate in dict.fromkeys((str(source), str(source.resolve()))):
        result = subprocess.run(
            [executable, "-S", candidate], check=False, capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            owner_field = result.stdout.splitlines()[0].rsplit(": ", 1)[0]
            owner = owner_field.split(", ", 1)[0]
            break
    if not owner:
        raise DebianPackageNotOwnedError(
            f"No Debian package owns bundled system library: {source}"
        )

    result = subprocess.run(
        [executable, "-W", "-f=${binary:Package}\t${Version}\n", owner],
        check=False, capture_output=True, text=True,
    )
    fields = result.stdout.strip().split("\t")
    if result.returncode or len(fields) != 2 or not all(fields):
        raise AppImageError(f"Could not query Debian package version for {owner}")
    binary_package, version = fields
    document_package = binary_package.split(":", 1)[0]
    copyright_source = documentation_root / document_package / "copyright"
    if not copyright_source.is_file():
        raise AppImageError(f"Debian copyright file is missing for {binary_package}")
    safe_package = re.sub(r"[^A-Za-z0-9._+-]", "-", binary_package)
    relative_notice = Path("debian") / f"{safe_package}.copyright"
    destination = license_directory / relative_notice
    if not destination.exists():
        _copy_file(copyright_source, destination)
    return {
        "kind": "debian-package",
        "name": binary_package,
        "version": version,
        "source_path": str(source.resolve()),
        "license_files": [relative_notice.as_posix()],
    }


def _debian_system_provenance(
    source: Path,
    license_directory: Path,
    archives: list[DebianPackageArchive],
) -> dict[str, object]:
    try:
        return _debian_package_provenance(source, license_directory)
    except DebianPackageNotOwnedError:
        if not archives:
            raise
        return _debian_archive_provenance(source, license_directory, archives)


def _hidapi_wheel_component(relative: str) -> tuple[str, str] | None:
    path = PurePosixPath(relative)
    if len(path.parts) < 2 or path.parts[0] != "hidapi.libs":
        return None
    for prefix, component in HIDAPI_WHEEL_LIBRARIES.items():
        if path.name.startswith(prefix):
            return component
    raise AppImageError(f"Unattributed library in the hidapi wheel: {relative}")


def _python_provenance(
    source: Path,
    distribution: tuple[str, str, str],
    package_licenses: dict[str, list[str]],
) -> dict[str, object]:
    package, version, relative = distribution
    hidapi_component = _hidapi_wheel_component(relative)
    if hidapi_component is not None:
        component, license_expression = hidapi_component
        return {
            "kind": "python-wheel-vendored-library",
            "name": component,
            "version": "recorded by hidapi wheel filename",
            "python_distribution": package,
            "python_distribution_version": version,
            "source_path": relative,
            "license_expression": license_expression,
            "license_files": ["HIDAPI-WHEEL-THIRD-PARTY-NOTICES.txt"],
        }
    if package.startswith("PySide6") and source.name.startswith("libicu"):
        return {
            "kind": "python-wheel-vendored-library",
            "name": "ICU",
            "version": "73.2",
            "python_distribution": package,
            "python_distribution_version": version,
            "source_path": relative,
            "license_expression": "Unicode-DFS-2016",
            "license_files": ["ICU-73.2-LICENSE.txt"],
        }
    if package.startswith("PySide6"):
        if any(name in relative for name in FORBIDDEN_QT_PAYLOADS):
            raise AppImageError(f"Forbidden unused Qt payload was bundled: {relative}")
        component = "Qt" if relative.startswith("PySide6/Qt/") else "PySide6"
        return {
            "kind": "python-wheel-component",
            "name": component,
            "version": version,
            "python_distribution": package,
            "source_path": relative,
            "license_expression": "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only",
            "license_files": [
                "QT-PYSIDE-SHIBOKEN-NOTICE.txt",
                "PySide6-bufferprocs_py37.h",
                "PySide6-PSF-3.7.0.txt",
                "LGPL-3.0.txt",
                "GPL-2.0.txt",
                "GPL-3.0.txt",
            ],
        }
    if package == "shiboken6":
        return {
            "kind": "python-wheel-component",
            "name": "Shiboken6",
            "version": version,
            "python_distribution": package,
            "source_path": relative,
            "license_expression": "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only",
            "license_files": [
                "QT-PYSIDE-SHIBOKEN-NOTICE.txt",
                "PySide6-bufferprocs_py37.h",
                "PySide6-PSF-3.7.0.txt",
                "LGPL-3.0.txt",
                "GPL-2.0.txt",
                "GPL-3.0.txt",
            ],
        }
    notices = package_licenses.get(package)
    if not notices:
        raise AppImageError(f"No license files were installed for Python package: {package}")
    return {
        "kind": "python-distribution",
        "name": package,
        "version": version,
        "source_path": relative,
        "license_files": notices,
    }


def _write_bundled_component_manifest(
    appdir: Path,
    frozen: Path,
    toc: Path,
    package_licenses: dict[str, list[str]],
    staged_sources: dict[Path, Path],
    debian_package_archives: list[DebianPackageArchive] | None = None,
) -> Path:
    license_directory = appdir / "usr" / "share" / "licenses" / "mc7-studio"
    for path in frozen.rglob("*"):
        relative = path.relative_to(frozen).as_posix()
        if relative.startswith("PySide6/Qt/qml/") or any(
            name in relative for name in FORBIDDEN_QT_PAYLOADS
        ):
            raise AppImageError(f"Forbidden unused Qt payload was bundled: {relative}")
    distribution_index = _distribution_file_index()
    staged_index = {source.resolve(): original for source, original in staged_sources.items()}
    package_cache: dict[Path, dict[str, object]] = {}
    archive_fallbacks = debian_package_archives or []
    files: list[dict[str, object]] = []
    components: dict[tuple[str, str, str], dict[str, object]] = {}

    for destination, source, kind in _read_native_toc(toc):
        bundled_relative = Path(destination) if kind == "EXECUTABLE" else Path("_internal") / destination
        bundled = frozen / bundled_relative
        if not bundled.is_file():
            raise AppImageError(f"Native PyInstaller output is missing: {destination}")
        resolved = source.resolve()
        original = staged_index.get(resolved)
        attribution_source = original if original is not None else source

        if kind == "EXECUTABLE" and destination == "mc7-studio":
            provenance: dict[str, object] = {
                "kind": "build-tool-runtime",
                "name": "PyInstaller",
                "version": metadata.version("PyInstaller"),
                "source_path": "generated PyInstaller bootloader executable",
                "license_files": package_licenses["PyInstaller"],
            }
        elif resolved in distribution_index:
            provenance = _python_provenance(
                source, distribution_index[resolved], package_licenses,
            )
        elif resolved.is_relative_to(Path(sys.base_prefix).resolve()):
            provenance = {
                "kind": "python-runtime",
                "name": platform.python_implementation(),
                "version": platform.python_version(),
                "source_path": resolved.relative_to(Path(sys.base_prefix).resolve()).as_posix(),
                "license_files": package_licenses["Python"],
            }
        else:
            system_source = attribution_source.absolute()
            if system_source not in package_cache:
                package_cache[system_source] = _debian_system_provenance(
                    attribution_source, license_directory, archive_fallbacks,
                )
            provenance = package_cache[system_source].copy()

        source_hash = _sha256(source)
        bundled_hash = _sha256(bundled)
        component_key = (
            str(provenance["kind"]), str(provenance["name"]), str(provenance["version"])
        )
        component = components.setdefault(
            component_key,
            {
                "kind": provenance["kind"],
                "name": provenance["name"],
                "version": provenance["version"],
                "license_files": provenance["license_files"],
            },
        )
        if component["license_files"] != provenance["license_files"]:
            raise AppImageError(f"Conflicting license mapping for {component_key[1]}")
        files.append({
            "path": (Path("usr/lib/mc7-studio") / bundled_relative).as_posix(),
            "type": kind,
            "sha256": bundled_hash,
            "source_sha256": source_hash,
            "provenance": provenance,
        })

    embedded_components = [
        {
            "kind": "python-runtime",
            "name": platform.python_implementation(),
            "version": platform.python_version(),
            "license_files": package_licenses["Python"],
        },
        *(
            {
                "kind": "python-distribution",
                "name": package,
                "version": metadata.version(package),
                "license_files": package_licenses[package],
            }
            for package in ("certifi", "hidapi", "psutil", "websocket-client")
        ),
        {
            "kind": "build-tool-runtime",
            "name": "PyInstaller",
            "version": metadata.version("PyInstaller"),
            "license_files": package_licenses["PyInstaller"],
        },
        {
            "kind": "bundled-tool",
            "name": "7-Zip",
            "version": SEVEN_ZIP_VERSION,
            "license_files": ["7-Zip-License.txt"],
        },
        {
            "kind": "appimage-runtime",
            "name": "AppImage type-2 runtime",
            "version": RUNTIME_VERSION,
            "sha256": RUNTIME_SHA256,
            "license_files": ["GPL-2.0.txt"],
        },
    ]
    for component in embedded_components:
        key = (str(component["kind"]), str(component["name"]), str(component["version"]))
        components.setdefault(key, component)

    for component in components.values():
        notices = component.get("license_files")
        if not isinstance(notices, list) or not notices:
            raise AppImageError(f"Bundled component has no license mapping: {component['name']}")
        for relative in notices:
            path = PurePosixPath(str(relative))
            if path.is_absolute() or ".." in path.parts or not (license_directory / path).is_file():
                raise AppImageError(
                    f"Bundled component license file is missing: {component['name']} -> {relative}"
                )

    manifest = {
        "schema_version": 1,
        "application": APP_NAME,
        "application_version": _project_version(),
        "generated_from": "PyInstaller COLLECT-00.toc",
        "python_build_distributions": [
            {
                "name": metadata.distribution(package).metadata.get("Name") or package,
                "version": metadata.version(package),
            }
            for package in PINNED_PYTHON_PACKAGES
        ],
        "components": sorted(
            components.values(), key=lambda item: (str(item["kind"]), str(item["name"]))
        ),
        "native_files": sorted(files, key=lambda item: str(item["path"])),
    }
    destination = license_directory / "BUNDLED-COMPONENTS.json"
    destination.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    print(
        f"Attributed {len(files)} PyInstaller native files to "
        f"{len(components)} bundled components"
    )
    return destination


def _environment(epoch: str, libusb: Path) -> dict[str, str]:
    temporary = LOCAL_TMP_ROOT / "temp"
    pyinstaller_config = LOCAL_TMP_ROOT / "pyinstaller-config"
    temporary.mkdir(parents=True, exist_ok=True)
    pyinstaller_config.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.update({
        "APPIMAGE_EXTRACT_AND_RUN": "1",
        "ARCH": ARCHITECTURE,
        "MC7_APPIMAGE_LIBUSB": str(libusb),
        "PYINSTALLER_CONFIG_DIR": str(pyinstaller_config),
        "PYTHONHASHSEED": "0",
        "SOURCE_DATE_EPOCH": epoch,
        "TMPDIR": str(temporary),
    })
    return environment


def _run(argv: list[str], *, environment: dict[str, str], cwd: Path = PROJECT_ROOT) -> None:
    print("+", shlex.join(argv), flush=True)
    result = subprocess.run(argv, cwd=cwd, env=environment, check=False)
    if result.returncode:
        raise AppImageError(f"Command exited with {result.returncode}: {argv[0]}")


def _run_pyinstaller(argv: list[str], environment: dict[str, str]) -> None:
    print("+", shlex.join(argv), flush=True)
    result = subprocess.run(
        argv, cwd=PROJECT_ROOT, env=environment, check=False,
        capture_output=True, text=True,
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if result.returncode:
        raise AppImageError(f"PyInstaller exited with {result.returncode}")
    missing = [
        line.strip() for line in (result.stdout + "\n" + result.stderr).splitlines()
        if "Library not found:" in line
    ]
    if missing:
        raise AppImageError("PyInstaller could not resolve a shared library: " + "; ".join(missing))


def _json_lines(output: str, label: str) -> list[dict[str, object]]:
    values = []
    for line in output.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise AppImageError(f"{label} produced non-JSON output: {line!r}") from error
        if not isinstance(value, dict):
            raise AppImageError(f"{label} produced a non-object JSON value")
        values.append(value)
    if not values:
        raise AppImageError(f"{label} produced no structured output")
    return values


def _verify_frozen_appdir(appdir: Path, environment: dict[str, str]) -> None:
    launcher = appdir / "AppRun"
    result = subprocess.run(
        [str(launcher), "--swarm2-appimage-self-test"], cwd=PROJECT_ROOT,
        env=environment, check=False, capture_output=True, text=True, timeout=30,
    )
    values = _json_lines(result.stdout, "Frozen AppImage self-test")
    if result.returncode or values[-1].get("result") != "ok":
        raise AppImageError(
            f"Frozen AppImage self-test failed with {result.returncode}: {values[-1]}"
        )

    helper_shapes = {
        "swarm2.hardware": "error",
        "swarm2.firmware_hardware": "error",
        "swarm2.restore_hardware": "error",
        "swarm2.dcu": "type",
        "swarm2.countdown": "type",
    }
    for module, required_key in helper_shapes.items():
        result = subprocess.run(
            [str(launcher), "--swarm2-helper", module], cwd=PROJECT_ROOT,
            env=environment, input="{}\n", check=False, capture_output=True,
            text=True, timeout=10,
        )
        values = _json_lines(result.stdout, module)
        if result.returncode != 2 or required_key not in values[-1]:
            raise AppImageError(
                f"{module} did not return its structured error path: "
                f"exit {result.returncode}, output {values[-1]}"
            )


def _parse_glibc_version(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)", value)
    if match is None:
        raise AppImageError("The maximum glibc version must look like 2.35")
    return int(match.group(1)), int(match.group(2))


def _verify_glibc_baseline(appdir: Path, maximum: str) -> None:
    limit = _parse_glibc_version(maximum)
    readelf = shutil.which("readelf")
    if readelf is None:
        raise AppImageError("Install readelf from binutils to verify the glibc baseline")
    scanned = 0
    incompatible: list[tuple[Path, tuple[int, int]]] = []
    for path in sorted(appdir.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            with path.open("rb") as handle:
                if handle.read(4) != b"\x7fELF":
                    continue
        except OSError as error:
            raise AppImageError(f"Could not inspect AppDir file: {path}") from error
        scanned += 1
        result = subprocess.run(
            [readelf, "--version-info", "--wide", str(path)],
            check=False, capture_output=True, text=True, timeout=15,
        )
        if result.returncode:
            raise AppImageError(
                f"readelf could not inspect {path.relative_to(appdir)}: "
                f"{result.stderr.strip()}"
            )
        versions = {
            (int(major), int(minor))
            for major, minor in _GLIBC_VERSION.findall(result.stdout)
        }
        required = max(versions, default=(0, 0))
        if required > limit:
            incompatible.append((path.relative_to(appdir), required))
    if not scanned:
        raise AppImageError("The AppDir contains no ELF files to verify")
    if incompatible:
        details = ", ".join(
            f"{path} needs GLIBC_{version[0]}.{version[1]}"
            for path, version in incompatible[:8]
        )
        if len(incompatible) > 8:
            details += f", and {len(incompatible) - 8} more"
        raise AppImageError(
            f"AppImage exceeds the GLIBC_{limit[0]}.{limit[1]} release baseline: {details}"
        )
    print(
        f"Verified {scanned} AppDir ELF files require no newer than "
        f"GLIBC_{limit[0]}.{limit[1]}"
    )


def _build_appdir(
    environment: dict[str, str],
    seven_zip: Path,
    seven_zip_license: Path,
    version: str,
    libusb_source: Path,
    debian_package_archives: list[DebianPackageArchive],
) -> Path:
    pyinstaller_dist = BUILD_ROOT / "pyinstaller-dist"
    _run_pyinstaller(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--clean",
            "--noconfirm",
            "--distpath",
            str(pyinstaller_dist),
            "--workpath",
            str(BUILD_ROOT / "pyinstaller-work"),
            str(PACKAGING_ROOT / "mc7-studio.spec"),
        ],
        environment,
    )
    frozen = pyinstaller_dist / "mc7-studio"
    executable = frozen / "mc7-studio"
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise AppImageError("PyInstaller did not create the MC7 Studio executable")
    appdir = BUILD_ROOT / "MC7-Studio.AppDir"
    library_root = appdir / "usr" / "lib" / "mc7-studio"
    shutil.copytree(frozen, library_root, symlinks=True)

    _copy_file(PACKAGING_ROOT / "AppRun", appdir / "AppRun", 0o755)
    _copy_file(PACKAGING_ROOT / "mc7-studio.desktop", appdir / "mc7-studio.desktop")
    _copy_file(PACKAGING_ROOT / "mc7-studio.svg", appdir / "mc7-studio.svg")
    (appdir / ".DirIcon").symlink_to("mc7-studio.svg")
    _copy_file(
        PACKAGING_ROOT / "mc7-studio.desktop",
        appdir / "usr" / "share" / "applications" / "mc7-studio.desktop",
    )
    _copy_file(
        PACKAGING_ROOT / "mc7-studio.svg",
        appdir / "usr" / "share" / "icons" / "hicolor" / "scalable" / "apps"
        / "mc7-studio.svg",
    )
    _copy_file(
        PACKAGING_ROOT / f"{APP_ID}.metainfo.xml",
        appdir / "usr" / "share" / "metainfo" / f"{APP_ID}.metainfo.xml",
    )
    bin_directory = appdir / "usr" / "bin"
    bin_directory.mkdir(parents=True)
    (bin_directory / "mc7-studio").symlink_to("../../AppRun")
    _copy_file(seven_zip, bin_directory / "7zz", 0o755)

    import certifi

    certificate = Path(certifi.where())
    if not certificate.is_file() or certificate.stat().st_size < 100_000:
        raise AppImageError("certifi did not provide a usable CA certificate bundle")
    _copy_file(
        certificate,
        appdir / "usr" / "share" / "mc7-studio" / "cacert.pem",
    )
    package_licenses = _install_licenses(appdir, seven_zip_license)
    _write_bundled_component_manifest(
        appdir,
        library_root,
        BUILD_ROOT / "pyinstaller-work" / "mc7-studio" / "COLLECT-00.toc",
        package_licenses,
        {Path(environment["MC7_APPIMAGE_LIBUSB"]): libusb_source},
        debian_package_archives,
    )

    bundled_libusb = list(library_root.rglob("libusb-1.0.so.0"))
    if len(bundled_libusb) != 1:
        raise AppImageError("PyInstaller output must contain one bundled libusb-1.0.so.0")
    expected_data = library_root / "_internal" / "swarm2" / "data" / "udev"
    if not expected_data.is_dir():
        raise AppImageError("PyInstaller output is missing the packaged MC7 data files")
    for relative in (
        Path("udev/70-swarm2-mc7.rules"),
        Path("gnome-shell/automatic-profiles@swarm2-mc7.local/extension.js"),
        Path("gnome-shell/automatic-profiles@swarm2-mc7.local/metadata.json"),
    ):
        source = PROJECT_ROOT / "src" / "swarm2" / "data" / relative
        bundled = library_root / "_internal" / "swarm2" / "data" / relative
        if not bundled.is_file() or bundled.read_bytes() != source.read_bytes():
            raise AppImageError(f"Packaged data differs from its source: {relative}")
    _verify_frozen_appdir(appdir, environment)
    return appdir


def _write_checksum(path: Path) -> Path:
    checksum = path.with_name(path.name + ".sha256")
    checksum.write_text(f"{_sha256(path)}  {path.name}\n", encoding="ascii")
    return checksum


def _verify_update_information(
    appimage: Path, expected: str, environment: dict[str, str]
) -> None:
    argv = [str(appimage), "--appimage-updateinformation"]
    print("+", shlex.join(argv), flush=True)
    runtime_environment = environment.copy()
    runtime_environment.pop("APPIMAGE_EXTRACT_AND_RUN", None)
    result = subprocess.run(
        argv, cwd=PROJECT_ROOT, env=runtime_environment, check=False,
        capture_output=True, text=True,
    )
    if result.returncode:
        raise AppImageError(
            f"Update-information check exited with {result.returncode}: {result.stderr.strip()}"
        )
    if result.stdout.strip() != expected:
        raise AppImageError(
            "Embedded AppImage update information differs from the requested value"
        )


def build(arguments: argparse.Namespace) -> list[Path]:
    _require_build_platform()
    _require_python_packages()
    if arguments.update_information and shutil.which("zsyncmake") is None:
        raise AppImageError("Install zsyncmake to build requested AppImage update metadata")
    version = _project_version()
    epoch = _source_date_epoch()
    output = arguments.output_dir.expanduser().resolve()
    if output.exists():
        raise AppImageError(f"Refusing to replace existing output directory: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    _reset_build_root()

    debian_package_archives = [
        _inspect_debian_package_archive(path)
        for path in arguments.debian_package_archive
    ]

    appimagetool = _pinned_file(
        arguments.appimagetool,
        url=APPIMAGETOOL_URL,
        sha256=APPIMAGETOOL_SHA256,
        name=f"appimagetool-{APPIMAGETOOL_VERSION}-x86_64.AppImage",
    )
    runtime = _pinned_file(
        arguments.runtime,
        url=RUNTIME_URL,
        sha256=RUNTIME_SHA256,
        name=f"runtime-{RUNTIME_VERSION}-x86_64",
    )
    seven_zip_archive = _pinned_file(
        arguments.seven_zip_archive,
        url=SEVEN_ZIP_URL,
        sha256=SEVEN_ZIP_SHA256,
        name=f"7zip-{SEVEN_ZIP_VERSION}-linux-x64.tar.xz",
    )
    appimagetool.chmod(appimagetool.stat().st_mode | stat.S_IXUSR)
    runtime.chmod(runtime.stat().st_mode | stat.S_IXUSR)
    seven_zip, seven_zip_license = _extract_seven_zip(seven_zip_archive)
    libusb_source = _find_libusb(arguments.libusb)
    libusb = BUILD_ROOT / "inputs" / "libusb-1.0.so.0"
    _copy_file(libusb_source, libusb, 0o755)
    environment = _environment(epoch, libusb)
    environment["VERSION"] = version
    appdir = _build_appdir(
        environment,
        seven_zip,
        seven_zip_license,
        version,
        libusb_source,
        debian_package_archives,
    )
    bundled_components = (
        appdir / "usr" / "share" / "licenses" / "mc7-studio"
        / "BUNDLED-COMPONENTS.json"
    )
    if not bundled_components.is_file():
        raise AppImageError("The AppImage component manifest was not generated")
    if arguments.maximum_glibc:
        _verify_glibc_baseline(appdir, arguments.maximum_glibc)

    staged_output = BUILD_ROOT / f"{APP_NAME}-{version}-{ARCHITECTURE}.AppImage"
    command = [
        str(appimagetool),
        "--runtime-file",
        str(runtime),
    ]
    if arguments.update_information:
        command.extend(["--updateinformation", arguments.update_information])
    command.extend([str(appdir), str(staged_output)])
    _run(command, environment=environment, cwd=BUILD_ROOT)
    if not staged_output.is_file() or staged_output.stat().st_size < 1024 * 1024:
        raise AppImageError("appimagetool did not create a usable AppImage")
    staged_output.chmod(0o755)

    staged_zsync = staged_output.with_name(staged_output.name + ".zsync")
    if arguments.update_information:
        if not staged_zsync.is_file():
            raise AppImageError("appimagetool did not create update metadata")
    release_staging = BUILD_ROOT / "release-output"
    release_staging.mkdir()
    release_appimage = release_staging / staged_output.name
    os.replace(staged_output, release_appimage)
    artifacts = [release_appimage, _write_checksum(release_appimage)]
    release_components = release_staging / (
        f"{APP_NAME}-{version}-{ARCHITECTURE}.AppImage.components.json"
    )
    _copy_file(bundled_components, release_components)
    artifacts.extend([release_components, _write_checksum(release_components)])
    if arguments.update_information:
        release_zsync = release_staging / staged_zsync.name
        os.replace(staged_zsync, release_zsync)
        artifacts.extend([release_zsync, _write_checksum(release_zsync)])

    _run([str(release_appimage), "--version"], environment=environment)
    if arguments.update_information:
        _verify_update_information(
            release_appimage, arguments.update_information, environment
        )
    os.replace(release_staging, output)
    artifacts = [output / artifact.name for artifact in artifacts]
    print(f"Built {len(artifacts)} AppImage release files in {output}")
    return artifacts


def _path(value: str) -> Path:
    return Path(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", type=_path, default=PROJECT_ROOT / "dist" / "appimage",
        help="new output directory (default: dist/appimage)",
    )
    parser.add_argument("--appimagetool", type=_path, help="pre-downloaded pinned appimagetool")
    parser.add_argument("--runtime", type=_path, help="pre-downloaded pinned type-2 runtime")
    parser.add_argument(
        "--seven-zip-archive", type=_path, help="pre-downloaded pinned official 7-Zip archive"
    )
    parser.add_argument("--libusb", type=_path, help="x86_64 libusb-1.0.so.0 build input")
    parser.add_argument(
        "--debian-package-archive",
        type=_path,
        action="append",
        default=[],
        help=(
            "verified .deb provenance fallback for a locally extracted system library; "
            "repeat for multiple packages"
        ),
    )
    parser.add_argument(
        "--maximum-glibc",
        help="reject bundled ELF files requiring a newer glibc version, such as 2.35",
    )
    parser.add_argument(
        "--update-information",
        help="AppImage update string; tagged GitHub builds use gh-releases-zsync",
    )
    arguments = parser.parse_args(argv)
    try:
        build(arguments)
        return 0
    except (AppImageError, OSError, subprocess.SubprocessError) as error:
        print(f"AppImage build error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
