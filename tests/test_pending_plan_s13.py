"""The plan that gets approved is the plan that runs — and only that one.

THE GAP THIS CLOSES. "Okay, make that change." names no change. The change was
described one or two turns earlier and then corrected, so an approval turn that
carried only its own text reached the builder with nothing to build — and the
plan the user actually approved was simply lost.

WHAT IS STORED, AND HOW IT IS KEYED

    conversation -> project -> the most recent change DESCRIBED but not approved

Both levels are load-bearing and both were found missing by review:

  conversation  one conversation's unapproved plan is not another's to run.

  project       keyed only by conversation, the plan was popped and executed
                against whatever project happened to be current at approval
                time, so "open flappy-bird / describe a change / switch to
                calc-tool / okay, make that change" fed the Flappy Bird plan to
                improve(calc-tool). A plan belongs to the project it was
                described for.

WHAT IT SUPPLIES. The plan supplies WHAT to do. Whether to do anything is
decided by the approving message, and the context-free mutation gate is
unchanged — `authorize_project_mutation("Go ahead.")` still refuses, because an
idle "go ahead" in conversation is the same string. The approval path pairs that
shape with a real proposal for the project actually in play. With no such
proposal, the identical words mutate nothing. Both directions are asserted.

CANCELLING. A proposal can also be withdrawn, which used to be impossible and
failed in both directions at once: "Actually don't." left the plan pending for a
later approval to run, and "Don't make that change." was stored AS the plan, so
approving it later executed those words as an instruction.

Run:  venv\\Scripts\\python.exe tests\\test_pending_plan_s13.py
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


async def test_a_plan_alone_never_executes():
    check.section("pending plan: describing a change does not start it")

    async with boot() as nova:
        rec = await _wire(nova)
        cid = str(uuid4())
        await _chat(nova, cid, "Open flappy-bird.")
        await _chat(nova, cid, "Help me make the pipe spacing easier, but "
                               "don't change anything yet.")
        check(bool(_pending(nova, cid, "flappy-bird")), "the plan was remembered")
        for filler in ("What's the difference between RAM and VRAM?",
                       "My coffee machine died this morning.",
                       "I might switch to tea.",
                       "Do you ever get bored?"):
            await _chat(nova, cid, filler)
        await _quiet(nova, rec)
        check(not rec.prompts,
              f"and four unrelated turns did not fire it ({len(rec.prompts)})")
        check(bool(_pending(nova, cid, "flappy-bird")),
              "it is still pending, not quietly dropped")


async def test_a_bare_approval_is_authority_only_with_a_proposal():
    """Both directions of the context-aware approval, in one conversation.

    The pairing is the safety property. A bare approval that could authorise on
    its own would turn an idle "go ahead" into an edit; a bare approval that can
    NEVER authorise leaves the user unable to say yes to a proposal Nova just
    made. Neither half is safe alone, so neither is asserted alone.
    """
    check.section("pending plan: 'go ahead' needs a proposal to be authority")

    async with boot() as nova:
        rec = await _wire(nova)
        cid = str(uuid4())
        await _chat(nova, cid, "Open flappy-bird.")

        # IDLE: nothing proposed. The same words must do nothing at all.
        for text in ("Go ahead.", "Do that.", "Yes, do it.", "Go for it.",
                     "Okay, go ahead.", "Sure, do it."):
            await _chat(nova, cid, text)
        await _quiet(nova, rec)
        check(not rec.prompts,
              f"idle approval starts nothing ({len(rec.prompts)} improve calls)")

        # …and the context-free gate is untouched, which is what keeps the
        # decision in the turn path where the context actually lives.
        from core.project_intent import authorize_project_mutation
        for text in ("Go ahead.", "Do that.", "Yes, do it."):
            check(not authorize_project_mutation(text, complaint=False).allowed,
                  f"the message-level gate still refuses {text!r}")

        # CONTEXTUAL: with a proposal for the CURRENT project, it approves.
        await _chat(nova, cid, "I'd like the pipe gap bigger, but don't change "
                               "anything yet.")
        check(bool(_pending(nova, cid, "flappy-bird")), "plan pending")
        n = len(rec.prompts)
        await _chat(nova, cid, "Go ahead.")
        check(await _settled(nova, rec, n), "a contextual 'go ahead' executes")
        check(rec.saw("pipe gap bigger"),
              f"and it carries the proposal ({rec.tails()})")
        check(not _pending(nova, cid, "flappy-bird"),
              "and consumes it")

        # …and immediately afterwards the same words are idle again.
        n = len(rec.prompts)
        await _chat(nova, cid, "Go ahead.")
        await asyncio.sleep(0.6)
        check(len(rec.prompts) == n,
              "the same words do nothing once the proposal is spent")


async def test_an_approval_that_names_nothing_refuses_when_nothing_is_pending():
    """"Okay, make that change." with no proposal must not invent one.

    This one passes the mutation gate — it has an action verb — so scoping the
    plan away from it was not enough on its own: the approval still ran, with
    its own sentence handed to the builder as the instruction. An edit that
    cannot know what to do should say so.
    """
    check.section("pending plan: an approval with no proposal says so")

    async with boot() as nova:
        rec = await _wire(nova)
        cid = str(uuid4())
        await _chat(nova, cid, "Open flappy-bird.")
        reply = await _chat(nova, cid, "Okay, make that change.")
        await _quiet(nova, rec)
        check(not rec.prompts,
              f"no edit was started ({len(rec.prompts)} improve calls)")
        check("flappy-bird" in reply.lower(),
              f"and Nova says which project she has nothing for ({reply[:70]!r})")

        # A message that NAMES the change is unaffected — the refusal is about
        # the pronoun, not about editing.
        n = len(rec.prompts)
        await _chat(nova, cid, "Make the pipe gap 20% larger.")
        check(await _settled(nova, rec, n),
              "a concrete instruction still executes with nothing pending")


async def test_the_correction_is_what_runs():
    check.section("pending plan: a correction replaces, it does not queue")

    async with boot() as nova:
        rec = await _wire(nova)
        cid = str(uuid4())
        await _chat(nova, cid, "Open flappy-bird.")
        await _chat(nova, cid, "Make the horizontal pipe spacing wider, but "
                               "don't change anything yet.")
        await _chat(nova, cid, "Actually, keep the horizontal spacing. I meant "
                               "make the vertical opening larger.")
        pending = _pending(nova, cid, "flappy-bird")
        check("vertical opening" in pending.lower(),
              f"the correction is what is pending ({pending[:60]!r})")
        check("wider" not in pending.lower(),
              "and the superseded plan is gone, not appended")

        await _chat(nova, cid, "Okay, make that change.")
        await _quiet(nova, rec, ticks=200)
        check(bool(rec.prompts), "the approval executed")
        check(rec.saw("vertical opening"),
              "the corrected requirement reached the edit")
        check(not rec.saw("spacing wider"),
              f"and the stale one did not ({rec.tails()})")


async def test_the_plan_is_scoped_to_its_conversation():
    """One conversation's unapproved plan is not another's to execute."""
    check.section("pending plan: it does not leak across conversations")

    async with boot() as nova:
        rec = await _wire(nova)
        a, b = str(uuid4()), str(uuid4())
        await _chat(nova, a, "Open flappy-bird.")
        await _chat(nova, a, "I want a rainbow trail behind the bird, but "
                             "don't change anything yet.")
        check("rainbow trail" in _pending(nova, a, "flappy-bird").lower(),
              "conversation A has a pending plan")
        check(not _pending(nova, b, "flappy-bird"), "conversation B has none")

        await _chat(nova, b, "Okay, make that change.")
        await _quiet(nova, rec, ticks=60)
        check(not rec.saw("rainbow trail"),
              f"B's approval did not execute A's plan ({rec.tails()})")
        check("rainbow trail" in _pending(nova, a, "flappy-bird").lower(),
              "and A's plan is still A's, unconsumed")

        # A bare approval in B must not reach across either.
        await _chat(nova, b, "Go ahead.")
        await _quiet(nova, rec, ticks=60)
        check(not rec.saw("rainbow trail"),
              f"nor does a bare approval in B ({rec.tails()})")


async def test_the_plan_is_scoped_to_its_project():
    """The defect this level of keying exists for.

    Measured on 3278f39: describe a change for flappy-bird, switch to calc-tool,
    approve — and the Flappy Bird plan was handed to improve(calc-tool).
    """
    check.section("pending plan: A's plan can never execute on B")

    async with boot() as nova:
        rec = await _wire(nova, "flappy-bird", "calc-tool")
        pb = nova.runtime._project_builder
        cid = str(uuid4())

        await _chat(nova, cid, "Open flappy-bird.")
        await _chat(nova, cid, "I want a rainbow trail behind the bird, but "
                               "don't change anything yet.")
        check("rainbow trail" in _pending(nova, cid, "flappy-bird").lower(),
              "the plan is filed under flappy-bird")

        await _chat(nova, cid, "Switch to calc-tool.")
        check(await pb.last_active() == "calc-tool", "we are on B")
        check("rainbow trail" in _pending(nova, cid, "flappy-bird").lower(),
              "switching did not re-target the plan")
        check(not _pending(nova, cid, "calc-tool"),
              f"and B has no plan of its own "
              f"({_pending(nova, cid, 'calc-tool')!r})")

        await _chat(nova, cid, "Okay, make that change.")
        await _quiet(nova, rec, ticks=60)
        check(not rec.saw("rainbow trail"),
              f"an approval on B cannot consume A's plan ({rec.tails()})")
        await _chat(nova, cid, "Go ahead.")
        await _quiet(nova, rec, ticks=60)
        check(not rec.saw("rainbow trail"),
              f"nor can a bare approval on B ({rec.tails()})")

        # A new plan for B does not disturb A's.
        await _chat(nova, cid, "For this one I'd like a percent key, but don't "
                               "change anything yet.")
        check("percent key" in _pending(nova, cid, "calc-tool").lower(),
              f"B has its own plan now ({_pending(nova, cid, 'calc-tool')!r})")
        check("rainbow trail" in _pending(nova, cid, "flappy-bird").lower(),
              "and A's is untouched")

        # Back on A, A's plan is still valid and is the one that runs.
        await _chat(nova, cid, "Go back to flappy-bird.")
        check(await pb.last_active() == "flappy-bird", "we are back on A")
        n = len(rec.prompts)
        await _chat(nova, cid, "Okay, make that change.")
        check(await _settled(nova, rec, n), "A's plan executes on A")
        check("rainbow trail" in rec.prompts[-1].lower(),
              f"and it is A's plan, not B's ({rec.tails()})")
        check("percent key" not in rec.prompts[-1].lower(),
              "B's plan did not come along")
        check("percent key" in _pending(nova, cid, "calc-tool").lower(),
              "B's plan is still pending for B")


async def test_cancellation_invalidates_the_proposal():
    check.section("pending plan: a cancellation withdraws it, and is not one")

    cancels = ("Actually don't.", "Don't make that change.", "Never mind.",
               "Forget that change.", "Cancel that.", "Leave it the way it is.",
               "Scrap that.", "On second thought, no.")

    async with boot() as nova:
        rec = await _wire(nova)
        for text in cancels:
            rec.prompts.clear()
            cid = str(uuid4())
            await _chat(nova, cid, "Open flappy-bird.")
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
        await _chat(nova, cid, "Open flappy-bird.")
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


async def test_an_approved_plan_is_consumed_once():
    check.section("pending plan: approving twice does not run it twice")

    async with boot() as nova:
        rec = await _wire(nova)
        cid = str(uuid4())
        await _chat(nova, cid, "Open flappy-bird.")
        await _chat(nova, cid, "I'd like a parallax background, but don't "
                               "change anything yet.")
        await _chat(nova, cid, "Okay, make that change.")
        await _quiet(nova, rec, ticks=200)
        check(rec.saw("parallax background"), "the plan ran once")
        check(not _pending(nova, cid, "flappy-bird"), "and was consumed")

        first = len(rec.prompts)
        await _chat(nova, cid, "Okay, make that change.")
        await asyncio.sleep(0.6)
        later = rec.prompts[first:]
        check(not any("parallax background" in p.lower() for p in later),
              f"a second approval does not replay it ({len(later)} later calls)")


async def main():
    await test_a_plan_alone_never_executes()
    await test_a_bare_approval_is_authority_only_with_a_proposal()
    await test_an_approval_that_names_nothing_refuses_when_nothing_is_pending()
    await test_the_correction_is_what_runs()
    await test_the_plan_is_scoped_to_its_conversation()
    await test_the_plan_is_scoped_to_its_project()
    await test_cancellation_invalidates_the_proposal()
    await test_a_cancellation_is_scoped_too()
    await test_an_approved_plan_is_consumed_once()
    check.finish()


if __name__ == "__main__":
    run(main)
