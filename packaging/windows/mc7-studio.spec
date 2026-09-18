# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path
import sys


ROOT = Path(SPECPATH).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from native_build import keep_qt_payload

a = Analysis(
    [str(ROOT / "packaging" / "windows" / "entrypoint.py")],
    pathex=[str(ROOT / "src"), str(ROOT / "packaging/native")],
    binaries=[(os.environ["MC7_NATIVE_SEVEN_ZIP"], "bin")],
    datas=[(str(ROOT / "src" / "swarm2" / "data"), "swarm2/data")],
    hiddenimports=["certifi", "hid", "psutil", "websocket"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
a.binaries = [entry for entry in a.binaries if keep_qt_payload(entry)]
a.datas = [entry for entry in a.datas if keep_qt_payload(entry)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MC7-Studio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

cli = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="MC7-Studio-CLI",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    cli,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="MC7-Studio",
)
