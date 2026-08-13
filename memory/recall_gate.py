from __future__ import annotations

"""Recall gate — decide whether a turn actually needs long-term memory.

Nova's retrieval got good, which made it expensive. `MemoryUnifier.search()`
runs on **every** turn (core/runtime.py), including "the second one" and "thanks"
— queries where a vector search over everything Marcus has ever said cannot
possibly help, and where the latency is paid on the most conversational turns.

The gate is an optimisation and nothing more. It is not memory logic and has no
authority to decide what Nova knows. That distinction drives the whole design:

    **It fails OPEN.** Every ambiguous, unrecognised or unexpected case
    RECALLS. Skipping is only allowed when there is positive evidence that the
    answer is already in working context.

The cost of a wrong SKIP is that Nova forgets something she knows, which is the
single worst thing this assistant can do. The cost of a wrong RECALL is a few
hundred milliseconds. Those are not comparable, so the thresholds are not
symmetric.

No LLM call. No embedding. Pure string work, so the gate can never cost more
than the search it is trying to avoid.
"""

import re
from dataclasses import dataclass, field

from memory.artifacts import parse_reference

#: Explicitly historical language. Any of these forces recall — they are direct
#: requests to remember, and the gate must never override one.
_HISTORICAL = re.compile(
    r'\b(remember|recall|forget|forgot|last (?:week|month|year|time|night)|'
    r'yesterday|earlier|before|previously|used to|we decided|you said|i told you|'
    r'i mentioned|back then|the other day|a while ago|ago|history|always|usually|'
    r'my |mine\b|our )',
    re.IGNORECASE,
)

#: Language pointing at the last few seconds of conversation rather than the past.
_IMMEDIATE = re.compile(
    r'\b(just (?:now|said|did)|a (?:second|moment|minute) ago|right now|'
    r'you just|we just|that one|this one)\b',
    re.IGNORECASE,
)

#: Pure social turns. An allowlist: anything not positively recognised here is
#: not treated as social.
_SOCIAL = re.compile(
    r'^\s*(?:'
    r'(?:hey|hi|hello|yo|morning|good morning|good afternoon|good evening|good night|'
    r'thanks|thank you|thx|cheers|ok|okay|cool|nice|great|awesome|sure|yep|yeah|nope|no|'
    r'sorry|please|bye|goodbye|night|later)'
    r'[\s,!.]*)+\??\s*$',
    re.IGNORECASE,
)

_STOPWORDS = frozenset("""
a an the and or but if then than that this these those is are was were be been being am
do does did doing have has had having i you he she it we they me him her us them my your
his its our their to of in on at by for with about from as so not no yes what which who
whom whose when where why how can could will would shall should may might must one two
me please just now also very really tell show give get make take see look
say said says saying told telling mean means asked ask answer answered call called
""".split())

_WORD = re.compile(r"[a-z0-9][a-z0-9'-]*")

#: Any alphanumeric character in any script. Used only to tell "this query has
#: content we failed to tokenise" apart from "this query is punctuation".
_ANY_ALNUM = re.compile(r"\w", re.UNICODE)


@dataclass
class GateDecision:
    recall: bool
    reason: str
    signals: dict[str, object] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.recall


def _stem(word: str) -> str:
    """Crudest possible stemmer, and deliberately so.

    It exists for one job: letting "decide" match "decided" when checking
    whether the last few turns already contain the answer. Anything cleverer
    would need a real morphology table, and getting this wrong in either
    direction only shifts a latency optimisation — never what Nova knows.
    """
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            word = word[: -len(suffix)]
            break
    # Also drop a trailing 'e', or the stemmer is asymmetric: "decided" would
    # reduce to "decid" while "decide" stayed whole, and the two would never
    # match each other.
    if len(word) > 3 and word.endswith("e"):
        word = word[:-1]
    return word


def _content_words(text: str) -> set[str]:
    return {_stem(w) for w in _WORD.findall((text or "").lower())
            if w not in _STOPWORDS and len(w) > 2}


def should_recall(
    query: str,
    *,
    recent_text: str = "",
    has_result_set: bool = False,
    item_count: int = 0,
    last_tool_summary: str = "",
) -> GateDecision:
    """Does this turn need a long-term memory search?

    Returns a decision, never a bare bool, because the reason is what makes a
    wrong answer debuggable.
    """
    q = (query or "").strip()
    if not q:
        return GateDecision(False, "empty query", {})

    # 1. Explicit request to remember always wins, before any skip rule. This
    #    is checked first so no later heuristic can suppress it.
    if _HISTORICAL.search(q):
        return GateDecision(True, "explicitly historical or personal language",
                            {"rule": "historical"})

    # 2. A positional reference into the result set on screen. "the second one"
    #    is answered by artifacts, and a semantic search for it is meaningless.
    if has_result_set and item_count > 0:
        ref = parse_reference(q, item_count=item_count)
        if ref.kind in {"ordinal", "last", "other"}:
            return GateDecision(False, f"resolves to result item ({ref.kind})",
                                {"rule": "artifact_reference", "reference": ref.kind,
                                 "index": ref.index})

    # 3. Pure social noise. The allowlist vetoes itself on anything else.
    if _SOCIAL.match(q):
        return GateDecision(False, "purely social turn", {"rule": "social"})

    words = _content_words(q)
    if not words:
        # Tokenising failed. That is NOT evidence there is nothing to find — the
        # tokeniser is ASCII-only, so any non-Latin script lands here. Skipping
        # would make Nova silently amnesiac in another language, which is
        # exactly the fail-closed behaviour this gate must never have.
        if _ANY_ALNUM.search(q):
            return GateDecision(True, "content this gate cannot tokenise — recalling to be safe",
                                {"rule": "untokenisable"})
        return GateDecision(False, "no content words to search for", {"rule": "no_content"})

    # 4. Everything the user is asking about is already sitting in the last few
    #    turns, AND they are pointing at the immediate past. Both halves are
    #    required: overlap alone is far too weak (asking a follow-up question
    #    about a topic does not mean the answer was already given).
    context_words = _content_words(recent_text) | _content_words(last_tool_summary)
    if context_words:
        covered = words & context_words
        coverage = len(covered) / len(words)
        if _IMMEDIATE.search(q) and coverage >= 0.6:
            return GateDecision(False, "answer is in the last few turns",
                                {"rule": "immediate_context", "coverage": round(coverage, 2)})

    # 5. Default: recall. This is the branch every unrecognised query takes.
    return GateDecision(True, "no evidence working context already answers this",
                        {"rule": "default_open", "content_words": len(words)})
