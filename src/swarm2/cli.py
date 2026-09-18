"""MC7 discovery, native settings/status reads, and the desktop interface."""

import argparse
import json
from pathlib import Path
import platform
import sys

from . import __version__
from .descriptor import DescriptorError, parse_descriptor
from .devices import BackendUnavailable, Enumeration, enumerate_devices, load_hidapi, read_descriptor


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"swarm2 {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("devices", "List recognized MC7 HID interfaces"),
                            ("doctor", "Check discovery and USB access prerequisites")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--backend", choices=("auto", "sysfs", "hidapi"), default="auto")
        command.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    descriptor = commands.add_parser("descriptor", help="Summarize a binary HID report descriptor")
    descriptor.add_argument("path", nargs="?", help="Binary descriptor file or /dev/hidrawN sysfs alias")
    descriptor.add_argument("--hex", dest="hex_data", help="Descriptor bytes as hexadecimal text")
    descriptor.add_argument("--json", action="store_true")
    commands.add_parser("gui", help="Open the desktop configurator (requires the GUI extra)")
    read = commands.add_parser("read", help="Read settings and status from a USB-connected MC7")
    read.add_argument("--profile", type=int, choices=range(1, 6), default=1)
    read.add_argument("--device", help="Physical device ID from the desktop interface")
    read.add_argument("--json", action="store_true")
    return parser


def _json(value: object) -> None:
    print(json.dumps(value, indent=2))


def _descriptor_path(value: str) -> Path:
    path = Path(value)
    if path.parent == Path("/dev") and path.name.startswith("hidraw") and path.name[6:].isdigit():
        return Path("/sys/class/hidraw") / path.name / "device/report_descriptor"
    return path


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "gui":
            from .gui import main as gui_main
            return gui_main()
        if args.command == "read":
            from .service import DeviceService
            from .transport import DeviceError
            service = DeviceService()
            devices = [d for d in service.discover() if "read_sensor" in d["capabilities"]
                       and (args.device is None or d["id"] == args.device)]
            if len(devices) != 1:
                raise ValueError("Select one accessible USB MC7; check device access with swarm2 doctor")
            try:
                snapshot = service.read(devices[0]["id"], args.profile)
            except DeviceError as error:
                raise ValueError(str(error)) from error
            if args.json:
                _json({**snapshot, "configuration": snapshot["configuration"].to_dict()})
            else:
                print(f"MC7 profile {args.profile} · current DPI {snapshot['summary']['dpi']}")
                status = snapshot["summary"].get("status")
                if status:
                    battery = "unknown" if status["battery_percent"] is None else f"{status['battery_percent']}%"
                    charging = " · charging" if status["charging"] else ""
                    print(f"Firmware {status['firmware_version']} · battery {battery}{charging}")
                for index, stage in enumerate(snapshot["configuration"].sensor.stages, 1):
                    print(f"  Stage {index}: {stage.value:5} DPI  {stage.color}  {'enabled' if stage.enabled else 'disabled'}")
            return 0
        if args.command == "descriptor":
            if (args.path is None) == (args.hex_data is None):
                parser.error("descriptor requires exactly one path or --hex")
            data = bytes.fromhex(args.hex_data) if args.hex_data is not None else read_descriptor(_descriptor_path(args.path))
            summary = parse_descriptor(data)
            if args.json:
                _json(summary.to_dict())
            else:
                print("TYPE     ID    PAYLOAD BYTES  REPORT BYTES")
                for report in summary.reports:
                    print(f"{report.kind:8} 0x{report.report_id:02x}  {report.payload_bytes:13}  {report.report_bytes:12}")
                for warning in summary.warnings:
                    print(f"warning: {warning}", file=sys.stderr)
            return 0

        try:
            enumeration = enumerate_devices(args.backend)
        except BackendUnavailable as error:
            if args.command != "doctor":
                raise
            enumeration = Enumeration(args.backend, [], [str(error)])
        if args.command == "devices":
            if args.json:
                _json([device.to_dict() for device in enumeration.devices])
            elif not enumeration.devices:
                print("No recognized Turtle Beach MC7 HID interfaces found.")
            else:
                for device in enumeration.devices:
                    interface = "?" if device.interface_number is None else str(device.interface_number)
                    print(f"{device.path}  {device.vendor_id:04x}:{device.product_id:04x}  interface={interface}  {device.product_name}")
                    for issue in device.issues:
                        print(f"  {issue}", file=sys.stderr)
            for issue in enumeration.issues:
                print(f"warning: {issue}", file=sys.stderr)
            return 0

        try:
            load_hidapi()
            hidapi_available, hidapi_issue = True, None
        except BackendUnavailable as error:
            hidapi_available, hidapi_issue = False, str(error)
        sysfs_devices = [device for device in enumeration.devices if device.backend == "sysfs"]
        report = {
            "platform": platform.system(), "python_version": platform.python_version(),
            "version": __version__, "backend": enumeration.backend,
            "recognized_interfaces": len({(device.vendor_id, device.product_id, device.path)
                                          for device in enumeration.devices}),
            "enumeration_records": len(enumeration.devices),
            "descriptor_summaries": sum(device.descriptor is not None for device in enumeration.devices),
            "hidapi_available": hidapi_available, "hidapi_issue": hidapi_issue,
            "visible_device_nodes": sum(device.device_node_exists is True for device in sysfs_devices),
            "readable_device_nodes": sum(device.device_node_readable is True for device in sysfs_devices),
            "writable_device_nodes": sum(device.device_node_writable is True for device in sysfs_devices),
            "device_node_check_applicable": bool(sysfs_devices),
            "hid_handles_opened": False,
            "configuration_supported": platform.system() == "Linux" or (platform.system() in ("Darwin", "Windows") and hidapi_available),
            "configuration_status": "USB settings and keyboard/mouse macros: Linux read/write/readback verified; macOS experimental and hardware untested. Full feature parity remains in development.",
            "issues": enumeration.issues + [issue for device in enumeration.devices for issue in device.issues],
        }
        if sysfs_devices and not report["visible_device_nodes"]:
            report["issues"].append("MC7 is visible in sysfs but /dev/hidraw nodes are unavailable in this environment.")
        elif any(device.device_node_exists and not device.device_node_writable for device in sysfs_devices):
            report["issues"].append("Some MC7 hidraw nodes are not writable; install the MC7 udev rule for feature access.")
        if args.json:
            _json(report)
        else:
            print(f"Platform: {report['platform']} ({enumeration.backend})")
            print(f"MC7 HID interfaces: {report['recognized_interfaces']}")
            print(f"Descriptor summaries: {report['descriptor_summaries']}")
            print(f"Optional hidapi: {'available' if hidapi_available else 'unavailable'}")
            print(f"Configuration: {report['configuration_status']}")
            if hidapi_issue:
                print(hidapi_issue)
            for issue in report["issues"]:
                print(f"Issue: {issue}")
        return 0
    except (BackendUnavailable, DescriptorError, OSError, ValueError) as error:
        if getattr(args, "json", False):
            _json({"error": str(error)})
        else:
            print(f"swarm2: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
