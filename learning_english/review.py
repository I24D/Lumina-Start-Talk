"""FSRS-backed vocabulary review scheduling with a safe local fallback."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

try:
    from fsrs import Card, Rating, Scheduler
except ImportError:  # The app remains usable until optional deps are installed.
    Card = Rating = Scheduler = None


_RATINGS = {"again": 1, "hard": 2, "good": 3, "easy": 4}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_date(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return _utc_now()


class ReviewScheduler:
    """Small persistence adapter around the official Python FSRS package."""

    engine = "fsrs-6" if Scheduler is not None else "local-fallback"

    def __init__(self) -> None:
        self._scheduler = Scheduler() if Scheduler is not None else None

    def new_card(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or _utc_now()
        if self._scheduler is not None:
            card = Card(due=now)
            return card.to_dict()
        return {
            "state": 1,
            "step": 0,
            "due": now.isoformat(),
            "stability": None,
            "difficulty": None,
            "last_review": None,
        }

    def review(
        self,
        card_data: dict[str, Any] | None,
        rating: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = now or _utc_now()
        key = str(rating or "").strip().lower()
        if key not in _RATINGS:
            raise ValueError("Review rating must be again, hard, good, or easy")

        if self._scheduler is not None:
            try:
                card = Card.from_dict(card_data) if card_data else Card(due=now)
            except (TypeError, ValueError, KeyError):
                card = Card(due=now)
            card, _log = self._scheduler.review_card(
                card, Rating(_RATINGS[key]), review_datetime=now
            )
            return {
                "card": card.to_dict(),
                "dueAt": card.due.isoformat(),
                "retrievability": round(
                    self._scheduler.get_card_retrievability(card, now), 4
                ),
                "engine": self.engine,
            }

        # A deterministic fallback keeps reviews functional without pretending
        # to be FSRS. Installing requirements.txt upgrades existing cards.
        intervals = {"again": 0, "hard": 1, "good": 3, "easy": 7}
        prior = dict(card_data or self.new_card(now))
        reviews = int(prior.get("reviews", 0)) + 1
        multiplier = min(6, max(1, reviews))
        due = now + timedelta(days=intervals[key] * multiplier, minutes=10 if key == "again" else 0)
        prior.update({"due": due.isoformat(), "last_review": now.isoformat(), "reviews": reviews})
        return {"card": prior, "dueAt": due.isoformat(), "retrievability": 1.0, "engine": self.engine}

    @staticmethod
    def is_due(card_data: dict[str, Any] | None, now: datetime | None = None) -> bool:
        if not card_data:
            return True
        return _parse_date(card_data.get("due")) <= (now or _utc_now())
