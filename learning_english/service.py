"""Gemini services for the English tutor: the browser's live session token and
the structured analysis of each finished turn."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from google import genai
from google.genai import types

from memory.config_manager import get_gemini_key
from .providers import LanguageToolProvider, OpenPronounceProvider
from .types import Correction, TutorResponse


PROMPT_PATH = Path(__file__).parent / "prompts" / "english_tutor.txt"
# The tutor talks through its own Live session in the browser. 3.1 Flash Live is
# the newest live voice model this key can open: measured on 2026-09-13 through
# an ephemeral token, first audio came 0.80 s after the speech ended, against
# 1.43 s for 2.5 native audio. 3.6 Flash has no live voice, so it analyses turns.
LIVE_MODEL = "models/gemini-3.1-flash-live-preview"
MODEL = "gemini-3.6-flash"

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "assistantText": {"type": "string"},
        "speechText": {"type": "string"},
        "uiLanguage": {"type": "string"},
        "exerciseType": {
            "type": "string",
            "enum": ["conversation", "pronunciation", "grammar", "vocabulary", "listening", "quick-lesson", "assessment"],
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
                "level": {"type": "string", "enum": ["A1", "A2", "B1", "B2", "C1", "C2"]},
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


class LearningEnglishService:
    def __init__(
        self,
        *,
        api_key_loader: Callable[[], str | None] = get_gemini_key,
        generator: Callable[..., Any] | None = None,
        token_factory: Callable[[dict[str, Any]], str] | None = None,
        grammar_provider: LanguageToolProvider | None = None,
        pronunciation_provider: OpenPronounceProvider | None = None,
    ):
        self._api_key_loader = api_key_loader
        self._generator = generator
        self._token_factory = token_factory
        self._grammar = grammar_provider or LanguageToolProvider()
        self._pronunciation = pronunciation_provider or OpenPronounceProvider()
        self._system_prompt = PROMPT_PATH.read_text(encoding="utf-8")

    def provider_status(self) -> list[dict[str, Any]]:
        return [self._grammar.status(), self._pronunciation.status()]

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
            "token": token,
            "model": LIVE_MODEL,
            "opening": "" if resume_handle else self.opening_instruction(snapshot),
        }

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
        if not user_text and not assistant_text:
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
                "lessonProgress must never be lower than currentProgress.",
            ],
        }
        try:
            if self._generator:
                raw = self._generator(request=request, schema=_RESPONSE_SCHEMA)
            else:
                key = self._api_key_loader()
                if not key:
                    return fallback
                client = genai.Client(api_key=key)
                response = client.models.generate_content(
                    model=MODEL,
                    contents=json.dumps(request, ensure_ascii=False),
                    config=types.GenerateContentConfig(
                        system_instruction=(
                            "You are the structured evaluator for Lumina Learning. "
                            "Return only the requested JSON. The live tutor already spoke; "
                            "analyze that turn without creating a second answer."
                        ),
                        response_mime_type="application/json",
                        response_json_schema=_RESPONSE_SCHEMA,
                        temperature=0.1,
                        max_output_tokens=1800,
                    ),
                )
                raw = response.text
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

    def analyze_pronunciation_file(
        self, audio_path: str, expected_text: str
    ) -> dict[str, Any] | None:
        """Run real phoneme analysis only when the optional local engine exists."""
        return self._pronunciation.analyze_file(audio_path, expected_text)

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
