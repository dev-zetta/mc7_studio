"""Bounded local image preparation and preview for the MC7 LCD background."""

from dataclasses import dataclass
import os
from pathlib import Path
import stat

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QImage, QImageReader, QPixmap, QTransform
from PySide6.QtWidgets import QDialog, QFileDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from .widgets import STYLE, label


MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
PREVIEW_WIDTH, PREVIEW_HEIGHT = 284, 76


@dataclass(frozen=True)
class PreparedBackground:
    filename: str
    preview: QImage
    # Vendor framebuffer orientation: 76 columns x 284 rows, RGBA8888.
    rgba: bytes


def prepare_background(path: str | Path) -> PreparedBackground:
    path = Path(path)
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
    source = reader.read()
    if source.isNull():
        raise ValueError(f"The image could not be decoded: {reader.errorString()}")
    if source.width() * source.height() > MAX_IMAGE_PIXELS:
        raise ValueError("Images must contain at most 16 million pixels.")
    # Crop excess source area before expanding. A valid 1 x 16,000,000
    # image must not create a multi-billion-pixel scaling intermediate.
    if source.width() * PREVIEW_HEIGHT > source.height() * PREVIEW_WIDTH:
        width = min(source.width(), max(1, round(source.height() * PREVIEW_WIDTH / PREVIEW_HEIGHT)))
        source = source.copy((source.width() - width) // 2, 0, width, source.height())
    else:
        height = min(source.height(), max(1, round(source.width() * PREVIEW_HEIGHT / PREVIEW_WIDTH)))
        source = source.copy(0, (source.height() - height) // 2, source.width(), height)
    scaled = source.scaled(PREVIEW_WIDTH, PREVIEW_HEIGHT, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                           Qt.TransformationMode.SmoothTransformation)
    preview = scaled.copy((scaled.width() - PREVIEW_WIDTH) // 2, (scaled.height() - PREVIEW_HEIGHT) // 2,
                          PREVIEW_WIDTH, PREVIEW_HEIGHT).convertToFormat(QImage.Format.Format_RGBA8888)
    wire = preview.transformed(QTransform().rotate(270)).convertToFormat(QImage.Format.Format_RGBA8888)
    rgba = bytes(wire.constBits())
    if wire.width() != 76 or wire.height() != 284 or len(rgba) != 86_336:
        raise ValueError("The image could not be converted to the mouse display format.")
    return PreparedBackground(str(path), preview, rgba)


class BackgroundDialog(QDialog):
    """Preview locally; Accepted explicitly requests an upload by the caller."""

    def __init__(self, parent=None, *, can_upload=False):
        super().__init__(parent)
        self.can_upload = can_upload
        self.prepared: PreparedBackground | None = None
        self.setWindowTitle("LCD background")
        self.resize(690, 390)
        self.setStyleSheet(STYLE)
        body = QVBoxLayout(self)
        body.setContentsMargins(24, 22, 24, 22)
        body.setSpacing(16)
        body.addWidget(label("Make the display yours", "title"))
        body.addWidget(label("Choose a PNG or JPEG. The image is fitted and cropped from the center to the 284 × 76 display. Uploading changes the background for every mouse profile.", "muted", True))
        self.preview = QLabel("Choose an image to preview")
        self.preview.setAccessibleName("LCD background preview")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(568, 152)
        self.preview.setStyleSheet("background: #101216; border: 1px solid #514762; border-radius: 8px; padding: 9px;")
        body.addWidget(self.preview)
        self.details = label("PNG / JPEG · up to 16 MiB and 16 million pixels", "muted", True)
        body.addWidget(self.details)
        self.message = label("Connect a supported USB mouse to upload the background." if not can_upload else "The image stays local until you click Upload background. A transfer can take up to two minutes.", "notice", True)
        body.addWidget(self.message)
        buttons = QHBoxLayout()
        self.choose_button = QPushButton("Choose image…")
        self.choose_button.clicked.connect(self.choose_image)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        self.upload_button = QPushButton("Upload background")
        self.upload_button.setObjectName("primary")
        self.upload_button.setEnabled(False)
        self.upload_button.clicked.connect(self.accept)
        for button in (self.choose_button, self.cancel_button, self.upload_button):
            button.setAutoDefault(False)
            buttons.addWidget(button)
        body.addLayout(buttons)

    def choose_image(self):
        filename, _ = QFileDialog.getOpenFileName(self, "Choose LCD background", "", "PNG or JPEG images (*.png *.jpg *.jpeg)")
        if not filename:
            return
        try:
            prepared = prepare_background(filename)
        except (OSError, ValueError) as error:
            self.prepared = None
            self.preview.setText("Choose an image to preview")
            self.details.setText("No image loaded")
            self.upload_button.setEnabled(False)
            self.message.setText(str(error))
            self.message.setObjectName("error")
            self.message.style().unpolish(self.message)
            self.message.style().polish(self.message)
            return
        self.prepared = prepared
        self.preview.setPixmap(QPixmap.fromImage(prepared.preview.scaled(568, 152, Qt.AspectRatioMode.KeepAspectRatio,
                                                                       Qt.TransformationMode.SmoothTransformation)))
        self.details.setText(f"{Path(filename).name} · Preview: 284 × 76 pixels")
        self.message.setText("The image stays local until you click Upload background. A transfer can take up to two minutes." if self.can_upload else "Preview ready. Connect a supported USB mouse to upload.")
        self.message.setObjectName("notice")
        self.message.style().unpolish(self.message)
        self.message.style().polish(self.message)
        self.upload_button.setEnabled(self.can_upload)

    def accept(self):
        if self.can_upload and self.prepared is not None:
            super().accept()
