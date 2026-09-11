"""
Read completed replies from local Codex and Claude Code chat histories.

The storage formats were verified on Windows against the installations on this
machine. Codex stores JSONL rollouts below ``~/.codex/sessions`` (with older
rollouts in ``~/.codex/archived_sessions``). A completed assistant reply is a
``response_item`` whose payload is an assistant ``message`` with
``phase=final_answer``; commentary, reasoning, tool calls, and tool outputs are
separate records and are intentionally ignored.

Claude Code stores project chats below ``~/.claude/projects`` even when its CLI
is not currently on PATH. Its completed replies are top-level ``assistant``
records whose message has ``role=assistant`` and ``stop_reason=end_turn``.
Thinking blocks, tool-use blocks, metadata, and sidechain agent messages are
ignored.

Reading those structured, append-only history files is done directly: no window
automation, no screenshots, no clipboard, no stolen focus, and a partially
written JSONL line is skipped safely.

Sending cannot work that way. Neither extension exposes an API or a CLI that
posts into the panel on screen, so writing goes through VS Code's own command
palette and the keyboard — see the section further down, which is the only part
of this plugin that touches the editor's window.
"""

from __future__ import annotations

import json
import os
import platform
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PLUGIN = {
    "name": "developer_chat_reader",
    "description": (
        "Reads and writes the local Codex and Claude Code developer chats in VS Code. "
        "action='read' (the default) returns the latest completed assistant response to be "
        "spoken aloud. Use it whenever the user asks what Codex or Claude Code answered, even "
        "if the chat application is already open. Do NOT use open_app, computer_control or "
        "copilot_bridge for these; they cannot read these structured developer chats, and "
        "Copilot is a different product. "
        "Read triggers: 'lee la respuesta de Codex', 'que respondio Claude Code', "
        "'leeme el chat de Codex', 'read the latest Claude Code response', 'what did Codex say'. "
        "action='send' writes a message into that chat and sends it, which is how the user talks "
        "to Codex or Claude Code through you: 'escribele a Claude Code que...', 'dile a Codex "
        "que...', 'mandale un mensaje a Claude Code', 'preguntale a Codex...', 'write to Claude "
        "Code', 'tell Codex to...', 'send this to Codex'. Put the message itself in 'text', and "
        "write it in the language the user used — another assistant reads it and answers in the "
        "language it was asked in. "
        "source='codex' | 'claude' | 'both' chooses the chat."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "read (default) | send",
            },
            "source": {
                "type": "STRING",
                "description": "Chat to read from or write to: codex | claude | both",
            },
            "text": {
                "type": "STRING",
                "description": (
                    "The message to write into the chat. Required for action='send'. "
                    "Write it in the language the user spoke in."
                ),
            },
        },
        "required": [],
    },
}


