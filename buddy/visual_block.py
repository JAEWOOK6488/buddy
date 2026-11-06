"""Parsing of the <visual title="...">...html...</visual> block the model adds when a
picture explains better than words (3D scenes, structure diagrams, animations)."""

import re
from dataclasses import dataclass
from typing import Optional

VISUAL_OPENING_MARKER = "<visual"
VISUAL_BLOCK_PATTERN = re.compile(
    r"<visual(?:\s+title\s*=\s*\"([^\"]*)\")?\s*>(.*?)</visual\s*>", re.DOTALL | re.IGNORECASE
)
# Models sometimes wrap the page in a markdown code fence inside the block.
CODE_FENCE_PATTERN = re.compile(r"^\s*```[a-zA-Z]*\s*\n(.*?)\n\s*```\s*$", re.DOTALL)

DEFAULT_VISUAL_TITLE = "visual"


@dataclass
class VisualParseResult:
    # Reply text with the block removed (what is shown and spoken).
    remaining_text: str
    html: Optional[str] = None
    title: str = DEFAULT_VISUAL_TITLE
    # Set when a block was started but never closed (e.g. the reply was cut off).
    error: Optional[str] = None


def text_before_visual(streaming_text: str) -> Optional[str]:
    """The spoken part of a reply once the model has started writing its visual, else None."""
    marker_index = streaming_text.lower().find(VISUAL_OPENING_MARKER)
    if marker_index == -1:
        return None
    return streaming_text[:marker_index].rstrip()


def parse_visual_block(response_text: str) -> VisualParseResult:
    block_match = VISUAL_BLOCK_PATTERN.search(response_text)
    if block_match is None:
        unfinished_index = response_text.lower().find(VISUAL_OPENING_MARKER)
        if unfinished_index == -1:
            return VisualParseResult(response_text)
        return VisualParseResult(
            remaining_text=response_text[:unfinished_index].rstrip(),
            error="The visual was cut off before it was finished.",
        )

    html = block_match.group(2).strip()
    fence_match = CODE_FENCE_PATTERN.match(html)
    if fence_match is not None:
        html = fence_match.group(1).strip()
    title = (block_match.group(1) or "").strip()[:80] or DEFAULT_VISUAL_TITLE

    remaining_text = (response_text[: block_match.start()].rstrip() + " " + response_text[block_match.end():].strip()).strip()
    if not html:
        return VisualParseResult(remaining_text, error="The visual was empty.")
    return VisualParseResult(remaining_text, html=html, title=title)
