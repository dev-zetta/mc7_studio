# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path


ROOT = Path(SPECPATH).parents[1]
LIBUSB = os.environ.get("MC7_APPIMAGE_LIBUSB")
if not LIBUSB:
    raise RuntimeError("MC7_APPIMAGE_LIBUSB must name the libusb-1.0.so.0 build input")

a = Analysis(
    [str(ROOT / "packaging" / "appimage" / "entrypoint.py")],
    pathex=[str(ROOT / "src")],
    binaries=[(LIBUSB, ".")],
    datas=[(str(ROOT / "src" / "swarm2" / "data"), "swarm2/data")],
    hiddenimports=["certifi", "hid", "psutil", "websocket"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

# QtGui's generic hook collects every image and input-context plugin. The app
# does not use PDF or Qt Virtual Keyboard; the latter is GPL-only in Qt 6.11.
# Remove both plugins and the dependencies that they alone pull into this
# widget-only application.
_unused_qt_payloads = (
    "PySide6/Qt/plugins/imageformats/libqpdf.so",
    "PySide6/Qt/plugins/platforminputcontexts/libqtvirtualkeyboardplugin.so",
    "PySide6/Qt/lib/libQt6Pdf.so",
    "PySide6/Qt/lib/libQt6Qml",
    "PySide6/Qt/lib/libQt6Quick.so",
    "PySide6/Qt/lib/libQt6VirtualKeyboard",
    "libQt6Pdf.so",
    "libQt6Qml",
    "libQt6Quick.so",
    "libQt6VirtualKeyboard",
)


def _keep_binary(entry):
    destination = entry[0]
    if destination.startswith("PySide6/Qt/plugins/imageformats/"):
        return destination.rsplit("/", 1)[-1] in {"libqjpeg.so", "libqsvg.so"}
    return not destination.startswith(_unused_qt_payloads)


a.binaries = [
    entry for entry in a.binaries
    if _keep_binary(entry)
]
a.datas = [
    entry for entry in a.datas
    if _keep_binary(entry)
    and not entry[0].startswith("PySide6/Qt/qml/")
]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="mc7-studio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="mc7-studio",
)
