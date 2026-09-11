"""Every turn Lumina and the user exchange, written to Supabase.

Until now the only thing that survived a session was the memory *document* —
the handful of facts save_memory judged worth keeping. The conversation itself
lived in ``JarvisApp._session_log`` and died with the process. "Do you remember
what we worked out yesterday?" had no answer available to it, because nothing
had been written down: the tables were there, and empty.

``public.interaction_log`` is that record. One row per completed turn, holding
what the user said and what Lumina answered, append-only — so recalling is a
search over what actually happened rather than a read of whichever summary
happened to survive.

Two properties matter more here than completeness:

  * **It must never delay speech.** Rows are queued and written by one
    background thread; the voice loop hands over a pair of strings and moves
    on. A Supabase that is slow, unreachable or switched off costs nothing at
    the microphone.
  * **It must never raise into its caller.** A failure produces one console
    line and nothing else — the same contract ``memory/supabase_store.py``
    already keeps. Memory is an enhancement to the assistant, never a
    precondition for it.

Reads are the opposite case and are deliberately synchronous: recall happens
inside a tool call that the model is already waiting on, and a recall that
returns after the answer has been spoken is worse than no recall at all.
"""

from __future__ import annotations

import atexit
import queue
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from memory.supabase_store import Credentials, credentials

TABLE_NAME = "interaction_log"

# The column is user_id, but this is a single-user desktop assistant: one
# person owns the machine, the microphone and the memory. A constant keeps the
# rows joinable if that ever stops being true.
DEFAULT_USER_ID = "dal"

# Reads block a tool call, writes do not — so they get different patience.
WRITE_TIMEOUT = (2.5, 8.0)
READ_TIMEOUT = (2.5, 6.0)

# A turn is a sentence or two of speech. Anything past this is a transcript
# glitch or a tool result that leaked into the log, and truncating it keeps one
# bad row from making every later write time out.
MAX_FIELD_CHARS = 8_000

# Written by one thread, so ordering is preserved; bounded so a Supabase outage
# grows memory by a known amount instead of without limit.
_QUEUE_MAX = 500

# (what was said, what was answered, when it happened). The timestamp travels
# with the row because the write can be seconds behind the turn, and a memory
# whose clock is the network's rather than the conversation's reorders itself.
_queue: queue.Queue[tuple[str, str, str] | None] = queue.Queue(maxsize=_QUEUE_MAX)
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()
_session: requests.Session | None = None
_credentials: Credentials | None = None
_base_dir: Path | None = None
_dropped = 0


def _config(base_dir: Path) -> Credentials:
    global _credentials, _base_dir
    if _credentials is None or _base_dir != base_dir:
        _credentials = credentials(base_dir)
        _base_dir = base_dir
    return _credentials


def _http() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


def _headers(config: Credentials, *, write: bool = False) -> dict[str, str]:
    headers = {
        "apikey": config.key,
        "Authorization": f"Bearer {config.key}",
        "Accept": "application/json",
        "Accept-Profile": config.schema,
    }
    if write:
        headers.update(
            {
                "Content-Type": "application/json",
                "Content-Profile": config.schema,
                # Nothing here reads the row back, and asking for it doubles the
                # response for no purpose.
                "Prefer": "return=minimal",
            }
        )
    return headers


def _endpoint(config: Credentials) -> str:
    return f"{config.url}/rest/v1/{TABLE_NAME}"


