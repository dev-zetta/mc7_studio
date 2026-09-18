"""Safe allocation and serialized upload of MC7 Open Application icons.

The twenty image stores are global across all five profiles. Allocation only
uses a store that no current command-0x29 Open Application record references.
There is no pixel readback, so an acknowledged upload is never described as a
verified image and a referenced store is never overwritten.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Sequence

from .configuration import host_action_icon_rgba
from .host_actions import decode_host_action_record
from .image_commands import (
    CUSTOM_ICON_STORE_COUNT, build_custom_icon_transfer,
    decode_image_response, encode_custom_icon_rgba,
)
from .lcd_commands import LCD_WIDGETS, LcdState, decode_lcd_response
from .screen_key_commands import ScreenKeyState, decode_screen_key_responses
from .status_commands import build_status_read_request, decode_status_response
from .transport import DeviceError


@dataclass(frozen=True)
class CustomIconUpload:
    icon_index: int
    rgba: bytes


@dataclass(frozen=True)
class CustomIconPlan:
    icon_indices: tuple[tuple[int | None, ...], ...]
    uploads: tuple[CustomIconUpload, ...]
    referenced_before: frozenset[int]


def custom_icon_reference(record: bytes) -> int | None:
    """Return a valid zero-based icon reference from one Open App record."""
    if (not isinstance(record, (bytes, bytearray)) or len(record) != 11
            or bytes(record[:4]) != b"\x00\x00\x01\x0b"
            or not 1 <= record[4] <= CUSTOM_ICON_STORE_COUNT):
        return None
    return record[4] - 1


def referenced_custom_icon_indices(
        states: Sequence[ScreenKeyState]) -> frozenset[int]:
    """Scan exact command-0x29 reads for every profile before allocation."""
    if (isinstance(states, (str, bytes, bytearray)) or not isinstance(states, Sequence)
            or len(states) != 5):
        raise DeviceError("Read custom-icon references from all five profiles before uploading")
    checked: dict[int, ScreenKeyState] = {}
    for state in states:
        if not isinstance(state, ScreenKeyState):
            raise DeviceError("Read valid screen-key records from all five profiles before uploading")
        try:
            parsed = decode_screen_key_responses(state.raw, state.profile_index)
        except ValueError as error:
            raise DeviceError("Read valid screen-key records from all five profiles before uploading") from error
        if parsed != state or state.profile_index in checked:
            raise DeviceError("Read distinct screen-key records from all five profiles before uploading")
        checked[state.profile_index] = state
    if set(checked) != set(range(5)):
        raise DeviceError("Read custom-icon references from all five profiles before uploading")
    return frozenset(
        record[4] - 1
        for state in checked.values()
        for page in state.pages
        for record in page.records
        # Reserve every plausible reference, including functions that this
        # implementation cannot decode. The stores are global and have no
        # pixel readback, so an unknown command must never be treated as free.
        if len(record) == 11 and 1 <= record[4] <= CUSTOM_ICON_STORE_COUNT
    )


def _matrix(value, name: str, *, minimum_rows: int = 1,
            maximum_rows: int = 3) -> tuple[tuple[object | None, ...], ...]:
    if (isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence)
            or not minimum_rows <= len(value) <= maximum_rows):
        if minimum_rows == maximum_rows:
            requirement = f"exactly {minimum_rows} LCD pages"
        else:
            requirement = f"{minimum_rows}..{maximum_rows} LCD pages"
        raise DeviceError(f"{name} must contain {requirement}")
    result = []
    for row in value:
        if (isinstance(row, (str, bytes, bytearray)) or not isinstance(row, Sequence)
                or len(row) != 4):
            raise DeviceError(f"Each {name} page must contain exactly four slots")
        result.append(tuple(row))
    return tuple(result)


def plan_custom_icons(
        current_lcd: LcdState,
        current_keys: ScreenKeyState,
        all_profile_keys: Sequence[ScreenKeyState],
        desired_pages,
        desired_targets,
        desired_icons,
        *,
        previous_icons=None,
        previous_icon_indices=None,
) -> CustomIconPlan:
    """Allocate unreferenced global stores and retain proven unchanged icons.

    ``previous_icons`` and ``previous_icon_indices`` are paired matrices from
    the fresh local baseline. A current store is reused only when both the
    pixels and their exact prior one-to-one store mapping are known. New,
    changed or unproven pixels are staged to an unreferenced store before any
    trigger record is written.
    """
    if not isinstance(current_lcd, LcdState) or not isinstance(current_keys, ScreenKeyState):
        raise DeviceError("Read the current LCD layout and key definitions before uploading icons")
    try:
        lcd = decode_lcd_response(current_lcd.raw, current_lcd.profile_index)
        keys = decode_screen_key_responses(current_keys.raw, current_keys.profile_index)
    except ValueError as error:
        raise DeviceError("Read the current LCD layout and key definitions before uploading icons") from error
    if lcd != current_lcd or keys != current_keys or lcd.profile_index != keys.profile_index:
        raise DeviceError("LCD and screen-key state must be fresh and belong to one profile")
    pages = _matrix(desired_pages, "desired LCD layout")
    targets = _matrix(
        desired_targets, "desired host-action targets",
        minimum_rows=len(pages), maximum_rows=len(pages))
    icons = _matrix(
        desired_icons, "desired host-action icons",
        minimum_rows=len(pages), maximum_rows=len(pages))
    previous_rows = (_matrix(
        previous_icons, "previous host-action icons", minimum_rows=1)
        if previous_icons else ())
    previous_index_rows = (_matrix(
        previous_icon_indices, "previous host-action icon indices",
        minimum_rows=1)
        if previous_icon_indices else ())
    if any(
            index is not None
            and (type(index) is not int or not 0 <= index < CUSTOM_ICON_STORE_COUNT)
            for row in previous_index_rows for index in row):
        raise DeviceError(
            "Previous host-action icon indices must be integers from 0 to 19 or null")
    previous = tuple(
        previous_rows[index] if index < len(previous_rows) else (None,) * 4
        for index in range(3))
    previous_indices = tuple(
        previous_index_rows[index]
        if index < len(previous_index_rows) else (None,) * 4
        for index in range(3))
    referenced = referenced_custom_icon_indices(all_profile_keys)
    allocated = set(referenced)
    indices: list[list[int | None]] = [[None] * 4 for _ in range(3)]
    uploads: list[CustomIconUpload] = []
    by_pixels: dict[bytes, int] = {}

    current_pages = tuple(tuple(
        None if widget is None else widget.key for widget in page.slots
    ) for page in lcd.pages[:3])
    if len(pages) > len(current_pages) or len(keys.pages) < len(pages):
        raise DeviceError("Enabling additional LCD pages is not yet supported")
    for page_index, page in enumerate(pages):
        for slot_index, widget in enumerate(page):
            target = targets[page_index][slot_index]
            icon_value = icons[page_index][slot_index]
            if widget != "open_application":
                if icon_value is not None:
                    raise DeviceError("Only Open Application LCD tiles may contain custom icons")
                continue
            if target is None or icon_value is None:
                # Existing raw hardware tiles may stay unresolved. The screen
                # key planner preserves their exact record at the same place.
                continue
            try:
                rgba = host_action_icon_rgba(icon_value)
            except ValueError as error:
                raise DeviceError(str(error)) from error
            original = keys.pages[page_index].records[slot_index]
            original_index = custom_icon_reference(original)
            same_coordinate = (
                page_index < len(current_pages)
                and current_pages[page_index][slot_index] == "open_application")
            if (same_coordinate and original_index is not None
                    and previous[page_index][slot_index] == icon_value
                    and previous_indices[page_index][slot_index]
                    == original_index):
                try:
                    decode_host_action_record(original, "open_application")
                except ValueError:
                    pass
                else:
                    indices[page_index][slot_index] = original_index
                    by_pixels.setdefault(rgba, original_index)
                    continue
            shared = by_pixels.get(rgba)
            if shared is not None:
                indices[page_index][slot_index] = shared
                continue
            free = next(
                (candidate for candidate in range(CUSTOM_ICON_STORE_COUNT)
                 if candidate not in allocated), None)
            if free is None:
                raise DeviceError(
                    "All 20 custom-icon stores are referenced. Remove an Open Application "
                    "tile and apply that layout before uploading another icon.")
            allocated.add(free)
            by_pixels[rgba] = free
            indices[page_index][slot_index] = free
            uploads.append(CustomIconUpload(free, rgba))
    return CustomIconPlan(
        tuple(tuple(row) for row in indices), tuple(uploads), referenced)


def upload_custom_icons(uploads: Sequence[CustomIconUpload], transport) -> tuple[dict, ...]:
    """Upload preallocated icons; callers bind their records only afterward."""
    if not uploads:
        return ()
    if (isinstance(uploads, (str, bytes, bytearray))
            or not isinstance(uploads, Sequence)
            or any(not isinstance(upload, CustomIconUpload) for upload in uploads)):
        raise DeviceError("Custom-icon uploads must use a validated allocation plan")
    transport.send(build_status_read_request())
    before = decode_status_response(transport.get_feature(9))
    deadline = time.monotonic() + max(30, len(uploads) * 25)
    results = []
    for position, upload in enumerate(uploads, 1):
        plan = build_custom_icon_transfer(
            encode_custom_icon_rgba(upload.rgba), icon_index=upload.icon_index)
        completed = 0
        try:
            for step in plan.steps:
                if time.monotonic() >= deadline:
                    raise DeviceError("Custom-icon transfer exceeded its deadline")
                response = transport.exchange_image(
                    step.report, delay_ms=step.minimum_delay_ms)
                decode_image_response(response, expected_command=step.command)
                completed += 1
        except (OSError, ValueError, DeviceError) as error:
            raise DeviceError(
                f"Custom icon {position}/{len(uploads)} transfer stopped after "
                f"{completed}/{len(plan.steps)} packets: {error}. No LCD tile was bound "
                "to the incomplete icon and no automatic replay was attempted.") from error
        results.append({
            "icon_index": upload.icon_index,
            "image_sha256": plan.image_sha256,
            "image_bytes": plan.image_bytes,
            "completed_packets": completed,
            "pixel_readback": False,
        })
    transport.send(build_status_read_request())
    after = decode_status_response(transport.get_feature(9))
    if after.firmware_version != before.firmware_version:
        raise DeviceError("Device identity changed during custom-icon transfer")
    return tuple(results)
