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
import threading
import time

from plugins._spoken_answer import spoken_answer

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
        "and reads Copilot's answer out loud when it arrives. Use this for ANY request "
        "that involves asking Copilot something or writing in Copilot's chat — do NOT use "
        "computer_control or open_app for that, they cannot see the chat box or read the reply. "
        "You are the messenger, not the judge: you do not know what Copilot can and cannot do, so "
        "never refuse a request or claim Copilot lacks a capability. Put the question to it and "
        "let its own answer say so. "
        "action='ask' (default) sends a question and returns at once; Copilot's answer then "
        "reaches you by itself as a [DELAYED_ANSWER] message: 'pregúntale a "
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
                "description": (
                    "The question or message to put to Copilot. Required for action='ask'. "
                    "Write it in the language the user asked in. This is not a machine parameter: "
                    "it is a question for another assistant, and Copilot answers in whatever "
                    "language it is asked, so an English question means the user hears an English "
                    "answer read back to them."
                ),
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

# Copilot adds a reply's toolbar (Copy Response, the feedback buttons) when the
# reply is finished, so a reply with its toolbar whose text has held still this
# long is done. The toolbar is found by control type, not by button names, so it
# holds in any interface language.
_FINISHED_STEADY_SECONDS = 0.25
# Without a toolbar (a different Copilot build), a reply that has stopped growing
# for this long is taken as finished, as it always was.
_SETTLE_SECONDS = 2.5
# A poll reads only the conversation, about 20 ms, so it can run this often.
# Walking the whole window takes half a second (measured 2026-09-15); with the
# old 1.2 s pause and 2.5 s settle, an answer reached Lumina 3.5-5 s after
# Copilot had finished writing it.
_POLL_SECONDS = 0.3
_DEFAULT_TIMEOUT = 90
# Nobody waits on the tool call any more, so this only bounds a stuck Copilot.
_BACKGROUND_TIMEOUT = 300

class _Detailed(str):
    """A Copilot answer, as opposed to one of the plugin's own status lines.

    Everything this plugin returns reaches Gemini as a tool result, and a model
    handed a long tool result compresses it to a sentence or two by default —
    which turns "ask Copilot to explain quantum computing" into a headline.
    Marking the string lets run() pass it through spoken_answer, which tells the
    model to read it whole or, past two thousand words, to summarise it. The
    short status lines ("Copilot is closed, sir.") stay unmarked and get spoken
    naturally.
    """


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


def _conversation(turn):
    """The element that holds every turn of the chat, found from one reply.

    Each reply sits in a wrapper of its own, so the conversation is the first
    ancestor with more than one child. Reading its children takes about 20 ms;
    walking the whole window takes half a second (measured 2026-09-15)."""
    node = turn
    for _ in range(6):
        node = node.parent()
        if node is None:
            return None
        if len(node.children()) > 1:
            return node
    return None


def _latest_reply(conversation):
    """The newest 'Copilot said' group in the conversation, or None."""
    for wrapper in reversed(conversation.children()):
        for element in (wrapper, *wrapper.children()):
            info = element.element_info
            if info.control_type == "Group" and (info.name or "").startswith(_SAID):
                return element
    return None


def _is_finished(turn) -> bool:
    """Whether Copilot has added the reply's toolbar, which it does once it is done."""
    return any(child.element_info.control_type == "ToolBar" for child in turn.children())


def _await_reply(window, baseline: str, timeout: int) -> str:
    """Block until the newest reply differs from `baseline` and is finished.

    Counting turns does not work: Copilot virtualises the transcript, exposing
    only the turns near the viewport, so the number of 'Copilot said:' groups
    stays flat while older ones fall out of the tree. Watching the text of the
    last turn change is the signal that survives that.

    After the first full walk only the conversation is read; every tenth poll
    walks the whole window again, in case Copilot has rebuilt it."""
    deadline = time.time() + timeout
    conversation, polls = None, 0
    last, changed_at = "", time.time()
    while time.time() < deadline:
        time.sleep(_POLL_SECONDS)
        polls += 1
        turn = None
        if conversation is not None and polls % 10:
            try:
                turn = _latest_reply(conversation)
            except Exception:
                conversation = None
        if turn is None:
            turns = _reply_turns(window)
            if not turns:
                continue
            turn = turns[-1]
            conversation = _conversation(turn)
        text = _turn_text(turn)
        if not text or text == baseline or _is_placeholder(text):
            continue
        now = time.time()
        if text != last:
            last, changed_at = text, now
            continue
        steady = now - changed_at
        if steady >= _SETTLE_SECONDS or (steady >= _FINISHED_STEADY_SECONDS and _is_finished(turn)):
            return text
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
    """Ask Lumina to speak. Called from the background job, after run() has returned."""
    try:
        say = getattr(player, "request_say", None)
        if callable(say):
            say(instruction)
    except Exception:
        pass


