"""Native HIDAPI transport for one directly connected MC7 on Windows."""

from contextlib import ExitStack
import ctypes
import hashlib
import os
from pathlib import Path
import platform

from .descriptor import DescriptorError, MAX_DESCRIPTOR_BYTES, Usage, parse_descriptor
from .devices import load_hidapi
from .macos import MacOSTransport
from .transport import DeviceError

WINDOWS_DEVICE_ID = "mc7-windows"
_VID, _PID = 0x10F5, 0x502C
_USB_BUS = 1
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def _configure_windows_api(hid_library, kernel32):
    hid_library.HidD_SetFeature.restype = ctypes.c_ubyte
    hid_library.HidD_SetFeature.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
    hid_library.HidD_GetFeature.restype = ctypes.c_ubyte
    hid_library.HidD_GetFeature.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]


class _WindowsControlHandle:
    def __init__(self, hid_library, kernel32, handle, description):
        self.hid_library = hid_library
        self.kernel32 = kernel32
        self.handle = handle
        self.description = description

    def close(self):
        if self.handle is not None:
            self.kernel32.CloseHandle(self.handle)
            self.handle = None

    def _handle(self):
        if self.handle is None:
            raise DeviceError("MC7 HID handle is closed")
        return self.handle

    def get_report_descriptor(self) -> bytes:
        return self.description

    def send_feature_report(self, report: bytes) -> int:
        buffer = (ctypes.c_ubyte * len(report)).from_buffer_copy(report)
        if not self.hid_library.HidD_SetFeature(self._handle(), buffer, len(buffer)):
            raise DeviceError("Windows MC7 feature write failed")
        return len(report)

    def get_feature_report(self, report_id: int, length: int, selector: int) -> bytes:
        buffer = (ctypes.c_ubyte * length)()
        buffer[0] = report_id
        buffer[1] = selector
        if not self.hid_library.HidD_GetFeature(self._handle(), buffer, length):
            raise DeviceError("Windows MC7 feature read failed")
        return bytes(buffer)


class _PythonEventHandle:
    def __init__(self, device, description):
        self.device = device
        self.description = description

    def close(self):
        if self.device is not None:
            self.device.close()
            self.device = None

    def get_report_descriptor(self):
        return self.description

    def set_nonblocking(self, value):
        if self.device is None:
            raise DeviceError("MC7 HID handle is closed")
        self.device.set_nonblocking(value)

    def read(self, length, timeout_ms=0):
        if self.device is None:
            raise DeviceError("MC7 HID handle is closed")
        return bytes(self.device.read(length, timeout_ms))


class _WindowsHidapi:
    def __init__(self, module, hid_library, kernel32):
        self.module = module
        self.hid_library = hid_library
        self.kernel32 = kernel32
        try:
            _configure_windows_api(hid_library, kernel32)
        except AttributeError as error:
            raise DeviceError(
                "Windows does not provide the HID APIs required for MC7 access"
            ) from error

    def enumerate(self):
        return self.module.enumerate(_VID, _PID)

    def open_path(self, path: bytes, interface: int):
        probe = self.module.device()
        try:
            probe.open_path(path)
            description = bytes(probe.get_report_descriptor())
            if not 0 < len(description) <= MAX_DESCRIPTOR_BYTES:
                raise DeviceError("Windows returned an invalid MC7 report descriptor")
            if interface == 1:
                return _PythonEventHandle(probe, description)
        except BaseException:
            probe.close()
            raise
        probe.close()
        path_text = os.fsdecode(path)
        handle = self.kernel32.CreateFileW(
            path_text, 0xC0000000, 0x00000003, None, 3, 0, None)
        if handle in (None, _INVALID_HANDLE_VALUE):
            raise DeviceError("Cannot open the MC7 vendor interface on Windows")
        return _WindowsControlHandle(
            self.hid_library, self.kernel32, handle, description)


