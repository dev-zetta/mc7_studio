"""Dedicated MC7 CFU endpoint access, sharing the normal device-session lock.

CFU replies belong to interface 2, unlike ordinary settings ACKs on interface
1. The version feature read sends no firmware offer, data or reset command.
"""

import ctypes
import os
import platform
import select
import struct
import math

from .descriptor import parse_descriptor
from .transport import DeviceError, HidrawTransport


def validate_cfu_descriptor(descriptor):
    summary = parse_descriptor(descriptor).to_dict()
    expected = {("feature", 0x2B, 61), ("feature", 0x2A, 61),
                ("output", 0x2A, 61), ("output", 0x2D, 17),
                ("input", 0x2C, 17), ("input", 0x2D, 17)}
    actual = {(r["kind"], r["report_id"], r["report_bytes"])
              for r in summary["reports"]
              if {"page": 0xFF0B, "usage": 0x104} in r["application_usages"]}
    if summary["warnings"] or not expected.issubset(actual):
        raise DeviceError("The connected mouse has an unsupported firmware-update interface")


class FirmwareTransport:
    def __init__(self, device_id):
        self.device_id = device_id
        self.normal = None
        self._usb = None

    def __enter__(self):
        if platform.system() == "Darwin":
            from .macos import MacOSTransport
            normal = MacOSTransport(self.device_id)
        elif platform.system() == "Linux":
            normal = HidrawTransport(self.device_id)
        else:
            raise DeviceError("MC7 firmware access requires Linux or macOS")
        self.normal = normal.__enter__()
        try:
            validate_cfu_descriptor(self._descriptor())
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        try:
            if self._usb is not None:
                usb, self._usb = self._usb, None
                try:
                    usb.close()
                except Exception as error:
                    # Preserve the original transfer failure, but expose a
                    # separate interface-restoration failure to the caller.
                    if len(args) > 1 and args[1] is not None:
                        original = args[1]
                        original.args = (f"{original}; USB interface cleanup: {error}",)
                    else:
                        raise
        finally:
            if self.normal is not None:
                normal, self.normal = self.normal, None
                normal.__exit__(*args)

    def _open_usb(self):
        from .firmware_usb import LinuxFirmwareUSB
        # The normal session has already verified this open HID handle and
        # holds the per-mouse lock. Resolve its node again rather than relying
        # on a remembered hidraw number across device reconnections.
        usb = LinuxFirmwareUSB(os.readlink(f"/proc/self/fd/{self._control()}"))
        usb.open()
        return usb

    def check_update_access(self):
        """Check Linux USB permissions/identity without detaching any driver."""
        if isinstance(self._control(), int):
            usb = self._open_usb()
            usb.close()

    def begin_update(self):
        """Claim only the vendor interface when installation actually starts."""
        if isinstance(self._control(), int) and self._usb is None:
            usb = self._open_usb()
            # Retain ownership before claim so partial failures are cleaned
            # up by the enclosing session, with its lock still held.
            self._usb = usb
            usb.claim()

    def _control(self):
        if self.normal is None:
            raise DeviceError("Open the mouse firmware interface first")
        return self.normal.control

    def _descriptor(self):
        control = self._control()
        if not isinstance(control, int):
            return control.get_report_descriptor()
        import fcntl
        size = bytearray(4)
        fcntl.ioctl(control, (2 << 30) | (4 << 16) | (ord("H") << 8) | 1, size, True)
        length, = struct.unpack("=I", size)
        if not 0 < length <= 4096:
            raise DeviceError("Invalid firmware interface descriptor length")
        data = bytearray(struct.pack("=I", length) + bytes(4096))
        fcntl.ioctl(control, (2 << 30) | (4100 << 16) | (ord("H") << 8) | 2, data, True)
        return bytes(data[4:4 + length])

    def get_feature(self, report_id=0x2A, length=64):
        if type(report_id) is not int or type(length) is not int or (report_id, length) != (0x2A, 64):
            raise DeviceError("Only the read-only CFU version feature is supported here")
        control = self._control()
        if self._usb is not None:
            return self._usb.get_feature(report_id, length)
        if isinstance(control, int):
            import fcntl
            data = bytearray((report_id,)) + bytearray(length - 1)
            count = fcntl.ioctl(control, self.normal._request(7, length), data, True)
        else:
            data = (ctypes.c_ubyte * length)()
            data[0] = report_id
            count = control.library.hid_get_feature_report(control._pointer(), data, length)
        if count not in (61, 64):
            raise DeviceError(f"Incomplete CFU version reply: {count}/{length} bytes")
        return bytes(data[:count])

    def read_event(self, timeout):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 <= timeout <= 5:
            raise DeviceError("Invalid firmware reply timeout")
        control = self._control()
        if self._usb is not None:
            return self._usb.read_input(timeout)
        if isinstance(control, int):
            if not select.select([control], [], [], timeout)[0]:
                return b""
            event = os.read(control, 64)
            if not event:
                raise DeviceError("The mouse disconnected during firmware communication")
            return event
        return bytes(control.read(64, max(1, int(timeout * 1000))))

    read_input = read_event

    def set_output(self, report):
        # Preserve the vendor API's 61-byte envelope for codec validation.
        # Native transfers use the descriptor size: offer 17, content 61.
        if (not isinstance(report, bytes) or len(report) != 61
                or report[0] not in (0x2A, 0x2D)
                or (report[0] == 0x2D and any(report[17:]))):
            raise DeviceError("Unsupported CFU output report")
        control = self._control()
        if isinstance(control, int):
            # Linux HIDIOCSOUTPUT prefers interrupt OUT on this device; it
            # does not reproduce the vendor's HidD_SetOutputReport transfer.
            if self._usb is None:
                raise DeviceError("Begin the firmware USB session before sending an offer")
            self._usb.set_output(report)
            return
        else:
            try:
                function = control.library.hid_send_output_report
            except AttributeError as error:
                raise DeviceError("This HIDAPI build cannot send firmware output reports") from error
            function.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_size_t]
            function.restype = ctypes.c_int
            length = 17 if report[0] == 0x2D else 61
            data = (ctypes.c_ubyte * length).from_buffer_copy(report[:length])
            count = function(control._pointer(), data, length)
        if count != length:
            raise DeviceError(f"Incomplete CFU output transfer: {count}/{length} bytes; update state is uncertain")
