"""Gemini and OpenAI services for the English tutor."""

from __future__ import annotations

import json
import tempfile
import time
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from google import genai
from google.genai import types
import httpx

from memory.config_manager import (
    get_gemini_key, get_openai_key, get_learning_voice_provider,
)
from core.openai_realtime import (
    OPENAI_REALTIME_MODEL,
    create_call as create_realtime_call,
    session_config as realtime_session_config,
)
from .activities import build_request as build_activity_request
from .activities import schema_for, validate as validate_activity
from .ollama_cloud import OllamaCloud, OllamaCloudError
from .providers import LanguageToolProvider, OpenPronounceProvider
from .types import Correction, TutorResponse


PROMPT_PATH = Path(__file__).parent / "prompts" / "english_tutor.txt"
# The tutor talks through its own Live session in the browser. 3.1 Flash Live is
# the newest live voice model this key can open: measured on 2026-09-13 through
# an ephemeral token, first audio came 0.80 s after the speech ended, against
# 1.43 s for 2.5 native audio. 3.6 Flash has no live voice, so it analyses turns
# and writes the practice activities.
LIVE_MODEL = "models/gemini-3.1-flash-live-preview"
MODEL = "gemini-3.6-flash"
# Free-tier text quotas are per model and per day: 3.6 Flash allowed 20 requests
# a day on 2026-09-14, and every analysed turn spends one. When a model's quota
# is spent, the next lighter model takes over instead of the corrections stopping.
GEMINI_TEXT_MODELS = (MODEL, "gemini-3.5-flash-lite", "gemini-2.5-flash-lite")
OPENAI_TEXT_MODEL = "gpt-5.6-luna"
# Gemma 4 on Ollama Cloud answered an analysis in 2-3 s with every key and
# Gemini-grade corrections, so it leads the per-turn analysis and saves Gemini's
# quota. Writing a whole activity stalled past 180 s, so there it comes last.
ANALYSIS_CHAIN = (
    ("ollama", ""), *(("gemini", model) for model in GEMINI_TEXT_MODELS),
    ("openai", OPENAI_TEXT_MODEL),
)
ACTIVITY_CHAIN = (
    *(("gemini", model) for model in GEMINI_TEXT_MODELS),
    ("openai", OPENAI_TEXT_MODEL), ("ollama", ""),
)

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "assistantText": {"type": "string"},
        "speechText": {"type": "string"},
        "uiLanguage": {"type": "string"},
        "exerciseType": {
            "type": "string",
            "enum": [
                "conversation", "pronunciation", "grammar", "vocabulary", "listening",
                "reading", "writing", "quick-lesson", "assessment",
            ],
        },
        "corrections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "original": {"type": "string"},
                    "corrected": {"type": "string"},
                    "explanation": {"type": "string"},
                    "category": {"type": "string", "enum": ["grammar", "vocabulary", "pronunciation"]},
                    "pronunciation": {"type": "string"},
                    "translation": {"type": "string"},
                },
                "required": ["original", "corrected", "explanation", "category"],
            },
        },
        "newVocabulary": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "word": {"type": "string"},
                    "meaning": {"type": "string"},
                    "example": {"type": "string"},
                },
                "required": ["word", "meaning", "example"],
            },
        },
        "nextAction": {"type": "string"},
        "lessonProgress": {"type": "integer", "minimum": 0, "maximum": 100},
        "shouldWaitForUser": {"type": "boolean"},
        "profileUpdates": {
            "type": "object",
            "properties": {
                "primary_language": {"type": "string"},
                "audience": {"type": "string", "enum": ["kids", "teens", "adults"]},
                "level": {"type": "string", "enum": ["PRE-A1", "A1", "A2", "B1", "B2", "C1", "C2"]},
                "goal": {"type": "string"},
                "strengths": {"type": "string"},
                "difficulties": {"type": "string"},
                "preferred_speed": {"type": "string"},
                "current_objective": {"type": "string"},
            },
        },
    },
    "required": [
        "assistantText", "speechText", "uiLanguage", "exerciseType",
        "corrections", "newVocabulary", "nextAction", "lessonProgress",
        "shouldWaitForUser", "profileUpdates",
    ],
}


def _quota_spent(exc: Exception) -> bool:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return (getattr(exc, "code", None) == 429 or status == 429
            or "RESOURCE_EXHAUSTED" in str(exc))


def _busy(exc: Exception) -> bool:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return (getattr(exc, "code", None) in (500, 502, 503)
            or status in (500, 502, 503) or "UNAVAILABLE" in str(exc))


