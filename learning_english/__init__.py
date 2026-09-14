"""Learning English mode for Lumina Start Talk.

The package intentionally owns educational state, validation and persistence.
Audio capture/playback stays in :mod:`main`, where the proven Gemini Live path
already guarantees that only one microphone and one speaker stream exist.
"""

from .controller import LearningEnglishController
from .intent import IntentMatch, detect_learning_english_intent
from .service import LearningEnglishService
from .store import LearningProgressStore
from .types import LearningEnglishState, TutorResponse

__all__ = [
    "IntentMatch",
    "LearningEnglishController",
    "LearningEnglishService",
    "LearningEnglishState",
    "LearningProgressStore",
    "TutorResponse",
    "detect_learning_english_intent",
]
