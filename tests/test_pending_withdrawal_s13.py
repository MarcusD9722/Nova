"""Withdrawing a proposal, and the grammar that decides what a withdrawal IS.

Split out of test_pending_plan_s13.py, which grew past the harness's 180s
watchdog — a suite killed mid-run reports nothing at all. That file keeps the
proposal LIFECYCLE (capture, correction, scoping, approval, consumption); this
one keeps everything about calling a change OFF.

THREE SHAPES OF WITHDRAWAL, narrowest first:

  1. an explicit withdrawal   "never mind", "cancel that", "actually don't"
  2. a GENERIC prohibition    "don't change anything" — about whatever is on
                              the table
  3. a SPECIFIC prohibition   "don't change the physics" — withdraws only a
                              proposal it actually refers to

(3) is why cancellation needs the pending proposal in hand. Without it the
choice was between cancelling whatever happened to be pending — so an unrelated
prohibition erased a dark-mode proposal — and cancelling nothing, which left a
withdrawn change pending for a later "Go ahead." to execute.

AND THE OPPOSITE OF A WITHDRAWAL IS A DEFERRAL. Both are refused by the mutation
gate for the same reason, so the reason cannot separate them:

    "Make the pipe gap easier, but don't change anything yet."   deferral
    "Don't change the physics."                                  withdrawal

A prohibition scoped in TIME still wants the change; an unqualified one does
not. The qualifier has to modify the PROHIBITION, not merely appear in the
sentence — "Don't fix the bug that happens later in the level" is a ban, and
reading it as a deferral stored it as work a later approval would carry out.

Run:  venv\\Scripts\\python.exe tests\\test_pending_withdrawal_s13.py
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


async def test_cancellation_invalidates_the_proposal():
    check.section("pending plan: a cancellation withdraws it, and is not one")

    cancels = ("Actually don't.", "Don't make that change.", "Never mind.",
               "Forget that change.", "Cancel that.", "Leave it the way it is.",
               "Scrap that.", "On second thought, no.")

    async with boot() as nova:
        rec = await _wire(nova)
        # Selected ONCE for the whole boot. `last_active` is durable, so
        # re-selecting per sub-case is redundant setup rather than
        # coverage - and this suite has to stay under the harness
        # watchdog.
        await _chat(nova, str(uuid4()), "Open flappy-bird.")
        for text in cancels:
            rec.prompts.clear()
            cid = str(uuid4())
            await _chat(nova, cid, "Make the pipe gap larger, but don't change "
                                   "anything yet.")
            check(bool(_pending(nova, cid, "flappy-bird")),
                  f"[{text}] a plan was pending first")

            await _chat(nova, cid, text)
            left = _pending(nova, cid, "flappy-bird")
            check(not left, f"{text!r} withdrew the proposal ({left[:40]!r})")

            await _chat(nova, cid, "The weather is bad today.")
            await _chat(nova, cid, "Okay, make that change.")
            await _quiet(nova, rec, ticks=40)
            check(not rec.saw("pipe gap larger"),
                  f"{text!r}: the withdrawn plan did not run later")
            # …and the cancellation itself never became the instruction.
            check(not rec.saw(text.rstrip(".").lower()),
                  f"{text!r} was not stored as the new plan ({rec.tails()})")

        # A bare approval after a cancellation is idle again.
        rec.prompts.clear()
        cid = str(uuid4())
        await _chat(nova, cid, "I'd like a parallax background, but don't "
                               "change anything yet.")
        await _chat(nova, cid, "Never mind.")
        await _chat(nova, cid, "Go ahead.")
        await _quiet(nova, rec, ticks=40)
        check(not rec.prompts,
              f"'go ahead' after a cancellation approves nothing ({rec.tails()})")


async def test_a_cancellation_is_scoped_too():
    """Withdrawing A's plan does not withdraw B's."""
    check.section("pending plan: a cancellation withdraws one project's plan")

    async with boot() as nova:
        rec = await _wire(nova, "flappy-bird", "calc-tool")
        cid = str(uuid4())
        await _chat(nova, cid, "Open flappy-bird.")
        await _chat(nova, cid, "I'd like a parallax background, but don't "
                               "change anything yet.")
        await _chat(nova, cid, "Switch to calc-tool.")
        await _chat(nova, cid, "I'd like a percent key, but don't change "
                               "anything yet.")
        check(bool(_pending(nova, cid, "flappy-bird")), "A has a plan")
        check(bool(_pending(nova, cid, "calc-tool")), "B has a plan")

        await _chat(nova, cid, "Never mind.")
        check(not _pending(nova, cid, "calc-tool"),
              "the cancellation withdrew the CURRENT project's plan")
        check("parallax" in _pending(nova, cid, "flappy-bird").lower(),
              f"and left the other project's alone "
              f"({_pending(nova, cid, 'flappy-bird')!r})")


async def test_a_prohibition_never_becomes_a_proposal():
    """A ban must not turn into executable work.

    Measured on b2a931e: "Don't change the physics." was stored as the pending
    proposal, and a later "Go ahead." CARRIED IT OUT — the exact inversion of
    what the mutation gate exists to prevent.

    The capture accepted every refusal whose reason was "vetoed: prohibition",
    and two opposite sentences share that reason. What separates them is not the
    verb but whether the prohibition is scoped in TIME: deferred means "later",
    unqualified means "not at all".
    """
    check.section("pending plan: a prohibition is not a deferred proposal")

    prohibitions = (
        "Don't change the physics.",
        "Don't modify the menu.",
        "Never change the scoring.",
        "I don't want you to change the physics.",
        "Do not update that file.",
        "Don't touch the collision code.",
        "Never update the readme.",
    )
    async with boot() as nova:
        rec = await _wire(nova)
        # Selected ONCE for the whole boot. `last_active` is durable, so
        # re-selecting per sub-case is redundant setup rather than
        # coverage - and this suite has to stay under the harness
        # watchdog.
        await _chat(nova, str(uuid4()), "Open flappy-bird.")
        for text in prohibitions:
            rec.prompts.clear()
            cid = str(uuid4())
            await _chat(nova, cid, text)
            held = _pending(nova, cid, "flappy-bird")
            check(not held, f"{text!r} was not stored as a proposal ({held[:40]!r})")

            await _chat(nova, cid, "Go ahead.")
            await _quiet(nova, rec, ticks=40)
            check(not rec.prompts,
                  f"{text!r}: and a later approval executed nothing "
                  f"({rec.tails()})")

        # KNOWN LIMIT, stated rather than hidden. What is recognised is a
        # deferral expressed as a PROHIBITION clause: "but don't ... yet".
        # A bare "..., but not yet." carries no prohibition and, in the
        # cases tried, no action verb the vocabulary knows, so it is not
        # captured. Widening the test to any sentence containing "later" or
        # "at some point" would misfire on "Fix the bug that happens later in
        # the level", so that is deliberately left alone.
        # The deferral half of the same grammar still works, or the fix would
        # have closed the feature instead of the hole.
        for text in ("Make the pipe gap easier, but don't change anything yet.",
                     "Add a pause menu later, but don't build it yet.",
                     "Improve the pipe spacing, but don't change it yet.",
                     "Change the physics, but don't change anything right now."):
            rec.prompts.clear()
            cid = str(uuid4())
            await _chat(nova, cid, text)
            held = _pending(nova, cid, "flappy-bird")
            check(bool(held), f"deferred proposal still captured: {text!r}")
            check(not rec.prompts, f"and not executed: {text!r}")

            n = len(rec.prompts)
            await _chat(nova, cid, "Okay, make that change.")
            check(await _settled(nova, rec, n),
                  f"and approving it runs it: {text!r}")

        # A deferral must not read as a cancellation either — it would throw away
        # the proposal it is half of.
        from core.project_intent import cancels_pending_change, defers_a_change
        for text in ("Make the pipe gap easier, but don't change anything yet.",
                     "I'd like a dark mode, but don't change anything yet."):
            check(defers_a_change(text), f"is a deferral: {text!r}")
            check(not cancels_pending_change(text),
                  f"and not a cancellation: {text!r}")
        # …while the same prohibition WITHOUT a time qualifier withdraws.
        for text in ("Don't change anything.", "Don't make any changes."):
            check(cancels_pending_change(text), f"withdraws: {text!r}")
            check(not defers_a_change(text), f"and defers nothing: {text!r}")


async def test_a_specific_withdrawal_cancels_the_proposal_it_names():
    """Calling a change off has to reach the change that was proposed.

    The previous round proved a prohibition never CREATES a proposal from an
    empty state. That is not the same claim. Measured on c86bfb1:

        "I'd like you to change the physics, but don't change it yet."
        "Actually, don't change the physics."
        "Go ahead."
          -> the withdrawn physics change EXECUTED

    The new prohibition was correctly not captured, and the old proposal
    survived underneath it.

    Cancellation is tied to the proposal by what the prohibition is ABOUT.
    Cancelling whatever happens to be pending would be just as wrong in the
    other direction, so both are asserted.
    """
    check.section("pending plan: a withdrawal cancels the proposal it names")

    async with boot() as nova:
        rec = await _wire(nova, "flappy-bird", "calc-tool")

        # Selected ONCE for the whole boot. `last_active` is durable, so
        # re-selecting per sub-case is redundant setup rather than
        # coverage - and this suite has to stay under the harness
        # watchdog.
        await _chat(nova, str(uuid4()), "Open flappy-bird.")
        for withdrawal in ("Actually, don't change the physics.",
                           "Don't change the physics.",
                           "Never change the physics."):
            rec.prompts.clear()
            cid = str(uuid4())
            await _chat(nova, cid, "I'd like you to change the physics, but "
                                   "don't change it yet.")
            check("physics" in _pending(nova, cid, "flappy-bird").lower(),
                  f"[{withdrawal}] the physics change is pending")

            await _chat(nova, cid, withdrawal)
            left = _pending(nova, cid, "flappy-bird")
            check(not left, f"{withdrawal!r} cancelled it ({left[:40]!r})")

            # …and the withdrawal did not become the next proposal.
            await _chat(nova, cid, "Go ahead.")
            await _quiet(nova, rec, ticks=40)
            check(not rec.prompts,
                  f"{withdrawal!r}: a later approval executed nothing "
                  f"({rec.tails()})")

            # An anaphoric approval must not resurrect it either.
            await _chat(nova, cid, "Okay, make that change.")
            await _quiet(nova, rec, ticks=40)
            check(not rec.saw("physics"),
                  f"{withdrawal!r}: nor did an anaphoric approval "
                  f"({rec.tails()})")

        # A prohibition about something ELSE must not erase the proposal.
        rec.prompts.clear()
        cid = str(uuid4())
        await _chat(nova, cid, "I'd like a dark mode, but don't change it yet.")
        for unrelated in ("Don't change the physics.",
                          "Don't remove the menu.",
                          "Don't update the readme."):
            await _chat(nova, cid, unrelated)
            check("dark mode" in _pending(nova, cid, "flappy-bird").lower(),
                  f"{unrelated!r} left the dark-mode proposal alone "
                  f"({_pending(nova, cid, 'flappy-bird')[:40]!r})")
        # …and it is still approvable, because it was never withdrawn.
        n = len(rec.prompts)
        await _chat(nova, cid, "Go ahead.")
        check(await _settled(nova, rec, n),
              "the untouched proposal still approves")
        check(rec.saw("dark mode"), f"and it is the right one ({rec.tails()})")

        # Two projects, a withdrawal naming only one.
        rec.prompts.clear()
        cid = str(uuid4())
        await _chat(nova, cid, "Open flappy-bird.")
        await _chat(nova, cid, "I'd like you to change the physics, but don't "
                               "change it yet.")
        await _chat(nova, cid, "Switch to calc-tool.")
        await _chat(nova, cid, "I'd like a percent key, but don't add it yet.")
        await _chat(nova, cid, "Go back to flappy-bird.")
        await _chat(nova, cid, "Actually, don't change the physics.")
        check(not _pending(nova, cid, "flappy-bird"),
              "only the named project's proposal was cancelled")
        check("percent key" in _pending(nova, cid, "calc-tool").lower(),
              f"the other project's proposal survived "
              f"({_pending(nova, cid, 'calc-tool')[:40]!r})")


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
        "I'd like a dark mode, but don't change it yet.",
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
        for text in ("Don't change it yet.", "Don't build that for now."):
            rec.prompts.clear()
            cid = str(uuid4())
            await _chat(nova, cid, "I'd like a parallax background.")
            await _chat(nova, cid, text)
            check(bool(_pending(nova, cid, "flappy-bird")),
                  f"a proposal survives {text!r}")
            n = len(rec.prompts)
            await _chat(nova, cid, "Go ahead.")
            check(await _settled(nova, rec, n),
                  f"and is approvable after {text!r}")


async def main():
    await test_cancellation_invalidates_the_proposal()
    await test_a_cancellation_is_scoped_too()
    await test_a_prohibition_never_becomes_a_proposal()
    await test_a_specific_withdrawal_cancels_the_proposal_it_names()
    await test_a_deferral_qualifier_must_modify_the_prohibition()
    check.finish()


if __name__ == "__main__":
    run(main)
