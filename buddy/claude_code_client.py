"""Claude backend that runs through the Claude Code CLI, so a Claude subscription
(Pro/Max) is used instead of an API key.

Each question starts `claude -p` in headless mode with stream-json input/output:
the screenshots go in as image blocks and the reply streams back as text deltas.
The CLI is isolated from the user's own Claude Code setup (no tools, no MCP servers,
no settings files, no slash commands, no saved session) and runs in an empty
directory so no project CLAUDE.md is picked up.
"""

import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import List, Optional, Tuple

from PyQt6.QtCore import QObject, pyqtSignal

from .message_content import build_screenshot_content_blocks
from .screen_capture import ScreenCapture

RESPONSE_EFFORT = "low"
KILL_GRACE_SECONDS = 3.0

# Removed from the child environment: an API key would make the CLI bill the API
# instead of the subscription, and the nesting marker is set when buddy itself is
# started from a Claude Code terminal.
ENVIRONMENT_VARIABLES_TO_DROP = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDECODE")


def _format_history_as_text(conversation_history: List[Tuple[str, str]]) -> str:
    """Headless input only carries user turns, so earlier exchanges are passed as a transcript."""
    transcript_lines = ["earlier in this conversation:"]
    for previous_question, previous_answer in conversation_history:
        transcript_lines.append(f"user: {previous_question}")
        transcript_lines.append(f"you: {previous_answer}")
    transcript_lines.append("(end of earlier conversation; the user's new question follows the screenshots.)")
    return "\n".join(transcript_lines)


