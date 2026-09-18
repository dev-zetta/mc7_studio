"""Inspect a pinned MC7 firmware archive without installing or executing it.

Only the catalog-pinned regular files are read from 7-Zip's stdout. No archive
member is extracted to the filesystem, and no vendor library is loaded.
"""

from configparser import ConfigParser, Error as IniError
from dataclasses import dataclass
import hashlib
import os
from .file_io import open_regular_read
from pathlib import Path, PurePosixPath
import selectors
from .pipe_io import pipe_selector
import shutil
import stat
import subprocess
import tempfile
import time
from typing import TYPE_CHECKING

from .runtime import host_command_environment

if TYPE_CHECKING:
    from .firmware_catalog import FirmwareRelease

MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_LISTING_BYTES = 128 * 1024
MAX_INI_BYTES = 4096
EXTRACT_TIMEOUT_SECONDS = 60


class FirmwarePackageError(ValueError):
    pass


@dataclass(frozen=True)
class FirmwarePackage:
    release: "FirmwareRelease"
    offer: bytes
    payload: bytes
    package_version: str
    fw_version: int
    auto_reset_version: int
    component_id: int

    def requires_reset(self, installed_numeric: int) -> bool:
        """Compare manifest metadata; a receiver reset protocol is not implied."""
        if type(installed_numeric) is not int or not 0 < installed_numeric <= 9999:
            raise FirmwarePackageError("A nonzero installed firmware version is required")
        return bool(self.auto_reset_version and installed_numeric < self.auto_reset_version)


def _executable(value=None) -> str:
    if value is not None:
        candidate = Path(value)
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise FirmwarePackageError("The selected 7-Zip executable is unavailable")
        return str(candidate.resolve())
    # Native bundles keep their archive tool private, without changing the
    # PATH inherited by host applications launched from LCD tiles.
    import sys
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        name = "7z.exe" if sys.platform == "win32" else "7zz"
        candidate = Path(sys._MEIPASS) / "bin" / name
        if candidate.is_file():
            return str(candidate)
    for name in ("7zz", "7z"):
        candidate = shutil.which(name)
        if candidate:
            return candidate
    # The development checkout already includes the official standalone tool.
    # Installed applications instead use 7zz/7z from their normal PATH.
    candidate = Path(__file__).resolve().parents[2] / ".tools" / "7zip" / "7zz"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    raise FirmwarePackageError("Install 7-Zip (7zz or 7z) to inspect the firmware package")


def _run_7z(executable: str, arguments: list[str], maximum: int) -> bytes:
    """Drain both pipes with hard byte/time limits; never capture unbounded output."""
    environment = host_command_environment({"LC_ALL": "C", "LANG": "C"})
    try:
        process = subprocess.Popen([executable, *arguments], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=environment, close_fds=True)
    except OSError as error:
        raise FirmwarePackageError(f"Could not start 7-Zip: {error}") from error
    output, errors = bytearray(), bytearray()
    deadline = time.monotonic() + EXTRACT_TIMEOUT_SECONDS
    try:
        with pipe_selector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ, (output, maximum))
            ready.register(process.stderr, selectors.EVENT_READ, (errors, 65536))
            while ready.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise FirmwarePackageError("Firmware package inspection timed out")
                for key, _ in ready.select(min(remaining, 0.1)):
                    buffer, limit = key.data
                    chunk = os.read(key.fileobj.fileno(), min(65536, limit - len(buffer) + 1))
                    if not chunk:
                        ready.unregister(key.fileobj)
                    else:
                        buffer.extend(chunk)
                        if len(buffer) > limit:
                            raise FirmwarePackageError("Firmware archive output exceeds its allowed size")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FirmwarePackageError("Firmware package inspection timed out")
            try:
                code = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as error:
                raise FirmwarePackageError("Firmware package inspection timed out") from error
        if code != 0:
            raise FirmwarePackageError("7-Zip could not read the verified firmware package")
        return bytes(output)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
        process.stderr.close()


def _copy_verified_archive(source, destination, release):
    if type(release.size) is not int or not 1 <= release.size <= MAX_ARCHIVE_BYTES:
        raise FirmwarePackageError("Firmware archive size is outside the supported limit")
    try:
        descriptor = open_regular_read(source)
        with os.fdopen(descriptor, "rb") as handle:
            identity = os.fstat(handle.fileno())
            if not stat.S_ISREG(identity.st_mode) or identity.st_size != release.size:
                raise FirmwarePackageError("Firmware archive size does not match the trusted release")
            md5, sha256 = hashlib.md5(usedforsecurity=False), hashlib.sha256()
            count = 0
            with destination.open("xb") as copied:
                while chunk := handle.read(min(1024 * 1024, release.size - count + 1)):
                    count += len(chunk)
                    if count > release.size:
                        raise FirmwarePackageError("Firmware archive changed while it was being checked")
                    md5.update(chunk)
                    sha256.update(chunk)
                    copied.write(chunk)
            if count != release.size or md5.hexdigest() != release.md5 or sha256.hexdigest() != release.sha256:
                raise FirmwarePackageError("Firmware archive hashes do not match the trusted release")
    except OSError as error:
        raise FirmwarePackageError(f"Could not read firmware archive: {error}") from error


