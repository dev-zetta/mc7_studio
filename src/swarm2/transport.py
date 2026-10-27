"""Linux wired/2.4 GHz hidraw transport; retains the mouse's input driver."""

from contextlib import ExitStack, contextmanager
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
               if d.vendor_id == 0x10F5 and d.product_id in (0x502C, 0x502E)
               and physical_id(d) == device_id]
    controls = [d for d in devices if d.interface_number == 2 and has_report(d, "feature", 64)]
    events = [d for d in devices if d.interface_number == 1 and has_report(d, "input", 8)]
    if (not device_id or len(controls) != 1 or len(events) != 1
            or controls[0].product_id != events[0].product_id):
        raise DeviceError("An MC7 mouse or 2.4 GHz receiver with matching control and event interfaces is required")
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
        self.product_id = control.product_id
        self.wireless = self.product_id == 0x502E
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

    @contextmanager
    def configuration_session(self, *, on_event=None):
        """Pair the vendor receiver reporting handshake, including on failure."""
        if on_event is not None and not callable(on_event):
            raise DeviceError("Receiver event handler must be callable")
        if not getattr(self, "wireless", False):
            yield
            return
        options = {"on_event": on_event} if on_event is not None else {}
        original = None
        try:
            self._set_receiver_reporting(False, **options)
            yield
        except BaseException as error:
            original = error
            raise
        finally:
            try:
                self._set_receiver_reporting(True, **options)
            except (OSError, DeviceError) as error:
                if original is None:
                    raise
                original.args = (f"{original}; restoring wireless reporting also failed: {error}",)

    def prepare_settings_readback(self):
        """A polling-rate write can undo the receiver's configuration downshift."""
        if getattr(self, "wireless", False):
            self._set_receiver_reporting(False, wait_for_rate=True)

    def _set_receiver_reporting(self, enabled, *, wait_for_rate=False, on_event=None):
        """Source FUN_180277e60: transient 0f/00 and 0f/07, never settings."""
        if not getattr(self, "wireless", False) or type(enabled) is not bool:
            raise DeviceError("Receiver reporting requires a wireless session and boolean state")
        if on_event is not None and not callable(on_event):
            raise DeviceError("Receiver event handler must be callable")
        for _ in range(128):
            event = self.read_event(0)
            if not event:
                break
            if on_event is not None and len(event) == 8 and event[0] == 0x10:
                on_event(event)
        else:
            raise DeviceError("Receiver event queue did not settle")
        report = bytes((0x10, 0x0F, 7 if enabled else 0)) + bytes(61)
        if self.write_feature(report) != 64:
            raise DeviceError("Incomplete receiver reporting transfer")
        deadline = time.monotonic() + 2
        accepted, rate_event = False, False
        for _ in range(128):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            event = self.read_event(min(.1, remaining))
            if len(event) != 8 or event[0] != 0x10:
                continue
            if event[2:4] == b"\x05\x05":
                rate_event = True
            if event[2:4] == b"\x06\x0f":
                self.acknowledgements.append(event.hex())
                if event[4] == 0:
                    accepted = True
                elif event[4] != 2:
                    raise DeviceError(f"Receiver reporting rejected with status {event[4]}")
            elif event[2:4] != b"\x05\x05" and on_event is not None:
                # Specialized pumps must not lose completion notifications
                # while the receiver changes its reporting rate.
                on_event(event)
            if accepted and (not (enabled or wait_for_rate) or rate_event):
                time.sleep(.3)
                return
        raise DeviceError("Receiver reporting acknowledgement or rate event timed out")

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
        self._feature_read = None
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
                if report[:3] == b"\x10\x1c\x00":
                    self._feature_read = report
                return
            if ack.status != AckStatus.BUSY:
                raise DeviceError(f"Mouse rejected command with status {ack.status_code}")
        if getattr(self, "wireless", False):
            raise DeviceError(f"Wireless MC7 acknowledgement timed out for command 0x{report[1]:02x}. Wake the mouse, check 2.4 GHz mode and read settings again before retrying")
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
        self._feature_read = None
        count = fcntl.ioctl(
            self.control, self._request(6, 64), bytearray(report), True)
        if count == 64 and report[:3] == b"\x10\x1c\x00":
            # Specialized event pumps await their own ACKs before GET. Keep
            # their profile/page identity just like the ordinary send path.
            self._feature_read = report
        return count

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
        if not getattr(self, "wireless", False):
            return self._get_feature_now(selector)
        # A receiver ACK can precede the radio response. GET may still contain
        # the previous selector or an empty cache. Poll GET only: replaying SET
        # could repeat a command. The codecs validate the complete matched reply.
        deadline = time.monotonic() + 2
        for _ in range(128):
            response = self._get_feature_now(selector)
            if (response[:2] == bytes((0x10, selector))
                    and self._matches_feature_identity(response, selector)):
                # The receiver pads LCD/page records to the HID envelope. Keep
                # the same strict codecs as USB and discard only a zero tail.
                length = {0x25: 61, 0x29: 54}.get(selector)
                if length is not None and len(response) == 64:
                    if any(response[length:]):
                        raise DeviceError("Unexpected data after the wireless settings reply")
                    return response[:length]
                return response
            if time.monotonic() >= deadline:
                break
            time.sleep(0.015)
        raise DeviceError("No matching reply from the wireless mouse. Wake it, check 2.4 GHz mode and read again.")

    def _matches_feature_identity(self, response: bytes, selector: int) -> bool:
        """A matching selector can still belong to a prior profile or LCD page."""
        request = getattr(self, "_feature_read", None)
        if (request is None or request[3] != selector
                or response[2:3] != b"\x00"):
            return True  # Let the codec reject malformed/status-error replies.
        if selector == 0x29 and len(response) >= 8:
            return response[6:8] == request[4:6]
        if selector in (0x10, 0x15, 0x16, 0x25, 0x2A) and len(response) >= 4:
            return response[3] == request[4]
        return True

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
