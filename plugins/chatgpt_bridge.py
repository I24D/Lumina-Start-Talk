"""Two-way bridge to the ChatGPT desktop app on Windows.

This module was verified against the unified ChatGPT Windows app installed on
this machine. Windows registers it as ``OpenAI.Codex_2p2nqsd0c76g0!App`` and
the owning process is ``ChatGPT.exe``. Despite the historical package name,
the accessible window is titled ``ChatGPT`` and exposes these stable elements:

* an Edit control named ``Work with ChatGPT`` for the composer;
* reply groups whose first accessible text is ``ChatGPT said:``;
* a ``Stop`` button while a reply is being generated; and
* accessible ``New chat`` and ChatGPT/Codex mode controls.

The signed-in desktop session is used directly. No OpenAI API key, browser
automation, screenshots, or blind coordinate clicks are needed. Clipboard
paste carries Spanish and other Unicode text without keyboard-layout damage,
but text is sent only after both the ChatGPT process and composer are verified.

Current Windows notifications are read without dismissing them from the local
notification database. The desktop package uses its package family identifier;
Phone Link mirrors mobile ChatGPT notices under ``com.openai.chatgpt``. Payloads
are filtered to those handlers before any notification text is returned.

The application and Windows notification schemas may evolve. Every operation
therefore fails closed and returns a spoken error instead of typing into an
unverified window or raising into Lumina's plugin runner.
"""

from __future__ import annotations

import os
import platform
import re
import sqlite3
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any


_IMPORT_ERROR = ""
if platform.system() == "Windows":
    try:
        import psutil
        import pyperclip
        import win32con
        import win32gui
        import win32process
        from pywinauto import Desktop
        from pywinauto.keyboard import send_keys
    except Exception as exc:
        _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
else:
    _IMPORT_ERROR = f"ChatGPT desktop control is Windows-only; this machine runs {platform.system()}."


PLUGIN = {
    "name": "chatgpt_bridge",
    "description": (
        "Controls the signed-in ChatGPT desktop application as the user: opens and focuses it, "
        "writes messages in its real chat, waits for the completed response, reads responses "
        "aloud, starts a new chat, and reads current ChatGPT Windows notifications. Always use "
        "this tool for requests involving the ChatGPT desktop app. Do NOT use open_app, "
        "computer_control, browser_control, developer_chat_reader, or copilot_bridge; those tools "
        "cannot verify ChatGPT's composer, completed response, or notifications. ChatGPT is not "
        "Copilot, Codex, Claude Code, or Lumina herself. "
        "action='connect' opens and focuses ChatGPT without sending anything: 'conéctate con "
        "ChatGPT', 'abre la aplicación de ChatGPT', 'connect to ChatGPT', 'open the ChatGPT app'. "
        "action='ask' sends text and returns ChatGPT's completed answer: 'pregúntale a ChatGPT "
        "que...', 'escríbele a ChatGPT...', 'dile a ChatGPT...', 'ask ChatGPT...', 'write to "
        "ChatGPT'. Put the complete message in text and preserve the user's language. "
        "action='read' reads the latest completed on-screen answer: 'lee la respuesta de ChatGPT', "
        "'qué respondió ChatGPT', 'read ChatGPT's answer'. action='notifications' reads current "
        "ChatGPT notifications without dismissing them: 'lee las notificaciones de ChatGPT', "
        "'tengo notificaciones de ChatGPT', 'read ChatGPT notifications'. action='new' starts a "
        "new ChatGPT conversation: 'nuevo chat de ChatGPT', 'start a new ChatGPT chat'. "
        "action='close' closes the desktop app: 'desconéctate de ChatGPT', 'cierra ChatGPT', "
        "'disconnect from ChatGPT', 'close ChatGPT'. You are the messenger, not the judge: pass "
        "the user's full request to ChatGPT and let ChatGPT answer for itself."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "ask (default) | connect | read | notifications | new | close",
            },
            "text": {
                "type": "STRING",
                "description": (
                    "Complete message to send for action='ask'. Preserve the user's language and "
                    "meaning; do not translate, summarize, judge, or answer it yourself."
                ),
            },
            "timeout": {
                "type": "INTEGER",
                "description": "Maximum seconds to wait for a ChatGPT answer; default 120.",
            },
            "limit": {
                "type": "INTEGER",
                "description": "Maximum notifications to read; default 5, maximum 10.",
            },
        },
        "required": [],
    },
}


