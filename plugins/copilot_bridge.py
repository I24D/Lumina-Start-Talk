"""
Two-way bridge to the Microsoft Copilot desktop app.

Lumina could already *launch* Copilot through open_app, but that is where it
stopped: computer_control's `type` sends keystrokes wherever the keyboard focus
happens to be and reports success unconditionally, so a question aimed at
Copilot silently went nowhere while Lumina announced it had been asked. Nothing
could read the answer back either.

This drives Copilot through UI Automation instead of blind keystrokes:

  * The composer is located as a real UIA element, so we know it exists before
    typing a single character — and if it is missing, we say so rather than
    pretending.
  * Text arrives by clipboard paste. Copilot's composer is a React-controlled
    web element: it ignores programmatic value changes (ValuePattern is accepted
    and discarded), and pyautogui's scancode typing drops every non-ASCII
    character, which quietly mangles 'é', 'ñ' and '¿' — the exact characters a
    Spanish question is made of. A paste is one real input event carrying full
    Unicode, and it is what actions/computer_control.py already falls back to
    for long text.
  * The reply is read out of the transcript, which Copilot marks up as
    'You said: …' / 'Copilot said: …' groups.

The one non-obvious prerequisite is _wake_accessibility(). Copilot renders in a
WebView2, and Chromium builds its accessibility tree lazily — until a client
asks for it, the web content looks like an empty pane and every lookup here
fails. Sending WM_GETOBJECT is how a screen reader asks; we ask the same way.
"""

from __future__ import annotations

import platform
import re
import subprocess
import time

_IMPORT_ERROR = ""
if platform.system() == "Windows":
    try:
        import pyperclip
        import win32con
        import win32gui
        import win32process
        from pywinauto import Desktop
        from pywinauto.keyboard import send_keys
    except Exception as exc:          # a missing optional dep must not break startup
        _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
else:
    _IMPORT_ERROR = f"Copilot is Windows-only; this machine runs {platform.system()}."


PLUGIN = {
    "name": "copilot_bridge",
    "description": (
        "Talks to the Microsoft Copilot desktop app: opens its chat, types a question into it, "
        "waits for Copilot's answer and reads that answer back out loud. Use this for ANY request "
        "that involves asking Copilot something or writing in Copilot's chat — do NOT use "
        "computer_control or open_app for that, they cannot see the chat box or read the reply. "
        "action='ask' (default) sends a question and returns Copilot's answer: 'pregúntale a "
        "Copilot qué capacidades tiene', 'escribe en el chat de Copilot que...', 'dile a Copilot "
        "que...', 'consúltale a Copilot', 'ask Copilot what it can do', 'write in Copilot's chat'. "
        "Put the question itself in 'text'. "
        "action='connect' just opens and focuses the Copilot chat without asking anything: "
        "'conéctate con Copilot', 'conéctate a Copilot', 'abre el chat de Copilot', 'abre Copilot', "
        "'connect to Copilot', 'open the Copilot chat'. "
        "action='read' re-reads the answer already on screen, for '¿qué respondió Copilot?', "
        "'léeme la respuesta de Copilot', 'read Copilot's answer again'. "
        "action='close' shuts the app down: 'desconéctate de Copilot', 'cierra Copilot', "
        "'disconnect from Copilot', 'close Copilot'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "ask (default) | connect | read | close",
            },
            "text": {
                "type": "STRING",
                "description": "The question or message to put to Copilot. Required for action='ask'.",
            },
        },
        "required": [],
    },
}

# Matched against the owning process, not the window title: several things call
# themselves "Copilot" (an Edge tab, a sidebar), and only these are the app.
_PROCESSES = {"m365copilot.exe", "copilot.exe"}

# shell:AppsFolder is the only launch path a packaged app reliably answers to;
# open_app's Start-Menu typing is kept as the last resort because it depends on
# focus landing where it expects.
_APP_IDS = (
    "Microsoft.MicrosoftOfficeHub_8wekyb3d8bbwe!Microsoft.MicrosoftOfficeHub",
    "Microsoft.Copilot_8wekyb3d8bbwe!App",
)

_SAID = "Copilot said:"
_OBJID_CLIENT = 0xFFFFFFFC

# A reply is finished once it has stopped growing for this long. Copilot streams
# token by token with no completion signal, so silence is the only signal there.
_SETTLE_SECONDS = 2.5
_POLL_SECONDS = 1.2
_DEFAULT_TIMEOUT = 90

# Spoken aloud, so long enough to be a real answer and short enough not to be a
# monologue. The full text still reaches the activity log.
_SPOKEN_LIMIT = 1500


# ── window plumbing ──────────────────────────────────────────────────────────

def _copilot_hwnd() -> int | None:
    """Handle of the visible Copilot window, or None when it is not running."""
    found: list[int] = []

    def visit(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd) or not win32gui.GetWindowText(hwnd).strip():
            return True
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            name = _process_name(pid)
        except Exception:
            return True
        if name in _PROCESSES:
            found.append(hwnd)
        return True

    win32gui.EnumWindows(visit, None)
    return found[0] if found else None


