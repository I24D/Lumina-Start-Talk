"""Learning English mode for Lumina Start Talk.

The package owns the course: curriculum, placement test, generated practice,
validation and persistence. The class itself talks through its own Gemini Live
session in the studio page, with the browser's microphone; Lumina's server only
mints that session's token and keeps the progress.
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
