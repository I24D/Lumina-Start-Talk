"""Local engines for objective grammar and pronunciation feedback.

Both run on this PC (see engines.py) and start with a class. Learner text and
voice never go to LanguageTool's or OpenPronounce's public services.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import requests

from .engines import LanguageToolServer, PronunciationWorker


def _json_safe(value: Any) -> Any:
    """Convert model arrays/scalars into values FastAPI can serialize."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "tolist"):
        return _json_safe(value.tolist())
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            pass
    return str(value)


def _engine_status(engine: Any, *, ready_detail: str) -> tuple[str, str]:
    if engine.ready.is_set():
        return "ready", ready_detail
    if engine.starting:
        return "starting", "Arrancando en este equipo…"
    if engine.installed:
        return "installed", "Instalado; se activa al empezar la clase"
    return "not-installed", "Instálalo con: python -m learning_english.engines install"


class LanguageToolProvider:
    """LanguageTool for the class, or the server at LUMINA_LANGUAGETOOL_URL."""

    def __init__(
        self,
        endpoint: str | None = None,
        *,
        server: LanguageToolServer | None = None,
        timeout: float = 4.0,
    ):
        self._external = (endpoint or os.getenv("LUMINA_LANGUAGETOOL_URL") or "").rstrip("/")
        self.server = None if self._external else (server or LanguageToolServer())
        self.timeout = timeout
        self._last_error = ""

    @property
    def endpoint(self) -> str:
        return self._external or self.server.endpoint

    @property
    def ready(self) -> bool:
        return bool(self._external) or self.server.ready.is_set()

    def start(self) -> None:
        if self.server is not None:
            self.server.start()

    def stop(self) -> None:
        if self.server is not None:
            self.server.stop()

    def status(self) -> dict[str, Any]:
        if self._external:
            state = "optional" if self._last_error else "ready"
            detail = f"Servidor externo ({self._external})"
        else:
            state, detail = _engine_status(self.server, ready_detail="Revisión gramatical local activa")
        return {
            "id": "languagetool",
            "label": "LanguageTool",
            "state": state,
            "detail": detail,
            "lastError": self._last_error,
        }

    def check(self, text: str, language: str = "en-US") -> list[dict[str, str]]:
        text = str(text or "").strip()
        if not text or not self.ready:
            return []
        try:
            response = requests.post(
                self.endpoint,
                data={"text": text, "language": language},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            self._last_error = ""
        except Exception as exc:
            self._last_error = type(exc).__name__
            return []

        corrections: list[dict[str, str]] = []
        for match in payload.get("matches", [])[:6]:
            try:
                offset = max(0, int(match.get("offset", 0)))
                length = max(0, int(match.get("length", 0)))
                original = text[offset : offset + length]
                replacements = match.get("replacements") or []
                corrected = str(replacements[0].get("value") or "") if replacements else ""
                if not original or not corrected:
                    continue
                issue = str((match.get("rule") or {}).get("issueType") or "grammar").lower()
                category = "vocabulary" if issue in {"misspelling", "typographical"} else "grammar"
                corrections.append({
                    "original": original,
                    "corrected": corrected,
                    "explanation": str(match.get("message") or "LanguageTool encontró una mejora objetiva.")[:500],
                    "category": category,
                    "source": "LanguageTool",
                })
            except (TypeError, ValueError, AttributeError):
                continue
        return corrections


class OpenPronounceProvider:
    """OpenPronounce for the class, or the server at LUMINA_OPENPRONOUNCE_URL."""

    def __init__(
        self,
        endpoint: str | None = None,
        *,
        worker: PronunciationWorker | None = None,
        timeout: float = 120.0,
    ):
        self._external = (endpoint or os.getenv("LUMINA_OPENPRONOUNCE_URL") or "").rstrip("/")
        self.worker = None if self._external else (worker or PronunciationWorker())
        self.timeout = timeout
        self._last_error = ""

    @property
    def ready(self) -> bool:
        return bool(self._external) or self.worker.ready.is_set()

    def start(self) -> None:
        if self.worker is not None:
            self.worker.start()

    def stop(self) -> None:
        if self.worker is not None:
            self.worker.stop()

    def status(self) -> dict[str, Any]:
        if self._external:
            state, detail = "ready", f"Servidor externo ({self._external})"
        else:
            state, detail = _engine_status(self.worker, ready_detail="Evaluación fonética local activa")
        return {
            "id": "openpronounce",
            "label": "OpenPronounce",
            "state": state,
            "detail": detail,
            "lastError": self._last_error,
        }

    def analyze_file(self, audio_path: str | Path, expected_text: str) -> dict[str, Any] | None:
        path = Path(audio_path)
        expected = str(expected_text or "").strip()
        if not path.is_file() or not expected or not self.ready:
            return None
        try:
            if self._external:
                with path.open("rb") as audio:
                    response = requests.post(
                        f"{self._external}/pronunciation",
                        files={"file": (path.name, audio, "audio/wav")},
                        data={"expected_text": expected, "lang": "en"},
                        timeout=self.timeout,
                    )
                response.raise_for_status()
                result = response.json()
                result.pop("prosody", None)
            else:
                result = self.worker.analyze(path, expected, timeout=self.timeout)
                if result is None:
                    self._last_error = self.worker.last_error
                    return None
            self._last_error = ""
            return _json_safe(result)
        except Exception as exc:
            self._last_error = type(exc).__name__
            return None
