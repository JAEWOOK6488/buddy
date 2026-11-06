"""Finds the UI element under the mouse pointer while point mode is active.

Two sources, best first:
1. AT-SPI accessibility (GTK, Qt, and Chromium/Electron apps that expose their tree):
   the exact rectangle of the button/menu item/field under the pointer, with role and name.
2. The top-most X11 client window under the pointer: always available, but coarse.

Claude later refines the rectangle from the screenshot with a [BOX:...] tag, so the
fallback only has to be a reasonable first guess.

All AT-SPI and X11 calls run on one worker thread: AT-SPI calls are blocking D-Bus
round trips and a busy app can stall them, which must never freeze the GUI.
"""

import threading
from dataclasses import dataclass
from typing import List, Optional, Tuple

from PyQt6.QtCore import QObject, QPointF, pyqtSignal
from Xlib import X, Xatom
from Xlib import display as xlib_display
from Xlib import error as xlib_error

try:
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    IS_ACCESSIBILITY_AVAILABLE = True
except (ImportError, ValueError):
    IS_ACCESSIBILITY_AVAILABLE = False

# A blocking accessibility call to a hung app gives up after this many milliseconds.
ACCESSIBILITY_CALL_TIMEOUT_MILLISECONDS = 400
MAXIMUM_TREE_DEPTH = 40
# Elements smaller than this are hit-test noise (separators, 1px borders).
MINIMUM_ELEMENT_SIDE = 6
# An element covering this much of its window is a container, not "the thing pointed at".
CONTAINER_AREA_RATIO = 0.6

# A text/icon inside one of these is described by its parent instead (the label of a button).
LEAF_ROLE_NAMES = {"label", "text", "static", "icon", "image", "paragraph", "section"}
INTERACTIVE_ROLE_NAMES = {
    "push button", "toggle button", "check box", "radio button", "menu item", "check menu item",
    "radio menu item", "menu", "link", "list item", "page tab", "combo box", "entry",
    "password text", "spin button", "slider", "tree item", "table cell", "tool bar", "button",
}


@dataclass
class PointerAnchor:
    """The pointer in two coordinate systems at the same instant.

    X11 and AT-SPI report physical pixels; Qt draws in logical pixels. Mapping
    relative to the pointer stays correct whatever the screen scale or origin is.
    """

    physical_x: int
    physical_y: int
    logical_point: QPointF
    device_pixel_ratio: float

    def physical_to_logical(self, physical_x: float, physical_y: float) -> QPointF:
        return QPointF(
            self.logical_point.x() + (physical_x - self.physical_x) / self.device_pixel_ratio,
            self.logical_point.y() + (physical_y - self.physical_y) / self.device_pixel_ratio,
        )

    def logical_to_physical(self, logical_point: QPointF) -> tuple:
        return (
            round(self.physical_x + (logical_point.x() - self.logical_point.x()) * self.device_pixel_ratio),
            round(self.physical_y + (logical_point.y() - self.logical_point.y()) * self.device_pixel_ratio),
        )


@dataclass
class PointedElement:
    # Rectangle in physical X11 root coordinates: (x, y, width, height).
    physical_rectangle: Tuple[int, int, int, int]
    # "accessibility" (tight bounds) or "window" (whole window, coarse).
    source: str
    role_name: str
    element_name: str
    application_name: str
    window_title: str

    def describe(self) -> str:
        """One-line summary for the notch and for the model."""
        if self.source == "accessibility":
            parts = []
            if self.element_name:
                parts.append(f"'{self.element_name}'")
            if self.role_name:
                parts.append(f"({self.role_name})")
            if self.application_name:
                parts.append(f"in {self.application_name}")
            return " ".join(parts) or "an element"
        return f"the window '{self.window_title}'" if self.window_title else "a window"


