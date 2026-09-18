"""Bounded Qt preparation for MC7 Open Application LCD icons."""

from dataclasses import dataclass
import os
from pathlib import Path
import stat

from PySide6.QtCore import QBuffer, QByteArray, QFileInfo, QIODevice, QSize, Qt
from PySide6.QtGui import QImage, QImageReader, QPainter, QTransform
from PySide6.QtWidgets import QFileIconProvider

from .background import MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS


ICON_CANVAS_WIDTH, ICON_CANVAS_HEIGHT = 64, 62
ICON_CONTENT_WIDTH, ICON_CONTENT_HEIGHT = 28, 26
ICON_WIRE_WIDTH, ICON_WIRE_HEIGHT = 62, 64
ICON_WIRE_RGBA_BYTES = ICON_WIRE_WIDTH * ICON_WIRE_HEIGHT * 4


@dataclass(frozen=True)
class PreparedCustomIcon:
    filename: str
    preview: QImage
    # Vendor framebuffer orientation: 62 columns x 64 rows, RGBA8888.
    rgba: bytes


def _render_icon(source: QImage, filename: str) -> PreparedCustomIcon:
    if source.isNull() or source.width() <= 0 or source.height() <= 0:
        raise ValueError("The icon could not be decoded.")
    canvas = QImage(
        ICON_CANVAS_WIDTH, ICON_CANVAS_HEIGHT, QImage.Format.Format_RGBA8888)
    canvas.fill(Qt.GlobalColor.transparent)
    scaled = source.scaled(
        ICON_CONTENT_WIDTH, ICON_CONTENT_HEIGHT,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    ).convertToFormat(QImage.Format.Format_RGBA8888)
    painter = QPainter(canvas)
    painter.drawImage(
        (ICON_CANVAS_WIDTH - scaled.width()) // 2,
        (ICON_CANVAS_HEIGHT - scaled.height()) // 2,
        scaled,
    )
    painter.end()
    wire = canvas.transformed(QTransform().rotate(270)).convertToFormat(
        QImage.Format.Format_RGBA8888)
    rgba = bytes(wire.constBits())
    if ((wire.width(), wire.height()) != (ICON_WIRE_WIDTH, ICON_WIRE_HEIGHT)
            or len(rgba) != ICON_WIRE_RGBA_BYTES):
        raise ValueError("The icon could not be converted to the mouse display format.")
    return PreparedCustomIcon(filename, canvas, rgba)


def _read_image(path: Path) -> QImage:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Choose a regular PNG or JPEG image file.")
        if info.st_size > MAX_IMAGE_BYTES:
            raise ValueError("Images must be no larger than 16 MiB.")
        content = source.read(MAX_IMAGE_BYTES + 1)
    if len(content) > MAX_IMAGE_BYTES:
        raise ValueError("Images must be no larger than 16 MiB.")
    buffer = QBuffer()
    buffer.setData(QByteArray(content))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    if bytes(reader.format()).lower() not in (b"png", b"jpeg", b"jpg"):
        raise ValueError("Choose a PNG or JPEG image.")
    size = reader.size()
    if not size.isValid() or size.width() <= 0 or size.height() <= 0:
        raise ValueError("The image dimensions could not be read.")
    if size.width() * size.height() > MAX_IMAGE_PIXELS:
        raise ValueError("Images must contain at most 16 million pixels.")
    reader.setAutoTransform(True)
    image = reader.read()
    if image.isNull():
        raise ValueError(f"The image could not be decoded: {reader.errorString()}")
    if image.width() * image.height() > MAX_IMAGE_PIXELS:
        raise ValueError("Images must contain at most 16 million pixels.")
    return image


def prepare_custom_icon(path: str | Path) -> PreparedCustomIcon:
    """Load a user image and render the source-confirmed native icon canvas."""
    source_path = Path(path)
    return _render_icon(_read_image(source_path), os.fspath(source_path))


def prepare_application_icon(path: str | Path) -> PreparedCustomIcon:
    """Render the platform's icon for an application path without launching it."""
    source_path = Path(path)
    try:
        info = source_path.stat()
    except OSError as error:
        raise ValueError("The selected application does not exist.") from error
    if not (stat.S_ISREG(info.st_mode)
            or (stat.S_ISDIR(info.st_mode) and source_path.suffix.lower() == ".app")):
        raise ValueError("Choose an application file or a macOS .app bundle.")
    icon = QFileIconProvider().icon(QFileInfo(os.fspath(source_path)))
    pixmap = icon.pixmap(QSize(256, 248))
    if pixmap.isNull():
        raise ValueError("The operating system did not provide an application icon.")
    return _render_icon(pixmap.toImage(), os.fspath(source_path))


def preview_from_wire_rgba(rgba: bytes) -> QImage:
    """Recreate the 64x62 editor preview from one exact wire RGBA payload."""
    if not isinstance(rgba, bytes) or len(rgba) != ICON_WIRE_RGBA_BYTES:
        raise ValueError("An application icon must contain exactly 62 by 64 RGBA pixels.")
    wire = QImage(
        rgba, ICON_WIRE_WIDTH, ICON_WIRE_HEIGHT,
        ICON_WIRE_WIDTH * 4, QImage.Format.Format_RGBA8888,
    ).copy()
    preview = wire.transformed(QTransform().rotate(90)).convertToFormat(
        QImage.Format.Format_RGBA8888)
    if (preview.width(), preview.height()) != (ICON_CANVAS_WIDTH, ICON_CANVAS_HEIGHT):
        raise ValueError("The application icon preview could not be reconstructed.")
    return preview
