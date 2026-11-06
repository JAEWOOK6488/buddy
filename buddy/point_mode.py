"""Detects "hold Ctrl+Alt and point": the user holds only Ctrl and Alt, moves the
mouse over something, and releases to have buddy explain it.

The keyboard is read with XQueryKeymap polling instead of a key grab. A grab on
Ctrl+Alt would swallow GNOME shortcuts such as Ctrl+Alt+T; polling only reads the
physical key state, so every other shortcut keeps working and simply cancels point mode.
"""

import enum
import time
from typing import Optional

from PyQt6.QtCore import QObject, QPointF, QTimer, pyqtSignal
from PyQt6.QtGui import QCursor, QGuiApplication
from Xlib import XK
from Xlib import display as xlib_display

from .pointer_inspector import PointerAnchor

POLL_INTERVAL_MILLISECONDS = 30
# Ctrl+Alt must be held alone this long before point mode starts, so quick chords
# like Ctrl+Alt+Space (the notch hotkey) or Ctrl+Alt+T never flash a highlight.
DEFAULT_ARMING_DELAY_SECONDS = 0.6
# Once active, Ctrl+Alt must stay down at least this long for the release to ask buddy.
# A shorter hold (a brief or accidental press) just clears the highlight.
MINIMUM_ACTIVE_SECONDS = 0.5

MODIFIER_KEYSYM_NAMES = {
    "control": ("Control_L", "Control_R"),
    "alt": ("Alt_L", "Alt_R", "Meta_L", "Meta_R"),
}


class _PointModePhase(enum.Enum):
    IDLE = "idle"
    ARMING = "arming"
    ACTIVE = "active"
    # A different chord was used, or point mode just finished: wait until Ctrl/Alt are released.
    WAITING_FOR_RELEASE = "waiting_for_release"


class PointModeDetector(QObject):
    point_mode_started = pyqtSignal(object)  # PointerAnchor
    pointer_moved = pyqtSignal(object)  # PointerAnchor, only while active
    point_mode_released = pyqtSignal(object)  # PointerAnchor at release
    point_mode_cancelled = pyqtSignal()

    def __init__(self, arming_delay_seconds: float = DEFAULT_ARMING_DELAY_SECONDS):
        super().__init__()
        self._arming_delay_seconds = arming_delay_seconds
        self._x_display = xlib_display.Display()
        self._root_window = self._x_display.screen().root

        self._control_keycodes = self._keycodes_for(MODIFIER_KEYSYM_NAMES["control"])
        self._alt_keycodes = self._keycodes_for(MODIFIER_KEYSYM_NAMES["alt"])
        self._modifier_keycodes = self._control_keycodes | self._alt_keycodes

        self._phase = _PointModePhase.IDLE
        self._arming_started_at = 0.0
        self._active_started_at = 0.0
        self._last_physical_position: Optional[tuple] = None
        self._last_anchor: Optional[PointerAnchor] = None
        self._is_enabled = True

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(POLL_INTERVAL_MILLISECONDS)
        self._poll_timer.timeout.connect(self._poll)

    def _keycodes_for(self, keysym_names) -> set:
        keycodes = set()
        for keysym_name in keysym_names:
            keysym = XK.string_to_keysym(keysym_name)
            if keysym:
                keycode = self._x_display.keysym_to_keycode(keysym)
                if keycode:
                    keycodes.add(keycode)
        return keycodes

    def start(self) -> None:
        self._poll_timer.start()

    def stop(self) -> None:
        self._poll_timer.stop()
        if self._phase == _PointModePhase.ACTIVE:
            self.point_mode_cancelled.emit()
        self._phase = _PointModePhase.IDLE

    def set_enabled(self, is_enabled: bool) -> None:
        """While disabled (e.g. typing in the notch), holding Ctrl+Alt does nothing."""
        self._is_enabled = is_enabled
        if not is_enabled and self._phase == _PointModePhase.ACTIVE:
            self._phase = _PointModePhase.WAITING_FOR_RELEASE
            self.point_mode_cancelled.emit()

    def current_anchor(self) -> PointerAnchor:
        """The pointer right now in physical and logical coordinates (also used to aim synthetic clicks)."""
        return self._current_anchor()

    def _current_anchor(self) -> PointerAnchor:
        pointer_state = self._root_window.query_pointer()
        logical_cursor = QPointF(QCursor.pos())
        cursor_screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        return PointerAnchor(
            physical_x=pointer_state.root_x,
            physical_y=pointer_state.root_y,
            logical_point=logical_cursor,
            device_pixel_ratio=cursor_screen.devicePixelRatio(),
        )

    def _poll(self) -> None:
        keymap_bytes = self._x_display.query_keymap()
        pressed_keycodes = {
            byte_index * 8 + bit_index
            for byte_index, byte_value in enumerate(keymap_bytes)
            if byte_value
            for bit_index in range(8)
            if byte_value & (1 << bit_index)
        }
        is_control_down = bool(pressed_keycodes & self._control_keycodes)
        is_alt_down = bool(pressed_keycodes & self._alt_keycodes)
        is_other_key_down = bool(pressed_keycodes - self._modifier_keycodes)
        is_only_control_alt_down = is_control_down and is_alt_down and not is_other_key_down

        if self._phase == _PointModePhase.IDLE:
            if is_only_control_alt_down and self._is_enabled:
                self._phase = _PointModePhase.ARMING
                self._arming_started_at = time.monotonic()
            elif is_other_key_down and (is_control_down or is_alt_down):
                self._phase = _PointModePhase.WAITING_FOR_RELEASE

        elif self._phase == _PointModePhase.ARMING:
            if is_other_key_down:
                self._phase = _PointModePhase.WAITING_FOR_RELEASE
            elif not (is_control_down and is_alt_down):
                self._phase = _PointModePhase.IDLE
            elif time.monotonic() - self._arming_started_at >= self._arming_delay_seconds:
                self._phase = _PointModePhase.ACTIVE
                self._active_started_at = time.monotonic()
                self._last_anchor = self._current_anchor()
                self._last_physical_position = (self._last_anchor.physical_x, self._last_anchor.physical_y)
                self.point_mode_started.emit(self._last_anchor)

        elif self._phase == _PointModePhase.ACTIVE:
            if is_other_key_down:
                self._phase = _PointModePhase.WAITING_FOR_RELEASE
                self.point_mode_cancelled.emit()
            elif not (is_control_down and is_alt_down):
                self._phase = _PointModePhase.WAITING_FOR_RELEASE if (is_control_down or is_alt_down) else _PointModePhase.IDLE
                if time.monotonic() - self._active_started_at < MINIMUM_ACTIVE_SECONDS:
                    self.point_mode_cancelled.emit()
                else:
                    self.point_mode_released.emit(self._current_anchor())
            else:
                pointer_anchor = self._current_anchor()
                physical_position = (pointer_anchor.physical_x, pointer_anchor.physical_y)
                if physical_position != self._last_physical_position:
                    self._last_physical_position = physical_position
                    self._last_anchor = pointer_anchor
                    self.pointer_moved.emit(pointer_anchor)

        elif self._phase == _PointModePhase.WAITING_FOR_RELEASE:
            if not is_control_down and not is_alt_down:
                self._phase = _PointModePhase.IDLE
