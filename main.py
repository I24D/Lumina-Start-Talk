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
# One HUD owns one microphone and one selected Live session. A Windows named
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
import traceback
import webbrowser
from collections import deque
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
from actions.screen_processor import (
    _capture_camera, _capture_screen, _normalise_capture_source,
    describe_capture_source, frame_change_score, frame_fingerprint,
)
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
    get_base_dir, get_brief_enabled, get_gemini_key, get_voice,
    get_input_device, get_output_device, get_voice_provider,
    get_learning_voice_provider, get_openai_key, get_openai_voice,
    load_api_keys, save_voice_settings,
)
from config                    import GEMINI_MODEL
from core.plugin_loader        import discover_plugins
from core                      import undo as undo_stack
from core                      import confirm as confirm_gate
from core                      import audio_devices
from core.openai_realtime      import (
    OpenAIRealtimeSession, OPENAI_REALTIME_MODEL, OPENAI_SAMPLE_RATE,
)
from core.echo_canceller       import EchoCanceller
from learning_english import LearningEnglishController, LearningEnglishService
from learning_english.recordings import VoiceRecordings
from learning_english.intent import confirmation_answer, detect_learning_english_intent

BASE_DIR        = get_base_dir()
PROMPT_PATH     = BASE_DIR / "core" / "prompt.txt"
# Measured on 2026-09-14 by streaming the same recorded question straight into
# each model: on 3.1 Flash Live the user's words came back 1.6 s after he stopped
# and her reply 1.7 s after, in three runs of three; on 2.5 native audio 12-14 s
# and 15-16 s, and one run of three never answered at all.
LIVE_MODEL          = "models/gemini-3.1-flash-live-preview"
LIVE_API_VERSION    = "v1beta"

# Sliding-window context compression, on by default, and switchable without an
# edit so it can be measured rather than argued about.
#
# It is one of only two things this session asks for that the upstream project
# does not, and the other — replaying a resumption handle — is already ruled
# out for the run that failed on 2026-09-12: that was a first connect, so the
# handle was None and the configuration was upstream's exactly apart from this
# field, and it degraded anyway. Compression is a server-side operation on the
# conversation so far, which makes it the one remaining candidate whose cost
# grows with session length — and session length is the axis this fault lives
# on. It works for four minutes and then does not.
#
# That is a suspect, not a finding, and it is left on until a measured run says
# otherwise. Turn it off for one run with LUMINA_COMPRESSION=off and compare
# the [VOICE REC] lines; the trade-off if it turns out to be the cause is that
# a very long conversation will eventually hit the context limit instead of
# being compressed, which is a reconnect rather than a failure.
_COMPRESSION_ON = True
try:
    import os as _os_cfg
    _COMPRESSION_ON = _os_cfg.environ.get(
        "LUMINA_COMPRESSION", "on"
    ).strip().lower() not in ("0", "off", "false", "no")
except Exception:
    pass
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
# It fired again at eight seconds old, while she was still delivering her
# own opening briefing, and threw away the session the user was about to
# speak into. A brand-new session has heard nothing yet — so has every
# healthy one — which is why "has heard nothing" cannot stand on its own.
# The real case, measured, was a session that stayed deaf for its whole
# life while the user talked at it repeatedly. These numbers describe that
# and nothing a working session can reach: a healthy session transcribes
# within a second or two of the first word.
_DEAF_MIN_SESSION     = 90.0    # younger than this is never rebuilt
_DEAF_VOICE_SECONDS   = 20.0    # this much *clear* speech, not this much sound
_DEAF_SILENCE_SECONDS = 25.0
_DEAF_VOICE_LEVEL     = 0.40    # measured: this room idles at 0.23 and peaks at 0.33
_DEAF_COOLDOWN        = 120.0   # never rebuild more often than this
_DEAF_ECHO_TAIL       = 2.0     # her own voice keeps arriving after she stops

# Plugin news nobody asked for (_run_announcements) waits for a quiet moment.
_ANNOUNCE_USER_QUIET  = 10.0    # the user spoke this recently: do not cut in
_ANNOUNCE_GAP         = 2.5     # a breath between her last words and the news
_ANNOUNCE_ANSWER_WAIT = 30.0    # stop waiting for her answer to the last item to start
_ANNOUNCE_MAX_AGE     = 900.0   # still unsaid after fifteen minutes, it is not news
_ANNOUNCE_QUEUE_LIMIT = 20

# Smart Vision compares each fresh sample with the last frame the model saw.
# A keyframe still refreshes context periodically when the source is static.
_VISION_SCREEN_CHANGE = 0.006
_VISION_CAMERA_CHANGE = 0.014
_VISION_KEYFRAME_SECS = 12.0

# Full duplex: whether the microphone may stay open while she speaks.
#
# It normally may not. Measured in this room with the speakers on, her own
# voice comes back into the microphone at up to full scale — louder than the
# level a person talking reaches — so no threshold can separate her from the
# user, and forwarding it means she transcribes herself, interrupts herself,
# and acts on her own words. That is what once had her open Facebook and
# query Copilot three times over.
#
# Headphones remove the path entirely: nothing she says reaches the
# microphone, so it can stay open permanently and the user can cut her off by
# speaking, the way they would interrupt a person. Windows renames the
# endpoint when they are plugged in, which is the whole detection.
_HEADPHONE_WORDS = ("headphone", "headset", "earphone", "earbud",
                    "auricular", "audifono", "audífono")


def _is_headphones(device_name: str) -> bool:
    name = (device_name or "").casefold()
    return any(word in name for word in _HEADPHONE_WORDS)
CHANNELS            = 1
SEND_SAMPLE_RATE    = 16000
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE          = 1024

# A client-side detector, used now only to light the "Listening..." hint and
# to time the reply-latency line. It no longer touches the audio stream: it
# used to end the stream after every pause, which deafened whole sessions.
#
# The floor is measured, not guessed. This room reads 0.231 on average with
# nobody talking and peaks at 0.334, so the old 0.20 start threshold fired
# continuously on an empty room — thirty-one times in one session. Speech from
# the same microphone clears 0.5 comfortably.
_LOCAL_VAD_MIN_START_LEVEL   = 0.40
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