def _list_entries(raw: bytes) -> dict[str, int]:
    try:
        text = raw.decode("utf-8", errors="strict").replace("\r\n", "\n")
    except UnicodeError as error:
        raise FirmwarePackageError("Firmware archive has an invalid file listing") from error
    entries = {}
    for block in text.strip().split("\n\n"):
        values = {}
        for line in block.splitlines():
            key, separator, value = line.partition(" = ")
            if not separator or key in values:
                raise FirmwarePackageError("Firmware archive has an ambiguous file listing")
            values[key] = value
        name = values.get("Path", "")
        path = PurePosixPath(name)
        if (not name or name in entries or "\\" in name or ":" in name
                or path.is_absolute() or any(part in ("", ".", "..") for part in name.split("/"))
                or any(ord(character) < 32 or ord(character) > 126 for character in name)
                or values.get("Encrypted") != "-"
                or any("link" in key.lower() for key in values)
                or values.get("Anti", "-") != "-"):
            raise FirmwarePackageError("Firmware archive contains an unsafe or unsupported entry")
        try:
            size = int(values["Size"])
        except (KeyError, ValueError) as error:
            raise FirmwarePackageError("Firmware archive is missing a valid entry size") from error
        attributes = values.get("Attributes", "")
        if any(part.startswith("l") for part in attributes.split()):
            raise FirmwarePackageError("Firmware archive links are not supported")
        directory = "D" in attributes or values.get("Folder") == "+"
        if size < 0 or size > MAX_PAYLOAD_BYTES or directory and size != 0:
            raise FirmwarePackageError("Firmware archive member exceeds the supported size")
        entries[name] = -1 if directory else size
        if len(entries) > 32:
            raise FirmwarePackageError("Firmware archive contains too many entries")
    return entries


def _ini(raw: bytes, *, general_header: bool, expected: set[str]) -> dict[str, str]:
    if not raw or len(raw) > MAX_INI_BYTES or b"\0" in raw:
        raise FirmwarePackageError("Firmware metadata has an invalid size or encoding")
    parser = ConfigParser(interpolation=None, strict=True)
    try:
        content = raw.decode("ascii", errors="strict")
        parser.read_string(content if general_header else "[General]\n" + content)
        if parser.sections() != ["General"] or parser.defaults():
            raise FirmwarePackageError("Firmware metadata contains unsupported sections")
        values = dict(parser["General"])
    except (UnicodeError, IniError) as error:
        raise FirmwarePackageError("Firmware package metadata is malformed") from error
    if set(values) != expected:
        raise FirmwarePackageError("Firmware metadata fields do not match the supported format")
    return values


