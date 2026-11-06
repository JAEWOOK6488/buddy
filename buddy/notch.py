"""The notch: a black pill hanging from the top center of the screen.

Collapsed, it is a thin lip that blends into the GNOME top bar and shows buddy's state
as a small colored bar. Expanded (hotkey or click), it holds the question input and the
streamed answer.
"""

import enum
import math

from PyQt6.QtCore import QEasingCurve, QEvent, QPropertyAnimation, QRect, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QCursor, QFont, QGuiApplication, QLinearGradient, QPainter, QPainterPath
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QVBoxLayout, QWidget

from .x11_focus import force_keyboard_focus

# Collapsed, the notch is only a status-colored underline right below GNOME's top bar
# (centered under the clock), so it never covers the clock. The strip is a little taller
# than the line so it can still be clicked.
COLLAPSED_WIDTH = 220
COLLAPSED_HEIGHT = 8
COLLAPSED_LINE_WIDTH = 120
COLLAPSED_LINE_HEIGHT = 2.0
EXPANDED_WIDTH = 660
EXPANDED_HEIGHT_WITHOUT_ANSWER = 118
EXPANDED_HEIGHT_WITH_ANSWER = 340

# Concave "ears" where the notch meets the top edge, like a hardware notch.
EAR_RADIUS = 10.0
BOTTOM_CORNER_RADIUS = 22.0
ANIMATION_DURATION_MILLISECONDS = 220

NOTCH_BACKGROUND_COLOR = QColor(8, 8, 10, 248)

STATUS_COLORS = {
    "idle": QColor(120, 120, 128),
    "thinking": QColor(59, 130, 246),
    "speaking": QColor(52, 211, 153),
    "error": QColor(248, 113, 113),
    "stopped": QColor(251, 191, 36),
    "waiting": QColor(167, 139, 250),
    "acting": QColor(232, 121, 249),
    "drawing": QColor(34, 211, 238),
}
STATUS_TEXTS = {
    "idle": "ready",
    "thinking": "looking at your screen…",
    "speaking": "speaking",
    "error": "something went wrong",
    "stopped": "stopped",
    "waiting": "waiting for your OK",
    "acting": "doing it…",
    "drawing": "drawing the visual…",
}

