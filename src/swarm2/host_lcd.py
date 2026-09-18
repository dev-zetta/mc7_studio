"""Bounded live-value updates for an already applied, active LCD layout."""

from dataclasses import dataclass
import time

from .host_metrics import _percentage, _temperature
from .host_lcd_commands import HostLcdUpdate, build_host_lcd_reports
from .settings import decode, read_raw, stable
from .transport import DeviceError


@dataclass(frozen=True)
class _ValidatedHostLcdRequest:
    device_id: str
    slot: int
    profile: int
    baseline: dict
    expected_lcd: object
    expected_profile: object
    values: dict
    target_widgets: tuple[str, ...]
    unavailable: tuple[str, ...]


def validate_host_lcd_request(request) -> _ValidatedHostLcdRequest:
    """Validate metrics and the stored baseline without opening the mouse."""
    if not isinstance(request, dict):
        raise DeviceError("Host LCD request must be an object")
    device_id = request.get("device_id")
    if not isinstance(device_id, str) or not device_id:
        raise DeviceError("Choose a connected mouse before starting live widgets")
    cpu = int(_percentage(request.get("cpu_percent"), "CPU"))
    ram = int(_percentage(request.get("ram_percent"), "RAM"))
    optional = {}
    for field, widget, convert, label in (
        ("gpu_percent", "gpu_load", _percentage, "GPU"),
        ("cpu_temperature_c", "cpu_temperature", _temperature, "CPU"),
        ("gpu_temperature_c", "gpu_temperature", _temperature, "GPU"),
    ):
        value = request.get(field)
        optional[widget] = None if value is None else int(convert(value, label))
    slot = request.get("profile_slot")
    if type(slot) is not int or not 1 <= slot <= 5:
        raise DeviceError("Profile slot must be 1..5")
    baseline = request.get("baseline")
    if (not isinstance(baseline, dict)
            or baseline.get("device_id") != device_id
            or baseline.get("profile_slot") != slot):
        raise DeviceError("Read the active mouse profile before starting live widgets")
    profile = slot - 1
    try:
        expected_lcd = decode("lcd", bytes.fromhex(baseline["settings"]["lcd"]), profile)
        expected_profile = decode("profile", bytes.fromhex(baseline["settings"]["profile"]), profile)
    except (KeyError, TypeError, ValueError) as error:
        raise DeviceError("Read the active profile and LCD layout before starting live widgets") from error
    if expected_profile.current_profile != profile:
        raise DeviceError("Live widgets require the active mouse profile")

    values = {"cpu_load": cpu, "ram_usage": ram, **optional}
    target_widgets = {
        widget.key
        for page in expected_lcd.pages[:min(3, expected_lcd.page_count)]
        for widget in page.slots or ()
        if widget is not None and widget.key in values
    }
    if not target_widgets:
        raise DeviceError("Add a system monitoring widget to an LCD page and Apply display settings first")
    unavailable = tuple(sorted(widget for widget in target_widgets if values[widget] is None))
    if len(unavailable) == len(target_widgets):
        raise DeviceError("The operating system did not expose the measurements used by this LCD layout")
    return _ValidatedHostLcdRequest(
        device_id, slot, profile, baseline, expected_lcd, expected_profile,
        values, tuple(sorted(target_widgets)), unavailable,
    )


def update_host_lcd(request, transport, *, _validated=None):
    # Whole values follow the vendor's truncation; failed samples never become
    # zero. hardware.transact supplies this same validation before opening USB.
    prepared = (_validated if isinstance(_validated, _ValidatedHostLcdRequest)
                else validate_host_lcd_request(request))
    identity = str(getattr(transport, "location_id", prepared.device_id))
    if prepared.baseline.get("transport_identity", prepared.device_id) != identity:
        raise DeviceError("The mouse USB location changed; read it again")
    profile = prepared.profile
    expected_lcd = prepared.expected_lcd
    expected_profile = prepared.expected_profile
    values = prepared.values
    target_widgets = prepared.target_widgets
    unavailable = prepared.unavailable

    def checked_layout():
        active = decode("profile", read_raw(transport, "profile", profile), profile)
        if active.current_profile != profile:
            raise DeviceError("The active mouse profile changed; live widgets stopped")
        if stable("profile", active.raw) != stable("profile", expected_profile.raw):
            raise DeviceError("The mouse profile settings changed; read it again before restarting live widgets")
        lcd = decode("lcd", read_raw(transport, "lcd", profile), profile)
        if lcd.signature != expected_lcd.signature:
            raise DeviceError("The LCD layout changed; read it again before restarting live widgets")
        return lcd

    lcd = checked_layout()
    updates = [HostLcdUpdate(widget.key, page_index, slot_index, values[widget.key])
               for page_index, page in enumerate(lcd.pages[:min(3, lcd.page_count)])
               for slot_index, widget in enumerate(page.slots or ())
               if (widget is not None and widget.key in values
                   and values[widget.key] is not None)]
    reports = build_host_lcd_reports(updates)
    for index, report in enumerate(reports):
        if index:
            # A3 carries no profile or widget identity. A physical profile
            # switch between reports must stop the remaining position writes.
            checked_layout()
        transport.send(report)
        time.sleep(0.03)
    checked_layout()
    return {"acknowledged": True, "widgets": len(updates), "reports": len(reports),
            "cpu_percent": values["cpu_load"], "ram_percent": values["ram_usage"],
            "gpu_percent": values["gpu_load"],
            "cpu_temperature_c": values["cpu_temperature"],
            "gpu_temperature_c": values["gpu_temperature"],
            "updated_values": {key: values[key] for key in sorted(target_widgets)
                               if values[key] is not None},
            "unavailable_widgets": list(unavailable)}
