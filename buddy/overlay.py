"""The blue triangle buddy that follows the cursor and flies to UI elements.

One transparent, click-through, override-redirect window covers each screen. The buddy
state lives in BuddyOverlay and is painted in global coordinates, so a flight can cross
from one monitor to another.

Flight timing and arc shape follow Clicky's OverlayWindow.swift (MIT, see THIRD_PARTY_NOTICES.md).
"""

import enum
import math
import time
from typing import List, Optional, Tuple

from PyQt6.QtCore import QObject, QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QCursor, QFont, QFontMetricsF, QGuiApplication, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QWidget

# The buddy sits down-right of the real cursor, pointing back at it.
CURSOR_FOLLOW_OFFSET = QPointF(35.0, 25.0)
FRAME_INTERVAL_MILLISECONDS = 16

TRIANGLE_LENGTH = 20.0
TRIANGLE_HALF_WIDTH = 11.0
# Heading in degrees, Qt screen space (y grows downward): -135 means the tip faces up-left.
RESTING_HEADING_DEGREES = -135.0

BUDDY_FILL_COLOR = QColor(59, 130, 246)
BUDDY_OUTLINE_COLOR = QColor(255, 255, 255, 230)
LABEL_BACKGROUND_COLOR = QColor(37, 99, 235, 235)

# Clicky: duration = distance / 800 px per second, clamped to 0.6-1.4 s; arc height 20% of distance, max 80 px.
FLIGHT_SPEED_PIXELS_PER_SECOND = 800.0
MINIMUM_FLIGHT_SECONDS = 0.6
MAXIMUM_FLIGHT_SECONDS = 1.4
ARC_HEIGHT_RATIO = 0.2
MAXIMUM_ARC_HEIGHT = 80.0
FLIGHT_PEAK_EXTRA_SCALE = 0.3

# How long the buddy keeps pointing: at least this long, and until speech has ended plus a pause.
MINIMUM_POINTING_SECONDS = 3.0
POINTING_PAUSE_AFTER_SPEECH_SECONDS = 1.5

# Painting area around the buddy (global coordinates) that must be repainted every frame.
BUDDY_PAINT_RADIUS = 34.0

# Point-mode highlight: a rounded border around the element under the mouse.
HIGHLIGHT_BORDER_COLOR = QColor(59, 130, 246, 235)
HIGHLIGHT_FILL_COLOR = QColor(59, 130, 246, 28)
HIGHLIGHT_BORDER_WIDTH = 2.5
HIGHLIGHT_CORNER_RADIUS = 6.0
HIGHLIGHT_PADDING = 3.0
# Fraction of the remaining distance the border moves per frame, so it glides between elements.
HIGHLIGHT_SMOOTHING = 0.35

# Point-mode trail: an afterimage of the path the mouse traced while Ctrl+Alt was held.
TRAIL_COLOR = QColor(59, 130, 246)
TRAIL_WIDTH = 4.0
# Each piece of the trail fades out over this many seconds after it was drawn.
TRAIL_LIFETIME_SECONDS = 1.2
# The full path (kept for the screenshot sent to the model) is capped at this many points.
TRAIL_MAXIMUM_RECORDED_POINTS = 1500
# Points closer than this to the previous one are skipped to keep the path light.
TRAIL_MINIMUM_STEP = 2.0


class BuddyVoiceState(enum.Enum):
    IDLE = "idle"
    PROCESSING = "processing"
    RESPONDING = "responding"


class BuddyMotionMode(enum.Enum):
    FOLLOWING_CURSOR = "following_cursor"
    FLYING_TO_TARGET = "flying_to_target"
    POINTING = "pointing"
    FLYING_BACK = "flying_back"


def _interpolate_angle_degrees(start_degrees: float, end_degrees: float, fraction: float) -> float:
    """Interpolates along the shortest way around the circle."""
    difference = (end_degrees - start_degrees + 180.0) % 360.0 - 180.0
    return start_degrees + difference * fraction


