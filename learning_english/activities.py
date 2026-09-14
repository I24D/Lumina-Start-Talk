"""Generated practice: unit checkpoints and guided readings.

Gemini writes the content inside the course's limits; this module validates it
and grades the answers, so a score never comes from the model's opinion of how
the student did. Guided reading follows the assisted-reading idea of LinguaCafe
(a glossary the student can save for review); the code and prompts are Lumina's.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from .catalog import audience_of


KINDS = ("checkpoint", "reading")
PASS_SCORE = 80

# English words in a guided reading, by CEFR level.
READING_LENGTH = {
    "PRE-A1": (25, 50), "A1": (50, 90), "A2": (90, 130), "B1": (130, 180),
    "B2": (180, 240), "C1": (240, 320), "C2": (260, 350),
}

AUDIENCE_RULES = {
    "kids": (
        "The student is a child of about 6 to 12. Use short sentences, concrete "
        "everyday topics (family, school, animals, colours, food, toys, games), a "
        "warm playful tone and nothing scary, violent or sensitive. Never ask for "
        "personal data."
    ),
    "teens": (
        "The student is a teenager of about 13 to 17. Use relatable topics "
        "(school, friends, games, music, sport, series), a respectful tone that is "
        "never childish, and nothing sensitive. Never ask for personal data."
    ),
    "adults": (
        "The student is an adult. Use practical topics tied to their goal, such as "
        "work, travel and daily life."
    ),
}

_QUESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string"},
        "options": {"type": "array", "items": {"type": "string"}, "minItems": 3, "maxItems": 4},
        "answerIndex": {"type": "integer", "minimum": 0, "maximum": 3},
        "explanation": {"type": "string"},
    },
    "required": ["prompt", "options", "answerIndex", "explanation"],
}

CHECKPOINT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "questions": {"type": "array", "items": _QUESTION_SCHEMA, "minItems": 5, "maxItems": 5},
    },
    "required": ["title", "questions"],
}

READING_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "text": {"type": "string"},
        "translation": {"type": "string"},
        "glossary": {
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
            "minItems": 4,
            "maxItems": 8,
        },
        "questions": {"type": "array", "items": _QUESTION_SCHEMA, "minItems": 3, "maxItems": 3},
    },
    "required": ["title", "text", "translation", "glossary", "questions"],
}


def schema_for(kind: str) -> dict[str, Any]:
    if kind == "checkpoint":
        return CHECKPOINT_SCHEMA
    if kind == "reading":
        return READING_SCHEMA
    raise ValueError("Unknown activity kind")


def build_request(kind: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    """The generation brief: the course decides level, unit and audience."""
    schema_for(kind)
    profile = snapshot.get("profile") or {}
    level = str(profile.get("level") or "A1").upper()
    audience = audience_of(profile)
    language = str(profile.get("primary_language") or "español")
    unit = snapshot.get("currentUnit") or {}
    rules = [
        AUDIENCE_RULES[audience],
        f"Write titles, instructions, explanations and meanings in {language}; "
        f"the English content must fit CEFR {level}.",
        "Each question has exactly one correct option; the other options are "
        "plausible but clearly wrong. answerIndex is the zero-based position of "
        "the correct option. Vary that position between questions.",
        "Never include personal questions, links or sensitive topics.",
    ]
    if level in {"PRE-A1", "A1"}:
        rules.append(
            "The student may know almost no English: use only very frequent words "
            "and very short sentences, and explain everything in their language."
        )
    if kind == "checkpoint":
        task = (
            "Write a five-question checkpoint for the unit below that checks its "
            "objective: mix vocabulary, grammar and understanding of short everyday "
            "situations. Each explanation says why the right option is right."
        )
    else:
        low, high = READING_LENGTH.get(level, (60, 120))
        task = (
            f"Write a guided reading of {low} to {high} English words about the unit "
            "below or the student's goal. Add a glossary of useful words or short "
            "expressions copied exactly as they appear in the text, a full "
            f"translation into {language}, and three comprehension questions "
            "about the text."
        )
    return {
        "task": task,
        "student": {
            "level": level,
            "audience": audience,
            "primaryLanguage": language,
            "goal": str(profile.get("goal") or ""),
        },
        "unit": {
            key: unit.get(key) for key in ("title", "objective", "skill", "level")
        },
        "recentErrors": [
            item.get("corrected")
            for item in (snapshot.get("frequent_errors") or [])[:5]
            if isinstance(item, dict)
        ],
        "rules": rules,
    }


def _clean(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _questions(raw: Any, *, minimum: int) -> list[dict[str, Any]]:
    valid: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("options"), list):
            continue
        prompt = _clean(item.get("prompt"), 300)
        options = [_clean(option, 160) for option in item["options"]]
        try:
            answer = int(item.get("answerIndex"))
        except (TypeError, ValueError):
            continue
        # An empty or repeated option would make the answer ambiguous.
        if (
            not prompt
            or not 3 <= len(options) <= 4
            or not all(options)
            or len({option.casefold() for option in options}) != len(options)
            or not 0 <= answer < len(options)
        ):
            continue
        valid.append({
            "prompt": prompt,
            "options": options,
            "answer": answer,
            "explanation": _clean(item.get("explanation"), 400),
        })
    if len(valid) < minimum:
        raise ValueError("The generated activity had too few valid questions")
    return valid[:5]


def validate(kind: str, raw: Any, *, unit_id: str = "") -> dict[str, Any]:
    """A usable activity, with its answer key, or ValueError."""
    schema_for(kind)
    if not isinstance(raw, dict):
        raise ValueError("The generated activity was not an object")
    title = _clean(raw.get("title"), 120) or (
        "Prueba de la unidad" if kind == "checkpoint" else "Lectura guiada"
    )
    activity: dict[str, Any] = {
        "id": uuid.uuid4().hex, "kind": kind, "unitId": unit_id, "title": title,
    }
    if kind == "checkpoint":
        activity["questions"] = _questions(raw.get("questions"), minimum=3)
        return activity

    text = _clean(raw.get("text"), 3000)
    if len(text.split()) < 12:
        raise ValueError("The generated reading was too short")
    glossary: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw.get("glossary") if isinstance(raw.get("glossary"), list) else []:
        if not isinstance(item, dict):
            continue
        word = _clean(item.get("word"), 60)
        meaning = _clean(item.get("meaning"), 200)
        # Only words the student can actually find and tap in the text.
        if (
            not word or not meaning or word.casefold() in seen
            or not re.search(rf"(?<![\w']){re.escape(word)}(?![\w'])", text, re.IGNORECASE)
        ):
            continue
        seen.add(word.casefold())
        glossary.append({
            "word": word, "meaning": meaning, "example": _clean(item.get("example"), 250),
        })
    activity.update({
        "text": text,
        "translation": _clean(raw.get("translation"), 3500),
        "glossary": glossary[:10],
        "questions": _questions(raw.get("questions"), minimum=2),
    })
    return activity


def public(activity: dict[str, Any]) -> dict[str, Any]:
    """What the browser may see: everything except the answer key."""
    data = {key: value for key, value in activity.items() if key != "questions"}
    data["questions"] = [
        {"prompt": question["prompt"], "options": list(question["options"])}
        for question in activity["questions"]
    ]
    return data


def grade(activity: dict[str, Any], answers: Any) -> dict[str, Any]:
    answers = answers if isinstance(answers, list) else []
    review = []
    correct = 0
    for index, question in enumerate(activity["questions"]):
        try:
            choice = int(answers[index])
        except (IndexError, TypeError, ValueError):
            choice = -1
        right = choice == question["answer"]
        correct += right
        review.append({
            "prompt": question["prompt"],
            "options": question["options"],
            "choice": choice,
            "answer": question["answer"],
            "correct": right,
            "explanation": question["explanation"],
        })
    total = len(activity["questions"])
    score = round(100 * correct / total) if total else 0
    return {
        "activityId": activity["id"],
        "kind": activity["kind"],
        "title": activity["title"],
        "unitId": activity.get("unitId", ""),
        "correct": correct,
        "total": total,
        "score": score,
        "passed": score >= PASS_SCORE,
        "review": review,
    }