def _clip(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= MAX_FIELD_CHARS else text[:MAX_FIELD_CHARS] + "…"


def _post(config: Credentials, rows: list[dict[str, Any]]) -> None:
    response = _http().post(
        _endpoint(config),
        headers=_headers(config, write=True),
        json=rows,
        timeout=WRITE_TIMEOUT,
    )
    if not response.ok:
        # Never the body: it can echo the row, and rows are conversation.
        raise RuntimeError(f"HTTP {response.status_code}")


def _drain() -> None:
    """Write queued turns until told to stop.

    Batches whatever has piled up while the previous request was in flight. A
    normal conversation queues one row at a time and this sends one row; a
    reconnect that flushes a backlog sends it in a single POST.
    """
    while True:
        item = _queue.get()
        if item is None:
            _queue.task_done()
            return

        batch = [item]
        while len(batch) < 50:
            try:
                extra = _queue.get_nowait()
            except queue.Empty:
                break
            if extra is None:
                # Shutdown arrived mid-batch: write what we have, then leave.
                _write_batch(batch)
                _queue.task_done()
                for _ in batch:
                    _queue.task_done()
                return
            batch.append(extra)

        _write_batch(batch)
        for _ in batch:
            _queue.task_done()


def _write_batch(batch: list[tuple[str, str, str]]) -> None:
    global _dropped
    if _base_dir is None:
        return
    config = _config(_base_dir)
    rows = [
        {
            "user_id": DEFAULT_USER_ID,
            "user_message": _clip(said),
            "reply": _clip(answered),
            "created_at": when,
        }
        for said, answered, when in batch
    ]
    try:
        _post(config, rows)
    except Exception as exc:
        _dropped += len(rows)
        print(f"[Memory] conversation log: {len(rows)} turn(s) not saved ({exc}); "
              f"{_dropped} lost this session.")


def _ensure_worker() -> None:
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(
                target=_drain, name="lumina-conversation-log", daemon=True
            )
            _worker.start()


def log_turn(base_dir: Path, said: str, answered: str) -> None:
    """Queue one completed turn. Returns immediately; never raises."""
    global _dropped
    said = (said or "").strip()
    answered = (answered or "").strip()
    if not said and not answered:
        return
    # A transcript fragment like "." or "eh" is a microphone artefact, not
    # something either of them said. Nothing answered it, and keeping it only
    # makes every later recall noisier.
    if not answered and len(re.sub(r"[^0-9A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+", "", said)) < 2:
        return
    try:
        config = _config(base_dir)
        if not config.configured or not config.allow_writes:
            return
        _ensure_worker()
        _queue.put_nowait((said, answered, datetime.now(timezone.utc).isoformat()))
    except queue.Full:
        _dropped += 1
    except Exception as exc:
        print(f"[Memory] conversation log unavailable: {type(exc).__name__}: {exc}")


def flush(timeout: float = 5.0) -> None:
    """Let the queue finish at shutdown. Best effort, bounded, never raises."""
    try:
        if _worker is None or not _worker.is_alive():
            return
        _queue.put_nowait(None)
        _worker.join(timeout=timeout)
    except Exception:
        pass


atexit.register(flush)


# ── recall ───────────────────────────────────────────────────────────────────

def _rows_to_lines(rows: list[dict[str, Any]]) -> list[str]:
    lines = []
    for row in reversed(rows):          # oldest first reads like a conversation
        stamp = str(row.get("created_at") or "")[:16].replace("T", " ")
        said = (row.get("user_message") or "").strip()
        answered = (row.get("reply") or "").strip()
        if said:
            lines.append(f"[{stamp}] You: {said}")
        if answered:
            lines.append(f"[{stamp}] Lumina: {answered}")
    return lines


def _rpc_search(config: Credentials, query: str, limit: int) -> list[dict[str, Any]] | None:
    """Ask Postgres to match without accents. None when the function is absent.

    Spanish is exactly where a plain ILIKE gives up: the model asks about
    "computacion cuantica" and the stored turn says "computación cuántica", so
    the two never meet and Lumina reports forgetting something she is holding.
    unaccent() folds both sides, and it has to run in the database because the
    rows that would need folding are the ones still on the server.
    """
    response = _http().post(
        f"{config.url}/rest/v1/rpc/lumina_search_interactions",
        headers=_headers(config, write=True) | {"Prefer": "return=representation"},
        json={"p_user_id": DEFAULT_USER_ID, "p_query": query, "p_limit": limit},
        timeout=READ_TIMEOUT,
    )
    if response.status_code in (404, 400):
        # The migration in memory/supabase_schema.sql has not been applied here.
        return None
    if not response.ok:
        raise RuntimeError(f"HTTP {response.status_code}")
    rows = response.json()
    return rows if isinstance(rows, list) else None


def _ilike_search(config: Credentials, query: str, limit: int) -> list[dict[str, Any]]:
    """Accent-sensitive fallback, so recall still works without the migration."""
    params: dict[str, str] = {
        "select": "user_message,reply,created_at",
        "user_id": f"eq.{DEFAULT_USER_ID}",
        "order": "created_at.desc",
        "limit": str(limit),
    }
    if query:
        # PostgREST spells its wildcards with *, and a comma inside the pattern
        # would split the or= list — so it cannot survive in the search text.
        pattern = f"*{query.replace(',', ' ')}*"
        params["or"] = f"(user_message.ilike.{pattern},reply.ilike.{pattern})"

    response = _http().get(
        _endpoint(config), headers=_headers(config), params=params, timeout=READ_TIMEOUT
    )
    if not response.ok:
        raise RuntimeError(f"HTTP {response.status_code}")
    rows = response.json()
    return rows if isinstance(rows, list) else []


def search_history(base_dir: Path, query: str, limit: int = 12) -> str:
    """Find past turns mentioning `query`. Empty query returns the latest ones.

    Synchronous on purpose — see the module docstring. Returns plain text ready
    to hand to the model, or a sentence explaining why there is nothing.
    """
    try:
        config = _config(base_dir)
        if not config.configured:
            return "The conversation history is not configured on this machine."

        query = (query or "").strip()
        limit = max(1, min(limit, 50))
        rows = _rpc_search(config, query, limit)
        if rows is None:
            rows = _ilike_search(config, query, limit)
    except Exception as exc:
        return f"I could not read the conversation history ({type(exc).__name__})."

    if not rows:
        if query:
            return f"Nothing in our past conversations mentions '{query}'."
        return "There are no earlier conversations stored yet."

    header = (
        f"From our earlier conversations, about '{query}':"
        if query
        else "The most recent things we talked about:"
    )
    return header + "\n" + "\n".join(_rows_to_lines(rows))
