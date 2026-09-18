"""Non-exclusive macOS HIDAPI transport for a single directly connected MC7.

Run this in the bounded hardware helper, which owns HIDAPI's process-wide open
mode. cython-hidapi 0.15 exposes enumeration but not the Darwin open-mode or
location APIs. Bind those exported C APIs from the same extension, never a
second library with independent state, and fail before opening if unavailable.

API evidence:
https://github.com/trezor/cython-hidapi/blob/master/hid.pyx
https://github.com/libusb/hidapi/blob/master/mac/hidapi_darwin.h
https://github.com/libusb/hidapi/blob/master/mac/hid.c

Hardware acceptance on macOS remains outstanding.
"""

from contextlib import ExitStack
import ctypes
import os
from pathlib import Path
import platform
import tempfile
import time

from .descriptor import DescriptorError, MAX_DESCRIPTOR_BYTES, Usage, parse_descriptor
from .devices import load_hidapi
from .protocol import AckStatus, SUPPORTED_COMMAND_IDS, decode_acknowledgement
from .transport import DeviceError

MACOS_DEVICE_ID = "mc7-macos"
_VID, _PID = 0x10F5, 0x502C
_USB_BUS = 1
_BYTE_POINTER = ctypes.POINTER(ctypes.c_ubyte)


def _configure_library(library):
    """Resolve every symbol before initialization or opening a device."""
    signatures = {
        "hid_init": (ctypes.c_int, []),
        "hid_darwin_set_open_exclusive": (None, [ctypes.c_int]),
        "hid_darwin_get_open_exclusive": (ctypes.c_int, []),
        "hid_darwin_is_device_open_exclusive": (ctypes.c_int, [ctypes.c_void_p]),
        "hid_darwin_get_location_id": (
            ctypes.c_int, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]),
        "hid_open_path": (ctypes.c_void_p, [ctypes.c_char_p]),
        "hid_close": (None, [ctypes.c_void_p]),
        "hid_get_report_descriptor": (
            ctypes.c_int, [ctypes.c_void_p, _BYTE_POINTER, ctypes.c_size_t]),
        "hid_send_feature_report": (
            ctypes.c_int, [ctypes.c_void_p, _BYTE_POINTER, ctypes.c_size_t]),
        "hid_get_feature_report": (
            ctypes.c_int, [ctypes.c_void_p, _BYTE_POINTER, ctypes.c_size_t]),
        "hid_read_timeout": (
            ctypes.c_int, [ctypes.c_void_p, _BYTE_POINTER, ctypes.c_size_t, ctypes.c_int]),
        "hid_set_nonblocking": (ctypes.c_int, [ctypes.c_void_p, ctypes.c_int]),
    }
    for name, (result, arguments) in signatures.items():
        function = getattr(library, name)
        function.restype = result
        function.argtypes = arguments


class _NativeHandle:
    def __init__(self, library, pointer):
        self.library = library
        self.pointer = pointer

    def close(self):
        if self.pointer:
            self.library.hid_close(self.pointer)
            self.pointer = None

    def _pointer(self):
        if not self.pointer:
            raise DeviceError("MC7 HID handle is closed")
        return self.pointer

    def get_location_id(self) -> int:
        location = ctypes.c_uint32()
        result = self.library.hid_darwin_get_location_id(self._pointer(), ctypes.byref(location))
        if result != 0 or location.value == 0:
            raise DeviceError("macOS did not provide a physical USB location for the MC7")
        return location.value

    def get_report_descriptor(self) -> bytes:
        buffer = (ctypes.c_ubyte * (MAX_DESCRIPTOR_BYTES + 1))()
        count = self.library.hid_get_report_descriptor(self._pointer(), buffer, len(buffer))
        if not 0 < count <= MAX_DESCRIPTOR_BYTES:
            raise DeviceError("macOS returned an invalid MC7 report descriptor")
        return bytes(buffer[:count])

    def set_nonblocking(self, value: int):
        if self.library.hid_set_nonblocking(self._pointer(), value) != 0:
            raise DeviceError("Cannot set the MC7 event handle to nonblocking mode")

    def send_feature_report(self, report: bytes) -> int:
        buffer = (ctypes.c_ubyte * len(report)).from_buffer_copy(report)
        return self.library.hid_send_feature_report(self._pointer(), buffer, len(buffer))

    def get_feature_report(self, report_id: int, length: int, selector: int) -> bytes:
        buffer = (ctypes.c_ubyte * length)()
        buffer[0] = report_id
        buffer[1] = selector
        count = self.library.hid_get_feature_report(self._pointer(), buffer, length)
        if count < 0 or count > length:
            raise DeviceError("macOS MC7 feature read failed")
        return bytes(buffer[:count])

    def read(self, length: int, timeout_ms: int = 0) -> bytes:
        buffer = (ctypes.c_ubyte * length)()
        count = self.library.hid_read_timeout(self._pointer(), buffer, length, timeout_ms)
        if count < 0 or count > length:
            raise DeviceError("macOS MC7 event read failed")
        return bytes(buffer[:count])


