"""Wires the notch, overlay, Claude, speech, hotkey, point mode and tray together.

Two ways to ask, one pipeline:
- typed: question in the notch
- point mode: hold Ctrl+Alt, point (and optionally trace a path), release

Then: hide buddy's windows -> screenshot every screen (point mode marks the pointer,
element and trail on it) -> stream Claude's reply into the notch -> parse tags
([POINT] flies the buddy, [BOX] tightens the point-mode highlight) -> speak the reply.

Tasks: when the reply ends with <action>{...}</action>, buddy shows the step, waits for
Enter, performs it with synthetic mouse/keyboard input, takes a fresh screenshot and
asks for the next step, until a "done" action, Esc, the stop hotkey or the step limit.
"""

import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import List, Optional

from PyQt6.QtCore import QObject, QRectF, QTimer
from PyQt6.QtGui import QCursor, QGuiApplication
from PyQt6.QtWidgets import QApplication

from .actions import ProposedAction, parse_action_tag
from .claude_code_client import ClaudeCodeResponder
from .config import AVAILABLE_MODELS, BACKEND_API, BuddyConfig, detect_locale_language
from .hotkey import GlobalHotkeyListener
from .input_control import (
    LEFT_BUTTON,
    RIGHT_BUTTON,
    InputControlError,
    InputController,
)
from .message_content import build_agent_step_question, build_point_mode_question
from .notch import NotchWindow
from .overlay import BuddyOverlay, BuddyVoiceState
from .point_mode import PointModeDetector
from .pointer_inspector import PointedElement, PointerAnchor, PointerInspector
from .pointing import (
    map_screenshot_box_to_global,
    map_screenshot_point_to_global,
    parse_box_tag,
    parse_pointing_tag,
    select_target_capture,
    strip_partial_point_tag,
)
from .prompts import COMPANION_SYSTEM_PROMPT
from .screen_capture import PointerAnnotation, ScreenCapture, capture_all_screens
from .speech import EdgeSpeechPlayer
from .tray import BuddyTray
from .visual_block import parse_visual_block, text_before_visual
from .visual_window import WEB_ENGINE_IMPORT_ERROR, VisualWindow, is_web_engine_available

# Time for the compositor to drop buddy's windows from the frame before grabbing it.
SCREENSHOT_HIDE_DELAY_MILLISECONDS = 120
# How long the point-mode highlight stays after the answer has been spoken (or shown).
HIGHLIGHT_LINGER_AFTER_SPEECH_MILLISECONDS = 1500
HIGHLIGHT_LINGER_WITHOUT_SPEECH_MILLISECONDS = 5000
HIGHLIGHT_LINGER_AFTER_ERROR_MILLISECONDS = 2000

HANGUL_PATTERN = re.compile(r"[가-힣]")

# A task stops after this many steps even if it isn't done, so a confused loop can't run away.
MAXIMUM_TASK_STEPS = 25
# Pause after a step before the next screenshot, so the app can react (menus open, pages load).
STEP_SETTLE_MILLISECONDS = 800
# Moving the real mouse into a screen corner before approving aborts the task (like pyautogui).
FAILSAFE_CORNER_PIXELS = 3
CLIPBOARD_RESTORE_DELAY_MILLISECONDS = 1500


@dataclass
class _AskRequest:
    # Shown in the notch and stored in the conversation history.
    display_text: str
    is_point_mode: bool
    pointer_annotation: Optional[PointerAnnotation] = None
    pointed_element: Optional[PointedElement] = None
    # A follow-up screenshot within a task (not a new question).
    is_task_step: bool = False


@dataclass
class _TaskSession:
    """A task buddy is carrying out step by step with the user's approval."""

    goal: str
    completed_step_descriptions: List[str] = field(default_factory=list)
    proposed_action: Optional[ProposedAction] = None
    # Screenshots the proposed action's coordinates refer to.
    proposal_screen_captures: List[ScreenCapture] = field(default_factory=list)
    # The app window that had the keyboard when the step was proposed (gets it back for typing).
    target_window_id: Optional[int] = None
    last_step_error: Optional[str] = None

    @property
    def step_number(self) -> int:
        return len(self.completed_step_descriptions) + 1


