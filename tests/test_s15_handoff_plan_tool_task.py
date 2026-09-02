"""Stage 15 — planning -> tool -> task, checked at the boundary itself.

Both endpoints of these handoffs have their own passing suites. This asserts
the VALUES that cross between them: which operation ran, against which project,
with which arguments, and whether the result says what actually happened rather
than what was asked for.

The builder and the router are spied, so "the plan was executed" is a
statement about the call that was made, not about the sentence Nova produced.

  I7   planning does not silently execute
  I8   approval does not imply execution
  I9   execution does not imply success
  I26  correction supersedes prior intent
  I27  stale planner output cannot execute after correction
  I30  results identify their true origin

Run:  venv\\Scripts\\python.exe tests\\test_s15_handoff_plan_tool_task.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, boot, run  # noqa: E402

from s15_bus import Recorder  # noqa: E402
from core.tool_router import ToolCall  # noqa: E402

check = Checks()


def seed(nova, name: str) -> Path:
    p = nova.projects_dir / name
    p.mkdir(parents=True, exist_ok=True)
    (p / "PROJECT.md").write_text(f"# {name}\n\n## Status\nidea\n",
                                  encoding="utf-8")
    (p / "main.py").write_text("print('x')\n", encoding="utf-8")
    return p


class Spy:
    """Every call that crossed a boundary, with its arguments."""

    def __init__(self, nova):
        self.nova = nova
        self.improve: list[tuple[str, str]] = []
        self.start: list[tuple[str, str]] = []
        self.tools: list[tuple[str, dict]] = []
        pb, router = nova.runtime._project_builder, nova.runtime.router
        self._pb, self._router = pb, router
        self._improve, self._start = pb.improve, pb.start
        self._exec = router.execute

        async def improve(*, slug, instructions, **k):
            self.improve.append((slug, instructions))
            return await self._improve(slug=slug, instructions=instructions, **k)

        async def start(*, name, brief, **k):
            self.start.append((name, brief))
            return await self._start(name=name, brief=brief, **k)

        async def execute(call, *a, **k):
            self.tools.append((call.name, dict(call.args or {})))
            return await self._exec(call, *a, **k)

        pb.improve, pb.start, router.execute = improve, start, execute

    def restore(self) -> None:
        self._pb.improve, self._pb.start = self._improve, self._start
        self._router.execute = self._exec


async def settle(nova) -> None:
    pb = nova.runtime._project_builder
    for _ in range(120):
        if not any(pb.is_building(p) for p in pb.list_projects()):
            return
        await asyncio.sleep(0.05)


# ── planning -> tool ───────────────────────────────────────────────────────

async def test_the_approved_plan_is_the_one_that_runs():
    check.section("I26/I27 a corrected plan replaces the one it corrected")
    async with boot(default_reply="Sure.") as nova:
        seed(nova, "flappy-bird")
        await nova.memory.add_fact(entity="projects", attribute="last_active",
                                   value="flappy-bird", confidence=0.99)
        conv = str(uuid4())
        spy = Spy(nova)
        try:
            # Describe a change without authorising it.
            await nova.brain.chat(
                "Make the pipe gap wider, but don't change anything yet.",
                conversation_id=conv)
            check(not spy.improve,
                  f"describing a change executes nothing ({spy.improve})")

            # Correct it, still without authorising.
            await nova.brain.chat(
                "Actually make the bird fall slower instead, not yet though.",
                conversation_id=conv)
            check(not spy.improve,
                  f"and neither does correcting it ({spy.improve})")

            # Now approve.
            await nova.brain.chat("Go ahead.", conversation_id=conv)
            await settle(nova)

            check(len(spy.improve) == 1,
                  f"approval runs exactly one change ({len(spy.improve)})")
            slug, instructions = spy.improve[0] if spy.improve else ("", "")
            check(slug == "flappy-bird",
                  f"against the project in play ({slug!r})")
            check("fall slower" in instructions,
                  f"and it carries the CORRECTED plan ({instructions[:70]!r})")
            check("pipe gap" not in instructions,
                  f"not the one it replaced ({instructions[:70]!r})")
        finally:
            spy.restore()


async def test_an_approval_with_nothing_pending_executes_nothing():
    check.section("I8 approval does not imply execution")
    async with boot(default_reply="Sure.") as nova:
        seed(nova, "flappy-bird")
        await nova.memory.add_fact(entity="projects", attribute="last_active",
                                   value="flappy-bird", confidence=0.99)
        spy = Spy(nova)
        try:
            await nova.brain.chat("Go ahead.", conversation_id=str(uuid4()))
            await settle(nova)
            check(not spy.improve,
                  f"a bare approval with no proposal runs nothing "
                  f"({spy.improve})")
            check(not spy.start, f"and starts nothing ({spy.start})")
        finally:
            spy.restore()


async def test_a_plan_does_not_follow_a_project_switch():
    """The plan is keyed to the project it was about.

    A proposal made about A, then a switch to B, then "go ahead" must not run
    A's words against B.
    """
    check.section("a pending plan belongs to its project, not to the turn")
    async with boot(default_reply="Sure.") as nova:
        seed(nova, "flappy-bird")
        seed(nova, "blog")
        await nova.memory.add_fact(entity="projects", attribute="last_active",
                                   value="flappy-bird", confidence=0.99)
        conv = str(uuid4())
        spy = Spy(nova)
        try:
            await nova.brain.chat(
                "Make the pipe gap wider, but don't change anything yet.",
                conversation_id=conv)
            # Switch projects, then approve.
            await nova.memory.add_fact(entity="projects",
                                       attribute="last_active",
                                       value="blog", confidence=0.99)
            await nova.brain.chat("Go ahead.", conversation_id=conv)
            await settle(nova)

            ran_on_blog = [i for i in spy.improve if i[0] == "blog"]
            check(not ran_on_blog,
                  f"flappy-bird's plan did not run against blog ({ran_on_blog})")
            if spy.improve:
                check(all("pipe gap" not in i[1] for i in spy.improve),
                      f"and its words ran nowhere ({spy.improve})")
        finally:
            spy.restore()


async def test_start_build_that_becomes_an_improve_says_so():
    """A plan step that deliberately becomes a different operation.

    `project.start_build` turns into an improve when the requested name
    overlaps an existing project, on purpose -- a near-duplicate project is
    worse than an edit. The handoff requirement is not that it never
    substitutes, but that the RESULT says which operation actually ran and
    against what.
    """
    check.section("I30 a substituted operation reports itself honestly")
    async with boot(default_reply="Sure.") as nova:
        seed(nova, "flappy-bird")
        spy = Spy(nova)
        try:
            res = await nova.runtime.router.execute(ToolCall(
                name="project.start_build",
                args={"name": "flappy bird", "brief": "add a score counter"}))
            await settle(nova)

            check(res.ok, f"the tool succeeded ({res.error})")
            payload = res.result if isinstance(res.result, dict) else {}
            check(str(payload.get("mode")) == "improve",
                  f"the result says an IMPROVE ran, not a build "
                  f"({payload.get('mode')!r})")
            check(str(payload.get("project")) == "flappy-bird",
                  f"and names the project it actually touched "
                  f"({payload.get('project')!r})")
            check(spy.improve and not spy.start,
                  f"which matches the call that was really made "
                  f"(improve={len(spy.improve)}, start={len(spy.start)})")
            check(spy.improve and spy.improve[0][0] == "flappy-bird",
                  f"against the existing project ({spy.improve})")
            check(spy.improve and "score counter" in spy.improve[0][1],
                  f"carrying the brief across the substitution ({spy.improve})")
        finally:
            spy.restore()


# ── tool -> task ───────────────────────────────────────────────────────────

async def test_tool_started_is_not_tool_succeeded():
    check.section("I9 'started' is a fact about a task, not about an outcome")
    async with boot(default_reply="Sure.") as nova:
        seed(nova, "flappy-bird")
        pb = nova.runtime._project_builder

        res = await nova.runtime.router.execute(ToolCall(
            name="project.improve",
            args={"name": "flappy-bird", "instructions": "make it prettier"}))
        payload = res.result if isinstance(res.result, dict) else {}
        check(res.ok and payload.get("started") is True,
              f"the tool reports it started work ({payload})")
        check(pb.is_building("flappy-bird"),
              "and a real task is running for that project")
        # The task it named is the task that exists.
        check(str(payload.get("project")) == "flappy-bird",
              f"the result names the project the task belongs to "
              f"({payload.get('project')!r})")
        await settle(nova)

        # The work has now finished, and it did NOT succeed -- the scripted
        # model returns prose, so there is nothing to apply. `started: True`
        # said nothing about that, which is the point.
        verdict = await nova.runtime.completion.evaluate(slug="flappy-bird")
        check(verdict.state != "complete",
              f"and the finished work is not complete ({verdict.state})")


async def test_a_tool_that_starts_nothing_says_started_false():
    check.section("I9 a refused start is reported as a refusal")
    async with boot(default_reply="Sure.") as nova:
        seed(nova, "flappy-bird")
        # Unknown project.
        res = await nova.runtime.router.execute(ToolCall(
            name="project.improve",
            args={"name": "no-such-thing", "instructions": "do something"}))
        payload = res.result if isinstance(res.result, dict) else {}
        check(payload.get("started") is False,
              f"started is False ({payload})")
        check("unknown" in str(payload.get("reason", "")).lower(),
              f"with a reason that names the problem ({payload.get('reason')!r})")
        check(not nova.runtime._project_builder.is_building("no-such-thing"),
              "and no task exists for it")

        # Missing arguments.
        res2 = await nova.runtime.router.execute(ToolCall(
            name="project.improve", args={"name": "flappy-bird"}))
        p2 = res2.result if isinstance(res2.result, dict) else {}
        check(p2.get("started") is False,
              f"a call with no instructions starts nothing ({p2})")
        check(not nova.runtime._project_builder.is_building("flappy-bird"),
              "and still no task")


async def test_two_starts_for_one_project_do_not_make_two_tasks():
    check.section("I16 one project, one running task")
    async with boot(default_reply="Sure.") as nova:
        seed(nova, "flappy-bird")
        pb = nova.runtime._project_builder
        rec = Recorder()
        rec.__enter__()

        first = await nova.runtime.router.execute(ToolCall(
            name="project.improve",
            args={"name": "flappy-bird", "instructions": "one"}))
        second = await nova.runtime.router.execute(ToolCall(
            name="project.improve",
            args={"name": "flappy-bird", "instructions": "two"}))
        p1 = first.result if isinstance(first.result, dict) else {}
        p2 = second.result if isinstance(second.result, dict) else {}

        check(p1.get("started") is True, f"the first started ({p1})")
        check(p2.get("started") is False,
              f"the second did not ({p2})")
        check("already building" in str(p2.get("reason", "")),
              f"and says why ({p2.get('reason')!r})")
        await settle(nova)
        started = rec.for_project("project.started", "flappy-bird")
        rec.__exit__()
        check(len(started) == 1,
              f"exactly one project.started was published ({len(started)})")


async def main() -> None:
    await test_the_approved_plan_is_the_one_that_runs()
    await test_an_approval_with_nothing_pending_executes_nothing()
    await test_a_plan_does_not_follow_a_project_switch()
    await test_start_build_that_becomes_an_improve_says_so()
    await test_tool_started_is_not_tool_succeeded()
    await test_a_tool_that_starts_nothing_says_started_false()
    await test_two_starts_for_one_project_do_not_make_two_tasks()
    check.finish()


if __name__ == "__main__":
    run(main)
