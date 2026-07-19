from __future__ import annotations

"""Lightweight mood signal detection for M1 (emotional presence).

Deliberately a coarse keyword heuristic, not an LLM call — this runs on every
user turn, and adding an LLM call per turn would slow down every single
message just to guess a mood. Consistent with the existing per-turn
regex-based passes (quick fact extraction, lesson capture) elsewhere in
core/runtime.py. Honest about what it is: a rough signal, not real
understanding — used to make replies a little warmer/more attentive, not to
claim genuine insight into how Marcus feels.
"""

import re

__all__ = ["detect_mood_signal", "MOOD_LABELS"]

MOOD_LABELS = {"stressed", "tired", "frustrated", "sad", "anxious", "happy", "excited", "content"}

_NEGATIVE_PATTERNS: dict[str, re.Pattern[str]] = {
    "stressed": re.compile(r"\b(?:stressed|stressful|overwhelmed|so much (?:going on|to do)|swamped)\b", re.IGNORECASE),
    "tired": re.compile(r"\b(?:exhausted|so tired|drained|worn out|dead tired|burnt out|burned out)\b", re.IGNORECASE),
    "frustrated": re.compile(r"\b(?:frustrat\w+|annoy\w+|fed up|sick of|so mad|pissed off|ugh+\b)\b", re.IGNORECASE),
    "sad": re.compile(r"\b(?:sad|down today|not (?:doing|feeling) (?:great|good|well)|rough day|hard day|awful day|terrible day)\b", re.IGNORECASE),
    "anxious": re.compile(r"\b(?:anxious|worried|nervous|on edge|freaking out|panicking)\b", re.IGNORECASE),
}
_POSITIVE_PATTERNS: dict[str, re.Pattern[str]] = {
    "excited": re.compile(r"\b(?:excited|can'?t wait|so pumped|thrilled)\b", re.IGNORECASE),
    "happy": re.compile(r"\b(?:great day|amazing day|wonderful day|so happy|feeling great|awesome day)\b", re.IGNORECASE),
    "content": re.compile(r"\b(?:good day|pretty good day|going (?:well|great)|feeling good)\b", re.IGNORECASE),
}


def detect_mood_signal(text: str) -> str | None:
    """Return a coarse mood label if the message carries a clear signal, else
    None. Deliberately conservative — most messages return None, since most
    messages (tasks, questions, small talk) don't carry an emotional signal
    worth recording."""
    t = (text or "").strip()
    if not t or len(t) > 600:
        return None
    for label, pattern in _NEGATIVE_PATTERNS.items():
        if pattern.search(t):
            return label
    for label, pattern in _POSITIVE_PATTERNS.items():
        if pattern.search(t):
            return label
    return None
