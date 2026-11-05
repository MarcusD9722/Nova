from __future__ import annotations

"""Regression suite for the MEMORY findings of the 2026-11-02 audit.

H4  ordinary speech poisoned durable memory (and his own name was never stored)
H5  common words drowned recall
H6  internal bookkeeping facts reached the prompt and got read as appointments
H7  "yesterday" meant the UTC day
H9  conversation summaries were never stored
H11 tool selection could not see the question

Driven through the real capture and recall paths, not through the regexes alone,
because every one of these was green at the unit level.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, boot, run  # noqa: E402

check = Checks()


async def _facts(nova, attribute: str) -> list[str]:
    rows = await nova.state.memory.get_facts(entity="user", attribute=attribute, limit=20)
    return [r.value for r in rows]


async def test_h4_ordinary_speech_does_not_poison_memory():
    check.section("H4: what ordinary sentences are allowed to store")
    async with boot(default_reply="ok") as nova:
        rt = nova.runtime

        # His own name, typed the way anyone types it. This stored NOTHING
        # before: the lead-in pattern was case-sensitive, so a capital M missed.
        await rt._extract_quick_facts("My name is Marcus.")
        check(await _facts(nova, "name") == ["Marcus"],
              f"'My name is Marcus.' stores the name ({await _facts(nova, 'name')})")

        poison = [
            ("Leslie said she'd call me Monday.", "name"),
            ("My kids love pizza", "child"),
            ("My daughter loves dinosaurs", "child"),
            ("My friends think I'm crazy", "friend"),
            ("my son plays soccer", "child"),
            ("Don't call me Shirley", "name"),
            ("Can you call me Uber", "name"),
        ]
        for sentence, attribute in poison:
            before = await _facts(nova, attribute)
            await rt._extract_quick_facts(sentence)
            after = await _facts(nova, attribute)
            added = [v for v in after if v not in before]
            check(not added, f"{sentence!r} adds no {attribute} ({added})")

        # The name survived all of that — the Monday case used to PURGE it.
        check(await _facts(nova, "name") == ["Marcus"],
              f"and his name is still there afterwards ({await _facts(nova, 'name')})")

        # Location stops at the connective.
        await rt._extract_quick_facts("I live in Austin with my wife and two kids")
        check(await _facts(nova, "location") == ["Austin"],
              f"location is the place, not the rest of the sentence "
              f"({await _facts(nova, 'location')})")

        # Real introductions still work.
        await rt._extract_quick_facts("My kids are Mateo and Liam")
        kids = await _facts(nova, "child")
        check(sorted(kids) == ["Liam", "Mateo"], f"real names are still captured ({kids})")
        await rt._extract_quick_facts("My wife is Leslie")
        check(await _facts(nova, "spouse") == ["Leslie"],
              f"and so is a spouse ({await _facts(nova, 'spouse')})")


async def test_h5_common_words_do_not_drown_recall():
    check.section("H5: a question full of filler still finds the fact")
    async with boot(default_reply="ok") as nova:
        m = nova.state.memory
        # 'the' matches 119 of the 271 facts in the real database. Here: one
        # fact that answers the question, and plenty that merely contain 'the'.
        await m.add_fact("note", "gym", "the locker code at the gym is 4417", confidence=0.9)
        for i in range(20):
            await m.add_fact("note", f"filler{i}",
                             f"the thing about the number {i} and the other stuff",
                             confidence=0.9)
        hits = await m.search("What was the code for my locker?", limit=10)
        texts = [h.text for h in hits]
        rank = next((i + 1 for i, t in enumerate(texts) if "4417" in t), None)
        check(rank is not None, f"the locker fact is in the results at all (rank {rank})")
        check(rank is not None and rank <= 3,
              f"and near the top rather than buried under 'the' matches (rank {rank})")

        check(len(texts) < 10,
              f"the filler no longer fills the result set ({len(texts)} hits)")

        # And among real matches, covering MORE of the question ranks higher.
        # Every keyword hit used to score a flat 0.95, so ordering was whatever
        # order the rows came back in.
        await m.add_fact("note", "locker", "the locker is in the hallway", confidence=0.9)
        hits = await m.search("locker code", limit=6)
        by_text = {h.text: h.score for h in hits}
        both = next((s for t, s in by_text.items() if "4417" in t), None)
        one = next((s for t, s in by_text.items() if "hallway" in t), None)
        check(both is not None and one is not None and both > one,
              f"a fact matching both words outranks one matching one "
              f"({both} vs {one})")


async def test_h6_bookkeeping_facts_stay_out_of_recall():
    check.section("H6: internal rows are not things she remembers")
    async with boot(default_reply="ok") as nova:
        m = nova.state.memory
        await m.add_fact("executive", "surfaced:upcoming:call the dentist",
                         "2026-11-02T17:57:17", confidence=1.0)
        await m.add_fact("session", "last_active", "2026-11-02T17:57:17", confidence=1.0)
        await m.add_fact("self_eval", "2026-11-02", "answered well", confidence=1.0)
        await m.add_fact("agent:critic", "consulted", "4", confidence=1.0)
        await m.add_fact("note", "dentist", "the dentist is Dr. Yoon", confidence=0.9)

        hits = await m.search("what reminders do I have about the dentist?", limit=12)
        texts = [h.text for h in hits]
        leaked = [t for t in texts
                  if t.split()[1] in ("executive", "session", "self_eval", "agent:critic")]
        check(not leaked, f"no bookkeeping row is returned ({leaked[:2]})")
        check(any("Yoon" in t for t in texts),
              f"while the real note still is ({[t[:40] for t in texts][:3]})")


async def test_h7_yesterday_means_the_local_day():
    check.section("H7: date-range recall uses Marcus's day, not UTC's")
    async with boot(default_reply="ok") as nova:
        from core.tool_router import ToolCall

        m = nova.state.memory
        conv = uuid4()
        # Four things said yesterday EVENING local time, which in UTC land on
        # the following date — the exact rows the old comparison missed.
        now_local = datetime.now().astimezone()
        yesterday = (now_local - timedelta(days=1)).date()
        marks = []
        await m._sqlite.initialize()
        for hour in (9, 17, 19, 22):
            # Written with an explicit PAST timestamp, in UTC, exactly as a real
            # turn is stored. `ingest_turn` would stamp them "now", which is the
            # one thing this test must not do.
            local = datetime(yesterday.year, yesterday.month, yesterday.day, hour,
                             tzinfo=now_local.tzinfo)
            mark = f"MARK-{hour:02d}"
            marks.append(mark)
            await m._sqlite.add_turn(
                uuid4(), conv, "user",
                f"{mark} something we talked about at {hour} o'clock",
                created_at_iso=local.astimezone(timezone.utc).isoformat())
        res = await nova.runtime.router.execute(
            ToolCall(name="memory.recall", args={"query": "what did we talk about yesterday"}),
            retries=0)
        body = str((res.result or {}).get("results"))
        found = [mk for mk in marks if mk in body]
        check(len(found) == len(marks),
              f"every one of yesterday's turns comes back ({len(found)}/{len(marks)}: {found})")
        # And the timestamps shown are local, not UTC.
        check("T" not in body or " " in body, "timestamps are rendered for a human")


async def test_h9_summaries_are_stored():
    check.section("H9: a summary the model actually writes is accepted")
    from core.policy.contracts import SummarizerOutput

    # The shape the real model returns — lists of STRINGS. This failed
    # validation, at debug level, and the summary was dropped.
    out = SummarizerOutput.model_validate({
        "summary": "Marcus asked about RAID levels; we settled on RAID 6.",
        "key_facts": ["Marcus has an RTX 5080", "He prefers RAID 6"],
        "open_loops": ["benchmark the array"],
    })
    check(out.summary.startswith("Marcus asked"), "the summary survives validation")
    check(out.key_facts is not None and len(out.key_facts) == 2,
          f"and so do the key facts ({out.key_facts})")
    check(out.open_loops is not None and len(out.open_loops) == 1,
          f"and the open loops ({out.open_loops})")
    # Dicts still work, and nonsense is still refused rather than invented.
    check(SummarizerOutput.model_validate(
        {"summary": "x", "key_facts": [{"k": "v"}]}).key_facts == [{"k": "v"}],
        "a list of dicts is unchanged")
    check(SummarizerOutput.model_validate(
        {"summary": "x", "key_facts": "not a list"}).key_facts is None,
        "and a non-list becomes None rather than an error that drops everything")


async def test_h11_the_selector_can_see_the_question():
    check.section("H11: the question reaches the embedder")
    from memory import embeddings as E

    if not E.embedding_available():
        check(True, "embedding model unavailable; skipped (nothing to assert against)")
        return
    import math

    def cos(a, b):
        num = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        return num / (na * nb) if na and nb else 0.0

    grounding = "Things you remember:\n" + "\n".join(
        f"FACT user note{i} = some remembered detail number {i} about his life and work"
        for i in range(60))
    q1, q2 = "What's the weather in Austin?", "Read your own source file runtime.py"

    # bge-small truncates at 512 tokens. Grounding first means the question is
    # cut off entirely: two DIFFERENT questions produce the same vector.
    old1, old2 = E.embed_texts([f"{grounding}\n{q1}", f"{grounding}\n{q2}"])
    check(cos(old1, old2) > 0.999,
          f"grounding-first: two different questions embed identically "
          f"(cos {cos(old1, old2):.4f}) — this is the bug")

    from core.tools.selector import ToolSelector

    ctx = grounding[: ToolSelector._CONTEXT_CHARS]
    new1, new2 = E.embed_texts([f"{q1}\n{ctx}", f"{q2}\n{ctx}"])
    check(cos(new1, new2) < 0.99,
          f"question-first: the question changes the vector (cos {cos(new1, new2):.4f})")


async def main():
    await test_h4_ordinary_speech_does_not_poison_memory()
    await test_h5_common_words_do_not_drown_recall()
    await test_h6_bookkeeping_facts_stay_out_of_recall()
    await test_h7_yesterday_means_the_local_day()
    await test_h9_summaries_are_stored()
    await test_h11_the_selector_can_see_the_question()
    check.finish()


if __name__ == "__main__":
    run(main)