_SPOKEN_LIMIT = 2200
_MAX_FILES_PER_SOURCE = 40
_FINAL_CODEX_PHASES = {"final", "final_answer"}
_FINAL_CLAUDE_REASONS = {"end_turn", "max_tokens", "stop_sequence"}
_MEMORY_CITATION_RE = re.compile(
    r"<oai-mem-citation>.*?</oai-mem-citation>",
    flags=re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class ChatReply:
    source: str
    text: str
    timestamp: float
    path: Path


def _configured_home(variable: str, fallback: Path) -> Path:
    value = os.environ.get(variable, "").strip()
    return Path(value).expanduser() if value else fallback


def _codex_roots() -> tuple[Path, ...]:
    home = _configured_home("CODEX_HOME", Path.home() / ".codex")
    return home / "sessions", home / "archived_sessions"


def _claude_roots() -> tuple[Path, ...]:
    home = _configured_home("CLAUDE_CONFIG_DIR", Path.home() / ".claude")
    return (home / "projects",)


def _history_files(roots: Iterable[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        try:
            files.extend(path for path in root.rglob("*.jsonl") if path.is_file())
        except OSError:
            continue
    files.sort(key=lambda path: _modified_time(path), reverse=True)
    return files[:_MAX_FILES_PER_SOURCE]


def _modified_time(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _jsonl_rows(path: Path):
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line_number, line in enumerate(stream):
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, UnicodeError):
                    continue
                if isinstance(row, dict):
                    yield line_number, row
    except OSError:
        return


def _timestamp(value: Any, path: Path, line_number: int) -> float:
    if isinstance(value, str) and value.strip():
        normalized = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            pass
    return _modified_time(path) + (line_number / 1_000_000)


def _content_text(content: Any, block_types: set[str]) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") not in block_types:
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n\n".join(parts)


def _latest_codex_reply() -> ChatReply | None:
    best: ChatReply | None = None
    fallback: ChatReply | None = None
    for path in _history_files(_codex_roots()):
        for line_number, row in _jsonl_rows(path):
            if row.get("type") != "response_item":
                continue
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            if payload.get("type") != "message" or payload.get("role") != "assistant":
                continue
            phase = str(payload.get("phase") or "").strip().lower()
            if phase and phase not in _FINAL_CODEX_PHASES:
                continue
            text = _content_text(payload.get("content"), {"output_text", "text"})
            if not text:
                continue
            reply = ChatReply(
                source="Codex",
                text=text,
                timestamp=_timestamp(row.get("timestamp"), path, line_number),
                path=path,
            )
            if not phase and (fallback is None or reply.timestamp > fallback.timestamp):
                fallback = reply
            if phase in _FINAL_CODEX_PHASES and (best is None or reply.timestamp > best.timestamp):
                best = reply
    return best or fallback


def _latest_claude_reply() -> ChatReply | None:
    best: ChatReply | None = None
    fallback: ChatReply | None = None
    for path in _history_files(_claude_roots()):
        for line_number, row in _jsonl_rows(path):
            if row.get("type") != "assistant" or row.get("isSidechain") is True:
                continue
            if row.get("isMeta") is True:
                continue
            message = row.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            text = _content_text(message.get("content"), {"text"})
            if not text:
                continue
            reply = ChatReply(
                source="Claude Code",
                text=text,
                timestamp=_timestamp(row.get("timestamp"), path, line_number),
                path=path,
            )
            reason = str(message.get("stop_reason") or "").strip().lower()
            if not reason and (fallback is None or reply.timestamp > fallback.timestamp):
                fallback = reply
            if reason in _FINAL_CLAUDE_REASONS and (best is None or reply.timestamp > best.timestamp):
                best = reply
    return best or fallback


def _speech_text(text: str) -> str:
    text = _MEMORY_CITATION_RE.sub("", text)
    text = re.sub(r"!\[([^]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"(?m)^#{1,6}\s+", "", text)
    text = re.sub(r"(?m)^\s*[-*+]\s+", "", text)
    text = text.replace("```", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    shortened = text[:limit].rsplit(" ", 1)[0].rstrip()
    return shortened + "... That is as far as I will read aloud."


def _format_reply(reply: ChatReply | None, limit: int) -> str:
    if reply is None:
        return ""
    text = _speech_text(reply.text)
    if not text:
        return ""
    return _shorten(text, limit)


def _normalize_source(value: Any) -> str:
    source = str(value or "").strip().lower().replace("_", " ").replace("-", " ")
    if source in {"codex", "openai codex", "codex cli"}:
        return "codex"
    if source in {"claude", "claude code", "anthropic", "anthropic claude"}:
        return "claude"
    if source in {"both", "all", "codex and claude", "claude and codex"}:
        return "both"
    return ""


def _read_source(source: str) -> str:
    if source == "codex":
        reply = _latest_codex_reply()
        text = _format_reply(reply, _SPOKEN_LIMIT)
        return text or "I could not find a completed Codex response in the local chat history."

    if source == "claude":
        reply = _latest_claude_reply()
        text = _format_reply(reply, _SPOKEN_LIMIT)
        return text or "I could not find a completed Claude Code response in the local chat history."

    codex = _format_reply(_latest_codex_reply(), _SPOKEN_LIMIT // 2)
    claude = _format_reply(_latest_claude_reply(), _SPOKEN_LIMIT // 2)
    if not codex and not claude:
        return "I could not find completed Codex or Claude Code responses in the local chat history."
    parts = []
    if codex:
        parts.append(f"Latest Codex response: {codex}")
    if claude:
        parts.append(f"Latest Claude Code response: {claude}")
    return "\n\n".join(parts)


# ── writing into the chats ───────────────────────────────────────────────────
#
# Reading is done from files; sending cannot be. Neither extension exposes an
# API, a port or a CLI that posts into the panel you are looking at, so the only
# way in is the way a person uses: focus the window, run the command, type.
#
# The Copilot approach — find the composer as a UIA element and write to it —
# was tried first and does not apply here. VS Code's accessibility tree was
# probed after the usual WM_GETOBJECT wake-up and exposes 117 nodes with not one
# Edit control in them; the chat panels are webviews whose insides never surface.
#
# What VS Code does expose is its command palette, and both extensions register
# commands for exactly this. Those command titles are the contract:
#
#     claude-vscode.focus   "Claude Code: Focus input"
#     chatgpt.openSidebar   "Open Codex Sidebar"
#
# Everything below was measured against the installed extensions
# (anthropic.claude-code 2.1.268, openai.chatgpt 26.908) rather than assumed.

_WINDOW_TITLE = "Visual Studio Code"

_PALETTE_COMMANDS = {
    "claude": "Claude Code: Focus input",
    "codex": "Open Codex Sidebar",
}

# Codex's sidebar has no composer at all until a chat exists in the window —
# the panel comes up empty and a paste goes nowhere. This opens one.
_CODEX_NEW_CHAT = "New Chat in ChatGPT Sidebar"

# Typing into a window is global state: there is one keyboard and one focused
# control. Two sends at once interleave their keystrokes into one garbled
# message, so they queue instead.
_send_lock = threading.Lock()

_SEND_ERROR = ""
if platform.system() == "Windows":
    try:
        import pyautogui
        import pyperclip
        import win32con
        import win32gui
        from pywinauto import Desktop
    except Exception as exc:        # a missing optional dep must not break reading
        _SEND_ERROR = f"{type(exc).__name__}: {exc}"
else:
    _SEND_ERROR = f"Writing into VS Code is Windows-only; this machine runs {platform.system()}."


def _vscode_window():
    """The VS Code window handle, or None when the editor is not running."""
    found = []

    def visit(handle, _):
        try:
            if win32gui.IsWindowVisible(handle) and _WINDOW_TITLE in win32gui.GetWindowText(handle):
                found.append(handle)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(visit, None)
    except Exception:
        return None
    return found[0] if found else None


def _focus_window(handle) -> None:
    """Bring VS Code forward.

    SetForegroundWindow on its own fails with "Element not found" (error 1168):
    Windows refuses to let a process that does not already own the foreground
    take it. pywinauto performs the AttachThreadInput dance that makes the
    request legal, which is why this does not call win32gui directly.
    """
    if win32gui.IsIconic(handle):
        win32gui.ShowWindow(handle, win32con.SW_RESTORE)
    Desktop(backend="uia").window(handle=handle).set_focus()
    time.sleep(0.7)


def _run_palette_command(title: str) -> None:
    """Run a VS Code command by its title through the command palette."""
    pyautogui.hotkey("ctrl", "shift", "p")
    time.sleep(0.7)
    # Pasted rather than typed: the palette filters on every keystroke, and a
    # typed title races its own filtering.
    pyperclip.copy(">" + title)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.9)
    pyautogui.press("enter")
    time.sleep(1.3)


def _type_and_send(text: str) -> None:
    """Replace whatever the composer holds, then send.

    Ctrl+A first is not defensive tidiness. Both composers keep a draft, and a
    paste without it appends: a test message landed in Codex as "Create an
    image ofPRUEBA de Lumina", which is what the model would then have been
    asked.

    The text arrives by clipboard because pyautogui's scancode typing drops
    every non-ASCII character — it would quietly mangle 'é', 'ñ' and '¿', the
    characters a Spanish instruction is made of.
    """
    pyperclip.copy(text)
    time.sleep(0.2)
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.2)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.6)
    pyautogui.press("enter")
    time.sleep(0.4)


def _send_to_chat(source: str, text: str) -> str:
    """Put one message into Codex's or Claude Code's chat and send it."""
    if _SEND_ERROR:
        return f"I cannot write into VS Code: {_SEND_ERROR}"
    if not text:
        return "What would you like me to write to them?"

    handle = _vscode_window()
    if handle is None:
        return "VS Code is not open, so there is no chat to write into."

    label = "Claude Code" if source == "claude" else "Codex"
    saved = ""
    try:
        saved = pyperclip.paste()
    except Exception:
        pass

    with _send_lock:
        try:
            _focus_window(handle)
            _run_palette_command(_PALETTE_COMMANDS[source])
            if source == "codex":
                # Harmless when a chat is already open, and the difference
                # between working and silently typing into nothing when one
                # is not.
                _run_palette_command(_CODEX_NEW_CHAT)
            _type_and_send(text)
        except Exception as exc:
            print(f"[DeveloperChat] send failed: {type(exc).__name__}: {exc}")
            return f"I could not write to {label}: {exc}"
        finally:
            try:
                pyperclip.copy(saved)
            except Exception:
                pass

    print(f"[DeveloperChat] sent to {label}: {text[:100]}")
    return (
        f"I have written that to {label} and sent it. Ask me to read their "
        "answer once they have had a moment to reply."
    )


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Read from or write to a developer chat, and never raise to the caller."""
    parameters = parameters or {}
    action = str(parameters.get("action") or "").strip().lower()
    text = str(parameters.get("text") or "").strip()
    source = _normalize_source(parameters.get("source"))

    # Supplying text only makes sense for a message being sent, so text with no
    # action named is a send. Everything else defaults to reading, which is what
    # this plugin did before it could write and what it must keep doing when the
    # model is vague.
    if action not in {"read", "send"}:
        action = "send" if text else "read"

    if action == "send":
        if not source:
            return "Should I write that to Codex or to Claude Code?"
        targets = ("codex", "claude") if source == "both" else (source,)
        replies = []
        for target in targets:
            try:
                replies.append(_send_to_chat(target, text))
            except Exception as exc:
                print(f"[DeveloperChat] {type(exc).__name__}: {exc}")
                replies.append(f"I could not write to {target}: {exc}")
        result = " ".join(replies)
        if player:
            try:
                player.write_log(f"[Developer chats] Sent to {source}: {text[:120]}")
            except Exception:
                pass
        return result

    if not source:
        return "Would you like me to read the latest response from Codex or Claude Code?"

    try:
        result = _read_source(source)
    except Exception as exc:
        print(f"[DeveloperChatReader] {type(exc).__name__}: {exc}")
        return f"I could not read the {source} chat history: {exc}"

    print(f"[DeveloperChatReader] {source} -> {len(result)} spoken characters")
    if player:
        try:
            player.write_log(f"[Developer chats] Read latest {source} response")
        except Exception:
            pass
    return result
