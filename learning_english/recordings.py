"""Student voice turns kept in Supabase for Learning English.

Each spoken student turn becomes an audio object in the private
``learning-voice`` bucket and one row in ``public.learning_voice_recordings``
holding its transcript, the tutor's reply, the class context and, when
OpenPronounce scored it, the pronunciation result. The schema lives in
memory/supabase_schema.sql.

The contract is the one memory/conversation_log.py keeps: saving never delays
the class and never raises into its caller. Audio is compressed to Opus with
ffmpeg when it is installed (about 3 KB per second of speech) and kept as
16 kHz WAV otherwise.
"""

from __future__ import annotations

import io
import queue
import shutil
import subprocess
import threading
import time
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

from memory.supabase_store import Credentials, credentials


BUCKET = "learning-voice"
TABLE = "learning_voice_recordings"
WORKSPACE_ID = "lumina-start-talk"
MAX_SECONDS = 30
TIMEOUT = (3.0, 20.0)
_QUEUE_MAX = 100
_TEXT_LIMIT = 4000
_TEXT_FIELDS = (
    "audience", "level", "mode", "unit_id", "scenario_id",
    "transcript", "tutor_text", "expected_text",
)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def encode_audio(pcm: bytes, sample_rate: int) -> tuple[bytes, str, str]:
    """(audio bytes, content type, file extension) for 16-bit mono PCM."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        try:
            done = subprocess.run(
                [
                    ffmpeg, "-hide_banner", "-loglevel", "error",
                    "-f", "s16le", "-ar", str(sample_rate), "-ac", "1", "-i", "pipe:0",
                    "-c:a", "libopus", "-b:a", "24k", "-f", "ogg", "pipe:1",
                ],
                input=pcm, capture_output=True, timeout=30, creationflags=_NO_WINDOW,
            )
            if done.returncode == 0 and done.stdout:
                return done.stdout, "audio/ogg", "ogg"
        except (OSError, subprocess.SubprocessError):
            pass
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buffer.getvalue(), "audio/wav", "wav"


class VoiceRecordings:
    def __init__(
        self,
        base_dir: Path,
        *,
        session: requests.Session | None = None,
        config_loader: Callable[[Path], Credentials] = credentials,
        encoder: Callable[[bytes, int], tuple[bytes, str, str]] = encode_audio,
    ):
        self._base_dir = base_dir
        self._http = session or requests.Session()
        self._config_loader = config_loader
        self._config_cache: Credentials | None = None
        self._encode = encoder
        self._queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()
        self.saved = 0
        self.failed = 0

    def _config(self) -> Credentials:
        if self._config_cache is None:
            self._config_cache = self._config_loader(self._base_dir)
        return self._config_cache

    @property
    def available(self) -> bool:
        config = self._config()
        return config.configured and config.allow_writes

    def save(self, pcm: bytes, sample_rate: int, details: dict[str, Any]) -> bool:
        """Queue one student turn. Returns immediately and never raises."""
        try:
            if not pcm or not self.available:
                return False
            pcm = pcm[: sample_rate * 2 * MAX_SECONDS]
            self._ensure_worker()
            self._queue.put_nowait((pcm, sample_rate, dict(details), datetime.now(timezone.utc)))
            return True
        except queue.Full:
            self.failed += 1
            return False
        except Exception as exc:
            print(f"[Learning English] voice recording unavailable ({type(exc).__name__})")
            return False

    def flush(self, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.05)

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._drain, name="learning-voice-recordings", daemon=True
                )
                self._worker.start()

    def _drain(self) -> None:
        while True:
            item = self._queue.get()
            try:
                self._upload(*item)
                self.saved += 1
            except Exception as exc:
                self.failed += 1
                # Never the response body: it can echo what the student said.
                print(f"[Learning English] voice recording not saved ({exc})")
            finally:
                self._queue.task_done()

    @staticmethod
    def _headers(config: Credentials, **extra: str) -> dict[str, str]:
        return {"apikey": config.key, "Authorization": f"Bearer {config.key}", **extra}

    def _upload(self, pcm: bytes, sample_rate: int, details: dict[str, Any], when: datetime) -> None:
        config = self._config()
        audio, content_type, extension = self._encode(pcm, sample_rate)
        session_id = "".join(ch for ch in str(details.get("session_id") or "") if ch.isalnum())[:64] or "no-session"
        path = f"{when:%Y/%m}/{session_id}/{when:%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}.{extension}"
        stored = self._http.post(
            f"{config.url}/storage/v1/object/{BUCKET}/{path}",
            headers=self._headers(config, **{"Content-Type": content_type, "x-upsert": "false"}),
            data=audio,
            timeout=TIMEOUT,
        )
        if not stored.ok:
            raise RuntimeError(f"storage HTTP {stored.status_code}")
        score = details.get("pronunciation_score")
        pronunciation = details.get("pronunciation")
        row = {
            "workspace_id": WORKSPACE_ID,
            "session_id": session_id,
            "recorded_at": when.isoformat(),
            **{key: str(details.get(key) or "")[:_TEXT_LIMIT] for key in _TEXT_FIELDS},
            "duration_ms": round(len(pcm) / 2 / sample_rate * 1000),
            "storage_bucket": BUCKET,
            "storage_path": path,
            "content_type": content_type,
            "size_bytes": len(audio),
            "pronunciation_score": score if isinstance(score, (int, float)) else None,
            "pronunciation": pronunciation if isinstance(pronunciation, dict) else None,
        }
        inserted = self._http.post(
            f"{config.url}/rest/v1/{TABLE}",
            headers=self._headers(config, **{
                "Content-Type": "application/json",
                "Content-Profile": config.schema,
                "Prefer": "return=minimal",
            }),
            json=row,
            timeout=TIMEOUT,
        )
        if not inserted.ok:
            # Audio without its row could never be found or deleted.
            self._delete_objects(config, [path])
            raise RuntimeError(f"table HTTP {inserted.status_code}")

    def _delete_objects(self, config: Credentials, paths: list[str]) -> None:
        for start in range(0, len(paths), 100):
            response = self._http.delete(
                f"{config.url}/storage/v1/object/{BUCKET}",
                headers=self._headers(config, **{"Content-Type": "application/json"}),
                json={"prefixes": paths[start : start + 100]},
                timeout=TIMEOUT,
            )
            if not response.ok:
                raise RuntimeError(f"storage HTTP {response.status_code}")

    def delete_all(self) -> int:
        """Delete every saved voice turn, audio and rows. Raises on failure."""
        config = self._config()
        if not config.configured:
            return 0
        # Uploads still queued would otherwise land after the deletion.
        self.flush()
        removed = 0
        while True:
            listed = self._http.get(
                f"{config.url}/rest/v1/{TABLE}",
                headers=self._headers(config, **{"Accept-Profile": config.schema}),
                params={"select": "id,storage_path", "workspace_id": f"eq.{WORKSPACE_ID}", "limit": "500"},
                timeout=TIMEOUT,
            )
            if not listed.ok:
                raise RuntimeError(f"table HTTP {listed.status_code}")
            rows = listed.json()
            if not rows:
                return removed
            self._delete_objects(config, [row["storage_path"] for row in rows])
            deleted = self._http.delete(
                f"{config.url}/rest/v1/{TABLE}",
                headers=self._headers(config, **{"Content-Profile": config.schema, "Prefer": "return=minimal"}),
                params={"id": f"in.({','.join(row['id'] for row in rows)})"},
                timeout=TIMEOUT,
            )
            if not deleted.ok:
                raise RuntimeError(f"table HTTP {deleted.status_code}")
            removed += len(rows)