_PROCESS = "chatgpt.exe"
_APP_IDS = (
    "OpenAI.Codex_2p2nqsd0c76g0!App",
    "OpenAI.ChatGPT-Desktop_2p2nqsd0c76g0!ChatGPT",
)
_OBJID_CLIENT = 0xFFFFFFFC
_HELPER_TITLES = {"default ime", "msctfime ui"}
_REPLY_MARKERS = {"chatgpt said:", "chatgpt dijo:"}
_COMPOSER_HINTS = ("chatgpt",)
_DEFAULT_TIMEOUT = 120
_BACKGROUND_TIMEOUT = 900
_POLL_SECONDS = 0.8
_SETTLE_SECONDS = 1.8
_SPOKEN_LIMIT = 12_000
_ANSWER_IN_DEPTH = "[ANSWER_IN_DEPTH]\n"
_ACTIONS = {
    "ask", "connect", "open", "read", "notifications", "notification",
    "new", "new_chat", "close", "disconnect",
}
class _Detailed(str):
    """A desktop answer that Lumina should cover without reducing to a headline."""


def _process_name(pid: int) -> str:
    try:
        return psutil.Process(pid).name().lower()
    except Exception:
        return ""


def _chatgpt_hwnd() -> int | None:
    """Return the ChatGPT app's own window, never a browser tab.

    The app hides into the notification area when its window is closed: the
    process stays alive and its windows keep their titles, but every one of
    them reports ``IsWindowVisible`` false. Requiring visibility is what made
    a read answer "the ChatGPT desktop app is not open" with the conversation
    still sitting in it. A hidden window's accessibility tree reads perfectly
    well — all four replies came back from one — so reading does not have to
    disturb a window the user deliberately put away.

    Hidden windows rank below visible ones, and larger ones above smaller:
    the app keeps a second, empty companion window under the same title, and
    only the size tells them apart.
    """
    if _IMPORT_ERROR:
        return None
    candidates: list[tuple[int, int, int, int]] = []

    def visit(hwnd, _):
        try:
            title = win32gui.GetWindowText(hwnd).strip()
            if not title or title.casefold() in _HELPER_TITLES:
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if _process_name(pid) != _PROCESS:
                return True
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            area = max(0, right - left) * max(0, bottom - top)
            if area <= 0:
                return True
            candidates.append((
                1 if win32gui.IsWindowVisible(hwnd) else 0,
                2 if title.casefold() == "chatgpt" else 1,
                area,
                hwnd,
            ))
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(visit, None)
    except Exception:
        return None
    return max(candidates, default=(0, 0, 0, 0))[3] or None


def _wake_accessibility(hwnd: int) -> None:
    """Ask Chromium to expose the current renderer's accessibility tree."""
    handles = [hwnd]
    try:
        win32gui.EnumChildWindows(hwnd, lambda handle, _: handles.append(handle), None)
    except Exception:
        pass
    for handle in handles:
        try:
            win32gui.SendMessageTimeout(
                handle,
                win32con.WM_GETOBJECT,
                0,
                _OBJID_CLIENT,
                win32con.SMTO_ABORTIFHUNG,
                1000,
            )
        except Exception:
            pass
    time.sleep(0.8)