def inspect_firmware_package(archive_path, release, *, executable=None) -> FirmwarePackage:
    """Verify trusted identity, exact file layout and bounded package contents.

    The resulting bytes are staging data, not permission or proof of safe
    flashing. The source-derived CFU parsers validate their structure here.
    """
    from .firmware_catalog import validate_release
    release = validate_release(release)
    tool = _executable(executable)
    if release.role not in ("mouse", "transmitter") or release.product_id not in (0x502C, 0x502E):
        raise FirmwarePackageError("Only known MC7 mouse and transmitter packages are supported")
    role_pid = {"mouse": 0x502C, "transmitter": 0x502E}[release.role]
    if release.vendor_id != 0x10F5 or release.product_id != role_pid:
        raise FirmwarePackageError("Firmware package role and USB identity do not match")
    number = release.firmware_version
    if type(number) is not int or not 1 <= number <= 9999:
        raise FirmwarePackageError("Unsupported firmware manifest version")
    if release.package_version != f"{number // 100}.{number % 100}.0.0":
        raise FirmwarePackageError("Firmware package and installed-version formats do not match")
    prefix = f"data/Firmware/COMMAND_MC7/10F5_{release.product_id:04X}"
    directory = f"{prefix}/{number}"
    stem = release.archive_stem
    offer_name, payload_name = stem + ".offer.bin", stem + ".payload.bin"
    paths = {"version": f"{prefix}/version.ini", "info": f"{directory}/Info.ini",
             "offer": f"{directory}/{offer_name}", "payload": f"{directory}/{payload_name}"}
    equivalents = [
        {"offer": f"{directory}/{alias}.offer.bin",
         "payload": f"{directory}/{alias}.payload.bin"}
        for alias in release.equivalent_archive_stems
    ]
    expected_files = set(paths.values()) | {
        path for equivalent in equivalents for path in equivalent.values()
    }
    permitted_directories = {str(parent) for name in expected_files
                             for parent in PurePosixPath(name).parents if str(parent) != "."}
    with tempfile.TemporaryDirectory(prefix="swarm2-firmware-") as temporary:
        archive = Path(temporary) / "verified.7z"
        _copy_verified_archive(archive_path, archive, release)
        listing = _run_7z(tool, ["l", "-slt", "-ba", "-sccUTF-8", "--", str(archive)], MAX_LISTING_BYTES)
        entries = _list_entries(listing)
        if ({path for path, size in entries.items() if size >= 0} != expected_files
                or any(path not in permitted_directories for path, size in entries.items() if size == -1)):
            raise FirmwarePackageError("Firmware archive contains unexpected files or role paths")
        for name in ("version", "info"):
            if not 0 < entries[paths[name]] <= MAX_INI_BYTES:
                raise FirmwarePackageError("Firmware metadata exceeds the supported size")
        if entries[paths["offer"]] != 16 or not 0 < entries[paths["payload"]] <= MAX_PAYLOAD_BYTES:
            raise FirmwarePackageError("Firmware offer or payload size is invalid")
        if any(entries[equivalent["offer"]] != 16
               or not 0 < entries[equivalent["payload"]] <= MAX_PAYLOAD_BYTES
               for equivalent in equivalents):
            raise FirmwarePackageError("Equivalent firmware members have invalid sizes")

        def member(name):
            path = paths[name]
            content = _run_7z(tool, ["x", "-so", "-y", "-bd", "-bb0", "-spd", "--", str(archive), path], entries[path])
            if len(content) != entries[path]:
                raise FirmwarePackageError("Firmware archive member length differs from its listing")
            return content

        metadata = _ini(member("version"), general_header=True,
                        expected={"version", "fw_version", "fw_auto_reset_version"})
        if metadata["version"] != release.package_version or metadata["fw_version"] != str(number):
            raise FirmwarePackageError("Firmware manifest does not match the trusted release")
        reset_text = metadata["fw_auto_reset_version"]
        if (not reset_text.isascii() or not reset_text.isdigit() or len(reset_text) > 4
                or reset_text != str(int(reset_text))
                or int(reset_text) != release.auto_reset_version):
            raise FirmwarePackageError("Firmware reset threshold is invalid")
        info = _ini(member("info"), general_header=False, expected={"offer_file", "payload_file"})
        if info != {"offer_file": offer_name, "payload_file": payload_name}:
            raise FirmwarePackageError("Firmware metadata refers to unexpected offer or payload files")
        offer, payload = member("offer"), member("payload")
        for equivalent in equivalents:
            equivalent_offer = _run_7z(
                tool,
                ["x", "-so", "-y", "-bd", "-bb0", "-spd", "--", str(archive), equivalent["offer"]],
                entries[equivalent["offer"]],
            )
            equivalent_payload = _run_7z(
                tool,
                ["x", "-so", "-y", "-bd", "-bb0", "-spd", "--", str(archive), equivalent["payload"]],
                entries[equivalent["payload"]],
            )
            if (len(equivalent_offer) != entries[equivalent["offer"]]
                    or len(equivalent_payload) != entries[equivalent["payload"]]):
                raise FirmwarePackageError("Firmware archive member length differs from its listing")
            if equivalent_offer != offer or equivalent_payload != payload:
                raise FirmwarePackageError("Equivalent firmware members do not match the selected image")
    from .firmware_commands import (FirmwareProtocolError, decode_realtek_version,
                                    parse_offer, validate_payload)
    try:
        parsed_offer = parse_offer(offer)
        payload_info = validate_payload(payload)
    except FirmwareProtocolError as error:
        raise FirmwarePackageError(f"Invalid firmware package contents: {error}") from error
    # Both independently hashed official archives use CFU component0F. Parsing
    # this identity does not reinterpret their separate packed CFU versions.
    if parsed_offer.component_id != 0x0F:
        raise FirmwarePackageError("Unexpected MC7 CFU component identifier")
    expected_part = 1 if release.role == "mouse" else 0
    version_parts = decode_realtek_version(parsed_offer.version_raw)
    if (version_parts != (expected_part, 7, number // 100, number % 100)
            or parsed_offer.token != 0 or parsed_offer.bank != 0):
        raise FirmwarePackageError("Firmware offer identity does not match the trusted release")
    if payload_info.record_count < 2 or payload_info.base_address != 0:
        raise FirmwarePackageError("The MC7 firmware payload requires multiple records starting at address zero")
    return FirmwarePackage(release, offer, payload, release.package_version, number,
                           int(reset_text), parsed_offer.component_id)
