#!/usr/bin/env python3
"""Build, sign and verify an Apple Silicon app in a symlink-preserving ZIP."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import zipfile

from native_build import ROOT, copy_notices, prepare_seven_zip, sha256, version


def verify(archive: Path) -> None:
    if archive.with_suffix(".zip.sha256").read_text(encoding="ascii").split()[0] != sha256(archive):
        raise RuntimeError("The macOS archive checksum does not match")
    with zipfile.ZipFile(archive) as source:
        names = set(source.namelist())
        required = {"MC7 Studio.app/Contents/Info.plist", "MC7 Studio.app/Contents/MacOS/MC7-Studio", "MC7 Studio.app/Contents/Resources/notices/LICENSE.txt"}
        if not required.issubset(names):
            raise RuntimeError("The macOS archive is missing required files")
        if any(name.startswith("/") or ".." in Path(name).parts for name in names):
            raise RuntimeError("The macOS archive contains an unsafe path")
        info = plistlib.loads(source.read("MC7 Studio.app/Contents/Info.plist"))
        if info.get("CFBundleIdentifier") != "io.github.dev_zetta.MC7Studio" or info.get("CFBundleShortVersionString") != version():
            raise RuntimeError("The macOS application identity is incorrect")


def build(output: Path) -> Path:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise RuntimeError("Build the macOS application on an Apple Silicon Mac")
    work = ROOT / "tmp/macos-build"
    shutil.rmtree(work, ignore_errors=True)
    output.mkdir(parents=True, exist_ok=True)
    tool = prepare_seven_zip("Darwin")
    notices = work / "notices"
    copy_notices(notices, "macos")
    environment = dict(os.environ, MC7_NATIVE_SEVEN_ZIP=str(tool), MC7_NATIVE_NOTICES=str(notices))
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--workpath", str(work / "pyinstaller"), "--distpath", str(work / "bundle"), str(ROOT / "packaging/macos/mc7-studio.spec")], cwd=ROOT, env=environment, check=True)
    app = work / "bundle/MC7 Studio.app"
    executable = app / "Contents/MacOS/MC7-Studio"
    subprocess.run(["lipo", str(executable), "-verify_arch", "arm64"], check=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    subprocess.run([str(executable), "--swarm2-native-self-test"], check=True, timeout=60)
    archive = output / f"MC7-Studio-{version()}-macos-arm64.zip"
    archive.unlink(missing_ok=True)
    subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(archive)], check=True)
    archive.with_suffix(".zip.sha256").write_text(f"{sha256(archive)}  {archive.name}\n", encoding="ascii")
    verify(archive)
    # Test what a user extracts, including symlinks and code signatures.
    extracted = work / "extracted"
    subprocess.run(["ditto", "-x", "-k", str(archive), str(extracted)], check=True)
    restored = extracted / "MC7 Studio.app"
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(restored)], check=True)
    subprocess.run([str(restored / "Contents/MacOS/MC7-Studio"), "--swarm2-native-self-test"], check=True, timeout=60)
    return archive


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist/macos")
    parser.add_argument("--verify-only", type=Path)
    args = parser.parse_args()
    if args.verify_only:
        verify(args.verify_only)
    else:
        print(build(args.output_dir))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
        print(f"macOS package build failed: {error}", file=sys.stderr)
        raise SystemExit(2)
