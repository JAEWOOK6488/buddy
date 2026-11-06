"""Claude backend using the Anthropic API directly (needs ANTHROPIC_API_KEY, billed per use).

Streams replies for a typed question plus screenshots, off the GUI thread.
"""

import threading
from typing import List, Optional, Tuple

import anthropic
from PyQt6.QtCore import QObject, pyqtSignal

from .config import MODELS_WITH_SERVER_SIDE_FALLBACK, SERVER_SIDE_FALLBACK_BETA
from .message_content import build_screenshot_content_blocks
from .screen_capture import ScreenCapture

# The reply is short and spoken, but thinking (always on for Opus 5.5) shares this budget.
MAX_RESPONSE_TOKENS = 16000
# Conversational route: low effort keeps the time to first word short.
RESPONSE_EFFORT = "low"


def _describe_api_error(error: Exception) -> str:
    if isinstance(error, anthropic.AuthenticationError):
        return "API key was rejected. Check ANTHROPIC_API_KEY in .env."
    if isinstance(error, anthropic.PermissionDeniedError):
        return "This API key has no access to the selected model."
    if isinstance(error, anthropic.NotFoundError):
        return "Model not found. Pick another model from the tray menu."
    if isinstance(error, anthropic.RateLimitError):
        return "Rate limited. Wait a moment and ask again."
    if isinstance(error, anthropic.BadRequestError):
        return f"Request rejected: {error.message}"
    if isinstance(error, anthropic.APIStatusError):
        return f"API error {error.status_code}. Try again shortly."
    if isinstance(error, anthropic.APIConnectionError):
        return "Network error. Check your internet connection."
    return f"Unexpected error: {error}"


class ClaudeResponder(QObject):
    """Runs one streaming request at a time. Starting a new request makes the previous
    one stop emitting, so a quick follow-up question cancels the old answer."""

    # (request_id, text accumulated so far)
    response_text_updated = pyqtSignal(int, str)
    # (request_id, full reply text)
    response_completed = pyqtSignal(int, str)
    # (request_id, user-facing error message)
    response_failed = pyqtSignal(int, str)

    def __init__(self, system_prompt: str):
        super().__init__()
        self._system_prompt = system_prompt
        self._anthropic_client: Optional[anthropic.Anthropic] = None
        self._latest_request_id = 0

    def _get_client(self) -> anthropic.Anthropic:
        # Created lazily so a missing key surfaces as a notch error, not a crash at startup.
        if self._anthropic_client is None:
            self._anthropic_client = anthropic.Anthropic(timeout=120.0, max_retries=2)
        return self._anthropic_client

    def cancel_current_request(self) -> None:
        self._latest_request_id += 1

    def shutdown(self) -> None:
        self.cancel_current_request()

    def start_request(
        self,
        model: str,
        conversation_history: List[Tuple[str, str]],
        screen_captures: List[ScreenCapture],
        user_question: str,
    ) -> int:
        self._latest_request_id += 1
        request_id = self._latest_request_id
        request_thread = threading.Thread(
            target=self._run_request,
            args=(request_id, model, list(conversation_history), screen_captures, user_question),
            daemon=True,
        )
        request_thread.start()
        return request_id

    def _is_current(self, request_id: int) -> bool:
        return request_id == self._latest_request_id

    def _run_request(
        self,
        request_id: int,
        model: str,
        conversation_history: List[Tuple[str, str]],
        screen_captures: List[ScreenCapture],
        user_question: str,
    ) -> None:
        # History is text-only (no old screenshots, no thinking blocks) to keep requests small.
        messages = []
        for previous_question, previous_answer in conversation_history:
            messages.append({"role": "user", "content": previous_question})
            messages.append({"role": "assistant", "content": previous_answer})
        messages.append({"role": "user", "content": build_screenshot_content_blocks(screen_captures, user_question)})

        request_arguments = dict(
            model=model,
            max_tokens=MAX_RESPONSE_TOKENS,
            system=[{"type": "text", "text": self._system_prompt, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
            output_config={"effort": RESPONSE_EFFORT},
        )

        try:
            client = self._get_client()
            if model in MODELS_WITH_SERVER_SIDE_FALLBACK:
                # On a safety decline the API retries on Anthropic's recommended fallback model.
                stream_manager = client.beta.messages.stream(
                    betas=[SERVER_SIDE_FALLBACK_BETA],
                    fallbacks="default",
                    **request_arguments,
                )
            else:
                stream_manager = client.messages.stream(**request_arguments)

            accumulated_text = ""
            with stream_manager as response_stream:
                for text_delta in response_stream.text_stream:
                    if not self._is_current(request_id):
                        return
                    accumulated_text += text_delta
                    self.response_text_updated.emit(request_id, accumulated_text)
                final_message = response_stream.get_final_message()

            if not self._is_current(request_id):
                return

            if final_message.stop_reason == "refusal":
                self.response_failed.emit(request_id, "Claude declined to answer this one.")
                return

            full_reply_text = "".join(
                content_block.text for content_block in final_message.content if content_block.type == "text"
            )
            self.response_completed.emit(request_id, full_reply_text)
        except Exception as error:  # noqa: BLE001 - every failure must reach the notch
            if self._is_current(request_id):
                self.response_failed.emit(request_id, _describe_api_error(error))
