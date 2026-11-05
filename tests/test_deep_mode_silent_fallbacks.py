"""Deep mode's two silent fallbacks, and why they stopped being silent.

THIRD consequence of the M10 think-leak chain, and the one with teeth. Deep
mode (`core/orchestrator/deep_mode.py`, Phase 2.3) is Planner -> Executor ->
Critic, strictly opt-in: the user asks for it with "be thorough", "think
carefully", "take your time". Both bookends call `chat()` at
`max_tokens=400, thinking=True` — the tightest reasoning budget in the
codebase — and both degrade to a no-op on an empty reply:

    plan()      -> ""         (deep mode proceeds with no plan)
    critique()  -> "approve"  (the draft ships unreviewed)

`chat()` had no escalation: three IDENTICAL 400-token attempts. Measured on
real weights by driving the REAL DeepPipeline, pre-fix policy vs post-fix,
same prompts:

                                        pre-fix          post-fix
    plan, 3 asks                        EMPTY 3/3        3/3 real plans
    critique, honest draft              approve          approve
    critique, draft claims a FAILED
      tool succeeded (must be revise)   approve  <-- !   revise + notes

So the review stage approved a draft that lied about a failed tool, on every
turn, after ~21.5s of burning three generations — and said nothing anywhere.
The escalation (budget -> 2x budget -> reasoning off) fixed the cause and costs
nothing: post-fix the critic is FASTER, because attempt 2 succeeds instead of a
third full attempt being wasted.

The cause is fixed in `core/llm_runtime.py` and pinned by
tests/test_think_block_leak.py. What THIS suite pins is the part that let it
hide for so long: the module imported a `logger` and never called it once, so
neither fallback left a trace. Specifically —

  1. an empty plan and an explicit DIRECT both return "", and they are
     OPPOSITE events ("the planner said nothing" vs "the planner judged no
     plan necessary"); they must be distinguishable in the record;
  2. a default approval — one the critic never actually gave — must be
     distinguishable from an approval it did give;
  3. and none of this may change a control-flow decision. A flaky critic still
     must not block a reply. The draft still ships; it ships recorded.

Run:  venv\\Scripts\\python.exe tests\\test_deep_mode_silent_fallbacks.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, run  # noqa: E402

check = Checks()

CATALOG = "- weather.get(city): current conditions"

#: What the real model actually returned for the lying draft, in shape: a
#: verdict plus notes naming the contradiction. Pinned so the parse path is
#: exercised on a realistic payload and not only on a tidy one.
REAL_REVISE = (
    '{"verdict": "revise", "notes": "The tool call failed (connection '
    'refused), but the draft presents specific weather details as fact."}')


class _Recorder:
    """Stands in for the module logger and remembers what it was told.

    Deliberately NOT structlog's capture_logs: this project configures a
    filtering bound logger from NOVA_LOG_LEVEL, so a captured `info` can be
    dropped before any processor sees it and the test would pass vacuously.
    Replacing the module's own reference cannot be filtered.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def _record(self, level: str):
        def fn(event: str, **kw: Any) -> None:
            self.events.append((level, event, kw))
        return fn

    def __getattr__(self, level: str):
        return self._record(level)

    def names(self) -> list[str]:
        return [e for _lvl, e, _kw in self.events]

    def find(self, event: str) -> dict[str, Any] | None:
        for _lvl, name, kw in self.events:
            if name == event:
                return kw
        return None

    def level_of(self, event: str) -> str | None:
        for lvl, name, _kw in self.events:
            if name == event:
                return lvl
        return None


class _Runtime:
    """The one method DeepPipeline uses, with a scripted reply."""

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages: list[dict[str, Any]], max_tokens: int = 512,
                   temperature: float = 0.7, thinking: bool = False,
                   **kw: Any) -> str:
        self.calls.append({"max_tokens": max_tokens, "thinking": thinking,
                           "temperature": temperature})
        return self._reply


def _pipeline(reply: str):
    """A real DeepPipeline over a scripted runtime, plus a log recorder."""
    from core.orchestrator import deep_mode as dm
    from core.orchestrator.model_router import ModelRouter

    rt = _Runtime(reply)
    pipe = dm.DeepPipeline(ModelRouter.single(rt, asyncio.Semaphore(1)))
    rec = _Recorder()
    return dm, pipe, rt, rec


