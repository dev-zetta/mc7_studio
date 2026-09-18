"""Exercise the frozen GUI and helper pipes without opening any device."""

import json
import os
from pathlib import Path
import platform
import subprocess
import tempfile


def main():
    import certifi
    import hid
    import psutil
    import websocket
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QImageReader
    from PySide6.QtWidgets import QApplication
    from swarm2.gui.app import MainWindow
    from swarm2.runtime import helper_command
    from swarm2.firmware_package import _executable

    assert callable(hid.enumerate) and callable(psutil.cpu_percent) and callable(websocket.create_connection)
    assert Path(certifi.where()).is_file()
    if platform.system() == "Windows":
        from swarm2.windows import _load_backend
    else:
        from swarm2.macos import _load_backend
    _load_backend()  # Resolve native APIs without enumerating or opening a mouse.
    result = subprocess.run(helper_command("swarm2.hardware"), input=json.dumps({"profile_slot": 1, "operation": "bundle-self-test"}), capture_output=True, text=True, timeout=20)
    if result.returncode != 2 or json.loads(result.stdout) != {"error": "Unsupported device operation"}:
        raise RuntimeError(f"Frozen hardware helper pipe test failed: {result.stdout!r} {result.stderr!r}")

    with tempfile.TemporaryDirectory(prefix="mc7-native-self-test-") as directory:
        root = Path(directory)
        payload = root / "payload.txt"
        payload.write_bytes(b"MC7 Studio native package\n")
        archive = root / "fixture.7z"
        tool = _executable()
        subprocess.run([tool, "a", "-bd", "-bb0", str(archive), str(payload)], check=True, capture_output=True, timeout=20)
        extracted = subprocess.run([tool, "x", "-so", "-bd", "-bb0", str(archive), "payload.txt"], check=True, capture_output=True, timeout=20)
        assert extracted.stdout == payload.read_bytes()
        os.environ["XDG_CONFIG_HOME"] = str(root / "config")
        os.environ["XDG_DATA_HOME"] = str(root / "data")
        app = QApplication([])
        formats = {bytes(value).decode("ascii") for value in QImageReader.supportedImageFormats()}
        assert "png" in formats and ("jpeg" in formats or "jpg" in formats)
        window = MainWindow(auto_discover=False)
        window.show()
        QTimer.singleShot(500, app.quit)
        result = app.exec()
        window.close()
        return result