CONTENT_STYLE_SHEET = """
QLabel#titleLabel { color: #f4f4f5; font-weight: 600; }
QLabel#statusLabel, QLabel#modelLabel, QLabel#hintLabel { color: #8b8b94; }
QLabel#questionLabel { color: #a1a1aa; }
QPlainTextEdit#answerView {
    background: transparent; border: none; color: #f4f4f5;
    selection-background-color: #2563eb;
}
QLineEdit#questionInput {
    background: #1c1c21; border: 1px solid #2e2e35; border-radius: 12px;
    color: #f4f4f5; padding: 8px 12px; selection-background-color: #2563eb;
}
QLineEdit#questionInput:focus { border: 1px solid #3b82f6; }
QLabel#actionLabel {
    background: #2e1065; border: 1px solid #7c3aed; border-radius: 10px;
    color: #ede9fe; padding: 8px 12px;
}
QScrollBar:vertical { background: transparent; width: 6px; }
QScrollBar::handle:vertical { background: #3f3f46; border-radius: 3px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""


class NotchState(enum.Enum):
    COLLAPSED = "collapsed"
    EXPANDED = "expanded"


class _StatusDot(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedSize(10, 10)
        self.color = STATUS_COLORS["idle"]

    def paintEvent(self, paint_event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.color)
        painter.drawEllipse(1, 1, 8, 8)
        painter.end()


class NotchWindow(QWidget):
    question_submitted = pyqtSignal(str)
    # The controller decides: stop a running answer first, otherwise collapse.
    escape_pressed = pyqtSignal()
    # Enter on an empty input while a step is proposed.
    action_approved = pyqtSignal()

    def __init__(self, hotkey_display_text: str, stop_hotkey_display_text: str):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            # Bypassing the window manager lets the notch overlap the GNOME top bar.
            | Qt.WindowType.X11BypassWindowManagerHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        self._notch_state = NotchState.COLLAPSED
        self._status_name = "idle"
        self._has_answer = False
        self._animation_phase = 0.0

        self._stop_hotkey_display_text = stop_hotkey_display_text
        self._build_content(hotkey_display_text, stop_hotkey_display_text)

        self._geometry_animation = QPropertyAnimation(self, b"geometry", self)
        self._geometry_animation.setDuration(ANIMATION_DURATION_MILLISECONDS)
        self._geometry_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._geometry_animation.finished.connect(self._on_geometry_animation_finished)

        # Drives the shimmering status bar while thinking or speaking.
        self._status_animation_timer = QTimer(self)
        self._status_animation_timer.setInterval(33)
        self._status_animation_timer.timeout.connect(self._advance_status_animation)

        self.setGeometry(self._collapsed_rectangle())

    # ----- layout -----

    def _build_content(self, hotkey_display_text: str, stop_hotkey_display_text: str) -> None:
        self._content_widget = QWidget(self)
        self._content_widget.setStyleSheet(CONTENT_STYLE_SHEET)

        title_label = QLabel("buddy")
        title_label.setObjectName("titleLabel")
        self._status_dot = _StatusDot()
        self._status_label = QLabel(STATUS_TEXTS["idle"])
        self._status_label.setObjectName("statusLabel")
        self._model_label = QLabel("")
        self._model_label.setObjectName("modelLabel")
        hint_label = QLabel(f"stop: Esc / {stop_hotkey_display_text}")
        hint_label.setObjectName("hintLabel")
        hint_label.setToolTip(f"{hotkey_display_text}: open or close the notch")

        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(8)
        header_layout.addWidget(self._status_dot)
        header_layout.addWidget(title_label)
        header_layout.addWidget(self._status_label)
        header_layout.addStretch(1)
        header_layout.addWidget(self._model_label)
        header_layout.addWidget(hint_label)

        self._question_label = QLabel("")
        self._question_label.setObjectName("questionLabel")
        self._question_label.setWordWrap(True)
        self._question_label.hide()

        self._answer_view = QPlainTextEdit()
        self._answer_view.setObjectName("answerView")
        self._answer_view.setReadOnly(True)
        self._answer_view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        answer_font = QFont()
        answer_font.setPointSizeF(11.5)
        self._answer_view.setFont(answer_font)
        self._answer_view.hide()

        self._action_label = QLabel("")
        self._action_label.setObjectName("actionLabel")
        self._action_label.setWordWrap(True)
        self._action_label.hide()
        self._is_action_pending = False

        self._question_input = QLineEdit()
        self._question_input.setObjectName("questionInput")
        self._question_input.setPlaceholderText("Ask about anything on your screen…  (Enter)")
        self._question_input.returnPressed.connect(self._submit_question)
        # Override-redirect windows get no focus from clicks; take it explicitly.
        self._question_input.installEventFilter(self)

        content_layout = QVBoxLayout(self._content_widget)
        # Top margin keeps the header below the ear curves.
        content_layout.setContentsMargins(int(EAR_RADIUS) + 20, 14, int(EAR_RADIUS) + 20, 18)
        content_layout.setSpacing(10)
        content_layout.addLayout(header_layout)
        content_layout.addWidget(self._question_label)
        content_layout.addWidget(self._answer_view, 1)
        content_layout.addWidget(self._action_label)
        content_layout.addWidget(self._question_input)

        self._content_widget.hide()

    def resizeEvent(self, resize_event) -> None:
        self._content_widget.setGeometry(self.rect())
        super().resizeEvent(resize_event)

    # ----- geometry -----

    @staticmethod
    def _below_top_bar(screen) -> QRect:
        """The screen area below GNOME's top bar: full width, starting where the work area starts."""
        screen_geometry = screen.geometry()
        top_bar_bottom = max(screen_geometry.top(), screen.availableGeometry().top())
        return QRect(screen_geometry.left(), top_bar_bottom, screen_geometry.width(), screen_geometry.bottom() - top_bar_bottom)

    def _target_screen_geometry(self) -> QRect:
        target_screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        return self._below_top_bar(target_screen)

    def _collapsed_rectangle(self) -> QRect:
        screen_geometry = self._below_top_bar(QGuiApplication.primaryScreen())
        return QRect(
            screen_geometry.center().x() - COLLAPSED_WIDTH // 2,
            screen_geometry.top(),
            COLLAPSED_WIDTH,
            COLLAPSED_HEIGHT,
        )

    def _expanded_rectangle(self) -> QRect:
        screen_geometry = self._target_screen_geometry()
        expanded_height = EXPANDED_HEIGHT_WITH_ANSWER if self._has_answer else EXPANDED_HEIGHT_WITHOUT_ANSWER
        return QRect(
            screen_geometry.center().x() - EXPANDED_WIDTH // 2,
            screen_geometry.top(),
            EXPANDED_WIDTH,
            expanded_height,
        )

    def _animate_to(self, target_rectangle: QRect) -> None:
        self._geometry_animation.stop()
        self._geometry_animation.setStartValue(self.geometry())
        self._geometry_animation.setEndValue(target_rectangle)
        self._geometry_animation.start()

    def _on_geometry_animation_finished(self) -> None:
        if self._notch_state == NotchState.EXPANDED:
            self._content_widget.show()

    # ----- public API -----

    @property
    def is_expanded(self) -> bool:
        return self._notch_state == NotchState.EXPANDED

    def show_collapsed(self) -> None:
        self.setGeometry(self._collapsed_rectangle())
        self.show()

    def toggle(self) -> None:
        """Hotkey behavior: open and focus; if already open, focus the input first, then close."""
        if not self.is_expanded:
            self.expand()
        elif not self._question_input.hasFocus():
            self.focus_input()
        else:
            self.collapse()

    def expand(self, should_focus_input: bool = True) -> None:
        """Opens the notch. Point mode opens it without focus so the app the user
        pointed at keeps the keyboard."""
        was_expanded = self.is_expanded
        self._notch_state = NotchState.EXPANDED
        self.show()
        self.raise_()
        if not was_expanded:
            self._animate_to(self._expanded_rectangle())
        if should_focus_input:
            # Content appears when the animation ends; focus can be set right away.
            QTimer.singleShot(0, self.focus_input)

    @property
    def is_input_focused(self) -> bool:
        return self._question_input.hasFocus()

    def collapse(self) -> None:
        self._notch_state = NotchState.COLLAPSED
        self._content_widget.hide()
        self._question_input.clearFocus()
        self._animate_to(self._collapsed_rectangle())
        self.update()

    def focus_input(self) -> None:
        self.raise_()
        force_keyboard_focus(int(self.winId()))
        self.activateWindow()
        self._question_input.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def set_model_label(self, model_label: str) -> None:
        self._model_label.setText(model_label)

    def set_status(self, status_name: str) -> None:
        self._status_name = status_name
        self._status_dot.color = STATUS_COLORS[status_name]
        self._status_dot.update()
        self._status_label.setText(STATUS_TEXTS[status_name])
        if status_name in ("thinking", "speaking", "drawing"):
            self._status_animation_timer.start()
        else:
            self._status_animation_timer.stop()
        self.update()

    def show_question(self, question_text: str) -> None:
        self._question_label.setText(question_text)
        self._question_label.show()
        self.set_answer_text("")

    def set_answer_text(self, answer_text: str) -> None:
        self._answer_view.setPlainText(answer_text)
        self._answer_view.verticalScrollBar().setValue(self._answer_view.verticalScrollBar().maximum())
        if not self._has_answer:
            self._has_answer = True
            self._answer_view.show()
            if self.is_expanded:
                self._animate_to(self._expanded_rectangle())

    def show_error(self, error_text: str) -> None:
        self.set_answer_text(error_text)
        self.set_status("error")

    def clear_conversation_view(self) -> None:
        self._has_answer = False
        self._question_label.clear()
        self._question_label.hide()
        self._answer_view.clear()
        self._answer_view.hide()
        if self.is_expanded:
            self._animate_to(self._expanded_rectangle())

    def set_hidden_for_screenshot(self, should_hide: bool) -> None:
        self.setWindowOpacity(0.0 if should_hide else 1.0)

    # ----- events -----

    def show_action_proposal(self, step_number: int, action_description: str) -> None:
        """Shows the proposed step and takes the keyboard so Enter/Esc answer it."""
        self._is_action_pending = True
        self._action_label.setText(
            f"Step {step_number}: {action_description}\nEnter = do it   ·   Esc = stop   ·   or type something else"
        )
        self._action_label.show()
        self._question_input.setPlaceholderText("Enter to approve this step…")
        self.set_status("waiting")
        self.expand(should_focus_input=True)

    def show_action_running(self, step_number: int, action_description: str) -> None:
        """Shows the step being carried out automatically; leaves the keyboard with the app."""
        self._is_action_pending = False
        self._action_label.setText(f"Step {step_number}: {action_description}\n{self._stop_hotkey_display_text} = stop")
        self._action_label.show()
        self.set_status("acting")
        self.expand(should_focus_input=False)

    def clear_action_proposal(self) -> None:
        self._is_action_pending = False
        self._action_label.hide()
        self._question_input.setPlaceholderText("Ask about anything on your screen…  (Enter)")

    @property
    def is_action_pending(self) -> bool:
        return self._is_action_pending

    def _submit_question(self) -> None:
        question_text = self._question_input.text().strip()
        if not question_text:
            if self._is_action_pending:
                self.action_approved.emit()
            return
        self._question_input.clear()
        self.question_submitted.emit(question_text)

    def eventFilter(self, watched_object, event) -> bool:
        if watched_object is self._question_input and event.type() == QEvent.Type.MouseButtonPress:
            self.focus_input()
        return super().eventFilter(watched_object, event)

    def mousePressEvent(self, mouse_event) -> None:
        if not self.is_expanded:
            self.expand()
        else:
            self.focus_input()
        super().mousePressEvent(mouse_event)

    def keyPressEvent(self, key_event) -> None:
        if key_event.key() == Qt.Key.Key_Escape:
            self.escape_pressed.emit()
            return
        super().keyPressEvent(key_event)

    def _advance_status_animation(self) -> None:
        self._animation_phase += 0.033
        if not self.is_expanded:
            self.update()

    # ----- painting -----

    def _notch_path(self) -> QPainterPath:
        width = float(self.width())
        height = float(self.height())
        ear_radius = min(EAR_RADIUS, height / 2.0)
        bottom_radius = min(BOTTOM_CORNER_RADIUS, height / 2.0, (width - 2.0 * ear_radius) / 2.0)
        body_left = ear_radius
        body_right = width - ear_radius

        notch_path = QPainterPath()
        notch_path.moveTo(0.0, 0.0)
        notch_path.quadTo(body_left, 0.0, body_left, ear_radius)
        notch_path.lineTo(body_left, height - bottom_radius)
        notch_path.quadTo(body_left, height, body_left + bottom_radius, height)
        notch_path.lineTo(body_right - bottom_radius, height)
        notch_path.quadTo(body_right, height, body_right, height - bottom_radius)
        notch_path.lineTo(body_right, ear_radius)
        notch_path.quadTo(body_right, 0.0, width, 0.0)
        notch_path.closeSubpath()
        return notch_path

    def paintEvent(self, paint_event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        is_fully_collapsed = not self.is_expanded and self.height() <= COLLAPSED_HEIGHT
        if is_fully_collapsed:
            # Only the underline; the black body shows while expanded or animating.
            self._paint_collapsed_status_bar(painter)
        else:
            painter.setBrush(NOTCH_BACKGROUND_COLOR)
            painter.drawPath(self._notch_path())
        painter.end()

    def _paint_collapsed_status_bar(self, painter: QPainter) -> None:
        status_color = QColor(STATUS_COLORS[self._status_name])
        if self._status_name == "idle":
            status_color.setAlpha(120)
        elif self._status_name in ("thinking", "speaking", "drawing"):
            status_color.setAlpha(int(150 + 105 * (0.5 + 0.5 * math.sin(self._animation_phase * 6.0))))

        line_rectangle = QRectF(
            (self.width() - COLLAPSED_LINE_WIDTH) / 2.0, 0.0, COLLAPSED_LINE_WIDTH, COLLAPSED_LINE_HEIGHT
        )
        # Fade out toward both ends so the line reads as a soft underline.
        transparent_color = QColor(status_color)
        transparent_color.setAlpha(0)
        line_gradient = QLinearGradient(line_rectangle.topLeft(), line_rectangle.topRight())
        line_gradient.setColorAt(0.0, transparent_color)
        line_gradient.setColorAt(0.2, status_color)
        line_gradient.setColorAt(0.8, status_color)
        line_gradient.setColorAt(1.0, transparent_color)
        painter.setBrush(line_gradient)
        painter.drawRoundedRect(line_rectangle, COLLAPSED_LINE_HEIGHT / 2.0, COLLAPSED_LINE_HEIGHT / 2.0)
