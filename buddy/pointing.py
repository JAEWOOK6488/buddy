"""Parsing of the [POINT:x,y:label:screenN] tag and mapping it onto the desktop."""

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from PyQt6.QtCore import QPointF, QRectF

from .screen_capture import ScreenCapture

# Matches [POINT:none], [POINT:123,456:label] or [POINT:123,456:label:screen2] at the end
# of the reply. Same grammar as the original Clicky parser.
POINT_TAG_PATTERN = re.compile(
    r"\[POINT:(?:none|(\d+)\s*,\s*(\d+)(?::([^\]:\s][^\]:]*?))?(?::screen(\d+))?)\]\s*$"
)

# [BOX:x1,y1,x2,y2:label] — in point mode, the tight bounds of the element the user pointed at.
BOX_TAG_PATTERN = re.compile(
    r"\[BOX:(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?::([^\]]*?))?\]\s*$"
)

# While streaming, anything from these markers on is hidden so a half-written tag never shows.
PARTIAL_TAG_MARKERS = ("[POINT", "[BOX", "<action", "<visual")


@dataclass
class BoxParseResult:
    remaining_text: str
    # (x1, y1, x2, y2) in screenshot pixels of the cursor screen, or None.
    box: Optional[Tuple[int, int, int, int]]


def parse_box_tag(response_text: str) -> BoxParseResult:
    box_match = BOX_TAG_PATTERN.search(response_text)
    if box_match is None:
        return BoxParseResult(response_text, None)
    x1, y1, x2, y2 = (int(value) for value in box_match.groups()[:4])
    return BoxParseResult(
        remaining_text=response_text[: box_match.start()].rstrip(),
        box=(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)),
    )


def map_screenshot_box_to_global(capture: ScreenCapture, box: Tuple[int, int, int, int]) -> QRectF:
    top_left = map_screenshot_point_to_global(capture, box[0], box[1])
    bottom_right = map_screenshot_point_to_global(capture, box[2], box[3])
    return QRectF(top_left, bottom_right)


@dataclass
class PointingParseResult:
    # Reply text with the tag removed; this is what gets displayed and spoken.
    spoken_text: str
    screenshot_x: Optional[int]
    screenshot_y: Optional[int]
    element_label: Optional[str]
    screen_number: Optional[int]

    @property
    def has_coordinate(self) -> bool:
        return self.screenshot_x is not None and self.screenshot_y is not None


def strip_partial_point_tag(streaming_text: str) -> str:
    marker_indices = [streaming_text.find(marker) for marker in PARTIAL_TAG_MARKERS]
    found_indices = [marker_index for marker_index in marker_indices if marker_index != -1]
    if found_indices:
        return streaming_text[: min(found_indices)].rstrip()
    # The stream may stop mid-marker ("... [PO"); hide that fragment too.
    for marker in PARTIAL_TAG_MARKERS:
        for fragment_length in range(len(marker) - 1, 0, -1):
            if streaming_text.endswith(marker[:fragment_length]):
                return streaming_text[:-fragment_length].rstrip()
    return streaming_text


def parse_pointing_tag(response_text: str) -> PointingParseResult:
    tag_match = POINT_TAG_PATTERN.search(response_text)
    if tag_match is None:
        return PointingParseResult(
            spoken_text=strip_partial_point_tag(response_text).strip(),
            screenshot_x=None,
            screenshot_y=None,
            element_label=None,
            screen_number=None,
        )

    spoken_text = response_text[: tag_match.start()].strip()
    x_text, y_text, label_text, screen_text = tag_match.groups()
    if x_text is None or y_text is None:
        # [POINT:none]
        return PointingParseResult(spoken_text, None, None, None, None)

    return PointingParseResult(
        spoken_text=spoken_text,
        screenshot_x=int(x_text),
        screenshot_y=int(y_text),
        element_label=label_text.strip() if label_text else None,
        screen_number=int(screen_text) if screen_text else None,
    )


def select_target_capture(
    parse_result: PointingParseResult, screen_captures: List[ScreenCapture]
) -> Optional[ScreenCapture]:
    """Picks the screenshot the coordinate refers to; without :screenN it is the cursor screen."""
    if not screen_captures:
        return None
    if parse_result.screen_number is not None and 1 <= parse_result.screen_number <= len(screen_captures):
        return screen_captures[parse_result.screen_number - 1]
    for capture in screen_captures:
        if capture.is_cursor_screen:
            return capture
    return screen_captures[0]


def map_screenshot_point_to_global(capture: ScreenCapture, screenshot_x: int, screenshot_y: int) -> QPointF:
    """Converts a pixel in the (downscaled) screenshot into Qt global logical coordinates."""
    clamped_x = max(0, min(screenshot_x, capture.image_width))
    clamped_y = max(0, min(screenshot_y, capture.image_height))

    screen_geometry = capture.screen_geometry
    scale_x = screen_geometry.width() / capture.image_width
    scale_y = screen_geometry.height() / capture.image_height

    return QPointF(
        screen_geometry.x() + clamped_x * scale_x,
        screen_geometry.y() + clamped_y * scale_y,
    )
