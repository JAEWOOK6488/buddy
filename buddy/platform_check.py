"""Display-server checks. buddy officially supports only X11 sessions."""

import os

WAYLAND_NOT_SUPPORTED_MESSAGE = (
    "buddy needs an X11 session.\n"
    "Wayland blocks global hotkeys, silent screenshots, reading the cursor position\n"
    "and placing overlay windows at exact coordinates.\n\n"
    "Log out, click the gear icon on the login screen, choose 'Ubuntu on Xorg',\n"
    "then log back in and start buddy again."
)

NO_DISPLAY_MESSAGE = (
    "No X11 display found (DISPLAY is not set).\n"
    "Start buddy from a terminal inside your desktop session."
)


def find_display_session_problem() -> "str | None":
    """Returns a user-facing message if the current session cannot run buddy, else None."""
    session_type = os.environ.get("XDG_SESSION_TYPE", "").strip().lower()
    if session_type == "wayland":
        return WAYLAND_NOT_SUPPORTED_MESSAGE
    if os.environ.get("WAYLAND_DISPLAY") and session_type != "x11":
        return WAYLAND_NOT_SUPPORTED_MESSAGE
    if not os.environ.get("DISPLAY"):
        return NO_DISPLAY_MESSAGE
    return None
