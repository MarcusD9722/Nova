"""A decision applies once, however many times it is delivered (13B closure).

WHAT THE STAGE 13B REPORT SAID, AND WHY THAT WAS NOT ENOUGH

    "apply_tool_decision's idempotency rests on the claim being exclusive
     rather than on its own statement"

True, and recorded rather than fixed, on the grounds that no caller invokes it
twice. "We currently call it once" is not an idempotency guarantee: it is an
argument about today's callers, and `MemoryUnifier.apply_tool_decision` is a
public method.

Measured on 5729e36, calling it twice for the SAME claimed decide task:

    3 tasks -> 5 tasks

a duplicate `demo.ok` AND a duplicate `__decide__` continuation, both queued
and both runnable. The gate above it is `status='active' AND generation=?` on
the GOAL, which for a tool decision is an `active -> active` touch - it passes
just as happily the second time - and the inserted ids are fresh per call, so
nothing collided.

THE FIX is one line in the place all four apply_* methods already shared:
`_complete_decision_task` now requires the decide task to still be `running`
and reports whether it was. A decision applies only while the task that decided
it is still the running one - the same rule the completion fence enforces one
layer down.

BOTH HALVES ARE TESTED, as the supplement asks: the boundary directly (call it
twice), and the upstream exclusivity that used to be the only thing holding it
up (two claimants cannot hold the same decide task).

Run:  venv\\Scripts\\python.exe tests\\test_decision_idempotency_s13b.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, run  # noqa: E402

from memory.unifier import MemoryUnifier  # noqa: E402

check = Checks()

P = "flappy-bird"


async def _mem(tmp: Path) -> MemoryUnifier:
    m = MemoryUnifier(tmp, enable_chroma=False)
    await m.initialize()
    return m


async def _claimed_decide(m):
    goal_id = await m.create_goal(project_name=P, title="add a pause menu",
                                  objective="pause menu",
                                  success_criteria="it pauses")
    await m.enqueue_goal_task(goal_id=goal_id, project_name=P,
                              tool_name="__decide__", args={})
    c = await m.claim_next_goal_task()
    return goal_id, str(c["task_id"]), int(c["generation"])


async def _rows(m, goal_id):
    return await m.list_goal_tasks(goal_id=str(goal_id), limit=50)


def _shape(rows):
    return sorted((str(r.get("tool_name")), str(r.get("status"))) for r in rows)


async def test_a_tool_decision_applies_once():
    check.section("idempotency: a repeated tool decision schedules once")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        m = await _mem(Path(td) / "a")
        goal_id, task_id, gen = await _claimed_decide(m)

        first = await m.apply_tool_decision(
            goal_id=goal_id, project_name=P, expected_generation=gen,
            task_id=task_id, tool_name="demo.ok", args={})
        after_first = _shape(await _rows(m, goal_id))
        check(first is True, f"the first application lands ({first})")
        check(("demo.ok", "queued") in after_first,
              f"scheduling the tool ({after_first})")

        second = await m.apply_tool_decision(
            goal_id=goal_id, project_name=P, expected_generation=gen,
            task_id=task_id, tool_name="demo.ok", args={})
        after_second = _shape(await _rows(m, goal_id))
        check(second is False, f"the repeat is refused ({second})")
        check(after_second == after_first,
              f"and nothing was added ({len(after_first)} -> {len(after_second)})")
        check(len([r for r in after_second if r[0] == "demo.ok"]) == 1,
              f"exactly one tool is scheduled ({after_second})")
        check(len([r for r in after_second
                   if r[0] == "__decide__" and r[1] == "queued"]) == 1,
              f"and exactly one continuation ({after_second})")


async def test_the_other_three_decisions_apply_once_too():
    """The fix is in the shared helper, so all four are covered - proved, not
    assumed."""
    check.section("idempotency: question, final and budget-pause too")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        m = await _mem(Path(td) / "b")

        # question -> pauses the goal and opens a proposal
        goal_id, task_id, gen = await _claimed_decide(m)
        a1 = await m.apply_question_decision(
            goal_id=goal_id, project_name=P, expected_generation=gen,
            task_id=task_id, message="Dark overlay or a blur?")
        a2 = await m.apply_question_decision(
            goal_id=goal_id, project_name=P, expected_generation=gen,
            task_id=task_id, message="Dark overlay or a blur?")
        props = await m.list_proposals(project_name=P, limit=20) \
            if hasattr(m, "list_proposals") else []
        check(a1 is True and a2 is False,
              f"a question applies once ({a1} then {a2})")
        if props:
            check(len(props) == 1,
                  f"leaving exactly one proposal ({len(props)})")

        # final -> completes the goal
        goal2, task2, gen2 = await _claimed_decide(m)
        b1 = await m.apply_final_decision(
            goal_id=goal2, project_name=P, expected_generation=gen2,
            task_id=task2, message="done")
        b2 = await m.apply_final_decision(
            goal_id=goal2, project_name=P, expected_generation=gen2,
            task_id=task2, message="done")
        check(b1 is True and b2 is False,
              f"a final applies once ({b1} then {b2})")

        # the budget pause
        goal3, task3, gen3 = await _claimed_decide(m)
        c1 = await m.apply_step_budget_pause(
            goal_id=goal3, project_name=P, expected_generation=gen3,
            task_id=task3, message="paused after too many steps")
        c2 = await m.apply_step_budget_pause(
            goal_id=goal3, project_name=P, expected_generation=gen3,
            task_id=task3, message="paused after too many steps")
        check(c1 is True and c2 is False,
              f"a budget pause applies once ({c1} then {c2})")


async def test_the_upstream_exclusivity_is_still_load_bearing():
    """The property that USED to be the only thing holding this up.

    It is still true and still worth pinning: if the claim ever stopped being
    exclusive, two supervisors would each hold the same `__decide__` and each
    apply its own decision. The fence below now catches that too, but a test
    that only checked the fence would let the claim rot silently.
    """
    check.section("exclusivity: two claimants cannot hold one decide task")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        m = await _mem(Path(td) / "c")
        goal_id = await m.create_goal(project_name=P, title="t",
                                      objective="o", success_criteria="c")
        await m.enqueue_goal_task(goal_id=goal_id, project_name=P,
                                  tool_name="__decide__", args={})

        # Eight racing claims for one runnable row.
        claims = await asyncio.gather(*[m.claim_next_goal_task()
                                        for _ in range(8)])
        got = [c for c in claims if c]
        ids = {str(c["task_id"]) for c in got}
        check(len(got) == 1,
              f"exactly one claimant wins ({len(got)} of 8)")
        check(len(ids) == 1, f"and they cannot share a row ({len(ids)})")

        # And the loser's decision, applied with the same ids, is refused.
        winner = got[0]
        ok1 = await m.apply_tool_decision(
            goal_id=goal_id, project_name=P,
            expected_generation=int(winner["generation"]),
            task_id=str(winner["task_id"]), tool_name="demo.ok", args={})
        ok2 = await m.apply_tool_decision(
            goal_id=goal_id, project_name=P,
            expected_generation=int(winner["generation"]),
            task_id=str(winner["task_id"]), tool_name="demo.ok", args={})
        check(ok1 is True and ok2 is False,
              f"and a second application of it is refused ({ok1}, {ok2})")


async def test_a_normal_run_still_makes_progress():
    """COUNTER-TEST. A fence that refuses everything would pass the above."""
    check.section("counter: consecutive decisions still advance the goal")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        m = await _mem(Path(td) / "d")
        goal_id, task_id, gen = await _claimed_decide(m)

        applied = await m.apply_tool_decision(
            goal_id=goal_id, project_name=P, expected_generation=gen,
            task_id=task_id, tool_name="demo.ok", args={})
        check(applied is True, f"step one applies ({applied})")

        # The continuation is claimed and decides again - a different task, so
        # it must be allowed.
        nxt = await m.claim_next_goal_task()
        while nxt and str(nxt.get("tool_name")) != "__decide__":
            await m.complete_goal_task(
                task_id=str(nxt["task_id"]), status="done",
                result={"ok": True}, error="",
                expected_generation=int(nxt["generation"]))
            nxt = await m.claim_next_goal_task()
        check(nxt is not None, "the continuation is claimable")
        again = await m.apply_tool_decision(
            goal_id=goal_id, project_name=P,
            expected_generation=int(nxt["generation"]),
            task_id=str(nxt["task_id"]), tool_name="demo.ok", args={})
        check(again is True,
              f"and the NEXT decision applies normally ({again})")
        rows = _shape(await _rows(m, goal_id))
        check(len([r for r in rows if r[0] == "demo.ok"]) == 2,
              f"two steps were scheduled across two decisions ({rows})")


async def main():
    await test_a_tool_decision_applies_once()
    await test_the_other_three_decisions_apply_once_too()
    await test_the_upstream_exclusivity_is_still_load_bearing()
    await test_a_normal_run_still_makes_progress()
    check.finish()


if __name__ == "__main__":
    run(main)
