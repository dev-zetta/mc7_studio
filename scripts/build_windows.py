#!/usr/bin/env python3
"""Build and verify the portable Windows MC7 Studio archive."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import zipfile


from native_build import ROOT, copy_notices, prepare_seven_zip, sha256 as _sha256, version as _version


class WindowsBuildError(RuntimeError):
    pass


def build(output: Path) -> tuple[Path, Path]:
    if platform.system() != "Windows" or platform.machine().lower() not in ("amd64", "x86_64"):
        raise WindowsBuildError("The portable package must be built on 64-bit Windows")
    version = _version()
    work = ROOT / "tmp" / "windows-build"
    bundle_root = work / "bundle"
    pyinstaller_work = work / "pyinstaller"
    shutil.rmtree(work, ignore_errors=True)
    output.mkdir(parents=True, exist_ok=True)
    tool = prepare_seven_zip("Windows")
    environment = dict(os.environ, MC7_NATIVE_SEVEN_ZIP=str(tool))
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--workpath", os.fspath(pyinstaller_work),
        "--distpath", os.fspath(bundle_root),
        os.fspath(ROOT / "packaging" / "windows" / "mc7-studio.spec"),
    ], cwd=ROOT, env=environment, check=True)
    bundle = bundle_root / "MC7-Studio"
    executable = bundle / "MC7-Studio.exe"
    if not executable.is_file():
        raise WindowsBuildError("PyInstaller did not create MC7-Studio.exe")
    copy_notices(bundle, "windows")
    console = bundle / "MC7-Studio-CLI.exe"
    subprocess.run([os.fspath(console), "--swarm2-native-self-test"], check=True, timeout=60)
    subprocess.run([os.fspath(executable), "--swarm2-native-self-test"], check=True, timeout=60)

    archive = output / f"MC7-Studio-{version}-windows-x86_64.zip"
    archive.unlink(missing_ok=True)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as destination:
        for source in sorted(bundle.rglob("*")):
            if source.is_file():
                destination.write(source, (Path("MC7-Studio") / source.relative_to(bundle)).as_posix())
    checksum = archive.with_suffix(archive.suffix + ".sha256")
    checksum.write_text(f"{_sha256(archive)}  {archive.name}\n", encoding="ascii", newline="\n")
    extracted = work / "extracted"
    with zipfile.ZipFile(archive) as source:
        source.extractall(extracted)
    subprocess.run([str(extracted / "MC7-Studio/MC7-Studio-CLI.exe"), "--swarm2-native-self-test"], check=True, timeout=60)
    return archive, checksum


def verify(archive: Path) -> None:
    checksum = archive.with_suffix(archive.suffix + ".sha256")
    expected = checksum.read_text(encoding="ascii").split()[0]
    if expected != _sha256(archive):
        raise WindowsBuildError("The Windows archive checksum does not match")
    with zipfile.ZipFile(archive) as source:
        names = set(source.namelist())
        required = {"MC7-Studio/MC7-Studio.exe", "MC7-Studio/MC7-Studio-CLI.exe", "MC7-Studio/_internal/bin/7z.exe", "MC7-Studio/LICENSE.txt", "MC7-Studio/README.md", "MC7-Studio/THIRD_PARTY_NOTICES.txt", "MC7-Studio/BUNDLED-COMPONENTS.json"}
        if not required.issubset(names):
            raise WindowsBuildError("The Windows archive is missing required files")
        if any(name.startswith("/") or ".." in Path(name).parts for name in names):
            raise WindowsBuildError("The Windows archive contains an unsafe path")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist" / "windows")
    parser.add_argument("--verify-only", type=Path)
    arguments = parser.parse_args()
    if arguments.verify_only:
        verify(arguments.verify_only)
    else:
        archive, _checksum = build(arguments.output_dir)
        verify(archive)
        print(archive)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.SubprocessError, RuntimeError, zipfile.BadZipFile) as error:
        print(f"Windows package build failed: {error}", file=sys.stderr)
        raise SystemExit(2)
