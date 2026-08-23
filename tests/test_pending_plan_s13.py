"""The plan that gets approved is the plan that runs (Stage 13A).

THE GAP THIS CLOSES. "Okay, make that change." names no change. The change was
described one or two turns earlier and then corrected, so an approval turn that
carried only its own text reached the builder with nothing to build — and the
plan the user actually approved was simply lost. Measured before the fix: the
approval executed `improve(instructions="Okay, make that change.")`.

WHAT IS AND IS NOT STORED. `_pending_plan` holds the most recent change that was
DESCRIBED but not authorised, keyed by conversation. It supplies WHAT to do. It
never supplies WHETHER: authority still comes from the approving message passing
`authorize_project_mutation`, exactly as before. A pending plan that could
authorise itself would be a far worse defect than the one being fixed — an idle
"go ahead" three turns later would execute it — so both halves are asserted
here, separately.

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


async def _wire(nova) -> Recorder:
    rec = Recorder()
    nova.llm.when("You are Nova improving an existing project", rec.plan,
                  label="improve-plan")
    nova.llm.when(lambda _p: True, lambda _p: "right.", label="flat")
    from core.tool_router import ToolCall
    res = await nova.runtime._router.execute(
        ToolCall("project.scaffold", {"name": "flappy-bird"}))
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


async def test_a_plan_alone_never_executes():
    check.section("pending plan: describing a change does not start it")

    async with boot() as nova:
        rec = await _wire(nova)
        cid = str(uuid4())
        await _chat(nova, cid, "Open flappy-bird.")
        await _chat(nova, cid, "Help me make the pipe spacing easier, but "
                               "don't change anything yet.")
        check(bool(nova.runtime._pending_plan.get(cid, "")),
              "the plan was remembered")
        for filler in ("What's the difference between RAM and VRAM?",
                       "My coffee machine died this morning.",
                       "I might switch to tea.",
                       "Do you ever get bored?"):
            await _chat(nova, cid, filler)
        await _quiet(nova, rec)
        check(not rec.prompts,
              f"and four unrelated turns did not fire it ({len(rec.prompts)})")
        check(bool(nova.runtime._pending_plan.get(cid, "")),
              "it is still pending, not quietly dropped")


async def test_a_bare_approval_with_no_plan_starts_nothing():
    """The mutation this pins: letting a pending plan supply the AUTHORITY.

    A bare "go ahead" is refused by `authorize_project_mutation` because a
    context-free message cannot be told apart from an idle one. Wiring the
    pending plan into that decision would be the tempting shortcut and is the
    reason this test exists.
    """
    check.section("pending plan: authority still comes from the message")

    async with boot() as nova:
        rec = await _wire(nova)
        cid = str(uuid4())
        await _chat(nova, cid, "Open flappy-bird.")
        for text in ("Go ahead.", "Do that.", "Yes, do it.", "Sure.", "Okay."):
            await _chat(nova, cid, text)
        await _quiet(nova, rec)
        check(not rec.prompts,
              f"no edit from bare approval with nothing pending "
              f"({len(rec.prompts)})")

        # …and with a plan pending, a bare approval is STILL not authority.
        await _chat(nova, cid, "I'd like the pipe gap bigger, but don't change "
                               "anything yet.")
        check(bool(nova.runtime._pending_plan.get(cid, "")), "plan pending")
        await _chat(nova, cid, "Go ahead.")
        await _quiet(nova, rec)
        check(not rec.prompts,
              f"a pending plan does not make 'go ahead' an instruction "
              f"({len(rec.prompts)})")

        # An approval that NAMES the action does execute, so the assertions
        # above are about the sentence and not about a dead path.
        await _chat(nova, cid, "Okay, make that change.")
        await _quiet(nova, rec, ticks=200)
        check(bool(rec.prompts), "an explicit approval does execute")
        check(rec.saw("pipe gap bigger"),
              f"and it carries the plan, not just its own words "
              f"({rec.prompts[0][-160:]!r})")


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
        pending = nova.runtime._pending_plan.get(cid, "")
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
              f"and the stale one did not ({rec.prompts[0][-200:]!r})")


async def test_the_plan_is_scoped_to_its_conversation():
    """One conversation's unapproved plan is not another's to execute."""
    check.section("pending plan: it does not leak across conversations")

    async with boot() as nova:
        rec = await _wire(nova)
        a, b = str(uuid4()), str(uuid4())
        await _chat(nova, a, "Open flappy-bird.")
        await _chat(nova, a, "I want a rainbow trail behind the bird, but "
                             "don't change anything yet.")
        check("rainbow trail" in nova.runtime._pending_plan.get(a, "").lower(),
              "conversation A has a pending plan")
        check(not nova.runtime._pending_plan.get(b, ""),
              "conversation B has none")

        await _chat(nova, b, "Okay, make that change.")
        await _quiet(nova, rec, ticks=200)
        check(not rec.saw("rainbow trail"),
              f"B's approval did not execute A's plan "
              f"({[p[-120:] for p in rec.prompts]})")
        check("rainbow trail" in nova.runtime._pending_plan.get(a, "").lower(),
              "and A's plan is still A's, unconsumed")


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
        check(not nova.runtime._pending_plan.get(cid, ""),
              "and was consumed")

        first = len(rec.prompts)
        await _chat(nova, cid, "Okay, make that change.")
        await _quiet(nova, rec, ticks=60)
        later = [p for p in rec.prompts[first:]]
        check(not any("parallax background" in p.lower() for p in later),
              f"a second approval does not replay it ({len(later)} later calls)")


async def main():
    await test_a_plan_alone_never_executes()
    await test_a_bare_approval_with_no_plan_starts_nothing()
    await test_the_correction_is_what_runs()
    await test_the_plan_is_scoped_to_its_conversation()
    await test_an_approved_plan_is_consumed_once()
    check.finish()


if __name__ == "__main__":
    run(main)
