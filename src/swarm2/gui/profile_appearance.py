"""Bounded, portable profile-library thumbnails; never sent to the mouse."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import os
from pathlib import Path
import stat

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QImageReader, QPainter, QPen, QPixmap

from ..configuration import (
    MAX_PROFILE_IMAGE_BYTES, PROFILE_IMAGE_PREFIX, ConfigurationError,
    ProfileAppearance, profile_image_bytes,
)


MAX_SOURCE_IMAGE_BYTES = 16 * 1024 * 1024
MAX_SOURCE_IMAGE_PIXELS = 16_000_000
PROFILE_THUMBNAIL_SIZE = 96


@dataclass(frozen=True)
class PreparedProfileImage:
    filename: str
    data_url: str
    preview: QImage


def prepare_profile_image(path: str | Path) -> PreparedProfileImage:
    """Read PNG/JPEG safely and normalize it to a small embedded PNG."""

    path = Path(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Choose a regular PNG or JPEG image file.")
        if info.st_size > MAX_SOURCE_IMAGE_BYTES:
            raise ValueError("Profile images must be no larger than 16 MiB.")
        content = source.read(MAX_SOURCE_IMAGE_BYTES + 1)
    if len(content) > MAX_SOURCE_IMAGE_BYTES:
        raise ValueError("Profile images must be no larger than 16 MiB.")

    source_buffer = QBuffer()
    source_buffer.setData(QByteArray(content))
    source_buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(source_buffer)
    if bytes(reader.format()).lower() not in (b"png", b"jpeg", b"jpg"):
        raise ValueError("Choose a PNG or JPEG profile image.")
    dimensions = reader.size()
    if not dimensions.isValid() or dimensions.width() <= 0 or dimensions.height() <= 0:
        raise ValueError("The profile image dimensions could not be read.")
    if dimensions.width() * dimensions.height() > MAX_SOURCE_IMAGE_PIXELS:
        raise ValueError("Profile images must contain at most 16 million pixels.")
    reader.setAutoTransform(True)
    image = reader.read()
    if image.isNull():
        raise ValueError(f"The profile image could not be decoded: {reader.errorString()}")
    if image.width() * image.height() > MAX_SOURCE_IMAGE_PIXELS:
        raise ValueError("Profile images must contain at most 16 million pixels.")

    side = min(image.width(), image.height())
    square = image.copy(
        (image.width() - side) // 2,
        (image.height() - side) // 2,
        side,
        side,
    )
    preview = square.scaled(
        PROFILE_THUMBNAIL_SIZE,
        PROFILE_THUMBNAIL_SIZE,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    ).convertToFormat(QImage.Format.Format_RGBA8888)
    encoded = QByteArray()
    output = QBuffer(encoded)
    if not output.open(QIODevice.OpenModeFlag.WriteOnly) or not preview.save(output, "PNG"):
        raise ValueError("The profile image could not be converted to PNG.")
    raw = bytes(encoded)
    if len(raw) > MAX_PROFILE_IMAGE_BYTES:
        raise ValueError("The normalized profile image exceeds 256 KiB.")
    data_url = PROFILE_IMAGE_PREFIX + base64.b64encode(raw).decode("ascii")
    profile_image_bytes(data_url)
    return PreparedProfileImage(str(path), data_url, preview)


def decode_profile_image(data_url: str | None) -> QImage:
    """Decode already-validated profile data, returning null on corruption."""

    if data_url is None:
        return QImage()
    try:
        raw = profile_image_bytes(data_url)
    except ConfigurationError:
        return QImage()
    image = QImage.fromData(QByteArray(raw), "PNG")
    if image.isNull():
        return QImage()
    return image.scaled(
        PROFILE_THUMBNAIL_SIZE,
        PROFILE_THUMBNAIL_SIZE,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def profile_icon(appearance: ProfileAppearance, size: int = 40) -> QIcon:
    """Render the library color and optional thumbnail into one local icon."""

    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    color = QColor(appearance.color)
    painter.setPen(QPen(color, 3))
    painter.setBrush(color)
    bounds = QRectF(2, 2, size - 4, size - 4)
    painter.drawRoundedRect(bounds, 7, 7)
    image = decode_profile_image(appearance.image)
    if not image.isNull():
        painter.drawImage(QRectF(5, 5, size - 10, size - 10), image)
    painter.end()
    return QIcon(pixmap)
