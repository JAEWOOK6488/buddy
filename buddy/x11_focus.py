"""Gives keyboard focus to buddy's own override-redirect windows.

The notch bypasses the window manager so it can sit over the GNOME top bar. Such
windows never receive focus from the window manager, and Qt's own activation request
carries a stale X timestamp (the hotkey arrives through a separate X connection), so
the X server would ignore it. XSetInputFocus with CurrentTime always takes effect, and
Qt then sees a normal FocusIn, which also wakes the input method (ibus/kime) for Korean.
"""

from Xlib import X
from Xlib import display as xlib_display

_focus_display = None


def force_keyboard_focus(native_window_id: int) -> None:
    global _focus_display
    if _focus_display is None:
        _focus_display = xlib_display.Display()
    target_window = _focus_display.create_resource_object("window", native_window_id)
    target_window.set_input_focus(X.RevertToPointerRoot, X.CurrentTime)
    _focus_display.sync()
