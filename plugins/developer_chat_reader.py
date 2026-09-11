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

This plugin reads those structured, append-only history files directly. It does
not automate either application's window, take screenshots, use the clipboard,
or steal keyboard focus. A partially written JSONL line is skipped safely.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PLUGIN = {
    "name": "developer_chat_reader",
    "description": (
        "Reads the latest completed assistant response from local Codex and Claude Code chat "
        "history and returns it to be spoken aloud. Use this whenever the user asks what Codex "
        "or Claude Code answered, even if the chat application is already open. Do NOT use "
        "open_app, computer_control, or copilot_bridge; those tools cannot reliably read these "
        "structured developer chats, and Copilot is a different product. "
        "Set source='codex' for 'lee la respuesta de Codex', 'qué respondió Codex', 'léeme el "
        "chat de Codex', 'read the latest Codex response', or 'what did Codex say'. "
        "Set source='claude' for 'lee la respuesta de Claude Code', 'qué respondió Claude Code', "
        "'léeme el chat de Claude Code', 'read the latest Claude Code response', or 'what did "
        "Claude Code say'. Set source='both' when the user explicitly asks for both responses."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "source": {
                "type": "STRING",
                "description": "Chat history to read: codex | claude | both",
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


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Read a completed developer-chat response and never raise to the caller."""
    parameters = parameters or {}
    source = _normalize_source(parameters.get("source"))
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