class _DarwinHidapi:
    def __init__(self, module, library):
        self.module = module  # Retain its library lifecycle until all handles close.
        self.library = library
        try:
            _configure_library(library)
        except AttributeError as error:
            raise DeviceError(
                "This hidapi build lacks the macOS non-exclusive/location APIs; "
                "install a current hidapi USB extra. No MC7 handles were opened."
            ) from error
        if library.hid_init() != 0:
            raise DeviceError("macOS HIDAPI initialization failed")
        library.hid_darwin_set_open_exclusive(0)
        if library.hid_darwin_get_open_exclusive() != 0:
            raise DeviceError("Cannot guarantee non-exclusive MC7 access on macOS")

    def enumerate(self):
        return self.module.enumerate(_VID, _PID)

    def open_path(self, path: bytes):
        # Recheck before every open. This backend belongs to an isolated helper.
        if self.library.hid_darwin_get_open_exclusive() != 0:
            raise DeviceError("HIDAPI open mode changed; refusing to seize the MC7")
        pointer = self.library.hid_open_path(path)
        if not pointer:
            raise DeviceError("Cannot open the MC7 vendor interface on macOS")
        handle = _NativeHandle(self.library, pointer)
        if self.library.hid_darwin_is_device_open_exclusive(pointer) != 0:
            handle.close()
            raise DeviceError("HIDAPI did not open the MC7 in non-exclusive mode")
        return handle


def _load_backend():
    module = load_hidapi()
    extension_path = getattr(module, "__file__", None)
    if not extension_path:
        raise DeviceError("Cannot locate the native hidapi extension for macOS")
    try:
        library = ctypes.CDLL(extension_path)
    except OSError as error:
        raise DeviceError("Cannot load the installed hidapi native API on macOS") from error
    return _DarwinHidapi(module, library)


def _candidate_paths(records) -> dict[bytes, int]:
    """macOS enumerates application usages repeatedly for the same HID path."""
    paths: dict[bytes, int] = {}
    for record in records:
        if record.get("vendor_id") != _VID or record.get("product_id") != _PID:
            continue
        if record.get("bus_type") != _USB_BUS:
            continue
        interface = record.get("interface_number")
        if interface not in (1, 2):
            if interface is None or interface == -1:
                raise DeviceError("macOS could not identify the MC7 USB interface numbers")
            continue  # Never open the pointer-only interface.
        path = record.get("path")
        if not isinstance(path, bytes) or not path or b"\x00" in path:
            raise DeviceError("macOS returned an invalid MC7 device path")
        if path in paths and paths[path] != interface:
            raise DeviceError("Ambiguous MC7 collection/interface metadata on macOS")
        paths[path] = interface
    if len(paths) != 2 or sorted(paths.values()) != [1, 2]:
        raise DeviceError("Connect exactly one MC7 directly by USB with both vendor interfaces available")
    return paths