def _ease_in_out_cubic(linear_progress: float) -> float:
    if linear_progress < 0.5:
        return 4.0 * linear_progress ** 3
    return 1.0 - (-2.0 * linear_progress + 2.0) ** 3 / 2.0


class _ScreenOverlayWindow(QWidget):
    def __init__(self, screen, buddy_overlay: "BuddyOverlay"):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.X11BypassWindowManagerHint
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self._buddy_overlay = buddy_overlay
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setGeometry(screen.geometry())

    def paintEvent(self, paint_event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Draw in global coordinates; the clip keeps it to this screen.
        painter.translate(-self.x(), -self.y())
        self._buddy_overlay.paint_buddy(painter)
        painter.end()


class _Flight:
    def __init__(self, start_point: QPointF, end_point: QPointF, start_heading_degrees: float):
        self.start_point = QPointF(start_point)
        self.end_point = QPointF(end_point)
        self.start_heading_degrees = start_heading_degrees
        distance = math.hypot(end_point.x() - start_point.x(), end_point.y() - start_point.y())
        self.duration_seconds = min(
            max(distance / FLIGHT_SPEED_PIXELS_PER_SECOND, MINIMUM_FLIGHT_SECONDS), MAXIMUM_FLIGHT_SECONDS
        )
        self.arc_height = min(distance * ARC_HEIGHT_RATIO, MAXIMUM_ARC_HEIGHT)
        self.started_at = time.monotonic()

    def control_point(self) -> QPointF:
        # Midpoint lifted upward so the path is a parabolic hop.
        return QPointF(
            (self.start_point.x() + self.end_point.x()) / 2.0,
            (self.start_point.y() + self.end_point.y()) / 2.0 - self.arc_height,
        )


class BuddyOverlay(QObject):
    def __init__(self):
        super().__init__()
        self._screen_windows: List[_ScreenOverlayWindow] = []
        self._is_shown = False

        self._buddy_position = QPointF(QCursor.pos()) + CURSOR_FOLLOW_OFFSET
        self._heading_degrees = RESTING_HEADING_DEGREES
        self._buddy_scale = 1.0
        self._motion_mode = BuddyMotionMode.FOLLOWING_CURSOR
        self._voice_state = BuddyVoiceState.IDLE
        self._active_flight: Optional[_Flight] = None

        self._pointing_label: Optional[str] = None
        self._pointing_started_at = 0.0
        self._is_speech_active = False
        self._speech_ended_at = 0.0

        self._highlight_target_rectangle: Optional[QRectF] = None
        self._highlight_drawn_rectangle: Optional[QRectF] = None

        # On-screen afterimage: (point, time it was recorded); entries older than the lifetime drop off.
        self._visible_trail: List[Tuple[QPointF, float]] = []
        # Whole path since point mode started, for the screenshot annotation.
        self._recorded_trail_points: List[QPointF] = []
        self._is_recording_trail = False

        self._animation_phase = 0.0
        self._previous_paint_bounds = QRectF()
        self._label_font = QFont()
        self._label_font.setPointSizeF(10.5)
        self._label_font.setBold(True)

        self._frame_timer = QTimer(self)
        self._frame_timer.setInterval(FRAME_INTERVAL_MILLISECONDS)
        self._frame_timer.timeout.connect(self._advance_frame)

        application = QGuiApplication.instance()
        application.screenAdded.connect(self._rebuild_windows_if_shown)
        application.screenRemoved.connect(self._rebuild_windows_if_shown)

    # ----- visibility -----

    @property
    def is_shown(self) -> bool:
        return self._is_shown

    def show(self) -> None:
        if self._is_shown:
            return
        self._is_shown = True
        self._create_windows()
        self._frame_timer.start()

    def hide(self) -> None:
        if not self._is_shown:
            return
        self._is_shown = False
        self._frame_timer.stop()
        self._destroy_windows()

    def set_hidden_for_screenshot(self, should_hide: bool) -> None:
        """Makes the windows fully transparent (without unmapping) while a screenshot is taken."""
        for screen_window in self._screen_windows:
            screen_window.setWindowOpacity(0.0 if should_hide else 1.0)

    def _create_windows(self) -> None:
        for screen in QGuiApplication.screens():
            screen_window = _ScreenOverlayWindow(screen, self)
            screen_window.show()
            self._screen_windows.append(screen_window)

    def _destroy_windows(self) -> None:
        for screen_window in self._screen_windows:
            screen_window.close()
            screen_window.deleteLater()
        self._screen_windows = []

    def _rebuild_windows_if_shown(self, *_unused) -> None:
        if self._is_shown:
            self._destroy_windows()
            self._create_windows()

    # ----- state from the controller -----

    def set_voice_state(self, voice_state: BuddyVoiceState) -> None:
        self._voice_state = voice_state

    def set_speech_active(self, is_speech_active: bool) -> None:
        if self._is_speech_active and not is_speech_active:
            self._speech_ended_at = time.monotonic()
        self._is_speech_active = is_speech_active

    def point_at(self, target_point: QPointF, element_label: Optional[str]) -> None:
        self._pointing_label = element_label
        self._active_flight = _Flight(self._buddy_position, target_point, self._heading_degrees)
        self._motion_mode = BuddyMotionMode.FLYING_TO_TARGET

    def set_highlight_rectangle(self, highlight_rectangle: Optional[QRectF], animate: bool = True) -> None:
        """Shows (or with None, removes) the point-mode border, in global logical coordinates."""
        if highlight_rectangle is None:
            self._highlight_target_rectangle = None
            self._highlight_drawn_rectangle = None
            return
        padded_rectangle = highlight_rectangle.normalized().adjusted(
            -HIGHLIGHT_PADDING, -HIGHLIGHT_PADDING, HIGHLIGHT_PADDING, HIGHLIGHT_PADDING
        )
        self._highlight_target_rectangle = padded_rectangle
        if not animate or self._highlight_drawn_rectangle is None:
            self._highlight_drawn_rectangle = QRectF(padded_rectangle)

    def start_trail(self) -> None:
        """Starts recording a fresh trail from the current mouse position."""
        start_point = QPointF(QCursor.pos())
        self._visible_trail = [(start_point, time.monotonic())]
        self._recorded_trail_points = [start_point]
        self._is_recording_trail = True

    def stop_trail_recording(self) -> None:
        """Stops adding points; what is on screen keeps fading out on its own."""
        self._is_recording_trail = False

    def recorded_trail_points(self) -> List[QPointF]:
        return list(self._recorded_trail_points)

    @property
    def is_pointing(self) -> bool:
        return self._motion_mode in (BuddyMotionMode.FLYING_TO_TARGET, BuddyMotionMode.POINTING)

    @property
    def has_highlight(self) -> bool:
        return self._highlight_target_rectangle is not None

    def cancel_pointing(self) -> None:
        if self._motion_mode in (BuddyMotionMode.FLYING_TO_TARGET, BuddyMotionMode.POINTING):
            self._start_flight_back()

    def _start_flight_back(self) -> None:
        self._pointing_label = None
        cursor_follow_point = QPointF(QCursor.pos()) + CURSOR_FOLLOW_OFFSET
        self._active_flight = _Flight(self._buddy_position, cursor_follow_point, self._heading_degrees)
        self._motion_mode = BuddyMotionMode.FLYING_BACK

    # ----- animation -----

    def _advance_frame(self) -> None:
        now = time.monotonic()
        self._animation_phase += FRAME_INTERVAL_MILLISECONDS / 1000.0

        if self._motion_mode == BuddyMotionMode.FOLLOWING_CURSOR:
            self._buddy_position = QPointF(QCursor.pos()) + CURSOR_FOLLOW_OFFSET
            self._heading_degrees = RESTING_HEADING_DEGREES
            self._buddy_scale = 1.0

        elif self._motion_mode in (BuddyMotionMode.FLYING_TO_TARGET, BuddyMotionMode.FLYING_BACK):
            if self._motion_mode == BuddyMotionMode.FLYING_BACK:
                # The cursor may keep moving during the return flight; chase it.
                self._active_flight.end_point = QPointF(QCursor.pos()) + CURSOR_FOLLOW_OFFSET
            self._advance_flight(now)

        elif self._motion_mode == BuddyMotionMode.POINTING:
            has_pointed_long_enough = now - self._pointing_started_at >= MINIMUM_POINTING_SECONDS
            has_speech_settled = (
                not self._is_speech_active
                and now - self._speech_ended_at >= POINTING_PAUSE_AFTER_SPEECH_SECONDS
            )
            if has_pointed_long_enough and has_speech_settled:
                self._start_flight_back()

        self._advance_highlight()
        self._advance_trail(now)
        self._repaint_dirty_area()

    def _advance_trail(self, now: float) -> None:
        if self._is_recording_trail:
            cursor_point = QPointF(QCursor.pos())
            last_point = self._recorded_trail_points[-1] if self._recorded_trail_points else None
            if last_point is None or math.hypot(
                cursor_point.x() - last_point.x(), cursor_point.y() - last_point.y()
            ) >= TRAIL_MINIMUM_STEP:
                self._visible_trail.append((cursor_point, now))
                self._recorded_trail_points.append(cursor_point)
                if len(self._recorded_trail_points) > TRAIL_MAXIMUM_RECORDED_POINTS:
                    del self._recorded_trail_points[0]
            elif self._visible_trail:
                # Holding still: refresh the tip so it stays bright under the cursor.
                self._visible_trail[-1] = (self._visible_trail[-1][0], now)

        # Drop pieces that have fully faded.
        while self._visible_trail and now - self._visible_trail[0][1] >= TRAIL_LIFETIME_SECONDS:
            del self._visible_trail[0]

    def _advance_highlight(self) -> None:
        if self._highlight_target_rectangle is None or self._highlight_drawn_rectangle is None:
            return
        drawn = self._highlight_drawn_rectangle
        target = self._highlight_target_rectangle
        self._highlight_drawn_rectangle = QRectF(
            drawn.x() + (target.x() - drawn.x()) * HIGHLIGHT_SMOOTHING,
            drawn.y() + (target.y() - drawn.y()) * HIGHLIGHT_SMOOTHING,
            drawn.width() + (target.width() - drawn.width()) * HIGHLIGHT_SMOOTHING,
            drawn.height() + (target.height() - drawn.height()) * HIGHLIGHT_SMOOTHING,
        )

    def _advance_flight(self, now: float) -> None:
        flight = self._active_flight
        linear_progress = min((now - flight.started_at) / flight.duration_seconds, 1.0)

        if linear_progress >= 1.0:
            self._buddy_position = QPointF(flight.end_point)
            self._heading_degrees = RESTING_HEADING_DEGREES
            self._buddy_scale = 1.0
            self._active_flight = None
            if self._motion_mode == BuddyMotionMode.FLYING_TO_TARGET:
                self._motion_mode = BuddyMotionMode.POINTING
                self._pointing_started_at = now
            else:
                self._motion_mode = BuddyMotionMode.FOLLOWING_CURSOR
            return

        eased_progress = _ease_in_out_cubic(linear_progress)
        start_point = flight.start_point
        control_point = flight.control_point()
        end_point = flight.end_point
        remaining = 1.0 - eased_progress

        # Quadratic bezier B(t) = (1-t)^2 P0 + 2(1-t)t P1 + t^2 P2
        self._buddy_position = QPointF(
            remaining * remaining * start_point.x()
            + 2.0 * remaining * eased_progress * control_point.x()
            + eased_progress * eased_progress * end_point.x(),
            remaining * remaining * start_point.y()
            + 2.0 * remaining * eased_progress * control_point.y()
            + eased_progress * eased_progress * end_point.y(),
        )

        # Face along the curve: B'(t) = 2(1-t)(P1-P0) + 2t(P2-P1)
        tangent_x = 2.0 * remaining * (control_point.x() - start_point.x()) + 2.0 * eased_progress * (
            end_point.x() - control_point.x()
        )
        tangent_y = 2.0 * remaining * (control_point.y() - start_point.y()) + 2.0 * eased_progress * (
            end_point.y() - control_point.y()
        )
        travel_heading_degrees = math.degrees(math.atan2(tangent_y, tangent_x))

        # Turn into the travel direction at take-off and back to the resting pose before landing.
        if linear_progress < 0.15:
            self._heading_degrees = _interpolate_angle_degrees(
                flight.start_heading_degrees, travel_heading_degrees, linear_progress / 0.15
            )
        elif linear_progress > 0.75:
            self._heading_degrees = _interpolate_angle_degrees(
                travel_heading_degrees, RESTING_HEADING_DEGREES, (linear_progress - 0.75) / 0.25
            )
        else:
            self._heading_degrees = travel_heading_degrees

        self._buddy_scale = 1.0 + math.sin(linear_progress * math.pi) * FLIGHT_PEAK_EXTRA_SCALE

    def _current_paint_bounds(self) -> QRectF:
        paint_radius = BUDDY_PAINT_RADIUS * self._buddy_scale
        paint_bounds = QRectF(
            self._buddy_position.x() - paint_radius,
            self._buddy_position.y() - paint_radius,
            paint_radius * 2.0,
            paint_radius * 2.0,
        )
        label_rectangle = self._label_rectangle()
        if label_rectangle is not None:
            paint_bounds = paint_bounds.united(label_rectangle.adjusted(-2, -2, 2, 2))
        if self._highlight_drawn_rectangle is not None:
            border_margin = HIGHLIGHT_BORDER_WIDTH + 2.0
            paint_bounds = paint_bounds.united(
                self._highlight_drawn_rectangle.adjusted(-border_margin, -border_margin, border_margin, border_margin)
            )
        if len(self._visible_trail) > 1:
            trail_x_values = [trail_point.x() for trail_point, _ in self._visible_trail]
            trail_y_values = [trail_point.y() for trail_point, _ in self._visible_trail]
            trail_margin = TRAIL_WIDTH + 2.0
            paint_bounds = paint_bounds.united(
                QRectF(
                    min(trail_x_values) - trail_margin,
                    min(trail_y_values) - trail_margin,
                    max(trail_x_values) - min(trail_x_values) + 2.0 * trail_margin,
                    max(trail_y_values) - min(trail_y_values) + 2.0 * trail_margin,
                )
            )
        return paint_bounds

    def _repaint_dirty_area(self) -> None:
        current_bounds = self._current_paint_bounds()
        dirty_bounds = current_bounds.united(self._previous_paint_bounds)
        self._previous_paint_bounds = current_bounds
        for screen_window in self._screen_windows:
            local_dirty_rectangle = dirty_bounds.translated(-screen_window.x(), -screen_window.y()).toAlignedRect()
            visible_dirty_rectangle = local_dirty_rectangle.intersected(screen_window.rect())
            if not visible_dirty_rectangle.isEmpty():
                screen_window.update(visible_dirty_rectangle)

    # ----- painting -----

    def _label_rectangle(self) -> Optional[QRectF]:
        if self._motion_mode != BuddyMotionMode.POINTING or not self._pointing_label:
            return None
        font_metrics = QFontMetricsF(self._label_font)
        text_width = font_metrics.horizontalAdvance(self._pointing_label)
        text_height = font_metrics.height()
        # Below-right of the tip, clear of the triangle body.
        return QRectF(
            self._buddy_position.x() + 18.0,
            self._buddy_position.y() + 20.0,
            text_width + 20.0,
            text_height + 10.0,
        )

    def _paint_trail(self, painter: QPainter) -> None:
        if len(self._visible_trail) < 2:
            return
        now = time.monotonic()
        painter.save()
        trail_color = QColor(TRAIL_COLOR)
        for segment_index in range(len(self._visible_trail) - 1):
            segment_start, start_time = self._visible_trail[segment_index]
            segment_end, end_time = self._visible_trail[segment_index + 1]
            # Each segment fades with its age and thins slightly, like an afterimage.
            segment_age = now - (start_time + end_time) / 2.0
            remaining_fraction = max(0.0, 1.0 - segment_age / TRAIL_LIFETIME_SECONDS)
            if remaining_fraction <= 0.0:
                continue
            trail_color.setAlphaF(remaining_fraction * remaining_fraction)
            segment_width = TRAIL_WIDTH * (0.45 + 0.55 * remaining_fraction)
            painter.setPen(QPen(trail_color, segment_width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawLine(segment_start, segment_end)
        painter.restore()

    def paint_buddy(self, painter: QPainter) -> None:
        self._paint_trail(painter)
        if self._highlight_drawn_rectangle is not None:
            painter.save()
            painter.setPen(QPen(HIGHLIGHT_BORDER_COLOR, HIGHLIGHT_BORDER_WIDTH))
            painter.setBrush(HIGHLIGHT_FILL_COLOR)
            painter.drawRoundedRect(self._highlight_drawn_rectangle, HIGHLIGHT_CORNER_RADIUS, HIGHLIGHT_CORNER_RADIUS)
            painter.restore()

        painter.save()
        painter.translate(self._buddy_position)
        painter.scale(self._buddy_scale, self._buddy_scale)
        painter.rotate(self._heading_degrees)

        # Arrowhead with its tip at the origin, pointing along +x before rotation.
        arrow_path = QPainterPath()
        arrow_path.moveTo(0.0, 0.0)
        arrow_path.lineTo(-TRIANGLE_LENGTH, -TRIANGLE_HALF_WIDTH)
        arrow_path.lineTo(-TRIANGLE_LENGTH * 0.72, 0.0)
        arrow_path.lineTo(-TRIANGLE_LENGTH, TRIANGLE_HALF_WIDTH)
        arrow_path.closeSubpath()

        glow_alpha = 70
        if self._voice_state == BuddyVoiceState.RESPONDING:
            # Pulsing glow while the answer is being spoken.
            glow_alpha = int(70 + 80 * (0.5 + 0.5 * math.sin(self._animation_phase * 6.0)))
        painter.setPen(QPen(QColor(59, 130, 246, glow_alpha), 7.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(arrow_path)

        painter.setPen(QPen(BUDDY_OUTLINE_COLOR, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(BUDDY_FILL_COLOR)
        painter.drawPath(arrow_path)

        if self._voice_state == BuddyVoiceState.PROCESSING:
            # Spinner ring around the arrow body while waiting for the model.
            spinner_center = QPointF(-TRIANGLE_LENGTH * 0.55, 0.0)
            spinner_radius = 19.0
            spinner_rectangle = QRectF(
                spinner_center.x() - spinner_radius,
                spinner_center.y() - spinner_radius,
                spinner_radius * 2.0,
                spinner_radius * 2.0,
            )
            painter.setPen(QPen(BUDDY_FILL_COLOR, 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            start_angle_sixteenths = int(-self._animation_phase * 420.0 * 16) % (360 * 16)
            painter.drawArc(spinner_rectangle, start_angle_sixteenths, 100 * 16)

        painter.restore()

        label_rectangle = self._label_rectangle()
        if label_rectangle is not None:
            painter.save()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(LABEL_BACKGROUND_COLOR)
            painter.drawRoundedRect(label_rectangle, 8.0, 8.0)
            painter.setPen(QColor(255, 255, 255))
            painter.setFont(self._label_font)
            painter.drawText(label_rectangle, Qt.AlignmentFlag.AlignCenter, self._pointing_label)
            painter.restore()
