"""Offline MC7 image encoders and bounded transfer descriptions.

The protocol transmits RGB565 followed by a separate alpha plane. These
helpers neither access USB nor claim that a completion echo proves persistence.
"""

from dataclasses import dataclass
from hashlib import sha256
import struct

from .protocol import ProtocolError, REPORT_BYTES

BACKGROUND_WIDTH = 76
BACKGROUND_HEIGHT = 284
BACKGROUND_RGBA_BYTES = BACKGROUND_WIDTH * BACKGROUND_HEIGHT * 4
BACKGROUND_IMAGE_BYTES = 12 + BACKGROUND_WIDTH * BACKGROUND_HEIGHT * 3
CUSTOM_ICON_SOURCE_WIDTH = 64
CUSTOM_ICON_SOURCE_HEIGHT = 62
CUSTOM_ICON_WIDTH = CUSTOM_ICON_SOURCE_HEIGHT
CUSTOM_ICON_HEIGHT = CUSTOM_ICON_SOURCE_WIDTH
CUSTOM_ICON_RGBA_BYTES = CUSTOM_ICON_WIDTH * CUSTOM_ICON_HEIGHT * 4
CUSTOM_ICON_IMAGE_BYTES = 12 + CUSTOM_ICON_WIDTH * CUSTOM_ICON_HEIGHT * 3
CUSTOM_ICON_FIRST_STORE_SELECTOR = 105
CUSTOM_ICON_STORE_COUNT = 20
CUSTOM_ICON_LAST_STORE_SELECTOR = (CUSTOM_ICON_FIRST_STORE_SELECTOR
                                   + CUSTOM_ICON_STORE_COUNT - 1)
IMAGE_BLOCK_BYTES = 4096
IMAGE_DATA_BYTES = 61
IMAGE_RESPONSE_SELECTORS = frozenset((0xA2, 0xA5, 0xFF))
BACKGROUND_SELECTION_SELECTOR = 0x2C
BACKGROUND_SELECTION_RESPONSE_BYTES = 8
BUILTIN_BACKGROUND_INDEX = 0
CUSTOM_BACKGROUND_INDEX = 1


def _bytes(data, length: int, label: str) -> bytes:
    if not isinstance(data, (bytes, bytearray)) or len(data) != length:
        raise ProtocolError(f"{label} must contain exactly {length} bytes")
    return bytes(data)


def rotate_background_rgba(rgba: bytes) -> bytes:
    """Rotate a cropped 284×76 RGBA8888 image 270° clockwise into wire orientation.

    Cropping/resizing is the caller's image-library operation. This exact
    quarter-turn needs no resampling or dependency on Qt.
    """
    source = _bytes(rgba, BACKGROUND_RGBA_BYTES, "Background RGBA input")
    result = bytearray(len(source))
    for y in range(BACKGROUND_HEIGHT):
        for x in range(BACKGROUND_WIDTH):
            source_offset = (x * BACKGROUND_HEIGHT + BACKGROUND_HEIGHT - 1 - y) * 4
            target_offset = (y * BACKGROUND_WIDTH + x) * 4
            result[target_offset:target_offset + 4] = source[source_offset:source_offset + 4]
    return bytes(result)


@dataclass(frozen=True)
class ImageBlob:
    raw: bytes
    width: int
    height: int
    rgb565: bytes
    alpha: bytes


def _dimensions(width: int, height: int) -> tuple[int, int]:
    if (type(width) is not int or type(height) is not int
            or not 1 <= width <= 0x7FF or not 1 <= height <= 0x7FF):
        raise ProtocolError("Image dimensions must be integers in the range 1..2047")
    return width, height


def encode_image_rgba(rgba: bytes, *, width: int, height: int,
                      label: str = "Image RGBA input") -> bytes:
    """Encode a wire-oriented straight RGBA8888 image, row by row.

    RGB quantization is independent of alpha. This implementation uses direct
    quantization, without copying the vendor's optional dithering implementation.
    """
    width, height = _dimensions(width, height)
    pixels = width * height
    source = _bytes(rgba, pixels * 4, label)
    colors = bytearray(pixels * 2)
    alpha = bytearray(pixels)
    for index in range(pixels):
        red, green, blue, opacity = source[index * 4:index * 4 + 4]
        color = ((red >> 3) << 11) | ((green >> 2) << 5) | (blue >> 3)
        struct.pack_into("<H", colors, index * 2, color)
        alpha[index] = opacity
    packed = 0x14 | (width << 10) | (height << 21)
    return struct.pack("<III", packed, pixels * 3, 12) + colors + alpha


def decode_image_blob(data: bytes, *, width: int, height: int,
                      label: str = "Image blob") -> ImageBlob:
    """Decode one image container while requiring the caller's exact dimensions."""
    width, height = _dimensions(width, height)
    raw = _bytes(data, 12 + width * height * 3, label)
    packed, payload_size, data_offset = struct.unpack_from("<III", raw)
    actual_width, actual_height = (packed >> 10) & 0x7FF, packed >> 21
    if (packed & 0x3FF) != 0x14 or (actual_width, actual_height) != (width, height):
        raise ProtocolError("Unsupported image format or dimensions")
    if payload_size != width * height * 3 or data_offset != 12:
        raise ProtocolError("Invalid image size or data offset")
    color_end = 12 + width * height * 2
    return ImageBlob(raw, width, height, raw[12:color_end], raw[color_end:])


