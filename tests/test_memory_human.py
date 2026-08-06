"""Human-like memory: retrieval reinforcement and encoding salience.

Two mechanisms borrowed from how human memory actually behaves, both of which
Nova was missing:

  1. THE TESTING EFFECT. Retrieving a memory is what makes it durable — far
     more than restating it. Nova only reinforced a fact when a duplicate was
     WRITTEN, so a fact she recalled daily was no more durable than one never
     mentioned again. Memory was shaped by repetition, not by use.

  2. EMOTIONAL SALIENCE. Nova already scored the emotional tone of every
     message and threw it away for memory purposes, so "Liam took his first
     steps" decayed exactly like "we had pasta". Salience now extends a
     memory's half-life — which is why you remember where you were for the
     big moments.

Both act on DECAY (ranking), never on truth: nothing here deletes a fact or
raises its confidence. Being reminded of something is not evidence that it is
more true, only that it matters more.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Checks

import aiosqlite

from core.mood import emotional_salience
from memory.backends.sqlite_backend import SQLiteMemoryBackend
from memory.unifier import MemoryUnifier, _default_salience, _is_undecayed, _staleness_factor

check = Checks()


async def test_emotional_salience() -> None:
    check.section("Emotional salience from a message")
    check(emotional_salience("we had pasta tonight") == 0.0,
          "an ordinary message marks nothing — most moments aren't memorable")
    check(emotional_salience("") == 0.0, "empty text is not salient")
    check(emotional_salience("I'm so excited, Liam took his first steps!") >= 0.85,
          "a charged positive moment scores high")
    check(emotional_salience("ugh this build is so frustrating") >= 0.75,
          "a charged negative moment scores high too — arousal, not valence")
    check(0.0 < emotional_salience("pretty good day") < 0.4,
          "a mildly pleasant day barely registers")
    check(emotional_salience("I'm exhausted") < emotional_salience("I'm so excited about this"),
          "low-arousal moods mark less than high-arousal ones")


async def test_default_salience() -> None:
    check.section("Encoding strength when the caller doesn't specify")
    check(_default_salience("user", "name", 0.9) == 1.0, "your name is maximally durable")
    check(_default_salience("user", "spouse", 0.9) == 1.0, "so is your spouse")
    check(_default_salience("lesson", "reflection", 0.8) == 0.9, "explicit corrections encode strongly")
    check(_default_salience("note", "general", 0.9) == 0.2, "free-form asides are the most forgettable")
    check(_default_salience("user", "favourite_snack", 0.6) < 1.0,
          "a non-core user attribute is not treated as identity")
    for e, a, c in (("note", "x", 0.5), ("user", "name", 0.9), ("random", "y", 1.0)):
        s = _default_salience(e, a, c)
        check(0.0 <= s <= 1.0, f"salience stays in range for {e}.{a} ({s})")


async def test_decay_shape() -> None:
    check.section("What decays, and how fast")
    for entity in ("user", "lesson", "project:flappy-bird", "session"):
        check(_is_undecayed(entity) is True, f"'{entity}' never decays")
    for entity in ("note", "conversation:abc:digest", "wellbeing", "interest_focus"):
        check(_is_undecayed(entity) is False, f"'{entity}' is subject to decay")

    old = (datetime.now(timezone.utc) - timedelta(days=180)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()

    check(_staleness_factor(fresh, None) > 0.99, "a brand-new memory is undecayed")
    plain_old = _staleness_factor(old, None)
    check(plain_old < 0.90, f"a 180-day-old memory has faded ({plain_old:.3f})")

    # Salience slows decay — the whole point.
    salient_old = _staleness_factor(old, None, salience=1.0)
    check(salient_old > plain_old,
          f"a memory that mattered fades more slowly ({salient_old:.3f} > {plain_old:.3f})")

    # So does having been recalled.
    recalled_old = _staleness_factor(old, None, access_count=20)
    check(recalled_old > plain_old,
          f"a frequently recalled memory fades more slowly ({recalled_old:.3f} > {plain_old:.3f})")

    # Diminishing returns: the 10th recall matters less than the 2nd.
    d1 = _staleness_factor(old, None, access_count=2) - _staleness_factor(old, None, access_count=1)
    d2 = _staleness_factor(old, None, access_count=20) - _staleness_factor(old, None, access_count=19)
    check(d1 > d2, "reinforcement has diminishing returns, as in humans")

    # Recall keeps a memory FRESH, not just stronger.
    check(_staleness_factor(old, None, last_accessed_at=fresh) > 0.99,
          "recalling an old memory makes it recent again")

    check.section("The CH1 invariant must survive")
    # Decay re-ranks; it must never push a real fact under the 0.80
    # high-confidence threshold, or Nova starts hedging about things she knows.
    worst = min(
        0.95 * _staleness_factor((datetime.now(timezone.utc) - timedelta(days=d)).isoformat(), None)
        for d in (30, 180, 365, 3650)
    )
    check(worst >= 0.80, f"even a 10-year-old memory stays above the hedge threshold ({worst:.4f})")


async def test_migration_and_reinforcement(tmp: Path) -> None:
    check.section("Schema migration v4 -> v5")
    db_path = tmp / "m" / "nova.sqlite3"
    sqlite = SQLiteMemoryBackend(db_path)
    await sqlite.initialize()
    version = await sqlite.schema_version()
    check(version >= 5, f"the database migrates to at least v5 (got {version})")

    async with aiosqlite.connect(db_path) as db:
        cols = {r[1] for r in await (await db.execute("PRAGMA table_info(facts)")).fetchall()}
    for col in ("access_count", "last_accessed_at", "salience"):
        check(col in cols, f"facts.{col} exists")

    check.section("touch_facts_accessed records USE, not truth")
    fid = uuid4()
    await sqlite.add_fact(fid, entity="note", attribute="coffee", value="oat milk",
                          confidence=0.8, salience=0.3)
    rows = await sqlite.search_facts("oat milk")
    check(rows and rows[0]["access_count"] == 0, "a new fact starts unrecalled")
    check(abs(float(rows[0]["salience"]) - 0.3) < 1e-6, "salience round-trips")

    before_conf = float(rows[0]["confidence"])
    await sqlite.touch_facts_accessed([str(fid)])
    await sqlite.touch_facts_accessed([str(fid)])
    rows = await sqlite.search_facts("oat milk")
    check(rows[0]["access_count"] == 2, f"each recall counts ({rows[0]['access_count']})")
    check(bool(rows[0]["last_accessed_at"]), "the recall time is stamped")
    check(float(rows[0]["confidence"]) == before_conf,
          "recall does NOT raise confidence — being reminded isn't evidence")

    await sqlite.touch_facts_accessed([])
    await sqlite.touch_facts_accessed(["not-a-real-id"])
    check(True, "empty / unknown ids are a no-op, not a raise")


async def test_recall_reinforces_end_to_end(tmp: Path) -> None:
    check.section("Recall reinforces, through the real search path")
    mem = MemoryUnifier(tmp / "e2e", enable_chroma=False)
    await mem.initialize()

    await mem.add_fact(entity="note", attribute="coffee",
                       value="Marcus likes oat milk in his coffee", confidence=0.9)
    await mem.add_fact(entity="note", attribute="unrelated",
                       value="the shed needs painting", confidence=0.9)

    async def access_count(needle: str) -> int:
        rows = await mem._sqlite.search_facts(needle)
        return int(rows[0]["access_count"]) if rows else -1

    check(await access_count("oat milk") == 0, "starts at zero")

    hits = await mem.search("oat milk coffee")
    check(any("oat milk" in h.text for h in hits), "the fact is recalled")
    check(await access_count("oat milk") == 1, "recalling it reinforced it")
    check(await access_count("shed needs painting") == 0,
          "a fact that did NOT surface is untouched")

    # Cooldown: repeated recall inside one conversation must not inflate it.
    mem._search_gen += 1
    await mem.search("oat milk coffee")
    mem._search_gen += 1
    await mem.search("oat milk coffee")
    check(await access_count("oat milk") == 1,
          "repeat recalls within the cooldown don't stack (the spacing effect)")

    # Past the cooldown, it counts again.
    for k in list(mem._recall_seen):
        mem._recall_seen[k] = datetime.now(timezone.utc) - timedelta(hours=1)
    mem._search_gen += 1
    await mem.search("oat milk coffee")
    check(await access_count("oat milk") == 2, "a later, separate recall does count")

    check.section("Reinforcement never breaks a turn")
    class Broken:
        async def touch_facts_accessed(self, _ids):
            raise RuntimeError("db exploded")

    real = mem._sqlite.touch_facts_accessed
    mem._sqlite.touch_facts_accessed = Broken().touch_facts_accessed
    try:
        mem._search_gen += 1
        hits = await mem.search("oat milk coffee")
        check(bool(hits), "search still returns results when reinforcement fails")
    finally:
        mem._sqlite.touch_facts_accessed = real


async def test_salience_changes_ranking(tmp: Path) -> None:
    """The observable payoff: two equally old memories, one that mattered."""
    check.section("A memory that mattered outranks one that didn't")
    mem = MemoryUnifier(tmp / "rank", enable_chroma=False)
    await mem.initialize()

    await mem.add_fact(entity="note", attribute="a", value="zebra pasta dinner",
                       confidence=0.9, salience=0.0)
    await mem.add_fact(entity="note", attribute="b", value="zebra first steps",
                       confidence=0.9, salience=1.0)

    old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    async with aiosqlite.connect(mem._sqlite._db_path) as db:
        await db.execute("UPDATE facts SET created_at=?, last_reinforced_at=NULL", (old,))
        await db.commit()

    hits = {h.text: h.score for h in await mem.search("zebra")}
    dinner = next((s for t, s in hits.items() if "pasta" in t), None)
    steps = next((s for t, s in hits.items() if "first steps" in t), None)
    check(dinner is not None and steps is not None, "both memories are recalled")
    check(steps > dinner,
          f"after 200 days the moment that mattered ranks higher ({steps:.4f} > {dinner:.4f})")


async def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        await test_emotional_salience()
        await test_default_salience()
        await test_decay_shape()
        await test_migration_and_reinforcement(tmp)
        await test_recall_reinforces_end_to_end(tmp)
        await test_salience_changes_ranking(tmp)
    check.finish()


asyncio.run(main())
