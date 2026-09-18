"""Pure transient-display codec for the MC7 OBS Studio Mode tile."""

from collections.abc import Sequence
from dataclasses import dataclass

from .protocol import ProtocolError, REPORT_BYTES


OBS_STUDIO_MODE_RECORD_COMMAND = 0x04
OBS_STUDIO_MODE_RECORDS_PER_REPORT = 6


def _coordinate(name: str, value: int, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ProtocolError(f"{name} must be an integer in 0..{maximum}")
    return value


@dataclass(frozen=True)
class ObsStudioModeDisplayUpdate:
    """One verified Studio Mode state in logical LCD coordinates."""

    page_index: int
    slot_index: int
    enabled: bool

    def __post_init__(self) -> None:
        _coordinate("page_index", self.page_index, 2)
        _coordinate("slot_index", self.slot_index, 3)
        if type(self.enabled) is not bool:
            raise ProtocolError("OBS Studio mode state must be a boolean")

    @property
    def record(self) -> bytes:
        return bytes((
            self.page_index + 1,
            3 - self.slot_index,
            OBS_STUDIO_MODE_RECORD_COMMAND,
        )) + int(self.enabled).to_bytes(2, "little") + bytes(4)


def build_obs_studio_mode_reports(
    updates: Sequence[ObsStudioModeDisplayUpdate],
) -> tuple[bytes, ...]:
    """Build sorted A3 packets containing at most six Studio Mode records."""

    if (
        not isinstance(updates, Sequence)
        or isinstance(updates, (str, bytes, bytearray))
        or len(updates) > 12
    ):
        raise ProtocolError(
            "OBS Studio Mode updates must contain at most twelve positions"
        )
    checked = []
    positions = set()
    for update in updates:
        if not isinstance(update, ObsStudioModeDisplayUpdate):
            raise ProtocolError("Expected an ObsStudioModeDisplayUpdate")
        update.__post_init__()
        position = update.page_index, update.slot_index
        if position in positions:
            raise ProtocolError(
                "Each OBS Studio Mode position may be updated only once per batch"
            )
        positions.add(position)
        checked.append(update)
    records = [
        update.record
        for update in sorted(
            checked,
            key=lambda item: (item.page_index, item.slot_index),
        )
    ]
    reports = []
    for start in range(0, len(records), OBS_STUDIO_MODE_RECORDS_PER_REPORT):
        batch = records[start:start + OBS_STUDIO_MODE_RECORDS_PER_REPORT]
        packet = bytes((0x10, 0xA3, 0, len(batch))) + b"".join(batch)
        reports.append(packet + bytes(REPORT_BYTES - len(packet)))
    return tuple(reports)
