"""Linux hidraw transport; does not detach the mouse's input driver."""

from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import select
import struct
import tempfile
import time

from .devices import Device, enumerate_sysfs
from .protocol import AckStatus, SUPPORTED_COMMAND_IDS, decode_acknowledgement


class DeviceError(RuntimeError):
    pass


def physical_id(device: Device) -> str:
    return (device.physical_path or "").rsplit("/input", 1)[0]


def has_report(device: Device, kind: str, length: int) -> bool:
    return any(report["kind"] == kind and report["report_id"] == 0x10
               and report["report_bytes"] == length
               and {"page": 0xFF01, "usage": 1} in report["application_usages"]
               for report in (device.descriptor or {}).get("reports", []))


def select_interfaces(device_id: str) -> tuple[Device, Device]:
    devices = [d for d in enumerate_sysfs().devices
               if d.product_id == 0x502C and physical_id(d) == device_id]
    controls = [d for d in devices if d.interface_number == 2 and has_report(d, "feature", 64)]
    events = [d for d in devices if d.interface_number == 1 and has_report(d, "input", 8)]
    if not device_id or len(controls) != 1 or len(events) != 1:
        raise DeviceError("A USB-connected MC7 with matching control and event interfaces is required")
    return controls[0], events[0]


class HidrawTransport:
    """One serialized transaction session. Run in the bounded helper process."""

    def __init__(self, device_id: str):
        self.device_id = device_id
        self._stack = ExitStack()
        self.acknowledgements: list[str] = []

    def __enter__(self):
        import fcntl
        control, events = select_interfaces(self.device_id)
        try:
            lock_name = hashlib.sha256(self.device_id.encode()).hexdigest()[:24]
            lock_path = Path(tempfile.gettempdir()) / f"swarm2-{os.getuid()}-{lock_name}.lock"
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            self._stack.callback(os.close, fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.events = os.open(events.path, os.O_RDONLY | os.O_NONBLOCK)
            self._stack.callback(os.close, self.events)
            self.control = os.open(control.path, os.O_RDWR | os.O_NONBLOCK)
            self._stack.callback(os.close, self.control)
            self._verify_handle(self.control, control)
            self._verify_handle(self.events, events)
        except PermissionError as error:
            self._stack.close()
            raise DeviceError("MC7 access denied. Open Device > Host integration in MC7 Studio, or install packaging/udev/70-swarm2-mc7.rules, then reconnect the mouse.") from error
        except BlockingIOError as error:
            self._stack.close()
            raise DeviceError("Another swarm2 process is using this mouse") from error
        except BaseException:
            self._stack.close()
            raise
        return self

    def __exit__(self, *exc):
        self._stack.close()

    @staticmethod
    def _request(number: int, length: int) -> int:
        return (3 << 30) | (length << 16) | (ord("H") << 8) | number

    @staticmethod
    def _verify_handle(fd: int, device: Device) -> None:
        """Catch hidraw node reuse after unplug between discovery and open."""
        import fcntl
        info = bytearray(8)
        fcntl.ioctl(fd, (2 << 30) | (8 << 16) | (ord("H") << 8) | 3, info, True)
        bus, vendor, product = struct.unpack("=IHH", info)
        location = bytearray(256)
        fcntl.ioctl(fd, (2 << 30) | (256 << 16) | (ord("H") << 8) | 5, location, True)
        physical = bytes(location).split(b"\0", 1)[0].decode("utf-8", errors="strict")
        if (bus, vendor, product, physical) != (3, device.vendor_id, device.product_id, device.physical_path):
            raise DeviceError("The USB mouse changed during connection. Refresh devices and read again.")

    def send(self, report: bytes, *, ack_discriminator: int = 0xF2) -> None:
        import fcntl
        if len(report) != 64 or report[0] != 0x10 or report[1] not in SUPPORTED_COMMAND_IDS:
            raise DeviceError("Unsupported feature command")
        if ack_discriminator not in (0xF2, 0x06):
            raise DeviceError("Unsupported acknowledgement channel")
        # Drop prior events on our own handle; never retain keyboard reports.
        for _ in range(128):
            if not select.select([self.events], [], [], 0)[0]:
                break
            os.read(self.events, 64)
        else:
            raise DeviceError("Mouse event queue did not settle; retry the operation")
        written = fcntl.ioctl(self.control, self._request(6, 64), bytearray(report), True)
        if written != 64:
            raise DeviceError(f"Incomplete feature transfer: {written}/64 bytes")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            ready = select.select([self.events], [], [], max(0, deadline - time.monotonic()))[0]
            if not ready:
                break
            event = os.read(self.events, 64)
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
        if (type(timeout) not in (int, float) or isinstance(timeout, bool)
                or not 0 <= timeout <= 2):
            raise DeviceError("MC7 event timeout must be between 0 and 2 seconds")
        if not select.select([self.events], [], [], timeout)[0]:
            return b""
        event = os.read(self.events, 64)
        if not event:
            raise DeviceError("Mouse disconnected while reading input events")
        return event

    def write_feature(self, report: bytes) -> int:
        """Write one complete feature report for a specialized serialized pump."""
        import fcntl
        if not isinstance(report, bytes) or len(report) != 64:
            raise DeviceError("MC7 feature report must contain exactly 64 bytes")
        return fcntl.ioctl(
            self.control, self._request(6, 64), bytearray(report), True)

    def _get_feature_now(self, selector: int) -> bytes:
        """Read a seeded feature report without the generic command delay."""
        import fcntl
        if type(selector) is not int or not 0 <= selector <= 255:
            raise DeviceError("Invalid feature response selector")
        response = bytearray(64)
        response[:2] = bytes((0x10, selector))
        count = fcntl.ioctl(self.control, self._request(7, 64), response, True)
        if not 1 <= count <= 64:
            raise DeviceError(f"Invalid feature response length: {count} bytes")
        return bytes(response[:count])

    def get_feature(self, selector: int) -> bytes:
        time.sleep(0.01)
        return self._get_feature_now(selector)

    def get_sensor(self) -> bytes:
        response = self.get_feature(0x10)
        if len(response) not in (48, 64):
            raise DeviceError(f"Incomplete sensor response: {len(response)} bytes")
        return response

    def exchange_image(self, report: bytes, *, delay_ms: int) -> bytes:
        """One bounded image SET/GET pair; this path does not wait for F2 OSD."""
        import fcntl
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
        count = fcntl.ioctl(self.control, self._request(6, 64), bytearray(report), True)
        if count != 64:
            raise DeviceError(
                f"Incomplete background transfer or custom-icon transfer: {count}/64 bytes")
        time.sleep(delay_ms / 1000)
        # Swarm II polls GET without replaying SET. Unknown selectors can be a
        # previous feature result; a recognized result with the wrong phase is
        # terminal because repeating that GET cannot prove this packet landed.
        response = b""
        for _ in range(16):
            response = self._get_feature_now(0xA2)
            time.sleep(0.03)
            if len(response) < 2:
                return decode_image_response(response, expected_command=report[2]).raw
            if response[1] in IMAGE_RESPONSE_SELECTORS:
                return decode_image_response(response, expected_command=report[2]).raw
        return decode_image_response(response, expected_command=report[2]).raw