def _resample_pcm16(data: bytes, from_rate: int, to_rate: int) -> bytes:
    """Linear resampling of 16-bit mono PCM; plenty for speech."""
    samples = np.frombuffer(data, dtype=np.int16)
    if from_rate == to_rate or samples.size == 0:
        return data
    count = max(1, int(round(samples.size * to_rate / from_rate)))
    positions = np.linspace(0, samples.size - 1, count)
    return np.interp(positions, np.arange(samples.size), samples).astype(np.int16).tobytes()


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
                "city": {"type": "STRING", "description": "City name in English, with the state or country when it could be ambiguous, e.g. 'New York, US' or 'Greeneville, Tennessee'"}
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
            "Takes one screen or webcam image when Live Vision is OFF. "
            "Call it when the user asks what is on screen or camera and no live "
            "video frames are already present. If Live Vision frames are present, "
            "answer directly from them and do NOT call this tool. "
            "After the image is captured it is sent directly to you — describe what you see and answer the user's question. "
            "A one-shot camera preview closes after the answer."
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
            "Stops Live Vision or closes the one-shot camera preview without "
            "ending the voice conversation. Call when the user says stop vision, "
            "stop sharing, close camera, turn off camera, cierra la cámara, "
            "deja de compartir la pantalla, apaga la cámara, etc."
        ),
        "parameters": {"type": "OBJECT", "properties": {}, "required": []}
    },
    {
        "name": "vision_highlight",
        "description": (
            "Shows a temporary visual pointer on the currently shared display or "
            "application window. Use only while Live Vision is active and the user "
            "asks where something is, where to click, or says 'show me'. Locate the "
            "target in the newest frame, then provide coordinates normalized from "
            "0 to 1000 relative to that frame. This points only; it never clicks."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "x": {"type": "INTEGER", "description": "Horizontal target coordinate, 0 left to 1000 right"},
                "y": {"type": "INTEGER", "description": "Vertical target coordinate, 0 top to 1000 bottom"},
                "label": {"type": "STRING", "description": "Short label shown beside the pointer"},
            },
            "required": ["x", "y", "label"],
        },
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
                "action": {"type": "STRING", "description": "wallpaper | wallpaper_url | current_wallpaper | organize | clean | list | stats"},
                "path":   {"type": "STRING", "description": "Image path for wallpaper"},
                "url":    {"type": "STRING", "description": "Image URL for wallpaper_url"},
                "mode":   {"type": "STRING", "description": "by_type or by_date for organize"},
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
        self._user_name     = "You"      # updated each session from config
        self.session              = None
        self.audio_in_queue       = None
        self.out_queue            = None
        # Gemini's measured path is 16 kHz. OpenAI Realtime takes 24 kHz PCM,
        # the rate her voice plays at, so its session opens the same device at
        # 24 kHz and the echo canceller compares like with like.
        self._input_sample_rate   = SEND_SAMPLE_RATE
        self._loop                = None
        self._is_speaking         = False
        # When her own audio last stopped. Her voice keeps reaching the
        # microphone for a moment after the gate reopens, and that echo is
        # not the user talking.
        self._last_spoke_at       = 0.0
        # Set once the speaker is open, from the name of the device it opened.
        self._full_duplex         = False
        # The OpenAI call's echo canceller, or None. With it the microphone
        # stays open while she speaks on speakers, as it does on headphones.
        self._echo: EchoCanceller | None = None
        self._echo_announced      = False
        self._mic_latency         = 0.0
        self._speaker_latency     = 0.0
        # Audio blocks that actually reached the model. Proof of the
        # one link that used to report nothing either way.
        self._sent_blocks         = 0
        self._speaking_lock       = threading.Lock()
        self._phone_active        = False   # True while phone mic is streaming; pauses PC mic
        self._pending_vision       = None    # (img_bytes, mime_type, question, angle) to inject after tool response
        self._vision_cam_active    = False   # True if camera was opened for vision → auto-close after response
        self._vision_close_pending = False   # True after vision injected; next turn_complete closes camera
        self._vision_last_time     = 0.0     # monotonic time of last screen_process call (cooldown guard)
        self._vision_busy          = False   # True while a vision capture/inject cycle is in flight
        # User-controlled, Copilot-style continuous Vision. This is deliberately
        # separate from screen_process above: that tool answers one request;
        # Live Vision keeps sending a selected source until the user presses STOP.
        self._live_vision_source   = ""      # "screen" | "camera" | ""
        self._live_vision_epoch    = 0       # invalidates an in-flight capture on switch/stop
        self._live_vision_latest   = None    # (jpeg bytes, mime, timestamp, epoch)
        self._live_vision_announced = False
        self._live_vision_should_greet = False
        self._live_vision_frames   = 0
        self._live_vision_paused   = False
        self._live_vision_fingerprint = None
        self._live_vision_last_sent_at = 0.0
        self._live_vision_skipped  = 0
        self._live_vision_label    = ""
        self._interrupted          = False   # True while draining audio after user interrupt
        self.ui.on_text_command   = self._on_text_command
        self.ui.on_remote_clicked = self._make_remote_key
        self.ui.on_learning_english = self._on_learning_english_clicked
        self.ui.on_interrupt      = self.interrupt
        self.ui.on_voice_change   = self._on_voice_change     # voice picker → rebuild session
        self.ui.on_audio_device_change = self._on_audio_device_change
        self.ui.on_vision_requested = self.start_live_vision
        self.ui.on_vision_stop      = self.stop_live_vision
        self.ui.on_vision_pause     = self.pause_live_vision
        self.ui.on_camera_frame     = self._on_live_camera_frame
        self.ui.on_camera_error     = self._on_live_camera_error
        self._reconnect_event: asyncio.Event | None = None
        # Keep strong references to long-lived dashboard tasks. The event loop
        # only keeps weak task references, so an unreferenced server task can
        # disappear before uvicorn reaches its listening state.
        self._dashboard_task: asyncio.Task | None = None
        self._dashboard_commands_task: asyncio.Task | None = None
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
        # Learning English runs its own selected Live session in the browser.
        # This session stays the general assistant throughout, tools included;
        # only its microphone and voice pause while a class is open.
        self._learning_confirmation_pending = False
        self._learning_confirmation_prompt_pending = False
        self._learning_seen_turns: deque[tuple[int, float]] = deque(maxlen=50)
        self._learning_analysis_lock: asyncio.Lock | None = None
        self._learning = LearningEnglishController(event_sink=self._emit_learning_event)
        self._learning_service = LearningEnglishService(api_key_loader=get_gemini_key)
        self._learning_recordings = VoiceRecordings(BASE_DIR)
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
        # Deaf rebuilds in a row, reset the moment a session hears one word.
        # The first rebuild keeps the conversation; a second one in a row means
        # keeping it is what is being rebuilt into, and drops it. See the
        # deafness watchdog for why.
        self._deaf_rebuilds = 0
        self._tool_running = 0
        # Tool calls the model has issued and not yet had answered. Gemini 3.1
        # cancels an open call if any text reaches it meanwhile; see plugin_say.
        self._tool_calls_open = 0
        # Tool calls run beside the conversation, not inside the receive loop.
        self._tool_tasks: set[asyncio.Task] = set()
        # Transcriptions this session has produced since it connected. The
        # failure the watchdog below exists for is a session born deaf: it
        # connects, answers typed text, and never transcribes one word of
        # audio for its whole life. A session that has transcribed even once
        # is listening, and must never be torn down on suspicion.
        self._heard_this_session = 0
        # Whether the live "You:" line already holds words from this turn. The
        # microphone thread checks it before writing a placeholder over them.
        self._live_has_text = False

        # ── Voice flight recorder ───────────────────────────────────────────
        # Every silence in this app looks the same from the outside: the user
        # talks and nothing comes back. Four very different faults produce it —
        # the server stopped sending, the microphone stopped delivering, the
        # audio stopped leaving at the rate it arrives, or this event loop
        # stopped running — and none of them could be told apart, because none
        # of the four was ever printed. A session that degraded after four
        # minutes and one that never worked read identically in the log.
        #
        # These are sampled by _run_voice_recorder on a fixed cadence. Nothing
        # here writes to the session, sends anything, or rebuilds anything: a
        # measurement that changes what it measures is how the last three
        # watchdogs destroyed healthy conversations.
        self._rx_at = 0.0                       # last message the stream yielded
        self._rx_kinds: dict[str, int] = {}     # what kind, since the last record
        self._mic_blocks = 0                    # delivered by the device, pre-gate
        self._mic_peak = 0.0                    # loudest block since the last record
        self._mic_dropped = 0                   # blocks the send queue had no room for

        # Keep the server audio configuration at its proven baseline. Optional
        # affective and proactive fields have both produced sessions that accept
        # microphone audio but never return a transcription. The application's
        # local proactive engine remains enabled and is independent of them.
        _core_names = {t["name"] for t in TOOL_DECLARATIONS}
        self._plugin_registry = discover_plugins(
            plugins_dir=Path(__file__).resolve().parent / "plugins",
            core_tool_names=_core_names,
            logger=lambda msg: (print(f"[Plugins] {msg}"), self.ui.write_log(f"SYS: {msg}")),
        )
        self.ui.get_plugins = self._plugin_registry.list_for_ui
        self.ui.request_say = self.plugin_say   # plugins: mid-task speech channel
        # News a plugin noticed on its own, waiting for _run_announcements to
        # find her free: (queued_at, instruction), oldest first.
        self._announcements: deque[tuple[float, str]] = deque(maxlen=_ANNOUNCE_QUEUE_LIMIT)
        self._announced_at = 0.0
        self.ui.request_announce = self.plugin_announce
        self._plugin_registry.start_all(player=self.ui)

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
                # Gemini 3.1 cancels an open tool call the moment any text
                # reaches it: the result is thrown away and the model calls
                # tools again (measured 2026-09-15). What a plugin has to say
                # waits until the open calls have been answered.
                if not isinstance(self.session, OpenAIRealtimeSession):
                    while self._tool_calls_open:
                        await asyncio.sleep(0.1)
                await self._send_text_turn(instruction)
            except Exception as e:
                print(f"[PluginSay] {e}")

        try:
            asyncio.run_coroutine_threadsafe(_say(), loop)
        except Exception as e:
            print(f"[PluginSay] {e}")

    def plugin_announce(self, instruction: str) -> None:
        """
        Thread-safe channel for news a plugin noticed on its own, such as
        another assistant finishing a task. plugin_say speaks at once because
        the user is waiting on that plugin; nobody is waiting on news, so it
        must never cut in. The instruction is queued, and _run_announcements
        delivers it once she is free.
        """
        self._announcements.append((time.monotonic(), instruction))
        print(f"[Announce] queued ({len(self._announcements)} waiting)")

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
                self._send_text_turn(
                        "The user just said your wake phrase because you were not "
                        "responding. Greet them in one short sentence, in their "
                        "language, and ask what they need."
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

    def start_live_vision(self, source: str) -> None:
        """Thread-safe UI entrypoint for persistent screen/camera sharing."""
        source = (
            "camera" if str(source).lower() == "camera"
            else _normalise_capture_source(source)
        )
        loop = getattr(self, "_loop", None)
        if not loop or not self.session:
            self.ui.set_vision_state("", "error", "VOICE SESSION OFFLINE")
            self.ui.write_log("ERR: Live Vision needs an active voice session.")
            return
        loop.call_soon_threadsafe(self._activate_live_vision, source, True)

    def stop_live_vision(self) -> None:
        """Thread-safe UI/tool entrypoint. Stops frames, not the voice chat."""
        loop = getattr(self, "_loop", None)
        if loop:
            loop.call_soon_threadsafe(self._deactivate_live_vision, "", False)
        else:
            self.ui.stop_camera_stream()
            self.ui.set_vision_state("", "off", "VISION OFF")

    def pause_live_vision(self, paused: bool) -> None:
        """Thread-safe pause/resume that never ends the voice conversation."""
        loop = getattr(self, "_loop", None)
        if loop:
            loop.call_soon_threadsafe(self._set_live_vision_paused, bool(paused))

    def _set_live_vision_paused(self, paused: bool) -> None:
        if not self._live_vision_source or self._live_vision_paused == paused:
            return
        self._live_vision_paused = paused
        self._live_vision_epoch += 1
        self._live_vision_latest = None
        self._live_vision_fingerprint = None
        self._live_vision_last_sent_at = 0.0
        if self._live_vision_source == "camera":
            if paused:
                self.ui.stop_camera_stream()
            else:
                self.ui.start_camera_stream()
        if paused:
            self.ui.set_vision_state(
                self._live_vision_source, "paused", "VISION PAUSED · VOICE ACTIVE"
            )
            self.ui.write_log("SYS: Live Vision paused. Voice is still active.")
            print("[VISION LIVE] ⏸ paused", flush=True)
        else:
            self._live_vision_announced = False
            self.ui.set_vision_state(
                self._live_vision_source, "starting", "RESUMING VISION…"
            )
            self.ui.write_log("SYS: Live Vision resuming.")
            print("[VISION LIVE] ▶ resumed", flush=True)

    def _activate_live_vision(self, source: str, greet: bool = True) -> None:
        """Apply a source change on the Live session's asyncio thread."""
        source = "camera" if source == "camera" else _normalise_capture_source(source)
        previous = self._live_vision_source
        if previous == source:
            return
        if previous == "camera":
            self.ui.stop_camera_stream()

        self._live_vision_epoch += 1
        self._live_vision_source = source
        self._live_vision_latest = None
        self._live_vision_announced = False
        self._live_vision_should_greet = bool(greet)
        self._live_vision_frames = 0
        self._live_vision_paused = False
        self._live_vision_fingerprint = None
        self._live_vision_last_sent_at = 0.0
        self._live_vision_skipped = 0
        self._live_vision_label = (
            "Camera" if source == "camera" else describe_capture_source(source)
        )
        detail = "OPENING CAMERA…" if source == "camera" else "CAPTURING SCREEN…"
        self.ui.set_vision_state(source, "starting", detail)
        self.ui.write_log(
            "SYS: Live Vision starting — "
            + ("camera." if source == "camera" else f"{self._live_vision_label}.")
        )
        print(f"[VISION LIVE] ▶ source={source} epoch={self._live_vision_epoch}", flush=True)
        if source == "camera":
            # One owner opens the device: the UI worker supplies both the fluid
            # local preview and the one-frame-per-second model feed.
            self.ui.start_camera_stream()

    def _deactivate_live_vision(self, detail: str = "", failed: bool = False) -> None:
        """Stop the current source immediately while leaving Voice connected."""
        previous = self._live_vision_source
        self._live_vision_epoch += 1
        self._live_vision_source = ""
        self._live_vision_latest = None
        self._live_vision_announced = False
        self._live_vision_should_greet = False
        self._live_vision_paused = False
        self._live_vision_fingerprint = None
        self._live_vision_last_sent_at = 0.0
        self._live_vision_label = ""
        if previous == "camera":
            self.ui.stop_camera_stream()

        if failed:
            message = detail or "VISION SOURCE FAILED"
            self.ui.set_vision_state("", "error", message)
            self.ui.write_log(f"ERR: Live Vision stopped — {message}")
            print(f"[VISION LIVE] ⛔ {message}", flush=True)
        else:
            self.ui.set_vision_state("", "off", "VISION OFF")
            if previous:
                self.ui.write_log("SYS: Live Vision stopped. Voice is still active.")
                print(f"[VISION LIVE] ■ stopped source={previous}", flush=True)

    def _on_live_camera_frame(self, data: bytes) -> None:
        """Receive the UI camera worker's rate-limited JPEG without blocking it."""
        if self._live_vision_source != "camera" or not data:
            return
        # A tuple assignment is atomic under CPython. Keeping only the newest
        # frame prevents latency/backlog if the network stalls.
        self._live_vision_latest = (
            bytes(data), "image/jpeg", time.monotonic(), self._live_vision_epoch
        )

    def _on_live_camera_error(self, message: str) -> None:
        if self._live_vision_source != "camera":
            return
        loop = getattr(self, "_loop", None)
        if loop:
            loop.call_soon_threadsafe(
                self._deactivate_live_vision,
                str(message)[:100] or "CAMERA UNAVAILABLE",
                True,
            )

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

    def _emit_learning_event(self, name: str, payload: dict | None = None) -> None:
        dashboard = self._dashboard
        loop = self._loop
        if not dashboard or not loop:
            return
        message = {"type": name, **(payload or {})}
        try:
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(dashboard.broadcast(message), loop)
        except Exception:
            pass

    def _learning_snapshot(self) -> dict:
        snapshot = self._learning.snapshot()
        selected_provider = get_learning_voice_provider()
        snapshot["providers"] = [
            {
                "id": "gemini-live", "label": "Gemini Live",
                "state": "ready" if bool(get_gemini_key()) else "missing",
                "detail": "Motor de voz disponible" if selected_provider == "gemini" else "Disponible como alternativa",
            },
            {
                "id": "openai-realtime", "label": "OpenAI · Sol (Shimmer)",
                "state": "ready" if bool(get_openai_key()) else "missing",
                "detail": "Motor de voz disponible" if get_openai_key() else "Añade la API key en Lumina",
            },
            *self._learning_service.provider_status(),
        ]
        snapshot["recordings"] = {"available": self._learning_recordings.available}
        snapshot["voiceProvider"] = selected_provider
        snapshot["voiceProviders"] = {
            "gemini": bool(get_gemini_key()),
            "openai": bool(get_openai_key()),
        }
        snapshot["openaiVoice"] = get_openai_voice()
        return snapshot

    def _broadcast_learning(self, message: dict) -> None:
        if not self._dashboard or not self._loop:
            return
        try:
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is self._loop:
                self._loop.create_task(self._dashboard.broadcast(message))
            else:
                asyncio.run_coroutine_threadsafe(
                    self._dashboard.broadcast(message), self._loop
                )
        except Exception:
            pass

    def _broadcast_learning_snapshot(self) -> None:
        """Safe from any thread: the engines call it when they become ready."""
        self._broadcast_learning({"type": "learning.snapshot", "state": self._learning_snapshot()})

    def _on_learning_english_clicked(self) -> None:
        """Qt-thread entrypoint. Scheduling keeps the desktop UI responsive."""
        if not self._loop or not self._loop.is_running():
            self.ui.write_log("SYS: Learning English is waiting for Lumina to connect.")
            return
        asyncio.run_coroutine_threadsafe(
            self._enter_learning_english(trigger="click", open_browser=True),
            self._loop,
        )

    async def _learning_studio_ready(self) -> bool:
        """Start the local web server when needed and wait for it. False means
        the studio cannot open; the log and the studio have already been told."""
        if not self._dashboard:
            self.ui.write_log("ERR: English Learning Studio needs the local dashboard.")
            self._broadcast_learning({
                "type": "learning.error",
                "message": "El servidor web de Lumina no está disponible.",
            })
            return False
        if self._dashboard_task is None or self._dashboard_task.done():
            self._dashboard_task = asyncio.create_task(
                self._dashboard.serve(), name="lumina-dashboard-lifecycle"
            )
        if not await self._dashboard.wait_until_ready(timeout=10.0):
            detail = self._dashboard.startup_error or "unknown startup error"
            self.ui.write_log(f"ERR: English Learning Studio could not start: {detail}")
            self._broadcast_learning({
                "type": "learning.error",
                "message": "El servidor web local de Lumina no pudo iniciar.",
            })
            return False
        return True

    async def _open_learning_browser(self) -> None:
        if not await self._learning_studio_ready():
            return
        url = self._dashboard.get_learning_url(local=True)
        opened = await asyncio.to_thread(webbrowser.open, url, new=2)
        if not opened:
            self.ui.write_log(f"SYS: Open Learning English manually: {url}")

    async def _enter_learning_english(
        self, *, trigger: str = "ui", open_browser: bool = True
    ) -> dict:
        if self._learning.active:
            if open_browser:
                await self._open_learning_browser()
            return self._learning_snapshot()

        # The studio is checked before anything changes: without it there is no
        # class to hand the microphone to.
        if open_browser and not await self._learning_studio_ready():
            self.speak(
                "[SYSTEM_ALERT] Tell the user briefly that Learning English could not "
                "open, so you are still their general assistant."
            )
            return self._learning_snapshot()

        # The class has its own Gemini session in the browser. This one keeps
        # running, tools and all; it only stops hearing and speaking until the
        # user presses "Volver a Lumina".
        self.interrupt()
        if self._live_vision_source:
            self.stop_live_vision()
        self._learning.enter(trigger)
        # LanguageTool and OpenPronounce warm up in their own processes while the
        # tutor connects; the studio hears when each one is ready.
        self._learning_service.start_engines(on_change=self._broadcast_learning_snapshot)
        self._learning_confirmation_pending = False
        self._learning_confirmation_prompt_pending = False
        self.ui.write_log(
            "SYS: Learning English is open in your browser — Lumina's microphone "
            "is paused until you press Volver a Lumina."
        )
        self._broadcast_learning({
            "type": "learning.snapshot", "state": self._learning_snapshot()
        })
        if open_browser:
            await self._open_learning_browser()
        return self._learning_snapshot()

    async def _exit_learning_english(self, *, trigger: str = "ui") -> dict:
        if not self._learning.active:
            return self._learning_snapshot()
        if self._learning_analysis_lock is not None:
            async with self._learning_analysis_lock:
                completed = self._learning.complete()
        else:
            completed = self._learning.complete()
        await asyncio.get_running_loop().run_in_executor(None, self._learning_service.stop_engines)
        self._broadcast_learning({
            "type": "learning.snapshot", "state": self._learning_snapshot()
        })
        self.ui.write_log(f"SYS: Learning English ended ({trigger}) — Lumina's microphone is back.")
        self.speak(
            "[SYSTEM_ALERT] Learning English terminó y el progreso quedó guardado. "
            f"Di una despedida de una frase. Resumen: {completed.get('lastSummary', '')}"
        )
        return self._learning_snapshot()

    async def _learning_live_session(self, handle: str = "") -> dict:
        """Describe/connect the browser tutor without exposing either API key."""
        if not self._learning.active:
            raise RuntimeError("Learning English is not active")
        snapshot = self._learning_snapshot()
        if get_learning_voice_provider() == "openai":
            return self._learning_service.openai_session_description(
                snapshot,
                assistant_name=self._asst_name,
                user_name=self._user_name,
                voice_name=get_openai_voice(),
            )
        session = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: self._learning_service.create_live_session(
                snapshot,
                assistant_name=self._asst_name,
                user_name=self._user_name,
                voice_name=get_voice(),
                resume_handle=handle or None,
            ),
        )
        if not handle:
            self._learning.session_ready()
            self._broadcast_learning({
                "type": "learning.snapshot", "state": self._learning_snapshot()
            })
        return session

    async def _learning_openai_call(self, sdp: str) -> dict:
        """Server-side WebRTC handshake; the OpenAI key never reaches JavaScript."""
        if not self._learning.active:
            raise RuntimeError("Learning English is not active")
        if get_learning_voice_provider() != "openai":
            raise RuntimeError("Learning English is currently using Gemini")
        if not sdp or len(sdp) > 100_000:
            raise ValueError("Invalid WebRTC offer")
        snapshot = self._learning_snapshot()
        answer = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: self._learning_service.create_openai_call(
                snapshot,
                assistant_name=self._asst_name,
                user_name=self._user_name,
                voice_name=get_openai_voice(),
                sdp=sdp,
            ),
        )
        self._learning.session_ready()
        self._broadcast_learning({
            "type": "learning.snapshot", "state": self._learning_snapshot()
        })
        return {"sdp": answer}

    async def _learning_turn(self, user_text: str, assistant_text: str) -> dict:
        """A finished turn from the browser tutor, analysed for the studio."""
        if not self._learning.active:
            raise RuntimeError("Learning English is not active")
        asyncio.create_task(self._process_learning_turn(user_text, assistant_text))
        return self._learning_snapshot()

    async def _learning_voice(self, pcm: bytes, sample_rate: int, details: dict) -> dict:
        """One spoken student turn: scored when there is a phrase to compare it
        with and OpenPronounce runs, and saved to Supabase unless turned off."""
        if not self._learning.active:
            raise RuntimeError("Learning English is not active")
        expected = str(details.get("expected_text") or "")
        result = None
        if expected:
            result = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self._learning_service.analyze_pronunciation(pcm, sample_rate, expected),
            )
        snapshot = self._learning_snapshot()
        saved = False
        if (snapshot.get("privacy") or {}).get("save_recordings", True):
            saved = self._learning_recordings.save(pcm, sample_rate, {
                "session_id": snapshot.get("current_session_id", ""),
                "audience": (snapshot.get("catalog") or {}).get("audience", ""),
                "level": (snapshot.get("profile") or {}).get("level", ""),
                "mode": snapshot.get("currentMode", ""),
                "unit_id": (snapshot.get("currentUnit") or {}).get("id", ""),
                "scenario_id": (snapshot.get("activeScenario") or {}).get("id", ""),
                "transcript": details.get("transcript", ""),
                "tutor_text": details.get("tutor_text", ""),
                "expected_text": expected,
                "pronunciation_score": (result or {}).get("score"),
                "pronunciation": result,
            })
        return {"pronunciation": result, "saved": saved}

    async def _learning_placement(self, answers: list) -> dict:
        """One step of the studio's written placement test."""
        if not self._learning.active:
            raise RuntimeError("Learning English is not active")
        step = self._learning.placement_step(answers)
        snapshot = self._learning_snapshot()
        if step["done"]:
            self._broadcast_learning({"type": "learning.snapshot", "state": snapshot})
        return {"step": step, "state": snapshot}

    async def _learning_activity(self, op: str, body: dict) -> dict:
        """Create a generated checkpoint or reading, or grade the answers to one."""
        if not self._learning.active:
            raise RuntimeError("Learning English is not active")
        if op == "submit":
            result = self._learning.submit_activity(
                str(body.get("activity_id") or ""), body.get("answers")
            )
            snapshot = self._learning_snapshot()
            self._broadcast_learning({"type": "learning.snapshot", "state": snapshot})
            return {"result": result, "state": snapshot}
        kind = str(body.get("kind") or "")
        snapshot = self._learning_snapshot()
        try:
            activity = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._learning_service.generate_activity(kind, snapshot)
            )
        except ValueError as exc:
            # Unusable content; the provider's text is never logged.
            print(f"[Learning English] Activity rejected ({type(exc).__name__}: {exc})")
            raise RuntimeError(
                "Lumina no pudo preparar la actividad. Inténtalo de nuevo."
            ) from None
        except Exception as exc:
            print(f"[Learning English] Activity generation failed ({type(exc).__name__})")
            # The analysis of every spoken turn shares this model's quota.
            if "RESOURCE_EXHAUSTED" in str(exc) or "429" in str(exc):
                raise RuntimeError(
                    "Gemini llegó a su límite de uso por ahora. Espera un minuto y vuelve a intentarlo."
                ) from None
            raise RuntimeError(
                "Lumina no pudo preparar la actividad. Inténtalo de nuevo."
            ) from None
        return {"activity": self._learning.register_activity(activity)}

    async def _learning_web_action(self, action: str, body: dict) -> dict:
        if action == "enter":
            return await self._enter_learning_english(trigger="web", open_browser=False)
        if action == "exit":
            return await self._exit_learning_english(trigger="web")
        if action in {"pause", "resume"}:
            self._learning.set_paused(action == "pause")
        elif action in {"mute", "unmute"}:
            self._learning.input_muted = action == "mute"
        elif action == "translation":
            self._learning.translation_enabled = bool(body.get("enabled", True))
        elif action == "mode":
            self._learning.set_mode(str(body.get("mode") or ""))
        elif action == "scenario":
            self._learning.select_scenario(str(body.get("scenario_id") or ""))
        elif action == "scenario-end":
            self._learning.end_scenario()
        elif action == "unit":
            self._learning.select_unit(str(body.get("unit_id") or ""))
        elif action == "listening-activity":
            self._learning.select_listening_activity(
                str(body.get("activity_id") or "")
            )
        elif action == "save-word":
            self._learning.store.save_word(
                str(body.get("word") or ""),
                str(body.get("meaning") or ""),
                str(body.get("example") or ""),
            )
        elif action == "review-word":
            self._learning.review_word(
                str(body.get("word") or ""), str(body.get("rating") or "")
            )
        elif action == "audience":
            self._learning.set_audience(str(body.get("audience") or ""))
        elif action == "delete-recordings":
            removed = await asyncio.get_running_loop().run_in_executor(
                None, self._learning_recordings.delete_all
            )
            self.ui.write_log(f"SYS: Learning English deleted {removed} saved voice turns.")
        elif action == "settings":
            preferred = str(body.get("preferred_speed") or "normal")
            if preferred not in {"slow", "normal", "natural"}:
                raise ValueError("Invalid preferred speed")
            if "audience" in body:
                self._learning.set_audience(str(body.get("audience") or ""))
            if "weekly_target" in body:
                self._learning.store.set_weekly_target(body.get("weekly_target"))
            if "save_recordings" in body:
                self._learning.store.set_save_recordings(bool(body.get("save_recordings")))
            if "voice_provider" in body:
                provider = str(body.get("voice_provider") or "").lower()
                if provider not in {"gemini", "openai"}:
                    raise ValueError("Invalid voice provider")
                if provider == "openai" and not get_openai_key():
                    raise RuntimeError(
                        "Añade primero tu OpenAI API key en Lumina > API Keys."
                    )
                save_voice_settings(learning_provider=provider)
            self._learning.translation_enabled = bool(body.get("translation_enabled", True))
            self._learning.store.update_profile({"preferred_speed": preferred})
        snapshot = self._learning_snapshot()
        self._broadcast_learning({"type": "learning.snapshot", "state": snapshot})
        return snapshot

    async def _process_learning_turn(self, user_text: str, assistant_text: str) -> None:
        if not user_text and not assistant_text:
            return
        turn_key = hash((user_text, assistant_text))
        now = time.monotonic()
        if any(key == turn_key and now - seen_at < 5 for key, seen_at in self._learning_seen_turns):
            return
        self._learning_seen_turns.append((turn_key, now))
        if self._learning_analysis_lock is None:
            self._learning_analysis_lock = asyncio.Lock()
        async with self._learning_analysis_lock:
            snapshot = self._learning_snapshot()
            response = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self._learning_service.analyze_turn(
                    user_text=user_text,
                    assistant_text=assistant_text,
                    snapshot=snapshot,
                ),
            )
            # A user can leave while structured analysis is in flight. Preserve the
            # finished turn, but never resurrect an inactive browser session.
            if self._learning.active:
                state = self._learning.apply_turn(user_text, response)
                self._broadcast_learning({
                    "type": "learning.turn", "response": response.to_dict()
                })
                self._broadcast_learning({"type": "learning.snapshot", "state": state})
                if state.get("storageStatus") == "temporary":
                    self._broadcast_learning({
                        "type": "learning.error",
                        "message": state.get("storageError") or "El progreso se guardará temporalmente.",
                    })

    def _on_text_command(self, text: str):
        # Before the session check, deliberately: typing the wake phrase is the
        # last resort when the session is gone, and the old early return made
        # that exact case do nothing at all.
        if _is_wake_phrase(text, self._asst_name):
            self.wake(text)
            return

        intent = detect_learning_english_intent(text, active=self._learning.active)
        if self._learning_confirmation_pending:
            answer = confirmation_answer(text)
            if answer is not None:
                self._learning_confirmation_pending = False
                if answer:
                    if self._loop:
                        asyncio.run_coroutine_threadsafe(
                            self._enter_learning_english(trigger="confirmation", open_browser=True),
                            self._loop,
                        )
                else:
                    self.speak("[SYSTEM_ALERT] Confirma brevemente que Learning English no se activará.")
                return
        if intent.action == "enter" and intent.confidence >= 0.9:
            if self._loop:
                asyncio.run_coroutine_threadsafe(
                    self._enter_learning_english(trigger="text", open_browser=True), self._loop
                )
            return
        if intent.action == "exit" and intent.confidence >= 0.9:
            if self._loop:
                asyncio.run_coroutine_threadsafe(
                    self._exit_learning_english(trigger="text"), self._loop
                )
            return
        if intent.action == "enter" and intent.confidence >= 0.6:
            self._learning_confirmation_pending = True
            self.speak(
                "[SYSTEM_ALERT] Pregunta solamente: '¿Quieres que active Learning English y abra el estudio?'"
            )
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
            self._send_text_turn(text),
            self._loop
        )

    @property
    def _mic_open_while_speaking(self) -> bool:
        """Headphones, or speakers with an echo canceller that has proven it
        removes her voice in this answer: the microphone need not close."""
        return self._full_duplex or (self._echo is not None and self._echo.open)

    def _echo_opened(self, echo: EchoCanceller) -> None:
        """The canceller has proven itself on this answer, so the microphone
        stays open for the rest of it. Called from the microphone thread."""
        if not self._echo_announced:
            self._echo_announced = True
            print(f"[JARVIS] 🔊 Echo cancellation measured at {echo.reduction_db():.0f} dB — "
                  "the microphone opens while she speaks; you can interrupt her",
                  flush=True)
            self.ui.write_log("SYS: Echo cancellation ready — you can interrupt me by speaking.")
        self._refresh_mic_state()

    def _note_audio_latency(self, *, mic=None, speaker=None) -> None:
        """Tell the echo canceller how late her voice comes back: the
        speaker's latency plus the microphone's."""
        if mic is not None:
            self._mic_latency = float(getattr(mic, "latency", 0.0) or 0.0)
        if speaker is not None:
            self._speaker_latency = float(getattr(speaker, "latency", 0.0) or 0.0)
        if self._echo is not None:
            self._echo.set_delay(self._mic_latency + self._speaker_latency)

    def _refresh_mic_state(self) -> None:
        """Tell the HUD what she is doing and whether the microphone is live.

        Two different questions, and conflating them is what made every
        silence ambiguous. She is deaf while speaking through speakers without
        echo cancellation, but not on headphones, and not while waiting on another
        assistant — that audio still reaches the model, it simply cannot be
        answered until the tool returns. The button says which, so the user
        never has to wonder whether talking is worth it.
        """
        if self._tool_running:
            reason = "WORKING"
        elif self._is_speaking:
            reason = "SPEAKING"
        else:
            reason = ""
        try:
            self.ui.set_busy(reason, self._mic_open_while_speaking or not self._is_speaking)
        except Exception:
            pass

    def set_speaking(self, value: bool):
        with self._speaking_lock:
            self._is_speaking = value
        if not value:
            self._last_spoke_at = time.monotonic()
        if value:
            self.ui.set_state("SPEAKING")
        elif not self.ui.muted:
            self.ui.set_state("LISTENING")
        self._refresh_mic_state()

    def _drain_playback(self) -> int:
        """Discard her queued audio; returns how many chunks were dropped."""
        q = self.audio_in_queue
        drained = 0
        while q is not None:
            try:
                q.get_nowait()
            except Exception:
                break
            drained += 1
        return drained

    def interrupt(self) -> None:
        """Stop JARVIS mid-speech: drain queued audio and open mic immediately."""
        self._interrupted = True
        with self._speaking_lock:
            was_speaking = self._is_speaking
        cancel = getattr(self.session, "cancel_response", None)
        if was_speaking and cancel and self._loop:
            try:
                asyncio.run_coroutine_threadsafe(cancel(), self._loop)
            except Exception:
                pass
        drained = self._drain_playback()
        if drained:
            print(f"[JARVIS] ✋ Interrupted — {drained} audio chunks discarded")
        self.set_speaking(False)
        if self._turn_done_event:
            self._turn_done_event.clear()
        self.ui.write_log("SYS: Interrupted — listening...")

    async def _send_text_turn(self, text: str) -> None:
        """One text turn for whichever model is live.

        Gemini 3.1 Flash Live accepts send_client_content only to seed the start
        of a session; mid-conversation it takes text as realtime input. The
        OpenAI session maps client content itself and keeps it."""
        session = self.session
        if isinstance(session, OpenAIRealtimeSession):
            await session.send_client_content(
                turns={"parts": [{"text": text}]}, turn_complete=True,
            )
        else:
            await session.send_realtime_input(text=text)

    def speak(self, text: str):
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(
            self._send_text_turn(text),
            self._loop
        )

    def speak_error(self, tool_name: str, error: str):
        short = str(error)[:120]
        self.ui.write_log(f"ERR: {tool_name} — {short}")
        self.speak(f"Sir, {tool_name} encountered an error. {short}")

    def _build_system_instruction(self) -> str:
        """Shared identity, time, memory and operating rules for either provider."""
        from datetime import datetime

        # Load customization from config
        try:
            _cfg = load_api_keys()
            self._asst_name = (_cfg.get("assistant_name") or "LUMINA").strip()
            _user_name = (_cfg.get("user_name") or "").strip()
        except Exception:
            self._asst_name = "LUMINA"
            _user_name = ""
        self._user_name = _user_name or "You"

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

        return "\n".join(parts)

    def _all_tool_declarations(self) -> list[dict]:
        return TOOL_DECLARATIONS + self._plugin_registry.get_tool_declarations()

    def _build_config(self) -> types.LiveConnectConfig:
        """Gemini's proven Live configuration. Keep this path provider-local."""
        cfg = dict(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            # No realtime_input_config. The upstream project this is forked
            # from does not set one, and setting one is what stopped her
            # hearing: turn_coverage=TURN_INCLUDES_ONLY_ACTIVITY admits only
            # the audio the server's own detector marks as activity and
            # discards the rest, so a detection that misfires throws the
            # user's whole sentence away — no transcript, no answer, for the
            # life of the session. Measured on one such session: thirty-one
            # utterances captured and sent, zero transcriptions returned.
            # The service's defaults handle turn-taking perfectly well.
            system_instruction=self._build_system_instruction(),
            # Hand back the handle captured from the last session_resumption
            # update. `handle=None` is exactly the old behaviour (ask for
            # handles, start fresh), so the first connect of a run is unchanged.
            session_resumption=types.SessionResumptionConfig(
                handle=self._resume_handle
            ),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=get_voice()
                    )
                )
            ),
        )

        cfg["tools"] = [{"function_declarations": self._all_tool_declarations()}]

        # Sliding-window compression: the session never dies of a full context
        # window, so one conversation can run for hours. Added here rather than
        # in the dict above so a measured run can leave it out entirely — see
        # _COMPRESSION_ON. Leaving it out is upstream's configuration.
        if _COMPRESSION_ON:
            cfg["context_window_compression"] = types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow(),
            )
        return types.LiveConnectConfig(**cfg)

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        # A running tool is a legitimate reason for the session to transcribe
        # nothing: the model is inside the call and not listening. The deafness
        # watchdog cannot tell that apart from a session that has stopped
        # hearing, and it tore one down in the middle of a Copilot query that
        # had another twenty seconds to run — losing the answer. So it is told.
        self._tool_running += 1
        self._refresh_mic_state()
        try:
            return await self._dispatch_tool(fc)
        finally:
            self._tool_running -= 1
            # Speech from while the tool was busy is not evidence of anything.
            self._voice_blocks_unheard = 0
            self._last_user_speech = time.monotonic()
            self._refresh_mic_state()

    async def _answer_tool_calls(self, session, calls) -> None:
        """Run one batch of tool calls beside the conversation and answer it.

        The receive loop used to await each tool itself, so for as long as a
        search, a file or another assistant took, nothing from the model was
        read: no transcript and no voice. It now keeps reading while this runs.
        """
        self._tool_calls_open += 1
        try:
            responses = [await self._execute_tool(fc) for fc in calls]
            if self.session is not session:
                print(f"[JARVIS] 📤 {len(responses)} tool result(s) dropped — "
                      "their session has closed", flush=True)
                return
            try:
                await session.send_tool_response(function_responses=responses)
            except Exception as exc:
                print(f"[JARVIS] ⛔ tool result not sent: {type(exc).__name__}: {exc}",
                      flush=True)
        finally:
            self._tool_calls_open -= 1

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
                angle = "camera" if args.get("angle", "screen").lower() == "camera" else "screen"
                if self._live_vision_source == angle:
                    result = (
                        f"[VISION_ALREADY_ACTIVE] Live {angle} frames are already available. "
                        "Answer the user's question directly from the newest visual context. "
                        "Do not call screen_process again."
                    )
                elif self._vision_busy or (_now - self._vision_last_time) < _cooldown:
                    _wait = max(0, _cooldown - (_now - self._vision_last_time))
                    print(f"[Vision] ⏳ Cooldown active ({_wait:.1f}s remaining) — ignoring duplicate call")
                    result = "Vision is still processing the previous request. I will not call this again."
                else:
                    self._vision_busy      = True
                    self._vision_last_time = _now
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
                if self._live_vision_source:
                    self._deactivate_live_vision()
                    result = "Live Vision stopped. The voice conversation remains active."
                else:
                    self.ui.stop_camera_stream()
                    result = "Camera closed."

            elif name == "vision_highlight":
                source = self._live_vision_source
                if not source or source == "camera" or self._live_vision_paused:
                    result = (
                        "A display or application window must be actively shared "
                        "before I can show a visual pointer."
                    )
                else:
                    try:
                        x = max(0, min(1000, int(args.get("x", 500))))
                        y = max(0, min(1000, int(args.get("y", 500))))
                    except (TypeError, ValueError):
                        x, y = 500, 500
                    label = str(args.get("label") or "Click here")[:48]
                    self.ui.show_vision_highlight(source, x, y, label)
                    result = (
                        f"Pointer shown at ({x}, {y}) on the shared source. "
                        "It does not click or control the computer."
                    )

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
                            await self._send_text_turn("Say a brief natural goodbye to the user.")
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
        # Stamped per session, because _sent_blocks counts for the life of the
        # process and the line below needs a rate, not a total.
        _began = time.monotonic()
        _began_at = self._sent_blocks
        while True:
            msg = await self.out_queue.get()
            # Gemini 3.1 Flash Live takes the microphone only as audio=, and
            # closes the session on the media chunks 2.5 native audio needed
            # (1007 "media_chunks is deprecated"). The OpenAI session reads its
            # own media= entry.
            #
            # Nothing else goes down this channel. A client-side detector used
            # to send audio_stream_end after every pause of 640 ms, to hurry
            # the turn along, and that is what left sessions deaf for their
            # whole life: audio_stream_end tells the server the audio stream
            # is over, the room's own noise floor measured 0.23 against the
            # detector's 0.20 threshold, and one session announced the end of
            # its audio thirty-one times while transcribing nothing at all.
            # The service's default activity detection does not need the help.
            try:
                if isinstance(self.session, OpenAIRealtimeSession):
                    await self.session.send_realtime_input(media=msg)
                else:
                    await self.session.send_realtime_input(
                        audio=types.Blob(data=msg["data"], mime_type=msg["mime_type"])
                    )
            except Exception as exc:
                # This task is the only path audio has to the model. If it
                # dies the microphone keeps working, the meters keep moving
                # and nothing is ever transcribed again — the exact shape of
                # the failure being chased, and until now the one link in the
                # chain that reported nothing at all.
                print(f"[JARVIS] ⛔ audio send failed: {type(exc).__name__}: {exc}",
                      flush=True)
                raise
            self._sent_blocks += 1
            if self._sent_blocks % 1000 == 0:      # about once a minute
                # Both clocks, because one of them alone says nothing. The
                # seconds of audio are arithmetic on the block count and come
                # out the same whether the stream is live or an hour behind;
                # only the wall clock beside them can say which. A minute of
                # audio delivered in a minute is a live microphone. A minute of
                # audio delivered in two is a model answering the past, and it
                # reads as "she got slow" rather than as the backlog it is.
                _audio = ((self._sent_blocks - _began_at) * CHUNK_SIZE
                          / self._input_sample_rate)
                _wall = time.monotonic() - _began
                print(f"[JARVIS] ⇢ {self._sent_blocks} audio blocks sent "
                      f"({_audio:.0f}s of audio in {_wall:.0f}s)",
                      flush=True)

    async def _run_live_vision(self) -> None:
        """Send the explicitly selected visual source to the main Live session.

        Gemini's documented ceiling is one video frame per second. Screen
        capture runs off the event loop; the camera preview worker supplies its
        newest JPEG. There is intentionally no backlog of stale visual frames.
        """
        print("[VISION LIVE] Stream worker ready", flush=True)
        last_camera_stamp = 0.0
        observed_epoch = -1

        while True:
            source = self._live_vision_source
            epoch = self._live_vision_epoch
            if not source or self._live_vision_paused:
                await asyncio.sleep(0.10)
                continue
            if epoch != observed_epoch:
                observed_epoch = epoch
                last_camera_stamp = 0.0

            frame_started = time.monotonic()
            try:
                if source != "camera":
                    img_bytes, mime_type = await asyncio.to_thread(
                        _capture_screen, source
                    )
                else:
                    latest = self._live_vision_latest
                    if not latest:
                        await asyncio.sleep(0.05)
                        continue
                    img_bytes, mime_type, stamp, frame_epoch = latest
                    if frame_epoch != epoch or stamp <= last_camera_stamp:
                        await asyncio.sleep(0.05)
                        continue
                    last_camera_stamp = stamp

                # A stop or source switch while capture was running invalidates
                # the frame before it can leave the machine.
                if (source != self._live_vision_source
                        or epoch != self._live_vision_epoch
                        or self._live_vision_paused):
                    continue

                fingerprint = await asyncio.to_thread(frame_fingerprint, img_bytes)
                change = frame_change_score(
                    self._live_vision_fingerprint, fingerprint
                )
                now = time.monotonic()
                threshold = (
                    _VISION_CAMERA_CHANGE if source == "camera"
                    else _VISION_SCREEN_CHANGE
                )
                keyframe_due = (
                    not self._live_vision_last_sent_at
                    or now - self._live_vision_last_sent_at >= _VISION_KEYFRAME_SECS
                )
                if self._live_vision_fingerprint is not None and not keyframe_due:
                    if change < threshold:
                        self._live_vision_skipped += 1
                        if self._live_vision_skipped % 30 == 0:
                            print(
                                f"[VISION LIVE] smart capture skipped "
                                f"{self._live_vision_skipped} unchanged frames",
                                flush=True,
                            )
                        elapsed = time.monotonic() - frame_started
                        await asyncio.sleep(max(0.05, 1.0 - elapsed))
                        continue

                if (source != self._live_vision_source
                        or epoch != self._live_vision_epoch
                        or self._live_vision_paused):
                    continue

                await self.session.send_realtime_input(
                    video=types.Blob(data=img_bytes, mime_type=mime_type)
                )
                self._live_vision_frames += 1
                self._live_vision_fingerprint = fingerprint
                self._live_vision_last_sent_at = time.monotonic()

                if not self._live_vision_announced:
                    self._live_vision_announced = True
                    kind = (
                        "CAMERA" if source == "camera"
                        else ("WINDOW" if source.startswith("window:") else "DISPLAY")
                    )
                    detail = f"SHARING {kind} · SMART · VOICE ACTIVE"
                    self.ui.set_vision_state(source, "active", detail)
                    self.ui.write_log(
                        "SYS: Live Vision active — ask follow-up questions by voice."
                    )
                    print(
                        f"[VISION LIVE] ● active source={source} "
                        f"frame={len(img_bytes):,} bytes",
                        flush=True,
                    )
                    if self._live_vision_should_greet:
                        self._live_vision_should_greet = False
                        source_label = (
                            "camera" if source == "camera" else self._live_vision_label
                        )
                        await self._send_text_turn(
                                f"[VISION_SESSION_STARTED] The user explicitly enabled live "
                                f"{source_label} sharing from the interface. You can now see "
                                f"fresh frames continuously. In one short sentence in the "
                                f"user's language, confirm that Vision is active and ask what "
                                f"they want help with. Do not call screen_process."
                            )
                elif self._live_vision_frames % 15 == 0:
                    print(
                        f"[VISION LIVE] ⇢ {self._live_vision_frames} frames sent "
                        f"(source={source}, skipped={self._live_vision_skipped})",
                        flush=True,
                    )

            except Exception as exc:
                if source == self._live_vision_source and epoch == self._live_vision_epoch:
                    self._deactivate_live_vision(
                        f"{type(exc).__name__}: {str(exc)[:80]}", failed=True
                    )
                await asyncio.sleep(0.25)
                continue

            elapsed = time.monotonic() - frame_started
            await asyncio.sleep(max(0.05, 1.0 - elapsed))

    async def _listen_audio(self):
        print("[JARVIS] 🎤 Mic started")
        loop = asyncio.get_event_loop()
        local_vad = _LocalVoiceActivityDetector()

        def enqueue_realtime(message: dict) -> None:
            """Enqueue from the audio thread without allowing callback errors."""
            try:
                self.out_queue.put_nowait(message)
            except asyncio.QueueFull:
                # Best effort: blocking PortAudio's callback to wait for room
                # would tear a hole in the captured stream, which is worse
                # than losing the block that would not fit.
                #
                # Counted as well as logged. This queue holds thirteen seconds
                # of audio, so it can only fill if the send task stopped
                # draining it — and until now that happened behind a diagnostic
                # switch that is off by default, which is to say invisibly.
                self._mic_dropped += 1
                _diag("microphone queue full; dropping one audio block")

        def callback(indata, frames, time_info, status):
            # Stamped on every block, before any gate: this is proof the device
            # is still delivering, which is a different question from whether we
            # are currently forwarding what it delivers.
            self._last_mic_block = time.monotonic()
            # Counted here for the same reason the stamp above is taken here:
            # "the device stopped delivering" and "we stopped forwarding what it
            # delivers" are different faults with one symptom.
            self._mic_blocks += 1
            with self._speaking_lock:
                jarvis_speaking = self._is_speaking
            # Her own speech only has to close the microphone when it can come
            # back through it. On headphones it cannot, so it stays open and
            # the user can talk over her at any point — which is the whole
            # difference between a voice assistant and a monologue. With echo
            # cancellation it does come back, and is removed before it is sent.
            # While a Learning English class is open the browser tutor has the
            # student's voice; this session must not answer it too.
            echo = self._echo
            her_voice = jarvis_speaking
            data = None
            if (echo is not None and not self.ui.muted and not self._phone_active
                    and not self._learning.active):
                # The canceller hears every block, gated or not: it cannot
                # learn her echo, or prove it removes it, from blocks it never
                # sees. Her voice is removed before anything reads the block,
                # so the meters, the local detector and the model hear the user.
                her_voice = jarvis_speaking or (
                    time.monotonic() - self._last_spoke_at < echo.TAIL_SECONDS
                )
                was_open = echo.open
                data = echo.capture(indata.tobytes(), her_voice=her_voice)
                if echo.open and not was_open:
                    self._echo_opened(echo)
            if ((not her_voice or self._mic_open_while_speaking)
                    and not self.ui.muted and not self._phone_active
                    and not self._learning.active):
                if data is None:
                    data = indata.tobytes()
                    level = _pcm_level(indata)
                else:
                    level = _pcm_level(np.frombuffer(data, dtype=np.int16))
                # Loudest block the model was actually given since the last
                # record. A silence with nothing above the noise floor in it is
                # the user not talking; a silence full of clear speech is the
                # session failing, and the log could not tell them apart.
                if level > self._mic_peak:
                    self._mic_peak = level
                loop.call_soon_threadsafe(enqueue_realtime, {
                    "data": data,
                    "mime_type": "audio/pcm",       # exactly what upstream sends
                })

                vad_event = local_vad.process(level)
                if vad_event == "start":
                    self._last_voice_end = 0.0
                    # Only when the line is still empty. This fires again after
                    # every pause longer than 640 ms — which is every gap
                    # between two phrases — and writing the placeholder each
                    # time wiped the words as fast as they were transcribed,
                    # leaving "Listening..." on screen for a user who could
                    # see the assistant answering them perfectly well.
                    if not self._live_has_text:
                        self.ui.set_live_transcript(
                            f"{self._user_name}: Listening..."
                        )
                    print("[VOICE FLOW] 1/3 speech detected", flush=True)
                    _diag("local VAD: speech started")
                elif vad_event == "end":
                    # Noted for the latency line, and nothing more. This used
                    # to tell the server the audio stream had ended, which is
                    # what deafened whole sessions — see _send_realtime.
                    ended_at = time.monotonic()
                    self._last_user_speech = ended_at
                    self._last_voice_end = ended_at
                    _diag("local VAD: speech ended")

                # Counted here rather than at the top of the callback: only
                # audio that was actually sent can be evidence that sending is
                # not working.
                try:
                    if (
                        level > _DEAF_VOICE_LEVEL
                        and time.monotonic() - self._last_spoke_at > _DEAF_ECHO_TAIL
                    ):
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
                    samplerate=self._input_sample_rate,
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
                self._note_audio_latency(mic=_mic_stream)
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

                    if self.ui.muted or self._learning.active:
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

                    _spoke_seconds = (self._voice_blocks_unheard * CHUNK_SIZE
                                      / self._input_sample_rate)
                    if (
                        self._heard_this_session == 0
                        and (time.monotonic() - self._session_started) > _DEAF_MIN_SESSION
                        and _spoke_seconds > _DEAF_VOICE_SECONDS
                        and (time.monotonic() - self._last_user_speech) > _DEAF_SILENCE_SECONDS
                        and (time.monotonic() - self._last_deaf_rebuild) > _DEAF_COOLDOWN
                    ):
                        self._voice_blocks_unheard = 0
                        self._last_deaf_rebuild = time.monotonic()
                        # Both clocks restart, not just the counter. Leaving
                        # this one stale is exactly what caused the loop.
                        self._last_user_speech = time.monotonic()
                        self._deaf_rebuilds += 1

                        # The first rebuild keeps the conversation. A second
                        # one in a row does not, because by then the
                        # conversation is the suspect.
                        #
                        # Measured on 2026-09-12: four rebuilds, six connects,
                        # and not one word transcribed after the first of them.
                        # Every rebuild replayed the same resumption handle, so
                        # every rebuilt session resumed the state it was being
                        # rebuilt to escape. That is why this fault has never
                        # once recovered on its own while restarting the
                        # process always cured it — killing the process is the
                        # only thing that has ever thrown the handle away.
                        #
                        # It costs little here: this branch only runs when the
                        # session has transcribed nothing at all, so there is
                        # no conversation from it to lose, only the older one
                        # it resumed. An assistant that can hear and has
                        # forgotten the last few minutes beats one that
                        # remembers everything and is deaf.
                        _keep = self._deaf_rebuilds < 2
                        print(f"[JARVIS] 🙉 {_spoke_seconds:.0f}s of speech sent and nothing "
                              f"transcribed — rebuilding the session"
                              f"{'' if _keep else ', without resuming it'}", flush=True)
                        self.ui.write_log(
                            "SYS: I stopped hearing you — reconnecting."
                            if _keep else
                            "SYS: Still not hearing you — starting a clean session."
                        )
                        self.request_reconnect(keep_context=_keep,
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
                    self._note_audio_latency(mic=_mic_stream)
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

    def _note_rx(self, response) -> None:
        """Record that the receive stream yielded something, and what it was.

        The failure this file has been chasing is a stream that stops yielding
        while the socket stays open and audio keeps being accepted. Nothing in
        the process could see it, because only transcripts and audio were ever
        printed — and the two possibilities look identical in that log while
        meaning opposite things. A session still sending resumption updates and
        usage metadata is alive and slow, and the fault is in what we asked it
        for. A session sending nothing at all is gone, and the fault is the
        connection. Counting every message by kind is what separates them.
        """
        self._rx_at = time.monotonic()
        kinds = self._rx_kinds
        seen = 0

        def _note(kind: str) -> None:
            nonlocal seen
            seen += 1
            kinds[kind] = kinds.get(kind, 0) + 1

        if getattr(response, "data", None):
            _note("audio")

        sc = getattr(response, "server_content", None)
        if sc is not None:
            _tx = getattr(sc, "input_transcription", None)
            if _tx is not None and getattr(_tx, "text", None):
                _note("heard")
            if getattr(sc, "interim_input_transcription", None) is not None:
                _note("interim")
            _out = getattr(sc, "output_transcription", None)
            if _out is not None and getattr(_out, "text", None):
                _note("said")
            if getattr(sc, "turn_complete", None):
                _note("turn")

        if getattr(response, "tool_call", None):
            _note("tool")
        if getattr(response, "session_resumption_update", None) is not None:
            _note("resume")

        if not seen:
            # Bookkeeping messages, counted only when they are all that came.
            # They carry no transcript and no audio, so they cannot make the
            # user's speech appear — but they are proof the session is still
            # being served, which is the question that matters during a
            # silence.
            if getattr(response, "usage_metadata", None) is not None:
                _note("usage")
            else:
                _note("other")

    async def _run_voice_recorder(self) -> None:
        """Print what the voice path is doing, on a fixed cadence.

        Read one of these lines and a silence stops being a mystery:

          `rx` empty while `mic` keeps counting and `peak` is high — the user
          is speaking, the audio is leaving, and the server has stopped
          answering. Nothing local can fix that and nothing local is wrong.

          `rx` carrying `usage`/`resume` but no `heard` — the session is being
          served and is not transcribing, which points at the configuration it
          was opened with, not at the network.

          `lag` in the hundreds of milliseconds — this event loop was held by
          something else, and every other number on the line is late rather
          than true. That is a bug in this process.

          `snd` far below `mic`, or `drop` above zero — audio is being captured
          faster than it is being sent, so the model is hearing the past.

        This task only watches. It sends nothing, rebuilds nothing, and
        cancels nothing: three watchdogs have now destroyed conversations that
        were working, and an instrument that can do that is not an instrument.
        """
        _TICK = 0.25          # short enough that a real stall shows up as lag
        _EVERY = 15.0         # one line every fifteen seconds

        last_report = time.monotonic()
        last_sent = self._sent_blocks
        last_mic = self._mic_blocks
        last_drop = self._mic_dropped
        lag_max = 0.0

        while True:
            _before = time.monotonic()
            await asyncio.sleep(_TICK)
            # Overshoot on a sleep this short is the event loop being held by
            # someone else. It is measured here rather than assumed because
            # "the server went quiet" and "we stopped listening to the server"
            # produce the same empty log.
            lag_max = max(lag_max, time.monotonic() - _before - _TICK)

            now = time.monotonic()
            if now - last_report < _EVERY:
                continue

            # An instrument that can take down the thing it measures is not an
            # instrument. This task lives in the session's TaskGroup, where one
            # unhandled error ends the call, so a bad format string here must
            # cost a line of log and nothing else.
            try:
                self._record_voice(now, last_sent, last_mic, last_drop, lag_max)
            except Exception as exc:
                print(f"[VOICE REC] recorder error: {type(exc).__name__}: {exc}",
                      flush=True)

            last_report = now
            last_sent = self._sent_blocks
            last_mic = self._mic_blocks
            last_drop = self._mic_dropped
            lag_max = 0.0
            self._mic_peak = 0.0

    def _record_voice(self, now: float, last_sent: int, last_mic: int,
                      last_drop: int, lag_max: float) -> None:
        """Write one flight-recorder line. Called only by _run_voice_recorder."""
        sent = self._sent_blocks
        mic = self._mic_blocks
        drop = self._mic_dropped
        kinds = self._rx_kinds
        self._rx_kinds = {}

        rx = " ".join(f"{k}×{v}" for k, v in sorted(kinds.items())) or "—"
        quiet = (now - self._rx_at) if self._rx_at else (now - self._session_started)
        age = now - self._session_started
        out_q = self.out_queue.qsize() if self.out_queue else 0
        play_q = self.audio_in_queue.qsize() if self.audio_in_queue else 0
        # How much quieter the microphone was after the echo canceller while
        # her voice was in the room. Near 0 dB means it is reaching the model;
        # "open" means the canceller has proven itself on the answer she is
        # giving and the microphone stays open through it.
        echo = ""
        canceller = self._echo
        if canceller is not None:
            db = canceller.interval_db()
            if db is not None:
                echo = f" | aec {db:.0f}dB {'open' if canceller.open else 'gated'}"

        print(
            f"[VOICE REC] {int(age // 60):d}:{int(age % 60):02d}"
            f" | srv {quiet:.1f}s ago | rx {rx}"
            f" | mic {mic - last_mic} peak {self._mic_peak:.2f}"
            f" | snd {sent - last_sent} q{out_q}"
            f"{f' drop {drop - last_drop}' if drop != last_drop else ''}"
            f" | play q{play_q}"
            f" | lag {lag_max * 1000:.0f}ms"
            f"{echo}",
            flush=True,
        )

    async def _receive_audio(self):
        print("[JARVIS] 👂 Recv started")
        out_buf = _TranscriptAccumulator()
        in_buf = _TranscriptAccumulator()
        mode_switch_requested = False

        _streams = 0
        try:
            while True:
                # Each pass opens a fresh receive stream. The loop exists so a
                # stream that finishes normally is replaced instead of ending
                # the task — but a finished stream on a session the server has
                # given up on yields nothing, forever, without raising. That
                # is silence with no error anywhere while audio keeps being
                # sent, which is exactly the failure being chased, so every
                # pass after the first is worth knowing about.
                _streams += 1
                if _streams > 1:
                    print(f"[JARVIS] ♻️ receive stream ended — opening #{_streams}",
                          flush=True)
                async for response in self.session.receive():
                    # Before anything is interpreted: this message existed.
                    # Whether it carried speech is a separate question from
                    # whether the server is still sending, and only the second
                    # one can be answered during a silence.
                    self._note_rx(response)

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

                    # The server saying it is about to end this session. It is
                    # the one warning there is, and nothing was listening for
                    # it: the connection would go quiet a moment later, the
                    # receive stream would stop yielding without ending or
                    # raising, and every local sign — socket, microphone,
                    # audio still accepted — kept saying the session was fine.
                    # Reconnecting here is what the message is for, and it
                    # happens before the silence rather than half a minute
                    # after it.
                    _bye = getattr(response, "go_away", None)
                    if _bye is not None:
                        _left = getattr(_bye, "time_left", None)
                        print(f"[JARVIS] 👋 server is ending the session"
                              f"{f' in {_left}' if _left else ''} — reconnecting now",
                              flush=True)
                        self.ui.write_log("SYS: The service is rotating the session — reconnecting.")
                        self.request_reconnect(keep_context=True,
                                               reason="the service ended the session")
                        break

                    if response.data:
                        if self._interrupted:
                            pass  # discard: interrupted
                        elif self._learning.active:
                            # The class is speaking in the browser. What this
                            # session says meanwhile still reaches the HUD log
                            # as text; it just does not talk over the teacher.
                            pass
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

                        if getattr(sc, "interrupted", False):
                            # The user spoke over her. The server has stopped
                            # her; what is still queued here must stop too.
                            drained = self._drain_playback()
                            with self._speaking_lock:
                                speaking = self._is_speaking
                            if drained or speaking:
                                print(f"[JARVIS] ✋ You spoke over her — {drained} "
                                      "audio chunks discarded", flush=True)
                                self.set_speaking(False)

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
                                    if in_buf.text:
                                        print(
                                            "[VOICE FLOW] 3/3 response started",
                                            flush=True,
                                        )

                        interim = getattr(sc, "interim_input_transcription", None)
                        if interim and interim.text and in_buf.set_interim(interim.text):
                            self._last_user_speech = time.monotonic()
                            self._voice_blocks_unheard = 0
                            self._heard_this_session += 1
                            # It heard. Whatever the last rebuild did, it
                            # worked, and the next one starts from one again.
                            self._deaf_rebuilds = 0
                            preview = in_buf.text
                            if preview:
                                self._live_has_text = True
                                self.ui.set_live_transcript(
                                    f"{self._user_name}: {preview}"
                                )
                                _diag(f"interim transcript={preview!r}")

                        if sc.input_transcription and sc.input_transcription.text:
                            raw_text = sc.input_transcription.text
                            txt = _normalise_transcript_spacing(raw_text)
                            if txt and in_buf.add_final(raw_text):
                                self._last_user_speech = time.monotonic()
                                self._voice_blocks_unheard = 0
                                self._heard_this_session += 1
                                self._deaf_rebuilds = 0

                                # Also on the console, fragment by fragment.
                                # "Heard:" below prints at turn_complete, which
                                # is after the assistant has already answered —
                                # useless for telling whether speech is arriving
                                # while it is being spoken.
                                # Timestamped because "the fragments arrive as
                                # you speak" and "they all arrive at once when
                                # you stop" print identically, and only the
                                # first of those is real-time transcription.
                                print(f"[JARVIS] 🎧 {time.strftime('%H:%M:%S')}"
                                      f".{int(time.time() % 1 * 1000):03d} {txt}",
                                      flush=True)

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
                                    self._live_has_text = True
                                    self.ui.set_live_transcript(
                                        f"{self._user_name}: {in_buf.text}"
                                    )
                                except Exception:
                                    pass

                                # The intent detector consumes Gemini's existing
                                # transcription, so Learning English never opens
                                # a competing STT engine or microphone.
                                if not mode_switch_requested:
                                    heard = in_buf.text
                                    if self._learning_confirmation_pending:
                                        answer = confirmation_answer(heard)
                                        if answer is not None:
                                            self._learning_confirmation_pending = False
                                            mode_switch_requested = bool(answer)
                                            if answer:
                                                asyncio.create_task(
                                                    self._enter_learning_english(
                                                        trigger="voice-confirmation",
                                                        open_browser=True,
                                                    )
                                                )
                                    else:
                                        learning_intent = detect_learning_english_intent(heard)
                                        if (
                                            learning_intent.action == "enter"
                                            and learning_intent.confidence >= 0.9
                                        ):
                                            mode_switch_requested = True
                                            asyncio.create_task(
                                                self._enter_learning_english(
                                                    trigger="voice", open_browser=True
                                                )
                                            )
                                        elif (
                                            learning_intent.action == "enter"
                                            and learning_intent.confidence >= 0.6
                                        ):
                                            self._learning_confirmation_pending = True
                                            self._learning_confirmation_prompt_pending = True

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
                            self._live_has_text = False
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
                                self.ui.write_log(f"{self._user_name}: {full_in}")
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

                            if self._learning_confirmation_prompt_pending and self.session:
                                self._learning_confirmation_prompt_pending = False
                                await self._send_text_turn(
                                        "[SYSTEM_ALERT] Pregunta solamente si el usuario quiere "
                                        "activar Learning English y abrir el estudio."
                                    )

                            # Vision injection: model finished tool-response turn → now send the image
                            if self._pending_vision and self.session:
                                import base64 as _b64
                                img_b, mime_t, question, angle = self._pending_vision
                                self._pending_vision = None
                                b64 = _b64.b64encode(img_b).decode("ascii")
                                print(f"[Vision] 📤 {len(img_b):,} bytes (angle={angle}) → main session")
                                if isinstance(self.session, OpenAIRealtimeSession):
                                    await self.session.send_client_content(
                                        turns={"parts": [
                                            {"inline_data": {"mime_type": mime_t, "data": b64}},
                                            {"text": question},
                                        ]},
                                        turn_complete=True,
                                    )
                                else:
                                    await self.session.send_realtime_input(
                                        video=types.Blob(data=img_b, mime_type=mime_t)
                                    )
                                    await self.session.send_realtime_input(text=question)
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
                                    # A user may have enabled persistent camera
                                    # Vision while the one-shot answer was being
                                    # spoken. Never let the old preview timer turn
                                    # off the newly selected live source.
                                    if self._live_vision_source != "camera":
                                        self.ui.stop_camera_stream()
                                asyncio.create_task(_cam_close())

                    if response.tool_call:
                        calls = list(response.tool_call.function_calls)
                        for fc in calls:
                            print(f"[JARVIS] 📞 {fc.name}")
                        # Beside the conversation, never inside it: while a tool
                        # works this loop keeps reading her voice and the user's
                        # words, and the result is answered when it is ready.
                        task = asyncio.create_task(
                            self._answer_tool_calls(self.session, calls)
                        )
                        self._tool_tasks.add(task)
                        task.add_done_callback(self._tool_tasks.discard)
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

        # get_output_device() returns "" for "whatever Windows is using", which
        # is the common case and is also the case where the user has just
        # plugged headphones in. Ask the sound card what that resolves to.
        _spk_actual = _spk_name
        if not _spk_actual:
            try:
                _spk_actual = str(sd.query_devices(kind="output")["name"])
            except Exception:
                _spk_actual = ""

        self._full_duplex = _is_headphones(_spk_actual)
        if self._full_duplex:
            print("[JARVIS] 🎧 Headphones — microphone stays open while she "
                  "speaks; you can interrupt her by talking", flush=True)
            self.ui.write_log("SYS: Headphones detected — you can interrupt me by speaking.")
        elif self._echo is not None:
            print(f"[JARVIS] 🔊 '{_spk_actual or 'system default'}' with echo "
                  "cancellation — each answer starts with the microphone closed "
                  "and it opens once the canceller proves itself", flush=True)
        else:
            print(f"[JARVIS] 🔇 '{_spk_actual or 'system default'}' is not "
                  "headphones — microphone closes while she speaks, to stop "
                  "her hearing herself", flush=True)

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
        self._note_audio_latency(speaker=stream)

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

        def _write(pcm: bytes) -> None:
            # The echo canceller hears what the speakers play on this thread,
            # immediately before the write. Fed from the event loop instead,
            # the sessions that interrupted themselves measured -1 to -9 dB;
            # fed here, the same speakers measured -17.8 dB.
            echo = self._echo
            if echo is not None:
                echo.render(pcm)
            stream.write(pcm)

        _SILENCE = bytes(2400 * 2)   # 100 ms at 24 kHz / 16-bit mono

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
                    # How long this chunk kept us waiting. A chunk already in
                    # the queue comes back in microseconds; one that arrives
                    # mid-wait takes as long as the network took to bring it.
                    # That is the whole difference between starving the sound
                    # card and having nothing yet to give it.
                    _got_after = time.monotonic() - _t0
                    _t_wait += _got_after
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
                    if self._echo is not None:
                        # Never let the speaker run empty while the canceller
                        # listens. An emptied buffer changes how late her voice
                        # is heard by the next answer, which then leaked through
                        # at -3 to -5 dB; with silence written and fed as its
                        # reference, -9 dB, and words the user said in the
                        # pause were heard instead of lost.
                        await loop.run_in_executor(_writer, _write, _SILENCE)
                    continue

                # Whether the audio was already here when we came for it.
                #
                # This used to read the queue depth after the await returned,
                # which answers a different question: a burst landing during
                # the gap leaves the queue full at exactly the moment the gap
                # ends, so every gap the model caused was recorded as one of
                # ours. One turn reported "chopped 11× for 8291 ms, 10 ours"
                # while the model was in fact delivering 5.7 s of speech over
                # 14 s — and a day was nearly spent looking for a stall in this
                # loop that was never in it.
                _waiting = _got_after < 0.005

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
                    await loop.run_in_executor(_writer, _write, bytes(batch))
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

        await self._send_text_turn(p1)
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

                await self._send_text_turn(p2)
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
            client = _genai.Client(api_key=get_gemini_key())
            resp   = await asyncio.to_thread(
                client.models.generate_content,
                model=GEMINI_MODEL,
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
                await self._send_text_turn(alert)
            except Exception as e:
                print(f"[Monitor] ⚠️ Could not send alert: {e}")

    # ── Plugin announcements ────────────────────────────────────────────────────

    async def _run_announcements(self) -> None:
        """Background task: speak queued plugin news, one item at a time.

        Waits until she is silent, no tool is running and the user has not
        spoken for a while. Sending the next item before her answer to the last
        one has started playing would stack two turns, so it waits for that too.
        """
        while True:
            await asyncio.sleep(1)
            if not self._announcements or not self.session:
                continue
            with self._speaking_lock:
                speaking = self._is_speaking
            now = time.monotonic()
            if speaking or self._tool_running:
                continue
            if now - self._last_user_speech < _ANNOUNCE_USER_QUIET:
                continue
            if now - self._last_spoke_at < _ANNOUNCE_GAP:
                continue
            answer_pending = self._last_spoke_at < self._announced_at
            if answer_pending and now - self._announced_at < _ANNOUNCE_ANSWER_WAIT:
                continue

            queued_at, instruction = self._announcements.popleft()
            if now - queued_at > _ANNOUNCE_MAX_AGE:
                print(f"[Announce] dropped news older than {_ANNOUNCE_MAX_AGE:.0f}s")
                continue
            try:
                await self._send_text_turn(instruction)
            except Exception as e:
                # The session is going away; the next one can still say it.
                self._announcements.appendleft((queued_at, instruction))
                print(f"[Announce] ⚠️ Could not send: {e}")
                await asyncio.sleep(5)
                continue
            self._announced_at = now
            self.ui.write_log("SYS: Announcing news from a plugin.")

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
                            await self._send_text_turn(msg)
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
                await self._send_text_turn(prompt)
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
            if not speaking and not self.ui.muted and not self._learning.active:
                if self._input_sample_rate != SEND_SAMPLE_RATE:
                    # The phone sends 16 kHz; OpenAI Realtime takes the PC
                    # microphone's 24 kHz.
                    chunk = {**chunk, "data": _resample_pcm16(
                        chunk["data"], SEND_SAMPLE_RATE, self._input_sample_rate)}
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
                    await self._send_text_turn(text)
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
        self._input_sample_rate = (
            OPENAI_SAMPLE_RATE if get_voice_provider() == "openai"
            else SEND_SAMPLE_RATE
        )
        audio_devices.configure(self._input_sample_rate, RECEIVE_SAMPLE_RATE)
        _configured_input_rate = self._input_sample_rate

        # Enumerate audio devices off-thread. The settings drawer must never pay
        # for host-API enumeration on the Qt thread.
        audio_devices.prefetch()

        # Start dashboard (optional — needs: pip install fastapi "uvicorn[standard]" cryptography)
        try:
            from dashboard.server import DashboardServer
            self._dashboard = DashboardServer()
            self._dashboard.set_connect_callback(self._on_phone_connected)
            self._dashboard.set_learning_callbacks(
                state=self._learning_snapshot,
                action=self._learning_web_action,
                session=self._learning_live_session,
                openai_call=self._learning_openai_call,
                turn=self._learning_turn,
                voice=self._learning_voice,
                placement=self._learning_placement,
                activity=self._learning_activity,
            )
            self._dashboard_task = asyncio.create_task(
                self._dashboard.serve(), name="lumina-dashboard-lifecycle"
            )
            # Do not announce Lumina as ready while the browser server is still
            # only an intention. Usually this takes under a second; failures are
            # retained and shown instead of opening a dead browser tab.
            if not await self._dashboard.wait_until_ready(timeout=10.0):
                detail = self._dashboard.startup_error or "unknown startup error"
                print(f"[Dashboard] Startup incomplete: {detail}")
                self.ui.write_log(f"ERR: Local web server could not start: {detail}")
            # Runs for the whole lifetime, not just inside an active session
            self._dashboard_commands_task = asyncio.create_task(
                self._process_dashboard_commands(),
                name="lumina-dashboard-commands",
            )
        except Exception as e:
            print(f"[Dashboard] Disabled: {e}")
            self._dashboard = None

        while True:
            try:
                print("[JARVIS] Connecting...")
                self.ui.set_state("THINKING")
                provider = get_voice_provider()
                desired_input_rate = (
                    OPENAI_SAMPLE_RATE if provider == "openai"
                    else SEND_SAMPLE_RATE
                )
                self._input_sample_rate = desired_input_rate
                if desired_input_rate != _configured_input_rate:
                    audio_devices.configure(desired_input_rate, RECEIVE_SAMPLE_RATE)
                    audio_devices.prefetch()
                    _configured_input_rate = desired_input_rate
                _resumed_with = provider == "gemini" and self._resume_handle is not None

                if provider == "openai":
                    session_cm = OpenAIRealtimeSession.open(
                        api_key=get_openai_key(),
                        instructions=self._build_system_instruction(),
                        voice=get_openai_voice(),
                        tools=self._all_tool_declarations(),
                        assistant_name=self._asst_name,
                    )
                    self._echo = EchoCanceller.create(OPENAI_SAMPLE_RATE)
                    self._echo_announced = False
                    print(
                        f"[JARVIS] Live provider: OpenAI; transport: WebSocket; "
                        f"model: {OPENAI_REALTIME_MODEL}; voice: {get_openai_voice()}; "
                        f"echo cancellation: {'on' if self._echo else 'unavailable'}"
                    )
                else:
                    self._echo = None
                    config = self._build_config()
                    # A fresh client avoids stale HTTP session state on reconnect.
                    # Keep Gemini on its measured v1beta path.
                    client = genai.Client(
                        api_key=get_gemini_key(),
                        http_options={"api_version": LIVE_API_VERSION},
                    )
                    session_cm = client.aio.live.connect(model=LIVE_MODEL, config=config)
                    print(
                        f"[JARVIS] Live provider: Gemini; model: {LIVE_MODEL.removeprefix('models/')}; "
                        f"transport: {LIVE_API_VERSION}; "
                        "optional server audio features: off; "
                        f"context compression: {'on' if _COMPRESSION_ON else 'off'}"
                    )

                async with session_cm as session, asyncio.TaskGroup() as tg:
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
                    # The recorder measures this session, not the last one.
                    self._rx_at = 0.0
                    self._rx_kinds = {}
                    self._mic_peak = 0.0
                    print("[JARVIS] Connected.")
                    if _resumed_with:
                        # Say it plainly: the difference between "it reconnected"
                        # and "it reconnected and still knows what we were doing"
                        # is the whole point, and it is invisible otherwise.
                        self.ui.write_log("SYS: Reconnected — conversation restored.")
                    self.ui.set_state("LISTENING")
                    self.ui.write_log(f"SYS: {self._asst_name} online.")

                    if self._live_vision_source:
                        # A transport rotation must not silently turn sharing
                        # off. Invalidate any old frame and let the new session's
                        # worker prove it is active with its first successful send.
                        self._live_vision_epoch += 1
                        self._live_vision_latest = None
                        self._live_vision_announced = False
                        self._live_vision_should_greet = False
                        self._live_vision_frames = 0
                        self._live_vision_fingerprint = None
                        self._live_vision_last_sent_at = 0.0
                        if self._live_vision_paused:
                            self.ui.set_vision_state(
                                self._live_vision_source, "paused",
                                "VISION PAUSED · VOICE ACTIVE",
                            )
                        else:
                            self.ui.set_vision_state(
                                self._live_vision_source, "starting",
                                "RESTORING VISION…",
                            )
                        if self._live_vision_source == "camera" and not self._live_vision_paused:
                            self.ui.start_camera_stream()

                    if self._dashboard:
                        await self._dashboard.broadcast({"type": "status", "state": "active"})

                    self._reconnect_event.clear()  # ignore requests from before this session
                    tg.create_task(self._watch_reconnect())
                    tg.create_task(self._send_realtime())
                    tg.create_task(self._run_live_vision())
                    tg.create_task(self._listen_audio())
                    tg.create_task(self._receive_audio())
                    tg.create_task(self._play_audio())
                    tg.create_task(self._run_voice_recorder())
                    tg.create_task(self._run_system_monitor())
                    tg.create_task(self._run_background_monitor())
                    tg.create_task(self._run_proactive_mode())
                    tg.create_task(self._run_announcements())
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
                    "UNAUTHENTICATED", "PERMISSION_DENIED", "invalid_api_key",
                    "Incorrect API key",
                )):
                    if provider == "openai":
                        self.ui.write_log(
                            "ERR: OpenAI API key invalid — update it in API Keys."
                        )
                        self.ui.set_state("SLEEPING")
                        # The same key fails the same way, as fast as this loop
                        # can spin. Wait for the key or the engine to change.
                        rejected = get_openai_key()
                        while (get_voice_provider() == "openai"
                               and get_openai_key() == rejected):
                            await asyncio.sleep(2)
                        self._conn_backoff = 0
                        continue
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
                self._echo = None
                if self._live_vision_source:
                    state = "paused" if self._live_vision_paused else "starting"
                    detail = (
                        "VISION PAUSED · VOICE RECONNECTING"
                        if self._live_vision_paused else "VOICE RECONNECTING…"
                    )
                    self.ui.set_vision_state(self._live_vision_source, state, detail)
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
