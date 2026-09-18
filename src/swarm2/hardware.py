"""Bounded subprocess entry point for the verified MC7 sensor transactions."""

import json
import platform
import sys

from .protocol import (build_physical_dpi_report, build_sensor_read_request,
                       decode_sensor_response)
from .transport import DeviceError, HidrawTransport
from .host_metrics import MetricsUnavailable


def read_sensor(transport, profile_index):
    transport.send(build_sensor_read_request(profile_index))
    return decode_sensor_response(transport.get_sensor(), profile_index)


def transact(request, transport_factory=None):
    if transport_factory is None:
        if platform.system() == "Darwin":
            from .macos import MacOSTransport
            transport_factory = MacOSTransport
        elif platform.system() == "Windows":
            from .windows import WindowsTransport
            transport_factory = WindowsTransport
        elif platform.system() == "Linux":
            transport_factory = HidrawTransport
        else:
            raise DeviceError("MC7 transactions require Linux, macOS or Windows")
    profile = request["profile_slot"]
    if type(profile) is not int or not 1 <= profile <= 5:
        raise DeviceError("Profile slot must be 1..5")
    if request["operation"] not in ("read", "apply", "read_settings", "apply_settings", "activate", "switch_profile", "read_active_profile", "setup_display", "upload_background", "read_status", "host_lcd"):
        raise DeviceError("Unsupported device operation")
    profile_index = profile - 1
    host_lcd_request = None
    if request["operation"] == "host_lcd":
        from .host_lcd import validate_host_lcd_request
        host_lcd_request = validate_host_lcd_request(request)
    with transport_factory(request["device_id"]) as transport:
        if request["operation"] == "host_lcd":
            from .host_lcd import update_host_lcd
            return update_host_lcd(request, transport, _validated=host_lcd_request)
        if request["operation"] == "read_status":
            from .status_commands import build_status_read_request, decode_status_response
            transport.send(build_status_read_request())
            status = decode_status_response(transport.get_feature(0x09))
            return {"firmware_version": status.firmware_version,
                    "firmware_catalog_version": status.firmware_catalog_version,
                    "firmware_numeric": status.firmware_numeric, "role": status.role,
                    "battery_percent": status.battery_percent, "charging": status.charging}
        if request["operation"] == "upload_background":
            from .background import upload_background
            return upload_background(request, transport)
        if request["operation"] not in ("read", "apply"):
            from .settings import transact_settings
            return transact_settings(request, transport)
        transport_identity = str(getattr(transport, "location_id", request["device_id"]))
        before = read_sensor(transport, profile_index)
        changed = False
        if request["operation"] == "apply":
            baseline = request.get("baseline")
            if not isinstance(baseline, dict) or baseline.get("device_id") != request["device_id"] or baseline.get("profile_slot") != profile:
                raise DeviceError("Read this mouse and profile before applying settings")
            if baseline.get("transport_identity", request["device_id"]) != transport_identity:
                raise DeviceError("The mouse USB location changed. Read this mouse again before applying.")
            previous = decode_sensor_response(bytes.fromhex(baseline["raw"]), profile_index)
            if previous.dpi_signature != before.dpi_signature:
                raise DeviceError("The mouse settings changed since the last read. Read again before applying.")
            settings = request["dpi"]
            report = build_physical_dpi_report(profile_index=profile_index, **settings)
            expected = (profile_index, settings["current_stage"], tuple(settings["dpi"]),
                        tuple(tuple(c) for c in settings["colors"]), tuple(settings["enabled"]),
                        settings["indicator_enabled"])
            if before.dpi_signature != expected:
                try:
                    transport.send(report)
                    after = read_sensor(transport, profile_index)
                    if after.dpi_signature != expected:
                        raise DeviceError("Mouse readback did not match the requested DPI settings")
                    if (before.raw[4:9], before.raw[45:47]) != (after.raw[4:9], after.raw[45:47]):
                        raise DeviceError("Unrelated sensor settings changed during the DPI update")
                    before, changed = after, True
                except (OSError, ValueError, DeviceError) as error:
                    raise DeviceError(f"Apply could not be verified: {error}. The mouse may have changed; read it again.") from error
        return {"raw": before.raw.hex(), "changed": changed,
                "transport_identity": transport_identity,
                "acknowledgements": transport.acknowledgements}


def main():
    try:
        raw = sys.stdin.buffer.read(1_100_001)
        if len(raw) > 1_100_000:
            raise DeviceError("Device request is too large")
        request = json.loads(raw)
        result = transact(request)
        print(json.dumps({"result": result}))
        return 0
    except (OSError, ValueError, DeviceError, MetricsUnavailable, KeyError, TypeError) as error:
        print(json.dumps({"error": str(error)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
