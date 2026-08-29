"""Checking that an acceptance contract actually covers the request (Stage 14 §4).

Two questions, both answerable without a model, which is why they live here
rather than in a prompt:

  1. Does this criterion quote words the user actually wrote?
  2. Does the set of criteria account for everything the user asked for?

The second is the one that matters most, and it is the hole that let a
calculator asked to "add and subtract" reach COMPLETE with only addition
decomposed. A criterion set can be individually impeccable and collectively
miss half the request; nothing about the criteria themselves reveals that,
because the missing capability is missing.

The check is deliberately syntactic and conservative. It splits the request
where a person joins separate asks — commas, "and", "then", "also", "plus" —
and requires every resulting clause with real content to be quoted by at least
one criterion. It does not try to understand the request. It only refuses to
let one disappear silently.
"""

from __future__ import annotations

import re

#: Where a person joins one ask to the next. Split on these and each piece is
#: something that was asked for separately.
_SPLIT = re.compile(
    r"\s*(?:,|;|\band then\b|\bthen\b|\band also\b|\balso\b|\bplus\b|\band\b|\bas well as\b)\s*",
    re.IGNORECASE)

#: Words that carry no request on their own. A clause made only of these is
#: connective tissue ("that can", "two numbers"), not a capability.
_FILLER = {
    "a", "an", "the", "that", "this", "it", "its", "which", "who", "whom",
    "can", "could", "should", "would", "will", "shall", "must", "may",
    "i", "you", "we", "me", "my", "your", "our", "us",
    "want", "need", "like", "please", "make", "build", "create", "write",
    "with", "for", "of", "to", "in", "on", "at", "by", "from", "into",
    "is", "are", "be", "been", "being", "am", "was", "were", "do", "does",
    "did", "have", "has", "had", "get", "gets", "got",
    "two", "three", "some", "any", "all", "them", "they", "one",
    "number", "numbers", "thing", "things", "app", "program", "project",
    "and", "or", "but", "so", "if", "when", "where", "how",
}


def normalise(text: str) -> str:
    """Whitespace- and case-insensitive form, for substring comparison."""
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def is_span_of(quote: str, request: str) -> bool:
    """Whether `quote` is genuinely a span of `request`.

    Compared on the normalised form so that a quote may differ in spacing and
    case, but not in words. A criterion citing words the user never wrote is
    not traceable to the request, whatever it claims.
    """
    q, r = normalise(quote), normalise(request)
    return bool(q) and q in r


def clauses(request: str) -> list[str]:
    """The separately-asked-for pieces of a request, in order.

    Clauses of pure filler are dropped: they are how a request is phrased, not
    another thing being asked for.
    """
    out: list[str] = []
    for piece in _SPLIT.split(normalise(request)):
        piece = piece.strip()
        if not piece:
            continue
        words = [w for w in re.findall(r"[a-z0-9']+", piece)]
        if not words:
            continue
        if all(w in _FILLER for w in words):
            continue
        out.append(piece)
    return out


def _quote_pieces(quote: str) -> list[str]:
    """A quote, split the same way the request is.

    A perfectly reasonable quote can straddle a boundary: "and subtract" is a
    real span of "…can add and subtract two numbers", but the request splits at
    that "and", so comparing the whole quote to either clause matches neither.
    Splitting the quote too lets "and subtract" cover the clause "subtract two
    numbers", which is plainly what it was quoting.
    """
    pieces = [p.strip() for p in _SPLIT.split(normalise(quote))]
    return [p for p in pieces if p] or [normalise(quote)]


def uncovered_clauses(request: str, quotes: list[str]) -> list[str]:
    """Clauses of the request that no criterion quotes.

    A clause counts as covered when some piece of some quote overlaps it —
    substring in either direction. Overlap rather than equality, because a
    criterion may reasonably quote "add" out of "that can add", or quote the
    whole clause verbatim, and both are the same act.
    """
    pieces: list[str] = []
    for q in quotes:
        pieces.extend(_quote_pieces(q))
    pieces = [p for p in pieces if p]
    missing: list[str] = []
    for clause in clauses(request):
        if any(p in clause or clause in p for p in pieces):
            continue
        missing.append(clause)
    return missing
