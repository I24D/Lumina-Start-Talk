import platform as _platform
import subprocess as _subprocess

# ── Console encoding ─────────────────────────────────────────────────────────
# Windows consoles default to a legacy codepage — cp1254 in Turkey, cp1251 in
# Russia, cp932 in Japan. Printing an emoji there raises UnicodeEncodeError, and
# several of these prints sit inside except handlers, so the handler itself dies
# and skips the recovery code after it. Reconfiguring to UTF-8 with a
# replacement fallback costs nothing and makes the app behave in every locale.
import sys as _sys

for _stream in ("stdout", "stderr"):
    try:
        _s = getattr(_sys, _stream, None)
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass          # pythonw / redirected pipes / anything exotic — never fatal

# ── Nuclear: force CREATE_NO_WINDOW on EVERY subprocess call on Windows ───────
# This patches Popen itself, so no per-file flag is needed anywhere.
if _platform.system() == "Windows":
    _OrigPopen = _subprocess.Popen

    class _Popen(_OrigPopen):
        def __init__(self, args, **kw):
            kw["creationflags"] = kw.get("creationflags", 0) | _subprocess.CREATE_NO_WINDOW
            kw.pop("startupinfo", None)   # drop any stale/shared STARTUPINFO
            super().__init__(args, **                       kw)

    _subprocess.Popen = _Popen

# ── Single UI instance ───────────────────────────────────────────────────────
# One HUD owns one microphone and one Gemini Live session. A Windows named
# mutex makes that invariant independent of which launcher was clicked and is
# released automatically even after a crash. A duplicate raises the existing
# LUMINA window and exits before importing the heavy audio and model modules.
_LUMINA_INSTANCE_MUTEX = None


def _focus_existing_lumina() -> None:
    """Restore the existing LUMINA window after a duplicate launch."""
    if _platform.system() != "Windows":
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )

        def visit(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            title = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title, len(title))
            if title.value.strip().upper() != "LUMINA":
                return True
            user32.ShowWindowAsync(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            return False

        user32.EnumWindows(callback_type(visit), 0)
    except Exception:
        # Focusing is a convenience; ownership remains correct even if Windows
        # declines to foreground a window from another process.
        pass


def _claim_lumina_instance() -> bool:
    """Return False when another Windows process already owns the LUMINA UI."""
    global _LUMINA_INSTANCE_MUTEX
    if _platform.system() != "Windows":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        )
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        handle = kernel32.CreateMutexW(
            None, False, "Local\\I24D.LuminaStartTalk.SingleUI"
        )
        if not handle:
            return True  # Fail open: a missing UI is worse than a duplicate.
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            _focus_existing_lumina()
            return False
        _LUMINA_INSTANCE_MUTEX = handle
    except Exception:
        return True
    return True


if __name__ == "__main__" and not _claim_lumina_instance():
    raise SystemExit(0)

# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import concurrent.futures
import re
import threading
import time
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

import sounddevice as sd
import numpy as np
from google import genai
from google.genai import types
from ui import JarvisUI
from memory.memory_manager import (
    load_memory, update_memory, format_memory_for_prompt,
    save_session_summary, pop_last_session,
    search_memory, set_trim_notifier,
)
from memory.conversation_log import (
    flush as flush_conversation_log,
    log_turn,
    search_history,
)

from actions.file_processor import file_processor
from actions.flight_finder     import flight_finder
from actions.open_app          import open_app
from actions.weather_report    import weather_action
from actions.send_message      import send_message
from actions.reminder          import reminder
from actions.computer_settings import computer_settings
from actions.screen_processor  import _capture_camera, _capture_screen
from actions.youtube_video     import youtube_video
from actions.desktop           import desktop_control
from actions.browser_control   import browser_control
from actions.file_controller   import file_controller
from actions.code_helper       import code_helper
from actions.dev_agent         import dev_agent
from actions.web_search        import web_search as web_search_action
from actions.computer_control  import computer_control
from actions.game_updater      import game_updater
from actions.system_monitor    import SystemMonitor, get_system_status
from actions.proactive         import ProactiveEngine
from actions.background_monitor import (
    add_monitor, remove_monitor, list_monitors, check_all as monitor_check_all,
)
from actions.web_search        import _news as _fetch_news_sync
from memory.config_manager     import (
    get_brief_enabled, get_voice, get_input_device, get_output_device,
)
from core.plugin_loader        import discover_plugins
from core                      import undo as undo_stack
from core                      import confirm as confirm_gate
from core                      import audio_devices

def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
PROMPT_PATH     = BASE_DIR / "core" / "prompt.txt"
LIVE_MODEL          = "models/gemini-2.5-flash-native-audio-preview-12-2025"
# How long the microphone may go without delivering a single block before it is
# treated as dead rather than as a quiet room. Blocks arrive continuously while
# a stream is healthy — silence still produces them — so a gap this long means
# the device stopped, not that nobody spoke. Long enough that a brief hiccup
# does not trigger a needless reopen.
_MIC_STALL_SECONDS  = 6.0

# How much real speech has to go unheard before the session is treated as deaf.
# Generous on purpose: a false alarm costs a reconnect in the middle of a
# conversation, and several seconds of speech with no transcription at all is
# already well outside anything normal.
# Deliberately conservative, because the first version was not and turned one
# fault into a worse one: it reset the voice counter when it fired but not the
# silence clock, so the condition was still true the instant the new session
# came up and it reconnected in a loop. A level of 0.02 also counts a quiet
# room as somebody talking.
_DEAF_VOICE_SECONDS   = 8.0     # this much *clear* speech, not this much sound
_DEAF_SILENCE_SECONDS = 25.0
_DEAF_VOICE_LEVEL     = 0.15    # well above a room; a person talking clears it
_DEAF_COOLDOWN        = 120.0   # never rebuild more often than this
CHANNELS            = 1
SEND_SAMPLE_RATE    = 16000
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE          = 1024

# Client-side VAD complements Gemini's automatic detector. The server still
# detects speech starts (and remains the fallback for speech ends), while this
# detector flushes the stream after a natural pause so the model can begin its
# response without waiting on a delayed remote timeout.
_LOCAL_VAD_MIN_START_LEVEL   = 0.20
_LOCAL_VAD_MIN_END_LEVEL     = 0.08
_LOCAL_VAD_START_BLOCKS      = 3
_LOCAL_VAD_SILENCE_BLOCKS    = 10  # 640 ms at 16 kHz / 1024 samples

# RMS below which 16-bit PCM is treated as room silence; above _LEVEL_FULL it
# reads as a full-height waveform. Tuned so ordinary speech lands mid-range and
# the bars still move for a quiet talker — language- and device-independent.
_LEVEL_FLOOR = 60.0
_LEVEL_FULL  = 2600.0


# ── Voice-path diagnostics ───────────────────────────────────────────────────
# Set LUMINA_DIAG=1 to trace the whole voice path: what the microphone captures,
# whether that audio is actually being forwarded, what Gemini transcribes back,
# and when a turn closes. "She does not answer me" has several very different
# causes — a muted or wrong microphone, audio dropped because the assistant was
# still speaking, or the model hearing perfectly well and choosing to stay quiet
# — and they are indistinguishable from the outside. Off by default: it prints
# a line a second while listening.
_DIAG = False
try:
    import os as _os_diag
    _DIAG = _os_diag.environ.get("LUMINA_DIAG", "").lower() not in ("", "0", "false", "no")
except Exception:
    _DIAG = False


def _diag(msg: str) -> None:
    if _DIAG:
        print(f"[DIAG {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _pcm_level(samples) -> float:
    """Map a block of int16 PCM samples to a 0.0–1.0 loudness level for the HUD
    waveform. Returns 0.0 on empty/invalid input so it can never raise."""
    try:
        x = np.asarray(samples, dtype=np.float32)
        if x.size == 0:
            return 0.0
        rms = float(np.sqrt(np.mean(x * x)))
    except Exception:
        return 0.0
    if rms <= _LEVEL_FLOOR:
        return 0.0
    return min(1.0, (rms - _LEVEL_FLOOR) / (_LEVEL_FULL - _LEVEL_FLOOR))


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are LUMINA, a capable personal AI assistant. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results — always call the appropriate tool."
        )

_CTRL_RE = re.compile(r"<ctrl\d+>", re.IGNORECASE)


def _clean_transcript_fragment(text: str) -> str:
    """Remove control tokens without destroying streaming word boundaries."""
    text = _CTRL_RE.sub("", text or "")
    return re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)


