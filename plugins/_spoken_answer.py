"""
How another assistant's answer is handed to Lumina to be spoken.

The user's rule: an answer of up to two thousand words is read out complete,
word for word, and only a longer one is summarised, closing with "La respuesta
completa la encuentras en el chat". Every plugin that relays an answer —
Copilot, ChatGPT, OpenClaw, Codex and Claude Code — goes through spoken_answer.

The words are counted here rather than left to the model. Asked to judge the
length by itself, Gemini cut a 399-word Claude Code answer to 80 words and a
135-word one to 59 (interaction_log, 2026-09-13).
"""

from __future__ import annotations

READ_IN_FULL_WORDS = 2_000

# A runaway guard, not an editorial limit: a summary has to see the whole answer
# to cover it, and the longest Claude Code answer on this machine was 2,042
# words, about 11,500 characters. Only a transcript scrape gone wrong gets near
# this.
MAX_CHARS = 60_000

# Recognised by the "Reading in full" and "Answering in depth" rules in
# core/prompt.txt.
READ_IN_FULL = "[READ_IN_FULL]\n"
ANSWER_IN_DEPTH = "[ANSWER_IN_DEPTH]\n"


def spoken_answer(text: str) -> str:
    """``text`` behind the marker that tells Lumina to read it whole or summarise it."""
    text = text.strip()
    marker = READ_IN_FULL if len(text.split()) <= READ_IN_FULL_WORDS else ANSWER_IN_DEPTH
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS].rsplit(" ", 1)[0] + "…"
    return marker + text
