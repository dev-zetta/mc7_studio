"""Source-derived pointer aim recommendations; no device or raw-input access.

These reproduce the vendor's angle and DPI recommendation arithmetic. Pointer
coordinates describe desktop aim, not physical sensor counts or distance.
Bounds and minimum spans are additional native-app validation.
"""

from collections.abc import Sequence
from dataclasses import dataclass
import math

ANGLE_STROKES = 10
PRACTICE_STROKES = 3
DPI_SAMPLES = 5
MIN_POINTER_SPAN = 32
MAX_COORDINATE = 1_000_000
Point = tuple[int, int]


def _point(point) -> Point:
    if (not isinstance(point, (tuple, list)) or len(point) != 2
            or any(type(value) is not int or abs(value) > MAX_COORDINATE for value in point)):
        raise ValueError("Use two bounded integer pointer coordinates")
    return tuple(point)


def _truncated_division(value: int, divisor: int) -> int:
    return (1 if value >= 0 else -1) * (abs(value) // divisor)


def angle_stroke_degrees(start: Point, end: Point) -> int:
    """Fold right-to-left strokes, then truncate degrees toward zero."""
    x0, y0 = _point(start)
    x1, y1 = _point(end)
    dx, dy = x1-x0, y1-y0
    if abs(dx) < MIN_POINTER_SPAN:
        raise ValueError("A horizontal pass must span at least 32 pointer pixels")
    if dx < 0:
        dx, dy = -dx, -dy
    return math.trunc(math.atan2(dy, dx) * (180 / math.pi))


@dataclass(frozen=True)
class AngleResult:
    stroke_angles: tuple[int, ...]
    mean_angle: int
    unclamped_angle: int
    suggested_angle: int


class AngleCalibrationSession:
    """Bounded alternating passes between two on-screen border regions."""

    def __init__(self, start: Point, *, strokes: int = ANGLE_STROKES, first_direction: int = 1):
        if type(strokes) is not int or strokes not in (PRACTICE_STROKES, ANGLE_STROKES):
            raise ValueError("Use three practice passes or ten measurement passes")
        if type(first_direction) is not int or first_direction not in (-1, 1):
            raise ValueError("The first pass direction must be -1 or 1")
        self.last_point = _point(start)
        self.stroke_count = strokes
        self.next_direction = first_direction
        self._angles: list[int] = []

    @property
    def angles(self) -> tuple[int, ...]:
        return tuple(self._angles)

    @property
    def complete(self) -> bool:
        return len(self._angles) == self.stroke_count

    def add_endpoint(self, endpoint: Point) -> int:
        if self.complete:
            raise ValueError("This pointer session is already complete")
        endpoint = _point(endpoint)
        if (endpoint[0]-self.last_point[0]) * self.next_direction <= 0:
            raise ValueError("Move toward the opposite border for the next pass")
        angle = angle_stroke_degrees(self.last_point, endpoint)
        self._angles.append(angle)
        self.last_point = endpoint
        self.next_direction *= -1
        return angle

    def result(self) -> AngleResult:
        if not self.complete or self.stroke_count != ANGLE_STROKES:
            raise ValueError("Complete all ten measurement passes before using a suggestion")
        mean = _truncated_division(sum(self._angles), ANGLE_STROKES)
        return AngleResult(self.angles, mean, -mean, max(-30, min(30, -mean)))


@dataclass(frozen=True)
class DpiSample:
    start: Point
    target: Point
    click: Point


@dataclass(frozen=True)
class DpiResult:
    sample_dpi: tuple[int, ...]
    mean_dpi: int
    suggested_dpi: int
    accuracy_pixels: int
    precision_pixels: int


def validate_current_dpi(current_dpi: int) -> int:
    if type(current_dpi) is not int or not 50 <= current_dpi <= 30000 or current_dpi % 50:
        raise ValueError("Current DPI must be 50..30000 in steps of 50")
    return current_dpi


def dpi_sample_value(current_dpi: int, sample: DpiSample) -> int:
    current_dpi = validate_current_dpi(current_dpi)
    if not isinstance(sample, DpiSample):
        raise ValueError("Use a pointer start, target and click sample")
    start, target, click = (_point(point) for point in (sample.start, sample.target, sample.click))
    desired = math.dist(start, target)
    travelled = math.dist(start, click)
    if min(desired, travelled) < MIN_POINTER_SPAN:
        raise ValueError("Move at least 32 pointer pixels toward the target before clicking")
    return math.trunc(max(50, min(30000, current_dpi * desired / travelled)))


def suggest_dpi(current_dpi: int, samples: Sequence[DpiSample]) -> DpiResult:
    """Average five clamped ratios and round to step50; exact half steps go down."""
    validate_current_dpi(current_dpi)
    if not isinstance(samples, Sequence) or len(samples) != DPI_SAMPLES:
        raise ValueError("Complete exactly five pointer target attempts")
    values = tuple(dpi_sample_value(current_dpi, sample) for sample in samples)
    mean = sum(values) // DPI_SAMPLES
    lower, remainder = divmod(mean, 50)
    proposed = (lower + int(remainder > 25)) * 50
    offsets = [(sample.click[0]-sample.target[0], sample.click[1]-sample.target[1]) for sample in samples]
    center = (_truncated_division(min(p[0] for p in offsets)+max(p[0] for p in offsets), 2),
              _truncated_division(min(p[1] for p in offsets)+max(p[1] for p in offsets), 2))
    accuracy = sum(math.trunc(math.hypot(*point)) for point in offsets) // DPI_SAMPLES
    precision = sum(math.trunc(math.dist(center, point)) for point in offsets) // DPI_SAMPLES
    return DpiResult(values, mean, proposed, accuracy, precision)
