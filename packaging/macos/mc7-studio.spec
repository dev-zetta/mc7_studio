# -*- mode: python ; coding: utf-8 -*-
import os
from pathlib import Path
import sys

ROOT = Path(SPECPATH).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from native_build import keep_qt_payload, version

a = Analysis(
    [str(ROOT / "packaging/native/native_entrypoint.py")],
    pathex=[str(ROOT / "src"), str(ROOT / "packaging/native")],
    binaries=[(os.environ["MC7_NATIVE_SEVEN_ZIP"], "bin")],
    datas=[(str(ROOT / "src/swarm2/data"), "swarm2/data"), (os.environ["MC7_NATIVE_NOTICES"], "notices")],
    hiddenimports=["certifi", "hid", "psutil", "websocket"],
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False,
)
a.binaries = [entry for entry in a.binaries if keep_qt_payload(entry)]
a.datas = [entry for entry in a.datas if keep_qt_payload(entry)]
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="MC7-Studio",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=False, argv_emulation=False,
    target_arch="arm64", codesign_identity=None, entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="MC7-Studio")
app = BUNDLE(
    coll, name="MC7 Studio.app", bundle_identifier="io.github.dev_zetta.MC7Studio",
    version=version(),
    info_plist={
        "CFBundleDisplayName": "MC7 Studio",
        "CFBundleShortVersionString": version(),
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
        "NSPrincipalClass": "NSApplication",
        "NSHumanReadableCopyright": "Copyright 2026 Gabriel Max. GPL-3.0-or-later.",
    },
)