def _normalise_transcript_spacing(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


class _TranscriptAccumulator:
    """Combine delta and cumulative transcription events into one live line.

    Live API models may emit token-sized deltas (including leading spaces),
    cumulative hypotheses, or both. Stripping every event and joining it with a
    space splits words such as ``Connection`` into visible syllables. This class
    preserves delta boundaries while allowing a newer cumulative hypothesis to
    replace the text it supersedes.
    """

    def __init__(self):
        self._committed = ""
        self._interim = ""

    @property
    def text(self) -> str:
        committed = _normalise_transcript_spacing(self._committed)
        interim = _normalise_transcript_spacing(self._interim)
        if not interim:
            return committed
        if not committed or interim.startswith(committed):
            return interim
        if committed.endswith(interim):
            return committed
        return _normalise_transcript_spacing(self._committed + self._interim)

    def add_final(self, text: str) -> bool:
        fragment = _clean_transcript_fragment(text)
        candidate = _normalise_transcript_spacing(fragment)
        if not candidate:
            return False

        current = _normalise_transcript_spacing(self._committed)
        if candidate == current or (current and current.endswith(candidate)):
            changed = False
        elif current and candidate.startswith(current):
            self._committed = fragment
            changed = True
        else:
            self._committed += fragment
            changed = True
        self._interim = ""
        return changed

    def set_interim(self, text: str) -> bool:
        fragment = _clean_transcript_fragment(text)
        if fragment == self._interim:
            return False
        self._interim = fragment
        return True

    def clear(self) -> None:
        self._committed = ""
        self._interim = ""


class _LocalVoiceActivityDetector:
    """Detect speech endings locally while leaving speech starts to Gemini.

    The microphone noise floor is estimated continuously, so a loud laptop fan
    or an amplified input does not look like speech. Three consecutive blocks
    above the adaptive start threshold reject isolated taps. Ten quiet blocks
    preserve natural pauses while finalizing a clean turn in roughly 640 ms.
    """

    def __init__(self):
        self._active = False
        self._voiced_blocks = 0
        self._silent_blocks = 0
        self._recent_levels: list[float] = []
        self._noise_floor = 0.0
        self._speech_peak = 0.0

    def _observe_noise(self, level: float) -> None:
        self._recent_levels.append(max(0.0, min(1.0, float(level))))
        del self._recent_levels[:-48]
        # The lower quartile is stable when speech occupies part of the rolling
        # window, unlike an average that rises sharply as soon as a user talks.
        self._noise_floor = float(np.percentile(self._recent_levels, 25))

    def _start_threshold(self) -> float:
        margin = max(0.18, self._noise_floor * 0.55)
        return min(0.90, max(_LOCAL_VAD_MIN_START_LEVEL,
                             self._noise_floor + margin))

    def _end_threshold(self) -> float:
        margin = max(0.10, self._noise_floor * 0.30)
        return min(0.85, max(_LOCAL_VAD_MIN_END_LEVEL,
                             self._noise_floor + margin))

    def process(self, level: float) -> str | None:
        if not self._active:
            self._observe_noise(level)
            if level >= self._start_threshold():
                self._voiced_blocks += 1
                if self._voiced_blocks >= _LOCAL_VAD_START_BLOCKS:
                    self._active = True
                    self._silent_blocks = 0
                    self._speech_peak = level
                    return "start"
            else:
                self._voiced_blocks = 0
            return None

        self._speech_peak = max(self._speech_peak * 0.995, level)
        strong_speech = max(self._start_threshold(), self._speech_peak * 0.78)
        if level >= strong_speech:
            self._voiced_blocks += 1
            # A single sharp noise must not erase an otherwise complete pause.
            if self._voiced_blocks >= 2:
                self._silent_blocks = 0
            return None
        self._voiced_blocks = 0

        quiet_level = max(self._end_threshold(), self._speech_peak * 0.50)
        if level < quiet_level:
            self._silent_blocks += 1
        else:
            # Borderline background noise should delay finalization slightly,
            # not erase all of the silence already observed.
            self._silent_blocks = max(0, self._silent_blocks - 1)
        if self._silent_blocks >= _LOCAL_VAD_SILENCE_BLOCKS:
            self.reset()
            return "end"
        return None

    def reset(self) -> None:
        self._active = False
        self._voiced_blocks = 0
        self._silent_blocks = 0
        self._speech_peak = 0.0


# ── Wake phrases ─────────────────────────────────────────────────────────────
# Proactive audio lets the model stay quiet when it judges that speech was not
# addressed to it. That is the right default in a room with other people, and
# wrong when it misjudges: the user talks and nothing answers, with no way back
# in. These phrases are the way back in — they are checked locally, on the
# transcript, so they work even when the model has decided to say nothing.
#
# Both languages, because the user speaks both. The assistant's name is
# substituted at match time so a renamed assistant keeps its wake phrase.
_WAKE_TEMPLATES = (
    "{name} activate",
    "{name} start talk",
    "{name} activa",
    "{name} despierta",
    "activa {name}",
    "despierta {name}",
)

_WAKE_STRIP_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WAKE_ACCENTS = str.maketrans("áàäâéèëêíìïîóòöôúùüûñ", "aaaaeeeeiiiioooouuuun")


def _normalise_for_wake(text: str) -> str:
    """Lowercase, unaccented, punctuation-free — so 'Lumina, ¡activate!' matches."""
    text = text.lower().translate(_WAKE_ACCENTS)
    return re.sub(r"\s+", " ", _WAKE_STRIP_RE.sub(" ", text)).strip()


# ── Closing the session ──────────────────────────────────────────────────────
# Shutting down is the one action with no undo: the window goes, and whatever
# the user was in the middle of goes with it. Left to the model's judgement it
# fired on "muchas gracias" and ended a working session, so judgement is no
# longer what decides it. One phrase does, checked here against the transcript
# exactly the way the wake phrases are — because a rule the code enforces
# cannot be talked out of.
#
# Both languages, and the article is optional because speech transcription is
# not reliable about small words. Nothing else counts: not goodbye, not thanks,
# not "that's all".
_CLOSE_TEMPLATES = (
    "cierra sesion",
    "cierra la sesion",
    "cerrar sesion",
    "close session",
    "close the session",
)

# How long a heard close phrase stays valid. The model often takes a turn to
# act on it, and requiring the same turn would make a deliberate instruction
# fail at random. Long enough to survive that, short enough that a phrase from
# earlier in the day can never close anything.
_CLOSE_PHRASE_SECONDS = 90


def _is_close_phrase(text: str) -> bool:
    """Did the user actually ask to close the session, in either language?"""
    haystack = _normalise_for_wake(text)
    return any(phrase in haystack for phrase in _CLOSE_TEMPLATES)


def _is_wake_phrase(text: str, assistant_name: str) -> bool:
    """True when the user is asking the assistant to wake up."""
    if not text:
        return False
    haystack = _normalise_for_wake(text)
    name = _normalise_for_wake(assistant_name) or "lumina"
    return any(
        _normalise_for_wake(t.format(name=name)) in haystack
        for t in _WAKE_TEMPLATES
    )

TOOL_DECLARATIONS = [
    {
        "name": "open_app",
        "description": (
            "Opens any application on the computer. "
            "Use this whenever the user asks to open, launch, or start any app, "
            "website, or program. Always call this tool — never just say you opened it."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "app_name": {
                    "type": "STRING",
                    "description": "Exact name of the application (e.g. 'WhatsApp', 'Chrome', 'Spotify')"
                }
            },
            "required": ["app_name"]
        }
    },
    {
        "name": "web_search",
        "description": (
            "Searches the web. Use for ANY question about current facts, events, prices, "
            "or topics — always prefer this over guessing. "
            "Modes: 'search' (default), 'news' (latest headlines on a topic), "
            "'research' (deep comprehensive answer), 'price' (product cost lookup), "
            "'compare' (side-by-side comparison of items)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query":  {"type": "STRING", "description": "Search query or topic"},
                "mode":   {"type": "STRING", "description": "search | news | research | price | compare"},
                "items":  {"type": "ARRAY",  "items": {"type": "STRING"}, "description": "Items to compare (compare mode)"},
                "aspect": {"type": "STRING", "description": "Comparison aspect: price | specs | reviews | features"},
            },
            "required": ["query"]
        }
    },
    {
        "name": "system_status",
        "description": (
            "Returns real-time system metrics: CPU usage, RAM, GPU load, CPU temperature, "
            "uptime, and process count. Use when the user asks about computer performance, "
            "temperature, memory, or resource usage."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "weather_report",
        "description": "Gives the weather report to user",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "city": {"type": "STRING", "description": "City name"}
            },
            "required": ["city"]
        }
    },
    {
        "name": "send_message",
        "description": "Sends a text message via WhatsApp, Telegram, or other messaging platform.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "receiver":     {"type": "STRING", "description": "Recipient contact name"},
                "message_text": {"type": "STRING", "description": "The message to send"},
                "platform":     {"type": "STRING", "description": "Platform: WhatsApp, Telegram, etc."}
            },
            "required": ["receiver", "message_text", "platform"]
        }
    },
    {
        "name": "reminder",
        "description": "Sets a timed reminder using Task Scheduler.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "date":    {"type": "STRING", "description": "Date in YYYY-MM-DD format"},
                "time":    {"type": "STRING", "description": "Time in HH:MM format (24h)"},
                "message": {"type": "STRING", "description": "Reminder message text"}
            },
            "required": ["date", "time", "message"]
        }
    },
    {
        "name": "youtube_video",
        "description": (
            "Controls YouTube. Use for: playing videos, summarizing a video's content, "
            "getting video info, or showing trending videos."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "play | summarize | get_info | trending (default: play)"},
                "query":  {"type": "STRING", "description": "Search query for play action"},
                "save":   {"type": "BOOLEAN", "description": "Save summary to Notepad (summarize only)"},
                "region": {"type": "STRING", "description": "Country code for trending e.g. TR, US"},
                "url":    {"type": "STRING", "description": "Video URL for get_info action"},
            },
            "required": []
        }
    },
    {
        "name": "screen_process",
        "description": (
            "Captures the screen or webcam image and lets you analyze it. "
            "MUST be called when user asks what is on screen, what you see, "
            "look at camera, analyze my screen, etc. "
            "You have NO visual ability without this tool. "
            "After the image is captured it is sent directly to you — describe what you see and answer the user's question. "
            "When using camera: the live view stays open until user says close it or calls close_camera."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "angle": {"type": "STRING", "description": "'screen' to capture display, 'camera' for webcam. Default: 'screen'"},
                "text":  {"type": "STRING", "description": "The question or instruction about the captured image"}
            },
            "required": ["text"]
        }
    },
    {
        "name": "close_camera",
        "description": (
            "Closes the live camera view shown on screen. "
            "Call when user says: close camera, stop camera, turn off camera, "
            "cierra la cámara, apaga la cámara, quita la cámara, creepy, etc."
        ),
        "parameters": {"type": "OBJECT", "properties": {}, "required": []}
    },
    {
        "name": "computer_settings",
        "description": (
            "Controls the computer: volume, brightness, window management, keyboard shortcuts, "
            "typing text on screen, closing apps, fullscreen, dark mode, WiFi, restart, shutdown, "
            "scrolling, tab management, zoom, screenshots, lock screen, refresh/reload page. "
            "Use for ANY single computer control command. "
            "restart, shutdown and toggle_wifi put a confirmation on the user's screen "
            "and do NOT happen until they press it — never claim they are done. "
            "Volume, brightness and dark mode can be reversed with the `undo` tool."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                # The exact vocabulary, spelled out.
                #
                # This used to say only "The action to perform", so the model
                # usually filled `description` instead — and computer_settings
                # then made a SECOND Gemini call, inside the tool, purely to
                # translate that sentence into one of these names. Every
                # "turn the volume down" cost two model round trips.
                "action": {
                    "type": "STRING",
                    "description": (
                        "The exact action. Prefer this over `description` — pick one of: "
                        "volume_up | volume_down | volume_set | mute | "
                        "brightness_up | brightness_down | sleep_display | "
                        "pause_video | close_app | close_window | full_screen | "
                        "minimize | maximize | snap_left | snap_right | "
                        "switch_window | show_desktop | task_manager | focus_search | "
                        "refresh_page | close_tab | new_tab | next_tab | prev_tab | "
                        "go_back | go_forward | zoom_in | zoom_out | zoom_reset | "
                        "find_on_page | scroll_up | scroll_down | scroll_top | "
                        "scroll_bottom | page_up | page_down | copy | paste | cut | "
                        "undo | redo | select_all | save | enter | escape | press_key | "
                        "type_text | screenshot | lock_screen | open_settings | "
                        "file_explorer | open_run | dark_mode | toggle_wifi | "
                        "restart | shutdown"
                    ),
                },
                "description": {
                    "type": "STRING",
                    "description": (
                        "Fallback only, when no action name above fits. "
                        "Resolved locally — no extra model call."
                    ),
                },
                "value":       {"type": "STRING", "description": "Optional value: volume level 0-100, text to type, key name, etc."}
            },
            "required": []
        }
    },
    {
        "name": "browser_control",
        "description": (
            "Controls any web browser. Use for: opening websites, searching the web, "
            "clicking elements, filling forms, scrolling, screenshots, navigation, any web-based task. "
            "Simple open/search requests launch the user's own browser normally (their real profile "
            "and logged-in accounts); interactive actions (click, type, fill_form...) attach an "
            "automation browser. "
            "Always pass the 'browser' parameter when the user specifies a browser (e.g. 'open in Edge', "
            "'use Firefox', 'open Chrome'). Multiple browsers can run simultaneously."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "go_to | search | click | type | scroll | fill_form | smart_click | smart_type | get_text | get_url | press | new_tab | close_tab | screenshot | back | forward | reload | switch | list_browsers | close | close_all"},
                "browser":     {"type": "STRING", "description": "Target browser: chrome | edge | firefox | opera | operagx | brave | vivaldi | safari. Omit to use the currently active browser."},
                "url":         {"type": "STRING", "description": "URL for go_to / new_tab action"},
                "query":       {"type": "STRING", "description": "Search query for search action"},
                "engine":      {"type": "STRING", "description": "Search engine: google | bing | duckduckgo | yandex (default: google)"},
                "selector":    {"type": "STRING", "description": "CSS selector for click/type"},
                "text":        {"type": "STRING", "description": "Text to click or type"},
                "description": {"type": "STRING", "description": "Element description for smart_click/smart_type"},
                "direction":   {"type": "STRING", "description": "up | down for scroll"},
                "amount":      {"type": "INTEGER", "description": "Scroll amount in pixels (default: 500)"},
                "key":         {"type": "STRING", "description": "Key name for press action (e.g. Enter, Escape, F5)"},
                "path":        {"type": "STRING", "description": "Save path for screenshot"},
                "incognito":   {"type": "BOOLEAN", "description": "Open in private/incognito mode"},
                "clear_first": {"type": "BOOLEAN", "description": "Clear field before typing (default: true)"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "file_controller",
        "description": "Manages files and folders: list, create, delete, move, copy, rename, read, write, find, disk usage.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "list | create_file | create_folder | delete | move | copy | rename | read | write | find | largest | disk_usage | organize_desktop | info"},
                "path":        {"type": "STRING", "description": "File/folder path or shortcut: desktop, downloads, documents, home"},
                "destination": {"type": "STRING", "description": "Destination path for move/copy"},
                "new_name":    {"type": "STRING", "description": "New name for rename"},
                "content":     {"type": "STRING", "description": "Content for create_file/write"},
                "name":        {"type": "STRING", "description": "File name to search for"},
                "extension":   {"type": "STRING", "description": "File extension to search (e.g. .pdf)"},
                "count":       {"type": "INTEGER", "description": "Number of results for largest"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "desktop_control",
        "description": "Controls the desktop: wallpaper, organize, clean, list, stats.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "wallpaper | wallpaper_url | organize | clean | list | stats | task"},
                "path":   {"type": "STRING", "description": "Image path for wallpaper"},
                "url":    {"type": "STRING", "description": "Image URL for wallpaper_url"},
                "mode":   {"type": "STRING", "description": "by_type or by_date for organize"},
                "task":   {"type": "STRING", "description": "Natural language desktop task"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "code_helper",
        "description": "Writes, edits, explains, runs, or builds code files.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "write | edit | explain | run | build | auto (default: auto)"},
                "description": {"type": "STRING", "description": "What the code should do or what change to make"},
                "language":    {"type": "STRING", "description": "Programming language (default: python)"},
                "output_path": {"type": "STRING", "description": "Where to save the file"},
                "file_path":   {"type": "STRING", "description": "Path to existing file for edit/explain/run/build"},
                "code":        {"type": "STRING", "description": "Raw code string for explain"},
                "args":        {"type": "STRING", "description": "CLI arguments for run/build"},
                "timeout":     {"type": "INTEGER", "description": "Execution timeout in seconds (default: 30)"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "dev_agent",
        "description": "Builds complete multi-file projects from scratch: plans, writes files, installs deps, opens VSCode, runs and fixes errors.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "description":  {"type": "STRING", "description": "What the project should do"},
                "language":     {"type": "STRING", "description": "Programming language (default: python)"},
                "project_name": {"type": "STRING", "description": "Optional project folder name"},
                "timeout":      {"type": "INTEGER", "description": "Run timeout in seconds (default: 30)"},
            },
            "required": ["description"]
        }
    },
    {
        "name": "computer_control",
        "description": "Direct computer control: type, click, hotkeys, scroll, move mouse, screenshots, find elements on screen.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "type | smart_type | click | double_click | right_click | hotkey | press | scroll | move | copy | paste | screenshot | wait | clear_field | focus_window | screen_find | screen_click | random_data | user_data"},
                "text":        {"type": "STRING", "description": "Text to type or paste"},
                "x":           {"type": "INTEGER", "description": "X coordinate"},
                "y":           {"type": "INTEGER", "description": "Y coordinate"},
                "keys":        {"type": "STRING", "description": "Key combination e.g. 'ctrl+c'"},
                "key":         {"type": "STRING", "description": "Single key e.g. 'enter'"},
                "direction":   {"type": "STRING", "description": "up | down | left | right"},
                "amount":      {"type": "INTEGER", "description": "Scroll amount (default: 3)"},
                "seconds":     {"type": "NUMBER",  "description": "Seconds to wait"},
                "title":       {"type": "STRING",  "description": "Window title for focus_window"},
                "description": {"type": "STRING",  "description": "Element description for screen_find/screen_click"},
                "type":        {"type": "STRING",  "description": "Data type for random_data"},
                "field":       {"type": "STRING",  "description": "Field for user_data: name|email|city"},
                "clear_first": {"type": "BOOLEAN", "description": "Clear field before typing (default: true)"},
                "path":        {"type": "STRING",  "description": "Save path for screenshot"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "game_updater",
        "description": (
            "THE ONLY tool for ANY Steam or Epic Games request. "
            "Use for: installing, downloading, updating games, listing installed games, "
            "checking download status, scheduling updates. "
            "ALWAYS call directly for any Steam/Epic/game request. "
            "NEVER use browser_control or web_search for Steam/Epic."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":    {"type": "STRING",  "description": "update | install | list | download_status | schedule | cancel_schedule | schedule_status (default: update)"},
                "platform":  {"type": "STRING",  "description": "steam | epic | both (default: both)"},
                "game_name": {"type": "STRING",  "description": "Game name (partial match supported)"},
                "app_id":    {"type": "STRING",  "description": "Steam AppID for install (optional)"},
                "hour":      {"type": "INTEGER", "description": "Hour for scheduled update 0-23 (default: 3)"},
                "minute":    {"type": "INTEGER", "description": "Minute for scheduled update 0-59 (default: 0)"},
                "shutdown_when_done": {"type": "BOOLEAN", "description": "Shut down PC when download finishes"},
            },
            "required": []
        }
    },
    {
        "name": "flight_finder",
        "description": "Searches Google Flights and speaks the best options.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "origin":      {"type": "STRING",  "description": "Departure city or airport code"},
                "destination": {"type": "STRING",  "description": "Arrival city or airport code"},
                "date":        {"type": "STRING",  "description": "Departure date (any format)"},
                "return_date": {"type": "STRING",  "description": "Return date for round trips"},
                "passengers":  {"type": "INTEGER", "description": "Number of passengers (default: 1)"},
                "cabin":       {"type": "STRING",  "description": "economy | premium | business | first"},
                "save":        {"type": "BOOLEAN", "description": "Save results to Notepad"},
            },
            "required": ["origin", "destination", "date"]
        }
    },
    {
        "name": "manage_monitor",
        "description": (
            "Add, remove, or list background monitoring topics. "
            "JARVIS checks these topics once a day and alerts the user when there is a new development. "
            "Use 'add' when the user says 'monitor X', 'track X', 'follow X'. "
            "Use 'remove' when the user says 'stop monitoring X'. "
            "Use 'list' when the user asks what is being monitored. "
            "Do NOT add crypto, financial, or trading topics."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type":        "STRING",
                    "description": "add | remove | list",
                },
                "topic": {
                    "type":        "STRING",
                    "description": "Topic to monitor or stop monitoring (e.g. 'space exploration', 'AI news')",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "shutdown_jarvis",
        "description": (
            "Shuts the assistant down completely: everything stops, the window closes, and the "
            "session is over. There is no undo. "
            "There is exactly ONE trigger, and it is a phrase, not an intention: the user says "
            "'cierra sesion' (Spanish) or 'close session' (English). Nothing else closes Lumina. "
            "Not 'gracias', not 'muchas gracias', not 'thank you', not 'adios', not 'bye', not "
            "'hasta luego', not 'good night', not 'apagate', not 'ya terminamos' — none of these, "
            "however final they sound. Someone who thanks you expects you to still be there. "
            "This is enforced in code as well: the transcript is checked for the phrase, and if it "
            "is not there the shutdown is refused no matter how sure you are. Calling it without "
            "the phrase wastes a turn and tells the user you tried to close on them. "
            "If they seem to want to finish, say the phrase they need rather than guessing."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
    "name": "file_processor",
    "description": (
        "Processes any file that the user has uploaded or dropped onto the interface. "
        "Use this when the user refers to an uploaded file and wants an action on it. "
        "Supports: images (describe/ocr/resize/compress/convert), "
        "PDFs (summarize/extract_text/to_word), "
        "Word docs & text files (summarize/fix/reformat/translate), "
        "CSV/Excel (analyze/stats/filter/sort/convert), "
        "JSON/XML (validate/format/analyze), "
        "code files (explain/review/fix/optimize/run/document/test), "
        "audio (transcribe/trim/convert/info), "
        "video (trim/extract_audio/extract_frame/compress/transcribe/info), "
        "archives (list/extract), "
        "presentations (summarize/extract_text). "
        "ALWAYS call this tool when a file has been uploaded and the user gives a command about it. "
        "If the user's command is ambiguous, pick the most logical action for that file type."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "file_path": {
                "type": "STRING",
                "description": "Full path to the uploaded file. Leave empty to use the currently uploaded file."
            },
            "action": {
                "type": "STRING",
                "description": (
                    "What to do with the file. Examples by type:\n"
                    "image: describe | ocr | resize | compress | convert | info\n"
                    "pdf: summarize | extract_text | to_word | info\n"
                    "docx/txt: summarize | fix | reformat | translate_hint | word_count | to_bullet\n"
                    "csv/excel: analyze | stats | filter | sort | convert | info\n"
                    "json: validate | format | analyze | to_csv\n"
                    "code: explain | review | fix | optimize | run | document | test\n"
                    "audio: transcribe | trim | convert | info\n"
                    "video: trim | extract_audio | extract_frame | compress | transcribe | info | convert\n"
                    "archive: list | extract\n"
                    "pptx: summarize | extract_text | analyze"
                )
            },
            "instruction": {
                "type": "STRING",
                "description": "Free-form instruction if action doesn't cover it. E.g. 'translate this to Turkish', 'find all email addresses'"
            },
            "format": {
                "type": "STRING",
                "description": "Target format for conversion. E.g. 'mp3', 'pdf', 'csv', 'png'"
            },
            "width":     {"type": "INTEGER", "description": "Target width for image resize"},
            "height":    {"type": "INTEGER", "description": "Target height for image resize"},
            "scale":     {"type": "NUMBER",  "description": "Scale factor for image resize (e.g. 0.5)"},
            "quality":   {"type": "INTEGER", "description": "Quality 1-100 for image/video compress"},
            "start":     {"type": "STRING",  "description": "Start time for trim: seconds or HH:MM:SS"},
            "end":       {"type": "STRING",  "description": "End time for trim: seconds or HH:MM:SS"},
            "timestamp": {"type": "STRING",  "description": "Timestamp for video frame extraction HH:MM:SS"},
            "column":    {"type": "STRING",  "description": "Column name for CSV filter/sort"},
            "value":     {"type": "STRING",  "description": "Filter value for CSV filter"},
            "condition": {"type": "STRING",  "description": "Filter condition: equals|contains|gt|lt"},
            "ascending": {"type": "BOOLEAN", "description": "Sort order for CSV sort (default: true)"},
            "save":      {"type": "BOOLEAN", "description": "Save result to file (default: true)"},
            "destination": {"type": "STRING", "description": "Output folder for archive extract"},
        },
        "required": []
    }
},
    {
        "name": "save_memory",
        "description": (
            "Save an important personal fact about the user to long-term memory. "
            "Call this silently whenever the user reveals something worth remembering: "
            "name, age, city, job, preferences, hobbies, relationships, projects, or future plans. "
            "Do NOT call for: weather, reminders, searches, or one-time commands. "
            "Do NOT announce that you are saving — just call it silently. "
            "Values must be in English regardless of the conversation language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {
                    "type": "STRING",
                    "description": (
                        "identity — name, age, birthday, city, job, language, nationality | "
                        "preferences — favorite food/color/music/film/game/sport, hobbies | "
                        "projects — active projects, goals, things being built | "
                        "relationships — friends, family, partner, colleagues | "
                        "wishes — future plans, things to buy, travel dreams | "
                        "notes — habits, schedule, anything else worth remembering"
                    )
                },
                "key":   {"type": "STRING", "description": "Short snake_case key (e.g. name, favorite_food, sister_name)"},
                "value": {"type": "STRING", "description": "Concise value in English (e.g. Fatih, pizza, older sister)"},
            },
            "required": ["category", "key", "value"]
        }
    },
    {
        "name": "recall_memory",
        "description": (
            "Look up a fact you have stored about the user but which is NOT in "
            "the memory block of your system prompt. "
            "The prompt lists the keys it did not have room for under "
            "'[ALSO REMEMBERED]' — if the user asks about anything named there, "
            "call this FIRST. "
            "Also call it before saying you do not know something personal, and "
            "when the user asks what you remember about them (leave query empty "
            "for everything). "
            "It searches two things at once: the facts you stored, and the record "
            "of everything the two of you have actually said to each other. So it "
            "is also the tool for 'what did we talk about yesterday', 'do you "
            "remember what we decided about the project', '¿te acuerdas de lo que "
            "hablamos de X?' — call it instead of saying you cannot remember "
            "previous conversations, because you can."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": (
                        "Keyword to search for — a name, a topic, a category, or "
                        "a subject the two of you discussed. "
                        "Search in the words the user themselves used: past "
                        "conversations are stored in the language they were held "
                        "in, so 'quantum' finds nothing in a conversation that "
                        "happened in Spanish, while 'cuantica' finds it. "
                        "One or two words search best; a whole sentence matches "
                        "nothing. Accents do not matter. "
                        "Leave empty for the most recent conversations."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "undo",
        "description": (
            "Reverse the last change YOU made to this computer — a file you "
            "moved, renamed, created or wrote, or a setting you changed such as "
            "volume, brightness, dark mode or WiFi. "
            "Call this whenever the user says undo, revert, take it back, put it "
            "back, cancel that, or tells you that you did the wrong thing, in ANY "
            "language. "
            "Use action='list' when they ask what can be undone. "
            "This only covers your own actions — it is not the Ctrl+Z of whatever "
            "application is on screen (that is computer_settings with action 'undo')."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "undo (default) — reverse the last change | list — show what can be undone",
                },
            },
            "required": [],
        },
    },
]

