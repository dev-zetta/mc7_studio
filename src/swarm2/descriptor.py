"""Bounded HID descriptor parser for report sizes and usage metadata.

This is a descriptor summary, not a decoder for vendor feature-report payloads.
Report byte lengths include the report ID only when the descriptor declares one.
"""

from dataclasses import asdict, dataclass, field, replace
from typing import Any

MAX_DESCRIPTOR_BYTES = 65_536
MAX_REPORT_BITS = 8 * 65_536


class DescriptorError(ValueError):
    """A descriptor is malformed or exceeds the parser's bounds."""


@dataclass(frozen=True)
class Usage:
    page: int
    usage: int


@dataclass
class Report:
    kind: str
    report_id: int
    bits: int = 0
    usages: list[Usage] = field(default_factory=list)
    usage_ranges: list[dict[str, Usage]] = field(default_factory=list)
    application_usages: list[Usage] = field(default_factory=list)

    @property
    def payload_bytes(self) -> int:
        return (self.bits + 7) // 8

    @property
    def report_bytes(self) -> int:
        return self.payload_bytes + bool(self.report_id)

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "payload_bytes": self.payload_bytes,
                "report_bytes": self.report_bytes}


@dataclass
class DescriptorSummary:
    reports: list[Report]
    application_usages: list[Usage]
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "reports": [report.to_dict() for report in self.reports],
            "application_usages": [asdict(usage) for usage in self.application_usages],
            "warnings": self.warnings,
        }


@dataclass
class _Globals:
    usage_page: int = 0
    report_id: int = 0
    report_size: int = 0
    report_count: int = 0


def _unique_append(items: list, item: Any) -> None:
    if item not in items:
        items.append(item)


def parse_descriptor(data: bytes) -> DescriptorSummary:
    """Parse short items and safely skip long items; reject truncated data.

    Report Size/Count, Report ID, global Push/Pop, collection scope, and local
    Usage/Usage Minimum/Maximum are handled. Usage ranges are never expanded.
    Reserved items are reported as warnings because they may have new semantics.
    """
    if not data:
        raise DescriptorError("empty report descriptor")
    if len(data) > MAX_DESCRIPTOR_BYTES:
        raise DescriptorError(f"descriptor exceeds {MAX_DESCRIPTOR_BYTES} bytes")
    state = _Globals()
    stack: list[_Globals] = []
    collections: list[tuple[int, Usage | None]] = []
    applications: list[Usage] = []
    reports: dict[tuple[str, int], Report] = {}
    warnings: list[str] = []
    usages: list[Usage] = []
    minimum: Usage | None = None
    maximum: Usage | None = None
    position = 0

    while position < len(data):
        offset = position
        prefix = data[position]
        position += 1
        if prefix == 0xFE:
            if position + 2 > len(data):
                raise DescriptorError(f"truncated long-item header at byte {offset}")
            length, tag = data[position:position + 2]
            position += 2
            if position + length > len(data):
                raise DescriptorError(f"truncated long item at byte {offset}")
            warnings.append(f"skipped long item 0x{tag:02x} at byte {offset}")
            position += length
            continue

        size = (0, 1, 2, 4)[prefix & 3]
        kind, tag = (prefix >> 2) & 3, prefix >> 4
        if position + size > len(data):
            raise DescriptorError(f"truncated short item at byte {offset}")
        value = int.from_bytes(data[position:position + size], "little")
        position += size

        if kind == 1:  # Global items.
            if tag == 0:
                if value > 0xFFFF:
                    raise DescriptorError(f"Usage Page exceeds 16 bits at byte {offset}")
                state.usage_page = value
            elif tag == 7:
                state.report_size = value
            elif tag == 8:
                if not 1 <= value <= 255:
                    raise DescriptorError(f"Report ID must be 1..255 at byte {offset}")
                state.report_id = value
            elif tag == 9:
                state.report_count = value
            elif tag == 10:
                stack.append(replace(state))
            elif tag == 11:
                if not stack:
                    raise DescriptorError(f"global Pop without Push at byte {offset}")
                state = stack.pop()
            elif tag not in (1, 2, 3, 4, 5, 6):
                warnings.append(f"reserved global item at byte {offset}")
        elif kind == 2:  # Local items are consumed by the next main item.
            usage = Usage(value >> 16, value & 0xFFFF) if size == 4 else Usage(state.usage_page, value)
            if tag == 0:
                _unique_append(usages, usage)
            elif tag == 1:
                minimum = usage
            elif tag == 2:
                maximum = usage
            elif tag == 10:
                warnings.append(f"usage delimiter alternatives are summarized together at byte {offset}")
            elif tag not in (3, 4, 5, 7, 8, 9):
                warnings.append(f"reserved local item at byte {offset}")
        elif kind == 0:
            if (minimum is None) != (maximum is None):
                raise DescriptorError(f"incomplete Usage Minimum/Maximum at byte {offset}")
            if minimum is not None and maximum is not None:
                if minimum.page != maximum.page or minimum.usage > maximum.usage:
                    raise DescriptorError(f"invalid usage range at byte {offset}")
            if tag in (8, 9, 11):
                report_kind = {8: "input", 9: "output", 11: "feature"}[tag]
                key = (report_kind, state.report_id)
                report = reports.setdefault(key, Report(*key))
                added_bits = state.report_size * state.report_count
                if state.report_size == 0 or state.report_count == 0:
                    raise DescriptorError(f"zero Report Size/Count at byte {offset}")
                if report.bits + added_bits > MAX_REPORT_BITS:
                    raise DescriptorError(f"report exceeds {MAX_REPORT_BITS} bits at byte {offset}")
                report.bits += added_bits
                for usage in usages:
                    _unique_append(report.usages, usage)
                if minimum is not None:
                    _unique_append(report.usage_ranges, {"minimum": minimum, "maximum": maximum})
                for collection_type, usage in collections:
                    if collection_type == 1 and usage is not None:
                        _unique_append(report.application_usages, usage)
            elif tag == 10:
                usage = usages[0] if usages else minimum
                collections.append((value, usage))
                if value == 1 and usage is not None:
                    _unique_append(applications, usage)
            elif tag == 12:
                if not collections:
                    raise DescriptorError(f"End Collection without Collection at byte {offset}")
                collections.pop()
            else:
                warnings.append(f"reserved main item at byte {offset}")
            usages, minimum, maximum = [], None, None
        else:
            warnings.append(f"reserved item type at byte {offset}")

    if collections:
        raise DescriptorError("unclosed Collection")
    if stack:
        warnings.append("global Push left on stack at end of descriptor")
    ids = {report.report_id for report in reports.values()}
    if 0 in ids and len(ids) > 1:
        raise DescriptorError("descriptor mixes unnumbered reports with Report IDs")
    order = {"input": 0, "output": 1, "feature": 2}
    return DescriptorSummary(
        sorted(reports.values(), key=lambda report: (order[report.kind], report.report_id)),
        applications, warnings,
    )
