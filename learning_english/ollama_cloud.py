"""Gemma 4 on Ollama Cloud: the fast helper model for Learning English.

Measured on 2026-09-14 with Lumina's own analysis schema: 2.3 to 2.9 s per
analysed turn, every required key present, and corrections as good as Gemini's
("My brother have 25 years" became "My brother is 25 years old", which
LanguageTool missed). In the first probe Ollama Cloud ignored the schema sent
as ``format`` (a fenced reply with renamed keys), so the schema also rides in the
system prompt and fences are stripped. Writing a whole checkpoint stalled past
180 s, which is why Gemma 4 leads the turn analysis but only backs Gemini up
for generated activities.

Settings come from the environment first, then Lumina's .env:
OLLAMA_CLOUD_ENABLED, OLLAMA_CLOUD_BASE_URL, OLLAMA_CLOUD_API_KEY and,
optionally, LUMINA_LEARNING_OLLAMA_MODEL. The key is never logged.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from memory.supabase_store import _read_dotenv


DEFAULT_MODEL = "gemma4:31b"
DEFAULT_BASE_URL = "https://ollama.com"
_FALSE_VALUES = {"", "0", "false", "no", "off"}
_FENCES = re.compile(r"^```(?:json)?\s*|\s*```$")


class OllamaCloudError(RuntimeError):
    """Carries the HTTP status as ``code``, never the response body."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class OllamaCloud:
    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    enabled: bool = False
    model: str = DEFAULT_MODEL

    @classmethod
    def from_env(cls, base_dir: Path | None = None) -> "OllamaCloud":
        base_dir = base_dir or Path(__file__).resolve().parent.parent
        dotenv = _read_dotenv(base_dir / ".env")

        def setting(name: str, default: str = "") -> str:
            return (os.environ.get(name) or dotenv.get(name) or default).strip()

        base_url = setting("OLLAMA_CLOUD_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
        if base_url.endswith("/api"):
            base_url = base_url[: -len("/api")]
        return cls(
            base_url=base_url,
            api_key=setting("OLLAMA_CLOUD_API_KEY"),
            enabled=setting("OLLAMA_CLOUD_ENABLED", "false").lower() not in _FALSE_VALUES,
            model=setting("LUMINA_LEARNING_OLLAMA_MODEL", DEFAULT_MODEL),
        )

    @property
    def configured(self) -> bool:
        return self.enabled and self.base_url.startswith("https://") and bool(self.api_key)

    def chat_json(
        self,
        *,
        system: str,
        request: dict[str, Any],
        schema: dict[str, Any],
        temperature: float,
        timeout: float,
    ) -> str:
        """The model's JSON text. Transport failures raise OllamaCloudError."""
        prompt = (
            f"{system}\nReturn one JSON object only, without markdown fences, matching "
            "this JSON schema exactly, with the same key names:\n"
            + json.dumps(schema, ensure_ascii=False)
        )
        try:
            response = requests.post(
                f"{self.base_url}/api/chat",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "stream": False,
                    "think": False,
                    "format": schema,
                    "options": {"temperature": temperature},
                    "messages": [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
                    ],
                },
                timeout=(5, timeout),
            )
        except requests.Timeout:
            raise OllamaCloudError("Ollama Cloud timed out", 503) from None
        except requests.RequestException as exc:
            raise OllamaCloudError(f"Ollama Cloud is unreachable ({type(exc).__name__})", 503) from None
        if response.status_code != 200:
            raise OllamaCloudError(f"Ollama Cloud HTTP {response.status_code}", response.status_code)
        try:
            content = response.json()["message"]["content"]
        except (ValueError, KeyError, TypeError):
            raise ValueError("Ollama Cloud returned an unexpected reply") from None
        return _FENCES.sub("", str(content).strip())
