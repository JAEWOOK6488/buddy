"""Multi-monitor screenshots for the model, labeled the way the system prompt expects."""

from dataclasses import dataclass, field
from typing import List, Optional

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRect, QRectF, Qt
from PyQt6.QtGui import QColor, QCursor, QGuiApplication, QPainter, QPen

JPEG_QUALITY = 80

POINTER_MARKER_COLOR = QColor(239, 68, 68)
ELEMENT_MARKER_COLOR = QColor(249, 115, 22)
TRAIL_MARKER_COLOR = QColor(234, 179, 8)
# A trail shorter than this (in logical pixels) is jitter, not a gesture.
MINIMUM_MEANINGFUL_TRAIL_LENGTH = 120.0
# ...and it must also spread at least this far, so hand tremor along a line doesn't count.
MINIMUM_MEANINGFUL_TRAIL_SPAN = 40.0


@dataclass
class PointerAnnotation:
    """What the user pointed at, in Qt logical global coordinates."""

    pointer_point: QPointF
    element_rectangle: Optional[QRectF]
    # Path the mouse traced while Ctrl+Alt was held (may circle or underline something).
    trail_points: List[QPointF] = field(default_factory=list)

    def has_meaningful_trail(self) -> bool:
        trail_length = 0.0
        for point_index in range(1, len(self.trail_points)):
            previous_point = self.trail_points[point_index - 1]
            current_point = self.trail_points[point_index]
            trail_length += ((current_point.x() - previous_point.x()) ** 2 + (current_point.y() - previous_point.y()) ** 2) ** 0.5
        if trail_length < MINIMUM_MEANINGFUL_TRAIL_LENGTH:
            return False
        x_values = [trail_point.x() for trail_point in self.trail_points]
        y_values = [trail_point.y() for trail_point in self.trail_points]
        return max(max(x_values) - min(x_values), max(y_values) - min(y_values)) >= MINIMUM_MEANINGFUL_TRAIL_SPAN


@dataclass
class ScreenCapture:
    jpeg_bytes: bytes
    label: str
    image_width: int
    image_height: int
    # Screen rectangle in Qt logical (device-independent) global coordinates.
    # Pointing coordinates from the model are mapped back into this space.
    screen_geometry: QRect
    is_cursor_screen: bool
    # Set on the cursor screen in point mode: the marks drawn into the image, in image pixels.
    pointer_in_image: Optional[QPointF] = None
    element_in_image: Optional[QRectF] = None


def _logical_to_image(logical_point: QPointF, screen_geometry: QRect, image_width: int, image_height: int) -> QPointF:
    return QPointF(
        (logical_point.x() - screen_geometry.x()) * image_width / screen_geometry.width(),
        (logical_point.y() - screen_geometry.y()) * image_height / screen_geometry.height(),
    )


def _draw_pointer_annotation(image, screen_geometry: QRect, pointer_annotation: PointerAnnotation):
    """Marks the pointed element (orange box) and the pointer (red ring) on the full-size image,
    so the model sees exactly what the user pointed at. Returns both in full-size image pixels."""
    pointer_in_image = _logical_to_image(
        pointer_annotation.pointer_point, screen_geometry, image.width(), image.height()
    )
    element_in_image = None
    if pointer_annotation.element_rectangle is not None:
        element_rectangle = pointer_annotation.element_rectangle
        element_top_left = _logical_to_image(element_rectangle.topLeft(), screen_geometry, image.width(), image.height())
        element_bottom_right = _logical_to_image(
            element_rectangle.bottomRight(), screen_geometry, image.width(), image.height()
        )
        element_in_image = QRectF(element_top_left, element_bottom_right)

    # Marker sizes scale with the image so they stay visible after downscaling.
    stroke_width = max(3.0, image.width() / 640.0)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    if pointer_annotation.has_meaningful_trail():
        trail_pen = QPen(TRAIL_MARKER_COLOR, stroke_width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        painter.setPen(trail_pen)
        trail_in_image = [
            _logical_to_image(trail_point, screen_geometry, image.width(), image.height())
            for trail_point in pointer_annotation.trail_points
        ]
        for point_index in range(1, len(trail_in_image)):
            painter.drawLine(trail_in_image[point_index - 1], trail_in_image[point_index])
    if element_in_image is not None:
        painter.setPen(QPen(ELEMENT_MARKER_COLOR, stroke_width))
        painter.drawRect(element_in_image)
    ring_radius = stroke_width * 5.0
    painter.setPen(QPen(POINTER_MARKER_COLOR, stroke_width))
    painter.drawEllipse(pointer_in_image, ring_radius, ring_radius)
    painter.end()
    return pointer_in_image, element_in_image


def capture_all_screens(
    max_dimension: int, pointer_annotation: Optional[PointerAnnotation] = None
) -> List[ScreenCapture]:
    """Grabs every connected screen. The screen under the cursor always comes first,
    so "screen1" in the model's reply means the cursor screen."""
    all_screens = QGuiApplication.screens()
    cursor_screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()

    sorted_screens = [cursor_screen] + [screen for screen in all_screens if screen is not cursor_screen]
    screen_count = len(sorted_screens)

    captures: List[ScreenCapture] = []
    for screen_index, screen in enumerate(sorted_screens):
        # grabWindow(0) grabs this screen's area of the X11 root window at native resolution.
        full_resolution_image = screen.grabWindow(0).toImage()

        pointer_in_full_image = None
        element_in_full_image = None
        if pointer_annotation is not None and screen is cursor_screen:
            pointer_in_full_image, element_in_full_image = _draw_pointer_annotation(
                full_resolution_image, screen.geometry(), pointer_annotation
            )

        image_to_send = full_resolution_image
        if max(full_resolution_image.width(), full_resolution_image.height()) > max_dimension:
            image_to_send = full_resolution_image.scaled(
                max_dimension,
                max_dimension,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )

        jpeg_byte_array = QByteArray()
        jpeg_buffer = QBuffer(jpeg_byte_array)
        jpeg_buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image_to_send.save(jpeg_buffer, "JPG", JPEG_QUALITY)
        jpeg_buffer.close()

        is_cursor_screen = screen is cursor_screen
        if screen_count == 1:
            screen_label = "user's screen (cursor is here)"
        elif is_cursor_screen:
            screen_label = f"screen {screen_index + 1} of {screen_count} — cursor is on this screen (primary focus)"
        else:
            screen_label = f"screen {screen_index + 1} of {screen_count} — secondary screen"

        # Convert the marks from full-size to sent-image pixels.
        downscale_factor = image_to_send.width() / full_resolution_image.width()
        pointer_in_image = None
        element_in_image = None
        if pointer_in_full_image is not None:
            pointer_in_image = QPointF(
                pointer_in_full_image.x() * downscale_factor, pointer_in_full_image.y() * downscale_factor
            )
        if element_in_full_image is not None:
            element_in_image = QRectF(
                element_in_full_image.x() * downscale_factor,
                element_in_full_image.y() * downscale_factor,
                element_in_full_image.width() * downscale_factor,
                element_in_full_image.height() * downscale_factor,
            )

        captures.append(
            ScreenCapture(
                jpeg_bytes=bytes(jpeg_byte_array),
                label=screen_label,
                image_width=image_to_send.width(),
                image_height=image_to_send.height(),
                screen_geometry=screen.geometry(),
                is_cursor_screen=is_cursor_screen,
                pointer_in_image=pointer_in_image,
                element_in_image=element_in_image,
            )
        )

    return captures
