"""Local speaker identification (V3 P5).

Open-set: Nova can say "Marcus", "Alice", "someone I don't recognise", "I can't
tell", "not enough audio" or "unavailable" — and the last four are not failures,
they are the honest answers.

NOT authentication. See core/speaker/service.py.
"""

from core.speaker.backend import EMBEDDER, MODEL_ID, MODEL_REVISION, enabled
from core.speaker.matcher import (STATUS_AMBIGUOUS, STATUS_KNOWN, STATUS_TOO_SHORT,
                                  STATUS_UNAVAILABLE, STATUS_UNKNOWN, SpeakerMatch)
from core.speaker.registry import SpeakerProfile, SpeakerRegistry
from core.speaker.service import SpeakerService

__all__ = [
    "EMBEDDER", "MODEL_ID", "MODEL_REVISION", "enabled",
    "SpeakerMatch", "SpeakerProfile", "SpeakerRegistry", "SpeakerService",
    "STATUS_KNOWN", "STATUS_UNKNOWN", "STATUS_AMBIGUOUS",
    "STATUS_TOO_SHORT", "STATUS_UNAVAILABLE",
]
