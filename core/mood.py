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

__all__ = ["detect_mood_signal", "MOOD_LABELS", "emotional_salience"]

MOOD_LABELS = {"stressed", "tired", "frustrated", "sad", "anxious", "happy", "excited", "content"}

# How strongly each mood marks a moment for memory. Emotional arousal — not
# valence — is what makes a memory stick: you remember the day something went
# badly wrong and the day something went wonderfully right, and forget the
# pleasant ordinary ones. So "excited" and "frustrated" score high while
# "content" barely registers.
_AROUSAL: dict[str, float] = {
    "excited": 0.9,
    "frustrated": 0.8,
    "anxious": 0.8,
    "sad": 0.75,
    "stressed": 0.7,
    "happy": 0.65,
    "tired": 0.4,
    "content": 0.25,
}

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


def emotional_salience(text: str) -> float:
    """How strongly this message marks the moment for memory, in [0, 1].

    Emotion is the clearest natural signal for which memories should last, and
    Nova already computes it on every turn — it was just being thrown away for
    memory purposes. A fact learned during a charged moment ("Liam was born
    today") should outlive one learned in passing ("we had pasta"), and this
    is what makes that happen: salience extends a memory's half-life rather
    than boosting its rank.

    Returns 0.0 for the ordinary messages that make up most of a conversation,
    which is correct — most moments are not memorable, and pretending
    otherwise would flatten the signal entirely.
    """
    label = detect_mood_signal(text)
    if label is None:
        return 0.0
    return _AROUSAL.get(label, 0.5)
