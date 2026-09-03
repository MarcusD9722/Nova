"""Stage 15 — two independent truths, and no surface allowed to speak for both.

The architecture has two authoritative axes, established by measurement rather
than assumption:

    goal/task system   did the planned WORK run?
    completion system  does the ARTIFACT satisfy the agreed criteria?

Neither derives from the other. The composition question is therefore not
"which one wins" -- it is whether any surface reports one as if it were the
whole answer.

One did. `status_text()` is what the pre-pass returns for "what's the status of
X?" and what the `project.status` tool returns, and it reported only the
completion verdict:

    completion=complete, goal=failed, task=failed
    status_text() -> "Project alpha: complete."

Zero prompts reach the model on that path -- the pre-pass answers outright --
so that sentence was the entire answer a person got about a project whose work
plan had failed.

  I10  tool/task success does not imply completion
  I19  foreground success cannot conceal background failure
  I20  failures stay associated with their real project
  I28  project A never satisfies project B
  I39  no answer is assembled from unscoped fragments

Run:  venv\\Scripts\\python.exe tests\\test_s15_axis_composition.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, boot, run  # noqa: E402

from core.completion import COMPLETE, FAILED, FAILING, PASSED  # noqa: E402

check = Checks()


async def project(nova, slug: str) -> Path:
    p = nova.projects_dir / slug
    p.mkdir(parents=True, exist_ok=True)
    (p / "main.py").write_text("def add(a, b):\n    return a + b\n",
                               encoding="utf-8")
    nova.runtime._project_builder._write_project_md(
        slug, brief="a tool that adds numbers", status="building")
    return p


async def contract(nova, slug: str, verdict: str | None):
    """A sealed contract; `verdict` None leaves it unproven."""
    svc = nova.runtime.completion
    rev = await svc.record_request(slug=slug,
                                   request_text="a tool that adds numbers")
    ids = await svc.set_criteria(slug=slug, revision=rev, criteria=[
        {"text": "adds numbers", "origin_quote": "adds numbers",
         "verify_kind": "machine"}])
    await svc.seal_contract(slug=slug, revision=rev)
    if verdict is not None:
        ctx = await svc.begin_check(slug=slug, criterion_id=ids[0])
        await svc.record_verdict(context=ctx, verdict=verdict,
                                 error="did not hold" if verdict == FAILED else "")
    return rev, ids


async def goal(nova, slug: str, title: str, *, outcome: str):
    """A goal whose task ends `done`, `failed`, or is left queued."""
    gid = await nova.memory.create_goal(project_name=slug, title=title,
                                        objective=title)
    await nova.memory.enqueue_goal_task(goal_id=gid, project_name=slug,
                                        tool_name="demo.step")
    if outcome != "queued":
        c = await nova.memory.claim_next_goal_task()
        await nova.memory.complete_goal_task(
            task_id=str(c["task_id"]), status=outcome,
            result={"ok": outcome == "done"},
            error="the generator crashed" if outcome == "failed" else "",
            expected_generation=int(c["generation"]))
        await nova.memory.update_goal_status(
            goal_id=gid, status="done" if outcome == "done" else "failed")
    return gid


async def status_of(nova, slug: str) -> str:
    return await nova.runtime._project_builder.status_text(slug)


# ── the five disagreement shapes ───────────────────────────────────────────

async def test_goal_success_with_no_contract():
    check.section("work succeeded, nothing was ever agreed")
    async with boot(default_reply="Sure.") as nova:
        await project(nova, "alpha")
        await goal(nova, "alpha", "build the adder", outcome="done")

        v = await nova.runtime.completion.evaluate(slug="alpha")
        check(v.state != COMPLETE,
              f"completion claims nothing ({v.state})")
        work = await nova.memory.describe_work_state(project_name="alpha")
        check("done" in work and "build the adder" in work,
              f"while the work record says the goal succeeded ({work[:60]!r})")

        said = await status_of(nova, "alpha")
        check("complete" not in said.split(".")[0].lower(),
              f"and the status sentence does not claim completion ({said[:70]!r})")


async def test_goal_failure_with_acceptance_complete():
    check.section("I19 the work plan failed; the artifact is proven")
    async with boot(default_reply="Sure.") as nova:
        await project(nova, "alpha")
        await contract(nova, "alpha", PASSED)
        gid = await goal(nova, "alpha", "add the adder", outcome="failed")

        v = await nova.runtime.completion.evaluate(slug="alpha")
        g = [x for x in await nova.memory.list_goals(project_name="alpha")
             if str(x["goal_id"]) == str(gid)][0]
        check(v.state == COMPLETE, f"the contract is satisfied ({v.state})")
        check(str(g["status"]) == "failed", f"and the goal failed ({g['status']})")

        said = await status_of(nova, "alpha")
        check("complete" in said.lower(),
              f"the status says the artifact is complete ({said[:60]!r})")
        check("did not all succeed" in said and "add the adder" in said,
              f"AND that the planned work failed, naming it ({said[:140]!r})")

        # The two records remain separately readable.
        work = await nova.memory.describe_work_state(project_name="alpha")
        record = await nova.runtime.completion.describe_for_chat(slug="alpha")
        check("failed" in work, "the work record still says failed")
        check("complete" in record, "and the completion record still says complete")


async def test_goal_still_running_with_acceptance_complete():
    check.section("reachable? a proven artifact with work still queued")
    async with boot(default_reply="Sure.") as nova:
        await project(nova, "alpha")
        await contract(nova, "alpha", PASSED)
        await goal(nova, "alpha", "polish the adder", outcome="queued")

        v = await nova.runtime.completion.evaluate(slug="alpha")
        g = (await nova.memory.list_goals(project_name="alpha"))[0]
        check(v.state == COMPLETE and str(g["status"]) == "active",
              f"the combination IS reachable ({v.state}, {g['status']})")

        said = await status_of(nova, "alpha")
        check("complete" in said.lower(),
              f"the artifact is reported complete ({said[:60]!r})")
        check("still planned work outstanding" in said
              and "polish the adder" in said,
              f"and the outstanding work is named too ({said[:140]!r})")


async def test_goal_success_with_acceptance_failing():
    check.section("I37 the work ran; the artifact does not satisfy the contract")
    async with boot(default_reply="Sure.") as nova:
        await project(nova, "alpha")
        await contract(nova, "alpha", FAILED)
        await goal(nova, "alpha", "build the adder", outcome="done")

        v = await nova.runtime.completion.evaluate(slug="alpha")
        check(v.state == FAILING, f"completion is failing ({v.state})")
        said = await status_of(nova, "alpha")
        check(said.lower().startswith("project alpha: failing"),
              f"and the status leads with that ({said[:50]!r})")
        check("complete" not in said.lower().split("failing")[0],
              "with no completion claim in front of it")
        check("adds numbers" in said,
              f"naming the criterion that is refuted ({said[:110]!r})")


async def test_both_axes_failing_stay_distinguishable():
    check.section("I20 two different failures, not one generic one")
    async with boot(default_reply="Sure.") as nova:
        await project(nova, "alpha")
        await contract(nova, "alpha", FAILED)
        await goal(nova, "alpha", "add the adder", outcome="failed")

        said = await status_of(nova, "alpha")
        check("failing" in said.lower(),
              f"the contract failure is stated ({said[:60]!r})")
        check("did not all succeed" in said,
              f"and the work failure separately ({said[:140]!r})")
        # The two must not be collapsed into one sentence that loses which
        # layer failed.
        check("adds numbers" in said and "add the adder" in said,
              f"each naming its own subject ({said[:150]!r})")


# ── isolation ──────────────────────────────────────────────────────────────

async def test_neither_axis_crosses_projects():
    check.section("I28 identical work and criteria in two projects")
    async with boot(default_reply="Sure.") as nova:
        for slug in ("alpha", "bravo"):
            await project(nova, slug)
        await contract(nova, "alpha", PASSED)
        await contract(nova, "bravo", None)
        await goal(nova, "alpha", "add the adder", outcome="failed")
        await goal(nova, "bravo", "add the adder", outcome="done")

        va = await nova.runtime.completion.evaluate(slug="alpha")
        vb = await nova.runtime.completion.evaluate(slug="bravo")
        check(va.state == COMPLETE and vb.state != COMPLETE,
              f"completion is per project ({va.state}, {vb.state})")

        said_a = await status_of(nova, "alpha")
        said_b = await status_of(nova, "bravo")
        check("did not all succeed" in said_a,
              "alpha reports its own failed work")
        check("did not all succeed" not in said_b,
              f"and bravo does not inherit it ({said_b[:80]!r})")
        check("complete" not in said_b.split(".")[0].lower(),
              f"nor alpha's completion ({said_b[:60]!r})")


async def test_a_correction_does_not_leave_a_stale_answer():
    check.section("I12 the old completion answer stops being current")
    async with boot(default_reply="Sure.") as nova:
        await project(nova, "alpha")
        rev1, _ = await contract(nova, "alpha", PASSED)
        await goal(nova, "alpha", "add the adder", outcome="done")
        first = await status_of(nova, "alpha")
        check("complete" in first.lower(), f"complete at rev {rev1}")

        rev2 = await nova.runtime.completion.record_request(
            slug="alpha", request_text="a tool that adds numbers and subtracts")
        v = await nova.runtime.completion.evaluate(slug="alpha")
        after = await status_of(nova, "alpha")
        check(int(v.revision) == rev2, f"the revision moved ({v.revision})")
        check(v.state != COMPLETE,
              f"and the state is no longer complete ({v.state})")
        check("complete" not in after.split(".")[0].lower(),
              f"so the status sentence stops saying it ({after[:70]!r})")


async def test_the_api_and_events_do_not_confuse_the_two_words():
    check.section("§8 `state` vs `status` across the wire")
    async with boot(default_reply="Sure.") as nova:
        await project(nova, "alpha")
        await contract(nova, "alpha", PASSED)
        await goal(nova, "alpha", "add the adder", outcome="failed")

        api = (await nova.http.get("/completion/alpha")).json()
        check(str(api.get("state")) == COMPLETE,
              f"the completion endpoint reports `state` ({api.get('state')!r})")
        check("status" not in api,
              f"and does not also carry a `status` to be confused with it "
              f"({sorted(api)})")

        goals = await nova.memory.list_goals(project_name="alpha")
        check(str(goals[0].get("status")) == "failed",
              f"while the goal row uses `status` ({goals[0].get('status')!r})")
        check("state" not in goals[0],
              f"and carries no `state` ({sorted(goals[0])})")


async def main() -> None:
    await test_goal_success_with_no_contract()
    await test_goal_failure_with_acceptance_complete()
    await test_goal_still_running_with_acceptance_complete()
    await test_goal_success_with_acceptance_failing()
    await test_both_axes_failing_stay_distinguishable()
    await test_neither_axis_crosses_projects()
    await test_a_correction_does_not_leave_a_stale_answer()
    await test_the_api_and_events_do_not_confuse_the_two_words()
    check.finish()


if __name__ == "__main__":
    run(main)