def encode_background_rgba(rgba: bytes) -> bytes:
    """Encode already-rotated 76×284 straight RGBA8888 pixels."""
    return encode_image_rgba(rgba, width=BACKGROUND_WIDTH, height=BACKGROUND_HEIGHT,
                             label="Background RGBA input")


def decode_background_blob(data: bytes) -> ImageBlob:
    return decode_image_blob(data, width=BACKGROUND_WIDTH, height=BACKGROUND_HEIGHT,
                             label="Background image blob")


def encode_custom_icon_rgba(rgba: bytes) -> bytes:
    """Encode an already-rotated 62×64 custom-icon wire image."""
    return encode_image_rgba(rgba, width=CUSTOM_ICON_WIDTH, height=CUSTOM_ICON_HEIGHT,
                             label="Custom icon RGBA input")


def decode_custom_icon_blob(data: bytes) -> ImageBlob:
    return decode_image_blob(data, width=CUSTOM_ICON_WIDTH, height=CUSTOM_ICON_HEIGHT,
                             label="Custom icon image blob")


@dataclass(frozen=True)
class ImageStep:
    report: bytes
    phase: str
    block_index: int | None
    image_offset: int | None
    minimum_delay_ms: int

    @property
    def command(self) -> int:
        return self.report[2]


@dataclass(frozen=True)
class BackgroundTransfer:
    image_sha256: str
    image_bytes: int
    block_count: int
    steps: tuple[ImageStep, ...]

    @property
    def minimum_duration_ms(self) -> int:
        # Each vendor GET is followed by another 30 ms pause.
        return sum(step.minimum_delay_ms + 30 for step in self.steps)


@dataclass(frozen=True)
class ImageTransfer:
    image_selector: int
    image_sha256: str
    image_bytes: int
    block_count: int
    steps: tuple[ImageStep, ...]

    @property
    def minimum_duration_ms(self) -> int:
        return sum(step.minimum_delay_ms + 30 for step in self.steps)


def _report(command: int, payload: bytes = b"") -> bytes:
    return bytes((0x10, 0xA5, command)) + payload + bytes(IMAGE_DATA_BYTES - len(payload))


def _transfer_steps(raw: bytes, image_selector: int) -> tuple[ImageStep, ...]:
    steps = [ImageStep(_report(image_selector), "select", None, None, 30)]
    blocks = (len(raw) + IMAGE_BLOCK_BYTES - 1) // IMAGE_BLOCK_BYTES
    for block in range(blocks):
        offset = block * IMAGE_BLOCK_BYTES
        chunk = raw[offset:offset + IMAGE_BLOCK_BYTES]
        steps.append(ImageStep(_report(0xF1), "block_start", block, offset, 30))
        for inner in range(0, len(chunk), IMAGE_DATA_BYTES):
            payload = chunk[inner:inner + IMAGE_DATA_BYTES]
            steps.append(ImageStep(_report(len(payload), payload), "data", block,
                                   offset + inner, 30))
        # Preserve the vendor's separate final-data and block-padding reports.
        padding = IMAGE_BLOCK_BYTES - len(chunk)
        for inner in range(0, padding, IMAGE_DATA_BYTES):
            count = min(IMAGE_DATA_BYTES, padding - inner)
            steps.append(ImageStep(_report(count, bytes(count)), "padding", block, None, 30))
        steps.append(ImageStep(_report(0xF2), "block_end", block, offset + len(chunk), 30))
    steps.append(ImageStep(_report(0xFF), "finish", None, len(raw), 2500))
    return tuple(steps)


def _known_image_selector(image_selector: int) -> int:
    if (type(image_selector) is not int
            or (image_selector != 0 and not CUSTOM_ICON_FIRST_STORE_SELECTOR
                <= image_selector <= CUSTOM_ICON_LAST_STORE_SELECTOR)):
        raise ProtocolError("Image selector must be background 0 or custom icon 105..124")
    return image_selector


def build_image_transfer(data: bytes, *, image_selector: int, width: int,
                         height: int) -> ImageTransfer:
    """Plan a transfer only for the source-confirmed background and icon stores."""
    image_selector = _known_image_selector(image_selector)
    width, height = _dimensions(width, height)
    expected_dimensions = ((BACKGROUND_WIDTH, BACKGROUND_HEIGHT) if image_selector == 0
                           else (CUSTOM_ICON_WIDTH, CUSTOM_ICON_HEIGHT))
    if (width, height) != expected_dimensions:
        raise ProtocolError("Image dimensions do not match the selected image store")
    blob = decode_image_blob(data, width=width, height=height)
    steps = _transfer_steps(blob.raw, image_selector)
    return ImageTransfer(image_selector, sha256(blob.raw).hexdigest(), len(blob.raw),
                         (len(blob.raw) + IMAGE_BLOCK_BYTES - 1) // IMAGE_BLOCK_BYTES,
                         steps)


