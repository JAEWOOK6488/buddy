"""System-wide hotkey on X11 via XGrabKey (python-xlib).

Only the configured combination is grabbed, so buddy never sees other keystrokes.
"""

import select
import threading
import time
from typing import Tuple

from PyQt6.QtCore import QObject, pyqtSignal
from Xlib import X, XK
from Xlib import display as xlib_display
from Xlib import error as xlib_error

MODIFIER_NAME_TO_MASK = {
    "ctrl": X.ControlMask,
    "control": X.ControlMask,
    "alt": X.Mod1Mask,
    "shift": X.ShiftMask,
    "super": X.Mod4Mask,
    "win": X.Mod4Mask,
}

# Caps Lock and Num Lock change the modifier state, so the same combination is grabbed
# once per lock combination; otherwise the hotkey dies whenever Num Lock is on.
LOCK_MASK_VARIANTS = (0, X.LockMask, X.Mod2Mask, X.LockMask | X.Mod2Mask)

# Holding the keys auto-repeats KeyPress; ignore presses closer together than this.
REPEAT_SUPPRESSION_SECONDS = 0.35
EVENT_POLL_TIMEOUT_SECONDS = 0.2


def parse_hotkey(hotkey_text: str) -> Tuple[int, str]:
    """'ctrl+alt+space' -> (ControlMask | Mod1Mask, 'space'). The key keeps its case
    because X keysym names are case-sensitive ('Escape', 'F9')."""
    key_names = [key_name.strip() for key_name in hotkey_text.split("+") if key_name.strip()]
    if not key_names:
        raise ValueError("Hotkey is empty.")
    modifier_mask = 0
    for modifier_name in key_names[:-1]:
        if modifier_name.lower() not in MODIFIER_NAME_TO_MASK:
            raise ValueError(f"Unknown modifier '{modifier_name}' in hotkey '{hotkey_text}'.")
        modifier_mask |= MODIFIER_NAME_TO_MASK[modifier_name.lower()]
    return modifier_mask, key_names[-1]


def resolve_keysym(key_name: str) -> int:
    """Tries the name as written, then common spellings ('esc', 'escape' -> 'Escape')."""
    aliases = {"esc": "Escape", "escape": "Escape", "enter": "Return", "return": "Return"}
    for candidate_name in (key_name, aliases.get(key_name.lower(), ""), key_name.lower(), key_name.capitalize()):
        if candidate_name:
            keysym = XK.string_to_keysym(candidate_name)
            if keysym:
                return keysym
    return 0


class GlobalHotkeyListener(QObject):
    """Signals are emitted from the listener thread; Qt queues them onto the GUI thread."""

    hotkey_activated = pyqtSignal()
    hotkey_registration_failed = pyqtSignal(str)

    def __init__(self, hotkey_text: str):
        super().__init__()
        self._hotkey_text = hotkey_text
        self._is_running = False
        self._listener_thread = None

    def start(self) -> None:
        self._is_running = True
        self._listener_thread = threading.Thread(target=self._listen, daemon=True)
        self._listener_thread.start()

    def stop(self) -> None:
        self._is_running = False

    def _listen(self) -> None:
        try:
            modifier_mask, key_name = parse_hotkey(self._hotkey_text)
        except ValueError as parse_error:
            self.hotkey_registration_failed.emit(str(parse_error))
            return

        # A dedicated connection: python-xlib displays must not be shared across threads.
        x_display = xlib_display.Display()
        root_window = x_display.screen().root

        keysym = resolve_keysym(key_name)
        keycode = x_display.keysym_to_keycode(keysym) if keysym else 0
        if keycode == 0:
            self.hotkey_registration_failed.emit(f"Unknown key '{key_name}' in hotkey '{self._hotkey_text}'.")
            x_display.close()
            return

        grab_error_catcher = xlib_error.CatchError(xlib_error.BadAccess)
        for lock_mask in LOCK_MASK_VARIANTS:
            root_window.grab_key(
                keycode,
                modifier_mask | lock_mask,
                True,
                X.GrabModeAsync,
                X.GrabModeAsync,
                onerror=grab_error_catcher,
            )
        x_display.sync()
        if grab_error_catcher.get_error() is not None:
            self.hotkey_registration_failed.emit(
                f"{self._hotkey_text} is already taken by another app. "
                "Pick a different combination in .env."
            )
            x_display.close()
            return

        last_activation_time = 0.0
        while self._is_running:
            readable, _, _ = select.select([x_display.fileno()], [], [], EVENT_POLL_TIMEOUT_SECONDS)
            if not readable:
                continue
            while x_display.pending_events():
                x_event = x_display.next_event()
                if x_event.type != X.KeyPress or x_event.detail != keycode:
                    continue
                now = time.monotonic()
                if now - last_activation_time < REPEAT_SUPPRESSION_SECONDS:
                    last_activation_time = now
                    continue
                last_activation_time = now
                self.hotkey_activated.emit()

        for lock_mask in LOCK_MASK_VARIANTS:
            root_window.ungrab_key(keycode, modifier_mask | lock_mask)
        x_display.sync()
        x_display.close()
