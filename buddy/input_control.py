"""Synthesizes mouse and keyboard input on X11 through the XTEST extension.

Text is pasted from the clipboard instead of typed key by key: XTEST can only press
keys of the current layout, so Korean (entered through an input method) and most
Unicode could not be typed otherwise. Terminals paste with Ctrl+Shift+V.
"""

import time
from typing import List, Optional

from Xlib import X, Xatom
from Xlib import display as xlib_display
from Xlib.ext import xtest

from .hotkey import MODIFIER_NAME_TO_MASK, resolve_keysym

MODIFIER_KEYSYM_NAMES = {
    "ctrl": "Control_L",
    "control": "Control_L",
    "alt": "Alt_L",
    "shift": "Shift_L",
    "super": "Super_L",
    "win": "Super_L",
}

# X button numbers.
LEFT_BUTTON = 1
MIDDLE_BUTTON = 2
RIGHT_BUTTON = 3
SCROLL_UP_BUTTON = 4
SCROLL_DOWN_BUTTON = 5
SCROLL_LEFT_BUTTON = 6
SCROLL_RIGHT_BUTTON = 7

# Small pauses make apps register synthetic input like a real hand.
EVENT_GAP_SECONDS = 0.02
DOUBLE_CLICK_GAP_SECONDS = 0.08
DRAG_STEPS = 20

TERMINAL_WINDOW_CLASS_HINTS = (
    "terminal", "term", "konsole", "kitty", "alacritty", "tilix", "terminator", "wezterm", "wave", "xterm",
)


class InputControlError(Exception):
    pass


class InputController:
    def __init__(self):
        self._x_display = xlib_display.Display()
        if not self._x_display.has_extension("XTEST"):
            raise InputControlError("The X server has no XTEST extension; mouse/keyboard control is unavailable.")
        self._root_window = self._x_display.screen().root

    # ----- mouse -----

    def move_to(self, physical_x: int, physical_y: int) -> None:
        xtest.fake_input(self._x_display, X.MotionNotify, x=int(physical_x), y=int(physical_y))
        self._x_display.sync()
        time.sleep(EVENT_GAP_SECONDS)

    def click(self, physical_x: int, physical_y: int, button: int = LEFT_BUTTON, click_count: int = 1) -> None:
        self.move_to(physical_x, physical_y)
        for click_index in range(click_count):
            xtest.fake_input(self._x_display, X.ButtonPress, button)
            self._x_display.sync()
            time.sleep(EVENT_GAP_SECONDS)
            xtest.fake_input(self._x_display, X.ButtonRelease, button)
            self._x_display.sync()
            if click_index + 1 < click_count:
                time.sleep(DOUBLE_CLICK_GAP_SECONDS)

    def drag(self, start_x: int, start_y: int, end_x: int, end_y: int) -> None:
        self.move_to(start_x, start_y)
        xtest.fake_input(self._x_display, X.ButtonPress, LEFT_BUTTON)
        self._x_display.sync()
        for step_index in range(1, DRAG_STEPS + 1):
            fraction = step_index / DRAG_STEPS
            self.move_to(start_x + (end_x - start_x) * fraction, start_y + (end_y - start_y) * fraction)
        xtest.fake_input(self._x_display, X.ButtonRelease, LEFT_BUTTON)
        self._x_display.sync()

    def scroll(self, physical_x: int, physical_y: int, direction: str, amount: int) -> None:
        button_by_direction = {
            "up": SCROLL_UP_BUTTON,
            "down": SCROLL_DOWN_BUTTON,
            "left": SCROLL_LEFT_BUTTON,
            "right": SCROLL_RIGHT_BUTTON,
        }
        if direction not in button_by_direction:
            raise InputControlError(f"Unknown scroll direction '{direction}'.")
        self.move_to(physical_x, physical_y)
        for _ in range(max(1, min(int(amount), 30))):
            xtest.fake_input(self._x_display, X.ButtonPress, button_by_direction[direction])
            xtest.fake_input(self._x_display, X.ButtonRelease, button_by_direction[direction])
            self._x_display.sync()
            time.sleep(EVENT_GAP_SECONDS)

    # ----- keyboard -----

    def _keycode_for(self, key_name: str) -> int:
        keysym_name = MODIFIER_KEYSYM_NAMES.get(key_name.lower(), key_name)
        keysym = resolve_keysym(keysym_name)
        keycode = self._x_display.keysym_to_keycode(keysym) if keysym else 0
        if not keycode:
            raise InputControlError(f"Unknown key '{key_name}'.")
        return keycode

    def press_key_combination(self, key_combination: str) -> None:
        """'ctrl+s', 'Return', 'alt+Tab', 'ctrl+shift+v'. Modifiers go down first, up last."""
        key_names = [key_name.strip() for key_name in key_combination.split("+") if key_name.strip()]
        if not key_names:
            raise InputControlError("Empty key combination.")
        modifier_names = [key_name for key_name in key_names[:-1] if key_name.lower() in MODIFIER_NAME_TO_MASK]
        if len(modifier_names) != len(key_names) - 1:
            raise InputControlError(f"Only ctrl/alt/shift/super can be combined: '{key_combination}'.")
        keycodes: List[int] = [self._keycode_for(key_name) for key_name in key_names]

        for keycode in keycodes:
            xtest.fake_input(self._x_display, X.KeyPress, keycode)
            self._x_display.sync()
            time.sleep(EVENT_GAP_SECONDS)
        for keycode in reversed(keycodes):
            xtest.fake_input(self._x_display, X.KeyRelease, keycode)
            self._x_display.sync()
            time.sleep(EVENT_GAP_SECONDS)

    def paste_shortcut_for_window(self, window_id: Optional[int]) -> str:
        return "ctrl+shift+v" if self.is_terminal_window(window_id) else "ctrl+v"

    # ----- windows -----

    def active_window_id(self) -> Optional[int]:
        active_window_property = self._root_window.get_full_property(
            self._x_display.intern_atom("_NET_ACTIVE_WINDOW"), Xatom.WINDOW
        )
        if active_window_property is None or not active_window_property.value:
            return None
        window_id = int(active_window_property.value[0])
        return window_id or None

    def is_terminal_window(self, window_id: Optional[int]) -> bool:
        if not window_id:
            return False
        try:
            window_class = self._x_display.create_resource_object("window", window_id).get_wm_class() or ()
        except Exception:  # noqa: BLE001 - the window may be gone
            return False
        class_text = " ".join(window_class).lower()
        return any(hint in class_text for hint in TERMINAL_WINDOW_CLASS_HINTS)

    def activate_window(self, window_id: Optional[int]) -> None:
        """Gives a normal app window the keyboard back (buddy's notch took it for the approval)."""
        if not window_id:
            return
        from Xlib.protocol import event as xlib_event

        target_window = self._x_display.create_resource_object("window", window_id)
        # Source indication 2 = "pager", which window managers honor without focus-stealing checks.
        activate_event = xlib_event.ClientMessage(
            window=target_window,
            client_type=self._x_display.intern_atom("_NET_ACTIVE_WINDOW"),
            data=(32, [2, X.CurrentTime, 0, 0, 0]),
        )
        self._root_window.send_event(
            activate_event, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask
        )
        try:
            target_window.set_input_focus(X.RevertToPointerRoot, X.CurrentTime)
        except Exception:  # noqa: BLE001 - not viewable; the WM request above still applies
            pass
        self._x_display.sync()
