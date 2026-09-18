"""Offline acceptance checks executed inside the frozen AppImage runtime."""

from __future__ import annotations

from importlib import resources
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def _check_dependencies() -> None:
    import certifi
    import hid
    import psutil
    import websocket

    if not callable(getattr(hid, "enumerate", None)):
        raise RuntimeError("hidapi is missing enumerate()")
    if not callable(getattr(psutil, "cpu_percent", None)):
        raise RuntimeError("psutil is missing cpu_percent()")
    if not callable(getattr(websocket, "create_connection", None)):
        raise RuntimeError("websocket-client is missing create_connection()")
    if not Path(certifi.where()).is_file():
        raise RuntimeError("certifi CA bundle is missing")


def _check_image_formats() -> list[str]:
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtGui import QImageReader

    application = QCoreApplication.instance() or QCoreApplication([])
    formats = sorted(bytes(value).decode("ascii") for value in QImageReader.supportedImageFormats())
    if "png" not in formats or not ({"jpeg", "jpg"} & set(formats)):
        raise RuntimeError("Qt PNG/JPEG image format support is incomplete")
    application.processEvents()
    return formats


def _check_resources() -> None:
    data = resources.files("swarm2").joinpath("data")
    rule = data.joinpath("udev").joinpath("70-swarm2-mc7.rules")
    extension = data.joinpath("gnome-shell")
    if b'idVendor}=="10f5"' not in rule.read_bytes():
        raise RuntimeError("Packaged udev rule is missing or invalid")
    if not any(child.name == "metadata.json" for directory in extension.iterdir() for child in directory.iterdir()):
        raise RuntimeError("Packaged GNOME extension assets are missing")


def _check_libusb() -> None:
    from swarm2.firmware_usb import _load_library

    _load_library()


def _check_host_environment() -> None:
    from swarm2.runtime import host_command_environment

    environment = host_command_environment()
    appdir = os.environ.get("APPDIR", "")
    for name in (
        "PATH", "XDG_DATA_DIRS", "SSL_CERT_FILE", "QT_PLUGIN_PATH", "QML2_IMPORT_PATH",
    ):
        value = environment.get(name, "")
        if appdir and value.startswith(appdir + os.sep):
            raise RuntimeError(f"Host commands inherit the bundled {name}")
    if any(name.startswith("MC7_STUDIO_HOST_") for name in environment):
        raise RuntimeError("Host commands inherit AppImage environment markers")


def _check_seven_zip() -> None:
    executable = shutil.which("7zz")
    if executable is None:
        raise RuntimeError("Bundled 7zz is not on PATH")
    with tempfile.TemporaryDirectory(prefix="mc7-appimage-self-test-") as directory:
        root = Path(directory)
        payload = root / "payload.txt"
        archive = root / "fixture.7z"
        payload.write_bytes(b"MC7 Studio AppImage\n")
        created = subprocess.run(
            [executable, "a", "-bd", "-bb0", str(archive), str(payload)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False,
        )
        if created.returncode:
            raise RuntimeError("Bundled 7zz could not create a synthetic archive")
        extracted = subprocess.run(
            [executable, "x", "-so", "-bd", "-bb0", str(archive), "payload.txt"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False,
        )
        if extracted.returncode or extracted.stdout != payload.read_bytes():
            raise RuntimeError("Bundled 7zz synthetic archive readback failed")


def main() -> int:
    try:
        _check_dependencies()
        formats = _check_image_formats()
        _check_resources()
        _check_libusb()
        _check_host_environment()
        _check_seven_zip()
        certificate = Path(os.environ.get("SSL_CERT_FILE", ""))
        if not certificate.is_file() or certificate.stat().st_size < 100_000:
            raise RuntimeError("AppRun did not select the bundled CA certificate file")
        print(json.dumps({"result": "ok", "image_formats": formats}, sort_keys=True))
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(json.dumps({"error": str(error)}, sort_keys=True))
        return 2
