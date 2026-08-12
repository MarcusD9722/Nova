"""Episodic → semantic consolidation: Nova noticing patterns across many days.

This is the first mechanism that writes beliefs Nova INFERRED rather than
facts Marcus stated, so most of these checks are about the guard rails, not
the happy path. A confidently-asserted wrong generalization is far worse than
no generalization at all.

The three invariants:
  * stored as INFERRED, so existing hedging makes her say "I think"
  * every insight carries the dates supporting it — "why do you think that?"
    must be answerable, and unanchored consolidation collapses diverse
    experience into confident mush
  * a fabricated date is rejected, not stored
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

from harness import Checks, ScriptedLLM

from core.workers.self_improve import SelfImproveWorker
from memory.provenance import INFERRED, is_assumption
from memory.unifier import INSIGHT_ENTITY, MemoryUnifier

check = Checks()


async def seed_days(mem: MemoryUnifier, *, days: int) -> list[str]:
    """Write user turns across `days` distinct calendar days."""
    dates = []
    for i in range(days):
        when = datetime.now(timezone.utc) - timedelta(days=i + 1)
        dates.append(when.strftime("%Y-%m-%d"))
        conv = uuid4()
        for n in range(3):
            tid = await mem.ingest_turn(conv, "user", f"day {i} message {n} about the workshop project")
            # backdate the turn so grouping sees separate days
            async with __import__("aiosqlite").connect(mem._dir / "sqlite" / "nova.sqlite3") as db:
                await db.execute("UPDATE turns SET created_at=? WHERE id=?", (when.isoformat(), str(tid)))
                await db.commit()
    return sorted(dates)


async def test_episode_grouping(tmp: Path) -> None:
    check.section("Episodes group by day")
    mem = MemoryUnifier(tmp / "ep", enable_chroma=False)
    await mem.initialize()
    dates = await seed_days(mem, days=5)

    eps = await mem.episodes_for_consolidation(days=30)
    check(len(eps) == 5, f"one entry per distinct day ({len(eps)})")
    check([e["date"] for e in eps] == dates, "days are ordered oldest-first")
    check(all(e.get("weekday") for e in eps), "each day carries its weekday (for 'you work late on Thursdays')")
    check(all(e["messages"] for e in eps), "each day carries its messages")

    conv = uuid4()
    await mem.ingest_turn(conv, "assistant", "This is something Nova said, not Marcus, and it is long enough.")
    eps = await mem.episodes_for_consolidation(days=30)
    joined = " ".join(m for e in eps for m in e["messages"])
    check("something Nova said" not in joined,
          "her OWN replies are excluded — generalizing over her output would drift")

    check(await mem.episodes_for_consolidation(days=0) == [], "a zero-day window yields nothing")


async def test_insight_storage(tmp: Path) -> None:
    check.section("Insights are stored as assumptions, with anchors")
    mem = MemoryUnifier(tmp / "ins", enable_chroma=False)
    await mem.initialize()

    ok = await mem.add_insight("You usually work late on Thursdays.", topic="work-rhythm",
                               evidence_dates=["2026-08-01", "2026-08-08"])
    check(ok is True, "a supported insight is stored")

    rows = await mem.get_facts(entity=INSIGHT_ENTITY, limit=5)
    check(len(rows) == 1, "it lands under the insight entity, not with stated facts")
    r = rows[0]
    check(r.verification_status == INFERRED, f"marked INFERRED, not stated ({r.verification_status})")
    check(is_assumption(r.verification_status) is True, "the hedging layer treats it as an assumption")
    check("2026-08-01" in (r.evidence or "") and "2026-08-08" in (r.evidence or ""),
          f"the supporting dates are retained ({r.evidence})")
    check(r.confidence <= 0.8, f"confidence is capped below a stated fact ({r.confidence})")

    got = await mem.get_insights()
    check(got and got[0]["text"].startswith("You usually work late"), "get_insights returns it")
    check(bool(got[0]["evidence"]), "with its evidence, so she can say why")

    check.section("Unsupported generalizations are refused")
    for label, kwargs in [
        ("no dates", dict(text="You seem stressed lately.", topic="mood", evidence_dates=[])),
        ("empty text", dict(text="", topic="x", evidence_dates=["2026-08-01"])),
        ("no topic", dict(text="A long enough sentence here.", topic="", evidence_dates=["2026-08-01"])),
        ("too short", dict(text="yes", topic="x", evidence_dates=["2026-08-01"])),
    ]:
        check(await mem.add_insight(**kwargs) is False, f"refused: {label}")
    check(len(await mem.get_facts(entity=INSIGHT_ENTITY, limit=10)) == 1, "none of them were stored")

    check.section("One belief per topic — re-derivation replaces")
    await mem.add_insight("You usually work late on Thursdays and Fridays.", topic="work-rhythm",
                          evidence_dates=["2026-08-15", "2026-08-22"])
    rows = await mem.get_facts(entity=INSIGHT_ENTITY, attribute="work-rhythm", limit=10)
    check(len(rows) == 1, f"the topic still holds exactly one belief ({len(rows)})")
    check("Fridays" in rows[0].value, "the newer belief won")
    check("2026-08-22" in (rows[0].evidence or ""), "and brought its fresher evidence")

    await mem.add_insight("You return to the woodworking project on weekends.", topic="weekend-focus",
                          evidence_dates=["2026-08-02", "2026-08-09"])
    check(len(await mem.get_facts(entity=INSIGHT_ENTITY, limit=10)) == 2,
          "a DIFFERENT topic is kept alongside, not superseded")


async def test_worker_rejects_fabricated_dates(tmp: Path) -> None:
    """The model must not be able to invent supporting evidence."""
    check.section("The consolidation pass verifies every cited date")
    mem = MemoryUnifier(tmp / "wk", enable_chroma=False)
    await mem.initialize()
    real = await seed_days(mem, days=6)

    llm = ScriptedLLM()
    llm.default_reply = (
        '{"observations": ['
        f'{{"topic":"workshop","text":"You keep coming back to the workshop project.","dates":["{real[0]}","{real[1]}"]}},'
        '{"topic":"invented","text":"You always go hiking on Sundays.","dates":["1999-01-01","1999-01-02"]},'
        f'{{"topic":"thin","text":"You mentioned the shed once.","dates":["{real[0]}"]}}'
        ']}'
    )
    w = SelfImproveWorker.__new__(SelfImproveWorker)
    w._memory, w._llm, w._sem = mem, llm, asyncio.Semaphore(1)
    await w._consolidate_episodes()

    topics = {r.attribute for r in await mem.get_facts(entity=INSIGHT_ENTITY, limit=10)}
    check("workshop" in topics, "a genuinely supported observation is kept")
    check("invented" not in topics, "an observation citing dates that never happened is REJECTED")
    check("thin" not in topics, "a single-day 'pattern' is rejected (needs >= 2 days)")

    check.section("Too little history means no guessing")
    mem2 = MemoryUnifier(tmp / "wk2", enable_chroma=False)
    await mem2.initialize()
    await seed_days(mem2, days=2)
    w2 = SelfImproveWorker.__new__(SelfImproveWorker)
    w2._memory, w2._llm, w2._sem = mem2, llm, asyncio.Semaphore(1)
    llm.reset_calls()
    await w2._consolidate_episodes()
    check(len(llm.prompts) == 0, "with under 4 days of history the model is never even asked")
    check(len(await mem2.get_facts(entity=INSIGHT_ENTITY, limit=5)) == 0, "and nothing is stored")

    check.section("A malformed model reply is survivable")
    mem3 = MemoryUnifier(tmp / "wk3", enable_chroma=False)
    await mem3.initialize()
    await seed_days(mem3, days=5)
    for reply in ("not json at all", '{"observations": "not a list"}', '{"observations": [{}]}', ""):
        bad = ScriptedLLM()
        bad.default_reply = reply
        w3 = SelfImproveWorker.__new__(SelfImproveWorker)
        w3._memory, w3._llm, w3._sem = mem3, bad, asyncio.Semaphore(1)
        await w3._consolidate_episodes()
    check(len(await mem3.get_facts(entity=INSIGHT_ENTITY, limit=5)) == 0,
          "four malformed replies store nothing and raise nothing")


async def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        await test_episode_grouping(tmp)
        await test_insight_storage(tmp)
        await test_worker_rejects_fabricated_dates(tmp)
    check.finish()


asyncio.run(main())