class PointerInspector(QObject):
    # (anchor the request was made with, element or None)
    element_found = pyqtSignal(object, object)

    def __init__(self):
        super().__init__()
        self._condition = threading.Condition()
        self._requested_anchor: Optional[PointerAnchor] = None
        self._is_running = True
        self._worker_thread = threading.Thread(target=self._work, daemon=True)
        self._worker_thread.start()

    @staticmethod
    def accessibility_available() -> bool:
        return IS_ACCESSIBILITY_AVAILABLE

    def request_inspection(self, pointer_anchor: PointerAnchor) -> None:
        """Only the most recent request matters; older pending ones are dropped."""
        with self._condition:
            self._requested_anchor = pointer_anchor
            self._condition.notify()

    def shutdown(self) -> None:
        with self._condition:
            self._is_running = False
            self._condition.notify()

    # ----- worker thread -----

    def _work(self) -> None:
        x_display = xlib_display.Display()
        if IS_ACCESSIBILITY_AVAILABLE:
            try:
                Atspi.init()
                Atspi.set_timeout(ACCESSIBILITY_CALL_TIMEOUT_MILLISECONDS, 2000)
            except Exception:  # noqa: BLE001 - no accessibility bus is not fatal
                pass

        while True:
            with self._condition:
                while self._is_running and self._requested_anchor is None:
                    self._condition.wait()
                if not self._is_running:
                    break
                pointer_anchor = self._requested_anchor
                self._requested_anchor = None

            try:
                pointed_element = self._inspect(x_display, pointer_anchor.physical_x, pointer_anchor.physical_y)
            except Exception:  # noqa: BLE001 - a misbehaving app must not kill the worker
                pointed_element = None
            self.element_found.emit(pointer_anchor, pointed_element)

        x_display.close()

    def _inspect(self, x_display, pointer_x: int, pointer_y: int) -> Optional[PointedElement]:
        window_under_pointer = _find_top_window_at(x_display, pointer_x, pointer_y)
        if window_under_pointer is None:
            return None
        window_rectangle, window_process_id, window_title = window_under_pointer

        if IS_ACCESSIBILITY_AVAILABLE and window_process_id:
            accessible_element = _find_accessible_at(window_process_id, pointer_x, pointer_y, window_rectangle)
            if accessible_element is not None:
                return accessible_element

        return PointedElement(
            physical_rectangle=window_rectangle,
            source="window",
            role_name="window",
            element_name="",
            application_name="",
            window_title=window_title,
        )


def _read_window_property(x_display, x_window, property_name: str, property_type):
    property_atom = x_display.intern_atom(property_name)
    property_value = x_window.get_full_property(property_atom, property_type)
    return property_value.value if property_value is not None else None


def _find_top_window_at(x_display, pointer_x: int, pointer_y: int):
    """Top-most normal client window containing the point: (rect, pid, title) or None."""
    root_window = x_display.screen().root
    stacked_window_ids = _read_window_property(x_display, root_window, "_NET_CLIENT_LIST_STACKING", Xatom.WINDOW)
    if not stacked_window_ids:
        return None

    desktop_type_atom = x_display.intern_atom("_NET_WM_WINDOW_TYPE_DESKTOP")
    hidden_state_atom = x_display.intern_atom("_NET_WM_STATE_HIDDEN")

    # _NET_CLIENT_LIST_STACKING is bottom-to-top, so walk it backwards.
    for window_id in reversed(list(stacked_window_ids)):
        client_window = x_display.create_resource_object("window", window_id)
        try:
            if client_window.get_attributes().map_state != X.IsViewable:
                continue
            window_types = _read_window_property(x_display, client_window, "_NET_WM_WINDOW_TYPE", Xatom.ATOM) or []
            if desktop_type_atom in window_types:
                return None
            window_states = _read_window_property(x_display, client_window, "_NET_WM_STATE", Xatom.ATOM) or []
            if hidden_state_atom in window_states:
                continue
            window_geometry = client_window.get_geometry()
            root_position = client_window.translate_coords(root_window, 0, 0)
        except xlib_error.XError:
            continue

        # translate_coords(root, 0, 0) gives root's origin in window space; negate for the window's origin.
        window_x = -root_position.x
        window_y = -root_position.y
        if not (window_x <= pointer_x < window_x + window_geometry.width
                and window_y <= pointer_y < window_y + window_geometry.height):
            continue

        process_ids = _read_window_property(x_display, client_window, "_NET_WM_PID", Xatom.CARDINAL)
        window_title = _read_window_property(
            x_display, client_window, "_NET_WM_NAME", x_display.intern_atom("UTF8_STRING")
        )
        if isinstance(window_title, bytes):
            window_title = window_title.decode("utf-8", errors="replace")
        return (
            (window_x, window_y, window_geometry.width, window_geometry.height),
            int(process_ids[0]) if process_ids else 0,
            window_title or "",
        )
    return None


