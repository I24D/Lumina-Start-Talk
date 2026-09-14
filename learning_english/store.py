"""Educational persistence layered on Lumina's existing memory document."""

from __future__ import annotations

import copy
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from memory.memory_manager import load_memory, save_memory
from .activities import PASS_SCORE
from .catalog import (
    AUDIENCES, build_catalog, find_listening_activity, find_scenario, find_unit,
    next_unit_id,
)
from .review import ReviewScheduler
from .types import TutorResponse


_LEVELS = {"PRE-A1", "A1", "A2", "B1", "B2", "C1", "C2"}
_REVIEW_SCHEDULER = ReviewScheduler()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_document() -> dict[str, Any]:
    return {
        "version": 2,
        "profile": {
            "primary_language": "",
            # kids, teens or adults; empty until the student says.
            "audience": "",
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
        "active_scenario_id": "",
        "sessions": [],
        "vocabulary": [],
        "frequent_errors": [],
        "skill_progress": {
            "conversation": 0,
            "pronunciation": 0,
            "grammar": 0,
            "vocabulary": 0,
            "listening": 0,
            "reading": 0,
            "writing": 0,
        },
        "weekly_activity": [],
        "curriculum": {
            "current_unit_id": "a1-foundations",
            "listening_activity_id": "multiple-choice",
            "completed_units": [],
            # Lessons finished per unit; a unit completes at its lesson count.
            "unit_lessons": {},
            "completed_scenarios": [],
            # Best checkpoint score per unit; passing one completes the unit.
            "checkpoints": {},
            "weekly_target": 3,
        },
        "metrics": {
            "xp": 0,
            "streak_days": 0,
            "total_turns": 0,
            "total_reviews": 0,
            "successful_reviews": 0,
            "last_activity_date": "",
        },
        "placement": None,
        "activity_log": [],
        "last_lesson": None,
    }


def _normalise_document(raw: Any) -> dict[str, Any]:
    default = _default_document()
    if not isinstance(raw, dict):
        return default
    result = copy.deepcopy(default)
    for key in result:
        if key not in raw:
            continue
        value = raw[key]
        # None marks an optional record (last lesson, placement) that is saved
        # as a dict; comparing types alone dropped it on every reload.
        if isinstance(value, type(result[key])) or (result[key] is None and isinstance(value, dict)):
            result[key] = copy.deepcopy(value)
    result["profile"] = {**default["profile"], **result.get("profile", {})}
    result["skill_progress"] = {**default["skill_progress"], **result["skill_progress"]}
    result["curriculum"] = {
        **default["curriculum"], **result.get("curriculum", {})
    }
    curriculum = result["curriculum"]
    for key in ("unit_lessons", "checkpoints"):
        if not isinstance(curriculum.get(key), dict):
            curriculum[key] = {}
    for key in ("completed_units", "completed_scenarios"):
        if not isinstance(curriculum.get(key), list):
            curriculum[key] = []
    result["metrics"] = {**default["metrics"], **result.get("metrics", {})}
    level = str(result["profile"].get("level", "A1")).upper()
    result["profile"]["level"] = level if level in _LEVELS else "A1"
    if result["profile"].get("audience") not in AUDIENCES:
        result["profile"]["audience"] = ""
    result["version"] = 2
    for item in result.get("vocabulary", []):
        if isinstance(item, dict) and not isinstance(item.get("srs"), dict):
            item["srs"] = _REVIEW_SCHEDULER.new_card()
            item["due_at"] = item["srs"].get("due", _now())
            item["review_count"] = int(item.get("review_count", 0))
            item["last_rating"] = str(item.get("last_rating", ""))
    return result


def _profile_value(key: str, value: Any) -> Any:
    """A validated profile value, or None when it must be ignored."""
    if key == "level":
        value = str(value).upper()
        return value if value in _LEVELS else None
    if key == "audience":
        value = str(value).lower()
        return value if value in AUDIENCES else None
    return value


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
                "scenario_id": doc.get("active_scenario_id", ""),
                "unit_id": doc.get("curriculum", {}).get("current_unit_id", ""),
                "turns": [],
                "correction_count": 0,
                "vocabulary_count": 0,
                "progress": 0,
                "summary": "",
            })
            doc["sessions"] = doc["sessions"][-30:]
            self._commit()
            return session_id

    def update_profile(self, updates: dict[str, Any]) -> None:
        if not isinstance(updates, dict):
            return
        allowed = {
            "primary_language", "audience", "level", "goal", "strengths",
            "difficulties", "preferred_speed", "current_objective",
        }
        with self._lock:
            doc = self._load()
            profile = doc["profile"]
            level_before = profile.get("level")
            for key in allowed:
                value = updates.get(key)
                if value in (None, "", []):
                    continue
                value = _profile_value(key, value)
                if value is None:
                    continue
                if key == "level":
                    profile["level_confirmed"] = True
                profile[key] = value
            if profile.get("level") != level_before:
                self._advance_unit(doc)
            self._commit()

    def set_mode(self, mode: str) -> None:
        with self._lock:
            doc = self._load()
            # A new mode ends any role-play; the session keeps its scenario_id.
            doc["active_scenario_id"] = ""
            session = self._current_session()
            if session:
                session["mode"] = str(mode or "conversation")
            self._commit()

    def set_scenario(self, scenario_id: str) -> dict[str, Any]:
        scenario = find_scenario(str(scenario_id or ""))
        if not scenario:
            raise ValueError("Unknown learning scenario")
        with self._lock:
            doc = self._load()
            catalog = build_catalog(doc["profile"], doc["curriculum"])
            available = next(
                (item.get("available") for item in catalog["scenarios"] if item["id"] == scenario["id"]),
                False,
            )
            if not available:
                raise ValueError("This scenario is locked for the current CEFR level")
            doc["active_scenario_id"] = scenario["id"]
            session = self._current_session()
            if session:
                session["scenario_id"] = scenario["id"]
                session["mode"] = scenario["mode"]
            doc["profile"]["current_objective"] = scenario["objective"]
            self._commit()
        return scenario

    def clear_scenario(self) -> None:
        with self._lock:
            doc = self._load()
            if doc.get("active_scenario_id"):
                doc["active_scenario_id"] = ""
                self._commit()

    def set_unit(self, unit_id: str) -> dict[str, Any]:
        unit = find_unit(str(unit_id or ""))
        if not unit:
            raise ValueError("Unknown curriculum unit")
        with self._lock:
            doc = self._load()
            catalog = build_catalog(doc["profile"], doc["curriculum"])
            available = any(
                candidate.get("available")
                for level in catalog["levels"]
                for candidate in level["units"]
                if candidate["id"] == unit["id"]
            )
            if not available:
                raise ValueError("This unit is locked for the current CEFR level")
            doc["curriculum"]["current_unit_id"] = unit["id"]
            doc["profile"]["current_objective"] = unit["objective"]
            # A unit lesson replaces any role-play.
            doc["active_scenario_id"] = ""
            session = self._current_session()
            if session:
                session["unit_id"] = unit["id"]
                session["mode"] = unit["skill"]
            self._commit()
        return unit

    def set_listening_activity(self, activity_id: str) -> dict[str, Any]:
        activity = find_listening_activity(str(activity_id or ""))
        if not activity:
            raise ValueError("Unknown listening activity")
        with self._lock:
            doc = self._load()
            doc["curriculum"]["listening_activity_id"] = activity["id"]
            doc["active_scenario_id"] = ""
            session = self._current_session()
            if session:
                session["mode"] = "listening"
            self._commit()
        return activity

    def set_weekly_target(self, target: Any) -> None:
        try:
            value = int(target)
        except (TypeError, ValueError):
            raise ValueError("Weekly target must be a number of classes") from None
        if not 1 <= value <= 7:
            raise ValueError("Weekly target must be between 1 and 7 classes")
        with self._lock:
            self._load()["curriculum"]["weekly_target"] = value
            self._commit()

    def apply_placement(self, level: str, *, correct: int, answered: int) -> None:
        """The written placement test's level, and the first unit of that level."""
        level = str(level or "").upper()
        if level not in _LEVELS:
            raise ValueError("Unknown CEFR level")
        with self._lock:
            doc = self._load()
            doc["profile"]["level"] = level
            doc["profile"]["level_confirmed"] = True
            doc["placement"] = {
                "level": level, "correct": int(correct), "answered": int(answered), "at": _now(),
            }
            self._advance_unit(doc)
            self._touch_activity(doc)
            self._commit()

    def record_activity(self, result: dict[str, Any]) -> None:
        """Keep a graded checkpoint or reading; a passed checkpoint completes its unit."""
        with self._lock:
            doc = self._load()
            kind = str(result.get("kind") or "")
            unit_id = str(result.get("unitId") or "")
            score = max(0, min(100, int(result.get("score", 0))))
            doc["activity_log"].append({
                "kind": kind,
                "title": str(result.get("title") or "")[:120],
                "unit_id": unit_id,
                "score": score,
                "at": _now(),
            })
            doc["activity_log"] = doc["activity_log"][-20:]
            doc["metrics"]["xp"] = int(doc["metrics"].get("xp", 0)) + 2 + score // 10
            self._touch_activity(doc)
            if kind == "reading":
                skills = doc["skill_progress"]
                skills["reading"] = max(int(skills.get("reading", 0)), score)
            unit = find_unit(unit_id)
            if kind == "checkpoint" and unit:
                curriculum = doc["curriculum"]
                previous = curriculum["checkpoints"].get(unit_id)
                previous = previous if isinstance(previous, dict) else {}
                curriculum["checkpoints"][unit_id] = {
                    "best": max(score, int(previous.get("best", 0))),
                    "passed": bool(previous.get("passed")) or score >= PASS_SCORE,
                    "at": _now(),
                }
                if score >= PASS_SCORE and unit_id not in curriculum["completed_units"]:
                    curriculum["completed_units"].append(unit_id)
                    self._advance_unit(doc)
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
                    "source": c.source[:60],
                } for c in response.corrections[:4]],
            })
            session["turns"] = session["turns"][-30:]
            session["correction_count"] += len(response.corrections)
            session["vocabulary_count"] += len(response.new_vocabulary)
            session["progress"] = max(
                int(session.get("progress", 0)), response.lesson_progress
            )
            session["mode"] = response.exercise_type
            # Only the student's own turns count as practice.
            if str(user_text or "").strip():
                self._touch_activity(doc)
                doc["metrics"]["total_turns"] = int(doc["metrics"].get("total_turns", 0)) + 1
                doc["metrics"]["xp"] = int(doc["metrics"].get("xp", 0)) + 5

            if response.profile_updates:
                profile = doc["profile"]
                level_before = profile.get("level")
                for key, value in response.profile_updates.items():
                    value = _profile_value(key, value)
                    if value is None:
                        continue
                    if key == "level":
                        profile["level_confirmed"] = True
                    profile[key] = value
                if profile.get("level") != level_before:
                    self._advance_unit(doc)

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
            # A placement test reaching 100% sets the level; it is not a lesson.
            if response.lesson_progress >= 100 and response.exercise_type != "assessment":
                self._finish_lesson(doc, session)
            self._commit()

    @classmethod
    def _finish_lesson(cls, doc: dict[str, Any], session: dict[str, Any]) -> None:
        """Credit a lesson that reached 100% once per class: to the role-play
        when a scenario is running, otherwise to the unit this class works on.

        The class's own unit_id is used, not the curriculum's current unit, so
        a unit that just completed and moved the route on cannot credit the next
        unit from the same finished lesson."""
        curriculum = doc["curriculum"]
        counted = session.setdefault("counted_lessons", [])
        scenario_id = str(doc.get("active_scenario_id") or "")
        if scenario_id:
            if f"scenario:{scenario_id}" not in counted:
                counted.append(f"scenario:{scenario_id}")
                if scenario_id not in curriculum["completed_scenarios"]:
                    curriculum["completed_scenarios"].append(scenario_id)
            return
        unit_id = str(session.get("unit_id") or "")
        unit = find_unit(unit_id)
        if not unit or f"unit:{unit_id}" in counted:
            return
        counted.append(f"unit:{unit_id}")
        lessons = curriculum["unit_lessons"]
        lessons[unit_id] = min(unit["lessons"], int(lessons.get(unit_id, 0)) + 1)
        completed = curriculum["completed_units"]
        if lessons[unit_id] >= unit["lessons"] and unit_id not in completed:
            completed.append(unit_id)
            cls._advance_unit(doc)

    @staticmethod
    def _advance_unit(doc: dict[str, Any]) -> None:
        """Keep the current unit on the student's level: move on from a finished
        unit, or to the level's first unfinished unit after a level change."""
        curriculum = doc["curriculum"]
        completed = curriculum["completed_units"]
        level = doc["profile"].get("level", "A1")
        current = find_unit(str(curriculum.get("current_unit_id") or ""))
        if current and current["level"] == level and current["id"] not in completed:
            return
        next_id = next_unit_id(level, completed)
        if next_id:
            curriculum["current_unit_id"] = next_id
            doc["profile"]["current_objective"] = find_unit(next_id)["objective"]

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
            if not isinstance(found.get("srs"), dict):
                found["srs"] = _REVIEW_SCHEDULER.new_card()
                found["due_at"] = found["srs"].get("due", _now())
        else:
            card = _REVIEW_SCHEDULER.new_card()
            doc["vocabulary"].append({
                "word": word.strip()[:100],
                "meaning": str(meaning)[:250],
                "example": str(example)[:350],
                "saved": False, "count": 1, "first_seen": _now(), "last_seen": _now(),
                "srs": card, "due_at": card.get("due", _now()),
                "review_count": 0, "last_rating": "",
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

    def due_reviews(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            words = [
                copy.deepcopy(item)
                for item in self._load().get("vocabulary", [])
                if isinstance(item, dict) and _REVIEW_SCHEDULER.is_due(item.get("srs"))
            ]
        words.sort(key=lambda item: str(item.get("due_at") or ""))
        return words[: max(1, min(50, int(limit)))]

    def review_word(self, word: str, rating: str) -> dict[str, Any]:
        key = str(word or "").strip().casefold()
        if not key:
            raise ValueError("A vocabulary word is required")
        with self._lock:
            doc = self._load()
            item = next(
                (entry for entry in doc["vocabulary"] if str(entry.get("word", "")).casefold() == key),
                None,
            )
            if item is None:
                raise ValueError("Vocabulary word not found")
            result = _REVIEW_SCHEDULER.review(item.get("srs"), rating)
            item["srs"] = result["card"]
            item["due_at"] = result["dueAt"]
            item["review_count"] = int(item.get("review_count", 0)) + 1
            item["last_rating"] = str(rating).lower()
            metrics = doc["metrics"]
            metrics["total_reviews"] = int(metrics.get("total_reviews", 0)) + 1
            if str(rating).lower() in {"good", "easy"}:
                metrics["successful_reviews"] = int(metrics.get("successful_reviews", 0)) + 1
                metrics["xp"] = int(metrics.get("xp", 0)) + 3
            self._touch_activity(doc)
            self._commit()
            return {
                "word": item["word"],
                "rating": item["last_rating"],
                "dueAt": item["due_at"],
                "engine": result["engine"],
            }

    @staticmethod
    def _touch_activity(doc: dict[str, Any]) -> None:
        today = datetime.now(timezone.utc).date()
        metrics = doc["metrics"]
        previous_raw = str(metrics.get("last_activity_date") or "")
        try:
            previous = datetime.fromisoformat(previous_raw).date()
        except ValueError:
            previous = None
        if previous == today:
            return
        metrics["streak_days"] = (
            int(metrics.get("streak_days", 0)) + 1
            if previous == today - timedelta(days=1)
            else 1
        )
        metrics["last_activity_date"] = today.isoformat()

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
                doc["metrics"]["xp"] = int(doc["metrics"].get("xp", 0)) + 20
                self._touch_activity(doc)
            # A role-play belongs to its class: the next class opens as the teacher.
            doc["active_scenario_id"] = ""
            doc["profile"]["last_class_at"] = _now()
            doc["current_session_id"] = ""
            self._commit()
            return copy.deepcopy(session or {})