def build_background_transfer(data: bytes) -> BackgroundTransfer:
    """Plan only the source-confirmed global background slot 0, never firmware.

    Every step requires one feature SET then a correlated feature GET. Do not
    interleave other device operations. Completion failure stops the transfer;
    restarting or replaying an unknown partial transfer is not defined here.
    """
    plan = build_image_transfer(data, image_selector=0, width=BACKGROUND_WIDTH,
                                height=BACKGROUND_HEIGHT)
    return BackgroundTransfer(plan.image_sha256, plan.image_bytes, plan.block_count,
                              plan.steps)


def custom_icon_store_selector(icon_index: int) -> int:
    """Map a zero-based custom-icon index to its A5 image-store selector."""
    if type(icon_index) is not int or not 0 <= icon_index < CUSTOM_ICON_STORE_COUNT:
        raise ProtocolError("Custom icon index must be an integer in the range 0..19")
    return CUSTOM_ICON_FIRST_STORE_SELECTOR + icon_index


def build_custom_icon_transfer(data: bytes, *, icon_index: int) -> ImageTransfer:
    """Plan an upload to one of the 20 source-confirmed custom-icon stores."""
    return build_image_transfer(data, image_selector=custom_icon_store_selector(icon_index),
                                width=CUSTOM_ICON_WIDTH, height=CUSTOM_ICON_HEIGHT)


def build_image_get_buffer() -> bytes:
    """Vendor's in-memory GET buffer; only report ID10 is sent by HIDAPI GET."""
    return bytes((0x10, 0xA2)) + bytes(REPORT_BYTES - 2)


@dataclass(frozen=True)
class ImageReply:
    raw: bytes
    selector: int
    command: int


def decode_image_response(data: bytes, *, expected_command: int) -> ImageReply:
    """Validate the response identity and the current transfer phase.

    A hardware trace from firmware 5.04 returned ``10 FF FF ...`` for a data
    packet whose count was 3D, matching the original host's decision not to
    interpret byte2 during data and padding. Selection, F1, F2 and finish retain
    exact phase correlation. Every phase still requires report ID 10 and one
    of the host's A2, A5 and FF ready selectors. No image-pixel readback is
    implied by this decoder.
    """
    selection_phase = (type(expected_command) is int
                       and (expected_command == 0 or CUSTOM_ICON_FIRST_STORE_SELECTOR
                            <= expected_command <= CUSTOM_ICON_LAST_STORE_SELECTOR))
    if (type(expected_command) is not int
            or expected_command not in (*range(62), 0xF1, 0xF2, 0xFF)
            and not selection_phase):
        raise ProtocolError("Unsupported image response phase")
    if not isinstance(data, (bytes, bytearray)) or not 3 <= len(data) <= REPORT_BYTES:
        raise ProtocolError("Image response must contain 3..64 bytes")
    raw = bytes(data)
    control_phase = selection_phase or expected_command in (0xF1, 0xF2, 0xFF)
    if (raw[0] != 0x10 or raw[1] not in IMAGE_RESPONSE_SELECTORS
            or (control_phase and raw[2] != expected_command)):
        raise ProtocolError("Image response report, selector or command echo does not match")
    return ImageReply(raw, raw[1], raw[2])


@dataclass(frozen=True)
class BackgroundSelection:
    raw: bytes
    background_index: int


def build_background_selection_read_request() -> bytes:
    return bytes((0x10, 0x1C, 0, BACKGROUND_SELECTION_SELECTOR, 0, 0, 0)) + bytes(REPORT_BYTES - 7)


def build_background_selection_get_buffer() -> bytes:
    return bytes((0x10, BACKGROUND_SELECTION_SELECTOR)) + bytes(REPORT_BYTES - 2)


def decode_background_selection_response(data: bytes) -> BackgroundSelection:
    """Decode native selector2c's eight-byte response without inventing a checksum.

    Only the all-zero built-in response is captured so far. The source consumes
    the index at byte3; bytes4..7 are retained with their meaning unresolved.
    An unfamiliar index is preserved for diagnostics, but cannot be written.
    """
    raw = _bytes(data, BACKGROUND_SELECTION_RESPONSE_BYTES, "Background selection response")
    if raw[:3] != bytes((0x10, BACKGROUND_SELECTION_SELECTOR, 0)):
        raise ProtocolError("Background selection report, selector or status does not match")
    return BackgroundSelection(raw, raw[3])


def build_background_selection_report(background_index: int) -> bytes:
    """Choose the built-in image (0) or successfully uploaded custom image (1).

    This ordinary F2-acknowledged command follows all A5 upload steps. Selection
    readback can verify the chosen index, but cannot verify the image pixels.
    """
    if type(background_index) is not int or background_index not in (BUILTIN_BACKGROUND_INDEX,
                                                                    CUSTOM_BACKGROUND_INDEX):
        raise ProtocolError("Background selection must be built-in (0) or custom (1)")
    return bytes((0x10, BACKGROUND_SELECTION_SELECTOR, background_index, 0, 0, 0, 0)) + bytes(REPORT_BYTES - 7)
