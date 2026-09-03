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

from core.project_intent import carries_a_proposal  # noqa: E402

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
        # RECORDED LIMITATION, measured rather than assumed. A leading target
        # phrase -- "For blog, add a sidebar, but not yet." -- is NOT stored
        # against blog, and is not stored at all: the affirmative grammar is
        # anchored at the start of the sentence, so "For blog," hides the
        # imperative behind it exactly as "No," used to. That is a sentence
        # SHAPE the grammar has never supported, not a correction opener, and
        # teaching it one would be broadening the grammar to fit a test. It is
        # left unsupported and written down here instead.
        check(not carries_a_proposal("For blog, add a sidebar, but not yet."),
              "a leading target phrase is not a proposal (known limitation)")

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
    await test_a_restart_leaves_no_pending_text_anywhere()
    check.finish()


if __name__ == "__main__":
    run(main)
