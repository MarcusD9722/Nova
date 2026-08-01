from __future__ import annotations

"""Semantic response cache (U5).

Repeated questions — asked in different words — currently re-run the whole turn:
memory search, tool decisions, full generation. This caches recent answers by
their *meaning* (embedding similarity) rather than exact text, so "what's my
wife's name" hits the entry stored for "who is my spouse".

The risk with any response cache is serving something stale or wrong, so the
admission rules are deliberately strict — it is far better to miss a cache hit
than to answer a new question with an old answer:

* **Never cache a turn that used tools.** Weather, maps, search, email are live
  by definition.
* **Never cache anything time-sensitive** — the question or answer mentioning a
  clock, date, "now", "today", "currently" disqualifies it.
* **Never cache short/degenerate exchanges**, which are usually greetings.
* **High similarity bar** (default 0.95) and a **short TTL** (default 1h).
* Any *write* to memory invalidates the whole cache — if what Nova knows
  changed, previous answers may no longer be true.

Off unless embeddings are available; falls back to simply never hitting.
"""

import math
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from core.logging_setup import get_logger

logger = get_logger(__name__)

# A question or answer containing any of these is inherently time-bound.
_VOLATILE = re.compile(
    r"\b(now|today|tonight|tomorrow|yesterday|currently|right now|this (?:morning|afternoon|evening|week)|"
    r"o'?clock|\d{1,2}:\d{2}|am|pm|weather|temperature|forecast|traffic|latest|news)\b",
    re.IGNORECASE,
)


def cache_enabled() -> bool:
    return os.getenv("NOVA_SEMANTIC_CACHE", "1").strip().lower() not in {"0", "false", "no", "off"}


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return (dot / (na * nb)) if na and nb else 0.0


@dataclass
class _Entry:
    vector: list[float]
    question: str
    answer: str
    created: float = field(default_factory=time.monotonic)


class SemanticCache:
    def __init__(self, *, threshold: float | None = None, ttl_s: float | None = None,
                 max_entries: int = 64) -> None:
        self._threshold = threshold if threshold is not None else float(
            os.getenv("NOVA_SEMANTIC_CACHE_THRESHOLD", "0.95") or 0.95)
        self._ttl = ttl_s if ttl_s is not None else float(
            os.getenv("NOVA_SEMANTIC_CACHE_TTL_S", "3600") or 3600)
        self._max = max_entries
        self._entries: list[_Entry] = []
        self.hits = 0
        self.misses = 0

    # ── admission policy (pure, easy to reason about and test) ──
    @staticmethod
    def cacheable(question: str, answer: str, *, tools_used: int = 0) -> bool:
        q, a = (question or "").strip(), (answer or "").strip()
        if tools_used:
            return False                      # live data — never cache
        if len(q) < 12 or len(a) < 20:
            return False                      # greetings/acks
        if _VOLATILE.search(q) or _VOLATILE.search(a):
            return False                      # time-bound
        return True

    def _embed(self, text: str) -> list[float]:
        from memory import embeddings

        if not embeddings.embedding_available():
            return []
        try:
            vecs = embeddings.embed_texts([text])
            return list(vecs[0]) if vecs else []
        except Exception as e:  # noqa: BLE001
            logger.debug("semantic_cache_embed_failed", error=str(e)[:160])
            return []

    def _prune(self) -> None:
        now = time.monotonic()
        self._entries = [e for e in self._entries if (now - e.created) < self._ttl]
        if len(self._entries) > self._max:
            self._entries = self._entries[-self._max:]

    def lookup(self, question: str) -> tuple[str, float] | None:
        """Best semantically-equivalent recent answer, or None."""
        if not cache_enabled() or not (question or "").strip():
            return None
        self._prune()
        if not self._entries:
            self.misses += 1
            return None
        vec = self._embed(question)
        if not vec:
            self.misses += 1
            return None
        best, best_score = None, 0.0
        for e in self._entries:
            score = _cosine(vec, e.vector)
            if score > best_score:
                best, best_score = e, score
        if best is not None and best_score >= self._threshold:
            self.hits += 1
            logger.info("semantic_cache_hit", score=round(best_score, 3), question=question[:60])
            return best.answer, best_score
        self.misses += 1
        return None

    def store(self, question: str, answer: str, *, tools_used: int = 0) -> bool:
        if not cache_enabled() or not self.cacheable(question, answer, tools_used=tools_used):
            return False
        vec = self._embed(question)
        if not vec:
            return False
        self._entries.append(_Entry(vector=vec, question=question.strip(), answer=answer.strip()))
        self._prune()
        return True

    def invalidate(self, reason: str = "memory changed") -> int:
        """Drop everything — called whenever memory is written, because a stored
        answer may no longer be true."""
        n = len(self._entries)
        self._entries.clear()
        if n:
            logger.debug("semantic_cache_invalidated", entries=n, reason=reason)
        return n

    def stats(self) -> dict[str, Any]:
        return {"enabled": cache_enabled(), "entries": len(self._entries),
                "hits": self.hits, "misses": self.misses,
                "threshold": self._threshold, "ttl_s": self._ttl}
