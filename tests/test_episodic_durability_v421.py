"""V3 P4.2.1: two edge cases P4.2 left open, both confirmed before being fixed.

P4.2 added a second asynchronous stage to episodic memory:

    producers -> BUS -> promoter queue -> persistence queue -> worker -> SQLite

P4.1's drain covered the second queue. Nothing covered the first.

**Bug 1 — shutdown loss. Confirmed.** `EpisodicPromoter.stop()` set its stop
flag before draining, and `_run` checks that flag at the top of its loop.
Measured on the unwired code: 12 events queued, **1** processed. At system level
it usually survived anyway, because an earlier worker's `stop()` yields long
enough for the promoter to drain by coincidence — which is not a durability
guarantee, and does not hold at all for the five producers that used to stop
*after* the promoter. `MemoryIngestWorker` publishes `memory.superseded` during
its own drain, so the likeliest correction in a shutdown was the one most at
risk.

**Bug 2 — changed choices. Confirmed.** Selection identity is the chosen
artifact, so "WD Gold" then "Seagate" from the same comparison produced two
episodes with `superseded_by IS NULL` — two simultaneously current answers to
"what did I choose?". Measured before the fix: 2 active.

Run:  venv\\Scripts\\python.exe tests\\test_episodic_durability_v421.py
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from harness import Checks, boot, run  # noqa: E402

check = Checks()

DRIVES = [
    {"title": "Seagate Exos X28", "capacity": "28 TB", "price": "$429"},
    {"title": "WD Gold", "capacity": "26 TB", "price": "$399"},
    {"title": "IronWolf Pro", "capacity": "24 TB", "price": "$389"},
]
MONITORS = [
    {"title": "Dell U2723QE", "size": "27 inch"},
    {"title": "LG 27GP950", "size": "27 inch"},
]


def show(nova, conv: str, turn_id: str, query: str, results: list[dict]):
    from memory.artifacts import capture_tool_result
    return capture_tool_result(
        nova.runtime._artifacts, conversation_id=conv, turn_id=turn_id,
        tool="web.search", args={"query": query}, result={"results": results})


async def settle(nova, *, timeout: float = 20.0) -> None:
    w = nova.runtime._episodic_worker
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if w.stats["persisted"] + w.stats["failed"] >= w.stats["queued"]:
            return
        await asyncio.sleep(0.02)


async def all_selections(nova, *, include_superseded: bool = True) -> list:
    """Read selections directly, INCLUDING replaced ones.

    `recent_episodes` filters superseded rows — which is the production
    behaviour being tested — so a test that used it could not tell "marked
    superseded" apart from "deleted".
    """
    import aiosqlite
    from memory.episodes import Episode
    path = nova.runtime._episodes._db_path
    sql = "SELECT * FROM episodes WHERE kind = 'selection'"
    if not include_superseded:
        sql += " AND superseded_by IS NULL"
    async with aiosqlite.connect(str(path)) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql + " ORDER BY created_at ASC") as cur:
            rows = await cur.fetchall()
    return [Episode.from_row(r) for r in rows]


def prompt_containing(nova, needle: str) -> str:
    for p in reversed(nova.llm.prompts):
        if needle.lower() in p.lower():
            return p
    return ""


# ── 1. the promoter drain contract ───────────────────────────────────────────

async def test_promoter_drains_its_queue():
    check.section("promoter.stop() processes what is already queued")
    from core.event_bus import BUS
    async with boot() as nova:
        p = nova.runtime._promoter
        await asyncio.sleep(0.2)          # let the task reach its get()
        p.stats["correction"] = 0

        n = 12
        # Synchronous publishes with no await between them: the consumer task
        # cannot run, so every event is still queued when stop() is called.
        for i in range(n):
            BUS.publish("memory.corrected",
                        {"entity": "user", "attribute": f"attr{i}",
                         "was": "old", "now": f"new{i}"})
        depth = p._queue.qsize() if p._queue else -1
        check(depth == n, f"all {n} events are queued before shutdown ({depth})")

        await p.stop()
        check(p.stats["correction"] == n,
              f"all {n} were promoted during the drain ({p.stats['correction']})")
        check(p.stats["undrained"] == 0, "and nothing was reported undrained")


async def test_drain_is_bounded_and_honest():
    check.section("a drain that cannot finish says so rather than hanging")
    from core.event_bus import BUS
    async with boot() as nova:
        p = nova.runtime._promoter
        await asyncio.sleep(0.2)
        for i in range(25):
            BUS.publish("memory.corrected",
                        {"entity": "user", "attribute": f"b{i}", "was": "x", "now": f"y{i}"})
        q = p._queue
        before = q.qsize()

        # Bounded by COUNT, which is deterministic. The wall-clock budget is the
        # other half of the bound but cannot be asserted on here: this drain
        # takes ~1 ms and `time.monotonic()` on Windows advances in ~15 ms
        # steps, so a zero budget legitimately never trips.
        t0 = time.perf_counter()
        drained = p._drain_remaining(q, max_events=5)
        elapsed = time.perf_counter() - t0

        check(elapsed < 1.0, f"the drain returned promptly ({elapsed * 1000:.0f}ms)")
        check(drained == 5, f"it stopped at the bound ({drained} of {before})")
        check(p.stats["undrained"] == before - 5,
              f"and reported the remainder ({p.stats['undrained']} pending)")
        # It must never block: no join(), so no accounting bug can deadlock it.
        check(q.qsize() == before - 5, "the queue still holds what it could not process")
        await p.stop()


# ── 2. shutdown survival, through a real restart ─────────────────────────────

async def test_correction_burst_survives_shutdown():
    check.section("a burst of corrections published right before shutdown survives")
    from core.event_bus import BUS
    shared = Path(tempfile.mkdtemp(prefix="nova-p421-corr-"))
    n = 15
    try:
        async with boot(env={"NOVA_MEMORY_DIR": str(shared)}) as nova:
            await asyncio.sleep(0.2)
            for i in range(n):
                BUS.publish("memory.corrected",
                            {"entity": "user", "attribute": f"gpu{i}",
                             "was": "3080", "now": f"5080-{i}"})
            # No settle. Shutdown begins immediately.

        async with boot(env={"NOVA_MEMORY_DIR": str(shared)}) as nova:
            eps = await nova.runtime._episodes.recent_episodes(limit=200)
            corr = [e for e in eps if e.kind == "correction"]
            check(len(corr) == n, f"all {n} corrections survived ({len(corr)})")
    finally:
        shutil.rmtree(shared, ignore_errors=True)


async def test_project_and_failure_survive_shutdown():
    check.section("a project completion and a threshold-crossing failure survive")
    from core.event_bus import BUS
    shared = Path(tempfile.mkdtemp(prefix="nova-p421-proj-"))
    try:
        async with boot(env={"NOVA_MEMORY_DIR": str(shared)}) as nova:
            from core.episodic_promoter import FAILURE_RECURRENCE
            await asyncio.sleep(0.2)
            BUS.publish("project.completed",
                        {"project": "countdown", "status": "complete",
                         "summary": "built and tested"})
            # Cross the failure threshold on the very last event before exit.
            for _ in range(FAILURE_RECURRENCE):
                BUS.publish("tool.error",
                            {"tool": "backup.run", "error": "disk quota exceeded"})

        async with boot(env={"NOVA_MEMORY_DIR": str(shared)}) as nova:
            eps = await nova.runtime._episodes.recent_episodes(limit=200)
            proj = [e for e in eps if e.kind == "project_event"]
            fail = [e for e in eps if e.kind == "failure"]
            check(len(proj) == 1, f"the project completion survived ({len(proj)})")
            check(len(fail) == 1, f"the recurring failure survived ({len(fail)})")
    finally:
        shutil.rmtree(shared, ignore_errors=True)


async def test_empty_shutdown_is_fast():
    check.section("nothing pending means shutdown is not slowed down")
    async with boot() as nova:
        p = nova.runtime._promoter
        await asyncio.sleep(0.2)
        check(p._queue is not None and p._queue.empty(), "the queue is empty")
        t0 = time.perf_counter()
        await p.stop()
        elapsed = (time.perf_counter() - t0) * 1000
        check(elapsed < 1500, f"stop() returned in {elapsed:.0f}ms")
        check(p.stats["undrained"] == 0, "with nothing undrained")


async def test_disabled_has_no_drain_work():
    check.section("NOVA_EPISODIC_MEMORY=0 creates no promoter drain work")
    from core.event_bus import BUS
    async with boot(env={"NOVA_EPISODIC_MEMORY": "0"}) as nova:
        p = nova.runtime._promoter
        BUS.publish("memory.corrected", {"entity": "user", "attribute": "x",
                                         "was": "a", "now": "b"})
        await asyncio.sleep(0.2)
        check(p._queue is None, "the promoter never subscribed")
        t0 = time.perf_counter()
        await p.stop()
        check((time.perf_counter() - t0) * 1000 < 500, "and stopping is immediate")
        check(p.stats["correction"] == 0, "nothing was promoted")


# ── 3. changing your mind ────────────────────────────────────────────────────

async def test_changed_choice_supersedes():
    check.section("a second choice from the same set replaces the first")
    shared = Path(tempfile.mkdtemp(prefix="nova-p421-sel-"))
    try:
        async with boot(env={"NOVA_MEMORY_DIR": str(shared)}) as nova:
            conv = "c-change"
            show(nova, conv, "t-d", "28 TB drives", DRIVES)
            await settle(nova)

            await nova.say("Let's go with the WD Gold.", conversation_id=conv)
            await settle(nova)
            await nova.say("Actually, I've changed my mind. Let's use the Seagate Exos X28.",
                           conversation_id=conv)
            await settle(nova)

            every = await all_selections(nova)
            check(len(every) == 2, f"both choices are on record ({len(every)})")
            active = [e for e in every if e.superseded_by is None]
            check(len(active) == 1,
                  f"exactly ONE is current for this result set ({len(active)})")
            if active:
                check("Seagate" in active[0].summary,
                      f"and it is the later one ({active[0].summary[:48]})")
            old = [e for e in every if e.superseded_by is not None]
            check(len(old) == 1 and "WD Gold" in old[0].summary,
                  "the first choice is marked, not deleted")
            if old:
                check(old[0].superseded_by == active[0].id,
                      "and it points at what replaced it")

        # ---- after a restart, through normal chat -------------------------
        async with boot(env={"NOVA_MEMORY_DIR": str(shared)}) as nova:
            nova.llm.reset_calls()
            await nova.say("What drive did I end up choosing?")
            prompt = prompt_containing(nova, "chose")
            check(bool(prompt), "the current choice is retrievable")
            check("Marcus chose Seagate" in prompt, "it names the latest choice")
            # "WD Gold" DOES appear — as one of the options the Seagate was
            # chosen from, which is context, not a competing answer. What must
            # not appear is the replaced DECISION.
            check("Marcus chose WD Gold" not in prompt,
                  "and the replaced decision is not offered alongside it")
            check("SUPERSEDED" not in prompt,
                  "nothing superseded reached an ordinary question")

            nova.llm.reset_calls()
            await nova.say("What drive did I originally choose before I changed my mind?")
            prompt = prompt_containing(nova, "WD Gold")
            check(bool(prompt), "the replaced choice is still recoverable")
            check("SUPERSEDED" in prompt,
                  "and it is labelled so it cannot read as current")
    finally:
        shutil.rmtree(shared, ignore_errors=True)


async def test_unrelated_selections_stay_independent():
    check.section("two different comparisons keep two live choices")
    async with boot() as nova:
        conv = "c-two"
        show(nova, conv, "t-d", "28 TB drives", DRIVES)
        await settle(nova)
        await nova.say("I'll take the WD Gold.", conversation_id=conv)
        await settle(nova)

        show(nova, conv, "t-m", "27 inch monitors", MONITORS)
        await settle(nova)
        await nova.say("I'll take the LG 27GP950.", conversation_id=conv)
        await settle(nova)

        every = await all_selections(nova)
        active = [e for e in every if e.superseded_by is None]
        check(len(every) == 2, f"two selections exist ({len(every)})")
        check(len(active) == 2,
              f"both remain current — different result sets ({len(active)})")
        titles = " ".join(e.summary for e in active)
        check("WD Gold" in titles and "LG 27GP950" in titles,
              "the drive and the monitor are both still chosen")


async def test_same_item_still_deduplicates():
    check.section("saying the same choice again is still one episode, still active")
    async with boot() as nova:
        conv = "c-same"
        show(nova, conv, "t-d", "28 TB drives", DRIVES)
        await settle(nova)

        for phrase in ("Let's go with the WD Gold.",
                       "Yeah, definitely the WD Gold.",
                       "I'll use the WD Gold."):
            await nova.say(phrase, conversation_id=conv)
        await settle(nova)

        every = await all_selections(nova)
        check(len(every) == 1, f"one episode, not three ({len(every)})")
        check(every and every[0].superseded_by is None,
              "and repeating a choice does not supersede it with itself")


async def test_switching_back_restores_the_first():
    check.section("changing your mind back makes the original current again")
    async with boot() as nova:
        conv = "c-back"
        show(nova, conv, "t-d", "28 TB drives", DRIVES)
        await settle(nova)

        await nova.say("Let's go with the WD Gold.", conversation_id=conv)
        await settle(nova)
        await nova.say("Actually, let's use the Seagate Exos X28.", conversation_id=conv)
        await settle(nova)
        await nova.say("No wait — let's go with the WD Gold after all.",
                       conversation_id=conv)
        await settle(nova)

        every = await all_selections(nova)
        active = [e for e in every if e.superseded_by is None]
        check(len(every) == 2, f"still only two decisions on record ({len(every)})")
        check(len(active) == 1, f"one current choice ({len(active)})")
        if active:
            check("WD Gold" in active[0].summary,
                  f"and it is the WD Gold again ({active[0].summary[:48]})")


# ── 4. nothing on the fast path ──────────────────────────────────────────────

async def test_fast_path_untouched():
    check.section("none of this costs an ordinary turn anything")
    async with boot() as nova:
        store = nova.runtime._episodes
        calls = {"n": 0}
        for attr in ("search_episodes", "recent_episodes", "search_decisions",
                     "supersede_selections"):
            real = getattr(store, attr)

            def wrap(_real=real):
                async def _w(*a, **k):
                    calls["n"] += 1
                    return await _real(*a, **k)
                return _w
            setattr(store, attr, wrap())

        conv = "c-fast"
        show(nova, conv, "t-d", "28 TB drives", DRIVES)
        await settle(nova)
        calls["n"] = 0
        queued = nova.runtime._episodic_worker.stats["queued"]

        for text in ("Good morning.", "Thanks!", "What time is it?"):
            await nova.say(text, conversation_id=conv)
        await asyncio.sleep(0.3)

        check(calls["n"] == 0, f"zero episodic database calls ({calls['n']})")
        check(nova.runtime._episodic_worker.stats["queued"] == queued,
              "zero episode writes")
        prompt = nova.llm.prompts[-1] if nova.llm.prompts else ""
        check("From earlier sessions" not in prompt and "SUPERSEDED" not in prompt,
              "zero historical prompt characters")


async def test_supersession_gate():
    check.section("only a question about the earlier choice looks at replaced ones")
    from memory.episodic_recall import wants_superseded

    cases = [
        ("What drive did I originally choose before I changed my mind?", True),
        ("Which one did I pick initially?", True),
        ("What was my first choice?", True),
        ("What did I want at first?", True),
        ("What drive did I end up choosing?", False),
        ("What drive did I choose?", False),
        ("Good morning.", False),
        ("What about the second one?", False),
    ]
    wrong = [q for q, want in cases if wants_superseded(q) != want]
    check(not wrong, f"the gate agrees on all {len(cases)} ({wrong or 'none wrong'})")


async def main():
    await test_promoter_drains_its_queue()
    await test_drain_is_bounded_and_honest()
    await test_correction_burst_survives_shutdown()
    await test_project_and_failure_survive_shutdown()
    await test_empty_shutdown_is_fast()
    await test_disabled_has_no_drain_work()
    await test_changed_choice_supersedes()
    await test_unrelated_selections_stay_independent()
    await test_same_item_still_deduplicates()
    await test_switching_back_restores_the_first()
    await test_fast_path_untouched()
    await test_supersession_gate()
    check.finish()


if __name__ == "__main__":
    run(main)
