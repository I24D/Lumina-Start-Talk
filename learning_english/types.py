"""Validated domain types used by the Learning English backend and web UI."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class LearningEnglishState(StrEnum):
    INACTIVE = "inactive"
    ENTERING = "entering"
    ONBOARDING = "onboarding"
    ASSESSMENT = "assessment"
    LESSON = "lesson"
    PAUSED = "paused"
    COMPLETING = "completing"
    EXITING = "exiting"


EXERCISE_TYPES = {
    "conversation",
    "pronunciation",
    "grammar",
    "vocabulary",
    "listening",
    "quick-lesson",
    "assessment",
}
CORRECTION_CATEGORIES = {"grammar", "vocabulary", "pronunciation"}


def _clean(value: Any, *, limit: int = 1000) -> str:
    return str(value or "").strip()[:limit]


def _number(value: Any, default: int = 0, minimum: int = 0, maximum: int = 100) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


@dataclass(slots=True)
class Correction:
    original: str
    corrected: str
    explanation: str
    category: str = "grammar"
    pronunciation: str = ""
    translation: str = ""

    @classmethod
    def from_mapping(cls, raw: Any) -> "Correction | None":
        if not isinstance(raw, dict):
            return None
        original = _clean(raw.get("original"), limit=500)
        corrected = _clean(raw.get("corrected"), limit=500)
        if not original or not corrected:
            return None
        category = _clean(raw.get("category"), limit=30).lower()
        if category not in CORRECTION_CATEGORIES:
            category = "grammar"
        return cls(
            original=original,
            corrected=corrected,
            explanation=_clean(raw.get("explanation"), limit=700),
            category=category,
            pronunciation=_clean(raw.get("pronunciation"), limit=300),
            translation=_clean(raw.get("translation"), limit=500),
        )


@dataclass(slots=True)
class VocabularyItem:
    word: str
    meaning: str
    example: str

    @classmethod
    def from_mapping(cls, raw: Any) -> "VocabularyItem | None":
        if not isinstance(raw, dict):
            return None
        word = _clean(raw.get("word"), limit=100)
        if not word:
            return None
        return cls(
            word=word,
            meaning=_clean(raw.get("meaning"), limit=400),
            example=_clean(raw.get("example"), limit=500),
        )


@dataclass(slots=True)
class TutorResponse:
    assistant_text: str
    speech_text: str
    ui_language: str = "es"
    exercise_type: str = "conversation"
    corrections: list[Correction] = field(default_factory=list)
    new_vocabulary: list[VocabularyItem] = field(default_factory=list)
    next_action: str = ""
    lesson_progress: int = 0
    should_wait_for_user: bool = True
    profile_updates: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_mapping(
        cls,
        raw: Any,
        *,
        fallback_assistant_text: str = "",
    ) -> "TutorResponse":
        if not isinstance(raw, dict):
            raw = {}
        assistant_text = _clean(
            raw.get("assistantText") or raw.get("assistant_text") or fallback_assistant_text,
            limit=6000,
        )
        speech_text = _clean(
            raw.get("speechText") or raw.get("speech_text") or assistant_text,
            limit=6000,
        )
        exercise_type = _clean(
            raw.get("exerciseType") or raw.get("exercise_type"), limit=40
        ).lower()
        if exercise_type not in EXERCISE_TYPES:
            exercise_type = "conversation"

        corrections: list[Correction] = []
        for item in raw.get("corrections", []) if isinstance(raw.get("corrections"), list) else []:
            parsed = Correction.from_mapping(item)
            if parsed:
                corrections.append(parsed)

        vocabulary: list[VocabularyItem] = []
        vocab_raw = raw.get("newVocabulary", raw.get("new_vocabulary", []))
        for item in vocab_raw if isinstance(vocab_raw, list) else []:
            parsed = VocabularyItem.from_mapping(item)
            if parsed:
                vocabulary.append(parsed)

        profile_updates = {}
        updates = raw.get("profileUpdates", raw.get("profile_updates", {}))
        if isinstance(updates, dict):
            allowed = {
                "primary_language",
                "level",
                "goal",
                "strengths",
                "difficulties",
                "preferred_speed",
                "current_objective",
            }
            for key in allowed:
                value = _clean(updates.get(key), limit=500)
                if value:
                    profile_updates[key] = value

        return cls(
            assistant_text=assistant_text,
            speech_text=speech_text,
            ui_language=_clean(raw.get("uiLanguage", raw.get("ui_language", "es")), limit=12) or "es",
            exercise_type=exercise_type,
            corrections=corrections,
            new_vocabulary=vocabulary,
            next_action=_clean(raw.get("nextAction", raw.get("next_action", "")), limit=800),
            lesson_progress=_number(raw.get("lessonProgress", raw.get("lesson_progress", 0))),
            should_wait_for_user=bool(raw.get("shouldWaitForUser", raw.get("should_wait_for_user", True))),
            profile_updates=profile_updates,
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return {
            "assistantText": data["assistant_text"],
            "speechText": data["speech_text"],
            "uiLanguage": data["ui_language"],
            "exerciseType": data["exercise_type"],
            "corrections": data["corrections"],
            "newVocabulary": data["new_vocabulary"],
            "nextAction": data["next_action"],
            "lessonProgress": data["lesson_progress"],
            "shouldWaitForUser": data["should_wait_for_user"],
            "profileUpdates": data["profile_updates"],
        }