class _ReconnectSignal(Exception):
    """Raised inside the session TaskGroup to force a clean, voluntary reconnect
    (e.g. the user picked a new voice — the voice is fixed at connect time, so
    the session must be rebuilt).

    Carries `keep_context`: True for an ordinary rebuild, where the stored
    resumption handle is replayed and the conversation continues; False when the
    new session must genuinely start clean (see the voice-change note in
    _on_voice_change)."""

    def __init__(self, keep_context: bool = True):
        super().__init__()
        self.keep_context = keep_context


def _is_reconnect_signal(exc: BaseException) -> bool:
    """True if `exc` is a _ReconnectSignal, or a(n) (Base)ExceptionGroup that
    wraps one — TaskGroup bundles child exceptions into a group."""
    if isinstance(exc, _ReconnectSignal):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_reconnect_signal(sub) for sub in exc.exceptions)
    return False


def _flatten_error(exc: BaseException) -> str:
    """Every message in an exception tree, joined.

    The session runs inside a TaskGroup, so a failure arrives wrapped in a
    BaseExceptionGroup whose own str() is just "unhandled errors in a TaskGroup
    (1 sub-exception)" — the real reason sits in .exceptions. Classifying on
    str(e) therefore misses it, and every branch below that looks for a specific
    cause silently stops matching: a timeout stops being recognised as a network
    error, a rejected resumption handle stops being dropped. This flattens the
    tree so the classification sees what actually happened."""
    parts = [f"{type(exc).__name__}: {exc}"]
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            parts.append(_flatten_error(sub))
    cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
    if cause is not None and cause is not exc:
        parts.append(f"{type(cause).__name__}: {cause}")
    return " | ".join(parts)


def _keep_context_of(exc: BaseException) -> bool:
    """Read `keep_context` off a reconnect signal, unwrapping the group the
    TaskGroup put it in. Defaults to True: an unexpected shape must not silently
    wipe the conversation."""
    if isinstance(exc, _ReconnectSignal):
        return getattr(exc, "keep_context", True)
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            if _is_reconnect_signal(sub):
                return _keep_context_of(sub)
    return True


