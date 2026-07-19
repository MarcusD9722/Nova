from __future__ import annotations

"""Shared intent classification for Nova's routing decisions.

One place to answer "is this a question?" — previously every subsystem invented
its own rule with different behavior:
  - core/project_builder.py QUESTION_LEAD_RE: anchored to the literal first word
  - core/policy/chat_decider.py _TASK_REQUEST_RE/_NON_TASK_CHAT_RE: anchored at 0

That anchoring is exactly how "I meant What other improvements can we make to
the flappy-bird game?" got treated as a work request instead of a question: the
"I meant" preamble hid the "What", so Nova went and edited code instead of
answering. This module strips conversational preamble first, and doesn't require
a literal "?" for WH-questions.
"""

import re

__all__ = ["is_question", "strip_preamble"]

# Conversational lead-ins that hide a sentence's real shape. Applied repeatedly
# so "So, Nova, I meant what..." reduces to "what...".
_PREAMBLE_RE = re.compile(
    r"^\s*(?:"
    r"i\s+(?:meant|mean|said|was\s+asking)|"
    r"so|well|actually|wait|also|and|but|ok|okay|hmm|um|uh|"
    r"hey\s+nova|hi\s+nova|nova|hey|hi|"
    r"please|just|quick\s+question|question"
    r")\b[\s,:—-]*",
    re.IGNORECASE,
)

# WH-words essentially never open an imperative in natural English, so they
# signal a question on their own — no "?" needed (people drop it constantly).
_WH_LEAD_RE = re.compile(
    r"^\s*(?:what|why|how|which|who|whom|whose|where|when)\b",
    re.IGNORECASE,
)

# Polar auxiliaries CAN open a real request ("Can you build me a game"), so
# these only count as a question when the sentence actually ends in "?".
_POLAR_LEAD_RE = re.compile(
    r"^\s*(?:is|are|was|were|do|does|did|can|could|would|will|shall|should|have|has|had|am)\b",
    re.IGNORECASE,
)

# "any ideas?", "thoughts?", "right?" — trailing-? with no imperative verb.
_IMPERATIVE_LEAD_RE = re.compile(
    r"^\s*(?:make|create|build|code|write|develop|start|begin|add|set\s+up|put|wire|hook|"
    r"fix|improve|change|update|remove|delete|give|show\s+me|open|run|try)\b",
    re.IGNORECASE,
)


def strip_preamble(text: str) -> str:
    """Remove conversational lead-ins so the sentence's real shape is visible."""
    t = (text or "").strip()
    for _ in range(6):  # bounded; handles stacked openers like "So, Nova, I meant ..."
        new = _PREAMBLE_RE.sub("", t, count=1).strip()
        if new == t or not new:
            break
        t = new
    return t or (text or "").strip()


def is_question(text: str) -> bool:
    """True when the message is ASKING something rather than instructing.

    Robust to preamble ("I meant ...", "So ...", "Nova, ...") and to a missing
    question mark on WH-questions.
    """
    raw = (text or "").strip()
    if not raw:
        return False
    core = strip_preamble(raw)
    ends_q = core.rstrip().endswith("?") or raw.rstrip().endswith("?")

    # "What/why/how ..." — a question with or without punctuation.
    if _WH_LEAD_RE.match(core):
        return True
    # "Can you ...?" / "Should we ...?" — only with the question mark, since
    # "Can you build me X" is a genuine request.
    if ends_q and _POLAR_LEAD_RE.match(core):
        return True
    # Ends in "?" and doesn't open with an imperative verb -> treat as asking.
    if ends_q and not _IMPERATIVE_LEAD_RE.match(core):
        return True
    return False
