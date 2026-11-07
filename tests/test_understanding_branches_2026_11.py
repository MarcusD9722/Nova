"""Branches of Nova's understanding that no suite had taken.

Test program layer 2, item 3 (docs/TEST_PROGRAM.md). Layer 1's map found the
judgement code mostly run but with branches never taken: empty input, a model
that answers in an unexpected shape, a budget that runs out, a reference that
resolves to nothing, an episode another speaker owns. Each is tested here for
what it is MEANT to do, from its docstring:

  intent           an empty message is not a question; stripping stacked
                   openers is bounded;
  understanding    every model judgement falls back when the model is not
                   there, or answers with something it was not offered;
  recall gate      empty or content-free input does not trigger a search;
  episodic recall  empty input is not historical; an episode with no owner is
                   the owner's; the budget is a budget; a reference to a past
                   result set resolves, reports ambiguity, or says why not.

One branch hid a bug, pinned here: when a question had no usable terms,
`retrieve` fell back to recent episodes as its comment promises -- and then
scored each one by term overlap, which is 0 with no terms, and dropped them
all. The fallback could never return anything.

Run:  venv\\Scripts\\python.exe tests\\test_understanding_branches_2026_11.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, run  # noqa: E402

check = Checks()


def _guest(pid: str = "p-alice", name: str = "Alice"):
    from core.speaker.matcher import SpeakerMatch
    from core.turn_identity import TurnIdentity

    class _P:
        role = "guest"
    return TurnIdentity.from_match(
        SpeakerMatch(status="known", profile_id=pid, display_name=name, attempted=True),
        profile=_P())


# ── intent ──────────────────────────────────────────────────────────────────

def test_intent() -> None:
    check.section("intent: empty input, and stacked openers")
    from core.intent import is_question, strip_preamble

    check(is_question("") is False and is_question("   ") is False,
          "an empty message is not a question")
    check(strip_preamble("So, Nova, I meant what time is it") == "what time is it",
          "stacked openers are stripped to the real sentence")
    many = "so so so so so so so so what time is it"
    out = strip_preamble(many)
    check(out.endswith("what time is it") and out.startswith("so"),
          f"the stripping is bounded: eight openers lose six, never loop ({out!r})")
    check(strip_preamble("so") == "so", "a message that is ONLY an opener is kept, not emptied")


# ── understanding ───────────────────────────────────────────────────────────

class _LLM:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0

    async def chat(self, *_a: Any, **_kw: Any) -> str:
        self.calls += 1
        return self.reply


async def test_understanding() -> None:
    check.section("Understanding: never worse than its fallback")
    from core.understanding import Understanding

    labels = ["low", "some", "elevated"]
    u = Understanding(_LLM("elevated"))
    check(await u.classify("rough week", labels=labels, fallback="low") == "elevated",
          "a bare label with no JSON around it is accepted")
    u = Understanding(_LLM('{"label": "panicked"}'))
    check(await u.classify("rough week", labels=labels, fallback="low") == "low",
          "a label it was not offered falls back")

    absent = Understanding(None)
    llm = _LLM('{"ids": ["a"]}')
    present = Understanding(llm)
    check(await absent.rank("pick", options={"a": "A"}, fallback=["z"]) == ["z"],
          "rank with no model: the fallback")
    check(await present.rank("pick", options={}, fallback=["z"]) == ["z"]
          and await present.rank("   ", options={"a": "A"}, fallback=["z"]) == ["z"],
          "rank with nothing to choose from, or nothing asked: the fallback")
    check(llm.calls == 0, "and the model is not even asked")

    check(await absent.expand_query("where does mom live", fallback=["mom"]) == ["mom"],
          "expand_query with no model: the caller's terms")
    u = Understanding(_LLM('{"terms": ["Mom", "mom", "a", "mother", "Mother!"]}'))
    got = await u.expand_query("where does mom live", fallback=["live"])
    check(got == ["live", "mom", "mother"],
          f"duplicates and one-letter terms are dropped, the caller's terms kept first ({got})")

    check(await absent.extract("I'm in Dallas", fields={"city": "the city"}, fallback={"x": 1}) == {"x": 1},
          "extract with no model: the fallback")
    eager = _LLM('{"city": "Dallas"}')
    check(await Understanding(eager).extract("   ", fields={"city": "c"}) == {} and eager.calls == 0,
          "extract with nothing to read: the fallback, and the model is not asked to invent")


# ── the fact-recall gate ────────────────────────────────────────────────────

def test_recall_gate() -> None:
    check.section("recall gate: nothing to search for is not a search")
    from memory.recall_gate import should_recall

    d = should_recall("")
    check(d.recall is False and d.reason == "empty query", f"empty: no recall ({d.reason})")
    d = should_recall("   ")
    check(d.recall is False and d.reason == "empty query", "whitespace: no recall")
    d = should_recall("?!...")
    check(d.recall is False and d.signals.get("rule") == "no_content",
          f"punctuation only: no recall ({d.reason})")
    d = should_recall("Как зовут мою сестру?")
    check(d.recall is True and d.signals.get("rule") == "untokenisable",
          "a script the tokeniser cannot read recalls, to be safe")


# ── episodic recall ─────────────────────────────────────────────────────────

async def _store(root: Path):
    from memory.backends.sqlite_backend import SQLiteMemoryBackend
    from memory.episodes import EpisodicStore
    db = root / "memory.db"
    await SQLiteMemoryBackend(db).initialize()
    return EpisodicStore(db)


def _episode(eid: str, summary: str, *, created: str, **kw: Any):
    from memory.episodes import Episode
    return Episode(id=eid, kind="tool_result", summary=summary, created_at=created, **kw)


async def test_episodic() -> None:
    check.section("episodic recall: empty input, ownership, scoring, budget")
    from core.turn_identity import active_turn
    from memory.episodic_recall import (
        EPISODE_SYSTEM_SCOPE,
        _may_read_episode,
        describe_decision,
        describe_episode,
        needs_episodic_memory,
        retrieve,
        retrieve_decisions,
        score_episode,
    )
    from memory.episodes import Decision

    d = needs_episodic_memory("")
    check(d.search is False and not d and d.reason == "empty query",
          "an empty message does not look at history")

    alice = _guest()
    unowned = _episode("e0", "a hand-built row", created="2026-11-01T10:00:00+00:00", privacy_scope="")
    system = _episode("e1", "an impersonal row", created="2026-11-01T10:00:00+00:00",
                      privacy_scope=EPISODE_SYSTEM_SCOPE)
    hers = _episode("e2", "her row", created="2026-11-01T10:00:00+00:00",
                    privacy_scope=str(alice.memory_entity))
    check(_may_read_episode(unowned) is True, "the owner reads an episode with no owner")
    with active_turn(alice):
        check(_may_read_episode(unowned) is False,
              "a guest does NOT: an episode with no owner is the owner's")
        check(_may_read_episode(system) is True and _may_read_episode(hers) is True,
              "a guest reads impersonal episodes and her own")

    ep = _episode("e3", "compared three 28 TB drives", created="2026-11-01T10:00:00+00:00")
    check(score_episode(ep, set()) == 0.0, "no query terms: no score")
    check(score_episode(ep, {"pizza"}) == 0.0, "no overlap: no score")
    from memory.episodic_recall import _tokens
    check(score_episode(ep, _tokens("which drives")) > 0.0,
          "overlap, with terms stemmed the way retrieve stems them: a score")

    root = Path(tempfile.mkdtemp(prefix="nova-ub-"))
    try:
        store = await _store(root)
        for i, day in enumerate(("2026-11-01", "2026-11-02", "2026-11-03")):
            await store.record_episode(_episode(f"r{i}", f"looked at garden plan number {i}",
                                                created=f"{day}T10:00:00+00:00"))
        res = await retrieve(store, "so?")
        got = [e.id for e in res.episodes]
        check(got[:1] == ["r2"] and len(got) == 3,
              f"a question with no usable terms falls back to the newest episodes ({got})")
        res = await retrieve(store, "garden plan", char_budget=10)
        check(res.prompt_text == "" and res.chars == 0,
              "a budget too small for one episode renders none")
        res = await retrieve(store, "garden plan")
        check(res.prompt_text.count("garden plan") == 3 and res.chars <= 1200,
              "a budget that fits renders them all")

        blank = _episode("b1", "a search with no named results", created="2026-11-04T10:00:00+00:00",
                         entities=["", "   "])
        none = _episode("b1", "a search with no named results", created="2026-11-04T10:00:00+00:00")
        check(describe_episode(blank) == describe_episode(none),
              "entities that are all blank render exactly as no entities")

        plain = Decision(id="D9", title="Use SQLite", decision="one database file")
        text = describe_decision(plain)
        check(text == "D9: Use SQLite — one database file",
              f"a decision with no constraint, rationale or status renders as one line ({text!r})")
        old = Decision(id="D8", title="Use JSON files", decision="flat files",
                       status="superseded", superseded_by="D9", rationale="simple at first")
        text = describe_decision(old)
        check("[superseded, replaced by D9]" in text and "because: simple at first" in text,
              "a replaced decision says so, and why it was made")
        long = Decision(id="D7", title="Model-specific tuning", decision="x" * 300,
                        constraints="re-measure on a model swap " + "y" * 450,
                        rationale="z" * 400)
        text = describe_decision(long, limit=700)
        check("constraint:" in text and "because:" not in text and len(text) <= 900,
              "under pressure the rationale goes first, never the constraint")
        kept, lines = await retrieve_decisions(_DecisionStore([plain, old]), "database")
        check([d.id for d in kept] == ["D9", "D8"] and "D8" in lines,
              "decisions that fit the budget are all kept")
    finally:
        shutil.rmtree(root, ignore_errors=True)


class _DecisionStore:
    def __init__(self, decisions) -> None:
        self._decisions = decisions

    async def search_decisions(self, query: str, *, limit: int = 5):
        return self._decisions[:limit]


async def test_historical_reference() -> None:
    check.section("a reference into a PAST result set: resolves, is ambiguous, or says why not")
    from memory.artifacts import FRESH_SLOW, ArtifactStore
    from memory.episodic_recall import HistoricalReference, resolve_historical_reference

    root = Path(tempfile.mkdtemp(prefix="nova-uh-"))
    try:
        store = await _store(root)
        hot = ArtifactStore()
        drives = [{"title": "Seagate Exos X28"}, {"title": "WD Gold"}, {"title": "Toshiba MG10"}]
        parent = hot.add_result_set(conversation_id="c1", turn_id="t1", summary="three drives",
                                    items=drives, source_tool="web.search", query="nas drives",
                                    freshness=FRESH_SLOW)
        await store.persist_result_set(parent, hot.items_of(parent.artifact_id))
        real = _episode("h1", "compared three drives", created="2026-11-05T10:00:00+00:00",
                        provenance={"artifact_id": parent.artifact_id})
        empty = _episode("h2", "a search whose results were never stored",
                         created="2026-11-04T10:00:00+00:00",
                         provenance={"artifact_id": "no-such-set"})

        ref = await resolve_historical_reference(store, "the second one",
                                                 ranked=[(1.0, real), (0.5, empty)])
        check(bool(ref) and ref.artifact is not None and not ref.ambiguous and len(ref.items) == 3,
              f"one clearly better result set: the second item of it ({ref.reason})")
        ref = await resolve_historical_reference(store, "the second one",
                                                 ranked=[(1.0, real), (0.9, empty)])
        check(bool(ref) and ref.ambiguous and ref.artifact is None,
              "two comparable sets: ambiguous, and it asks rather than guesses")
        ref = await resolve_historical_reference(store, "the second one", ranked=[(1.0, empty)])
        check(not ref and "no stored children" in ref.reason,
              f"a set with nothing stored says so ({ref.reason})")
        ref = await resolve_historical_reference(store, "the seventh one", ranked=[(1.0, real)])
        check(not ref and ref.items and "did not resolve" in ref.reason,
              f"a position past the end says so ({ref.reason})")
        check(not HistoricalReference() and bool(HistoricalReference(ambiguous=True)),
              "an empty reference is false; an ambiguous one is true, so the caller asks")
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def main() -> None:
    test_intent()
    await test_understanding()
    test_recall_gate()
    await test_episodic()
    await test_historical_reference()
    check.finish()


if __name__ == "__main__":
    run(main)
