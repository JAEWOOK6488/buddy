"""Parsing and validation of the <action>{...}</action> tag the model uses to propose
one mouse/keyboard step at a time."""

import json
import re
from dataclasses import dataclass, field
from typing import Optional

ACTION_TAG_PATTERN = re.compile(r"<action>\s*(\{.*?\})\s*</action>\s*$", re.DOTALL)

ACTION_TYPES_WITH_POSITION = {"click", "double_click", "right_click", "scroll", "drag"}
ALL_ACTION_TYPES = ACTION_TYPES_WITH_POSITION | {"type", "key", "wait", "done"}

MAXIMUM_WAIT_SECONDS = 10.0
MAXIMUM_TYPED_CHARACTERS = 2000


@dataclass
class ProposedAction:
    action_type: str
    # Screenshot pixel coordinates on screen `screen_number` (1 = cursor screen).
    x: Optional[int] = None
    y: Optional[int] = None
    to_x: Optional[int] = None
    to_y: Optional[int] = None
    screen_number: int = 1
    label: str = ""
    text: str = ""
    keys: str = ""
    direction: str = ""
    amount: int = 3
    seconds: float = 1.0
    summary: str = ""
    raw: dict = field(default_factory=dict)

    def describe(self) -> str:
        """Short line for the approval prompt and the step log."""
        target = f" '{self.label}'" if self.label else (f" at ({self.x}, {self.y})" if self.x is not None else "")
        if self.action_type == "click":
            return f"click{target}"
        if self.action_type == "double_click":
            return f"double-click{target}"
        if self.action_type == "right_click":
            return f"right-click{target}"
        if self.action_type == "drag":
            return f"drag{target} to ({self.to_x}, {self.to_y})"
        if self.action_type == "scroll":
            return f"scroll {self.direction} ×{self.amount}{target}"
        if self.action_type == "type":
            visible_text = self.text.rstrip("\n").replace("\n", " ⏎ ")
            preview = visible_text if len(visible_text) <= 80 else visible_text[:77] + "…"
            return f"type “{preview}”" + (" + Enter" if self.text.endswith("\n") else "")
        if self.action_type == "key":
            return f"press {self.keys}"
        if self.action_type == "wait":
            return f"wait {self.seconds:g}s"
        return f"done: {self.summary}" if self.summary else "done"


@dataclass
class ActionParseResult:
    remaining_text: str
    action: Optional[ProposedAction]
    # Set when a tag was present but invalid; shown to the user instead of acting.
    error: Optional[str] = None


def _read_int(action_fields: dict, field_name: str) -> Optional[int]:
    value = action_fields.get(field_name)
    if value is None:
        return None
    return int(round(float(value)))


def parse_action_tag(response_text: str) -> ActionParseResult:
    tag_match = ACTION_TAG_PATTERN.search(response_text)
    if tag_match is None:
        return ActionParseResult(response_text, None)
    remaining_text = response_text[: tag_match.start()].rstrip()

    try:
        action_fields = json.loads(tag_match.group(1))
        action_type = str(action_fields.get("type", "")).strip().lower()
        if action_type not in ALL_ACTION_TYPES:
            raise ValueError(f"unknown action type '{action_type}'")

        proposed_action = ProposedAction(
            action_type=action_type,
            x=_read_int(action_fields, "x"),
            y=_read_int(action_fields, "y"),
            to_x=_read_int(action_fields, "to_x"),
            to_y=_read_int(action_fields, "to_y"),
            screen_number=_read_int(action_fields, "screen") or 1,
            label=str(action_fields.get("label", ""))[:60],
            text=str(action_fields.get("text", "")),
            keys=str(action_fields.get("keys", "")).strip(),
            direction=str(action_fields.get("direction", "")).strip().lower(),
            amount=_read_int(action_fields, "amount") or 3,
            seconds=min(float(action_fields.get("seconds", 1.0)), MAXIMUM_WAIT_SECONDS),
            summary=str(action_fields.get("summary", "")),
            raw=action_fields,
        )

        if action_type in ACTION_TYPES_WITH_POSITION and (proposed_action.x is None or proposed_action.y is None):
            raise ValueError(f"'{action_type}' needs x and y")
        if action_type == "drag" and (proposed_action.to_x is None or proposed_action.to_y is None):
            raise ValueError("'drag' needs to_x and to_y")
        if action_type == "type" and not proposed_action.text:
            raise ValueError("'type' needs text")
        if action_type == "type" and len(proposed_action.text) > MAXIMUM_TYPED_CHARACTERS:
            raise ValueError("text is too long to type")
        if action_type == "key" and not proposed_action.keys:
            raise ValueError("'key' needs keys")
        if action_type == "scroll" and proposed_action.direction not in ("up", "down", "left", "right"):
            raise ValueError("'scroll' needs direction up/down/left/right")
    except (ValueError, TypeError, json.JSONDecodeError) as parse_error:
        return ActionParseResult(remaining_text, None, f"Could not use the proposed action: {parse_error}")

    return ActionParseResult(remaining_text, proposed_action)