async def _plan(reply: str) -> tuple[str, _Recorder, _Runtime]:
    dm, pipe, rt, rec = _pipeline(reply)
    original = dm.logger
    dm.logger = rec
    try:
        out = await pipe.plan(user_text="back up my photos and check space",
                             grounding="(none)", tool_catalog=CATALOG)
    finally:
        dm.logger = original
    return out, rec, rt


async def _critique(reply: str) -> tuple[tuple[str, str], _Recorder, _Runtime]:
    dm, pipe, rt, rec = _pipeline(reply)
    original = dm.logger
    dm.logger = rec
    try:
        out = await pipe.critique(
            user_text="What's the weather in Austin?",
            plan="1. Call weather.get for Austin. 2. Report it.",
            draft="It is currently 71°F and clear in Austin.",
            tool_results=[{"tool": "weather.get", "ok": False,
                           "error": "connection refused"}])
    finally:
        dm.logger = original
    return out, rec, rt


# ---------------------------------------------------------------- the planner

async def test_the_budget_under_test_is_the_one_production_uses() -> None:
    """If this drifts, every measurement in the docstring is about nothing."""
    check.section("the call under test is production's")
    _out, _rec, rt = await _plan("1. Do a thing.")
    check(len(rt.calls) == 1, f"the planner makes one call (got {len(rt.calls)})")
    call = rt.calls[0]
    check(call["max_tokens"] == 400,
          f"at the 400-token budget that failed live (got {call['max_tokens']})")
    check(call["thinking"] is True,
          f"with reasoning ON (got {call['thinking']!r})")

    _out2, _rec2, rt2 = await _critique(REAL_REVISE)
    check(rt2.calls and rt2.calls[0]["max_tokens"] == 400,
          "the critic uses the same 400-token budget")
    check(rt2.calls and rt2.calls[0]["thinking"] is True,
          "and also asks for reasoning")


async def test_an_empty_plan_is_not_recorded_as_a_deliberate_direct() -> None:
    """The pre-fix reality: empty 3/3, and nothing said so."""
    check.section("an empty plan is recorded as empty")
    out, rec, _rt = await _plan("")
    check(out == "", f"still returns no plan, as before (got {out!r})")
    kw = rec.find("deep_plan_empty")
    check(kw is not None,
          f"the empty planner is recorded (events: {rec.names()})")
    check(rec.level_of("deep_plan_empty") == "warning",
          f"as a warning, not a whisper (got {rec.level_of('deep_plan_empty')!r})")
    check(rec.find("deep_plan_direct") is None,
          "and NOT as a deliberate DIRECT, which is the opposite event")


async def test_a_direct_plan_is_recorded_as_direct() -> None:
    check.section("DIRECT is recorded as DIRECT")
    out, rec, _rt = await _plan("DIRECT")
    check(out == "", f"returns no plan, as before (got {out!r})")
    check(rec.find("deep_plan_direct") is not None,
          f"the deliberate choice is recorded (events: {rec.names()})")
    check(rec.find("deep_plan_empty") is None,
          "and never confused with the planner having failed")


async def test_the_two_zero_plan_outcomes_are_distinguishable() -> None:
    """The whole point: both return "", so only the record can tell them apart."""
    check.section("empty and DIRECT are told apart")
    empty_out, empty_rec, _a = await _plan("")
    direct_out, direct_rec, _b = await _plan("DIRECT")
    check(empty_out == direct_out == "",
          "both outcomes are an empty string, as production expects")
    check(empty_rec.names() != direct_rec.names(),
          f"but the records differ: {empty_rec.names()} vs {direct_rec.names()}")


async def test_a_real_plan_is_returned_and_nothing_is_flagged() -> None:
    check.section("a working planner is left alone")
    plan = "1. Call weather.get(\"Austin\").\n2. Report the conditions."
    out, rec, _rt = await _plan(plan)
    check(out == plan, f"returned verbatim (got {out!r})")
    check(not any(n.startswith("deep_plan") for n in rec.names()),
          f"and no degradation is recorded (events: {rec.names()})")