class JarvisLive:

    def __init__(self, ui: JarvisUI):
        self.ui             = ui
        self._asst_name     = "LUMINA"   # updated each session from config
        self.session              = None
        self.audio_in_queue       = None
        self.out_queue            = None
        self._loop                = None
        self._is_speaking         = False
        self._speaking_lock       = threading.Lock()
        self._phone_active        = False   # True while phone mic is streaming; pauses PC mic
        self._pending_vision       = None    # (img_bytes, mime_type, question, angle) to inject after tool response
        self._vision_cam_active    = False   # True if camera was opened for vision → auto-close after response
        self._vision_close_pending = False   # True after vision injected; next turn_complete closes camera
        self._vision_last_time     = 0.0     # monotonic time of last screen_process call (cooldown guard)
        self._vision_busy          = False   # True while a vision capture/inject cycle is in flight
        self._interrupted          = False   # True while draining audio after user interrupt
        self.ui.on_text_command   = self._on_text_command
        self.ui.on_remote_clicked = self._make_remote_key
        self.ui.on_interrupt      = self.interrupt
        self.ui.on_voice_change   = self._on_voice_change     # voice picker → rebuild session
        self.ui.on_audio_device_change = self._on_audio_device_change
        self._reconnect_event: asyncio.Event | None = None
        self._reconnect_keep = True   # False → next rebuild drops the resumption handle
        # Transcripts arrive in fragments, so the wake phrase is seen on several
        # consecutive updates. This keeps one spoken phrase to one wake.
        self._last_wake = 0.0
        # When the microphone last handed over a block of audio. Watched by
        # _listen_audio, which cannot otherwise tell a silent room from a
        # device that has stopped calling back.
        self._last_mic_block = 0.0

        # ── Session resumption ─────────────────────────────────────────
        # The server issues a resumption handle every few seconds and reissues
        # it as the conversation moves on. Before this, session_resumption was
        # switched ON in the config and the update was never read, so the handle
        # was thrown away and EVERY reconnect — a dropped packet, a voice change,
        # switching microphone — started an empty session. "Unlimited sessions"
        # leaked through exactly this hole.
        #
        # Deliberately in RAM only, never written to disk. Persisting it would
        # make a fresh launch continue yesterday's conversation, which sounds
        # appealing but breaks the session-summary flow: _save_session_summary
        # runs at shutdown and the morning briefing pops it the next day. A
        # conversation that never ends never produces a summary, and the
        # "yesterday we talked about…" line silently disappears.
        self._resume_handle: str | None = None
        self._turn_done_event: asyncio.Event | None = None
        self._dashboard     = None
        self._briefing_sent    = False          # morning briefing fires once per process
        self._sys_monitor      = SystemMonitor()  # persistent cooldown state
        self._proactive        = ProactiveEngine()
        self._last_user_speech = time.monotonic()  # updated on every user utterance
        self._last_voice_end   = 0.0               # local VAD end for latency diagnostics
        self._session_log: list[str] = []          # conversation turns for end-of-session summary
        # When the close-session phrase was last actually heard. shutdown_jarvis
        # refuses unless this is recent, so no amount of model confidence can
        # end the session on its own.
        self._close_heard_at = 0.0
        self._session_started = time.monotonic()
        # Blocks of real speech forwarded since the session last transcribed
        # anything. The difference between "nobody is talking" and "they are
        # talking and it is not arriving" is invisible without this.
        self._voice_blocks_unheard = 0
        self._last_deaf_rebuild = 0.0
        self._tool_running = 0
        # Transcriptions this session has produced since it connected. The
        # failure the watchdog below exists for is a session born deaf: it
        # connects, answers typed text, and never transcribes one word of
        # audio for its whole life. A session that has transcribed even once
        # is listening, and must never be torn down on suspicion.
        self._heard_this_session = 0

        # Tracked apart because models support them apart: gemini-3.1-flash-live
        # takes proactive audio and refuses affective dialog. Bundled together,
        # one refusal switched off both — and proactive audio is the one that
        # keeps the assistant quiet when the room is talking about something
        # else, so losing it as collateral is the wrong trade.
        # Each is dropped only if the server actually objects to it.
        self._affective_live = True
        self._proactive_live = True
        _core_names = {t["name"] for t in TOOL_DECLARATIONS}
        self._plugin_registry = discover_plugins(
            plugins_dir=Path(__file__).resolve().parent / "plugins",
            core_tool_names=_core_names,
            logger=lambda msg: (print(f"[Plugins] {msg}"), self.ui.write_log(f"SYS: {msg}")),
        )
        self.ui.get_plugins = self._plugin_registry.list_for_ui
        self.ui.request_say = self.plugin_say   # plugins: mid-task speech channel

    def plugin_say(self, instruction: str) -> None:
        """
        Thread-safe speech channel for plugins: lets a plugin ask JARVIS to
        say something short WHILE its run() is still executing (plugins block
        their executor thread, so they can't speak through the tool response
        until they finish). The instruction is injected into the Live session
        exactly like a proactive check-in; Gemini phrases it naturally in the
        user's language. Silently a no-op when no session is connected.
        """
        loop = getattr(self, "_loop", None)
        if not loop or not self.session:
            return

        async def _say():
            try:
                await self.session.send_client_content(
                    turns={"parts": [{"text": instruction}]},
                    turn_complete=True,
                )
            except Exception as e:
                print(f"[PluginSay] {e}")

        try:
            asyncio.run_coroutine_threadsafe(_say(), loop)
        except Exception as e:
            print(f"[PluginSay] {e}")

    def request_reconnect(self, keep_context: bool = True, reason: str = ""):
        """Thread-safe: ask the run loop to tear down and rebuild the Live
        session. Called from the Qt thread. No-op until the async loop and
        reconnect event exist.

        `keep_context=False` drops the resumption handle so the new session
        starts empty — only for changes the server cannot apply to a resumed
        session."""
        loop = getattr(self, "_loop", None)
        ev   = self._reconnect_event
        self._reconnect_keep   = keep_context
        self._reconnect_reason = reason
        if loop and ev is not None:
            loop.call_soon_threadsafe(ev.set)

    def wake(self, heard: str = "") -> None:
        """Force the assistant back into the conversation.

        Two different kinds of stuck, so two different remedies. When the
        session is alive the model simply chose not to answer — proactive audio
        judged the speech was not aimed at it — and an explicit client turn
        overrides that judgement. When there is no session at all, nothing can
        be sent, so the run loop is asked to rebuild one; the conversation is
        kept, because waking up should not cost the user their context."""
        self._last_wake = time.monotonic()
        self.ui.write_log("SYS: Wake phrase heard — waking up.")
        print(f"[JARVIS] ⏰ Wake phrase: {heard[:60]!r}")

        # Only cut in when there is actually something to cut off. interrupt()
        # raises _interrupted, and the recv loop drops the next turn_complete it
        # sees to discard the abandoned reply — so interrupting while silent
        # would swallow the very answer this wake is asking for.
        with self._speaking_lock:
            speaking = self._is_speaking
        if speaking:
            try:
                self.interrupt()
            except Exception:
                pass

        loop = getattr(self, "_loop", None)
        if not self.session or not loop:
            self.request_reconnect(keep_context=True, reason="wake phrase")
            return

        try:
            asyncio.run_coroutine_threadsafe(
                self.session.send_client_content(
                    turns={"parts": [{"text": (
                        "The user just said your wake phrase because you were not "
                        "responding. Greet them in one short sentence, in their "
                        "language, and ask what they need."
                    )}]},
                    turn_complete=True,
                ),
                loop,
            )
        except Exception as e:
            print(f"[JARVIS] Wake failed, rebuilding the session: {e}")
            self.request_reconnect(keep_context=True, reason="wake phrase")

    def _on_voice_change(self):
        """Voice picker applied.

        The voice is baked into the session at connect time, so a rebuild is
        required. It is rebuilt WITHOUT the resumption handle on purpose:
        resuming restores the server's own session state, and the safe reading
        is that it restores the voice with it — which would make the picker
        appear to do nothing. Losing context here is acceptable because changing
        voice is a deliberate, rare act; losing it on a dropped packet was not."""
        self.request_reconnect(keep_context=False, reason="new voice")

    def _on_audio_device_change(self):
        """Microphone or speaker changed. Both streams are opened inside the
        session TaskGroup, so they can only be re-opened by rebuilding it —
        but the conversation is kept, which is the whole reason resumption
        landed before this feature did."""
        self.request_reconnect(keep_context=True, reason="audio device")

    async def _watch_reconnect(self):
        """Session-scoped task: when a voluntary reconnect is requested, raise a
        signal that unwinds the TaskGroup so the run loop rebuilds the session."""
        assert self._reconnect_event is not None
        await self._reconnect_event.wait()
        self._reconnect_event.clear()
        keep   = self._reconnect_keep
        reason = getattr(self, "_reconnect_reason", "") or "settings"
        self.ui.write_log(
            f"SYS: Applying {reason} — reconnecting"
            + ("..." if keep else " (starting a fresh conversation)...")
        )
        raise _ReconnectSignal(keep_context=keep)

    def _make_remote_key(self):
        """Called from Qt main thread when user presses Remote Control."""
        if self._dashboard is None:
            self.ui.write_log(
                "SYS: Dashboard unavailable. "
                "Run: pip install fastapi \"uvicorn[standard]\" cryptography"
            )
            return None
        key    = self._dashboard.new_key()
        url    = self._dashboard.get_url()
        manual = self._dashboard.get_manual_url()
        return url, key, f"{url}/auto-login?key={key}", manual

    def _on_text_command(self, text: str):
        # Before the session check, deliberately: typing the wake phrase is the
        # last resort when the session is gone, and the old early return made
        # that exact case do nothing at all.
        if _is_wake_phrase(text, self._asst_name):
            self.wake(text)
            return

        if not self._loop or not self.session:
            return

        if _is_close_phrase(text):
            self._close_heard_at = time.monotonic()
            print("[JARVIS] 🔒 Close-session phrase typed", flush=True)

        # Recorded here rather than at turn_complete. Typed text never reaches
        # input_transcription, and holding it until the next completed turn
        # pairs it with whatever the model happened to be saying already — the
        # startup briefing, most visibly. A row of its own is simply true.
        log_turn(BASE_DIR, text, "")

        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def set_speaking(self, value: bool):
        with self._speaking_lock:
            self._is_speaking = value
        if value:
            self.ui.set_state("SPEAKING")
        elif not self.ui.muted:
            self.ui.set_state("LISTENING")

    def interrupt(self) -> None:
        """Stop JARVIS mid-speech: drain queued audio and open mic immediately."""
        self._interrupted = True
        q = self.audio_in_queue
        if q:
            drained = 0
            while True:
                try:
                    q.get_nowait()
                    drained += 1
                except Exception:
                    break
            if drained:
                print(f"[JARVIS] ✋ Interrupted — {drained} audio chunks discarded")
        self.set_speaking(False)
        if self._turn_done_event:
            self._turn_done_event.clear()
        self.ui.write_log("SYS: Interrupted — listening...")

    def speak(self, text: str):
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def speak_error(self, tool_name: str, error: str):
        short = str(error)[:120]
        self.ui.write_log(f"ERR: {tool_name} — {short}")
        self.speak(f"Sir, {tool_name} encountered an error. {short}")

    def _build_config(self) -> types.LiveConnectConfig:
        from datetime import datetime

        # Load customization from config
        try:
            _cfg = json.loads(open(API_CONFIG_PATH, encoding="utf-8").read())
            self._asst_name = (_cfg.get("assistant_name") or "LUMINA").strip()
            _user_name = (_cfg.get("user_name") or "").strip()
        except Exception:
            self._asst_name = "LUMINA"
            _user_name = ""

        memory     = load_memory()
        mem_str    = format_memory_for_prompt(memory)
        sys_prompt = _load_system_prompt()

        now      = datetime.now()
        time_str = now.strftime("%A, %B %d, %Y — %I:%M %p")
        time_ctx = (
            f"[CURRENT DATE & TIME]\n"
            f"Right now it is: {time_str}\n"
            f"Use this to calculate exact times for reminders.\n\n"
        )

        # Identity injection — overrides any hardcoded name in prompt.txt
        _addr = (f"ADDRESS: Always call the user '{_user_name}'."
                 if _user_name
                 else "ADDRESS: Address the user with the ordinary respectful form "
                      "for a superior in the language you are currently speaking — "
                      "\"sir\" in English, its everyday equivalent in any other "
                      "language. Never an archaic or aristocratic form, and never "
                      "the form from a different language than the one you are "
                      "speaking in this sentence.")
        identity_ctx = (
            f"[IDENTITY]\n"
            f"Your name is {self._asst_name}. "
            f"Always refer to yourself as {self._asst_name}.\n"
            f"{_addr}\n\n"
        )

        parts = [time_ctx, identity_ctx]
        if mem_str:
            parts.append(mem_str)
        parts.append(sys_prompt)

        cfg = dict(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            # End the user's turn promptly instead of relying on the service's
            # more conservative defaults. Five hundred milliseconds preserves
            # natural clause pauses while removing the long wait after speech.
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=False,
                    start_of_speech_sensitivity=(
                        types.StartSensitivity.START_SENSITIVITY_HIGH
                    ),
                    end_of_speech_sensitivity=(
                        types.EndSensitivity.END_SENSITIVITY_HIGH
                    ),
                    prefix_padding_ms=80,
                    silence_duration_ms=500,
                ),
                activity_handling=(
                    types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS
                ),
                turn_coverage=types.TurnCoverage.TURN_INCLUDES_ONLY_ACTIVITY,
            ),
            system_instruction="\n".join(parts),
            tools=[{"function_declarations": TOOL_DECLARATIONS + self._plugin_registry.get_tool_declarations()}],
            # Hand back the handle captured from the last session_resumption
            # update. `handle=None` is exactly the old behaviour (ask for
            # handles, start fresh), so the first connect of a run is unchanged.
            session_resumption=types.SessionResumptionConfig(
                handle=self._resume_handle
            ),
            # Sliding-window compression: session never dies from a full context
            # window — JARVIS can stay in one conversation for hours
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow(),
            ),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=get_voice()
                    )
                )
            ),
        )
        # Affective dialog: the assistant hears tone and emotion and adapts its
        # own voice. Asked for separately, because not every Live model has it.
        if self._affective_live:
            cfg["enable_affective_dialog"] = True
        # Proactive audio: it stays silent when the speech was not addressed to
        # it — background chatter, or someone else in the room being spoken to.
        if self._proactive_live:
            cfg["proactivity"] = types.ProactivityConfig(proactive_audio=True)
        return types.LiveConnectConfig(**cfg)

    @property
    def _enhanced_live(self) -> bool:
        """True while either enhanced feature is still in play — both live on
        the v1alpha endpoint, so it decides which API version to connect to."""
        return self._affective_live or self._proactive_live

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        # A running tool is a legitimate reason for the session to transcribe
        # nothing: the model is inside the call and not listening. The deafness
        # watchdog cannot tell that apart from a session that has stopped
        # hearing, and it tore one down in the middle of a Copilot query that
        # had another twenty seconds to run — losing the answer. So it is told.
        self._tool_running += 1
        try:
            return await self._dispatch_tool(fc)
        finally:
            self._tool_running -= 1
            # Speech from while the tool was busy is not evidence of anything.
            self._voice_blocks_unheard = 0
            self._last_user_speech = time.monotonic()

    async def _dispatch_tool(self, fc) -> types.FunctionResponse:
        name = fc.name
        args = dict(fc.args or {})

        print(f"[JARVIS] 🔧 {name}  {args}")
        self.ui.set_state("THINKING")

        if name == "save_memory":
            category = args.get("category", "notes")
            key      = args.get("key", "")
            value    = args.get("value", "")
            if key and value:
                update_memory({category: {key: {"value": value}}})
                print(f"[Memory] 💾 save_memory: {category}/{key} = {value}")
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": "ok", "silent": True}
            )

        loop   = asyncio.get_event_loop()
        result = "Done."

        try:
            if name == "recall_memory":
                # Two stores, one question. The local file holds the facts
                # save_memory distilled out of conversations; Supabase holds the
                # conversations themselves, which is where "what did we decide
                # about X?" actually lives — a question the distilled facts can
                # never answer.
                #
                # The file scan stays on this thread on purpose: it is a
                # dictionary scan over a few hundred short strings, and a thread
                # hop would cost more than the work. The network read does not,
                # because it can take seconds and this loop also carries audio.
                query = args.get("query", "")
                facts = search_memory(query, limit=8)
                history = await loop.run_in_executor(
                    None, lambda: search_history(BASE_DIR, query, limit=12)
                )
                result = f"{facts}\n\n{history}"

            elif name == "undo":
                if str(args.get("action", "")).lower().strip() == "list":
                    items = undo_stack.history()
                    result = ("Things I can undo, most recent first:\n"
                              + "\n".join(f"{i+1}. {t}" for i, t in enumerate(items))
                              ) if items else "I have not changed anything I can undo yet."
                else:
                    result = await loop.run_in_executor(None, undo_stack.undo_last)

            elif name == "open_app":
                r = await loop.run_in_executor(None, lambda: open_app(parameters=args, response=None, player=self.ui))
                result = r or f"Opened {args.get('app_name')}."

            elif name == "weather_report":
                r = await loop.run_in_executor(None, lambda: weather_action(parameters=args, player=self.ui))
                result = r or "Weather delivered."

            elif name == "browser_control":
                r = await loop.run_in_executor(None, lambda: browser_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "file_controller":
                r = await loop.run_in_executor(None, lambda: file_controller(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "send_message":
                r = await loop.run_in_executor(None, lambda: send_message(parameters=args, response=None, player=self.ui, session_memory=None))
                result = r or f"Message sent to {args.get('receiver')}."

            elif name == "reminder":
                r = await loop.run_in_executor(None, lambda: reminder(parameters=args, response=None, player=self.ui))
                result = r or "Reminder set."

            elif name == "youtube_video":
                r = await loop.run_in_executor(None, lambda: youtube_video(parameters=args, response=None, player=self.ui))
                result = r or "Done."

            elif name == "screen_process":
                import time as _t_mod
                _now = _t_mod.monotonic()
                _cooldown = 4.0  # seconds — covers echo window after speaking ends
                if self._vision_busy or (_now - self._vision_last_time) < _cooldown:
                    _wait = max(0, _cooldown - (_now - self._vision_last_time))
                    print(f"[Vision] ⏳ Cooldown active ({_wait:.1f}s remaining) — ignoring duplicate call")
                    result = "Vision is still processing the previous request. I will not call this again."
                else:
                    self._vision_busy      = True
                    self._vision_last_time = _now
                    angle     = args.get("angle", "screen").lower()
                    user_text = args.get("text", "What do you see?")
                    if angle == "camera":
                        img_b, mime_t = await loop.run_in_executor(None, _capture_camera)
                        self.ui.start_camera_stream()
                        self._vision_cam_active = True
                        print(f"[Vision] 📷 Camera: {len(img_b):,} bytes")
                        _stall = "camera"
                    else:
                        img_b, mime_t = await loop.run_in_executor(None, _capture_screen)
                        print(f"[Vision] 🖥️  Screen: {len(img_b):,} bytes")
                        _stall = "screen"
                    self._pending_vision = (img_b, mime_t, user_text, angle)
                    result = (
                        f"[VISION_ACTIVE] {_stall.capitalize()} captured. "
                        f"Immediately say ONE short natural sentence in the user's own language, "
                        f"telling them you are looking at their {_stall} right now. "
                        f"Do NOT describe or guess content — the actual image arrives in the NEXT message."
                    )

            elif name == "close_camera":
                self.ui.stop_camera_stream()
                result = "Camera closed."

            elif name == "computer_settings":
                r = await loop.run_in_executor(None, lambda: computer_settings(parameters=args, response=None, player=self.ui))
                result = r or "Done."

            elif name == "desktop_control":
                r = await loop.run_in_executor(None, lambda: desktop_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "code_helper":
                r = await loop.run_in_executor(None, lambda: code_helper(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "dev_agent":
                r = await loop.run_in_executor(None, lambda: dev_agent(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "web_search":
                r = await loop.run_in_executor(None, lambda: web_search_action(parameters=args, player=self.ui))
                result = r or "Done."
                # Mirror results to the on-screen content panel
                _mode = args.get("mode", "search")
                if r and not r.startswith("No results") and not r.startswith("Search failed"):
                    _query = args.get("query") or ", ".join(args.get("items", []))
                    _label = f"{_mode.upper()} — {_query[:38]}" if _query else _mode.upper()
                    self.ui.show_content(_label, r)
            elif name == "file_processor":
                if not args.get("file_path") and self.ui.current_file:
                    args["file_path"] = self.ui.current_file
                r = await loop.run_in_executor(
                    None,
                    lambda: file_processor(parameters=args, player=self.ui, speak=self.speak)
                )
                result = r or "Done."

            elif name == "computer_control":
                r = await loop.run_in_executor(None, lambda: computer_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "game_updater":
                r = await loop.run_in_executor(None, lambda: game_updater(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "flight_finder":
                r = await loop.run_in_executor(None, lambda: flight_finder(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "system_status":
                r = await loop.run_in_executor(None, get_system_status)
                result = str(r)

            elif name == "manage_monitor":
                action = args.get("action", "").lower().strip()
                topic  = args.get("topic", "").strip()
                if action == "add" and topic:
                    result = await asyncio.to_thread(add_monitor, topic)
                elif action == "remove" and topic:
                    result = await asyncio.to_thread(remove_monitor, topic)
                elif action == "list":
                    topics = await asyncio.to_thread(list_monitors)
                    result = ("Monitoring: " + ", ".join(topics)) if topics else "No topics are being monitored."
                else:
                    result = "Specify action (add/remove/list) and a topic."

            elif name == "shutdown_jarvis":
                # The model asks; the transcript decides. This tool used to fire
                # on "muchas gracias" and end a working session, and a shutdown
                # cannot be taken back — so nothing closes unless the user
                # actually said the phrase, checked locally on what was heard.
                _since = time.monotonic() - self._close_heard_at
                if self._close_heard_at <= 0 or _since > _CLOSE_PHRASE_SECONDS:
                    print("[JARVIS] 🔒 Shutdown refused — close phrase not heard", flush=True)
                    result = (
                        "I am not closing. Shutting down ends the session and cannot be undone, "
                        "so I only do it when the user says 'cierra sesión' or 'close session'. "
                        "Tell them that is the phrase, and carry on."
                    )
                    if not self.ui.muted:
                        self.ui.set_state("LISTENING")
                    print(f"[JARVIS] 📤 {name} → {str(result)[:80]}")
                    return types.FunctionResponse(id=fc.id, name=name, response={"result": result})

                self.ui.write_log("SYS: Shutdown requested.")
                async def _do_shutdown():
                    await self._save_session_summary()
                    if self.session:
                        try:
                            await self.session.send_client_content(
                                turns={"parts": [{"text": "Say a brief natural goodbye to the user."}]},
                                turn_complete=True,
                            )
                        except Exception:
                            pass
                    await asyncio.sleep(1.5)
                    # _exit skips atexit handlers, so the last turns of the
                    # conversation would never leave the queue.
                    flush_conversation_log()
                    import os as _os
                    _os._exit(0)
                asyncio.create_task(_do_shutdown())

            else:
                if self._plugin_registry.has(name):
                    r = await loop.run_in_executor(
                        None,
                        lambda: self._plugin_registry.run(name, args, player=self.ui, session_memory=None)
                    )
                    result = r or "Done."
                else:
                    result = f"Unknown tool: {name}"

        except Exception as e:
            result = f"Tool '{name}' failed: {e}"
            traceback.print_exc()
            self.speak_error(name, e)

        if not self.ui.muted:
            self.ui.set_state("LISTENING")

        print(f"[JARVIS] 📤 {name} → {str(result)[:80]}")
        return types.FunctionResponse(
            id=fc.id, name=name,
            response={"result": result}
        )

    async def _send_realtime(self):
        while True:
            msg = await self.out_queue.get()
            if msg.get("audio_stream_end"):
                await self.session.send_realtime_input(audio_stream_end=True)
            else:
                # The 2.5 native-audio configuration with proactive and
                # affective audio requires the legacy media transport. The
                # newer audio= field is rejected when those features are on.
                await self.session.send_realtime_input(media=msg)

    async def _listen_audio(self):
        print("[JARVIS] 🎤 Mic started")
        loop = asyncio.get_event_loop()
        local_vad = _LocalVoiceActivityDetector()

        def enqueue_realtime(message: dict) -> None:
            """Enqueue from the audio thread without allowing callback errors."""
            try:
                self.out_queue.put_nowait(message)
            except asyncio.QueueFull:
                # A delayed stream end is preferable to losing it completely.
                # Normal audio remains best effort because blocking PortAudio's
                # callback would cause larger gaps in the captured stream.
                if message.get("audio_stream_end"):
                    asyncio.create_task(self.out_queue.put(message))
                else:
                    _diag("microphone queue full; dropping one audio block")

        def callback(indata, frames, time_info, status):
            # Stamped on every block, before any gate: this is proof the device
            # is still delivering, which is a different question from whether we
            # are currently forwarding what it delivers.
            self._last_mic_block = time.monotonic()
            with self._speaking_lock:
                jarvis_speaking = self._is_speaking
            if not jarvis_speaking and not self.ui.muted and not self._phone_active:
                data = indata.tobytes()
                level = _pcm_level(indata)
                loop.call_soon_threadsafe(enqueue_realtime, {
                    "data": data,
                    "mime_type": "audio/pcm;rate=16000",
                })

                vad_event = local_vad.process(level)
                if vad_event == "start":
                    self._last_voice_end = 0.0
                    self.ui.set_live_transcript("You: Listening...")
                    _diag("local VAD: speech started")
                elif vad_event == "end":
                    ended_at = time.monotonic()
                    self._last_user_speech = ended_at
                    self._last_voice_end = ended_at
                    loop.call_soon_threadsafe(
                        enqueue_realtime, {"audio_stream_end": True}
                    )
                    _diag("local VAD: speech ended; flushing audio stream")

                # Counted here rather than at the top of the callback: only
                # audio that was actually sent can be evidence that sending is
                # not working.
                try:
                    if level > _DEAF_VOICE_LEVEL:
                        self._voice_blocks_unheard += 1
                except Exception:
                    pass
                # Feed the live mic level to the HUD so the waveform reacts to
                # the user's actual voice while listening. Purely cosmetic — any
                # failure here must never disturb the mic.
                try:
                    self.ui.set_audio_level(level)
                except Exception:
                    pass
            else:
                local_vad.reset()

        try:
            def _open_mic(dev):
                return sd.InputStream(
                    samplerate=SEND_SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="int16",
                    blocksize=CHUNK_SIZE,
                    device=dev,
                    callback=callback,
                )

            # Which microphone. resolve() returns None for "system default" and
            # for a saved device that is no longer present — so a headset
            # unplugged since the last run falls back to the built-in mic
            # instead of raising on startup and taking the session with it.
            _mic_name = get_input_device()
            _mic_dev  = audio_devices.resolve(_mic_name, "input")
            if _mic_dev is not None:
                print(f"[JARVIS] 🎤 Input device: {_mic_name}")
            try:
                _mic_stream = _open_mic(_mic_dev)
            except Exception as _e:
                # A device the picker listed but the driver will not open right
                # now — exclusive mode, a webcam already in use, a virtual mic
                # whose source went away. Chosen hardware failing must never
                # mean the assistant cannot hear at all.
                if _mic_dev is None:
                    raise
                print(f"[JARVIS] ⚠️  Mic '{_mic_name}' failed: {_e} — using default")
                self.ui.write_log(
                    f"SYS: Microphone '{_mic_name}' unavailable — using system default."
                )
                _mic_stream = _open_mic(None)

            # Not a `with`: the block below may replace the stream, and a
            # context manager would still close the object it was entered with,
            # leaving the replacement to leak and the dead one closed twice.
            _mic_stream.start()
            try:
                print("[JARVIS] 🎤 Mic stream open")
                self._last_mic_block = time.monotonic()

                # A microphone can stop delivering without anything raising:
                # sounddevice hands audio to a callback, and when Windows
                # rebuilds its audio graph — a device added or removed, a driver
                # reset, power management parking the endpoint — the callback
                # simply stops being called. The stream object stays open, this
                # loop keeps sleeping, and the HUD keeps saying MICROPHONE
                # ACTIVE while nothing is heard again until the app is
                # restarted. Silence is not proof of a working microphone, so
                # this checks that blocks are still arriving and reopens the
                # device when they are not.
                while True:
                    await asyncio.sleep(0.5)

                    if self.ui.muted:
                        # Muting stops nothing at the device, but there is no
                        # sense reopening hardware the user has switched off.
                        self._last_mic_block = time.monotonic()
                        continue

                    # A second, different deafness. The check below reopens a
                    # device that stopped delivering. This one is for the case
                    # that keeps happening instead: the device delivers, the
                    # HUD says LISTENING, the level meter moves — and the
                    # session transcribes nothing, because the connection is
                    # alive enough to answer typed text and no longer hears
                    # audio. Nothing raises, so nothing recovers, and the user
                    # is left talking to something that looks like it works.
                    # Rebuilding the session is the only cure; the context is
                    # kept so the conversation survives it.
                    if self._tool_running:
                        # Nothing said while a tool runs counts towards deafness.
                        self._voice_blocks_unheard = 0
                        self._last_user_speech = time.monotonic()

                    _spoke_seconds = self._voice_blocks_unheard * CHUNK_SIZE / SEND_SAMPLE_RATE
                    if (
                        self._heard_this_session == 0
                        and _spoke_seconds > _DEAF_VOICE_SECONDS
                        and (time.monotonic() - self._last_user_speech) > _DEAF_SILENCE_SECONDS
                        and (time.monotonic() - self._last_deaf_rebuild) > _DEAF_COOLDOWN
                    ):
                        self._voice_blocks_unheard = 0
                        self._last_deaf_rebuild = time.monotonic()
                        # Both clocks restart, not just the counter. Leaving
                        # this one stale is exactly what caused the loop.
                        self._last_user_speech = time.monotonic()
                        print(f"[JARVIS] 🙉 {_spoke_seconds:.0f}s of speech sent and nothing "
                              f"transcribed — rebuilding the session", flush=True)
                        self.ui.write_log("SYS: I stopped hearing you — reconnecting.")
                        self.request_reconnect(keep_context=True,
                                               reason="microphone not reaching the model")
                        continue

                    silent_for = time.monotonic() - self._last_mic_block
                    if silent_for < _MIC_STALL_SECONDS:
                        continue

                    print(f"[JARVIS] 🎙️  No microphone input for {silent_for:.0f}s "
                          f"— reopening the device", flush=True)
                    self.ui.write_log("SYS: Microphone stopped responding — reopening it.")
                    try:
                        _mic_stream.stop()
                        _mic_stream.close()
                    except Exception:
                        pass
                    try:
                        _mic_stream = _open_mic(_mic_dev)
                        _mic_stream.start()
                    except Exception as _e:
                        # The saved device may be the thing that went away.
                        # The system default is better than staying deaf.
                        print(f"[JARVIS] ⚠️  Reopening '{_mic_name}' failed: {_e} — using default")
                        _mic_stream = _open_mic(None)
                        _mic_stream.start()
                    self._last_mic_block = time.monotonic()
                    print("[JARVIS] 🎤 Mic stream reopened", flush=True)
                    self.ui.write_log("SYS: Microphone back online.")
            finally:
                try:
                    _mic_stream.stop()
                    _mic_stream.close()
                except Exception:
                    pass
        except Exception as e:
            print(f"[JARVIS] ❌ Mic: {e}")
            raise

    async def _receive_audio(self):
        print("[JARVIS] 👂 Recv started")
        out_buf = _TranscriptAccumulator()
        in_buf = _TranscriptAccumulator()

        try:
            while True:
                async for response in self.session.receive():

                    # ── Session resumption ───────────────────────────────────
                    # The server sends this periodically. `resumable` goes false
                    # while a turn is mid-flight — replaying a handle from that
                    # moment is what the flag exists to prevent — so only
                    # resumable handles are kept. This is three lines and it is
                    # the entire fix for "every reconnect forgets everything".
                    _sru = getattr(response, "session_resumption_update", None)
                    if _sru is not None:
                        if getattr(_sru, "resumable", False) and getattr(_sru, "new_handle", None):
                            if self._resume_handle is None:
                                print("[JARVIS] 🔗 Session resumption armed")
                            self._resume_handle = _sru.new_handle

                    if response.data:
                        if self._interrupted:
                            pass  # discard: interrupted
                        else:
                            if self._turn_done_event and self._turn_done_event.is_set():
                                self._turn_done_event.clear()
                            # Split into ~50 ms chunks so interrupt() stops audio within 50 ms
                            # (24000 Hz × 2 bytes/sample × 0.05 s = 2400 bytes per slice)
                            _audio_data = response.data
                            _SLICE = 2400
                            for _i in range(0, len(_audio_data), _SLICE):
                                self.audio_in_queue.put_nowait(_audio_data[_i : _i + _SLICE])

                    if response.server_content:
                        sc = response.server_content

                        if sc.output_transcription and sc.output_transcription.text:
                            was_empty = not out_buf.text
                            changed = out_buf.add_final(sc.output_transcription.text)
                            if changed:
                                # First fragment of this turn: the moment the
                                # assistant actually starts answering. Paired
                                # with the 🎧 lines above it gives the real
                                # speech-to-reply latency, instead of guessing
                                # from when the log finished drawing.
                                if was_empty:
                                    # How long the user actually waited, and how
                                    # much audio is still queued behind this
                                    # reply. "It takes a while after talking for
                                    # a bit" is a claim about a number that was
                                    # never printed: the console had no clock,
                                    # so a session that got slower over an hour
                                    # looked exactly like one that did not.
                                    _backlog = self.audio_in_queue.qsize() if self.audio_in_queue else 0
                                    _held = f", {_backlog} chunks still queued" if _backlog else ""
                                    _age = (time.monotonic() - self._session_started) / 60.0

                                    # Only a turn the user actually spoke in has
                                    # a wait worth reporting. _last_user_speech
                                    # is stamped at startup, so on a briefing —
                                    # which nobody asked for — it measures time
                                    # since the app opened and reads as a
                                    # twenty-second delay that never happened.
                                    if in_buf.text:
                                        speech_end = self._last_voice_end or self._last_user_speech
                                        _lag = time.monotonic() - speech_end
                                        _when = f"{_lag:.1f}s after you stopped"
                                    else:
                                        _when = "unprompted"

                                    print(f"[JARVIS] 💬 replying... ({_when}, "
                                          f"{_age:.0f} min into the session"
                                          f"{_held})", flush=True)

                        interim = getattr(sc, "interim_input_transcription", None)
                        if interim and interim.text and in_buf.set_interim(interim.text):
                            self._last_user_speech = time.monotonic()
                            self._voice_blocks_unheard = 0
                            self._heard_this_session += 1
                            preview = in_buf.text
                            if preview:
                                self.ui.set_live_transcript(f"You: {preview}")
                                _diag(f"interim transcript={preview!r}")

                        if sc.input_transcription and sc.input_transcription.text:
                            raw_text = sc.input_transcription.text
                            txt = _normalise_transcript_spacing(raw_text)
                            if txt and in_buf.add_final(raw_text):
                                self._last_user_speech = time.monotonic()
                                self._voice_blocks_unheard = 0
                                self._heard_this_session += 1

                                # Also on the console, fragment by fragment.
                                # "Heard:" below prints at turn_complete, which
                                # is after the assistant has already answered —
                                # useless for telling whether speech is arriving
                                # while it is being spoken.
                                print(f"[JARVIS] 🎧 {txt}", flush=True)

                                if _is_close_phrase(in_buf.text):
                                    self._close_heard_at = time.monotonic()
                                    print("[JARVIS] 🔒 Close-session phrase heard", flush=True)

                                # Show the words as they are recognised. The
                                # finished line is still written at
                                # turn_complete; until then this is the only
                                # sign the microphone is being heard at all,
                                # and its absence is indistinguishable from the
                                # assistant ignoring the user.
                                try:
                                    self.ui.set_live_transcript(f"You: {in_buf.text}")
                                except Exception:
                                    pass

                                # Checked here rather than at turn_complete: if
                                # the model has gone quiet there may never be a
                                # turn to complete, and waiting for one is the
                                # very failure the phrase exists to break.
                                if (
                                    time.monotonic() - self._last_wake > 5
                                    and _is_wake_phrase(in_buf.text, self._asst_name)
                                ):
                                    in_buf.clear()
                                    self.wake(txt)

                        if sc.turn_complete:
                            if self._turn_done_event:
                                self._turn_done_event.set()

                            # The turn is over, so the preview has nothing left
                            # to preview. Writing the finished "You:" line takes
                            # it down too, but a turn that produced no speech
                            # writes no line — and the stale preview would sit
                            # there looking like the assistant was still hearing
                            # something.
                            try:
                                self.ui.set_live_transcript("")
                            except Exception:
                                pass

                            # If this turn_complete ends an interrupted response, clear the
                            # flag and skip all further processing for that turn.
                            if self._interrupted:
                                self._interrupted = False
                                in_buf.clear()
                                out_buf.clear()
                                continue

                            full_in = in_buf.text
                            if full_in:
                                # Also on the console: when the assistant seems
                                # not to hear, the first thing worth knowing is
                                # whether the words ever arrived, and the HUD
                                # alone cannot be read from a log.
                                print(f"[JARVIS] 🗣  Heard: {full_in}")
                                self.ui.write_log(f"You: {full_in}")
                                self._session_log.append(f"User: {full_in}")
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "user",
                                        "text": full_in,
                                        "ts": datetime.now().isoformat(),
                                    }))
                            in_buf.clear()

                            full_out = out_buf.text
                            if full_out:
                                self.ui.write_log(f"{self._asst_name}: {full_out}")
                                self._session_log.append(f"{self._asst_name}: {full_out}")
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "jarvis",
                                        "text": full_out,
                                        "ts": datetime.now().isoformat(),
                                    }))
                            out_buf.clear()

                            # One row per finished turn, so tomorrow's "do you
                            # remember what we worked out?" has something to
                            # read. Queued and written by a background thread —
                            # see memory/conversation_log — so a slow or absent
                            # Supabase never delays the next thing she says.
                            if full_in or full_out:
                                log_turn(BASE_DIR, full_in, full_out)

                            # Vision injection: model finished tool-response turn → now send the image
                            if self._pending_vision and self.session:
                                import base64 as _b64
                                img_b, mime_t, question, angle = self._pending_vision
                                self._pending_vision = None
                                b64 = _b64.b64encode(img_b).decode("ascii")
                                print(f"[Vision] 📤 {len(img_b):,} bytes (angle={angle}) → main session")
                                await self.session.send_client_content(
                                    turns={"parts": [
                                        {"inline_data": {"mime_type": mime_t, "data": b64}},
                                        {"text": question},
                                    ]},
                                    turn_complete=True,
                                )
                                # Mark next turn_complete behaviour depending on angle
                                if self._vision_cam_active:
                                    # Camera: keep busy until JARVIS finishes speaking the answer
                                    self._vision_cam_active    = False
                                    self._vision_close_pending = True
                                else:
                                    # Screen-only: no camera to close; release busy flag now
                                    self._vision_busy = False
                            elif self._vision_close_pending:
                                # This turn_complete IS the vision answer — close camera + release busy flag
                                self._vision_close_pending = False
                                self._vision_busy = False
                                async def _cam_close():
                                    await asyncio.sleep(2.0)
                                    self.ui.stop_camera_stream()
                                asyncio.create_task(_cam_close())

                    if response.tool_call:
                        fn_responses = []
                        for fc in response.tool_call.function_calls:
                            print(f"[JARVIS] 📞 {fc.name}")
                            fr = await self._execute_tool(fc)
                            fn_responses.append(fr)
                        await self.session.send_tool_response(
                            function_responses=fn_responses
                        )
        except Exception as e:
            print(f"[JARVIS] ❌ Recv: {e}")
            traceback.print_exc()
            raise

    async def _play_audio(self):
        print("[JARVIS] 🔊 Play started")

        _spk_name = get_output_device()
        _spk_dev  = audio_devices.resolve(_spk_name, "output")
        if _spk_dev is not None:
            print(f"[JARVIS] 🔊 Output device: {_spk_name}")

        def _open_spk(dev):
            try:
                return _open_spk_buffered(dev)
            except Exception as _le:
                # A device that will not grant the larger buffer still has to
                # play. Losing the cushion is a worse voice, not no voice.
                print(f"[Audio] output: large buffer refused ({_le}) — using the default one")
                return _open_spk_plain(dev)

        def _open_spk_plain(dev):
            st = sd.RawOutputStream(
                samplerate=RECEIVE_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SIZE,
                device=dev,
            )
            st.start()
            return st

        def _open_spk_buffered(dev):
            st = sd.RawOutputStream(
                samplerate=RECEIVE_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SIZE,
                device=dev,
                # Asked for, and on this machine ignored: measured against the
                # output the probe in core/audio_devices settles on, MME reports
                # 213.3 ms of latency whether this is set or not. It is kept
                # because a host API that does honour it gives the same cushion
                # for free, and _open_spk falls back when a device refuses it —
                # but the thing that actually stopped the stutter here was
                # priming the first write, not this.
                latency="high",
            )
            st.start()
            return st

        try:
            stream = _open_spk(_spk_dev)
        except Exception as _e:
            # A chosen output that the host API accepts by name but refuses to
            # open (exclusive mode, wrong sample rate, device asleep) must not
            # cost the user their voice. Fall back to the default and say so.
            if _spk_dev is None:
                raise
            print(f"[JARVIS] ⚠️  Output device '{_spk_name}' failed: {_e} — using default")
            self.ui.write_log(f"SYS: Speaker '{_spk_name}' unavailable — using system default.")
            stream = _open_spk(None)

        # The sound card gets a thread of its own. Every tool in this app runs
        # through run_in_executor(None, ...) and asyncio.to_thread uses that same
        # default pool, so a write to the speaker was queueing behind whatever
        # the assistant happened to be doing — the Copilot bridge holds a worker
        # for up to ninety seconds, the VS Code one for about five. Measured over
        # one turn: fifteen gaps totalling 1912 ms, every one of them with audio
        # already waiting in the queue. The card cannot wait; a plugin can.
        _writer = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="lumina-audio-out"
        )
        loop = asyncio.get_running_loop()

        _spoken = 0          # bytes written to the sound card this turn

        # Choppy speech is not a matter of opinion, so it gets measured rather
        # than argued about. _dry_at is when the sound card is expected to run
        # out of the audio already handed to it; arriving later than that means
        # it sat silent in the middle of a sentence, which is exactly what the
        # user hears as cutting out.
        _dry_at = 0.0
        _gaps = 0
        _gap_ms = 0.0

        # Not every silence is a fault. A turn that stops to run a tool — a web
        # search, a memory lookup — legitimately sends no audio for seconds, and
        # counting that as choppy speech would point the diagnosis at the sound
        # card every single time she looks something up. Starvation is a buffer
        # running out: tens of milliseconds, a few hundred at worst. Past this,
        # she was thinking, not stuttering.
        _CHOP_MAX = 1.5
        _pauses = 0

        # Knowing the gaps are ours is not the same as knowing where they come
        # from. Giving the writes a thread of their own removed some and left
        # twelve of fourteen in a later turn, so the remaining time is being
        # spent somewhere else in this loop. These three cover everywhere it
        # can go: waiting on the queue, drawing the HUD, and the write itself.
        _t_wait = 0.0
        _t_level = 0.0
        _t_write = 0.0

        # Two very different faults sound identical. If the sound card ran dry
        # while audio was already sitting in the queue, this loop starved it and
        # the fix is here. If the queue was empty, the audio had simply not
        # arrived from the model yet, and no amount of local buffering invents
        # sound that does not exist. Counting the first tells them apart.
        _ours = 0

        # Speech starts the moment the first 50 ms slice lands, which leaves the
        # card with 50 ms of cushion against a network that delivers in bursts.
        # Holding back a fifth of a second before the first write is inaudible
        # as delay and is the difference between continuous speech and a stutter
        # at the start of every answer.
        _PRIME_BYTES = 9600          # 200 ms at 24 kHz / 16-bit mono
        _PRIME_WAIT = 0.35           # never hold speech longer than this

        try:
            while True:
                _t0 = time.monotonic()
                try:
                    chunk = await asyncio.wait_for(
                        self.audio_in_queue.get(),
                        timeout=0.1
                    )
                    _t_wait += time.monotonic() - _t0
                except asyncio.TimeoutError:
                    if (
                        self._turn_done_event
                        and self._turn_done_event.is_set()
                        and self.audio_in_queue.empty()
                    ):
                        # How much voice actually reached the sound card this
                        # turn. Zero here, with a reply in the log, is the whole
                        # difference between "it ignored me" and "it answered
                        # and I could not hear it".
                        if _spoken:
                            _chop = (f", chopped {_gaps}× for {_gap_ms:.0f} ms"
                                     f" ({_ours} ours, {_gaps - _ours} waiting on the model)"
                                     if _gaps else "")
                            _paused = f", paused {_pauses}×" if _pauses else ""
                            if _gaps:
                                _paused += (f" [wait {_t_wait:.1f}s, hud {_t_level:.1f}s, "
                                            f"write {_t_write:.1f}s]")
                            print(f"[JARVIS] 🔈 Spoke {_spoken} bytes "
                                  f"({_spoken / 48000:.1f}s){_chop}{_paused}", flush=True)
                        _spoken = 0
                        _gaps = 0
                        _gap_ms = 0.0
                        _ours = 0
                        _pauses = 0
                        _t_wait = _t_level = _t_write = 0.0
                        _dry_at = 0.0
                        self.set_speaking(False)
                        self._turn_done_event.clear()
                    continue

                # Sampled before the batching below empties it.
                _waiting = self.audio_in_queue.qsize()

                self.set_speaking(True)

                # Batch all immediately-available chunks into one write to reduce
                # thread-pool round-trips (was one asyncio.to_thread per 50ms slice).
                # Cap at ~200 ms so interrupt() still stops audio within ~200 ms.
                batch = bytearray(chunk)
                while len(batch) < 9600:   # 9600 bytes ≈ 200 ms at 24 kHz / 16-bit mono
                    try:
                        batch.extend(self.audio_in_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                # First write of a turn: wait, briefly, for a cushion. Every
                # later write already has the card playing ahead of it.
                if not _spoken:
                    _prime_until = time.monotonic() + _PRIME_WAIT
                    while len(batch) < _PRIME_BYTES and time.monotonic() < _prime_until:
                        try:
                            batch.extend(
                                await asyncio.wait_for(
                                    self.audio_in_queue.get(), timeout=0.05
                                )
                            )
                        except (asyncio.TimeoutError, asyncio.CancelledError):
                            break

                # Did the card run dry before this batch reached it?
                _now = time.monotonic()
                if _spoken and _now > _dry_at + 0.02:
                    _silence = _now - _dry_at
                    if _silence <= _CHOP_MAX:
                        _gaps += 1
                        _gap_ms += _silence * 1000
                        if _waiting:
                            _ours += 1
                    else:
                        _pauses += 1
                _dry_at = max(_now, _dry_at) + len(batch) / 48000.0

                # Drive the HUD waveform from JARVIS's own voice while speaking.
                _t0 = time.monotonic()
                try:
                    self.ui.set_audio_level(_pcm_level(
                        np.frombuffer(bytes(batch), dtype=np.int16)))
                except Exception:
                    pass
                _t_level += time.monotonic() - _t0

                try:
                    _t0 = time.monotonic()
                    await loop.run_in_executor(_writer, stream.write, bytes(batch))
                    _t_write += time.monotonic() - _t0
                    _spoken += len(batch)
                except (RuntimeError, asyncio.CancelledError):
                    # Playback ending here is invisible otherwise: the assistant
                    # goes on answering, the log fills up, and not a sound comes
                    # out — which is indistinguishable from it ignoring the user.
                    print(f"[JARVIS] 🔇 Playback stopped after {_spoken} bytes", flush=True)
                    break
        except Exception as e:
            print(f"[JARVIS] ❌ Play: {e}")
            raise
        finally:
            self.set_speaking(False)
            stream.stop()
            stream.close()
            _writer.shutdown(wait=False)

    # ── Morning briefing ────────────────────────────────────────────────────────

    async def _send_startup_briefing(self) -> None:
        """
        Two-phase briefing optimized for speed:
          Phase 1 — instant greeting (no tools) → speech starts in <1s
          Phase 2 — news pre-fetched in a background thread while Phase 1 plays,
                    delivered as ready text (no Gemini tool-call round-trip) and
                    shown on the UI content panel. Waits for turn_complete event
                    instead of a fixed sleep so there is no unnecessary gap.
        """
        memory   = load_memory()
        identity = memory.get("identity", {})

        def _val(k: str) -> str:
            e = identity.get(k, {})
            return (e.get("value", "") if isinstance(e, dict) else str(e)).strip()

        lang = _val("language")
        name = _val("name")
        time_str = datetime.now().strftime("%H:%M")

        # Start fetching news immediately — runs in parallel while phase 1 plays
        loop = asyncio.get_event_loop()
        news_future = loop.run_in_executor(None, _fetch_news_sync, "top world news today")

        await asyncio.sleep(0.3)
        if not self.session:
            return

        # ── Phase 1: instant greeting ─────────────────────────────────────────
        # The briefing fires before the user has said anything, so the
        # remembered language is the only signal there is. It is a starting
        # point, not a setting: the moment they reply, their language wins.
        lang_clause = (f" Speak this greeting in {lang}, then follow the "
                       f"user's own language from their first reply onward."
                       if lang else "")
        name_clause = f" Address the user as {name}." if name else ""

        # Inject last session context if available — pop removes it so it's never repeated
        last = await asyncio.to_thread(pop_last_session)
        session_clause = ""
        if last:
            try:
                _delta = (datetime.now() - datetime.strptime(last["date"], "%Y-%m-%d")).days
                _when  = "earlier today" if _delta == 0 else ("yesterday" if _delta == 1 else f"{_delta} days ago")
            except Exception:
                _when = "last time"
            session_clause = (
                f" Also briefly and naturally mention that {_when}: {last['summary']}"
            )

        p1 = (
            f"Greet the user warmly, mention it is {time_str}, and say you are fetching today's news now.{session_clause} "
            f"Keep it to 2 short sentences max. Do not call any tools.{lang_clause}{name_clause}"
        )

        # Clear the turn-done event so we can wait for Phase 1 to finish
        if self._turn_done_event:
            self._turn_done_event.clear()

        await self.session.send_client_content(
            turns={"parts": [{"text": p1}]},
            turn_complete=True,
        )
        self.ui.write_log("SYS: Briefing phase 1 (greeting) sent.")

        # ── Phase 2: fire as soon as Phase 1 audio is done ───────────────────
        async def _deliver_news():
            try:
                lang_str = (f" Speak in {lang} unless the user has since "
                            f"spoken another language, in which case use theirs."
                            if lang else "")

                # Wait for news fetch (already running) and Phase 1 turn-complete
                # in parallel — whichever takes longer determines the wait time
                news_done   = asyncio.wrap_future(news_future)
                turn_waited = False
                if self._turn_done_event:
                    try:
                        await asyncio.wait_for(self._turn_done_event.wait(), timeout=6.0)
                        turn_waited = True
                    except asyncio.TimeoutError:
                        pass

                # Extra buffer: turn_complete fires when Gemini finishes *generating*
                # Phase 1, but audio may still be playing.  Waiting a beat here
                # prevents Phase 2 audio from arriving while Phase 1 is mid-sentence
                # (which sounds like a "repeated first response" to the user).
                if turn_waited:
                    await asyncio.sleep(0.8)
                else:
                    await asyncio.sleep(1.0)

                try:
                    news_text = await asyncio.wait_for(news_done, timeout=8.0)
                except Exception as e:
                    self.ui.write_log(f"SYS: News fetch timed out/failed: {e!r}")
                    news_text = ""

                if not self.session:
                    return

                failed = (not news_text) or news_text.startswith(
                    ("No news found", "Search failed", "Please provide")
                )
                if not failed:
                    # Show on UI content panel immediately
                    self.ui.show_content("NEWS — top world news today", news_text)

                    p2 = (
                        f"[BRIEFING] Here are today's top news headlines:\n{news_text}\n\n"
                        "Pick ONE headline, summarise it in one sentence, then say the full list "
                        f"is displayed on screen. Do not call any tools.{lang_str}"
                    )
                else:
                    self.ui.write_log(
                        f"SYS: News unavailable — backend returned: {news_text[:120]!r}"
                    )
                    p2 = (
                        "News headlines could not be fetched right now. "
                        f"Let the user know briefly.{lang_str}"
                    )

                await self.session.send_client_content(
                    turns={"parts": [{"text": p2}]},
                    turn_complete=True,
                )
                self.ui.write_log("SYS: Briefing phase 2 (news) sent.")
            except Exception as e:
                print(f"[Briefing] Phase 2 error: {e}")
                self.ui.write_log(f"SYS: Briefing phase 2 failed: {e}")

        asyncio.create_task(_deliver_news())

    # ── Session memory ──────────────────────────────────────────────────────────

    async def _save_session_summary(self) -> None:
        """Summarise the current session in 1-2 sentences and save to long_term.json."""
        log = self._session_log
        if len(log) < 3:          # need at least one exchange to be worth saving
            return
        self._session_log = []    # reset immediately so the next session starts clean

        memory = load_memory()
        lang_entry = memory.get("identity", {}).get("language", {})
        lang = (lang_entry.get("value", "") if isinstance(lang_entry, dict) else str(lang_entry)).strip()
        lang = lang or "English"

        convo = "\n".join(log[-40:])   # cap at last 40 turns to stay within token budget
        prompt = (
            f"Summarize this conversation in 1-2 sentences in {lang}. "
            "Focus on what the user accomplished or discussed. "
            "Output ONLY the summary text, nothing else:\n\n" + convo
        )
        try:
            from google import genai as _genai
            client = _genai.Client(api_key=_get_api_key())
            resp   = await asyncio.to_thread(
                client.models.generate_content,
                model="gemini-flash-latest",
                contents=prompt,
            )
            summary = (resp.text or "").strip()
            if summary:
                save_session_summary(summary, lang)
        except Exception as e:
            print(f"[Memory] ⚠️ Session summary failed: {e}")

    # ── System monitor ──────────────────────────────────────────────────────────

    async def _run_system_monitor(self) -> None:
        """Background task: voice alerts when metrics exceed thresholds."""
        while True:
            await asyncio.sleep(10)
            alert = await asyncio.to_thread(self._sys_monitor.check)
            if not alert or not self.session:
                continue
            # Don't interrupt an active conversation
            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking or (time.monotonic() - self._last_user_speech) < 10:
                continue
            try:
                await self.session.send_client_content(
                    turns={"parts": [{"text": alert}]},
                    turn_complete=True,
                )
            except Exception as e:
                print(f"[Monitor] ⚠️ Could not send alert: {e}")

    # ── Background monitor ──────────────────────────────────────────────────────

    async def _run_background_monitor(self) -> None:
        """Check user-configured topics once per day; speak alerts when new headlines appear."""
        await asyncio.sleep(300)          # wait 5 min after startup before first check
        while True:
            if self.session:
                # Don't interrupt if user spoke recently or JARVIS is mid-sentence
                with self._speaking_lock:
                    speaking = self._is_speaking
                recent_speech = (time.monotonic() - self._last_user_speech) < 30
                if not speaking and not recent_speech:
                    try:
                        alerts = await asyncio.to_thread(monitor_check_all)
                        memory = load_memory()
                        lang_e = memory.get("identity", {}).get("language", {})
                        lang   = (lang_e.get("value", "") if isinstance(lang_e, dict) else str(lang_e)).strip() or "English"
                        for alert in alerts:
                            msg = (
                                f"{alert}\n\n"
                                f"Inform the user about this development naturally in {lang}. "
                                "One brief sentence only."
                            )
                            await self.session.send_client_content(
                                turns={"parts": [{"text": msg}]},
                                turn_complete=True,
                            )
                            self.ui.write_log(f"SYS: Monitor alert sent.")
                            await asyncio.sleep(6)   # gap between consecutive alerts
                    except Exception as e:
                        print(f"[Monitor] ⚠️ Background check error: {e}")
            await asyncio.sleep(1800)     # check every 30 minutes

    # ── Proactive mode ──────────────────────────────────────────────────────────

    async def _run_proactive_mode(self) -> None:
        """
        Background task: periodically checks if the user has been silent long enough,
        then hands time + memory context to Gemini so it can decide what (if anything)
        to say proactively. No hardcoded rules — Gemini makes the call.
        """
        while True:
            await asyncio.sleep(60)   # evaluate once per minute

            if not self.session:
                continue

            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking:
                continue

            if not self._proactive.should_trigger(self._last_user_speech):
                continue

            self._proactive.mark_triggered()

            try:
                memory       = await asyncio.to_thread(load_memory)
                monitors     = await asyncio.to_thread(list_monitors)
                recent_turns = self._session_log[-8:] if self._session_log else []
                prompt = self._proactive.build_prompt(
                    memory       = memory,
                    monitors     = monitors or None,
                    recent_turns = recent_turns or None,
                )
                await self.session.send_client_content(
                    turns={"parts": [{"text": prompt}]},
                    turn_complete=True,
                )
                self.ui.write_log("SYS: Proactive check-in.")
            except Exception as e:
                print(f"[Proactive] ⚠️ {e}")

    # ── Phone audio relay ────────────────────────────────────────────────────────

    async def _relay_phone_audio(self) -> None:
        """Forward phone mic PCM chunks from dashboard queue into the Gemini Live session."""
        q = self._dashboard._phone_audio_queue
        while True:
            try:
                chunk = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                # No audio for 1 s → phone mic inactive, give PC mic back
                self._phone_active = False
                continue
            self._phone_active = True   # phone is streaming — silence PC mic
            with self._speaking_lock:
                speaking = self._is_speaking
            if not speaking and not self.ui.muted:
                try:
                    self.out_queue.put_nowait(chunk)
                except asyncio.QueueFull:
                    pass

    def _on_phone_connected(self) -> None:
        self.ui.write_log("SYS: Phone connected via Remote Dashboard.")
        self.ui.notify_phone_connected()

    # ── dashboard command relay ─────────────────────────────────────────────

    async def _process_dashboard_commands(self) -> None:
        while True:
            try:
                text = await asyncio.wait_for(
                    self._dashboard._command_queue.get(), timeout=0.5
                )
                if not text:
                    continue
                # Wait up to 8s for session to become ready after a wake
                for _ in range(80):
                    if self.session:
                        break
                    await asyncio.sleep(0.1)
                if self.session:
                    await self.session.send_client_content(
                        turns={"parts": [{"text": text}]},
                        turn_complete=True,
                    )
                    self.ui.write_log(f"[Web]: {text}")
                else:
                    print(f"[Dashboard] Dropped command (no session): {text}")
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                print(f"[Dashboard] Command error: {e}")
                await asyncio.sleep(0.5)

    # ── main loop ───────────────────────────────────────────────────────────

    async def run(self):
        self._loop = asyncio.get_event_loop()
        self._reconnect_event = asyncio.Event()

        # ── Wire the shared core services to the interface ───────────────────
        # The confirmation gate is useless without a way to ask, and a memory
        # trim is invisible without a way to say so. Both are bound once here
        # rather than passed down through every action signature.
        confirm_gate.bind(
            show = self.ui.show_confirm,
            hide = self.ui.hide_confirm,
            log  = self.ui.write_log,
        )
        set_trim_notifier(self.ui.write_log)

        # Tell the device picker the exact rates the streams open at, from the
        # constants that actually open them — so it can never list a device that
        # cannot be opened at them.
        audio_devices.configure(SEND_SAMPLE_RATE, RECEIVE_SAMPLE_RATE)

        # Enumerate audio devices off-thread. The settings drawer must never pay
        # for host-API enumeration on the Qt thread.
        audio_devices.prefetch()

        # Start dashboard (optional — needs: pip install fastapi "uvicorn[standard]" cryptography)
        try:
            from dashboard.server import DashboardServer
            self._dashboard = DashboardServer()
            self._dashboard.set_connect_callback(self._on_phone_connected)
            asyncio.create_task(self._dashboard.serve())
            # Runs for the whole lifetime, not just inside an active session
            asyncio.create_task(self._process_dashboard_commands())
        except Exception as e:
            print(f"[Dashboard] Disabled: {e}")
            self._dashboard = None

        while True:
            try:
                print("[JARVIS] Connecting...")
                self.ui.set_state("THINKING")
                _resumed_with = self._resume_handle is not None
                config = self._build_config()

                # Fresh client on every reconnect — avoids stale HTTP session state
                # v1alpha carries the enhanced audio features (affective dialog,
                # proactive audio); if they get rejected we fall back to v1beta.
                client = genai.Client(
                    api_key=_get_api_key(),
                    http_options={"api_version": "v1alpha" if self._enhanced_live else "v1beta"}
                )

                async with (
                    client.aio.live.connect(model=LIVE_MODEL, config=config) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    self.session          = session
                    self.audio_in_queue   = asyncio.Queue()
                    self.out_queue        = asyncio.Queue(maxsize=200)
                    self._turn_done_event = asyncio.Event()

                    # Reset transient state that must not carry over from a previous session
                    self._pending_vision       = None
                    self._vision_cam_active    = False
                    self._vision_close_pending = False
                    self._vision_busy          = False
                    self._vision_last_time     = 0.0
                    self._interrupted          = False

                    # Stamped so the reply-latency line can say how old this
                    # session was when it answered. A session that slows down
                    # the longer it runs and one that is simply slow today look
                    # identical without it.
                    self._session_started = time.monotonic()
                    self._heard_this_session = 0
                    print("[JARVIS] Connected.")
                    if _resumed_with:
                        # Say it plainly: the difference between "it reconnected"
                        # and "it reconnected and still knows what we were doing"
                        # is the whole point, and it is invisible otherwise.
                        self.ui.write_log("SYS: Reconnected — conversation restored.")
                    self.ui.set_state("LISTENING")
                    self.ui.write_log(f"SYS: {self._asst_name} online.")

                    if self._dashboard:
                        await self._dashboard.broadcast({"type": "status", "state": "active"})

                    self._reconnect_event.clear()  # ignore requests from before this session
                    tg.create_task(self._watch_reconnect())
                    tg.create_task(self._send_realtime())
                    tg.create_task(self._listen_audio())
                    tg.create_task(self._receive_audio())
                    tg.create_task(self._play_audio())
                    tg.create_task(self._run_system_monitor())
                    tg.create_task(self._run_background_monitor())
                    tg.create_task(self._run_proactive_mode())
                    if self._dashboard:
                        tg.create_task(self._relay_phone_audio())

                    # Morning briefing — fires once per process launch (if enabled)
                    if not self._briefing_sent and get_brief_enabled():
                        self._briefing_sent = True
                        tg.create_task(self._send_startup_briefing())

            except KeyboardInterrupt:
                raise
            except SystemExit:
                raise
            except BaseException as e:
                # Catches both Exception and BaseExceptionGroup (Python 3.11+
                # TaskGroup raises BaseExceptionGroup when tasks are cancelled
                # externally, which `except Exception` would miss, letting the
                # exception escape the while-loop and causing asyncio.run() to
                # start shutdown — resulting in "executor after shutdown" errors).
                # Voluntary reconnect (voice change) — not an error. Rebuild the
                # session immediately with no backoff and no scary logs.
                if _is_reconnect_signal(e):
                    print("[JARVIS] Voluntary reconnect requested.")
                    if not _keep_context_of(e):
                        # A deliberate clean slate (voice change) — drop the
                        # handle so the next connect really does start empty.
                        self._resume_handle = None
                    self._conn_backoff = 0
                    continue

                # A resumption handle the server will not accept — expired, or
                # belonging to a session it has since dropped. Without this, the
                # same dead handle would be replayed on every retry and the
                # assistant would never come back at all: the feature meant to
                # survive a reconnect would be the thing preventing one. Drop it
                # once and let the next attempt start clean.
                if _resumed_with and (
                    "resum" in str(e).lower()
                    or "handle" in str(e).lower()
                    or "INVALID_ARGUMENT" in str(e)
                    or "NOT_FOUND" in str(e)
                ):
                    print("[JARVIS] 🔗 Resumption handle rejected — starting a fresh session")
                    self.ui.write_log("SYS: Could not restore the conversation — starting fresh.")
                    self._resume_handle = None
                    self._conn_backoff = 0
                    continue

                err_str = _flatten_error(e)
                print(f"[JARVIS] Error ({type(e).__name__}): {e}")
                traceback.print_exc()

                # An enhanced audio feature rejected by the server. Give up one
                # at a time, and only the one that was named: dropping both on a
                # single refusal is how a model that merely lacks affective
                # dialog also loses proactive audio, which is the feature that
                # keeps the assistant from answering a conversation it was never
                # part of. A refusal that names neither is attributed to
                # affective dialog first, since it is the rarer of the two.
                lowered = err_str.lower()
                generic = (
                    "INVALID_ARGUMENT" in err_str
                    or "invalid argument" in lowered
                    or "Unknown name" in err_str
                    or "unexpected keyword" in err_str
                )
                if self._proactive_live and "proactiv" in lowered:
                    self._proactive_live = False
                    self.ui.write_log(
                        "SYS: Proactive audio unavailable on this model — reconnecting without it."
                    )
                    continue
                if self._affective_live and ("affective" in lowered or generic):
                    self._affective_live = False
                    self.ui.write_log(
                        "SYS: Affective dialog unavailable on this model — reconnecting without it."
                    )
                    continue
                if self._proactive_live and generic:
                    self._proactive_live = False
                    self.ui.write_log(
                        "SYS: Proactive audio unavailable on this model — reconnecting without it."
                    )
                    continue

                # The server refused the audio we were sending. Seen mid-session
                # after a long tool call, and fatal if it is resumed into: the
                # handle restores the same rejected configuration and the next
                # connection dies the same way. Start clean instead — the cost
                # is the conversation history, which beats an assistant that
                # cannot hear.
                if "CONTENT_TYPE_AUDIO" in err_str or "audio content type" in err_str.lower():
                    print("[JARVIS] 🔇 Server rejected the audio stream — reconnecting clean")
                    self.ui.write_log("SYS: Audio session refused — rebuilding it.")
                    self._resume_handle = None
                    self._conn_backoff = 0
                    continue

                # Invalid API key — stop hammering the API, prompt re-configuration.
                # Matched on what the API actually says about credentials. It used
                # to also match "1007", which is merely the WebSocket close code
                # for a bad payload and accompanies failures that have nothing to
                # do with the key — including the audio rejection handled above.
                # On that reading it demanded a new key and then blocked forever
                # waiting for one, which is indistinguishable from the assistant
                # having died.
                if any(k in err_str for k in (
                    "API key not valid", "API_KEY_INVALID",
                    "UNAUTHENTICATED", "PERMISSION_DENIED",
                )):
                    self.ui.write_log("ERR: API key invalid — please re-enter your key.")
                    self.ui.set_state("SLEEPING")
                    self.ui.prompt_reconfig()
                    while not self.ui._win._ready:
                        await asyncio.sleep(1)
                    print("[JARVIS] New API key saved — reconnecting...")
                    _conn_backoff = 3
                    continue

                # Network / timeout errors — log clearly and back off
                is_net_err = any(k in err_str for k in (
                    "TimeoutError", "timed out", "getaddrinfo", "CancelledError",
                    "ConnectionRefusedError", "OSError", "Cannot connect",
                ))
                if is_net_err:
                    _conn_backoff = min(getattr(self, "_conn_backoff", 3) * 2, 60)
                    self._conn_backoff = _conn_backoff
                    self.ui.write_log(
                        f"NET: Connection failed — retrying in {_conn_backoff}s. "
                        "(a VPN may be required)"
                    )
                else:
                    self._conn_backoff = 3
            finally:
                self.session = None
                # Only save if there was a real conversation (≥3 turns)
                if len(self._session_log) >= 3:
                    asyncio.create_task(self._save_session_summary())

            self.set_speaking(False)
            self.ui.set_state("SLEEPING")

            if self._dashboard:
                await self._dashboard.broadcast({"type": "status", "state": "sleeping"})

            delay = getattr(self, "_conn_backoff", 3)
            print(f"[JARVIS] Reconnecting in {delay}s...")
            # Say so on screen, not only on a console nobody has open. A dropped
            # session is several seconds during which speech reaches no one, and
            # until now it looked exactly like being ignored — the user keeps
            # talking to something that is not there.
            self.ui.write_log(f"SYS: Connection lost — reconnecting in {delay}s.")
            await asyncio.sleep(delay)

def main():
    ui = JarvisUI("face.png")

    def runner():
        ui.wait_for_api_key()
        jarvis = JarvisLive(ui)
        try:
            asyncio.run(jarvis.run())
        except KeyboardInterrupt:
            print("\n🔴 Shutting down...")

    threading.Thread(target=runner, daemon=True).start()
    ui.root.mainloop()

if __name__ == "__main__":
    main()
