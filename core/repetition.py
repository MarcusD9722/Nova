from __future__ import annotations

"""Don't answer the last question again.

Live: Nova gave a capability answer, Marcus then said something entirely new —

    "She's enjoying preparing everything. She has decided on themes for their
     parties already."

— and Nova replied with essentially her previous capability answer.

The semantic response cache was ruled out first: `core/semantic_cache.py` is
imported by no production module (only by a test), so it is not in the chat
path, and this was not a stale cache hit. Prior assistant replies ARE in the
prompt for continuity, which is correct and must stay; the model simply reused
one.

So the guard belongs on the OUTPUT, not on the context. Removing history to
stop repetition would trade this bug for a worse one — an assistant with no
memory of what it just said.

WHAT THIS IS NOT
----------------
It is not a ban on saying the same thing twice. Asked her birthday twice,
Nova should give the same date both times. The target is accidental reuse of a
WHOLE reply when the user has moved on — so the comparison is whole-response
similarity, and an explicit request to repeat bypasses it entirely.
"""

import re
from difflib import SequenceMatcher

__all__ = [
    "REPEAT_SIMILARITY",
    "normalize",
    "similarity",
    "is_near_duplicate",
    "wants_repeat",
    "materially_different",
]

#: Whole-response similarity above which a reply counts as accidental reuse.
#: High on purpose: two answers about the same subject share plenty of wording,
#: and the failure being caught is near-verbatim replay, not overlap.
REPEAT_SIMILARITY = 0.90

#: Below this many characters, similarity is noise ("Sure." vs "Sure!").
MIN_GUARDED_CHARS = 40

_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]")

#: "say that again", "repeat that", "what did you just say"
_REPEAT_REQUEST_RE = re.compile(
    r"\b(?:say\s+(?:that|it|this)\s+again|repeat\s+(?:that|it|this|your\s+last|"
    r"the\s+last|what\s+you)|again\s+please|"
    r"what\s+did\s+you\s+(?:just\s+)?say|"
    r"can\s+you\s+repeat|could\s+you\s+repeat|"
    r"one\s+more\s+time|come\s+again)\b",
    re.IGNORECASE,
)


def normalize(text: str) -> str:
    """Casefolded, punctuation-free, whitespace-collapsed."""
    t = _PUNCT_RE.sub(" ", (text or "").casefold())
    return _WS_RE.sub(" ", t).strip()


def similarity(a: str, b: str) -> float:
    """Deterministic 0..1 similarity. No model call — this runs on every reply.

    SequenceMatcher over normalised text, combined with token overlap so a
    reordered or lightly-edited replay still scores high. Both halves are cheap
    and stable; the first check for a repeat should not cost another generation.
    """
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0

    ratio = SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    jaccard = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
    return max(ratio, jaccard)


def wants_repeat(user_text: str) -> bool:
    """Did the user ASK for the previous answer again?"""
    return bool(_REPEAT_REQUEST_RE.search(user_text or ""))


def materially_different(new_user_text: str, previous_user_text: str) -> bool:
    """Is the user actually saying something new?

    If they repeated themselves, an identical answer is reasonable and the
    guard should stay out of the way.
    """
    if not previous_user_text:
        return True
    return similarity(new_user_text, previous_user_text) < REPEAT_SIMILARITY


def is_near_duplicate(candidate: str, previous_replies: list[str] | None,
                      *, threshold: float = REPEAT_SIMILARITY) -> str | None:
    """The recent reply this candidate is a replay of, or None.

    Returns the matched reply (not just a bool) so the caller can put it in the
    regeneration prompt — telling the model exactly what not to say again is
    far more effective than telling it to "be different".
    """
    cand = (candidate or "").strip()
    if len(normalize(cand)) < MIN_GUARDED_CHARS:
        return None
    for prev in reversed(previous_replies or []):
        prev = (prev or "").strip()
        if len(normalize(prev)) < MIN_GUARDED_CHARS:
            continue
        if similarity(cand, prev) >= threshold:
            return prev
    return None
