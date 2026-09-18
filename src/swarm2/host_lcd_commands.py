"""Pure encoders for the MC7's transient host-monitoring LCD values.

The A3 report addresses the active profile, with page/slot coordinates. It
does not install a widget, select a profile, or store a measurement on the host.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from types import MappingProxyType

from .protocol import Acknowledgement, ProtocolError, REPORT_BYTES, decode_acknowledgement

HOST_LCD_WIDGET_TYPES = MappingProxyType({
    "gpu_temperature": 0x37,
    "gpu_load": 0x38,
    "cpu_temperature": 0x39,
    "cpu_load": 0x3A,
    "ram_usage": 0x41,
})
_PERCENT_WIDGETS = frozenset(("cpu_load", "gpu_load", "ram_usage"))
HOST_LCD_RECORDS_PER_REPORT = 6
HOST_LCD_ACK_DELAY_MS = 30


def _integer(name: str, value: int, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProtocolError(f"{name} must be an integer in {minimum}..{maximum}")
    return value


def host_lcd_icon_index(value: int) -> int:
    """Return the vendor's wire icon ordering: low=2, medium=1, high=0."""
    _integer("value", value, 0, 0xFFFF)
    return 2 if value < 50 else 1 if value < 70 else 0


@dataclass(frozen=True)
class HostLcdUpdate:
    widget: str
    page_index: int
    slot_index: int
    value: int

    def __post_init__(self) -> None:
        if type(self.widget) is not str or self.widget not in HOST_LCD_WIDGET_TYPES:
            raise ProtocolError("Unsupported host LCD telemetry widget")
        _integer("page_index", self.page_index, 0, 2)
        _integer("slot_index", self.slot_index, 0, 3)
        maximum = 100 if self.widget in _PERCENT_WIDGETS else 0xFFFF
        _integer("value", self.value, 0, maximum)


def build_host_lcd_reports(updates: Sequence[HostLcdUpdate]) -> tuple[bytes, ...]:
    """Encode independent monitoring values, preserving other LCD slots.

    Inputs use the same zero-based logical slot order as lcd_commands. The
    caller must verify these widgets occupy the indicated active-profile slots
    immediately before sending; A3 has no profile or widget-type field. Both
    icon and number are refreshed each time, including when values are stable.
    Empty input produces no reports. Duplicate positions are rejected.
    """
    if (not isinstance(updates, Sequence)
            or isinstance(updates, (str, bytes, bytearray)) or len(updates) > 12):
        raise ProtocolError("Host LCD updates must be a sequence of at most twelve positions")
    pages: dict[int, list[HostLcdUpdate]] = {}
    positions: set[tuple[int, int]] = set()
    for update in updates:
        if not isinstance(update, HostLcdUpdate):
            raise ProtocolError("Expected a HostLcdUpdate")
        # Revalidate even an object constructed outside the normal dataclass API.
        update.__post_init__()
        position = update.page_index, update.slot_index
        if position in positions:
            raise ProtocolError("Each host LCD position may be updated only once per batch")
        positions.add(position)
        pages.setdefault(update.page_index, []).append(update)

    reports = []
    for page_index in sorted(pages):
        records = []
        for update in sorted(pages[page_index], key=lambda item: item.slot_index):
            for command, value in ((4, host_lcd_icon_index(update.value)),
                                   (5, update.value)):
                records.append(bytes((page_index + 1, 3 - update.slot_index, command))
                               + value.to_bytes(2, "little") + bytes(4))
        for start in range(0, len(records), HOST_LCD_RECORDS_PER_REPORT):
            batch = records[start:start + HOST_LCD_RECORDS_PER_REPORT]
            packet = bytes((0x10, 0xA3, 0, len(batch))) + b"".join(batch)
            reports.append(packet + bytes(REPORT_BYTES - len(packet)))
    return tuple(reports)


def decode_host_lcd_acknowledgement(data: bytes) -> Acknowledgement:
    """Check the report/command identity; acceptance is not rendered-value readback."""
    result = decode_acknowledgement(data, target="mouse")
    if result.raw[0] != 0x10 or result.raw[3] != 0xA3:
        raise ProtocolError("Host LCD acknowledgement report or command does not match")
    return result
