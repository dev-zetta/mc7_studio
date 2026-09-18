"""Linux control-endpoint CFU access, claiming only MC7 vendor interface 2.

HIDIOCSOUTPUT can use interrupt OUT when it exists. The original updater uses
control SET_REPORT instead, so firmware transfers need an explicit USB channel.
Opening this channel only checks identity/access. Claiming is a separate step
and must happen while the caller retains the ordinary mouse-session lock.
"""
from __future__ import annotations

import ctypes
from ctypes.util import find_library
from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import stat

from .transport import DeviceError

_U8 = ctypes.c_uint8
_U16 = ctypes.c_uint16
_U8P = ctypes.POINTER(_U8)
_VOID = ctypes.c_void_p
_NO_DEVICE = -4
_TIMEOUT = -7
_SYSFS_ROOT = Path("/sys/class/hidraw")


class _DeviceDescriptor(ctypes.Structure):
    _fields_ = [("bLength", _U8), ("bDescriptorType", _U8), ("bcdUSB", _U16),
                ("bDeviceClass", _U8), ("bDeviceSubClass", _U8),
                ("bDeviceProtocol", _U8), ("bMaxPacketSize0", _U8),
                ("idVendor", _U16), ("idProduct", _U16), ("bcdDevice", _U16),
                ("iManufacturer", _U8), ("iProduct", _U8),
                ("iSerialNumber", _U8), ("bNumConfigurations", _U8)]


class _EndpointDescriptor(ctypes.Structure):
    _fields_ = [("bLength", _U8), ("bDescriptorType", _U8),
                ("bEndpointAddress", _U8), ("bmAttributes", _U8),
                ("wMaxPacketSize", _U16), ("bInterval", _U8),
                ("bRefresh", _U8), ("bSynchAddress", _U8),
                ("extra", _U8P), ("extra_length", ctypes.c_int)]


class _InterfaceDescriptor(ctypes.Structure):
    _fields_ = [("bLength", _U8), ("bDescriptorType", _U8),
                ("bInterfaceNumber", _U8), ("bAlternateSetting", _U8),
                ("bNumEndpoints", _U8), ("bInterfaceClass", _U8),
                ("bInterfaceSubClass", _U8), ("bInterfaceProtocol", _U8),
                ("iInterface", _U8),
                ("endpoint", ctypes.POINTER(_EndpointDescriptor)),
                ("extra", _U8P), ("extra_length", ctypes.c_int)]


class _Interface(ctypes.Structure):
    _fields_ = [("altsetting", ctypes.POINTER(_InterfaceDescriptor)),
                ("num_altsetting", ctypes.c_int)]


class _ConfigDescriptor(ctypes.Structure):
    _fields_ = [("bLength", _U8), ("bDescriptorType", _U8),
                ("wTotalLength", _U16), ("bNumInterfaces", _U8),
                ("bConfigurationValue", _U8), ("iConfiguration", _U8),
                ("bmAttributes", _U8), ("MaxPower", _U8),
                ("interface", ctypes.POINTER(_Interface)),
                ("extra", _U8P), ("extra_length", ctypes.c_int)]


