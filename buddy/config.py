"""Runtime configuration loaded from environment variables and an optional .env file."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Model id -> label shown in the tray menu.
AVAILABLE_MODELS = {
    "claude-opus-5-5": "Opus 5.5",
    "claude-sonnet-5-5": "Sonnet 5.5",
    "claude-haiku-5-5": "Haiku 5.5",
}

DEFAULT_MODEL = "claude-opus-5-5"

# Server-side refusal fallback ("fallbacks": "default") is only accepted on these
# models; Haiku has no server-side fallback, so the plain endpoint is used there.
MODELS_WITH_SERVER_SIDE_FALLBACK = {"claude-opus-5-5", "claude-sonnet-5-5"}
SERVER_SIDE_FALLBACK_BETA = "server-side-fallback-2026-07-01"

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


BACKEND_SUBSCRIPTION = "subscription"
BACKEND_API = "api"


@dataclass
class BuddyConfig:
    # "subscription": through the Claude Code CLI login (Pro/Max).
    # "api": Anthropic API with ANTHROPIC_API_KEY.
    backend: str
    model: str
    notch_hotkey: str
    notch_hotkey_display_text: str
    # Stops the current answer (generation, speech, pointing) from anywhere.
    stop_hotkey: str
    stop_hotkey_display_text: str
    speech_enabled: bool
    korean_speech_voice: str
    english_speech_voice: str
    # "ko"/"en" from BUDDY_LANGUAGE, or "" to follow the last typed question, then the system locale.
    # Only matters for point mode, where there is no typed text to take the language from.
    reply_language: str
    # Hold Ctrl+Alt and point to have buddy explain the element under the mouse.
    point_mode_enabled: bool
    # How mouse/keyboard tasks are approved:
    #   once  - the request itself is the approval; steps run to the end automatically,
    #           except a step that presses Enter in a terminal (runs a command) still asks
    #   never - every step runs automatically, terminals included
    #   each  - every step waits for Enter
    task_approval: str = "once"
    # Screenshots are downscaled so the longest side is at most this many pixels.
    screenshot_max_dimension: int = 1280
    # Only the most recent exchanges are resent to keep the request small.
    conversation_history_limit: int = 10


TASK_APPROVAL_MODES = ("once", "never", "each")


def _read_task_approval() -> str:
    task_approval = os.environ.get("BUDDY_TASK_APPROVAL", "once").strip().lower()
    return task_approval if task_approval in TASK_APPROVAL_MODES else "once"


def _read_bool(variable_name: str, default_value: bool) -> bool:
    raw_value = os.environ.get(variable_name)
    if raw_value is None:
        return default_value
    return raw_value.strip().lower() in ("1", "true", "yes", "on")


def _hotkey_to_display_text(hotkey: str) -> str:
    """Turns 'ctrl+alt+space' into 'Ctrl+Alt+Space'."""
    key_names = [key_name.strip() for key_name in hotkey.split("+")]
    return "+".join(key_name.capitalize() for key_name in key_names)


def load_config() -> BuddyConfig:
    # A .env in the current directory wins over one in the repository root.
    load_dotenv(Path.cwd() / ".env")
    load_dotenv(REPOSITORY_ROOT / ".env")

    model = os.environ.get("BUDDY_MODEL", DEFAULT_MODEL)
    if model not in AVAILABLE_MODELS:
        model = DEFAULT_MODEL

    notch_hotkey = os.environ.get("BUDDY_NOTCH_HOTKEY", "ctrl+alt+space")
    # Not ctrl+alt+Escape: GNOME already uses it (cycle-panels).
    stop_hotkey = os.environ.get("BUDDY_STOP_HOTKEY", "ctrl+alt+BackSpace")

    backend = os.environ.get("BUDDY_BACKEND", BACKEND_SUBSCRIPTION).strip().lower()
    if backend not in (BACKEND_SUBSCRIPTION, BACKEND_API):
        backend = BACKEND_SUBSCRIPTION

    return BuddyConfig(
        backend=backend,
        model=model,
        notch_hotkey=notch_hotkey,
        notch_hotkey_display_text=_hotkey_to_display_text(notch_hotkey),
        stop_hotkey=stop_hotkey,
        stop_hotkey_display_text=_hotkey_to_display_text(stop_hotkey),
        speech_enabled=_read_bool("BUDDY_SPEECH_ENABLED", True),
        korean_speech_voice=os.environ.get("BUDDY_VOICE_KO", "ko-KR-SunHiNeural"),
        english_speech_voice=os.environ.get("BUDDY_VOICE_EN", "en-US-AriaNeural"),
        reply_language=_read_configured_language(),
        point_mode_enabled=_read_bool("BUDDY_POINT_MODE", True),
        task_approval=_read_task_approval(),
    )


def _read_configured_language() -> str:
    configured_language = os.environ.get("BUDDY_LANGUAGE", "").strip().lower()
    return configured_language if configured_language in ("ko", "en") else ""


def detect_locale_language() -> str:
    system_locale = os.environ.get("LANGUAGE") or os.environ.get("LC_ALL") or os.environ.get("LANG") or ""
    return "ko" if system_locale.lower().startswith("ko") else "en"
