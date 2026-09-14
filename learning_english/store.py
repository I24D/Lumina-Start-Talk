"""Educational persistence layered on Lumina's existing memory document."""

from __future__ import annotations

import copy
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from memory.memory_manager import load_memory, save_memory
from .types import TutorResponse


_LEVELS = {"A1", "A2", "B1", "B2", "C1", "C2"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_document() -> dict[str, Any]:
    return {
        "version": 1,
        "profile": {
            "primary_language": "",
            "level": "A1",
            "level_confirmed": False,
            "goal": "",
            "strengths": [],
            "difficulties": [],
            "preferred_speed": "normal",
            "current_objective": "Primeros pasos",
            "last_class_at": "",
        },
        "current_session_id": "",
        "sessions": [],
        "vocabulary": [],
        "frequent_errors": [],
        "skill_progress": {
            "conversation": 0,
            "pronunciation": 0,
            "grammar": 0,
            "vocabulary": 0,
            "listening": 0,
        },
        "weekly_activity": [],
        "last_lesson": None,
    }


def _normalise_document(raw: Any) -> dict[str, Any]:
    default = _default_document()
    if not isinstance(raw, dict):
        return default
    result = copy.deepcopy(default)
    for key in result:
        if key in raw and isinstance(raw[key], type(result[key])):
            result[key] = copy.deepcopy(raw[key])
    result["profile"] = {**default["profile"], **result.get("profile", {})}
    level = str(result["profile"].get("level", "A1")).upper()
    result["profile"]["level"] = level if level in _LEVELS else "A1"
    return result


class LearningProgressStore:
    """Keeps a temporary in-memory copy if disk or Supabase persistence fails."""

    def __init__(
        self,
        *,
        loader: Callable[[], dict] = load_memory,
        saver: Callable[[dict], None] = save_memory,
    ):
        self._loader = loader
        self._saver = saver
        self._lock = threading.RLock()
        self._cache: dict[str, Any] | None = None
        self.last_error = ""
        self.storage_status = "saved"

    def _load(self) -> dict[str, Any]:
        if self._cache is not None:
            return self._cache
        try:
            memory = self._loader()
            self._cache = _normalise_document(memory.get("learning_english"))
        except Exception as exc:
            self._cache = _default_document()
            self.last_error = f"No se pudo cargar el progreso ({type(exc).__name__})."
            self.storage_status = "temporary"
        return self._cache

    def _commit(self) -> None:
        document = self._load()
        try:
            memory = self._loader()
            memory["learning_english"] = copy.deepcopy(document)
            self._saver(memory)
            self.last_error = ""
            self.storage_status = "saved"
        except Exception as exc:
            self.last_error = f"El progreso sigue en memoria temporal ({type(exc).__name__})."
            self.storage_status = "temporary"

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            result = copy.deepcopy(self._load())
            result["storageStatus"] = self.storage_status
            result["storageError"] = self.last_error
            return result

    def begin_session(self, trigger: str) -> str:
        with self._lock:
            doc = self._load()
            existing = str(doc.get("current_session_id") or "")
            if existing:
                for session in reversed(doc["sessions"]):
                    if session.get("id") == existing and session.get("status") == "active":
                        return existing
            session_id = uuid.uuid4().hex
            doc["current_session_id"] = session_id
            doc["sessions"].append({
                "id": session_id,
                "started_at": _now(),
                "ended_at": "",
                "status": "active",
                "trigger": str(trigger or "ui"),
                "mode": "conversation",
                "turns": [],
                "correction_count": 0,
                "vocabulary_count": 0,
                "progress": 0,
                "summary": "",
            })
            doc["sessions"] = doc["sessions"][-12:]
            self._commit()
            return session_id

    def update_profile(self, updates: dict[str, Any]) -> None:
        if not isinstance(updates, dict):
            return
        allowed = {
            "primary_language", "level", "goal", "strengths", "difficulties",
            "preferred_speed", "current_objective",
        }
        with self._lock:
            profile = self._load()["profile"]
            for key in allowed:
                value = updates.get(key)
                if value in (None, "", []):
                    continue
                if key == "level":
                    value = str(value).upper()
                    if value not in _LEVELS:
                        continue
                    profile["level_confirmed"] = True
                profile[key] = value
            self._commit()

    def set_mode(self, mode: str) -> None:
        with self._lock:
            session = self._current_session()
            if session:
                session["mode"] = str(mode or "conversation")
                self._commit()

    def _current_session(self) -> dict[str, Any] | None:
        doc = self._load()
        session_id = doc.get("current_session_id")
        for session in reversed(doc["sessions"]):
            if session.get("id") == session_id:
                return session
        return None

    def record_turn(self, user_text: str, response: TutorResponse) -> None:
        with self._lock:
            doc = self._load()
            session = self._current_session()
            if not session:
                self.begin_session("recovered")
                session = self._current_session()
            assert session is not None
            session["turns"].append({
                "at": _now(),
                "user": str(user_text or "")[:1200],
                "assistant": response.assistant_text[:1800],
                "exercise_type": response.exercise_type,
                "corrections": [{
                    "original": c.original[:300],
                    "corrected": c.corrected[:300],
                    "explanation": c.explanation[:500],
                    "category": c.category,
                    "pronunciation": c.pronunciation[:200],
                    "translation": c.translation[:300],
                } for c in response.corrections[:4]],
            })
            session["turns"] = session["turns"][-30:]
            session["correction_count"] += len(response.corrections)
            session["vocabulary_count"] += len(response.new_vocabulary)
            session["progress"] = max(
                int(session.get("progress", 0)), response.lesson_progress
            )
            session["mode"] = response.exercise_type

            if response.profile_updates:
                profile = doc["profile"]
                for key, value in response.profile_updates.items():
                    if key == "level":
                        value = str(value).upper()
                        if value not in _LEVELS:
                            continue
                        profile["level_confirmed"] = True
                    profile[key] = value

            for item in response.new_vocabulary:
                self._upsert_vocabulary(doc, item.word, item.meaning, item.example)

            for correction in response.corrections:
                signature = correction.corrected.casefold()
                found = next((e for e in doc["frequent_errors"] if e.get("signature") == signature), None)
                if found:
                    found["count"] = int(found.get("count", 0)) + 1
                    found["last_seen"] = _now()
                else:
                    doc["frequent_errors"].append({
                        "signature": signature,
                        "original": correction.original,
                        "corrected": correction.corrected,
                        "category": correction.category,
                        "count": 1,
                        "last_seen": _now(),
                    })
            doc["frequent_errors"] = sorted(
                doc["frequent_errors"], key=lambda item: int(item.get("count", 0)), reverse=True
            )[:30]

            skill = response.exercise_type
            if skill in doc["skill_progress"]:
                old = int(doc["skill_progress"].get(skill, 0))
                doc["skill_progress"][skill] = max(old, response.lesson_progress)
            self._commit()

    @staticmethod
    def _upsert_vocabulary(doc: dict[str, Any], word: str, meaning: str, example: str) -> None:
        key = word.strip().casefold()
        if not key:
            return
        found = next((item for item in doc["vocabulary"] if item.get("word", "").casefold() == key), None)
        if found:
            found.update({
                "meaning": str(meaning)[:250],
                "example": str(example)[:350],
                "last_seen": _now(),
            })
            found["count"] = int(found.get("count", 0)) + 1
        else:
            doc["vocabulary"].append({
                "word": word.strip()[:100],
                "meaning": str(meaning)[:250],
                "example": str(example)[:350],
                "saved": False, "count": 1, "first_seen": _now(), "last_seen": _now(),
            })
        doc["vocabulary"] = doc["vocabulary"][-80:]

    def save_word(self, word: str, meaning: str = "", example: str = "") -> None:
        with self._lock:
            doc = self._load()
            self._upsert_vocabulary(doc, word, meaning, example)
            for item in doc["vocabulary"]:
                if item.get("word", "").casefold() == word.strip().casefold():
                    item["saved"] = True
            self._commit()

    def complete_session(self, summary: str) -> dict[str, Any]:
        with self._lock:
            doc = self._load()
            session = self._current_session()
            if session:
                session["status"] = "completed"
                session["ended_at"] = _now()
                session["summary"] = str(summary or "")[:1200]
                # Finished lessons retain representative recent turns; totals,
                # corrections, vocabulary and progress remain as structured
                # fields without letting the shared memory document grow forever.
                session["turns"] = session.get("turns", [])[-6:]
                doc["last_lesson"] = {
                    "at": session["ended_at"],
                    "mode": session.get("mode", "conversation"),
                    "summary": session["summary"],
                    "progress": session.get("progress", 0),
                }
                today = session["ended_at"][:10]
                activity = next((x for x in doc["weekly_activity"] if x.get("date") == today), None)
                if activity:
                    activity["lessons"] = int(activity.get("lessons", 0)) + 1
                else:
                    doc["weekly_activity"].append({"date": today, "lessons": 1})
                doc["weekly_activity"] = doc["weekly_activity"][-7:]
            doc["profile"]["last_class_at"] = _now()
            doc["current_session_id"] = ""
            self._commit()
            return copy.deepcopy(session or {})
