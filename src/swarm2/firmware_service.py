"""Firmware catalog, verified archives and isolated update preparation."""

import json
from pathlib import Path
import subprocess
import selectors
from .pipe_io import pipe_selector
import os
import platform
import time

from .firmware_catalog import FirmwareError, current_release, release_by_key
from .firmware_download import download_release
from .runtime import helper_command


def release_for_role(role):
    return current_release(role)


def _selected_release(release_key):
    release = release_by_key(release_key)
    if release is None:
        raise FirmwareError("Choose an exact package from the verified firmware catalog")
    return release


class FirmwareService:
    def __init__(self, device_service):
        self.devices = device_service

    @property
    def installation_available(self):
        return platform.system() != "Windows"

    def read_status(self, device_id):
        return self.devices.read_status(device_id)

    def download(self, release_key, destination):
        release = _selected_release(release_key)
        result = download_release(release, destination)
        return {"path": str(result.path), "role": release.role, "release_key": release.key,
                "version": release.package_version, "sha256": release.sha256,
                "size": release.size, "installation_supported": release.installation_supported}

    def inspect(self, release_key, archive_path):
        from .firmware_package import inspect_firmware_package
        release = _selected_release(release_key)
        package = inspect_firmware_package(Path(archive_path), release)
        return {"path": str(Path(archive_path).resolve()), "role": release.role,
                "release_key": release.key, "version": release.package_version, "sha256": release.sha256,
                "size": release.size, "firmware_version": package.fw_version,
                "auto_reset_version": package.auto_reset_version,
                "payload_size": len(package.payload), "component_id": package.component_id,
                "installation_supported": release.installation_supported}

    def prepare(self, device_id, release_key, archive_path, backup_directory):
        if platform.system() == "Windows":
            raise FirmwareError(
                "Firmware installation is not yet available on Windows; package download and inspection remain available"
            )
        release = _selected_release(release_key)
        if not release.installation_supported:
            raise FirmwareError("This historical package is preserved for download and inspection; installation is not validated")
        request = {"operation": "prepare", "device_id": device_id, "role": release.role,
                   "release_key": release.key,
                   "archive_path": str(archive_path), "backup_directory": str(backup_directory)}
        try:
            process = subprocess.run(helper_command("swarm2.firmware_hardware"),
                                     input=json.dumps(request), text=True, capture_output=True, timeout=90)
        except subprocess.TimeoutExpired as error:
            raise FirmwareError("Firmware preparation timed out; no firmware was installed") from error
        try:
            value = json.loads(process.stdout)
        except ValueError as error:
            raise FirmwareError("Firmware preparation returned no valid result") from error
        if not isinstance(value, dict) or process.returncode or "result" not in value:
            raise FirmwareError(value.get("error", "Firmware preparation failed") if isinstance(value, dict) else "Firmware preparation failed")
        return value["result"]

    def install(self, prepared, progress=lambda value: None):
        """Explicit installation; the helper rechecks the prepared target/backup.

        No retry, background auto-update, or cancellation is performed here.
        A missing terminal result must never be presented as success.
        """
        if platform.system() == "Windows":
            raise FirmwareError("Firmware installation is not yet available on Windows")
        request = {"operation": "install", "device_id": prepared['device_id'],
                   "role": prepared['role'], "archive_path": prepared['archive_path'],
                   "release_key": prepared['release_key'], "prepared": prepared}
        process = subprocess.Popen(helper_command('swarm2.firmware_hardware'),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        terminal, buffer = None, bytearray()
        deadline = time.monotonic() + 1940
        try:
            process.stdin.write(json.dumps(request).encode())
            process.stdin.close()
            with pipe_selector() as ready:
                ready.register(process.stdout, selectors.EVENT_READ, 'output')
                ready.register(process.stderr, selectors.EVENT_READ, 'error')
                while ready.get_map():
                    if time.monotonic() >= deadline:
                        raise FirmwareError('The firmware helper exceeded its deadline; device state is uncertain')
                    for key, _ in ready.select(0.1):
                        chunk = os.read(key.fileobj.fileno(), 4096)
                        if not chunk:
                            ready.unregister(key.fileobj)
                            continue
                        if key.data == 'error':
                            continue  # Structured failures belong on stdout.
                        buffer.extend(chunk)
                        if len(buffer) > 65536:
                            raise FirmwareError('The firmware helper returned an oversized response')
                        while b'\n' in buffer:
                            line, _, rest = buffer.partition(b'\n')
                            buffer = bytearray(rest)
                            value = json.loads(line)
                            if not isinstance(value, dict) or terminal is not None:
                                raise FirmwareError('The firmware helper returned an invalid message sequence')
                            if value.get('type') == 'progress':
                                try:
                                    progress(value)
                                except Exception:
                                    # A closed or broken presentation layer is
                                    # not a firmware abort instruction.
                                    pass
                            elif 'error' in value or 'result' in value:
                                terminal = value
                            else:
                                raise FirmwareError('The firmware helper returned an unknown message')
            process.wait(timeout=5)
            if buffer or terminal is None:
                raise FirmwareError('The firmware helper stopped without a complete result; device state is uncertain')
            if 'error' in terminal:
                raise FirmwareError(str(terminal['error']))
            result = terminal['result']
            if process.returncode or not isinstance(result, dict) or result.get('verified') is not True:
                raise FirmwareError('The firmware helper did not verify installation; read the mouse before continuing')
            return result
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()