class BuddyController(QObject):
    def __init__(self, application: QApplication, config: BuddyConfig):
        super().__init__()
        self._application = application
        self._config = config
        self._selected_model = config.model
        self._is_speech_enabled = config.speech_enabled
        self._last_typed_question_language: Optional[str] = None

        # (question, spoken answer) pairs; older ones drop off automatically.
        self._conversation_history = deque(maxlen=config.conversation_history_limit)
        self._active_request_id: Optional[int] = None
        self._pending_ask: Optional[_AskRequest] = None
        self._active_ask: Optional[_AskRequest] = None
        self._active_screen_captures: List[ScreenCapture] = []

        self._task_session: Optional[_TaskSession] = None
        self._input_controller: Optional[InputController] = None
        self._task_step_timer = QTimer(self)
        self._task_step_timer.setSingleShot(True)
        self._task_step_timer.timeout.connect(self._ask_for_next_task_step)

        # Point mode state while Ctrl+Alt is held.
        self._is_point_mode_active = False
        self._latest_pointed_element: Optional[PointedElement] = None
        self._latest_highlight_rectangle: Optional[QRectF] = None

        self._notch = NotchWindow(config.notch_hotkey_display_text, config.stop_hotkey_display_text)
        self._overlay = BuddyOverlay()
        if config.backend == BACKEND_API:
            # Imported here so the subscription backend runs without the anthropic package.
            from .claude_client import ClaudeResponder

            self._claude_responder = ClaudeResponder(COMPANION_SYSTEM_PROMPT)
        else:
            self._claude_responder = ClaudeCodeResponder(COMPANION_SYSTEM_PROMPT)
        self._speech_player = EdgeSpeechPlayer(config.korean_speech_voice, config.english_speech_voice)
        self._hotkey_listener = GlobalHotkeyListener(config.notch_hotkey)
        self._stop_hotkey_listener = GlobalHotkeyListener(config.stop_hotkey)
        self._point_mode_detector = PointModeDetector()
        self._pointer_inspector = PointerInspector()
        self._tray = BuddyTray(
            config.notch_hotkey_display_text,
            config.stop_hotkey_display_text,
            self._selected_model,
            self._is_speech_enabled,
        )

        # Floating window for <visual> replies (needs PyQt6-WebEngine).
        self._visual_window: Optional[VisualWindow] = VisualWindow() if is_web_engine_available() else None
        # Spoken part of a reply, started as soon as the model began writing its visual.
        self._early_spoken_text: Optional[str] = None

        self._highlight_clear_timer = QTimer(self)
        self._highlight_clear_timer.setSingleShot(True)
        self._highlight_clear_timer.timeout.connect(lambda: self._overlay.set_highlight_rectangle(None))

        self._connect_signals()

    def _connect_signals(self) -> None:
        self._notch.question_submitted.connect(self._on_question_submitted)
        self._notch.escape_pressed.connect(self._on_notch_escape_pressed)
        self._notch.action_approved.connect(self._on_action_approved)

        self._claude_responder.response_text_updated.connect(self._on_response_text_updated)
        self._claude_responder.response_completed.connect(self._on_response_completed)
        self._claude_responder.response_failed.connect(self._on_response_failed)

        self._speech_player.playback_started.connect(self._on_speech_started)
        self._speech_player.playback_finished.connect(self._on_speech_finished)
        self._speech_player.speech_failed.connect(self._on_speech_failed)

        self._hotkey_listener.hotkey_activated.connect(self._notch.toggle)
        self._hotkey_listener.hotkey_registration_failed.connect(self._on_hotkey_registration_failed)
        self._stop_hotkey_listener.hotkey_activated.connect(self._stop_answer)
        self._stop_hotkey_listener.hotkey_registration_failed.connect(self._on_hotkey_registration_failed)

        self._point_mode_detector.point_mode_started.connect(self._on_point_mode_started)
        self._point_mode_detector.pointer_moved.connect(self._pointer_inspector.request_inspection)
        self._point_mode_detector.point_mode_released.connect(self._on_point_mode_released)
        self._point_mode_detector.point_mode_cancelled.connect(self._on_point_mode_cancelled)
        self._pointer_inspector.element_found.connect(self._on_pointed_element_found)

        self._tray.open_notch_requested.connect(self._notch.expand)
        self._tray.stop_answer_requested.connect(self._stop_answer)
        self._tray.buddy_cursor_toggled.connect(self._on_buddy_cursor_toggled)
        self._tray.speech_toggled.connect(self._on_speech_toggled)
        self._tray.model_selected.connect(self._on_model_selected)
        self._tray.clear_conversation_requested.connect(self._clear_conversation)
        self._tray.quit_requested.connect(self._quit)

    def start(self) -> None:
        self._notch.set_model_label(AVAILABLE_MODELS[self._selected_model])
        self._notch.show_collapsed()
        self._overlay.show()
        if BuddyTray.is_available():
            self._tray.show()
        else:
            print("buddy: no system tray found; use the hotkey or click the notch.")
        self._hotkey_listener.start()
        self._stop_hotkey_listener.start()
        if self._config.point_mode_enabled:
            self._point_mode_detector.start()
        print(
            f"buddy: backend={self._config.backend}, model={self._selected_model}, "
            f"hotkey={self._config.notch_hotkey_display_text}, stop={self._config.stop_hotkey_display_text}, "
            f"point mode={'on' if self._config.point_mode_enabled else 'off'} "
            f"(accessibility {'available' if PointerInspector.accessibility_available() else 'unavailable'})"
        )
        if self._config.backend != BACKEND_API and ClaudeCodeResponder.find_cli() is None:
            self._notch.show_error(
                "Claude Code CLI not found. Install it and run `claude` once to log in with your subscription."
            )

    def show_notch(self) -> None:
        """Called when buddy is launched again while running."""
        self._notch.expand()

    def _reply_language(self) -> str:
        if self._config.reply_language:
            return self._config.reply_language
        if self._last_typed_question_language:
            return self._last_typed_question_language
        return detect_locale_language()

    # ----- asking -----

    def _on_question_submitted(self, question_text: str) -> None:
        self._last_typed_question_language = "ko" if HANGUL_PATTERN.search(question_text) else "en"
        self._start_ask(_AskRequest(display_text=question_text, is_point_mode=False))

    def _interrupt_current_answer(self) -> None:
        self._claude_responder.cancel_current_request()
        self._active_request_id = None
        self._speech_player.stop()
        self._overlay.cancel_pointing()

    def _start_ask(self, ask_request: _AskRequest) -> None:
        # A new question interrupts whatever the previous one was doing, including a task.
        self._interrupt_current_answer()
        if not ask_request.is_task_step:
            self._end_task_session()

        self._pending_ask = ask_request
        self._early_spoken_text = None
        self._notch.show_question(ask_request.display_text)
        self._notch.set_status("thinking")
        self._overlay.set_voice_state(BuddyVoiceState.PROCESSING)

        # Keep the notch and buddy out of the screenshot without unmapping them.
        self._notch.set_hidden_for_screenshot(True)
        self._overlay.set_hidden_for_screenshot(True)
        QTimer.singleShot(SCREENSHOT_HIDE_DELAY_MILLISECONDS, self._capture_screens_and_ask)

    def _capture_screens_and_ask(self) -> None:
        ask_request = self._pending_ask
        if ask_request is None:
            # Stopped during the short hide-before-screenshot delay.
            self._notch.set_hidden_for_screenshot(False)
            self._overlay.set_hidden_for_screenshot(False)
            return
        try:
            screen_captures = capture_all_screens(
                self._config.screenshot_max_dimension,
                ask_request.pointer_annotation if ask_request is not None else None,
            )
        finally:
            self._notch.set_hidden_for_screenshot(False)
            self._overlay.set_hidden_for_screenshot(False)

        if ask_request is None:
            return
        self._pending_ask = None
        self._active_ask = ask_request
        self._active_screen_captures = screen_captures

        if ask_request.is_task_step and self._task_session is not None:
            user_question = build_agent_step_question(
                task_goal=self._task_session.goal,
                completed_step_descriptions=self._task_session.completed_step_descriptions,
                step_number=self._task_session.step_number,
                last_step_error=self._task_session.last_step_error,
            )
        elif ask_request.is_point_mode:
            user_question = build_point_mode_question(
                cursor_capture=screen_captures[0],
                pointed_element=ask_request.pointed_element,
                reply_language=self._reply_language(),
                has_meaningful_trail=ask_request.pointer_annotation.has_meaningful_trail(),
            )
        else:
            user_question = ask_request.display_text

        self._active_request_id = self._claude_responder.start_request(
            model=self._selected_model,
            conversation_history=list(self._conversation_history),
            screen_captures=screen_captures,
            user_question=user_question,
        )

    def _on_response_text_updated(self, request_id: int, accumulated_text: str) -> None:
        if request_id != self._active_request_id:
            return
        self._notch.set_answer_text(strip_partial_point_tag(accumulated_text))
        if self._early_spoken_text is None:
            spoken_before_visual = text_before_visual(accumulated_text)
            if spoken_before_visual is not None:
                # The explanation is complete; speak it while the visual is still being written.
                self._early_spoken_text = spoken_before_visual
                self._notch.set_status("drawing")
                if self._is_speech_enabled and spoken_before_visual:
                    self._overlay.set_speech_active(True)
                    self._speech_player.speak(spoken_before_visual)

    def _on_response_completed(self, request_id: int, full_reply_text: str) -> None:
        if request_id != self._active_request_id:
            return
        self._active_request_id = None
        ask_request = self._active_ask

        is_task_step = ask_request is not None and ask_request.is_task_step

        # The visual block can sit anywhere; take it out first.
        visual_result = parse_visual_block(full_reply_text)
        if visual_result.html is not None:
            self._show_visual(visual_result.html, visual_result.title)

        # Tags sit at the end of the reply; peel them off one by one.
        action_result = parse_action_tag(visual_result.remaining_text)
        reply_text = action_result.remaining_text
        if ask_request is not None and ask_request.is_point_mode:
            box_result = parse_box_tag(reply_text)
            reply_text = box_result.remaining_text
            if box_result.box is not None and self._active_screen_captures:
                # Replace the first-guess highlight with the model's tight bounds.
                refined_rectangle = map_screenshot_box_to_global(self._active_screen_captures[0], box_result.box)
                self._overlay.set_highlight_rectangle(refined_rectangle)

        parse_result = parse_pointing_tag(reply_text)
        spoken_text = parse_result.spoken_text
        displayed_text = spoken_text
        for problem in (action_result.error, visual_result.error):
            if problem:
                displayed_text = f"{displayed_text}\n\n({problem})".strip()
        self._notch.set_answer_text(displayed_text)
        if ask_request is not None and not is_task_step:
            remembered_text = spoken_text
            if visual_result.html is not None:
                remembered_text = f"{spoken_text} (showed a visual: {visual_result.title})".strip()
            self._conversation_history.append((ask_request.display_text, remembered_text))
        if action_result.error:
            spoken_text = displayed_text

        proposed_action = action_result.action
        if proposed_action is not None and proposed_action.action_type != "done":
            self._propose_task_step(ask_request, proposed_action, speak_text=spoken_text if not is_task_step else "")
            return
        if self._task_session is not None:
            # A "done" action, or a step reply without any action, ends the task.
            summary = proposed_action.summary if proposed_action is not None else ""
            self._finish_task_session(summary)
            if summary and is_task_step:
                spoken_text = f"{spoken_text}\n{summary}".strip() if spoken_text else summary
                self._notch.set_answer_text(spoken_text)

        if parse_result.has_coordinate:
            target_capture = select_target_capture(parse_result, self._active_screen_captures)
            if target_capture is not None and self._overlay.is_shown:
                target_point = map_screenshot_point_to_global(
                    target_capture, parse_result.screenshot_x, parse_result.screenshot_y
                )
                self._overlay.point_at(target_point, parse_result.element_label)

        self._overlay.set_voice_state(BuddyVoiceState.IDLE)
        if self._early_spoken_text is not None:
            # Already spoken (or speaking) while the visual was being written.
            if self._speech_player.is_busy:
                self._notch.set_status("speaking")
            else:
                self._notch.set_status("idle")
                self._schedule_highlight_clear(HIGHLIGHT_LINGER_WITHOUT_SPEECH_MILLISECONDS)
        elif self._is_speech_enabled and spoken_text:
            # The status stays "thinking" until audio actually starts.
            self._overlay.set_speech_active(True)
            self._speech_player.speak(spoken_text)
        else:
            self._notch.set_status("idle")
            self._schedule_highlight_clear(HIGHLIGHT_LINGER_WITHOUT_SPEECH_MILLISECONDS)

    def _on_response_failed(self, request_id: int, error_message: str) -> None:
        if request_id != self._active_request_id:
            return
        self._active_request_id = None
        self._end_task_session()
        self._overlay.set_voice_state(BuddyVoiceState.IDLE)
        self._notch.show_error(error_message)
        self._schedule_highlight_clear(HIGHLIGHT_LINGER_AFTER_ERROR_MILLISECONDS)

    def _show_visual(self, html: str, title: str) -> None:
        if self._visual_window is None:
            self._notch.show_error(
                "Visuals need PyQt6-WebEngine: pip install -r requirements.txt "
                f"({WEB_ENGINE_IMPORT_ERROR})"
            )
            return
        self._visual_window.show_visual(html, title)

    def _schedule_highlight_clear(self, delay_milliseconds: int) -> None:
        self._highlight_clear_timer.start(delay_milliseconds)

    # ----- tasks: mouse and keyboard control -----

    def _get_input_controller(self) -> InputController:
        if self._input_controller is None:
            self._input_controller = InputController()
        return self._input_controller

    def _propose_task_step(self, ask_request: Optional[_AskRequest], proposed_action: ProposedAction, speak_text: str) -> None:
        if self._task_session is None:
            goal = ask_request.display_text if ask_request is not None else "the user's request"
            self._task_session = _TaskSession(goal=goal)
        task_session = self._task_session

        if task_session.step_number > MAXIMUM_TASK_STEPS:
            self._finish_task_session("")
            self._notch.show_error(f"Stopped after {MAXIMUM_TASK_STEPS} steps without finishing.")
            return

        task_session.proposed_action = proposed_action
        task_session.proposal_screen_captures = self._active_screen_captures
        try:
            task_session.target_window_id = self._get_input_controller().active_window_id()
        except InputControlError as control_error:
            self._finish_task_session("")
            self._notch.show_error(str(control_error))
            return

        # Let the buddy fly to the step's target so the user sees where it will act.
        target_point = self._action_target_point(proposed_action, task_session.proposal_screen_captures)
        if target_point is not None and self._overlay.is_shown:
            self._overlay.point_at(target_point, proposed_action.label or proposed_action.action_type)

        self._overlay.set_voice_state(BuddyVoiceState.IDLE)
        self._notch.show_action_proposal(task_session.step_number, proposed_action.describe())
        if self._is_speech_enabled and speak_text:
            self._speech_player.speak(speak_text)

    def _action_target_point(self, proposed_action: ProposedAction, screen_captures: List[ScreenCapture]):
        if proposed_action.x is None or proposed_action.y is None or not screen_captures:
            return None
        screen_index = proposed_action.screen_number - 1
        if not 0 <= screen_index < len(screen_captures):
            screen_index = 0
        return map_screenshot_point_to_global(screen_captures[screen_index], proposed_action.x, proposed_action.y)

    def _is_pointer_in_failsafe_corner(self) -> bool:
        cursor_point = QCursor.pos()
        cursor_screen = QGuiApplication.screenAt(cursor_point)
        if cursor_screen is None:
            return False
        screen_geometry = cursor_screen.geometry()
        is_at_horizontal_edge = (
            cursor_point.x() - screen_geometry.left() <= FAILSAFE_CORNER_PIXELS
            or screen_geometry.right() - cursor_point.x() <= FAILSAFE_CORNER_PIXELS
        )
        is_at_vertical_edge = (
            cursor_point.y() - screen_geometry.top() <= FAILSAFE_CORNER_PIXELS
            or screen_geometry.bottom() - cursor_point.y() <= FAILSAFE_CORNER_PIXELS
        )
        return is_at_horizontal_edge and is_at_vertical_edge

    def _on_action_approved(self) -> None:
        task_session = self._task_session
        if task_session is None or task_session.proposed_action is None:
            return
        proposed_action = task_session.proposed_action
        task_session.proposed_action = None
        self._notch.clear_action_proposal()

        if self._is_pointer_in_failsafe_corner():
            self._stop_answer()
            self._notch.show_error("Mouse was in a screen corner, so the task was stopped (failsafe).")
            return

        self._speech_player.stop()
        self._overlay.cancel_pointing()
        self._notch.set_status("acting")

        settle_milliseconds = STEP_SETTLE_MILLISECONDS
        task_session.last_step_error = None
        try:
            settle_milliseconds = self._perform_action(proposed_action, task_session)
        except (InputControlError, ValueError) as control_error:
            task_session.last_step_error = str(control_error)
        except Exception as unexpected_error:  # noqa: BLE001 - X errors must not crash the app
            task_session.last_step_error = f"unexpected error: {unexpected_error}"

        task_session.completed_step_descriptions.append(
            proposed_action.describe() + (" (failed)" if task_session.last_step_error else "")
        )
        self._task_step_timer.start(settle_milliseconds)

    def _perform_action(self, proposed_action: ProposedAction, task_session: _TaskSession) -> int:
        """Runs one approved step. Returns how long to wait before the next screenshot."""
        input_controller = self._get_input_controller()
        action_type = proposed_action.action_type

        if action_type == "wait":
            return int(proposed_action.seconds * 1000)

        if action_type in ("type", "key"):
            # The approval took the keyboard; hand it back to the app before typing into it.
            input_controller.activate_window(task_session.target_window_id)
            time.sleep(0.15)

        if action_type == "key":
            input_controller.press_key_combination(proposed_action.keys)
            return STEP_SETTLE_MILLISECONDS

        if action_type == "type":
            self._paste_text(input_controller, task_session.target_window_id, proposed_action.text)
            return STEP_SETTLE_MILLISECONDS

        # Pointer actions: screenshot pixels -> Qt logical -> physical X11 pixels.
        target_point = self._action_target_point(proposed_action, task_session.proposal_screen_captures)
        if target_point is None:
            raise ValueError("the step has no position")
        pointer_anchor = self._point_mode_detector.current_anchor()
        physical_x, physical_y = pointer_anchor.logical_to_physical(target_point)

        if action_type == "click":
            input_controller.click(physical_x, physical_y, LEFT_BUTTON, 1)
        elif action_type == "double_click":
            input_controller.click(physical_x, physical_y, LEFT_BUTTON, 2)
        elif action_type == "right_click":
            input_controller.click(physical_x, physical_y, RIGHT_BUTTON, 1)
        elif action_type == "scroll":
            input_controller.scroll(physical_x, physical_y, proposed_action.direction, proposed_action.amount)
        elif action_type == "drag":
            screen_index = max(0, min(proposed_action.screen_number - 1, len(task_session.proposal_screen_captures) - 1))
            end_point = map_screenshot_point_to_global(
                task_session.proposal_screen_captures[screen_index], proposed_action.to_x, proposed_action.to_y
            )
            end_x, end_y = pointer_anchor.logical_to_physical(end_point)
            input_controller.drag(physical_x, physical_y, end_x, end_y)
        return STEP_SETTLE_MILLISECONDS

    def _paste_text(self, input_controller: InputController, target_window_id: Optional[int], text: str) -> None:
        """Pastes through the clipboard (works for Korean and any Unicode), then restores it.
        A trailing newline becomes a real Enter key press after the paste."""
        presses_enter = text.endswith("\n")
        text_to_paste = text[:-1] if presses_enter else text
        clipboard = QGuiApplication.clipboard()
        previous_clipboard_text = clipboard.text()
        if text_to_paste:
            clipboard.setText(text_to_paste)
            # Let the clipboard ownership settle before the target app asks for the content.
            QApplication.processEvents()
            time.sleep(0.05)
            input_controller.press_key_combination(input_controller.paste_shortcut_for_window(target_window_id))
        if presses_enter:
            time.sleep(0.1)
            input_controller.press_key_combination("Return")
        QTimer.singleShot(CLIPBOARD_RESTORE_DELAY_MILLISECONDS, lambda: clipboard.setText(previous_clipboard_text))

    def _ask_for_next_task_step(self) -> None:
        if self._task_session is None:
            return
        self._start_ask(
            _AskRequest(
                display_text=f"Step {self._task_session.step_number}: checking the screen…",
                is_point_mode=False,
                is_task_step=True,
            )
        )

    def _finish_task_session(self, summary: str) -> None:
        task_session = self._task_session
        self._end_task_session()
        if task_session is not None and task_session.completed_step_descriptions:
            self._conversation_history.append(
                (f"(task) {task_session.goal}", summary or f"did {len(task_session.completed_step_descriptions)} steps")
            )

    def _end_task_session(self) -> None:
        self._task_session = None
        self._task_step_timer.stop()
        self._notch.clear_action_proposal()

    # ----- stopping -----

    def _is_answer_in_progress(self) -> bool:
        return (
            self._pending_ask is not None
            or self._task_session is not None
            or self._active_request_id is not None
            or self._speech_player.is_busy
            or self._overlay.is_pointing
            or self._overlay.has_highlight
        )

    def _stop_answer(self) -> None:
        """Stops generation, speech, the buddy's pointing and the highlight; history is kept.
        A partial answer stays visible in the notch but is not added to the history."""
        if not self._is_answer_in_progress():
            # Nothing running: the stop hotkey clears a visual that is still open.
            if self._visual_window is not None:
                self._visual_window.close_visual()
            return
        self._pending_ask = None
        self._interrupt_current_answer()
        self._end_task_session()
        self._highlight_clear_timer.stop()
        self._overlay.set_highlight_rectangle(None)
        self._overlay.set_speech_active(False)
        self._overlay.set_voice_state(BuddyVoiceState.IDLE)
        self._notch.set_status("stopped")

    def _on_notch_escape_pressed(self) -> None:
        # First Esc stops a running answer, the next one closes the notch.
        if self._is_answer_in_progress():
            self._stop_answer()
        else:
            self._notch.collapse()

    # ----- point mode -----

    def _on_point_mode_started(self, pointer_anchor: PointerAnchor) -> None:
        if not self._overlay.is_shown or self._notch.is_input_focused:
            # Typing in the notch, or the buddy is hidden: leave Ctrl+Alt alone.
            return
        self._is_point_mode_active = True
        self._highlight_clear_timer.stop()
        self._latest_pointed_element = None
        self._latest_highlight_rectangle = None
        self._overlay.set_highlight_rectangle(None)
        self._overlay.start_trail()
        self._pointer_inspector.request_inspection(pointer_anchor)

    def _on_pointed_element_found(self, pointer_anchor: PointerAnchor, pointed_element: Optional[PointedElement]) -> None:
        if not self._is_point_mode_active:
            return
        self._latest_pointed_element = pointed_element
        if pointed_element is None:
            self._latest_highlight_rectangle = None
            self._overlay.set_highlight_rectangle(None)
            return
        rect_x, rect_y, rect_width, rect_height = pointed_element.physical_rectangle
        highlight_rectangle = QRectF(
            pointer_anchor.physical_to_logical(rect_x, rect_y),
            pointer_anchor.physical_to_logical(rect_x + rect_width, rect_y + rect_height),
        )
        self._latest_highlight_rectangle = highlight_rectangle
        self._overlay.set_highlight_rectangle(highlight_rectangle)

    def _on_point_mode_released(self, pointer_anchor: PointerAnchor) -> None:
        if not self._is_point_mode_active:
            return
        self._is_point_mode_active = False
        self._overlay.stop_trail_recording()

        pointed_element = self._latest_pointed_element
        pointer_annotation = PointerAnnotation(
            pointer_point=pointer_anchor.logical_point,
            element_rectangle=self._latest_highlight_rectangle,
            trail_points=self._overlay.recorded_trail_points(),
        )
        if pointed_element is not None and pointed_element.source == "accessibility":
            display_text = f"Pointing at {pointed_element.describe()}"
        elif pointer_annotation.has_meaningful_trail():
            display_text = "Pointing at the traced area"
        else:
            display_text = "Pointing at the spot under the cursor"

        # Show the answer without taking the keyboard from the app being pointed at.
        self._notch.expand(should_focus_input=False)
        self._start_ask(
            _AskRequest(
                display_text=display_text,
                is_point_mode=True,
                pointer_annotation=pointer_annotation,
                pointed_element=pointed_element,
            )
        )

    def _on_point_mode_cancelled(self) -> None:
        if not self._is_point_mode_active:
            return
        self._is_point_mode_active = False
        self._overlay.stop_trail_recording()
        self._overlay.set_highlight_rectangle(None)

    # ----- speech -----

    def _on_speech_started(self) -> None:
        self._notch.set_status("speaking")
        self._overlay.set_voice_state(BuddyVoiceState.RESPONDING)

    def _on_speech_finished(self) -> None:
        self._overlay.set_speech_active(False)
        if self._notch.is_action_pending:
            # Still waiting for the user to approve the proposed step.
            self._overlay.set_voice_state(BuddyVoiceState.IDLE)
            self._notch.set_status("waiting")
            return
        if self._active_request_id is not None and self._early_spoken_text is not None:
            # Done talking, the visual is still being written.
            self._notch.set_status("drawing")
            return
        if self._active_request_id is None and self._pending_ask is None:
            self._overlay.set_voice_state(BuddyVoiceState.IDLE)
            self._notch.set_status("idle")
            if not self._is_point_mode_active:
                self._schedule_highlight_clear(HIGHLIGHT_LINGER_AFTER_SPEECH_MILLISECONDS)

    def _on_speech_failed(self, error_message: str) -> None:
        print(f"buddy: {error_message}")

    # ----- tray actions -----

    def _on_buddy_cursor_toggled(self, should_show: bool) -> None:
        if should_show:
            self._overlay.show()
        else:
            self._overlay.hide()

    def _on_speech_toggled(self, is_enabled: bool) -> None:
        self._is_speech_enabled = is_enabled
        if not is_enabled:
            self._speech_player.stop()

    def _on_model_selected(self, model_id: str) -> None:
        self._selected_model = model_id
        self._notch.set_model_label(AVAILABLE_MODELS[model_id])

    def _clear_conversation(self) -> None:
        self._interrupt_current_answer()
        self._overlay.set_voice_state(BuddyVoiceState.IDLE)
        self._overlay.set_highlight_rectangle(None)
        self._conversation_history.clear()
        self._notch.clear_conversation_view()
        self._notch.set_status("idle")

    def _on_hotkey_registration_failed(self, error_message: str) -> None:
        print(f"buddy: hotkey unavailable — {error_message}")
        self._tray.show_message("buddy hotkey unavailable", error_message)

    def _quit(self) -> None:
        self._hotkey_listener.stop()
        self._stop_hotkey_listener.stop()
        self._point_mode_detector.stop()
        self._pointer_inspector.shutdown()
        self._claude_responder.shutdown()
        self._speech_player.shutdown()
        self._overlay.hide()
        if self._visual_window is not None:
            self._visual_window.close_visual()
        self._tray.hide()
        self._application.quit()