class ClaudeCodeResponder(QObject):
    """Same signals and methods as ClaudeResponder, so the controller can use either."""

    response_text_updated = pyqtSignal(int, str)
    response_completed = pyqtSignal(int, str)
    response_failed = pyqtSignal(int, str)

    def __init__(self, system_prompt: str):
        super().__init__()
        self._system_prompt = system_prompt
        self._latest_request_id = 0
        self._running_process: Optional[subprocess.Popen] = None
        self._process_lock = threading.Lock()
        self._isolated_working_directory = tempfile.mkdtemp(prefix="buddy-claude-code-")

    @staticmethod
    def find_cli() -> Optional[str]:
        """Finds the claude CLI. Started from the app grid, PATH may lack the user's own
        bin directories (npm prefix, nvm), so common install locations are checked too."""
        configured_path = os.environ.get("BUDDY_CLAUDE_CLI", "").strip()
        if configured_path:
            return configured_path if os.access(configured_path, os.X_OK) else None
        found_path = shutil.which("claude")
        if found_path:
            return found_path
        home_directory = Path.home()
        candidate_paths = [
            home_directory / ".local/bin/claude",
            home_directory / ".claude/local/claude",
            home_directory / ".npm-global/bin/claude",
            Path("/usr/local/bin/claude"),
            *sorted((home_directory / ".nvm/versions/node").glob("*/bin/claude"), reverse=True),
        ]
        for candidate_path in candidate_paths:
            if candidate_path.is_file() and os.access(candidate_path, os.X_OK):
                return str(candidate_path)
        return None

    def cancel_current_request(self) -> None:
        self._latest_request_id += 1
        self._terminate_running_process()

    def shutdown(self) -> None:
        self.cancel_current_request()
        shutil.rmtree(self._isolated_working_directory, ignore_errors=True)

    def start_request(
        self,
        model: str,
        conversation_history: List[Tuple[str, str]],
        screen_captures: List[ScreenCapture],
        user_question: str,
    ) -> int:
        self._terminate_running_process()
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

    def _terminate_running_process(self) -> None:
        with self._process_lock:
            process_to_stop = self._running_process
            self._running_process = None
        if process_to_stop is None or process_to_stop.poll() is not None:
            return
        # The CLI exits within about a second on SIGTERM; SIGKILL only if it hangs.
        process_to_stop.terminate()
        kill_timer = threading.Timer(
            KILL_GRACE_SECONDS, lambda: process_to_stop.poll() is None and process_to_stop.kill()
        )
        kill_timer.daemon = True
        kill_timer.start()

    def _build_command(self, cli_path: str, model: str) -> List[str]:
        return [
            cli_path,
            "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--model", model,
            "--effort", RESPONSE_EFFORT,
            "--system-prompt", self._system_prompt,
            "--tools", "",
            "--strict-mcp-config",
            "--setting-sources", "",
            "--disable-slash-commands",
            "--no-session-persistence",
        ]

    def _run_request(
        self,
        request_id: int,
        model: str,
        conversation_history: List[Tuple[str, str]],
        screen_captures: List[ScreenCapture],
        user_question: str,
    ) -> None:
        cli_path = self.find_cli()
        if cli_path is None:
            self.response_failed.emit(
                request_id,
                "Claude Code CLI not found. Install it and run `claude` once to log in with your subscription.",
            )
            return

        content_blocks = []
        if conversation_history:
            content_blocks.append({"type": "text", "text": _format_history_as_text(conversation_history)})
        content_blocks.extend(build_screenshot_content_blocks(screen_captures, user_question))
        input_line = json.dumps(
            {"type": "user", "message": {"role": "user", "content": content_blocks}}
        )

        child_environment = {
            name: value for name, value in os.environ.items() if name not in ENVIRONMENT_VARIABLES_TO_DROP
        }
        # An npm-installed CLI runs through `env node`; node sits next to it (nvm).
        cli_directory = os.path.dirname(cli_path)
        child_environment["PATH"] = os.pathsep.join(
            [cli_directory] + [entry for entry in child_environment.get("PATH", "").split(os.pathsep) if entry]
        )

        try:
            cli_process = subprocess.Popen(
                self._build_command(cli_path, model),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self._isolated_working_directory,
                env=child_environment,
                text=True,
                encoding="utf-8",
            )
        except OSError as launch_error:
            self.response_failed.emit(request_id, f"Could not start Claude Code: {launch_error}")
            return

        with self._process_lock:
            if not self._is_current(request_id):
                cli_process.terminate()
                return
            self._running_process = cli_process

        # Drain stderr in the background so a chatty CLI cannot block on a full pipe.
        stderr_chunks: List[str] = []
        stderr_thread = threading.Thread(
            target=lambda: stderr_chunks.append(cli_process.stderr.read()), daemon=True
        )
        stderr_thread.start()

        try:
            cli_process.stdin.write(input_line + "\n")
            cli_process.stdin.close()
        except OSError:
            pass

        accumulated_text = ""
        result_event = None
        for output_line in cli_process.stdout:
            if not self._is_current(request_id):
                break
            try:
                output_event = json.loads(output_line)
            except json.JSONDecodeError:
                continue

            event_type = output_event.get("type")
            if event_type == "stream_event":
                stream_event = output_event.get("event", {})
                delta = stream_event.get("delta", {})
                if stream_event.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                    accumulated_text += delta.get("text", "")
                    self.response_text_updated.emit(request_id, accumulated_text)
            elif event_type == "result":
                result_event = output_event

        cli_process.wait()
        stderr_thread.join(timeout=1.0)
        with self._process_lock:
            if self._running_process is cli_process:
                self._running_process = None

        if not self._is_current(request_id):
            return

        if result_event is None:
            stderr_text = "".join(stderr_chunks).strip()
            self.response_failed.emit(
                request_id,
                f"Claude Code exited without an answer (code {cli_process.returncode}). {stderr_text[-300:]}",
            )
            return

        if result_event.get("is_error"):
            error_text = str(result_event.get("result") or result_event.get("subtype") or "unknown error")
            self.response_failed.emit(request_id, f"Claude Code error: {error_text}")
            return

        if result_event.get("stop_reason") == "refusal":
            self.response_failed.emit(request_id, "Claude declined to answer this one.")
            return

        final_text = result_event.get("result")
        self.response_completed.emit(request_id, final_text if isinstance(final_text, str) else accumulated_text)
