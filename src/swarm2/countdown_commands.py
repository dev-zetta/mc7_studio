"""Pure MC7 countdown-timer touch and transient display codecs.

The stored LCD layout only identifies app 1030 as type ``46/00``. Timer IDs,
durations and running state remain on the host. Swarm II sends one nine-byte
``0xA3`` record for each visible timer position.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from .protocol import ProtocolError, REPORT_BYTES


COUNTDOWN_WIRE_TYPE = 0x46
COUNTDOWN_WIRE_SUBTYPE = 0x00
COUNTDOWN_RECORD_COMMAND = 0x0B
COUNTDOWN_MAX_SECONDS = 600
COUNTDOWN_RECORDS_PER_REPORT = 6


def _integer(name: str, value: int, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProtocolError(f"{name} must be an integer in {minimum}..{maximum}")
    return value


def _running_band(total_seconds: int, remaining_seconds: int) -> int:
    """Return the native live-update progress band, 1 (full) through 4."""
    # Compare integers so boundary behavior exactly matches 0.75/0.5/0.25
    # without introducing floating-point drift in a long-running helper.
    scaled = remaining_seconds * 4
    if scaled >= total_seconds * 3:
        return 1
    if scaled >= total_seconds * 2:
        return 2
    if scaled >= total_seconds:
        return 3
    return 4


def countdown_live_values(state: int, total_seconds: int,
                          remaining_seconds: int) -> tuple[int, int]:
    """Encode Swarm II's live ``TimerData`` branch.

    State 0 starts a timer, state 1 is a one-second tick and state 2 stops it.
    Start/stop reset the display to the configured duration. Ticks carry a
    one-based progress band and the remaining seconds.
    """
    _integer("timer state", state, 0, 2)
    total = _integer("timer duration", total_seconds, 1, COUNTDOWN_MAX_SECONDS)
    remaining = _integer("timer remaining seconds", remaining_seconds, 0, total)
    if state in (0, 2):
        return 0, total
    return _running_band(total, remaining), remaining


def countdown_sync_values(state: int, total_seconds: int,
                          remaining_seconds: int) -> tuple[int, int]:
    """Encode the initial layout synchronization record.

    Native layout synchronization sends the configured duration as ``0,total``.
    Running ticks and stop transitions use the separate live path; accepting
    those states here would make two protocol branches look interchangeable.
    """
    _integer("timer state", state, 0, 2)
    total = _integer("timer duration", total_seconds, 1, COUNTDOWN_MAX_SECONDS)
    remaining = _integer("timer remaining seconds", remaining_seconds, 0, total)
    if state != 0 or remaining != total:
        raise ProtocolError(
            "Countdown layout sync requires the stopped configured duration")
    return 0, total


@dataclass(frozen=True)
class CountdownDisplayUpdate:
    """One timer value addressed by zero-based logical LCD coordinates."""

    page_index: int
    slot_index: int
    value0: int
    value1: int

    def __post_init__(self) -> None:
        _integer("page_index", self.page_index, 0, 2)
        _integer("slot_index", self.slot_index, 0, 3)
        _integer("countdown value0", self.value0, 0, 4)
        _integer("countdown value1", self.value1, 0, COUNTDOWN_MAX_SECONDS)

    @classmethod
    def live(cls, page_index: int, slot_index: int, *, state: int,
             total_seconds: int, remaining_seconds: int):
        return cls(page_index, slot_index,
                   *countdown_live_values(state, total_seconds, remaining_seconds))

    @classmethod
    def sync(cls, page_index: int, slot_index: int, *, state: int,
             total_seconds: int, remaining_seconds: int):
        return cls(page_index, slot_index,
                   *countdown_sync_values(state, total_seconds, remaining_seconds))

    @property
    def record(self) -> bytes:
        return bytes((self.page_index + 1, 3 - self.slot_index,
                      COUNTDOWN_RECORD_COMMAND)) + self.value0.to_bytes(
                          2, "little") + self.value1.to_bytes(2, "little") + bytes(2)


def build_countdown_reports(updates: Sequence[CountdownDisplayUpdate]) -> tuple[bytes, ...]:
    """Build sorted A3 packets containing at most six timer records each."""
    if (not isinstance(updates, Sequence)
            or isinstance(updates, (str, bytes, bytearray)) or len(updates) > 12):
        raise ProtocolError("Countdown updates must be a sequence of at most twelve positions")
    positions: set[tuple[int, int]] = set()
    checked = []
    for update in updates:
        if not isinstance(update, CountdownDisplayUpdate):
            raise ProtocolError("Expected a CountdownDisplayUpdate")
        update.__post_init__()
        position = update.page_index, update.slot_index
        if position in positions:
            raise ProtocolError("Each countdown position may be updated only once per batch")
        positions.add(position)
        checked.append(update)
    records = [update.record for update in sorted(
        checked, key=lambda item: (item.page_index, item.slot_index))]
    reports = []
    for start in range(0, len(records), COUNTDOWN_RECORDS_PER_REPORT):
        batch = records[start:start + COUNTDOWN_RECORDS_PER_REPORT]
        packet = bytes((0x10, 0xA3, 0, len(batch))) + b"".join(batch)
        reports.append(packet + bytes(REPORT_BYTES - len(packet)))
    return tuple(reports)


@dataclass(frozen=True)
class CountdownTouch:
    page_index: int
    slot_index: int
    raw: bytes


def decode_countdown_press(data: bytes) -> CountdownTouch | None:
    """Return a countdown press or ``None`` for another eight-byte event.

    The vendor callback ignores bytes 5 and 7. Byte 1 separates LCD events
    from the firmware-control path; byte 2 packs page and reversed slot.
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) != 8:
        raise ProtocolError("MC7 input event must contain exactly 8 bytes")
    raw = bytes(data)
    if (raw[0] != 0x10 or raw[1] != 0x33
            or raw[3] != COUNTDOWN_WIRE_TYPE
            or raw[4] != COUNTDOWN_WIRE_SUBTYPE
            or raw[6] != 1):
        return None
    page_wire, slot_wire = raw[2] >> 4, raw[2] & 0x0F
    if not 1 <= page_wire <= 3 or not 0 <= slot_wire <= 3:
        raise ProtocolError("Countdown touch contains an invalid LCD coordinate")
    return CountdownTouch(page_wire - 1, 3 - slot_wire, raw)