def _load_backend():
    module = load_hidapi()
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        raise DeviceError("The Windows HID transport requires Windows")
    try:
        hid_library = loader("hid.dll", use_last_error=True)
        kernel32 = loader("kernel32.dll", use_last_error=True)
    except OSError as error:
        raise DeviceError("Cannot load the native Windows HID API") from error
    return _WindowsHidapi(module, hid_library, kernel32)


def _candidate_paths(records) -> dict[bytes, int]:
    paths: dict[bytes, int] = {}
    for record in records:
        if record.get("vendor_id") != _VID or record.get("product_id") != _PID:
            continue
        bus = record.get("bus_type")
        if bus is not None and bus != _USB_BUS:
            continue
        interface = record.get("interface_number")
        if interface not in (1, 2):
            if interface is None or interface == -1:
                raise DeviceError("Windows could not identify the MC7 USB interface numbers")
            continue
        path = record.get("path")
        if isinstance(path, str):
            path = os.fsencode(path)
        if not isinstance(path, bytes) or not path or b"\x00" in path:
            raise DeviceError("Windows returned an invalid MC7 device path")
        if path in paths and paths[path] != interface:
            raise DeviceError("Ambiguous MC7 collection/interface metadata on Windows")
        paths[path] = interface
    if len(paths) != 2 or sorted(paths.values()) != [1, 2]:
        raise DeviceError(
            "Connect exactly one MC7 directly by USB with both vendor interfaces available"
        )
    return paths


def _lock_path() -> Path:
    root = os.environ.get("LOCALAPPDATA")
    directory = Path(root) if root and Path(root).is_absolute() else Path.home() / "AppData" / "Local"
    return directory / "MC7 Studio" / "locks" / "mc7.lock"


class WindowsTransport(MacOSTransport):
    """Windows implementation of the bounded MC7 HID session API."""

    def __init__(self, device_id: str = WINDOWS_DEVICE_ID):
        self.device_id = device_id
        self._stack = ExitStack()
        self._opened = False
        self.acknowledgements: list[str] = []

    def __enter__(self):
        if platform.system() != "Windows":
            raise DeviceError("The Windows HIDAPI transport requires Windows")
        if self.device_id != WINDOWS_DEVICE_ID:
            raise DeviceError("Unknown Windows MC7 device selection")
        if self._opened:
            raise DeviceError("The MC7 session is already open")
        try:
            import msvcrt
            lock_path = _lock_path()
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            self._stack.callback(os.close, fd)
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise DeviceError("Another swarm2 process is using the MC7") from error
            self._stack.callback(msvcrt.locking, fd, msvcrt.LK_UNLCK, 1)

            backend = _load_backend()
            paths = _candidate_paths(backend.enumerate())
            for path, interface in paths.items():
                handle = backend.open_path(path, interface)
                self._stack.callback(handle.close)
                summary = parse_descriptor(handle.get_report_descriptor())
                kind, length = ("feature", 64) if interface == 2 else ("input", 8)
                matches = [
                    report for report in summary.reports
                    if report.kind == kind and report.report_id == 0x10
                    and report.report_bytes == length
                    and Usage(0xFF01, 1) in report.application_usages
                ]
                if summary.warnings or len(matches) != 1:
                    raise DeviceError(
                        f"MC7 USB interface {interface} has an unrecognized report descriptor"
                    )
                if interface == 2:
                    self.control = handle
                else:
                    self.events = handle
            identity = hashlib.sha256(b"\0".join(sorted(paths))).hexdigest()[:16]
            self.location_id = f"windows:{identity}"
            self.events.set_nonblocking(1)
            self._opened = True
            return self
        except (OSError, RuntimeError, DescriptorError) as error:
            self._stack.close()
            if isinstance(error, DeviceError):
                raise
            raise DeviceError(f"Windows MC7 access failed: {error}") from error
        except BaseException:
            self._stack.close()
            raise
