"""Builds the user-turn content (screenshots + question) shared by both Claude backends."""

import base64
from typing import List, Optional

from .pointer_inspector import PointedElement
from .screen_capture import ScreenCapture


def build_agent_step_question(task_goal: str, completed_step_descriptions: List[str], step_number: int, last_step_error: Optional[str]) -> str:
    """Sent after each approved step, together with a fresh screenshot."""
    question_lines = [
        f"[step {step_number}] you're doing this task for me: {task_goal}",
        "steps done so far:",
    ]
    question_lines.extend(
        f"{index + 1}. {description}" for index, description in enumerate(completed_step_descriptions)
    )
    if last_step_error:
        question_lines.append(f"the last step failed to run: {last_step_error}")
    question_lines.append(
        "this is the screen now. check the last step worked, then propose the next single <action>, "
        "or finish with a done action."
    )
    return "\n".join(question_lines)


def build_point_mode_question(
    cursor_capture: ScreenCapture,
    pointed_element: Optional[PointedElement],
    reply_language: str,
    has_meaningful_trail: bool,
) -> str:
    """The text sent in place of a typed question when the user points with Ctrl+Alt."""
    question_lines = ["[point mode] i'm holding ctrl+alt and pointing at something on my screen. what is this?"]
    if has_meaningful_trail:
        question_lines.append(
            "the yellow line is the path my mouse traced while pointing. if it circles, underlines or "
            "connects things, i mean that whole area or those things, not only the spot under the red ring."
        )
    if cursor_capture.pointer_in_image is not None:
        pointer = cursor_capture.pointer_in_image
        question_lines.append(
            f"mouse position in the screen 1 image: ({int(pointer.x())}, {int(pointer.y())}) — the red ring."
        )
    if cursor_capture.element_in_image is not None:
        element_box = cursor_capture.element_in_image
        box_text = (
            f"({int(element_box.left())}, {int(element_box.top())}) to "
            f"({int(element_box.right())}, {int(element_box.bottom())})"
        )
        if pointed_element is not None and pointed_element.source == "accessibility":
            question_lines.append(
                f"orange box {box_text}: from accessibility data, {pointed_element.describe()}. usually tight."
            )
        else:
            question_lines.append(
                f"orange box {box_text}: the whole window under the mouse, so the real element is smaller."
            )
    else:
        question_lines.append("no element was detected under the mouse; use the red ring.")
    if reply_language == "ko":
        question_lines.append("reply in korean.")
    return "\n".join(question_lines)


def build_screenshot_content_blocks(screen_captures: List[ScreenCapture], user_question: str) -> list:
    """Label + image for every screen, then the question.

    The label carries the pixel size because the system prompt tells the model to
    answer [POINT:x,y] in exactly that coordinate space.
    """
    content_blocks = []
    for capture in screen_captures:
        content_blocks.append(
            {
                "type": "text",
                "text": f"{capture.label} (image dimensions: {capture.image_width}x{capture.image_height} pixels)",
            }
        )
        content_blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.standard_b64encode(capture.jpeg_bytes).decode("ascii"),
                },
            }
        )
    content_blocks.append({"type": "text", "text": user_question})
    return content_blocks
