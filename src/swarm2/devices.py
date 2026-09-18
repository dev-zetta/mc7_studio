"""Discover supported MC7 HID interfaces without sending device reports."""

from dataclasses import asdict, dataclass, field
import importlib
import os
from pathlib import Path
import platform
from typing import Any

from .descriptor import DescriptorError, MAX_DESCRIPTOR_BYTES, parse_descriptor

MC7_VENDOR_ID = 0x10F5
MC7_PRODUCT_IDS = frozenset((0x502C, 0x502E))


class BackendUnavailable(RuntimeError):
    pass


def is_mc7(vendor_id: int, product_id: int) -> bool:
    return vendor_id == MC7_VENDOR_ID and product_id in MC7_PRODUCT_IDS


@dataclass
class Device:
    path: str
    vendor_id: int
    product_id: int
    product_name: str
    backend: str
    interface_number: int | None = None
    serial_number: str | None = None
    usage_page: int | None = None
    usage: int | None = None
    physical_path: str | None = None
    descriptor_path: str | None = None
    descriptor: dict[str, Any] | None = None
    device_node_exists: bool | None = None
    device_node_readable: bool | None = None
    device_node_writable: bool | None = None
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Enumeration:
    backend: str
    devices: list[Device]
    issues: list[str] = field(default_factory=list)


def read_descriptor(path: Path) -> bytes:
    with path.open("rb") as handle:
        data = handle.read(MAX_DESCRIPTOR_BYTES + 1)
    if len(data) > MAX_DESCRIPTOR_BYTES:
        raise DescriptorError(f"descriptor exceeds {MAX_DESCRIPTOR_BYTES} bytes")
    return data


def _interface_number(device_directory: Path) -> int | None:
    resolved = device_directory.resolve()
    for directory in (resolved, *list(resolved.parents)[:8]):
        try:
            return int((directory / "bInterfaceNumber").read_text().strip(), 16)
        except (OSError, ValueError):
            continue
    return None


def enumerate_sysfs(sysfs_root: Path = Path("/sys/class/hidraw"),
                    dev_root: Path = Path("/dev")) -> Enumeration:
    """Read Linux kernel metadata, including descriptors, without hidraw open."""
    result = Enumeration("sysfs", [])
    if not sysfs_root.is_dir():
        result.issues.append(f"HID sysfs directory is unavailable: {sysfs_root}")
        return result
    try:
        entries = sorted(sysfs_root.glob("hidraw*"), key=lambda path: path.name)
    except OSError as error:
        result.issues.append(f"Cannot list HID sysfs directory: {error}")
        return result
    for entry in entries:
        directory = entry / "device"
        try:
            metadata = dict(line.split("=", 1) for line in
                            (directory / "uevent").read_text().splitlines() if "=" in line)
            _, vendor, product = metadata["HID_ID"].split(":")
            vendor_id, product_id = int(vendor, 16), int(product, 16)
        except (OSError, KeyError, ValueError) as error:
            result.issues.append(f"Cannot identify {entry.name}: {error}")
            continue
        if not is_mc7(vendor_id, product_id):
            continue
        node = dev_root / entry.name
        descriptor_path = directory / "report_descriptor"
        device = Device(
            path=str(node), vendor_id=vendor_id, product_id=product_id,
            product_name=metadata.get("HID_NAME", "Turtle Beach Command Series MC7"),
            backend="sysfs", interface_number=_interface_number(directory),
            serial_number=metadata.get("HID_UNIQ") or None,
            physical_path=metadata.get("HID_PHYS"),
            descriptor_path=str(descriptor_path), device_node_exists=node.exists(),
            device_node_readable=os.access(node, os.R_OK),
            device_node_writable=os.access(node, os.W_OK),
        )
        try:
            device.descriptor = parse_descriptor(read_descriptor(descriptor_path)).to_dict()
        except (OSError, DescriptorError) as error:
            device.issues.append(f"Cannot summarize report descriptor: {error}")
        result.devices.append(device)
    return result


def load_hidapi() -> Any:
    try:
        module = importlib.import_module("hid")
    except (ImportError, OSError) as error:
        raise BackendUnavailable(
            "hidapi is unavailable; install the USB extra with: pip install '.[usb]'"
        ) from error
    if not callable(getattr(module, "enumerate", None)) or not callable(getattr(module, "device", None)):
        raise BackendUnavailable("The imported 'hid' module is not the supported hidapi package")
    return module


def enumerate_hidapi() -> Enumeration:
    """Use HIDAPI on supported platforms; discovery opens no HID handle."""
    module = load_hidapi()
    result = Enumeration("hidapi", [])
    for product_id in sorted(MC7_PRODUCT_IDS):
        try:
            records = module.enumerate(MC7_VENDOR_ID, product_id)
        except (OSError, RuntimeError) as error:
            raise BackendUnavailable(f"hidapi enumeration failed: {error}") from error
        for record in records:
            if not is_mc7(record.get("vendor_id"), record.get("product_id")):
                continue
            raw_path = record.get("path", b"")
            path = os.fsdecode(raw_path)
            result.devices.append(Device(
                path=path, vendor_id=record["vendor_id"], product_id=record["product_id"],
                product_name=record.get("product_string") or "Turtle Beach Command Series MC7",
                backend="hidapi", interface_number=record.get("interface_number"),
                serial_number=record.get("serial_number") or None,
                usage_page=record.get("usage_page"), usage=record.get("usage"),
            ))
    result.devices.sort(key=lambda device: (device.product_id, device.path,
                                           device.usage_page or 0, device.usage or 0))
    return result


def enumerate_devices(backend: str = "auto") -> Enumeration:
    selected = ("sysfs" if platform.system() == "Linux" else "hidapi") if backend == "auto" else backend
    if selected == "sysfs":
        if platform.system() != "Linux":
            raise BackendUnavailable("The sysfs backend requires Linux; use --backend hidapi")
        return enumerate_sysfs()
    if selected == "hidapi":
        return enumerate_hidapi()
    raise ValueError(f"Unknown backend: {backend}")