def _extents_of(accessible) -> Optional[Tuple[int, int, int, int]]:
    component = accessible.get_component_iface()
    if component is None:
        return None
    extents = component.get_extents(Atspi.CoordType.SCREEN)
    return (extents.x, extents.y, extents.width, extents.height)


def _contains(rectangle: Tuple[int, int, int, int], point_x: int, point_y: int) -> bool:
    rect_x, rect_y, rect_width, rect_height = rectangle
    return rect_x <= point_x < rect_x + rect_width and rect_y <= point_y < rect_y + rect_height


def _find_accessible_at(
    process_id: int, pointer_x: int, pointer_y: int, window_rectangle: Tuple[int, int, int, int]
) -> Optional[PointedElement]:
    desktop = Atspi.get_desktop(0)
    application = None
    for application_index in range(desktop.get_child_count()):
        candidate_application = desktop.get_child_at_index(application_index)
        if candidate_application is not None and candidate_application.get_process_id() == process_id:
            application = candidate_application
            break
    if application is None:
        return None

    # The app's top-level window that is showing and contains the point; prefer the active one.
    top_level_window = None
    for window_index in range(application.get_child_count()):
        candidate_window = application.get_child_at_index(window_index)
        if candidate_window is None:
            continue
        window_extents = _extents_of(candidate_window)
        if window_extents is None or not _contains(window_extents, pointer_x, pointer_y):
            continue
        window_state = candidate_window.get_state_set()
        if not window_state.contains(Atspi.StateType.SHOWING):
            continue
        top_level_window = candidate_window
        if window_state.contains(Atspi.StateType.ACTIVE):
            break
    if top_level_window is None:
        return None

    # Walk down to the deepest element under the point.
    ancestry: List = [top_level_window]
    current_element = top_level_window
    for _ in range(MAXIMUM_TREE_DEPTH):
        component = current_element.get_component_iface()
        if component is None:
            break
        child_element = component.get_accessible_at_point(pointer_x, pointer_y, Atspi.CoordType.SCREEN)
        if child_element is None or child_element == current_element:
            break
        current_element = child_element
        ancestry.append(current_element)

    # Use the innermost element with sane bounds; describe a button rather than its label.
    window_area = max(1, window_rectangle[2] * window_rectangle[3])
    for depth in range(len(ancestry) - 1, 0, -1):
        element = ancestry[depth]
        element_extents = _extents_of(element)
        if element_extents is None or not _contains(element_extents, pointer_x, pointer_y):
            continue
        if element_extents[2] < MINIMUM_ELEMENT_SIDE or element_extents[3] < MINIMUM_ELEMENT_SIDE:
            continue
        if element_extents[2] * element_extents[3] >= window_area * CONTAINER_AREA_RATIO:
            return None

        role_name = (element.get_role_name() or "").lower()
        parent_element = ancestry[depth - 1]
        parent_role_name = (parent_element.get_role_name() or "").lower()
        if role_name in LEAF_ROLE_NAMES and parent_role_name in INTERACTIVE_ROLE_NAMES:
            parent_extents = _extents_of(parent_element)
            if parent_extents is not None and parent_extents[2] * parent_extents[3] < window_area * CONTAINER_AREA_RATIO:
                element, element_extents, role_name = parent_element, parent_extents, parent_role_name

        element_name = element.get_name() or ""
        if not element_name and role_name in LEAF_ROLE_NAMES:
            text_interface = element.get_text_iface()
            if text_interface is not None:
                element_name = text_interface.get_text(0, min(80, text_interface.get_character_count()))

        return PointedElement(
            physical_rectangle=element_extents,
            source="accessibility",
            role_name=role_name,
            element_name=element_name.strip()[:80],
            application_name=application.get_name() or "",
            window_title="",
        )
    return None
