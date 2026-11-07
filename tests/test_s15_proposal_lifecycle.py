"""Stage 15 — the pending proposal itself, not the mutation it eventually causes.

Every check here reads `runtime._pending_plan` directly: the conversation it is
filed under, the project it is filed against, and the exact text held. A test
that only watches for a file change cannot tell "the correction replaced the
plan" from "the correction was dropped and the old plan happened to be
harmless".

  I7   planning does not silently execute
  I8   approval does not imply execution
  I26  correction supersedes prior intent
  I27  stale planner output cannot execute after correction
  I28  project A state never modifies project B

Run:  venv\\Scripts\\python.exe tests\\test_s15_proposal_lifecycle.py
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

from core.project_intent import carries_a_proposal, leading_target  # noqa: E402

check = Checks()


def seed(nova, name: str) -> Path:
    p = nova.projects_dir / name
    p.mkdir(parents=True, exist_ok=True)
    (p / "PROJECT.md").write_text(f"# {name}\n\n## Status\nidea\n",
                                  encoding="utf-8")
    (p / "main.py").write_text("print('x')\n", encoding="utf-8")
    return p


def held(nova, conv: str, slug: str) -> str:
    """The pending proposal actually stored for this conversation+project."""
    return str(nova.runtime._pending_plan.get(conv, {}).get(slug, ""))


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


async def test_each_correction_replaces_the_stored_text():
    check.section("I26 four deferred corrections, inspected after each one")
    async with boot(default_reply="Sure.") as nova:
        seed(nova, "flappy-bird")
        await nova.memory.add_fact(entity="projects", attribute="last_active",
                                   value="flappy-bird", confidence=0.99)
        conv = str(uuid4())

        steps = [
            ("Make the pipe gap wider, but not yet.", "pipe gap"),
            ("Actually make the bird fall slower instead, not yet though.",
             "fall slower"),
            ("No, add a parallax background instead, but not right now.",
             "parallax"),
            ("Sorry - add a score counter instead, but not just yet.",
             "score counter"),
        ]
        previous: list[str] = []
        for text, marker in steps:
            await nova.brain.chat(text, conversation_id=conv)
            stored = held(nova, conv, "flappy-bird")
            check(marker in stored,
                  f"after {text[:34]!r}... the stored plan is that one "
                  f"({stored[:44]!r})")
            for old in previous:
                check(old not in stored,
                      f"and no longer mentions {old!r} ({stored[:44]!r})")
            previous.append(marker)

        check(len(nova.runtime._pending_plan.get(conv, {})) == 1,
              f"exactly one proposal is held for this conversation "
              f"({nova.runtime._pending_plan.get(conv, {}).keys()})")

        spy = ImproveSpy(nova)
        try:
            await nova.brain.chat("Go ahead.", conversation_id=conv)
            await settle(nova)
            check(len(spy.calls) == 1, f"one change ran ({len(spy.calls)})")
            ran = spy.calls[0][1] if spy.calls else ""
            check("score counter" in ran,
                  f"the newest one ({ran[:50]!r})")
            for old in ("pipe gap", "fall slower", "parallax"):
                check(old not in ran,
                      f"and not the superseded {old!r} ({ran[:50]!r})")
            check(not held(nova, conv, "flappy-bird"),
                  "and the proposal is consumed, not left to run twice")
        finally:
            spy.restore()


async def test_withdrawal_then_a_new_correction():
    check.section("I8 withdraw, then propose again")
    async with boot(default_reply="Sure.") as nova:
        seed(nova, "flappy-bird")
        await nova.memory.add_fact(entity="projects", attribute="last_active",
                                   value="flappy-bird", confidence=0.99)
        conv = str(uuid4())

        await nova.brain.chat("Make the pipe gap wider, but not yet.",
                              conversation_id=conv)
        check("pipe gap" in held(nova, conv, "flappy-bird"), "a plan is held")

        await nova.brain.chat("Actually, don't change the pipe gap.",
                              conversation_id=conv)
        check(not held(nova, conv, "flappy-bird"),
              f"the withdrawal cleared it ({held(nova, conv, 'flappy-bird')!r})")

        await nova.brain.chat("Add a parallax background, but not yet.",
                              conversation_id=conv)
        stored = held(nova, conv, "flappy-bird")
        check("parallax" in stored, f"a new plan is held ({stored[:40]!r})")
        check("pipe gap" not in stored,
              "and the withdrawn one did not come back with it")

        spy = ImproveSpy(nova)
        try:
            await nova.brain.chat("Go ahead.", conversation_id=conv)
            await settle(nova)
            check(len(spy.calls) == 1 and "parallax" in spy.calls[0][1],
                  f"only the new plan ran ({spy.calls})")
        finally:
            spy.restore()


async def test_two_conversations_do_not_share_proposals():
    check.section("I28 concurrent conversations hold their own plans")
    async with boot(default_reply="Sure.") as nova:
        seed(nova, "flappy-bird")
        await nova.memory.add_fact(entity="projects", attribute="last_active",
                                   value="flappy-bird", confidence=0.99)
        conv_a, conv_b = str(uuid4()), str(uuid4())

        await nova.brain.chat("Make the pipe gap wider, but not yet.",
                              conversation_id=conv_a)
        await nova.brain.chat("Add a parallax background, but not yet.",
                              conversation_id=conv_b)

        check("pipe gap" in held(nova, conv_a, "flappy-bird"),
              f"A holds its own ({held(nova, conv_a, 'flappy-bird')[:40]!r})")
        check("parallax" in held(nova, conv_b, "flappy-bird"),
              f"B holds its own ({held(nova, conv_b, 'flappy-bird')[:40]!r})")
        check("parallax" not in held(nova, conv_a, "flappy-bird"),
              "and A did not acquire B's")
        check("pipe gap" not in held(nova, conv_b, "flappy-bird"),
              "and B did not acquire A's")

        spy = ImproveSpy(nova)
        try:
            # Approving in A must run A's plan, and must leave B's alone.
            await nova.brain.chat("Go ahead.", conversation_id=conv_a)
            await settle(nova)
            check(len(spy.calls) == 1 and "pipe gap" in spy.calls[0][1],
                  f"A's approval ran A's plan ({spy.calls})")
            check("parallax" in held(nova, conv_b, "flappy-bird"),
                  f"and B's is still pending ({held(nova, conv_b, 'flappy-bird')[:40]!r})")
        finally:
            spy.restore()


async def test_two_projects_do_not_share_correction_state():
    check.section("I28 a plan is filed against the project it names")
    async with boot(default_reply="Sure.") as nova:
        seed(nova, "flappy-bird")
        seed(nova, "blog")
        await nova.memory.add_fact(entity="projects", attribute="last_active",
                                   value="flappy-bird", confidence=0.99)
        conv = str(uuid4())

        await nova.brain.chat("Make the pipe gap wider, but not yet.",
                              conversation_id=conv)

        # A proposal for the OTHER project, made while it is the current one.
        #
        # Stage 15 recorded a leading target phrase -- "For blog, add a
        # sidebar, but not yet." -- as a shape the grammar did not support: it
        # hid the imperative exactly as "No," used to, and the proposal was
        # dropped. Marcus approved supporting it on 2026-11-07. It counts only
        # when the phrase names the project the turn resolved, which is what
        # these two pin; test_a_fronted_target_files_against_that_project
        # drives it through a real turn.
        check(carries_a_proposal("For blog, add a sidebar, but not yet.",
                                 project="blog"),
              "a leading target naming blog carries blog's proposal")
        check(not carries_a_proposal("For blog, add a sidebar, but not yet."),
              "and with no project resolved, the phrase is not a target")
        check(not carries_a_proposal("For blog, add a sidebar, but not yet.",
                                     project="flappy-bird"),
              "nor is it flappy-bird's when it names blog")
        check(carries_a_proposal(
                  "For the blog project, add a sidebar, but not yet.",
                  project="blog"),
              "'the blog project' names blog")
        check(carries_a_proposal("So, for blog, add a sidebar, but not yet.",
                                 project="blog"),
              "behind a lead-in too")
        check(not carries_a_proposal(
                  "For blog, I changed the menu yesterday, but don't change "
                  "anything yet.", project="blog"),
              "and every veto still applies behind it: a retrospective "
              "proposes nothing")
        check(leading_target("So, for blog, add a sidebar") == "blog"
              and leading_target("No, in flappy-bird, make it faster")
              == "flappy-bird"
              and leading_target("Add a sidebar") == "",
              "leading_target finds the phrase behind a lead-in or a "
              "correction, and nothing where there is none")

        await nova.memory.add_fact(entity="projects", attribute="last_active",
                                   value="blog", confidence=0.99)
        await nova.brain.chat("Add a sidebar, but not yet.",
                              conversation_id=conv)
        await nova.memory.add_fact(entity="projects", attribute="last_active",
                                   value="flappy-bird", confidence=0.99)

        fb = held(nova, conv, "flappy-bird")
        bl = held(nova, conv, "blog")
        check("pipe gap" in fb,
              f"flappy-bird still holds its own plan ({fb[:40]!r})")
        check("sidebar" in bl,
              f"and blog holds the one that named it ({bl[:40]!r})")
        check("sidebar" not in fb and "pipe gap" not in bl,
              "neither acquired the other's")

        spy = ImproveSpy(nova)
        try:
            # The pointer decides which one an unqualified approval runs.
            await nova.brain.chat("Go ahead.", conversation_id=conv)
            await settle(nova)
            check(len(spy.calls) == 1,
                  f"one change ran ({len(spy.calls)})")
            check(spy.calls and spy.calls[0][0] == "flappy-bird",
                  f"against the CURRENT project ({spy.calls})")
            check(spy.calls and "pipe gap" in spy.calls[0][1],
                  f"with its own plan ({spy.calls})")
            check("sidebar" in held(nova, conv, "blog"),
                  "and blog's plan is untouched, still waiting")
        finally:
            spy.restore()


async def test_a_fronted_target_files_against_that_project():
    check.section("I28 'For blog, ...' files the plan against blog")
    async with boot(default_reply="Sure.") as nova:
        seed(nova, "flappy-bird")
        seed(nova, "blog")
        await nova.memory.add_fact(entity="projects", attribute="last_active",
                                   value="flappy-bird", confidence=0.99)
        conv = str(uuid4())

        await nova.brain.chat("Make the pipe gap wider, but not yet.",
                              conversation_id=conv)
        # Said while flappy-bird is current.
        await nova.brain.chat("For blog, add a sidebar, but not yet.",
                              conversation_id=conv)
        fb, bl = held(nova, conv, "flappy-bird"), held(nova, conv, "blog")
        check("sidebar" in bl, f"blog holds the sidebar plan ({bl[:50]!r})")
        check("pipe gap" in fb and "sidebar" not in fb,
              f"and flappy-bird keeps its own, unchanged ({fb[:50]!r})")

        # A second project named later in the sentence does not win: the
        # fronted phrase says where the change is FOR. Without that, the
        # longer slug is the one the resolver returns.
        await nova.brain.chat(
            "For blog, add a footer like flappy-bird's, but not yet.",
            conversation_id=conv)
        fb, bl = held(nova, conv, "flappy-bird"), held(nova, conv, "blog")
        check("footer" in bl, f"blog's plan is now the footer ({bl[:50]!r})")
        check("footer" not in fb and "pipe gap" in fb,
              f"and flappy-bird's is still the pipe gap ({fb[:50]!r})")

        # A correction in front of the target: blog's plan is replaced.
        await nova.brain.chat(
            "No, for blog, add a search box instead, but not yet.",
            conversation_id=conv)
        bl = held(nova, conv, "blog")
        check("search box" in bl and "footer" not in bl,
              f"'No, for blog, ...' replaces blog's plan ({bl[:50]!r})")

        spy = ImproveSpy(nova)
        try:
            await nova.brain.chat("Go ahead.", conversation_id=conv)
            await settle(nova)
            check(len(spy.calls) == 1 and spy.calls[0][0] == "flappy-bird"
                  and "pipe gap" in spy.calls[0][1],
                  f"approving on flappy-bird runs flappy-bird's plan "
                  f"({[(s, i[:30]) for s, i in spy.calls]})")
            check("search box" in held(nova, conv, "blog"),
                  "and blog's waits for blog")

            await nova.memory.add_fact(entity="projects",
                                       attribute="last_active",
                                       value="blog", confidence=0.99)
            await nova.brain.chat("Go ahead.", conversation_id=conv)
            await settle(nova)
            check(len(spy.calls) == 2 and spy.calls[1][0] == "blog"
                  and "search box" in spy.calls[1][1],
                  f"on blog, the approval runs blog's plan "
                  f"({[(s, i[:30]) for s, i in spy.calls]})")
        finally:
            spy.restore()


async def test_a_fronted_target_that_is_not_a_project_files_nothing():
    check.section("I28 'For the blog, ...' with no blog files nothing")
    async with boot(default_reply="Sure.") as nova:
        seed(nova, "flappy-bird")
        await nova.memory.add_fact(entity="projects", attribute="last_active",
                                   value="flappy-bird", confidence=0.99)
        conv = str(uuid4())
        # There is no blog project. Reading "For the blog," as a target that
        # resolves to nothing, and then filing the sidebar against the CURRENT
        # project, would be the wrong-project defect Stage 14 exists for.
        await nova.brain.chat("For the blog, add a sidebar, but not yet.",
                              conversation_id=conv)
        check(not held(nova, conv, "flappy-bird"),
              f"nothing is filed against flappy-bird "
              f"({held(nova, conv, 'flappy-bird')[:50]!r})")
        check(not nova.runtime._pending_plan.get(conv),
              f"or anywhere else ({nova.runtime._pending_plan.get(conv)})")


async def test_a_restart_leaves_no_pending_text_anywhere():
    check.section("I27 pending proposals do not survive a process")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        conv = str(uuid4())
        async with boot(root=root, default_reply="Sure.") as nova:
            seed(nova, "flappy-bird")
            await nova.memory.add_fact(entity="projects",
                                       attribute="last_active",
                                       value="flappy-bird", confidence=0.99)
            await nova.brain.chat("Make the pipe gap wider, but not yet.",
                                  conversation_id=conv)
            check("pipe gap" in held(nova, conv, "flappy-bird"),
                  "the first life holds a plan")

        async with boot(root=root, default_reply="Sure.") as nova2:
            check(not nova2.runtime._pending_plan,
                  f"the second life holds none at all "
                  f"({nova2.runtime._pending_plan})")
            spy = ImproveSpy(nova2)
            try:
                await nova2.brain.chat("Go ahead.", conversation_id=conv)
                await settle(nova2)
                check(not spy.calls,
                      f"and an approval runs nothing ({spy.calls})")
            finally:
                spy.restore()


async def main() -> None:
    await test_each_correction_replaces_the_stored_text()
    await test_withdrawal_then_a_new_correction()
    await test_two_conversations_do_not_share_proposals()
    await test_two_projects_do_not_share_correction_state()
    await test_a_fronted_target_files_against_that_project()
    await test_a_fronted_target_that_is_not_a_project_files_nothing()
    await test_a_restart_leaves_no_pending_text_anywhere()
    check.finish()


if __name__ == "__main__":
    run(main)
