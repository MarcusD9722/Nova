"""Write-time contradiction reconciliation.

Singleton attributes already supersede by key, so a changed favourite food
replaces cleanly. The case that leaked is free-form: "I love running" and,
months later, "I hate running" both land as notes with nothing keying them
together, so both sit in memory as equally current and recall surfaces
whichever happens to score higher.

The bias throughout is toward KEEPING memories. Losing something real is much
worse than holding a stale note, so a fact is retired only on an unambiguous
CONTRADICTS verdict, and any failure anywhere degrades to the old behaviour
(just add the new fact).
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Checks, ScriptedLLM

from core.conversation_state import ConversationStateStore
from core.events import MemoryIngestEvent
from core.policy.memory_extractor import MemoryExtractorLLM
from core.policy.summarizer import SummarizerLLM
from core.workers.memory_ingest import MemoryIngestWorker
from memory.backends.diskcache_backend import DiskCacheBackend
from memory.unifier import MemoryUnifier, _content_words

check = Checks()


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


async def build(tmp: Path, judge_reply: str):
    mem = MemoryUnifier(tmp, enable_chroma=False)
    await mem.initialize()
    llm = ScriptedLLM()
    llm.default_reply = judge_reply
    sem = asyncio.Semaphore(1)
    w = MemoryIngestWorker(
        memory=mem, extractor=MemoryExtractorLLM(llm, llm_semaphore=sem),
        summarizer=SummarizerLLM(llm, llm_semaphore=sem),
        state=ConversationStateStore(DiskCacheBackend(tmp / "dc")),
        queue=asyncio.Queue(), summarize_queue=asyncio.Queue(),
    )
    return mem, w, llm


async def test_candidate_narrowing(tmp: Path) -> None:
    check.section("Deterministic narrowing (no model call)")
    check(_content_words("I love running") & _content_words("I hate running") == {"running"},
          "shared substance is found past the stopwords")
    check(not (_content_words("I love running") & _content_words("my hobby is woodworking")),
          "unrelated statements share nothing")

    mem = MemoryUnifier(tmp / "cand", enable_chroma=False)
    await mem.initialize()
    await mem.add_fact(entity="note", attribute="general", value="Marcus loves running in the mornings", confidence=0.9)
    await mem.add_fact(entity="note", attribute="general", value="Marcus's favourite drink is cold brew", confidence=0.9)
    await mem.add_fact(entity="note", attribute="hobbies", value="Marcus enjoys woodworking", confidence=0.9)

    got = await mem.find_conflict_candidates(entity="note", value="Marcus hates running now")
    vals = [c.value for c in got]
    check(len(vals) == 1 and "running" in vals[0], f"only the running note is a candidate ({vals})")

    got = await mem.find_conflict_candidates(entity="note", value="Marcus is learning the guitar")
    check(got == [], "a genuinely new subject has no candidates — so no model call at all")

    got = await mem.find_conflict_candidates(entity="note", value="")
    check(got == [], "an empty value never matches everything")


async def test_contradiction_retires_the_old(tmp: Path) -> None:
    check.section("A contradiction retires the outdated belief")
    mem, w, llm = await build(tmp / "c1", '{"verdicts":[{"n":1,"verdict":"CONTRADICTS"}]}')
    await mem.add_fact(entity="note", attribute="general", value="Marcus loves running in the mornings", confidence=0.9)

    await w._reconcile(entity="note", attribute="general", value="Marcus hates running now")
    await mem.add_fact(entity="note", attribute="general", value="Marcus hates running now", confidence=0.9)

    notes = [f.value for f in await mem.get_facts(entity="note", limit=10)]
    check(not any("loves running" in v for v in notes), f"the outdated belief is gone ({notes})")
    check(any("hates running" in v for v in notes), "the new one is kept")

    check.section("Everything else is left alone")
    for verdict in ("REFINES", "DUPLICATE", "UNRELATED"):
        mem2, w2, _ = await build(tmp / f"c-{verdict}", '{"verdicts":[{"n":1,"verdict":"%s"}]}' % verdict)
        await mem2.add_fact(entity="note", attribute="general", value="Marcus loves running in the mornings", confidence=0.9)
        await w2._reconcile(entity="note", attribute="general", value="Marcus loves running on trails")
        kept = [f.value for f in await mem2.get_facts(entity="note", limit=10)]
        check(any("mornings" in v for v in kept), f"{verdict} keeps the existing fact")


async def test_failures_degrade_safely(tmp: Path) -> None:
    check.section("Any failure degrades to 'just add the new fact'")
    for label, reply in [
        ("unparseable", "I think they contradict"),
        ("wrong shape", '{"verdicts": "nope"}'),
        ("bad index", '{"verdicts":[{"n":99,"verdict":"CONTRADICTS"}]}'),
        ("empty", ""),
    ]:
        mem, w, _ = await build(tmp / f"f-{label[:5]}", reply)
        await mem.add_fact(entity="note", attribute="general", value="Marcus loves running in the mornings", confidence=0.9)
        await w._reconcile(entity="note", attribute="general", value="Marcus hates running now")
        kept = [f.value for f in await mem.get_facts(entity="note", limit=10)]
        check(any("loves running" in v for v in kept), f"{label}: nothing is retired on a bad verdict")

    mem, w, _ = await build(tmp / "f-raise", '{"verdicts":[]}')
    await mem.add_fact(entity="note", attribute="general", value="Marcus loves running in the mornings", confidence=0.9)

    async def _boom(*_a, **_kw):
        raise RuntimeError("model down")

    w._llm_judge = _boom
    await w._reconcile(entity="note", attribute="general", value="Marcus hates running now")
    kept = [f.value for f in await mem.get_facts(entity="note", limit=10)]
    check(any("loves running" in v for v in kept), "a model outage retires nothing and raises nothing")


async def test_end_to_end(tmp: Path) -> None:
    check.section("End to end through the ingest worker")
    facts = '{"facts":[{"entity":"user","attribute":"hobby","value":"running","confidence":0.9,"persist":true}]}'
    mem, w, llm = await build(tmp / "e2e", facts)
    await mem.add_fact(entity="user", attribute="hobby", value="running every morning", confidence=0.9)

    # Extraction first, then the judge — same scripted model, so route by prompt.
    llm.when("Decide whether it CONTRADICTS", '{"verdicts":[{"n":1,"verdict":"CONTRADICTS"}]}')
    llm.default_reply = facts

    w.start()
    await w._q.put(MemoryIngestEvent(conversation_id=uuid4(), user_message="I gave up running",
                                     assistant_message="ok", timestamp=_now(), policy_memory_facts=[]))
    await asyncio.wait_for(w._q.join(), timeout=15)
    await w.stop()

    hobbies = [f.value for f in await mem.get_facts(entity="user", attribute="hobby", limit=10)]
    check("running" in hobbies, f"the new fact was written ({hobbies})")
    check(not any("every morning" in v for v in hobbies),
          f"the contradicted one was retired on the way in ({hobbies})")

    check.section("Singletons skip the model entirely")
    mem2, w2, llm2 = await build(tmp / "sing", '{"verdicts":[]}')
    await mem2.add_fact(entity="user", attribute="favorite_food", value="sushi", confidence=0.9)
    llm2.reset_calls()
    check(mem2._is_singleton_fact("user", "favorite_food") is True, "favorite_food is singleton")
    check(mem2._is_singleton_fact("note", "general") is False, "a free-form note is not")


async def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        await test_candidate_narrowing(tmp)
        await test_contradiction_retires_the_old(tmp)
        await test_failures_degrade_safely(tmp)
        await test_end_to_end(tmp)
    check.finish()


asyncio.run(main())