async def test_whitespace_only_is_empty_not_a_plan() -> None:
    check.section("a blank-looking plan is empty")
    out, rec, _rt = await _plan("   \n\t  ")
    check(out == "", f"no plan (got {out!r})")
    check(rec.find("deep_plan_empty") is not None,
          "and it is recorded as the planner saying nothing")


# ----------------------------------------------------------------- the critic

async def test_an_unparseable_critique_still_approves_but_says_so() -> None:
    """The measured pre-fix failure, now visible instead of silent."""
    check.section("a default approval is recorded as one")
    (verdict, notes), rec, _rt = await _critique("")
    check(verdict == "approve",
          f"a flaky critic STILL must not block the reply (got {verdict!r})")
    check(notes == "", f"with no notes (got {notes!r})")
    kw = rec.find("deep_critique_unparsed")
    check(kw is not None,
          f"but the rubber stamp is recorded (events: {rec.names()})")
    check(rec.level_of("deep_critique_unparsed") == "warning",
          "as a warning")
    check(kw is not None and kw.get("raw_empty") is True,
          f"and it says the reply was empty, which is the live failure (got "
          f"{None if kw is None else kw.get('raw_empty')!r})")


async def test_an_explicit_approval_is_not_flagged() -> None:
    check.section("a real approval is not a rubber stamp")
    (verdict, notes), rec, _rt = await _critique('{"verdict": "approve"}')
    check(verdict == "approve", f"approved (got {verdict!r})")
    check(notes == "", "with no notes")
    check(rec.find("deep_critique_unparsed") is None,
          f"and NOT flagged, or the warning means nothing (events: "
          f"{rec.names()})")


async def test_a_revise_verdict_survives_untouched() -> None:
    """The verdict the live run produced for a draft that lied."""
    check.section("revise passes through")
    (verdict, notes), rec, _rt = await _critique(REAL_REVISE)
    check(verdict == "revise", f"revise (got {verdict!r})")
    check("connection refused" in notes,
          f"with the notes that name the contradiction (got {notes[:70]!r})")
    check(rec.find("deep_critique_unparsed") is None,
          "and nothing is flagged, because the critic answered")


async def test_a_verdict_that_is_neither_is_flagged_and_still_approves() -> None:
    check.section("an unknown verdict is flagged")
    (verdict, _notes), rec, _rt = await _critique('{"verdict": "maybe?"}')
    check(verdict == "approve",
          f"anything unparseable still approves, by design (got {verdict!r})")
    kw = rec.find("deep_critique_unparsed")
    check(kw is not None, f"and is recorded (events: {rec.names()})")
    check(kw is not None and kw.get("raw_empty") is False,
          "noting that the reply was NOT empty, so this is a different fault")
    check(kw is not None and kw.get("verdict") == "maybe?",
          f"and carrying what the critic actually said (got "
          f"{None if kw is None else kw.get('verdict')!r})")


async def test_reasoning_around_the_json_is_still_read() -> None:
    """A critic that thinks out loud before the JSON is not a broken critic."""
    check.section("prose around the verdict is tolerated")
    (verdict, notes), rec, _rt = await _critique(
        'The draft states a temperature but the tool failed.\n' + REAL_REVISE)
    check(verdict == "revise", f"the verdict is still found (got {verdict!r})")
    check("connection refused" in notes, "with its notes")
    check(rec.find("deep_critique_unparsed") is None, "and nothing is flagged")


async def main() -> None:
    await test_the_budget_under_test_is_the_one_production_uses()
    await test_an_empty_plan_is_not_recorded_as_a_deliberate_direct()
    await test_a_direct_plan_is_recorded_as_direct()
    await test_the_two_zero_plan_outcomes_are_distinguishable()
    await test_a_real_plan_is_returned_and_nothing_is_flagged()
    await test_whitespace_only_is_empty_not_a_plan()
    await test_an_unparseable_critique_still_approves_but_says_so()
    await test_an_explicit_approval_is_not_flagged()
    await test_a_revise_verdict_survives_untouched()
    await test_a_verdict_that_is_neither_is_flagged_and_still_approves()
    await test_reasoning_around_the_json_is_still_read()
    check.finish()


if __name__ == "__main__":
    run(main)
