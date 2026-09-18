"""Download a known official archive with bounded reads and atomic installation.

No device access, extraction, vendor updater execution, or firmware flashing.
"""

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .firmware_catalog import FirmwareError, FirmwareRelease, validate_release

_MAX_RESOLVER_BYTES = 8192
_CHUNK_BYTES = 65536
_NETWORK_TIMEOUT = 20
_TOTAL_SECONDS = 120
_SEVEN_Z_MAGIC = b"7z\xbc\xaf\x27\x1c"


@dataclass(frozen=True)
class FirmwareDownload:
    path: Path
    release: FirmwareRelease
    reused_existing: bool


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Validate each target before making any subsequent request.
        return None


def _allowed_url(url: str, release: FirmwareRelease) -> str:
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.username is not None or parts.password is not None
            or parts.port not in (None, 443) or parts.query or parts.fragment
            or url not in (release.resolver_url, release.cdn_url)):
        raise FirmwareError("Firmware download left the verified official URLs")
    return url


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise FirmwareError("Firmware download exceeded its time limit")
    return min(_NETWORK_TIMEOUT, remaining)


def _open_url(opener, url: str, release: FirmwareRelease, deadline: float):
    for _ in range(4):
        _allowed_url(url, release)
        request = Request(url, headers={"User-Agent": "MC7-Studio/0.1 firmware-download",
                                        "Accept-Encoding": "identity"})
        try:
            response = opener.open(request, timeout=_remaining(deadline))
        except HTTPError as error:
            try:
                if error.code not in (301, 302, 303, 307, 308):
                    raise FirmwareError(f"Official firmware server returned HTTP {error.code}") from error
                location = error.headers.get("Location")
                if not location:
                    raise FirmwareError("Official firmware redirect has no destination") from error
                url = _allowed_url(urljoin(url, location), release)
            finally:
                error.close()
            continue
        if response.status != 200:
            response.close()
            raise FirmwareError("Official firmware server did not return a complete file")
        try:
            _allowed_url(response.geturl(), release)
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise FirmwareError("Unexpected compressed HTTP firmware response")
        except Exception:
            response.close()
            raise
        return response
    raise FirmwareError("Too many official firmware redirects")


def _resolve(opener, release: FirmwareRelease, deadline: float) -> str:
    with _open_url(opener, release.resolver_url, release, deadline) as response:
        # A future redirect directly to the already verified CDN path is safe.
        if response.geturl() == release.cdn_url:
            return release.cdn_url
        raw = bytearray()
        while len(raw) <= _MAX_RESOLVER_BYTES:
            chunk = _read_chunk(response, _MAX_RESOLVER_BYTES + 1 - len(raw), deadline)
            if not chunk:
                break
            raw.extend(chunk)
    _remaining(deadline)
    if len(raw) > _MAX_RESOLVER_BYTES:
        raise FirmwareError("Official firmware resolver response is too large")
    try:
        resolved = raw.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise FirmwareError("Official firmware resolver did not return a URL") from error
    if resolved != release.cdn_url:
        raise FirmwareError("The official package URL changed; the verified catalog needs updating")
    return _allowed_url(resolved, release)


def _read_chunk(response, size: int, deadline: float) -> bytes:
    _remaining(deadline)
    # read1 returns after at most one underlying buffered read. read(size) can
    # otherwise wait indefinitely for many slowly arriving socket fragments.
    reader = getattr(response, "read1", response.read)
    chunk = reader(size)
    _remaining(deadline)
    return chunk


def _hashes_match(data_size: int, md5: str, sha256: str, release: FirmwareRelease) -> bool:
    return data_size == release.size and md5 == release.md5 and sha256 == release.sha256


def _verify_existing(path: Path, release: FirmwareRelease) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_size != release.size:
        raise FirmwareError("The destination exists and is not this verified firmware archive")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        current = os.fstat(stream.fileno())
        if not stat.S_ISREG(current.st_mode) or current.st_size != release.size:
            raise FirmwareError("The destination changed while checking the existing archive")
        md5 = hashlib.md5(usedforsecurity=False)
        sha256 = hashlib.sha256()
        count = 0
        while count <= release.size:
            chunk = stream.read(min(_CHUNK_BYTES, release.size + 1 - count))
            if not chunk:
                break
            count += len(chunk)
            md5.update(chunk)
            sha256.update(chunk)
        if not _hashes_match(count, md5.hexdigest(), sha256.hexdigest(), release):
            raise FirmwareError("The destination exists but its firmware checksums do not match")
    return True


def download_release(release: FirmwareRelease, destination: str | Path) -> FirmwareDownload:
    """Save to the full chosen .7z path, reusing an already verified identical file.

    Any other existing file is refused. A sibling temporary file is verified
    completely before atomically creating the destination with a hard link;
    this also refuses a file created by another process during the download.
    """
    release = validate_release(release)
    path = Path(destination).expanduser().absolute()
    if path.suffix.lower() != ".7z" or not path.parent.is_dir():
        raise FirmwareError("Choose a .7z filename in an existing directory")
    temporary = None
    try:
        if _verify_existing(path, release):
            return FirmwareDownload(path, release, True)
        deadline = time.monotonic() + _TOTAL_SECONDS
        opener = build_opener(_NoRedirect())
        url = _resolve(opener, release, deadline)
        with _open_url(opener, url, release, deadline) as response:
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdecimal() or int(length) != release.size):
                raise FirmwareError("Official firmware size does not match the verified catalog")
            fd, temporary_name = tempfile.mkstemp(prefix=".mc7-firmware-", suffix=".part", dir=path.parent)
            temporary = Path(temporary_name)
            md5 = hashlib.md5(usedforsecurity=False)
            sha256 = hashlib.sha256()
            count = 0
            prefix = bytearray()
            with os.fdopen(fd, "wb") as output:
                while True:
                    chunk = _read_chunk(response, min(_CHUNK_BYTES, release.size + 1 - count), deadline)
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > release.size:
                        raise FirmwareError("Official firmware archive is larger than expected")
                    if len(prefix) < len(_SEVEN_Z_MAGIC):
                        prefix.extend(chunk[:len(_SEVEN_Z_MAGIC) - len(prefix)])
                    output.write(chunk)
                    md5.update(chunk)
                    sha256.update(chunk)
                if (bytes(prefix) != _SEVEN_Z_MAGIC
                        or not _hashes_match(count, md5.hexdigest(), sha256.hexdigest(), release)):
                    raise FirmwareError("Official firmware archive failed size or checksum verification")
                output.flush()
                os.fsync(output.fileno())
        _remaining(deadline)
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            if not _verify_existing(path, release):
                raise FirmwareError("The destination changed during the download")
            return FirmwareDownload(path, release, True)
        return FirmwareDownload(path, release, False)
    except FirmwareError:
        raise
    except (URLError, OSError, ValueError) as error:
        raise FirmwareError(f"Could not download the verified firmware archive: {error}") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