def _load_library():
    names = tuple(dict.fromkeys(
        name for name in (find_library("usb-1.0"), "libusb-1.0.so.0") if name
    ))
    library = None
    load_error = None
    for candidate in names:
        try:
            library = ctypes.CDLL(candidate)
            break
        except OSError as error:
            load_error = error
    if library is None:
        raise DeviceError("Firmware installation needs the libusb-1.0 library") from load_error
    try:
        signatures = {
            "libusb_init": ([ctypes.POINTER(_VOID)], ctypes.c_int),
            "libusb_exit": ([_VOID], None),
            "libusb_wrap_sys_device": ([_VOID, ctypes.c_ssize_t, ctypes.POINTER(_VOID)], ctypes.c_int),
            "libusb_close": ([_VOID], None),
            "libusb_get_device": ([_VOID], _VOID),
            "libusb_get_device_descriptor": ([_VOID, ctypes.POINTER(_DeviceDescriptor)], ctypes.c_int),
            "libusb_get_bus_number": ([_VOID], _U8),
            "libusb_get_device_address": ([_VOID], _U8),
            "libusb_get_port_numbers": ([_VOID, _U8P, ctypes.c_int], ctypes.c_int),
            "libusb_get_active_config_descriptor":
                ([_VOID, ctypes.POINTER(ctypes.POINTER(_ConfigDescriptor))], ctypes.c_int),
            "libusb_free_config_descriptor": ([ctypes.POINTER(_ConfigDescriptor)], None),
            "libusb_kernel_driver_active": ([_VOID, ctypes.c_int], ctypes.c_int),
            "libusb_detach_kernel_driver": ([_VOID, ctypes.c_int], ctypes.c_int),
            "libusb_attach_kernel_driver": ([_VOID, ctypes.c_int], ctypes.c_int),
            "libusb_claim_interface": ([_VOID, ctypes.c_int], ctypes.c_int),
            "libusb_release_interface": ([_VOID, ctypes.c_int], ctypes.c_int),
            "libusb_control_transfer":
                ([_VOID, _U8, _U8, _U16, _U16, _U8P, _U16, ctypes.c_uint], ctypes.c_int),
            "libusb_interrupt_transfer":
                ([_VOID, _U8, _U8P, ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_uint], ctypes.c_int),
            "libusb_error_name": ([ctypes.c_int], ctypes.c_char_p),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(library, name)
            function.argtypes = arguments
            function.restype = result
        return library
    except (AttributeError, OSError) as error:
        raise DeviceError("Firmware installation needs libusb-1.0 with libusb_wrap_sys_device support") from error


@dataclass(frozen=True)
class _USBIdentity:
    usb_path: Path
    bus: int
    address: int
    ports: tuple[int, ...]
    configuration: int

    @property
    def node(self):
        return f"/dev/bus/usb/{self.bus:03d}/{self.address:03d}"


def _identity_from_hidraw(control_path: str) -> _USBIdentity:
    path = Path(control_path)
    if path.parent != Path("/dev") or re.fullmatch(r"hidraw[0-9]+", path.name) is None:
        raise DeviceError("Firmware access requires the selected MC7 hidraw control path")
    try:
        hid = (_SYSFS_ROOT / path.name / "device").resolve(strict=True)
        interface = hid.parent
        usb = interface.parent

        def number(directory, name, base=10):
            return int((directory / name).read_text().strip(), base)

        if (number(interface, "bInterfaceNumber", 16),
                number(interface, "bAlternateSetting"),
                number(interface, "bInterfaceClass", 16)) != (2, 0, 3):
            raise DeviceError("Firmware access requires MC7 HID interface 2, alternate setting 0")
        if (number(usb, "idVendor", 16), number(usb, "idProduct", 16)) != (0x10F5, 0x502C):
            raise DeviceError("The firmware USB device is not the wired MC7 mouse")
        bus, address = number(usb, "busnum"), number(usb, "devnum")
        ports = tuple(int(part) for part in (usb / "devpath").read_text().strip().split("."))
        if not 1 <= bus <= 255 or not 1 <= address <= 127 or not 1 <= len(ports) <= 7 \
                or any(not 1 <= port <= 255 for port in ports):
            raise DeviceError("Invalid MC7 USB bus or port identity")
        endpoints = {}
        for endpoint in interface.glob("ep_*"):
            address_value = number(endpoint, "bEndpointAddress", 16)
            endpoints[address_value] = (number(endpoint, "bmAttributes", 16),
                                        number(endpoint, "wMaxPacketSize", 16))
        if endpoints != {0x03: (3, 64), 0x83: (3, 64)}:
            raise DeviceError("The MC7 firmware USB endpoints do not match the supported interface")
        return _USBIdentity(usb, bus, address, ports, number(usb, "bConfigurationValue"))
    except (OSError, ValueError) as error:
        raise DeviceError(f"Cannot verify the selected MC7 USB interface: {error}") from error


class LinuxFirmwareUSB:
    """An identity-bound USB channel; ``open`` never detaches or sends reports."""

    def __init__(self, control_path: str):
        self.control_path = control_path
        self.library = None
        self.context = _VOID()
        self.handle = _VOID()
        self.fd = None
        self.identity = None
        self.claimed = False
        self.detached = False

    def _check(self, result, action):
        if result < 0:
            name = self.library.libusb_error_name(result)
            detail = name.decode("ascii", errors="replace") if name else str(result)
            raise DeviceError(f"MC7 firmware USB {action} failed: {detail}")
        return result

    def _cleanup_failure(self, original):
        try:
            self.close()
        except DeviceError as cleanup:
            raise DeviceError(f"{original}; cleanup also failed: {cleanup}") from original

    def open(self):
        if self.handle:
            return self
        try:
            self.identity = _identity_from_hidraw(self.control_path)
            self.library = _load_library()
            self.fd = os.open(self.identity.node, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
            info = os.fstat(self.fd)
            expected_minor = (self.identity.bus - 1) * 128 + self.identity.address - 1
            if not stat.S_ISCHR(info.st_mode) or os.major(info.st_rdev) != 189 \
                    or os.minor(info.st_rdev) != expected_minor:
                raise DeviceError("The opened firmware USB node changed identity")
            self._check(self.library.libusb_init(ctypes.byref(self.context)), "initialization")
            self._check(self.library.libusb_wrap_sys_device(
                self.context, self.fd, ctypes.byref(self.handle)), "open")
            self._validate_opened_device()
            if _identity_from_hidraw(self.control_path) != self.identity:
                raise DeviceError("The mouse USB interface changed while opening firmware access")
            return self
        except PermissionError as error:
            self._cleanup_failure(error)
            raise DeviceError("MC7 firmware USB access denied. Open Device > Host integration "
                              "in MC7 Studio, or install the updated packaging/udev/70-swarm2-mc7.rules, "
                              "then reconnect the mouse.") from error
        except BaseException as error:
            self._cleanup_failure(error)
            raise

    def _validate_opened_device(self):
        device = self.library.libusb_get_device(self.handle)
        if not device:
            raise DeviceError("Cannot identify the opened MC7 firmware USB device")
        descriptor = _DeviceDescriptor()
        self._check(self.library.libusb_get_device_descriptor(device, ctypes.byref(descriptor)), "identity read")
        ports = (_U8 * 7)()
        count = self._check(self.library.libusb_get_port_numbers(device, ports, 7), "port identity read")
        # Linux wrap_sys_device creates a device without its sysfs parent
        # chain, so get_port_numbers normally returns zero for a wrapped FD.
        # The opened node's major/minor, VID/PID, bus/address and the selected
        # hidraw's sysfs identity (checked before and after open) still bind
        # this handle to the physical mouse. Reject any supplied wrong chain.
        if (descriptor.idVendor, descriptor.idProduct) != (0x10F5, 0x502C) \
                or self.library.libusb_get_bus_number(device) != self.identity.bus \
                or self.library.libusb_get_device_address(device) != self.identity.address \
                or not 0 <= count <= 7 or (count and tuple(ports[:count]) != self.identity.ports):
            raise DeviceError("The opened firmware USB device does not match the selected mouse")
        config = ctypes.POINTER(_ConfigDescriptor)()
        self._check(self.library.libusb_get_active_config_descriptor(device, ctypes.byref(config)), "configuration read")
        try:
            if not config or config.contents.bConfigurationValue != self.identity.configuration \
                    or not 1 <= config.contents.bNumInterfaces <= 32 or not config.contents.interface:
                raise DeviceError("The opened mouse has an unsupported USB configuration")
            matches = []
            for index in range(config.contents.bNumInterfaces):
                interface = config.contents.interface[index]
                if not 1 <= interface.num_altsetting <= 32 or not interface.altsetting:
                    raise DeviceError("The opened mouse has an invalid USB interface descriptor")
                for alternate in range(interface.num_altsetting):
                    entry = interface.altsetting[alternate]
                    if entry.bInterfaceNumber == 2 and entry.bAlternateSetting == 0:
                        matches.append(entry)
            if len(matches) != 1:
                raise DeviceError("The opened mouse lacks the firmware USB interface")
            entry = matches[0]
            if entry.bInterfaceClass != 3 or entry.bNumEndpoints != 2 or not entry.endpoint:
                raise DeviceError("The opened firmware USB interface is unsupported")
            endpoints = {(entry.endpoint[i].bEndpointAddress, entry.endpoint[i].bmAttributes,
                          entry.endpoint[i].wMaxPacketSize) for i in range(entry.bNumEndpoints)}
            if endpoints != {(0x03, 3, 64), (0x83, 3, 64)}:
                raise DeviceError("The opened firmware USB endpoints are unsupported")
        finally:
            if config:
                self.library.libusb_free_config_descriptor(config)

    def claim(self):
        if not self.handle:
            raise DeviceError("Open the MC7 firmware USB channel before claiming it")
        if self.claimed:
            return self
        try:
            active = self._check(self.library.libusb_kernel_driver_active(self.handle, 2), "driver check")
            if active not in (0, 1):
                raise DeviceError("Invalid MC7 firmware USB kernel-driver status")
            if active:
                self._check(self.library.libusb_detach_kernel_driver(self.handle, 2), "vendor-interface detach")
                self.detached = True
            self._check(self.library.libusb_claim_interface(self.handle, 2), "vendor-interface claim")
            self.claimed = True
            return self
        except BaseException as error:
            self._cleanup_failure(error)
            raise

    def _require_claim(self):
        if not self.handle or not self.claimed:
            raise DeviceError("Claim the MC7 firmware USB interface before transferring reports")

    def get_feature(self, report_id=0x2A, length=64):
        if type(report_id) is not int or type(length) is not int or (report_id, length) != (0x2A, 64):
            raise DeviceError("Only the CFU version feature is supported by the firmware USB channel")
        self._require_claim()
        data = (_U8 * 64)()
        count = self._check(self.library.libusb_control_transfer(
            self.handle, 0xA1, 0x01, 0x032A, 2, data, 64, 2000), "version read")
        if count not in (61, 64):
            raise DeviceError(f"Incomplete CFU version reply: {count}/64 bytes")
        return bytes(data[:count])

    def set_output(self, report):
        if not isinstance(report, bytes) or len(report) != 61 or report[0] not in (0x2A, 0x2D) \
                or (report[0] == 0x2D and any(report[17:])):
            raise DeviceError("Unsupported CFU USB output report")
        self._require_claim()
        # The vendor passes a 61-byte API buffer, but HID class drivers size
        # each output by its descriptor. The MC7 ignores a padded USB offer.
        length = 17 if report[0] == 0x2D else 61
        data = (_U8 * length).from_buffer_copy(report[:length])
        count = self._check(self.library.libusb_control_transfer(
            self.handle, 0x21, 0x09, 0x0200 | report[0], 2, data, length, 2000), "output transfer")
        if count != length:
            raise DeviceError(f"Incomplete CFU USB output transfer: {count}/{length} bytes; update state is uncertain")

    def read_input(self, timeout):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 <= timeout <= 5:
            raise DeviceError("Invalid firmware USB reply timeout")
        self._require_claim()
        data = (_U8 * 64)()
        actual = ctypes.c_int()
        # libusb timeout=0 waits indefinitely. A nonblocking drain must use 1ms.
        milliseconds = max(1, math.ceil(timeout * 1000))
        result = self.library.libusb_interrupt_transfer(
            self.handle, 0x83, data, 64, ctypes.byref(actual), milliseconds)
        if result != _TIMEOUT:
            self._check(result, "acknowledgement read")
        if not 0 <= actual.value <= 64:
            raise DeviceError("Invalid CFU USB acknowledgement length")
        return bytes(data[:actual.value])

    def close(self):
        errors = []
        if self.handle:
            for needed, name, action in (
                    (self.claimed, "libusb_release_interface", "vendor-interface release"),
                    (self.detached, "libusb_attach_kernel_driver", "vendor-interface reattach")):
                if needed:
                    try:
                        result = getattr(self.library, name)(self.handle, 2)
                        if result != _NO_DEVICE:
                            self._check(result, action)
                    except Exception as error:
                        errors.append(str(error))
            self.library.libusb_close(self.handle)
            self.handle = _VOID()
        self.claimed = self.detached = False
        if self.context:
            self.library.libusb_exit(self.context)
            self.context = _VOID()
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if errors:
            raise DeviceError("; ".join(errors))