def _process_name(pid: int) -> str:
    try:
        import psutil
        return psutil.Process(pid).name().lower()
    except Exception:
        return ""


def _wake_accessibility(hwnd: int) -> None:
    """Ask the WebView2 renderer to build its accessibility tree.

    Without this every lookup below finds an empty pane: Chromium keeps the tree
    off until a client requests it, and WM_GETOBJECT is that request."""
    targets = [hwnd]
    try:
        win32gui.EnumChildWindows(hwnd, lambda h, _: targets.append(h), None)
    except Exception:
        pass
    for handle in targets:
        try:
            win32gui.SendMessageTimeout(
                handle, win32con.WM_GETOBJECT, 0, _OBJID_CLIENT,
                win32con.SMTO_ABORTIFHUNG, 1000,
            )
        except Exception:
            pass
    time.sleep(1.2)


def _launch() -> bool:
    for app_id in _APP_IDS:
        try:
            subprocess.Popen(
                ["explorer.exe", f"shell:AppsFolder\\{app_id}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            continue
        for _ in range(20):
            time.sleep(1.0)
            if _copilot_hwnd():
                return True
    try:
        from actions.open_app import open_app
        open_app({"app_name": "Copilot"})
        for _ in range(12):
            time.sleep(1.0)
            if _copilot_hwnd():
                return True
    except Exception:
        pass
    return False


def _window(launch_if_needed: bool = True):
    """The Copilot window, woken and ready to be queried."""
    hwnd = _copilot_hwnd()
    if hwnd is None and launch_if_needed:
        if not _launch():
            return None
        hwnd = _copilot_hwnd()
    if hwnd is None:
        return None
    _wake_accessibility(hwnd)
    return Desktop(backend="uia").window(handle=hwnd)


def _descend(element, match, depth: int = 0, limit: int = 16, budget=None):
    """First descendant satisfying `match`, breadth of the search capped."""
    if budget is None:
        budget = {"n": 3000}
    if depth > limit or budget["n"] <= 0:
        return None
    for child in element.children():
        budget["n"] -= 1
        if budget["n"] <= 0:
            return None
        try:
            if match(child):
                return child
            hit = _descend(child, match, depth + 1, limit, budget)
            if hit is not None:
                return hit
        except Exception:
            continue
    return None


def _composer(window):
    return _descend(window, lambda c: c.element_info.control_type == "Edit")


def _reply_turns(window) -> list:
    """Every 'Copilot said: …' turn in the transcript, oldest first."""
    turns: list = []

    def walk(element, depth=0, budget={"n": 4000}):
        if depth > 20 or budget["n"] <= 0:
            return
        for child in element.children():
            budget["n"] -= 1
            if budget["n"] <= 0:
                return
            try:
                info = child.element_info
                if info.control_type == "Group" and (info.name or "").startswith(_SAID):
                    turns.append(child)
                walk(child, depth + 1, budget)
            except Exception:
                continue

    walk(window)
    return turns


def _turn_text(turn) -> str:
    """Flatten one reply into prose fit for speaking.

    Copilot nests the emphasised part of a line inside the line itself — a
    ListItem carrying the whole bullet, holding a Text node with just its bold
    lead-in. Taking both says the lead-in twice, so the outermost node that has
    text wins and its children are left alone."""
    parts: list[str] = []

    def walk(element, depth=0, budget={"n": 1200}):
        if depth > 18 or budget["n"] <= 0:
            return
        for child in element.children():
            budget["n"] -= 1
            if budget["n"] <= 0:
                return
            try:
                info = child.element_info
                kind = info.control_type
                # The per-reply toolbar (Copy Response, feedback buttons) is
                # chrome, not part of what Copilot said.
                if kind == "ToolBar":
                    continue
                name = (info.name or "").strip()
                if kind in ("Text", "ListItem") and name and name != _SAID:
                    if not parts or parts[-1] != name:
                        parts.append(name)
                    continue          # its children only repeat fragments of it
                walk(child, depth + 1, budget)
            except Exception:
                continue

    walk(turn)
    text = " ".join(parts).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _is_placeholder(text: str) -> bool:
    """True for the 'Putting it together…' style filler Copilot shows first.

    Keyed on shape rather than wording — a short line trailing an ellipsis — so
    it holds in whatever language Copilot answers in."""
    return len(text) < 40 and text.rstrip().endswith(("…", "..."))


def _await_reply(window, baseline: str, timeout: int) -> str:
    """Block until the newest reply differs from `baseline` and stops growing.

    Counting turns does not work: Copilot virtualises the transcript, exposing
    only the turns near the viewport, so the number of 'Copilot said:' groups
    stays flat while older ones fall out of the tree. Watching the text of the
    last turn change is the signal that survives that."""
    deadline = time.time() + timeout
    last, settled_at = "", None
    while time.time() < deadline:
        time.sleep(_POLL_SECONDS)
        turns = _reply_turns(window)
        if not turns:
            continue
        text = _turn_text(turns[-1])
        if not text or text == baseline or _is_placeholder(text):
            continue
        if text == last:
            if settled_at is None:
                settled_at = time.time()
            elif time.time() - settled_at >= _SETTLE_SECONDS:
                return text
        else:
            last, settled_at = text, None
    return last


def _send(window, box, question: str) -> None:
    """Put the question in the composer and submit it."""
    window.set_focus()
    time.sleep(0.4)
    box.click_input()
    time.sleep(0.3)

    saved = ""
    try:
        saved = pyperclip.paste()
    except Exception:
        pass
    try:
        send_keys("^a")          # replace whatever the box was already holding
        time.sleep(0.15)
        pyperclip.copy(question)
        time.sleep(0.15)
        send_keys("^v")
        time.sleep(0.5)
        send_keys("{ENTER}")
    finally:
        try:
            pyperclip.copy(saved)
        except Exception:
            pass


# ── actions ──────────────────────────────────────────────────────────────────

def _act_connect() -> str:
    window = _window()
    if window is None:
        return "I could not open Copilot, sir. It may not be installed on this machine."
    try:
        window.set_focus()
    except Exception:
        pass
    if _composer(window) is None:
        return "Copilot is open, sir, but its chat box has not finished loading yet."
    return "Copilot is open and its chat is ready, sir."


def _say(player, instruction: str) -> None:
    """Speak while run() is still blocked, the way phone_notifications does."""
    try:
        say = getattr(player, "request_say", None)
        if callable(say):
            say(instruction)
    except Exception:
        pass


def _act_ask(question: str, timeout: int, player=None) -> str:
    if not question:
        return "What would you like me to ask Copilot, sir?"

    window = _window()
    if window is None:
        return "I could not open Copilot, sir. It may not be installed on this machine."

    box = _composer(window)
    if box is None:
        return "I found Copilot, sir, but not its chat box — it may still be loading."

    turns = _reply_turns(window)
    baseline = _turn_text(turns[-1]) if turns else ""
    _send(window, box, question)
    # Copilot can take half a minute to answer. Say so now rather than leaving
    # the user with silence until the tool response finally lands.
    _say(player, "Tell the user briefly that you have put the question to Copilot "
                 "and are waiting for its answer.")
    answer = _await_reply(window, baseline, timeout)

    if not answer:
        return (
            "I put the question to Copilot, sir, but it had not answered before I "
            "stopped waiting. The question is in its chat if you want to look."
        )
    return answer


def _act_read() -> str:
    window = _window(launch_if_needed=False)
    if window is None:
        return "Copilot is not open, sir."
    turns = _reply_turns(window)
    if not turns:
        return "Copilot has not said anything yet, sir."
    text = _turn_text(turns[-1])
    return text or "I can see Copilot's reply, sir, but I could not read any text from it."


def _act_close() -> str:
    hwnd = _copilot_hwnd()
    if hwnd is None:
        return "Copilot is already closed, sir."
    try:
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    except Exception as exc:
        return f"I could not close Copilot, sir: {exc}"
    for _ in range(10):
        time.sleep(0.4)
        if _copilot_hwnd() is None:
            return "Copilot is closed, sir."
    return "I asked Copilot to close, sir, but its window is still up."


_ACTIONS = {"ask", "connect", "open", "read", "close", "disconnect"}


def run(parameters: dict, player=None, session_memory=None) -> str:
    if _IMPORT_ERROR:
        return f"Sir, the Copilot bridge is unavailable: {_IMPORT_ERROR}"

    parameters = parameters or {}
    action = (parameters.get("action") or "ask").strip().lower()
    text = (parameters.get("text") or "").strip()
    if action not in _ACTIONS:
        action = "ask" if text else "connect"

    if player:
        try:
            player.write_log(f"[Copilot] {action}")
        except Exception:
            pass

    try:
        if action in ("connect", "open"):
            result = _act_connect()
        elif action in ("close", "disconnect"):
            result = _act_close()
        elif action == "read":
            result = _act_read()
        else:
            timeout = int(parameters.get("timeout") or _DEFAULT_TIMEOUT)
            result = _act_ask(text, timeout, player)
    except Exception as exc:
        print(f"[Copilot] {type(exc).__name__}: {exc}")
        return f"Sir, the Copilot bridge failed: {exc}"

    print(f"[Copilot] {action} → {result[:120]}")
    if player:
        try:
            player.write_log(f"LUMINA: {result[:200]}")
        except Exception:
            pass

    if len(result) > _SPOKEN_LIMIT:
        return result[:_SPOKEN_LIMIT].rsplit(" ", 1)[0] + "… That is as far as I will read, sir."
    return result
