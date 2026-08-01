"""U5: semantic response cache + conversational memory correction.

The cache's admission rules are the safety story (never serve a stale answer to
a live question), so they're tested as pure logic without needing an embedding
model. Correction is tested end-to-end against real memory.
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.semantic_cache import SemanticCache, _cosine
from memory.unifier import MemoryUnifier

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


class StubCache(SemanticCache):
    """Deterministic fake embeddings: bag-of-words vector over a fixed vocab, so
    cache behavior is testable without loading a real embedding model."""
    VOCAB = ["spouse", "wife", "name", "who", "what", "married", "weather", "time", "project", "build"]

    def _embed(self, text: str):
        low = (text or "").lower()
        return [1.0 if w in low else 0.0 for w in self.VOCAB] or [0.0]


async def main():
    # ── cosine sanity ──
    check(abs(_cosine([1, 0], [1, 0]) - 1.0) < 1e-9, "cosine of identical vectors is 1")
    check(_cosine([1, 0], [0, 1]) == 0.0, "orthogonal vectors score 0")
    check(_cosine([], [1]) == 0.0, "degenerate vectors score 0 (no crash)")

    # ── ADMISSION RULES — the safety story ──
    long_q = "who is my spouse and what is her name"
    long_a = "Your spouse is Leslie, and you have two children together."
    check(SemanticCache.cacheable(long_q, long_a), "a stable factual exchange is cacheable")
    check(not SemanticCache.cacheable(long_q, long_a, tools_used=1),
          "a turn that used TOOLS is never cached (live data)")
    check(not SemanticCache.cacheable("what time is it right now", "It is 4:15 PM."),
          "time-sensitive question is never cached")
    check(not SemanticCache.cacheable("what's the weather in Austin", "It is 88F and sunny."),
          "weather is never cached")
    check(not SemanticCache.cacheable("hey", long_a), "a too-short question is not cached")
    check(not SemanticCache.cacheable(long_q, "yes"), "a too-short answer is not cached")
    check(not SemanticCache.cacheable("what did we do today", long_a),
          "a question about 'today' is not cached")

    # ── hit on a semantically equivalent rephrasing ──
    # NOTE: the stub's bag-of-words vectors are far coarser than real embeddings
    # (the rephrasing scores ~0.58, an unrelated question ~0.29), so the
    # threshold here is set to separate those two — this exercises the cache's
    # MECHANICS, not embedding quality.
    c = StubCache(threshold=0.5)
    check(c.store(long_q, long_a), "stored a cacheable exchange")
    hit = c.lookup("what is my wife's name")
    check(hit is not None, "semantically equivalent question HITS")
    if hit:
        check(hit[0] == long_a, "hit returns the stored answer")
        check(hit[1] >= 0.5, f"hit score meets the threshold (got {hit[1]:.2f})")

    check(c.lookup("what should I build for this project") is None,
          "an unrelated question MISSES (no false hit)")

    # ── threshold is respected ──
    strict = StubCache(threshold=0.999)
    strict.store(long_q, long_a)
    check(strict.lookup("what is my wife's name") is None, "a high threshold suppresses a loose match")

    # ── a memory write invalidates everything (answers may no longer be true) ──
    c2 = StubCache(threshold=0.5)
    c2.store(long_q, long_a)
    check(c2.invalidate("test") == 1, "invalidate clears entries and reports the count")
    check(c2.lookup("what is my wife's name") is None, "after invalidation, nothing hits")

    # ── TTL expiry ──
    ttl = StubCache(threshold=0.5, ttl_s=0.01)
    ttl.store(long_q, long_a)
    await asyncio.sleep(0.05)
    check(ttl.lookup(long_q) is None, "entries expire after the TTL")

    st = c.stats()
    check(st["hits"] >= 1 and st["misses"] >= 1, "stats track hits and misses")

    # ── CONVERSATIONAL MEMORY CORRECTION ──
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem = MemoryUnifier(Path(td), enable_chroma=False)
        await mem.initialize()

        # Nova remembered something wrong.
        await mem.add_fact(entity="user", attribute="anniversary", value="June 12", confidence=0.8)
        check(len(await mem.get_facts(entity="user", attribute="anniversary")) == 1, "original fact stored")

        res = await mem.correct_fact("user", "anniversary", "June 14")
        check(res["ok"] and res["previous"] == "June 12" and res["now"] == "June 14",
              f"correction reports what changed (got {res})")

        rows = await mem.get_facts(entity="user", attribute="anniversary")
        check(len(rows) == 1, "correction SUPERSEDES — not two contradictory facts")
        check(rows[0].value == "June 14", "the corrected value is what remains")
        check(rows[0].verification_status == "stated", "correction is recorded as user-stated")
        check("June 12" in (rows[0].evidence or ""), "the superseded value is kept as evidence (auditable)")

        # correcting a list-valued attribute with an explicit old value
        await mem.add_fact(entity="user", attribute="friend", value="Dave", confidence=0.8)
        await mem.add_fact(entity="user", attribute="friend", value="Sam", confidence=0.8)
        await mem.correct_fact("user", "friend", "David", old_value="Dave")
        friends = {r.value for r in await mem.get_facts(entity="user", attribute="friend")}
        check("David" in friends and "Dave" not in friends, "targeted correction replaces only the named value")
        check("Sam" in friends, "other values in a list-valued attribute are untouched")

        bad = await mem.correct_fact("user", "", "x")
        check(bad["ok"] is False, "correction rejects missing fields")

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


asyncio.run(main())
