"""Small state machine joining the tutor, persistence and browser API."""

from __future__ import annotations

from typing import Any, Callable

from .store import LearningProgressStore
from .types import LearningEnglishState, TutorResponse


class LearningEnglishController:
    def __init__(
        self,
        store: LearningProgressStore | None = None,
        *,
        event_sink: Callable[[str, dict[str, Any]], None] | None = None,
    ):
        self.store = store or LearningProgressStore()
        self.state = LearningEnglishState.INACTIVE
        self.active = False
        self.paused = False
        self.input_muted = False
        self.translation_enabled = True
        self.current_mode = "conversation"
        self.last_response: TutorResponse | None = None
        self.last_summary = ""
        self._event_sink = event_sink

    def _emit(self, name: str, payload: dict[str, Any] | None = None) -> None:
        if self._event_sink:
            self._event_sink(name, payload or {})

    def enter(self, trigger: str = "ui") -> dict[str, Any]:
        if self.active:
            return self.snapshot()
        self.state = LearningEnglishState.ENTERING
        self.active = True
        # The summary belongs to the class that ended; a new class starts clean.
        self.last_summary = ""
        self.paused = False
        self.input_muted = False
        self.store.begin_session(trigger)
        self._emit("learningEnglish.modeEntered", {"trigger": trigger})
        return self.snapshot()

    def session_ready(self) -> dict[str, Any]:
        profile = self.store.snapshot()["profile"]
        complete = bool(
            profile.get("primary_language")
            and profile.get("goal")
            and profile.get("level_confirmed")
        )
        self.state = LearningEnglishState.LESSON if complete else LearningEnglishState.ONBOARDING
        self._emit("learningEnglish.sessionStarted", {"onboarding": not complete})
        return self.snapshot()

    def set_mode(self, mode: str) -> dict[str, Any]:
        allowed = {
            "conversation", "pronunciation", "grammar", "vocabulary",
            "listening", "quick-lesson", "assessment",
        }
        if mode not in allowed:
            raise ValueError("Unsupported learning mode")
        self.current_mode = mode
        self.store.set_mode(mode)
        self.state = (
            LearningEnglishState.ASSESSMENT
            if mode == "assessment"
            else LearningEnglishState.LESSON
        )
        return self.snapshot()

    def set_paused(self, paused: bool) -> dict[str, Any]:
        if not self.active:
            return self.snapshot()
        self.paused = bool(paused)
        self.state = LearningEnglishState.PAUSED if paused else LearningEnglishState.LESSON
        return self.snapshot()

    def apply_turn(self, user_text: str, response: TutorResponse) -> dict[str, Any]:
        if not self.active:
            raise RuntimeError("Learning English is not active")
        self.last_response = response
        self.current_mode = response.exercise_type
        self.store.record_turn(user_text, response)
        self._emit("learningEnglish.userSpoke", {"text": user_text})
        for correction in response.corrections:
            self._emit("learningEnglish.correctionCreated", {"correction": correction.corrected})
        self._emit("learningEnglish.progressUpdated", {"progress": response.lesson_progress})
        return self.snapshot()

    def summary_text(self) -> str:
        data = self.store.snapshot()
        session_id = data.get("current_session_id")
        session = next(
            (item for item in reversed(data.get("sessions", [])) if item.get("id") == session_id),
            {},
        )
        turns = len(session.get("turns", []))
        corrections = int(session.get("correction_count", 0))
        vocabulary = int(session.get("vocabulary_count", 0))
        mode = str(session.get("mode", self.current_mode)).replace("-", " ")
        return (
            f"Clase completada: {turns} intervenciones en modo {mode}, "
            f"{corrections} correcciones y {vocabulary} palabras nuevas. "
            "La próxima vez continuaremos desde este punto."
        )

    def complete(self) -> dict[str, Any]:
        if not self.active:
            return self.snapshot()
        self.state = LearningEnglishState.COMPLETING
        self.last_summary = self.summary_text()
        completed = self.store.complete_session(self.last_summary)
        self._emit("learningEnglish.sessionCompleted", {"summary": self.last_summary})
        self.state = LearningEnglishState.EXITING
        self.active = False
        self.paused = False
        self.input_muted = False
        self._emit("learningEnglish.modeExited", {"session": completed.get("id", "")})
        self.state = LearningEnglishState.INACTIVE
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        progress = self.store.snapshot()
        return {
            "active": self.active,
            "state": self.state.value,
            "paused": self.paused,
            "inputMuted": self.input_muted,
            "translationEnabled": self.translation_enabled,
            "currentMode": self.current_mode,
            "lastSummary": self.last_summary,
            "lastResponse": self.last_response.to_dict() if self.last_response else None,
            **progress,
        }
