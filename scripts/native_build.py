"""Shared inputs for Windows and Apple Silicon release bundles."""

from __future__ import annotations

import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import tarfile
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ("PyInstaller", "PySide6", "PySide6_Essentials", "PySide6_Addons", "shiboken6", "hidapi", "psutil", "websocket-client", "certifi")
SEVEN_ZIP = {
    "Windows": ("7z2603-extra.7z", "191894e6acb3647ffb69ce630479ff318523b2e2b9890aa7f05c1127c2e59b8f"),
    "Darwin": ("7z2603-mac.tar.xz", "5ca87677072c59f5602e5c49baa27d4694bacd2259b4e507f0094249d4281480"),
}


def version() -> str:
    namespace = {}
    exec((ROOT / "src/swarm2/__init__.py").read_text(encoding="utf-8"), namespace)
    return namespace["__version__"]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_seven_zip(system: str) -> Path:
    name, digest = SEVEN_ZIP[system]
    cache = ROOT / "tmp/native-downloads"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / name
    if not archive.is_file() or sha256(archive) != digest:
        partial = archive.with_suffix(".part")
        with urlopen(f"https://github.com/ip7z/7zip/releases/download/26.03/{name}", timeout=120) as response, partial.open("wb") as output:
            shutil.copyfileobj(response, output)
        if sha256(partial) != digest:
            raise RuntimeError("The official 7-Zip download checksum does not match")
        os.replace(partial, archive)
    destination = cache / system
    destination.mkdir(exist_ok=True)
    if system == "Windows":
        tool = shutil.which("7z") or str(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "7-Zip/7z.exe")
        subprocess.run([tool, "e", "-y", f"-o{destination}", str(archive), "x64/7za.exe", "License.txt"], check=True)
        executable = destination / "7z.exe"
        shutil.copy2(destination / "7za.exe", executable)
    else:
        with tarfile.open(archive, "r:xz") as source:
            for name in ("7zz", "License.txt"):
                member = source.getmember(name)
                if not member.isfile():
                    raise RuntimeError("Unexpected 7-Zip archive member")
                with source.extractfile(member) as input_file, (destination / name).open("wb") as output:
                    shutil.copyfileobj(input_file, output)
        executable = destination / "7zz"
        executable.chmod(0o755)
    return executable


def copy_notices(destination: Path, platform_name: str) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "LICENSE", destination / "LICENSE.txt")
    shutil.copy2(ROOT / "README.md", destination / "README.md")
    shutil.copy2(ROOT / f"packaging/{platform_name}/THIRD_PARTY_NOTICES.txt", destination / "THIRD_PARTY_NOTICES.txt")
    licenses = destination / "licenses"
    shutil.copytree(ROOT / "packaging/appimage/licenses", licenses / "project-notices", dirs_exist_ok=True)
    candidates = [Path(sys.base_prefix) / "LICENSE.txt", Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"]
    python_license = next((path for path in candidates if path.is_file()), None)
    if python_license is None:
        raise RuntimeError("The Python runtime license file is unavailable")
    shutil.copy2(python_license, licenses / "Python-LICENSE.txt")
    system = "Windows" if platform_name == "windows" else "Darwin"
    shutil.copy2(ROOT / "tmp/native-downloads" / system / "License.txt", licenses / "7-Zip-LICENSE.txt")
    for name in COMPONENTS:
        distribution = metadata.distribution(name)
        for item in distribution.files or ():
            if Path(str(item)).name.upper().startswith(("LICENSE", "COPYING", "NOTICE")):
                source = Path(distribution.locate_file(item))
                if source.is_file() and source.stat().st_size <= 1024 * 1024:
                    target = licenses / name / Path(str(item)).name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
    components = {name: metadata.version(name) for name in COMPONENTS}
    components.update({"Python": sys.version.split()[0], "7-Zip": "26.03"})
    (destination / "BUNDLED-COMPONENTS.json").write_text(json.dumps(components, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def keep_qt_payload(entry) -> bool:
    """Match the Qt Base/SVG sources retained with the release."""
    name = entry[0].replace("\\", "/")
    if "/imageformats/" in name:
        return Path(name).name in {"qjpeg.dll", "qsvg.dll", "libqjpeg.dylib", "libqsvg.dylib"}
    return not any(part in name for part in ("Qt6Pdf", "QtPdf", "qpdf", "Qt6Qml", "QtQml", "Qt6Quick", "QtQuick", "Qt6VirtualKeyboard", "QtVirtualKeyboard", "qtvirtualkeyboard", "/qml/"))