# One question at a time. While Copilot answers, the user keeps talking — a stray
# remark, a cough the transcriber turns into words, the assistant's own voice
# returning through the speakers — and this plugin's description is emphatic
# enough that the model routes those straight back here. Measured in a real
# session: one question sent to Copilot three times and three answers spoken
# over each other, which is also what "I cannot hear you properly" sounds like
# from the other side.
#
# The model cannot know the bridge is busy. The bridge can.
_ask_lock = threading.Lock()
_asking = ""


def _deliver(question: str, answer: str, player=None) -> None:
    """Speak an answer that arrived after its tool call had returned."""
    print(f"[Copilot] answered after the fact: {len(answer)} chars")
    if player:
        try:
            player.write_log(f"[Copilot] answer received ({len(answer)} chars)")
        except Exception:
            pass
    _say(
        player,
        "[DELAYED_ANSWER] Copilot has answered the question you put to it "
        f"('{question[:120]}'). Its answer follows.\n\n" + spoken_answer(answer),
    )


def _run_job(question: str, timeout: int, player=None) -> None:
    """Background worker: put the question to Copilot, wait, speak the answer."""
    global _asking
    answer, error = "", ""
    try:
        window = _window()
        if window is None:
            error = "Copilot would not open; it may not be installed on this machine"
        else:
            box = _composer(window)
            if box is None:
                error = "Copilot's chat box was not there; it may still have been loading"
            else:
                turns = _reply_turns(window)
                baseline = _turn_text(turns[-1]) if turns else ""
                _send(window, box, question)
                answer = _await_reply(window, baseline, timeout)
                if not answer:
                    error = "Copilot did not answer in time; the question is in its chat"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        with _ask_lock:
            _asking = ""

    if answer:
        _deliver(question, answer, player)
        return
    print(f"[Copilot] ask failed: {error}")
    _say(player, "The question you put to Copilot earlier did not get an answer. "
                 f"Tell the user so in one sentence, and say why: {error}")


def _act_ask(question: str, timeout: int, player=None) -> str:
    """Hand the question over and return at once.

    Gemini 3.1 Flash Live cancels an open tool call the moment any text reaches
    it. This call used to stay open while Copilot answered and say "I have asked
    Copilot" in the middle of it: the call was cancelled, the model called tools
    again, and the answer returned later was never read (measured against the
    API on 2026-09-15). Returning now and delivering the answer as a
    [DELAYED_ANSWER] is what ChatGPT's and OpenClaw's bridges already do."""
    global _asking
    if not question:
        return "What would you like me to ask Copilot, sir?"

    with _ask_lock:
        if _asking:
            return (
                f"I am still waiting for Copilot to answer '{_asking[:80]}', sir. "
                "I will read the answer out as soon as it arrives."
            )
        _asking = question

    threading.Thread(
        target=_run_job,
        args=(question, max(timeout, _BACKGROUND_TIMEOUT), player),
        name="lumina-copilot-ask",
        daemon=True,
    ).start()
    return ("I have put that question to Copilot, sir. I will read its answer out loud "
            "the moment it arrives.")


def _act_read() -> str:
    window = _window(launch_if_needed=False)
    if window is None:
        return "Copilot is not open, sir."
    turns = _reply_turns(window)
    if not turns:
        return "Copilot has not said anything yet, sir."
    text = _turn_text(turns[-1])
    if text:
        return _Detailed(text)
    return "I can see Copilot's reply, sir, but I could not read any text from it."


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

    print(f"[Copilot] {action} → {len(result)} chars → {result[:120]}")
    if player:
        try:
            player.write_log(f"LUMINA: {result[:200]}")
        except Exception:
            pass

    if isinstance(result, _Detailed):
        return spoken_answer(result)
    return result
