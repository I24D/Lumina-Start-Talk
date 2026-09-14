"""Optional local engines for objective grammar and pronunciation feedback."""

from __future__ import annotations

import importlib.util
import os
import time
from pathlib import Path
from typing import Any

import requests


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


class LanguageToolProvider:
    """Use a self-hosted LanguageTool server; never sends learner text publicly."""

    def __init__(self, endpoint: str | None = None, *, timeout: float = 1.2):
        self.endpoint = (endpoint or os.getenv("LUMINA_LANGUAGETOOL_URL") or "http://127.0.0.1:8081/v2/check").rstrip("/")
        self.timeout = timeout
        self._retry_after = 0.0
        self._last_error = ""
        self._available = False

    def status(self) -> dict[str, Any]:
        return {
            "id": "languagetool",
            "label": "LanguageTool",
            "state": "ready" if self._available else "optional",
            "detail": "Verificación gramatical local activa" if self._available else "Servidor local opcional en 127.0.0.1:8081",
            "lastError": self._last_error,
        }

    def check(self, text: str, language: str = "en-US") -> list[dict[str, str]]:
        text = str(text or "").strip()
        if not text or time.monotonic() < self._retry_after:
            return []
        try:
            response = requests.post(
                self.endpoint,
                data={"text": text, "language": language},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            self._available = True
            self._last_error = ""
        except Exception as exc:
            self._available = False
            self._last_error = type(exc).__name__
            self._retry_after = time.monotonic() + 60
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
    """Status and analysis adapter for the optional OpenPronounce runtime."""

    def __init__(self, endpoint: str | None = None, *, timeout: float = 120.0):
        self.endpoint = (endpoint or os.getenv("LUMINA_OPENPRONOUNCE_URL") or "").rstrip("/")
        self.timeout = timeout
        self._installed = importlib.util.find_spec("openpronounce") is not None
        self._last_error = ""

    def status(self) -> dict[str, Any]:
        ready = bool(self.endpoint or self._installed)
        return {
            "id": "openpronounce",
            "label": "OpenPronounce",
            "state": "ready" if ready else "not-installed",
            "detail": "Evaluación fonética local disponible" if ready else "Paquete opcional; los modelos no se incluyen en Lumina",
            "lastError": self._last_error,
        }

    def analyze_file(self, audio_path: str | Path, expected_text: str) -> dict[str, Any] | None:
        path = Path(audio_path)
        expected = str(expected_text or "").strip()
        if not path.is_file() or not expected:
            return None
        try:
            if self.endpoint:
                with path.open("rb") as audio:
                    response = requests.post(
                        f"{self.endpoint}/pronunciation",
                        files={"file": (path.name, audio, "audio/wav")},
                        data={"expected_text": expected, "lang": "en"},
                        timeout=self.timeout,
                    )
                response.raise_for_status()
                return _json_safe(response.json())
            if self._installed:
                from openpronounce import compare_audio_with_text, load_audio

                return _json_safe(
                    compare_audio_with_text(load_audio(str(path)), expected, lang="en")
                )
        except Exception as exc:
            self._last_error = type(exc).__name__
        return None
