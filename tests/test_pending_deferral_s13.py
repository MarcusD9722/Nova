"""A deferral: what "not yet" is, and what it is not.

Split out of test_pending_withdrawal_s13.py so each subject gets its own
process — the harness watchdog kills a suite at 180s and reports nothing at
all, so headroom is part of being diagnosable.

TWO PROPERTIES, and they fail in opposite directions.

WHERE the qualifier binds. Searching a message for "later" or "eventually"
called these deferrals:

    "Don't fix the bug that happens later in the level."
    "Don't change the animation that appears later."

The temporal word describes the OBJECT. Reading them as deferrals stored a ban
as pending work that a later "Go ahead." would carry out. The qualifier has to
modify the PROHIBITION: follow the negated verb across a short object and sit
at a clause boundary.

WHAT a deferral contains. It answers "not now"; it does not answer "what".
"Don't build it yet." on its own replaced a real pending proposal, and with
nothing pending it created an executable one out of a sentence that proposes
nothing. So a deferral only becomes a proposal when positive change intent
survives stripping the deferral clause.

Run:  venv\\Scripts\\python.exe tests\\test_pending_deferral_s13.py
"""



from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, boot, run  # noqa: E402

from core.project_intent import defers_a_change  # noqa: E402

check = Checks()


class Recorder:
    """Every instruction that reached the edit orchestration."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def plan(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return json.dumps({"changes": [], "summary": "nothing to do"})

    def saw(self, needle: str) -> bool:
        return any(needle.lower() in p.lower() for p in self.prompts)

    def tails(self, n: int = 140) -> list[str]:
        return [p[-n:].replace("\n", " ") for p in self.prompts]


async def _wire(nova, *projects: str) -> Recorder:
    rec = Recorder()
    nova.llm.when("You are Nova improving an existing project", rec.plan,
                  label="improve-plan")
    nova.llm.when(lambda _p: True, lambda _p: "right.", label="flat")
    from core.tool_router import ToolCall
    for name in (projects or ("flappy-bird",)):
        res = await nova.runtime._router.execute(
            ToolCall("project.scaffold", {"name": name}))
        assert res.ok, res.error
    return rec


async def _chat(nova, cid: str, text: str) -> str:
    r = await nova.http.post("/chat", json={"message": text,
                                            "conversation_id": cid})
    assert r.status_code == 200, f"/chat {r.status_code}: {r.text[:200]}"
    return str(r.json().get("assistant") or "")


async def _quiet(nova, rec: Recorder, *, ticks: int = 30) -> None:
    """Give any edit that WAS started time to reach the orchestration."""
    for _ in range(ticks):
        if rec.prompts:
            return
        await asyncio.sleep(0.05)


async def _settled(nova, rec: Recorder, before: int, *, ticks: int = 200) -> bool:
    for _ in range(ticks):
        if len(rec.prompts) > before:
            return True
        await asyncio.sleep(0.05)
    return False


def _pending(nova, cid: str, slug: str) -> str:
    return nova.runtime._pending_plan.get(cid, {}).get(slug, "")


async def test_a_deferral_qualifier_must_modify_the_prohibition():
    """"later" describing the OBJECT is not a deferral.

    `defers_a_change` searched the whole message for temporal words, which is
    the ambiguity the previous round claimed to have avoided. Measured on
    c86bfb1: "Don't fix the bug that happens later in the level." was stored as
    a deferred proposal and a later "Go ahead." executed the ban.

    The qualifier now has to modify the prohibition itself — follow the negated
    verb across a SHORT object and finish the sentence. The four-word relative
    clause in "the animation that appears later" is what tells the two apart,
    and bare "later" is not a qualifier at all; only the bound "until later" is.
    """
    check.section("pending plan: a deferral qualifies the prohibition, not its object")

    not_deferrals = (
        "Don't fix the bug that happens later in the level.",
        "Don't change the animation that appears later.",
        "Don't remove the level that comes later in the game.",
        "Don't modify the eventually-called cleanup function.",
        "Never change that eventually.",
        "Don't change the later animation.",
        "Don't add the button that shows up later on.",
    )
    deferrals = (
        "Don't change it yet.",
        "Don't change anything right now.",
        "Don't build that for now.",
        "Don't implement it until later.",
        "Make the gap larger, but don't change anything yet.",
        "I'd like you to add a dark mode, but don't change it yet.",
    )

    for text in not_deferrals:
        check(not defers_a_change(text), f"not a deferral: {text!r}")
    for text in deferrals:
        check(defers_a_change(text), f"is a deferral: {text!r}")

    async with boot() as nova:
        rec = await _wire(nova)
        # Selected ONCE for the whole boot. `last_active` is durable, so
        # re-selecting per sub-case is redundant setup rather than
        # coverage - and this suite has to stay under the harness
        # watchdog.
        await _chat(nova, str(uuid4()), "Open flappy-bird.")
        for text in not_deferrals:
            rec.prompts.clear()
            cid = str(uuid4())
            await _chat(nova, cid, text)
            held = _pending(nova, cid, "flappy-bird")
            check(not held,
                  f"{text!r} was not stored as a proposal ({held[:40]!r})")
            await _chat(nova, cid, "Go ahead.")
            await _quiet(nova, rec, ticks=40)
            check(not rec.prompts,
                  f"{text!r}: and a later approval executed nothing "
                  f"({rec.tails()})")

        # The legitimate deferral still survives the whole round trip, or the
        # fix would have closed the feature instead of the ambiguity.
        #
        # `bool(pending)` and "an improve call happened" are NOT evidence: this
        # assertion was exactly that, and it passed while the deferral was
        # REPLACING the proposal. The payload is what has to be checked.
        for text in ("Don't change it yet.", "Don't build that for now."):
            rec.prompts.clear()
            cid = str(uuid4())
            # A real proposal: "I'd like you to add a parallax background." carries no
            # action verb the vocabulary knows, so it never created one -
            # this block used to pass only because the DEFERRAL was being
            # stored as the proposal, which is the defect under test.
            await _chat(nova, cid, "Add a parallax background, but don't "
                                   "change anything yet.")
            await _chat(nova, cid, text)
            held = _pending(nova, cid, "flappy-bird")
            check("parallax background" in held.lower(),
                  f"the PROPOSAL survives {text!r}, not the deferral ({held!r})")
            check(text.lower().rstrip(".") not in held.lower(),
                  f"and {text!r} did not become the proposal")
            n = len(rec.prompts)
            await _chat(nova, cid, "Go ahead.")
            check(await _settled(nova, rec, n),
                  f"and is approvable after {text!r}")
            check("parallax background" in rec.prompts[-1].lower(),
                  f"and PARALLAX is what reached improve() ({rec.tails()})")


async def test_a_standalone_deferral_is_not_a_proposal():
    """A deferral says WHEN NOT to act. It does not say what to build.

    The capture accepted the whole deferral bucket, so a sentence carrying only
    timing became the pending proposal. Measured on e88104c:

        "Add a parallax background, but don't change anything yet."
        "Don't build it yet."      -> REPLACED the parallax proposal
        "Go ahead."                -> improve() ran with "Don't build it yet."

    and with nothing pending at all, "Don't change it yet." CREATED an
    executable proposal out of a sentence that proposes nothing.

    Every assertion here reads the payload — the exact pending text and the
    exact instruction that reached improve(). `bool(pending)` and "an improve
    call happened" are what let this through in the first place.
    """
    check.section("pending plan: a deferral alone proposes nothing")

    async with boot() as nova:
        rec = await _wire(nova)
        await _chat(nova, str(uuid4()), "Open flappy-bird.")

        # A. a COMPLETE proposal plus its deferral
        cid = str(uuid4())
        await _chat(nova, cid, "Add a parallax background, but don't change "
                               "anything yet.")
        held = _pending(nova, cid, "flappy-bird")
        check("parallax background" in held.lower(),
              f"A: the proposal is pending ({held[:50]!r})")
        await _quiet(nova, rec, ticks=20)
        check(not rec.prompts, f"A: and nothing ran yet ({rec.tails()})")

        # B. a STANDALONE deferral on top of it
        await _chat(nova, cid, "Don't build it yet.")
        held = _pending(nova, cid, "flappy-bird")
        check("parallax background" in held.lower(),
              f"B: the proposal is still the pending one ({held[:50]!r})")
        check("don't build it yet" not in held.lower(),
              f"B: the deferral did not replace it ({held[:50]!r})")
        await _quiet(nova, rec, ticks=20)
        check(not rec.prompts, f"B: and still nothing ran ({rec.tails()})")

        n = len(rec.prompts)
        await _chat(nova, cid, "Go ahead.")
        check(await _settled(nova, rec, n), "B: the approval executed")
        ran = rec.prompts[-1].lower()
        check("parallax background" in ran,
              f"B: PARALLAX is what reached improve() ({rec.tails()})")
        check("don't build it yet" not in ran,
              f"B: and the deferral text did not ({rec.tails()})")

        # C. a standalone deferral with NOTHING pending
        for text in ("Don't change it yet.", "Don't build it for now.",
                     "Don't implement that until later."):
            rec.prompts.clear()
            cid = str(uuid4())
            await _chat(nova, cid, text)
            held = _pending(nova, cid, "flappy-bird")
            check(not held,
                  f"C: {text!r} created no proposal ({held[:40]!r})")
            await _chat(nova, cid, "Go ahead.")
            await _quiet(nova, rec, ticks=40)
            check(not rec.prompts,
                  f"C: {text!r}: and a later approval ran nothing "
                  f"({rec.tails()})")


async def main():
    await test_a_deferral_qualifier_must_modify_the_prohibition()
    await test_a_standalone_deferral_is_not_a_proposal()
    check.finish()


if __name__ == "__main__":
    run(main)
