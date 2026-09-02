"""Stage 15 — the trailing-deferral grammar, and the proposal it produces.

Stage 13A's grammar is a compatibility boundary: the new clause may only add
sentences that were previously executed by mistake, and may not take away
sentences that previously worked. So the battery below runs BOTH directions,
and the negative half is the important half -- "not yet" describes the world far
more often than it qualifies a request, and reading those as deferrals would
refuse work that was actually asked for.

The lifecycle half then checks that a deferred proposal behaves like a
proposal: the newest correction is what runs, a withdrawal really withdraws,
and nothing older is ever resurrected -- including across a restart, where the
pending plan is process-local and must simply be gone.

  I7   planning does not silently execute
  I8   approval does not imply execution
  I26  correction supersedes prior intent
  I27  stale planner output cannot execute after correction

Run:  venv\\Scripts\\python.exe tests\\test_s15_deferral_grammar.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, boot, run  # noqa: E402

from core.project_intent import (  # noqa: E402
    authorize_project_mutation, carries_a_proposal, defers_a_change,
)

check = Checks()

#: Sentences that ASK for something and put it off. Each must be refused as a
#: deferral and must still read as a proposal, or the change is simply lost.
DEFERRED = [
    "Make it slower, but not yet.",
    "Make it slower, but not yet",                    # no full stop
    "Make it slower, not yet though.",
    "Make it slower, not yet, though.",               # comma before though
    "Make it slower - not yet.",                      # dash
    "Make it slower; not yet.",                       # semicolon
    "Make it slower, though not just yet.",
    "Make it slower, however not yet.",
    "Make it slower, not just yet!",
    "Make it slower, not yet   ",                     # trailing whitespace
    "Make it slower, but not right now.",
    "Make it slower, but not for now.",
    "Make it slower, but not now.",
    "Make it slower, just not yet.",
    "Make it slower, not at the moment.",
    "Make it slower, but not until later.",
]

#: Sentences where "not yet"/"now" describes the WORLD, or is simply part of a
#: name. Every one of these must be untouched by the new clause: a deferral
#: reading would refuse work the person asked for.
NOT_DEFERRED = [
    "The score is not showing yet, add it.",
    "It is not done yet, keep going.",
    "Not yet finished, please fix the physics.",
    "The bird does not fall slower yet, make it slower.",
    "It is not working right now, fix it.",
    "The build is not ready for now, please retry it.",
    "Add a not-yet-implemented banner.",
    "Add a now-playing widget.",
    "Is it not done yet?",
    "Make it slower.",
    "Do it now.",
]


def seed(nova, name: str) -> Path:
    p = nova.projects_dir / name
    p.mkdir(parents=True, exist_ok=True)
    (p / "PROJECT.md").write_text(f"# {name}\n\n## Status\nidea\n",
                                  encoding="utf-8")
    (p / "main.py").write_text("print('x')\n", encoding="utf-8")
    return p


class ImproveSpy:
    def __init__(self, nova):
        self.calls: list[tuple[str, str]] = []
        self._pb = nova.runtime._project_builder
        self._real = self._pb.improve

        async def improve(*, slug, instructions, **k):
            self.calls.append((slug, instructions))
            return await self._real(slug=slug, instructions=instructions, **k)

        self._pb.improve = improve

    def restore(self) -> None:
        self._pb.improve = self._real


async def settle(nova) -> None:
    pb = nova.runtime._project_builder
    for _ in range(120):
        if not any(pb.is_building(p) for p in pb.list_projects()):
            return
        await asyncio.sleep(0.05)


# ── the grammar ────────────────────────────────────────────────────────────

async def test_the_deferral_battery():
    check.section("13A boundary: what the new clause adds, and what it must not")
    missed = []
    for text in DEFERRED:
        verdict = authorize_project_mutation(text)
        if not (defers_a_change(text) and not verdict.allowed):
            missed.append((text, verdict.reason))
    check(not missed,
          f"all {len(DEFERRED)} deferral phrasings are refused as deferrals "
          f"({missed[:2]})")

    lost = [t for t in DEFERRED if not carries_a_proposal(t)]
    check(not lost,
          f"and every one still reads as a PROPOSAL, so the change is not "
          f"lost ({lost[:2]})")

    false_positives = [t for t in NOT_DEFERRED if defers_a_change(t)]
    check(not false_positives,
          f"none of the {len(NOT_DEFERRED)} world-describing sentences became "
          f"a deferral ({false_positives[:3]})")

    # And the ones that were instructions before are still instructions.
    for text in ("Make it slower.", "Add a leaderboard now.",
                 "Add a not-yet-implemented banner.",
                 "Add a now-playing widget."):
        check(authorize_project_mutation(text).allowed,
              f"{text!r} is still an authorised instruction")

    # RECORDED, NOT ASSERTED AS A DEFECT. "Do it now." is refused as "no
    # affirmative instruction" -- and so are "Do it." and "Please do it now."
    # `do` is simply not in Stage 13A's imperative vocabulary, which predates
    # all of this; a bare pro-verb after a proposal is handled by the APPROVAL
    # path, not by the mutation gate. I asserted it was authorised without
    # checking, which was an assumption rather than a measurement.
    bare = authorize_project_mutation("Do it now.")
    check(bare.reason == "no affirmative instruction",
          f"'Do it now.' is refused for the pre-existing reason, not by the "
          f"new deferral clause ({bare.reason!r})")


async def test_the_anchor_is_what_does_the_work():
    """The same words, trailing versus mid-sentence."""
    check.section("the clause must CLOSE the sentence")
    pairs = [
        ("Make it slower, but not yet.", "It is not yet slower, make it slower."),
        ("Add a leaderboard, not right now.",
         "The leaderboard is not right now visible, add it."),
    ]
    for deferred, immediate in pairs:
        check(defers_a_change(deferred),
              f"trailing: {deferred!r} defers")
        check(not defers_a_change(immediate),
              f"mid-sentence: {immediate!r} does not")


# ── the proposal it produces ───────────────────────────────────────────────

async def test_the_newest_of_several_corrections_is_what_runs():
    check.section("I26 three deferred corrections, the last one wins")
    async with boot(default_reply="Sure.") as nova:
        seed(nova, "flappy-bird")
        await nova.memory.add_fact(entity="projects", attribute="last_active",
                                   value="flappy-bird", confidence=0.99)
        conv = str(uuid4())
        spy = ImproveSpy(nova)
        try:
            await nova.brain.chat("Make the pipe gap wider, but not yet.",
                                  conversation_id=conv)
            await nova.brain.chat(
                "Actually make the bird fall slower instead, not yet though.",
                conversation_id=conv)
            await nova.brain.chat(
                "No, add a parallax background instead, but not right now.",
                conversation_id=conv)
            check(not spy.calls,
                  f"none of the three ran on their own ({spy.calls})")

            await nova.brain.chat("Go ahead.", conversation_id=conv)
            await settle(nova)

            check(len(spy.calls) == 1,
                  f"approval ran exactly one change ({len(spy.calls)})")
            instructions = spy.calls[0][1] if spy.calls else ""
            check("parallax" in instructions,
                  f"and it is the NEWEST correction ({instructions[:60]!r})")
            check("pipe gap" not in instructions and "fall slower" not in instructions,
                  f"neither of the ones it replaced ({instructions[:60]!r})")
        finally:
            spy.restore()


async def test_a_withdrawal_leaves_nothing_to_approve():
    check.section("I8 a withdrawn proposal is not resurrected by an approval")
    async with boot(default_reply="Sure.") as nova:
        seed(nova, "flappy-bird")
        await nova.memory.add_fact(entity="projects", attribute="last_active",
                                   value="flappy-bird", confidence=0.99)
        conv = str(uuid4())
        spy = ImproveSpy(nova)
        try:
            await nova.brain.chat("Make the pipe gap wider, but not yet.",
                                  conversation_id=conv)
            await nova.brain.chat("Actually, don't change the pipe gap.",
                                  conversation_id=conv)
            await nova.brain.chat("Go ahead.", conversation_id=conv)
            await settle(nova)
            check(not spy.calls,
                  f"the withdrawn plan did not run ({spy.calls})")

            # A NEW proposal after the withdrawal is still approvable.
            await nova.brain.chat("Add a parallax background, but not yet.",
                                  conversation_id=conv)
            await nova.brain.chat("Go ahead.", conversation_id=conv)
            await settle(nova)
            check(len(spy.calls) == 1,
                  f"and the replacement does run ({len(spy.calls)})")
            check(spy.calls and "parallax" in spy.calls[0][1],
                  f"with the right words ({spy.calls})")
        finally:
            spy.restore()


async def test_a_deferred_proposal_does_not_survive_a_restart():
    """The pending plan is process-local, and that is the safe direction.

    After a restart there is no proposal, so "go ahead" authorises nothing.
    What must NOT happen is an older plan reappearing, or the words of the
    approval turn being executed as if they were the plan.
    """
    check.section("I27 a restart leaves nothing pending to approve")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        conv = str(uuid4())
        async with boot(root=root, default_reply="Sure.") as nova:
            seed(nova, "flappy-bird")
            await nova.memory.add_fact(entity="projects",
                                       attribute="last_active",
                                       value="flappy-bird", confidence=0.99)
            spy = ImproveSpy(nova)
            try:
                await nova.brain.chat("Make the pipe gap wider, but not yet.",
                                      conversation_id=conv)
                check(not spy.calls, "nothing ran before the restart")
                pending = nova.runtime._pending_plan.get(conv, {})
                check(pending.get("flappy-bird", "").startswith("Make the pipe"),
                      f"and the proposal really was held ({pending})")
            finally:
                spy.restore()

        # A second life against the same durable root.
        async with boot(root=root, default_reply="Sure.") as nova2:
            spy2 = ImproveSpy(nova2)
            try:
                held = nova2.runtime._pending_plan.get(conv, {})
                check(not held,
                      f"the new process holds no pending plan ({held})")
                await nova2.brain.chat("Go ahead.", conversation_id=conv)
                await settle(nova2)
                check(not spy2.calls,
                      f"so an approval after a restart runs nothing "
                      f"({spy2.calls})")
                # And specifically: the approval's own words are not the plan.
                check(all("Go ahead" not in c[1] for c in spy2.calls),
                      f"and 'Go ahead.' was not executed as an instruction "
                      f"({spy2.calls})")
            finally:
                spy2.restore()


async def main() -> None:
    await test_the_deferral_battery()
    await test_the_anchor_is_what_does_the_work()
    await test_the_newest_of_several_corrections_is_what_runs()
    await test_a_withdrawal_leaves_nothing_to_approve()
    await test_a_deferred_proposal_does_not_survive_a_restart()
    check.finish()


if __name__ == "__main__":
    run(main)