def _complete_analysis(raw: Any) -> dict[str, Any]:
    """A helper model that renames or drops keys must not pass as an analysis."""
    if not isinstance(raw, dict) or any(key not in raw for key in _RESPONSE_SCHEMA["required"]):
        raise ValueError("the analysis is missing required keys")
    return raw


class LearningEnglishService:
    def __init__(
        self,
        *,
        api_key_loader: Callable[[], str | None] = get_gemini_key,
        openai_key_loader: Callable[[], str | None] = get_openai_key,
        generator: Callable[..., Any] | None = None,
        token_factory: Callable[[dict[str, Any]], str] | None = None,
        client_factory: Callable[[str], Any] | None = None,
        ollama: OllamaCloud | None = None,
        grammar_provider: LanguageToolProvider | None = None,
        pronunciation_provider: OpenPronounceProvider | None = None,
        openai_call_factory: Callable[[dict[str, Any], str], str] | None = None,
        openai_response_factory: Callable[[dict[str, Any], str], Any] | None = None,
    ):
        self._api_key_loader = api_key_loader
        self._openai_key_loader = openai_key_loader
        self._generator = generator
        self._token_factory = token_factory
        self._client_factory = client_factory or (lambda key: genai.Client(api_key=key))
        self._ollama = ollama if ollama is not None else OllamaCloud.from_env()
        self._grammar = grammar_provider or LanguageToolProvider()
        self._pronunciation = pronunciation_provider or OpenPronounceProvider()
        self._openai_call_factory = openai_call_factory
        self._openai_response_factory = openai_response_factory
        self._system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
        # Text models whose quota ran out, and until when they are skipped.
        self._spent_until: dict[str, float] = {}

    @property
    def openai_available(self) -> bool:
        return bool((self._openai_key_loader() or "").strip())

    def provider_status(self) -> list[dict[str, Any]]:
        return [self._grammar.status(), self._pronunciation.status()]

    def start_engines(self, on_change: Callable[[], None] | None = None) -> None:
        """Start the local engines in the background for a class."""
        for provider in (self._grammar, self._pronunciation):
            engine = getattr(provider, "server", None) or getattr(provider, "worker", None)
            if engine is not None:
                engine.on_ready = on_change
            start = getattr(provider, "start", None)
            if start:
                start()

    def stop_engines(self) -> None:
        for provider in (self._grammar, self._pronunciation):
            stop = getattr(provider, "stop", None)
            if stop:
                stop()

    def pronunciation_ready(self) -> bool:
        return bool(getattr(self._pronunciation, "ready", False))

    def build_live_system_prompt(
        self,
        snapshot: dict[str, Any],
        *,
        assistant_name: str,
        user_name: str,
    ) -> str:
        profile = snapshot.get("profile", {})
        recent_errors = snapshot.get("frequent_errors", [])[:8]
        last_lesson = snapshot.get("last_lesson")
        context = {
            "assistant_name": assistant_name,
            "student_name": user_name,
            "profile": profile,
            "current_mode": snapshot.get("currentMode", "conversation"),
            "recent_errors": recent_errors,
            "last_lesson": last_lesson,
            "active_scenario": snapshot.get("activeScenario"),
            "current_unit": snapshot.get("currentUnit"),
            "level_complete": bool((snapshot.get("catalog") or {}).get("levelComplete")),
            "listening_activity": snapshot.get("activeListeningActivity"),
            "reviews_due": [
                {"word": item.get("word"), "meaning": item.get("meaning")}
                for item in snapshot.get("reviewQueue", [])[:6]
            ],
        }
        return self._system_prompt + "\n\n[STUDENT CONTEXT]\n" + json.dumps(
            context, ensure_ascii=False
        )

    def opening_instruction(self, snapshot: dict[str, Any]) -> str:
        profile = snapshot.get("profile", {})
        if profile.get("primary_language") and profile.get("goal") and profile.get("level_confirmed"):
            last = snapshot.get("last_lesson") or {}
            previous = str(last.get("summary") or "tu última práctica")
            reviews = len(snapshot.get("reviewQueue", []))
            return (
                "Da una bienvenida breve al estudiante. Menciona que la última vez "
                f"trabajaron en {previous}. "
                + (f"Indica que tiene {reviews} palabras listas para repasar. " if reviews else "")
                + "Pregunta si quiere continuar, repasar o elegir un escenario."
            )
        return (
            "Di exactamente al inicio: 'Modo Learning English activado. Desde ahora seré "
            "tu maestra personal de inglés.' Después pregunta una sola cosa: si quiere "
            "comenzar desde cero, continuar su progreso o hacer una prueba rápida de nivel."
        )

    def create_live_session(
        self,
        snapshot: dict[str, Any],
        *,
        assistant_name: str,
        user_name: str,
        voice_name: str,
        resume_handle: str | None = None,
    ) -> dict[str, Any]:
        """A single-use token for the tutor's own Live session in the browser.

        The browser never receives the API key. Everything that makes the
        tutor — model, prompt, voice, transcription — is locked inside the
        token, so the page can open exactly this session and nothing else. A
        resumed session carries its handle and skips the opening greeting."""
        config: dict[str, Any] = {
            "response_modalities": ["AUDIO"],
            "system_instruction": self.build_live_system_prompt(
                snapshot, assistant_name=assistant_name, user_name=user_name
            ),
            "input_audio_transcription": {},
            "output_audio_transcription": {},
            "speech_config": {
                "voice_config": {"prebuilt_voice_config": {"voice_name": voice_name}}
            },
            "session_resumption": {"handle": resume_handle} if resume_handle else {},
            "context_window_compression": {"sliding_window": {}},
        }
        now = datetime.now(timezone.utc)
        token_config = {
            "uses": 1,
            "expire_time": now + timedelta(minutes=30),
            "new_session_expire_time": now + timedelta(minutes=1),
            "live_connect_constraints": {"model": LIVE_MODEL, "config": config},
            "http_options": {"api_version": "v1alpha"},
        }
        if self._token_factory:
            token = self._token_factory(token_config)
        else:
            key = self._api_key_loader()
            if not key:
                raise RuntimeError("Gemini API key is not configured")
            client = genai.Client(api_key=key, http_options={"api_version": "v1alpha"})
            token = client.auth_tokens.create(config=token_config).name
        return {
            "provider": "gemini",
            "token": token,
            "model": LIVE_MODEL,
            "opening": "" if resume_handle else self.opening_instruction(snapshot),
        }

    def openai_session_description(
        self,
        snapshot: dict[str, Any],
        *,
        assistant_name: str,
        user_name: str,
        voice_name: str,
    ) -> dict[str, Any]:
        """Safe metadata the browser needs before it creates its WebRTC offer."""
        if not self.openai_available:
            raise RuntimeError(
                "OpenAI API key is not configured. Add it in Lumina > API Keys."
            )
        return {
            "provider": "openai",
            "model": OPENAI_REALTIME_MODEL,
            "voice": voice_name,
            "opening": self.opening_instruction(snapshot),
        }

    def create_openai_call(
        self,
        snapshot: dict[str, Any],
        *,
        assistant_name: str,
        user_name: str,
        voice_name: str,
        sdp: str,
    ) -> str:
        """Exchange the browser offer for an answer without exposing the key."""
        key = (self._openai_key_loader() or "").strip()
        if not key:
            raise RuntimeError(
                "OpenAI API key is not configured. Add it in Lumina > API Keys."
            )
        payload = {
            "sdp": sdp,
            "session": realtime_session_config(
                instructions=self.build_live_system_prompt(
                    snapshot, assistant_name=assistant_name, user_name=user_name
                ),
                voice=voice_name,
            ),
        }
        if self._openai_call_factory:
            return str(self._openai_call_factory(payload, key))
        return create_realtime_call(key, payload["sdp"], payload["session"])

    @staticmethod
    def _openai_output_text(response: Any) -> str:
        if isinstance(response, str):
            return response
        for item in (response or {}).get("output") or []:
            for content in item.get("content") or []:
                if content.get("type") == "output_text" and content.get("text"):
                    return str(content["text"])
        raise ValueError("OpenAI response contained no output text")

    def _openai_json(
        self, *, request: dict[str, Any], schema: dict[str, Any],
        system_instruction: str, max_output_tokens: int, timeout: float,
    ) -> str:
        key = (self._openai_key_loader() or "").strip()
        if not key:
            raise RuntimeError("OpenAI API key is not configured")
        payload = {
            "model": OPENAI_TEXT_MODEL,
            "instructions": system_instruction,
            "input": json.dumps(request, ensure_ascii=False),
            "reasoning": {"effort": "none"},
            "max_output_tokens": max_output_tokens,
            "text": {"format": {
                "type": "json_schema", "name": "lumina_learning",
                "strict": False, "schema": schema,
            }},
        }
        if self._openai_response_factory:
            return self._openai_output_text(self._openai_response_factory(payload, key))
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {key}"}, json=payload,
            )
            response.raise_for_status()
            return self._openai_output_text(response.json())

    def _generate_json(
        self,
        request: dict[str, Any],
        schema: dict[str, Any],
        *,
        chain: tuple[tuple[str, str], ...],
        system_instruction: str,
        temperature: float,
        max_output_tokens: int,
        timeout: float,
        validate: Callable[[Any], Any],
    ) -> Any:
        """The first usable, validated JSON result along a chain of text models."""
        failure: Exception | None = None
        ordered = list(chain)
        # Selecting OpenAI for the class means OpenAI leads both conversation
        # and the structured corrections/activities. Gemini remains available
        # as a fallback; selecting Gemini preserves the measured old ordering.
        if get_learning_voice_provider() == "openai":
            ordered.sort(key=lambda item: 0 if item[0] == "openai" else 1)
        for provider, model in ordered:
            name = f"{provider}:{model or self._ollama.model}"
            if self._spent_until.get(name, 0.0) > time.monotonic():
                continue
            try:
                if provider == "ollama":
                    if not self._ollama.configured:
                        continue
                    text = self._ollama.chat_json(
                        system=system_instruction, request=request, schema=schema,
                        temperature=temperature, timeout=timeout,
                    )
                elif provider == "gemini":
                    key = self._api_key_loader()
                    if not key:
                        continue
                    text = self._client_factory(key).models.generate_content(
                        model=model,
                        contents=json.dumps(request, ensure_ascii=False),
                        config=types.GenerateContentConfig(
                            system_instruction=system_instruction,
                            response_mime_type="application/json",
                            response_json_schema=schema,
                            temperature=temperature,
                            max_output_tokens=max_output_tokens,
                        ),
                    ).text
                else:
                    if not self.openai_available:
                        continue
                    text = self._openai_json(
                        request=request,
                        schema=schema,
                        system_instruction=system_instruction,
                        max_output_tokens=max_output_tokens,
                        timeout=timeout,
                    )
                return validate(json.loads(text) if isinstance(text, str) else text)
            except ValueError as exc:
                # Unusable JSON or content: the next model may do better.
                print(f"[Learning English] {name} gave unusable output ({exc}); trying the next model")
                failure = exc
            except Exception as exc:
                if _quota_spent(exc):
                    # A daily limit stays spent for hours; a per-minute one clears fast.
                    wait = 3600 if "PerDay" in str(exc) else 60
                    self._spent_until[name] = time.monotonic() + wait
                    print(f"[Learning English] {name} is out of quota; trying the next model")
                elif _busy(exc):
                    # Overloaded for this request only (measured: a 503 from
                    # 3.5 Flash-Lite that the next request did not repeat).
                    print(f"[Learning English] {name} is busy; trying the next model")
                elif isinstance(exc, OllamaCloudError):
                    # A helper with a rejected key must not stop the class.
                    self._spent_until[name] = time.monotonic() + 600
                    print(f"[Learning English] {name} failed ({exc}); trying the next model")
                else:
                    raise
                failure = exc
        if failure is not None:
            raise failure
        raise RuntimeError("RESOURCE_EXHAUSTED: no text model is available for Learning English")

    def analyze_turn(
        self,
        *,
        user_text: str,
        assistant_text: str,
        snapshot: dict[str, Any],
    ) -> TutorResponse:
        """Validate one structured UI result; always return a safe fallback."""
        fallback = TutorResponse(
            assistant_text=assistant_text,
            speech_text=assistant_text,
            exercise_type=str(snapshot.get("currentMode") or "conversation"),
            lesson_progress=self._current_progress(snapshot),
            next_action="Continúa con una pregunta o ejercicio breve.",
        )
        if not user_text:
            # Only the tutor spoke: there is nothing to correct, and skipping
            # it keeps the daily model quota for the student's own turns.
            return fallback

        grammar_findings = self._grammar.check(user_text)

        request = {
            "task": "Analyze the completed tutor turn. Do not invent a different tutor reply.",
            "studentText": user_text,
            "actualTutorText": assistant_text,
            "profile": snapshot.get("profile", {}),
            "currentMode": snapshot.get("currentMode", "conversation"),
            "currentProgress": self._current_progress(snapshot),
            "scenario": snapshot.get("activeScenario"),
            "curriculumUnit": snapshot.get("currentUnit"),
            "objectiveGrammarFindings": grammar_findings,
            "rules": [
                "Return corrections only after the student's turn is complete.",
                "If only an STT transcript is available, never claim exact phoneme analysis.",
                "Use pronunciation only for intelligibility or an approximate reading aid.",
                "Copy actualTutorText into assistantText and speechText.",
                "Profile updates must contain only facts clearly supplied by the student.",
                "Set profileUpdates.audience only when the student said the class is "
                "for a child, a teenager or an adult.",
                "lessonProgress must never be lower than currentProgress.",
                # Gemma 4 once returned "await_user_response" here; the studio
                # shows this field to the student as it is.
                "nextAction is one short sentence for the student, in their primary "
                "language, saying what to do next; never a code or identifier.",
            ],
        }
        try:
            if self._generator:
                raw = self._generator(request=request, schema=_RESPONSE_SCHEMA)
            else:
                raw = self._generate_json(
                    request,
                    _RESPONSE_SCHEMA,
                    chain=ANALYSIS_CHAIN,
                    system_instruction=(
                        "You are the structured evaluator for Lumina Learning. "
                        "Return only the requested JSON. The live tutor already spoke; "
                        "analyze that turn without creating a second answer."
                    ),
                    temperature=0.1,
                    max_output_tokens=1800,
                    timeout=25,
                    validate=_complete_analysis,
                )
            if isinstance(raw, str):
                raw = json.loads(raw)
            parsed = TutorResponse.from_mapping(raw, fallback_assistant_text=assistant_text)
            existing = {
                (item.original.casefold(), item.corrected.casefold())
                for item in parsed.corrections
            }
            for finding in grammar_findings:
                correction = Correction.from_mapping(finding)
                signature = (
                    correction.original.casefold(), correction.corrected.casefold()
                ) if correction else None
                if correction and signature not in existing:
                    parsed.corrections.append(correction)
                    existing.add(signature)
            # The user must see the exact answer the Live tutor actually said,
            # never a second answer rephrased by the analysis request.
            if assistant_text:
                parsed.assistant_text = assistant_text
                parsed.speech_text = assistant_text
            return parsed
        except Exception as exc:
            # The UI can keep the live transcript even when structured analysis
            # is unavailable. No credential or provider response is logged.
            print(f"[Learning English] Structured analysis fallback ({type(exc).__name__})")
            for finding in grammar_findings:
                correction = Correction.from_mapping(finding)
                if correction:
                    fallback.corrections.append(correction)
            return fallback

    def generate_activity(self, kind: str, snapshot: dict[str, Any]) -> dict[str, Any]:
        """A checkpoint or guided reading for the current unit, validated.

        Raises ValueError when no model wrote usable content. The returned
        activity still carries its answer key, which only Lumina's server keeps."""
        request = build_activity_request(kind, snapshot)
        schema = schema_for(kind)
        unit_id = str((snapshot.get("currentUnit") or {}).get("id") or "")
        if self._generator:
            raw = self._generator(request=request, schema=schema)
            return validate_activity(kind, json.loads(raw) if isinstance(raw, str) else raw, unit_id=unit_id)
        return self._generate_json(
            request,
            schema,
            chain=ACTIVITY_CHAIN,
            system_instruction=(
                "You write practice material for Lumina Learning, an English "
                "course for children, teenagers and adults. Return only the "
                "requested JSON."
            ),
            temperature=0.7,
            max_output_tokens=6000,
            timeout=75,
            validate=lambda raw: validate_activity(kind, raw, unit_id=unit_id),
        )

    def analyze_pronunciation(
        self, pcm: bytes, sample_rate: int, expected_text: str
    ) -> dict[str, Any] | None:
        """Score a spoken attempt with OpenPronounce; None when it is not running."""
        if not self.pronunciation_ready():
            return None
        path = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp:
                path = temp.name
            with wave.open(path, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(sample_rate)
                wav.writeframes(pcm)
            return self._pronunciation.analyze_file(path, expected_text)
        finally:
            if path:
                Path(path).unlink(missing_ok=True)

    @staticmethod
    def _current_progress(snapshot: dict[str, Any]) -> int:
        sessions = snapshot.get("sessions", [])
        session_id = snapshot.get("current_session_id")
        for session in reversed(sessions if isinstance(sessions, list) else []):
            if session.get("id") == session_id:
                try:
                    return max(0, min(100, int(session.get("progress", 0))))
                except (TypeError, ValueError):
                    return 0
        return 0
