"""Adaptive written placement test for Learning English.

A short staircase over Lumina's own question bank: three questions per level,
two right answers pass a level and two wrong answers fail it. The browser only
ever sees the next question; the answer key and the grading stay here. The
result is a provisional level that the tutor's spoken assessment can refine.
"""

from __future__ import annotations

from typing import Any


LEVEL_ORDER = ("PRE-A1", "A1", "A2", "B1", "B2", "C1")

QUESTIONS: tuple[dict[str, Any], ...] = (
    {"id": "pre-a1-colour", "level": "PRE-A1", "prompt": "¿Cómo se dice «rojo» en inglés?", "options": ["Blue", "Red", "Green", "Yellow"], "answer": 1},
    {"id": "pre-a1-number", "level": "PRE-A1", "prompt": "¿Qué número es «seven»?", "options": ["6", "7", "11", "17"], "answer": 1},
    {"id": "pre-a1-hello", "level": "PRE-A1", "prompt": "Alguien te dice «Hello!». ¿Qué respondes?", "options": ["Goodbye!", "Hello!", "Thank you!", "Sorry!"], "answer": 1},
    {"id": "a1-to-be", "level": "A1", "prompt": "Complete: I ___ a student.", "options": ["am", "is", "are", "be"], "answer": 0},
    {"id": "a1-name", "level": "A1", "prompt": "Choose the right answer: What's your name?", "options": ["I'm fine, thanks.", "My name is Ana.", "It's ten o'clock.", "Yes, I am."], "answer": 1},
    {"id": "a1-third-person", "level": "A1", "prompt": "Complete: She ___ in Mexico.", "options": ["live", "living", "lives", "to live"], "answer": 2},
    {"id": "a2-past", "level": "A2", "prompt": "Complete: Yesterday we ___ to the beach.", "options": ["go", "went", "gone", "going"], "answer": 1},
    {"id": "a2-quantity", "level": "A2", "prompt": "Choose the correct sentence.", "options": ["There is many people here.", "There are much people here.", "There are a lot of people here.", "There is a lot people here."], "answer": 2},
    {"id": "a2-plans", "level": "A2", "prompt": "Complete: I'm going ___ my grandmother this weekend.", "options": ["visit", "to visit", "visiting", "visited"], "answer": 1},
    {"id": "b1-conditional", "level": "B1", "prompt": "Complete: If it rains tomorrow, we ___ at home.", "options": ["stay", "will stay", "would stay", "stayed"], "answer": 1},
    {"id": "b1-since", "level": "B1", "prompt": "Complete: I have lived here ___ 2019.", "options": ["for", "since", "from", "during"], "answer": 1},
    {"id": "b1-reply", "level": "B1", "prompt": "Choose the best reply: \"Could you help me with this form?\"", "options": ["Sure, what do you need?", "Yes, I could.", "No, I don't help.", "It's a form."], "answer": 0},
    {"id": "b2-past-perfect", "level": "B2", "prompt": "Complete: By the time we arrived, the film ___.", "options": ["already started", "has already started", "had already started", "was already starting"], "answer": 2},
    {"id": "b2-phrasal", "level": "B2", "prompt": "Choose the closest meaning: \"She turned down the job offer.\"", "options": ["She accepted it.", "She rejected it.", "She lowered it.", "She reported it."], "answer": 1},
    {"id": "b2-wish", "level": "B2", "prompt": "Complete: I wish I ___ more time to study.", "options": ["have", "had", "will have", "am having"], "answer": 1},
    {"id": "c1-inversion", "level": "C1", "prompt": "Complete: Hardly ___ the meeting started when the power went out.", "options": ["had", "has", "did", "was"], "answer": 0},
    {"id": "c1-collocation", "level": "C1", "prompt": "Choose the most natural option: \"The results were ___ from what we expected.\"", "options": ["far removed", "far removing", "far remove", "far to remove"], "answer": 0},
    {"id": "c1-not-only", "level": "C1", "prompt": "Complete: Not only ___ late, but he also forgot the documents.", "options": ["he arrived", "did he arrive", "he did arrive", "arrived he"], "answer": 1},
)

_BY_ID = {question["id"]: question for question in QUESTIONS}


def _answers_by_level(answers: Any) -> dict[str, list[bool]]:
    """Right or wrong per level, in the order answered; unknown or repeated
    question ids are ignored so a replayed request cannot skew the result."""
    by_level: dict[str, list[bool]] = {level: [] for level in LEVEL_ORDER}
    seen: set[str] = set()
    for item in answers if isinstance(answers, list) else []:
        if not isinstance(item, dict):
            continue
        question = _BY_ID.get(str(item.get("id") or ""))
        if not question or question["id"] in seen:
            continue
        seen.add(question["id"])
        try:
            choice = int(item.get("choice"))
        except (TypeError, ValueError):
            choice = -1
        by_level[question["level"]].append(choice == question["answer"])
    return by_level


def _decision(results: list[bool]) -> bool | None:
    if results.count(True) >= 2:
        return True
    if results.count(False) >= 2:
        return False
    return None


def advance(answers: Any, *, audience: str = "adults") -> dict[str, Any]:
    """The next question, or the provisional level once the staircase settles.

    Children start at Pre-A1 and everyone else at A1. A passed level moves up,
    a failed one moves down, and the test ends where the two meet."""
    by_level = _answers_by_level(answers)
    decisions = {level: _decision(results) for level, results in by_level.items()}
    answered = sum(len(results) for results in by_level.values())
    correct = sum(results.count(True) for results in by_level.values())
    level = "PRE-A1" if audience == "kids" else "A1"
    while True:
        index = LEVEL_ORDER.index(level)
        decided = decisions[level]
        if decided is None:
            pending = [q for q in QUESTIONS if q["level"] == level][len(by_level[level])]
            return {
                "done": False,
                "answered": answered,
                "question": {
                    "id": pending["id"],
                    "prompt": pending["prompt"],
                    "options": list(pending["options"]),
                },
            }
        if decided:
            above = LEVEL_ORDER[index + 1] if index + 1 < len(LEVEL_ORDER) else None
            if above is None or decisions[above] is False:
                return _result(level, correct, answered)
            level = above
        else:
            if index == 0:
                return _result(level, correct, answered)
            below = LEVEL_ORDER[index - 1]
            if decisions[below] is True:
                return _result(below, correct, answered)
            level = below


def _result(level: str, correct: int, answered: int) -> dict[str, Any]:
    return {"done": True, "level": level, "correct": correct, "answered": answered}
