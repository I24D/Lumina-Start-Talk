"""Supabase persistence for Lumina's local memory document.

The remote contract uses ``public.lumina_state_documents`` by default. Each
application owns one row identified by ``workspace_id``, ``scope``, and
``document_key``. The complete memory document is stored as JSONB so the
existing local schema remains backward compatible.

Runtime configuration is read from the process environment first and then from
the repository ``.env`` file:

``SUPABASE_URL``
    Project URL, for example ``https://example.supabase.co``.
``SUPABASE_SERVICE_ROLE_KEY``
    Trusted local key. It is never logged or returned by this module.
``LUMINA_SUPABASE_SCHEMA``
    Data API schema. Defaults to ``public``.
``LUMINA_SUPABASE_ALLOW_WRITES``
    Must be true for remote upserts. Reads remain available when false.

This desktop integration intentionally keeps ``memory/long_term.json`` as the
fast offline source. Supabase adds recovery and cross-run persistence; a network
failure never prevents Lumina from starting or remembering locally.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

import requests


TABLE_NAME = "lumina_state_documents"
WORKSPACE_ID = "lumina-start-talk"
DOCUMENT_SCOPE = "memory"
DOCUMENT_KEY = "long-term-v1"
DEFAULT_SCHEMA = "public"
REQUEST_TIMEOUT = (2.5, 6.0)

_FALSE_VALUES = {"", "0", "false", "no", "off"}
_SCHEMA_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SupabaseStoreError(RuntimeError):
    """A safe error that never contains credentials or response bodies."""


@dataclass(frozen=True)
class RemoteDocument:
    payload: dict[str, Any]
    updated_at: str


def document_hash(payload: dict[str, Any]) -> str:
    """Return a stable content hash for synchronization decisions."""
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_dotenv(path: Path) -> dict[str, str]:
    """Read simple KEY=value entries without adding a dotenv dependency."""
    if not path.exists():
        return {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return {}

    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def _setting(name: str, dotenv: dict[str, str], default: str = "") -> str:
    return (os.environ.get(name) or dotenv.get(name) or default).strip()


@dataclass(frozen=True)
class Credentials:
    """How this machine reaches Supabase, resolved once and shared.

    conversation_log needs exactly the same four answers this module already
    works out, and two modules each deciding for themselves what "configured"
    means is how one of them quietly stops writing.
    """

    url: str
    key: str
    schema: str
    allow_writes: bool

    @property
    def configured(self) -> bool:
        return self.url.startswith("https://") and bool(self.key)


def credentials(base_dir: Path) -> Credentials:
    """Read Supabase settings from the environment, then from the .env file."""
    dotenv = _read_dotenv(base_dir / ".env")
    schema = _setting("LUMINA_SUPABASE_SCHEMA", dotenv, DEFAULT_SCHEMA)
    writes = _setting("LUMINA_SUPABASE_ALLOW_WRITES", dotenv, "false").lower()
    return Credentials(
        url=_setting("SUPABASE_URL", dotenv).rstrip("/"),
        key=_setting("SUPABASE_SERVICE_ROLE_KEY", dotenv),
        schema=schema if _SCHEMA_PATTERN.fullmatch(schema) else DEFAULT_SCHEMA,
        allow_writes=writes not in _FALSE_VALUES,
    )


class SupabaseMemoryStore:
    """Small PostgREST client for one versioned Lumina memory document."""

    def __init__(self, base_dir: Path):
        dotenv = _read_dotenv(base_dir / ".env")
        self.url = _setting("SUPABASE_URL", dotenv).rstrip("/")
        self._key = _setting("SUPABASE_SERVICE_ROLE_KEY", dotenv)
        schema = _setting("LUMINA_SUPABASE_SCHEMA", dotenv, DEFAULT_SCHEMA)
        self.schema = schema if _SCHEMA_PATTERN.fullmatch(schema) else DEFAULT_SCHEMA
        writes = _setting("LUMINA_SUPABASE_ALLOW_WRITES", dotenv, "false").lower()
        self.allow_writes = writes not in _FALSE_VALUES
        self._session = requests.Session()

    @property
    def configured(self) -> bool:
        return self.url.startswith("https://") and bool(self._key)

    @property
    def endpoint(self) -> str:
        return f"{self.url}/rest/v1/{TABLE_NAME}"

    def _headers(self, *, write: bool = False) -> dict[str, str]:
        headers = {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Accept": "application/json",
            "Accept-Profile": self.schema,
        }
        if write:
            headers.update(
                {
                    "Content-Type": "application/json",
                    "Content-Profile": self.schema,
                    "Prefer": "resolution=merge-duplicates,return=representation",
                }
            )
        return headers

    @staticmethod
    def _raise_safe(response: requests.Response, operation: str) -> None:
        if response.ok:
            return
        raise SupabaseStoreError(
            f"Supabase {operation} failed with HTTP {response.status_code}."
        )

    def fetch(self) -> RemoteDocument | None:
        """Fetch this application's remote document, or None when absent."""
        if not self.configured:
            return None
        try:
            response = self._session.get(
                self.endpoint,
                headers=self._headers(),
                params={
                    "select": "payload,updated_at",
                    "workspace_id": f"eq.{WORKSPACE_ID}",
                    "scope": f"eq.{DOCUMENT_SCOPE}",
                    "document_key": f"eq.{DOCUMENT_KEY}",
                    "limit": "1",
                },
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise SupabaseStoreError(
                f"Supabase read is unavailable ({type(exc).__name__})."
            ) from None
        self._raise_safe(response, "read")
        try:
            rows = response.json()
        except ValueError:
            raise SupabaseStoreError("Supabase read returned invalid JSON.") from None
        if not isinstance(rows, list) or not rows:
            return None
        payload = rows[0].get("payload")
        if not isinstance(payload, dict):
            raise SupabaseStoreError("Supabase memory payload has an invalid shape.")
        return RemoteDocument(payload=payload, updated_at=str(rows[0].get("updated_at", "")))

    def upsert(self, payload: dict[str, Any]) -> RemoteDocument:
        """Create or replace this application's remote memory document."""
        if not self.configured:
            raise SupabaseStoreError("Supabase memory is not configured.")
        if not self.allow_writes:
            raise SupabaseStoreError("Supabase memory writes are disabled.")
        body = {
            "workspace_id": WORKSPACE_ID,
            "scope": DOCUMENT_SCOPE,
            "document_key": DOCUMENT_KEY,
            "payload": payload,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            response = self._session.post(
                self.endpoint,
                headers=self._headers(write=True),
                params={"on_conflict": "workspace_id,scope,document_key"},
                json=body,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise SupabaseStoreError(
                f"Supabase write is unavailable ({type(exc).__name__})."
            ) from None
        self._raise_safe(response, "write")
        try:
            rows = response.json()
        except ValueError:
            raise SupabaseStoreError("Supabase write returned invalid JSON.") from None
        if not isinstance(rows, list) or not rows or not isinstance(rows[0].get("payload"), dict):
            raise SupabaseStoreError("Supabase write returned an invalid document.")
        return RemoteDocument(
            payload=rows[0]["payload"],
            updated_at=str(rows[0].get("updated_at", "")),
        )


_store: SupabaseMemoryStore | None = None
_store_lock = Lock()
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lumina-memory-sync")
_pending: Future | None = None
_pending_lock = Lock()


def get_store(base_dir: Path) -> SupabaseMemoryStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = SupabaseMemoryStore(base_dir)
        return _store


def queue_upsert(base_dir: Path, payload: dict[str, Any]) -> Future | None:
    """Queue one ordered remote write while keeping the caller non-blocking."""
    global _pending
    store = get_store(base_dir)
    if not store.configured or not store.allow_writes:
        return None
    snapshot = json.loads(json.dumps(payload, ensure_ascii=False))

    def _write() -> RemoteDocument | None:
        try:
            return store.upsert(snapshot)
        except SupabaseStoreError as exc:
            print(f"[Memory] Supabase sync warning: {exc}")
            return None

    with _pending_lock:
        _pending = _executor.submit(_write)
        return _pending


def wait_for_pending_sync(timeout: float = 10.0) -> RemoteDocument | None:
    """Wait for the latest queued write; intended for shutdown and validation."""
    with _pending_lock:
        future = _pending
    if future is None:
        return None
    try:
        result = future.result(timeout=timeout)
    except Exception as exc:
        print(f"[Memory] Supabase sync warning: {type(exc).__name__}.")
        return None
    return result if isinstance(result, RemoteDocument) else None


@atexit.register
def _finish_pending_sync() -> None:
    wait_for_pending_sync(timeout=8.0)
    _executor.shutdown(wait=False, cancel_futures=False)