class MacOSTransport:
    """Same bounded-session API as HidrawTransport; macOS hardware untested."""

    def __init__(self, device_id: str = MACOS_DEVICE_ID):
        self.device_id = device_id
        self._stack = ExitStack()
        self._opened = False
        self.acknowledgements: list[str] = []

    def __enter__(self):
        if platform.system() != "Darwin":
            raise DeviceError("The macOS HIDAPI transport requires macOS")
        if self.device_id != MACOS_DEVICE_ID:
            raise DeviceError("Unknown macOS MC7 device selection")
        if self._opened:
            raise DeviceError("The MC7 session is already open")
        try:
            import fcntl
            lock_path = Path(tempfile.gettempdir()) / f"swarm2-{os.getuid()}-macos-mc7.lock"
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            self._stack.callback(os.close, fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            backend = _load_backend()
            paths = _candidate_paths(backend.enumerate())
            locations = set()
            for path, interface in paths.items():
                handle = backend.open_path(path)
                self._stack.callback(handle.close)
                locations.add(handle.get_location_id())
                summary = parse_descriptor(handle.get_report_descriptor())
                kind, length = ("feature", 64) if interface == 2 else ("input", 8)
                matches = [r for r in summary.reports if r.kind == kind and r.report_id == 0x10
                           and r.report_bytes == length and Usage(0xFF01, 1) in r.application_usages]
                if summary.warnings or len(matches) != 1:
                    raise DeviceError(f"MC7 USB interface {interface} has an unrecognized report descriptor")
                if interface == 2:
                    self.control = handle
                else:
                    self.events = handle
            if len(locations) != 1 or not next(iter(locations)):
                raise DeviceError("MC7 control and event interfaces do not share one physical USB location")
            self.location_id = next(iter(locations))
            self.events.set_nonblocking(1)
            self._opened = True
            return self
        except BlockingIOError as error:
            self._stack.close()
            raise DeviceError("Another swarm2 process is using the MC7") from error
        except (OSError, RuntimeError, DescriptorError) as error:
            self._stack.close()
            if isinstance(error, DeviceError):
                raise
            raise DeviceError(f"macOS MC7 access failed: {error}") from error
        except BaseException:
            self._stack.close()
            raise

    def __exit__(self, *exc):
        self._opened = False
        self._stack.close()

    def _require_open(self):
        if not self._opened:
            raise DeviceError("Open an MC7 session before sending reports")

    def send(self, report: bytes, *, ack_discriminator: int = 0xF2) -> None:
        self._require_open()
        if len(report) != 64 or report[0] != 0x10 or report[1] not in SUPPORTED_COMMAND_IDS:
            raise DeviceError("Unsupported feature command")
        if ack_discriminator not in (0xF2, 0x06):
            raise DeviceError("Unsupported acknowledgement channel")
        for _ in range(128):
            if not self.events.read(64, 0):
                break
        else:
            raise DeviceError("Mouse event queue did not settle; retry the operation")
        written = self.control.send_feature_report(report)
        if written != 64:
            raise DeviceError(f"Incomplete feature transfer: {written}/64 bytes")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            timeout_ms = max(1, min(50, int((deadline - time.monotonic()) * 1000)))
            event = bytes(self.events.read(64, timeout_ms))
            if len(event) != 8 or event[0] != 0x10 or event[2] != ack_discriminator or event[3] != report[1]:
                continue
            ack = decode_acknowledgement(event, target="mouse" if ack_discriminator == 0xF2 else "transmitter")
            self.acknowledgements.append(event.hex())
            if ack.status == AckStatus.ACCEPTED:
                return
            if ack.status != AckStatus.BUSY:
                raise DeviceError(f"Mouse rejected command with status {ack.status_code}")
        raise DeviceError("MC7 acknowledgement timed out; read settings again before retrying")

    def read_event(self, timeout: float = 0) -> bytes:
        """Read one vendor-interface input report for a specialized event pump."""
        self._require_open()
        if (type(timeout) not in (int, float) or isinstance(timeout, bool)
                or not 0 <= timeout <= 2):
            raise DeviceError("MC7 event timeout must be between 0 and 2 seconds")
        timeout_ms = 0 if timeout == 0 else max(1, int(timeout * 1000))
        return bytes(self.events.read(64, timeout_ms))

    def write_feature(self, report: bytes) -> int:
        """Write one complete feature report for a specialized serialized pump."""
        self._require_open()
        if not isinstance(report, bytes) or len(report) != 64:
            raise DeviceError("MC7 feature report must contain exactly 64 bytes")
        return self.control.send_feature_report(report)

    def _get_feature_now(self, selector: int) -> bytes:
        self._require_open()
        if type(selector) is not int or not 0 <= selector <= 255:
            raise DeviceError("Invalid feature response selector")
        return bytes(self.control.get_feature_report(0x10, 64, selector))

    def get_feature(self, selector: int) -> bytes:
        self._require_open()
        time.sleep(0.01)
        return self._get_feature_now(selector)

    def get_sensor(self) -> bytes:
        response = self.get_feature(0x10)
        if len(response) not in (48, 64):
            raise DeviceError(f"Incomplete sensor response: {len(response)} bytes")
        return response

    def exchange_image(self, report: bytes, *, delay_ms: int) -> bytes:
        self._require_open()
        from .image_commands import (
            CUSTOM_ICON_FIRST_STORE_SELECTOR, CUSTOM_ICON_LAST_STORE_SELECTOR,
            IMAGE_RESPONSE_SELECTORS, decode_image_response,
        )
        command = report[2] if len(report) >= 3 else -1
        if (len(report) != 64 or report[:2] != b"\x10\xa5"
                or (command not in (*range(62), 0xF1, 0xF2, 0xFF)
                    and not CUSTOM_ICON_FIRST_STORE_SELECTOR
                    <= command <= CUSTOM_ICON_LAST_STORE_SELECTOR)
                or type(delay_ms) is not int
                or delay_ms != (2500 if command == 0xFF else 30)):
            raise DeviceError("Unsupported background transfer or custom-icon transfer")
        count = self.control.send_feature_report(report)
        if count != 64:
            raise DeviceError(
                f"Incomplete background transfer or custom-icon transfer: {count}/64 bytes")
        time.sleep(delay_ms / 1000)
        response = b""
        for _ in range(16):
            response = self._get_feature_now(0xA2)
            time.sleep(0.03)
            if len(response) < 2:
                return decode_image_response(response, expected_command=report[2]).raw
            if response[1] in IMAGE_RESPONSE_SELECTORS:
                return decode_image_response(response, expected_command=report[2]).raw
        return decode_image_response(response, expected_command=report[2]).raw