def _launch() -> bool:
    for app_id in _APP_IDS:
        try:
            subprocess.Popen(
                ["explorer.exe", f"shell:AppsFolder\\{app_id}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            continue
        for _ in range(25):
            time.sleep(0.6)
            if _chatgpt_hwnd():
                return True
    return False


def _window(launch_if_needed: bool = True):
    hwnd = _chatgpt_hwnd()
    if hwnd is None and launch_if_needed:
        if not _launch():
            return None
        hwnd = _chatgpt_hwnd()
    if hwnd is None:
        return None
    _wake_accessibility(hwnd)
    return Desktop(backend="uia").window(handle=hwnd)


def _descend(element, match, depth: int = 0, limit: int = 22, budget=None):
    if budget is None:
        budget = {"remaining": 5000}
    if depth > limit or budget["remaining"] <= 0:
        return None
    try:
        children = element.children()
    except Exception:
        return None
    for child in children:
        budget["remaining"] -= 1
        if budget["remaining"] <= 0:
            return None
        try:
            if match(child):
                return child
            found = _descend(child, match, depth + 1, limit, budget)
            if found is not None:
                return found
        except Exception:
            continue
    return None


def _composer(window):
    """Find the message composer while excluding the settings search field."""
    def matches(control) -> bool:
        info = control.element_info
        if info.control_type != "Edit" or info.automation_id == "settings-search":
            return False
        name = (info.name or "").casefold()
        return any(hint in name for hint in _COMPOSER_HINTS)

    return _descend(window, matches)


def _ensure_chatgpt_mode(window) -> bool:
    """Select ChatGPT in the unified app and refuse an unverified mode."""
    switch = _descend(
        window,
        lambda control: (
            control.element_info.control_type == "Button"
            and (control.element_info.name or "").casefold().startswith("switch mode")
        ),
    )
    if switch is None:
        # Older standalone builds have no mode switch. Their ChatGPT-labelled
        # composer is sufficient proof that this is the intended surface.
        return _composer(window) is not None

    current = (switch.element_info.name or "").casefold()
    if "current mode: chatgpt" in current:
        return True

    try:
        _focus_verified(window)
        switch.click_input()
        time.sleep(0.4)
        _wake_accessibility(window.handle)
        choice = _named_control(window, {"ChatGPT"}, {"Button", "MenuItem"})
        if choice is None:
            send_keys("{ESC}")
            return False
        choice.click_input()
        time.sleep(0.8)
        _wake_accessibility(window.handle)
        updated = _descend(
            window,
            lambda control: (
                control.element_info.control_type == "Button"
                and "current mode: chatgpt" in (control.element_info.name or "").casefold()
            ),
        )
        return updated is not None and _composer(window) is not None
    except Exception:
        try:
            send_keys("{ESC}")
        except Exception:
            pass
        return False


def _named_control(window, names: set[str], control_types: set[str]):
    normalized = {name.casefold() for name in names}
    return _descend(
        window,
        lambda control: (
            control.element_info.control_type in control_types
            and (control.element_info.name or "").strip().casefold() in normalized
        ),
    )


def _reply_snapshots(window) -> list[tuple[str, Any]]:
    """Segment ChatGPT replies from the flattened accessible conversation.

    The current app exposes messages as siblings under one large content group,
    not one group per message. ``ChatGPT said:`` starts a reply and ``You said:``
    ends it. The last Copy button in that segment is the response-level copy
    action; earlier Copy buttons may belong to code blocks.
    """
    snapshots: list[tuple[str, Any]] = []
    parts: list[str] = []
    copy_button = None
    capturing = False
    budget = {"remaining": 9000}

    def finish() -> None:
        nonlocal parts, copy_button, capturing
        if capturing:
            text = " ".join(parts).replace("\xa0", " ")
            text = re.sub(r"\s+([,.;:!?])", r"\1", text)
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                snapshots.append((text, copy_button))
        parts = []
        copy_button = None
        capturing = False

    def walk(element, depth: int = 0) -> None:
        nonlocal parts, copy_button, capturing
        if depth > 26 or budget["remaining"] <= 0:
            return
        try:
            children = element.children()
        except Exception:
            return
        for child in children:
            budget["remaining"] -= 1
            if budget["remaining"] <= 0:
                return
            try:
                info = child.element_info
                kind = info.control_type
                name = (info.name or "").strip()
                folded = name.casefold()

                if kind == "Text" and folded in _REPLY_MARKERS:
                    finish()
                    capturing = True
                    continue
                if kind == "Text" and folded in {"you said:", "tú dijiste:", "tu dijiste:"}:
                    finish()
                    continue
                if kind == "Edit" or (
                    kind == "Text" and folded.startswith("chatgpt can make mistakes")
                ):
                    finish()
                    continue
                if kind == "Button":
                    if capturing and folded in {"copy", "copiar", "copy response"}:
                        copy_button = child
                    continue
                if kind in {"StatusBar", "ToolBar", "Image"}:
                    continue
                if capturing and kind in {"Text", "ListItem", "Hyperlink"} and name:
                    if not parts or parts[-1] != name:
                        parts.append(name)
                    continue
                walk(child, depth + 1)
            except Exception:
                continue

    walk(window)
    finish()
    return snapshots


_clipboard_lock = threading.Lock()


# ── copying a reply out of the app ───────────────────────────────────────────

# What is written to the clipboard before asking for a copy, so that "the copy
# arrived" can be told apart from "the clipboard never changed".
_CONTENT_REFERENCE_RE = re.compile(r"(?m)^\s*::[a-z-]+\{[^}]*\}\s*$")

_COPY_SENTINEL = "<<lumina-copy-pending>>"
_COPY_WAIT_SECONDS = 2.5


def _invoke_copy(button) -> bool:
    """Ask the button to act on itself. Does not need it to be on screen."""
    try:
        button.invoke()
        return True
    except Exception:
        return False


def _click_copy(button) -> bool:
    """Fall back to a real click, but only at a button that is really drawn."""
    try:
        if not button.is_visible():
            return False
        button.click_input()
        return True
    except Exception:
        return False


def _await_clipboard(sentinel: str) -> str:
    """The clipboard once it stops being the sentinel; empty if it never does."""
    deadline = time.monotonic() + _COPY_WAIT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(0.1)
        try:
            current = str(pyperclip.paste() or "")
        except Exception:
            return ""
        if current and current != sentinel:
            return current.strip()
    return ""


def _speech_text(text: str) -> str:
    """Strip what belongs to the app rather than to the answer.

    Copy hands back ChatGPT's own markdown, and with it markup that was never
    on screen: six ``::chatgpt-content-reference{index="0"}`` directives came
    back inside one email summary. Bold markers, headings and fences are the
    same problem in smaller form — they are instructions to a renderer, and
    this text is on its way to a voice.
    """
    text = _CONTENT_REFERENCE_RE.sub("", text or "")
    text = re.sub(r"!\[([^]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"(?m)^#{1,6}\s+", "", text)
    text = re.sub(r"(?m)^\s*[-*+]\s+", "", text)
    text = text.replace("```", "").replace("**", "")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _copy_reply(snapshot: tuple[str, Any]) -> str:
    """Use ChatGPT's Copy action for exact Unicode, then restore the clipboard.

    Copy belongs to a hover toolbar: the button is in the accessibility tree
    with ``visible=False`` and a rectangle over blank screen, so a click sent
    to it lands on nothing. Worse, that failure is silent — reading the
    clipboard afterwards returns whatever was already on it, and this bridge
    pastes in order to send, so what was already on it is usually the last
    message the user sent. That is exactly how a read of a 1697-character
    answer came back as "SEGUNDA PRUEBA Codex".

    The Invoke pattern asks the control to act on itself. It does not depend
    on the button being drawn and does not take the pointer away from the
    user; on this app it delivers the reply in about 0.6 s. The sentinel
    written beforehand is what makes a real copy distinguishable from an
    untouched clipboard, and the text read out of the accessibility tree is
    returned whenever no copy arrives — it is complete and correctly
    accented, only without markdown.
    """
    fallback, button = snapshot
    if button is None:
        return fallback

    with _clipboard_lock:
        saved = ""
        try:
            saved = pyperclip.paste()
        except Exception:
            pass
        try:
            pyperclip.copy(_COPY_SENTINEL)
        except Exception:
            return fallback

        try:
            for ask_to_copy in (_invoke_copy, _click_copy):
                if not ask_to_copy(button):
                    continue
                copied = _await_clipboard(_COPY_SENTINEL)
                if copied:
                    return copied
            return fallback
        finally:
            try:
                pyperclip.copy(saved)
            except Exception:
                pass


def _latest_reply(window, exact: bool = False) -> str:
    snapshots = _reply_snapshots(window)
    if not snapshots:
        return ""
    return _copy_reply(snapshots[-1]) if exact else snapshots[-1][0]


def _is_generating(window) -> bool:
    return _named_control(window, {"Stop", "Detener"}, {"Button"}) is not None


def _await_reply(window, baseline: str, baseline_count: int, timeout: int) -> str:
    deadline = time.monotonic() + timeout
    last = ""
    settled_at = 0.0
    while time.monotonic() < deadline:
        time.sleep(_POLL_SECONDS)
        _wake_accessibility(window.handle)
        snapshots = _reply_snapshots(window)
        text = snapshots[-1][0] if snapshots else ""
        is_new = len(snapshots) > baseline_count or (text and text != baseline)
        if not is_new:
            continue
        if text != last:
            last = text
            settled_at = time.monotonic()
            continue
        if not _is_generating(window) and time.monotonic() - settled_at >= _SETTLE_SECONDS:
            return _copy_reply(snapshots[-1])
    return last


def _activate(hwnd: int) -> None:
    """Bring the app back the way its own tray icon does.

    ShowWindow makes a tray-hidden window render again, and that turns out not
    to be the same thing as making it usable. Measured on this app: UIA still
    read the whole conversation and a screenshot still showed it, while every
    click and keystroke was dropped and the composer reported
    ``has_keyboard_focus`` False. A message pasted into a window revived that
    way lands nowhere, and the ask that follows spends its entire timeout
    waiting for an answer to something that was never sent — two of those in a
    row is what left Lumina silent for four minutes and cost her the session.

    The shell activation verb is what the tray icon and the Start menu use.
    After it, the same click focuses the composer and the same paste arrives.
    """
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(0.4)
    if win32gui.IsWindowVisible(hwnd):
        return

    for app_id in _APP_IDS:
        try:
            subprocess.Popen(
                ["explorer.exe", f"shell:AppsFolder\\{app_id}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            continue
        for _ in range(20):
            time.sleep(0.3)
            if win32gui.IsWindowVisible(hwnd):
                time.sleep(1.0)     # up, but still painting
                return


def _focus_verified(window) -> None:
    """Put ChatGPT in front, fetching it out of the tray first when it is there."""
    try:
        _activate(window.handle)
    except Exception:
        pass

    window.set_focus()
    time.sleep(0.35)
    foreground = win32gui.GetForegroundWindow()
    _, pid = win32process.GetWindowThreadProcessId(foreground)
    if _process_name(pid) != _PROCESS:
        raise RuntimeError("Windows did not give keyboard focus to ChatGPT")


def _focus_composer(window, composer):
    """Click into the message box and prove the app took the keyboard.

    ``has_keyboard_focus`` is the difference between a window that is merely
    painted and one that can be typed into, and it is the only warning
    available before a paste disappears without a sound. The element is
    re-located between attempts because the app re-renders as it comes
    forward, and the handle found a moment earlier can describe a layout that
    no longer exists.
    """
    for attempt in range(3):
        try:
            composer.click_input()
        except Exception:
            pass
        time.sleep(0.4 + 0.3 * attempt)
        try:
            if composer.has_keyboard_focus():
                return composer
        except Exception:
            pass
        replacement = _composer(window)
        if replacement is not None:
            composer = replacement
    return None


def _send(window, composer, text: str) -> None:
    """Paste and submit only after focus is verified as belonging to ChatGPT."""
    _focus_verified(window)

    focused = _focus_composer(window, composer)
    if focused is None:
        raise RuntimeError(
            "ChatGPT is on screen but its message box would not take the "
            "keyboard, so nothing was typed"
        )

    foreground = win32gui.GetForegroundWindow()
    _, pid = win32process.GetWindowThreadProcessId(foreground)
    if _process_name(pid) != _PROCESS:
        raise RuntimeError("ChatGPT lost focus before the message could be sent")

    saved = ""
    try:
        saved = pyperclip.paste()
    except Exception:
        pass
    try:
        pyperclip.copy(text)
        send_keys("^a")
        time.sleep(0.1)
        send_keys("^v")
        time.sleep(0.35)
        send_keys("{ENTER}")
    finally:
        time.sleep(0.2)
        try:
            pyperclip.copy(saved)
        except Exception:
            pass


def _say(player, instruction: str) -> None:
    try:
        say = getattr(player, "request_say", None)
        if callable(say):
            say(instruction)
    except Exception:
        pass


def _act_connect() -> str:
    window = _window()
    if window is None:
        return "I could not open the ChatGPT desktop app. It may not be installed."
    try:
        _focus_verified(window)
    except Exception as exc:
        return f"ChatGPT is open, but I could not focus it: {exc}"
    if not _ensure_chatgpt_mode(window):
        return "ChatGPT is open, but I could not switch the unified app from Codex to ChatGPT mode."
    if _composer(window) is None:
        return "ChatGPT is open, but its message box is not ready. It may be showing settings or a dialog."
    return "The ChatGPT desktop app is open and its chat is ready."


def _act_new() -> str:
    window = _window()
    if window is None:
        return "I could not open the ChatGPT desktop app."
    if not _ensure_chatgpt_mode(window):
        return "ChatGPT is open, but I could not verify ChatGPT mode before starting a new chat."
    button = _named_control(window, {"New chat", "Nuevo chat"}, {"Button", "MenuItem"})
    if button is None:
        return "ChatGPT is open, but I could not find its New chat control."
    _focus_verified(window)
    button.click_input()
    for _ in range(20):
        time.sleep(0.25)
        _wake_accessibility(window.handle)
        if _composer(window) is not None:
            return "A new ChatGPT conversation is ready."
    return "I selected New chat, but its message box did not become ready."


_job_lock = threading.Lock()
_job_question = ""            # non-empty while an answer is still being waited for
_job_started = 0.0


def _deliver(question: str, answer: str, player=None) -> None:
    """Speak an answer that arrived long after its tool call returned."""
    spoken = _speech_text(answer)
    if len(spoken) > _SPOKEN_LIMIT:
        spoken = spoken[:_SPOKEN_LIMIT].rsplit(" ", 1)[0] + "…"

    print(f"[ChatGPT] answered after the fact: {len(answer)} chars")
    if player:
        try:
            player.write_log(f"[ChatGPT] answer received ({len(answer)} chars)")
        except Exception:
            pass

    # The marker is not decoration. Without it this lands as an ordinary user
    # turn and the model reads it as the user making conversation, which is
    # how OpenClaw's first delayed answer came back as "I have already put the
    # question to it and am waiting". core/prompt.txt recognises
    # [DELAYED_ANSWER] the way it recognises [SYSTEM_ALERT].
    _say(
        player,
        "[DELAYED_ANSWER] ChatGPT has finished the message you sent it earlier "
        f"('{question[:120]}'). Its answer follows.\n\n"
        + _ANSWER_IN_DEPTH + spoken,
    )


def _run_job(question: str, timeout: int, player=None) -> None:
    """Background worker: send, wait for ChatGPT, then speak what came back."""
    global _job_question
    answer, error = "", ""
    try:
        window = _window()
        if window is None:
            error = "the desktop app would not open"
        elif not _ensure_chatgpt_mode(window):
            error = "the app would not leave Codex mode"
        else:
            composer = _composer(window)
            if composer is None:
                error = "its message box was not there"
            else:
                snapshots = _reply_snapshots(window)
                baseline = snapshots[-1][0] if snapshots else ""
                _send(window, composer, question)
                answer = _await_reply(window, baseline, len(snapshots), timeout)
                if not answer:
                    error = "it did not finish answering in time"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        with _job_lock:
            _job_question = ""

    if answer:
        _deliver(question, answer, player)
        return

    print(f"[ChatGPT] ask failed: {error}")
    _say(
        player,
        "The message you sent to ChatGPT earlier did not go through. Tell the "
        f"user so in one sentence, and say what went wrong: {error}",
    )


def _act_ask(text: str, timeout: int, player=None) -> str:
    """Hand the message over and return at once.

    Waiting here is what froze her. The call blocked for its whole timeout,
    twice in a row, and by the end of it the live session had dropped its
    resumption handle because she had not spoken in four minutes. ChatGPT is
    quick when it is quick and slow when it is not, and neither is a reason
    for her to go silent — OpenClaw is answered the same way.
    """
    global _job_question, _job_started
    if not text:
        return "What would you like me to say to ChatGPT?"

    with _job_lock:
        if _job_question:
            waited = int(time.monotonic() - _job_started)
            return (
                f"I am still waiting for ChatGPT to answer '{_job_question[:80]}' — "
                f"{waited} seconds so far. I will read that answer out the moment "
                "it arrives; ask me again afterwards and I will send the new one."
            )
        _job_question = text
        _job_started = time.monotonic()

    # The caller's timeout was sized for a wait someone was sitting through.
    # Nobody is sitting through this one, so it only has to be long enough
    # that a stuck app is eventually given up on.
    threading.Thread(
        target=_run_job,
        args=(text, max(timeout, _BACKGROUND_TIMEOUT), player),
        name="lumina-chatgpt-ask",
        daemon=True,
    ).start()

    return (
        "I have sent that to ChatGPT. I will not keep you waiting for it — "
        "carry on, and I will read the answer out loud the moment it arrives."
    )


def _act_read(timeout: int) -> str:
    window = _window(launch_if_needed=False)
    if window is None:
        return "The ChatGPT desktop app is not open."
    if not _ensure_chatgpt_mode(window):
        return "The app is open, but I could not switch from Codex to ChatGPT mode to read its answer."
    current = _latest_reply(window, exact=True)
    if _is_generating(window):
        current = _await_reply(window, "", 0, timeout) or current
    if current:
        return _Detailed(current)
    return "I can see ChatGPT, but there is no completed answer to read."


def _notification_database() -> Path:
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if not local:
        return Path()
    return Path(local) / "Microsoft" / "Windows" / "Notifications" / "wpndatabase.db"


def _decode_payload(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        return ""
    raw = bytes(payload)
    encodings = ("utf-8", "utf-16-le") if raw.count(b"\x00") < len(raw) // 4 else ("utf-16-le", "utf-8")
    for encoding in encodings:
        try:
            return raw.decode(encoding).lstrip("\ufeff\x00")
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _notification_text(payload: Any) -> str:
    xml = _decode_payload(payload).strip()
    if not xml:
        return ""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return ""
    parts = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1].casefold() != "text":
            continue
        text = " ".join(part.strip() for part in node.itertext() if part.strip())
        text = re.sub(r"\s+", " ", text).strip()
        if text and (not parts or parts[-1] != text):
            parts.append(text)
    return ": ".join(parts)


def _arrival_label(value: Any) -> str:
    try:
        raw = int(value)
        if raw > 20_000_000_000_000_000:
            timestamp = (raw - 116_444_736_000_000_000) / 10_000_000
        elif raw > 10_000_000_000:
            timestamp = raw / 1000
        else:
            timestamp = raw
        return datetime.fromtimestamp(timestamp).strftime("%b %d at %H:%M")
    except (OverflowError, OSError, TypeError, ValueError):
        return ""


def _act_notifications(limit: int) -> str:
    path = _notification_database()
    if not path.is_file():
        return "I could not find the Windows notification database."
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=2) as database:
            rows = database.execute(
                """
                SELECT n.Payload, n.ArrivalTime
                FROM Notification AS n
                JOIN NotificationHandler AS h ON h.RecordId = n.HandlerId
                WHERE lower(h.PrimaryId) LIKE '%openai.codex_%'
                   OR lower(h.PrimaryId) LIKE '%com.openai.chatgpt%'
                   OR lower(h.PrimaryId) LIKE '%openai.chatgpt%'
                ORDER BY n.ArrivalTime DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    except (OSError, sqlite3.Error) as exc:
        return f"I could not read ChatGPT notifications from Windows: {exc}"

    notices = []
    for payload, arrived in rows:
        text = _notification_text(payload)
        if not text or text in {notice[1] for notice in notices}:
            continue
        notices.append((_arrival_label(arrived), text))
    if not notices:
        return "There are no current ChatGPT notifications in Windows."
    formatted = []
    for index, (arrived, text) in enumerate(notices, start=1):
        when = f", received {arrived}" if arrived else ""
        formatted.append(f"Notification {index}{when}: {text}")
    return " ".join(formatted)


def _act_close() -> str:
    hwnd = _chatgpt_hwnd()
    if hwnd is None:
        return "The ChatGPT desktop app is already closed."
    try:
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    except Exception as exc:
        return f"I could not close ChatGPT: {exc}"
    for _ in range(15):
        time.sleep(0.3)
        try:
            if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
                return "The ChatGPT desktop window is closed."
        except Exception:
            return "The ChatGPT desktop window is closed."
    return "I asked ChatGPT to close, but its desktop window is still visible."


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Execute one ChatGPT desktop action and never raise to the caller."""
    if _IMPORT_ERROR:
        return f"The ChatGPT desktop bridge is unavailable: {_IMPORT_ERROR}"
    parameters = parameters or {}
    action = str(parameters.get("action") or "ask").strip().lower().replace("-", "_")
    text = str(parameters.get("text") or "").strip()
    if action not in _ACTIONS:
        action = "ask" if text else "connect"

    if player:
        try:
            player.write_log(f"[ChatGPT] {action}")
        except Exception:
            pass

    try:
        timeout = max(10, min(300, int(parameters.get("timeout") or _DEFAULT_TIMEOUT)))
        if action in {"connect", "open"}:
            result = _act_connect()
        elif action in {"new", "new_chat"}:
            result = _act_new()
        elif action == "read":
            result = _act_read(timeout)
        elif action in {"notification", "notifications"}:
            limit = max(1, min(10, int(parameters.get("limit") or 5)))
            result = _act_notifications(limit)
        elif action in {"close", "disconnect"}:
            result = _act_close()
        else:
            result = _act_ask(text, timeout, player)
    except Exception as exc:
        print(f"[ChatGPT] {type(exc).__name__}: {exc}")
        return f"The ChatGPT desktop bridge failed: {exc}"

    print(f"[ChatGPT] {action} -> {len(result)} chars -> {result[:120]}")
    if player:
        try:
            player.write_log(f"LUMINA: {result[:200]}")
        except Exception:
            pass
    if isinstance(result, _Detailed):
        result = _ANSWER_IN_DEPTH + _speech_text(result)
    if len(result) > _SPOKEN_LIMIT:
        result = result[:_SPOKEN_LIMIT].rsplit(" ", 1)[0]
        return result + "... That is as far as I will read aloud."
    return result
