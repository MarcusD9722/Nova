"""Stage 15 — task -> goal, proved at the rows rather than at the status.

Stage 13B fenced every write on this path and tested each fence. What Stage 15
asks is different: that the COUPLING between a task and the goal, project and
generation it belongs to survives the handoff, so no outcome can be applied to
work it does not belong to.

Every assertion reads the durable row -- status, attempts, generation, project,
result, error -- because "the goal ends up in the right state" is exactly the
oracle that cannot tell a correct handoff from two errors cancelling out.

  I9   execution does not imply success
  I20  failures stay associated with their real project/task/revision
  I21  cancellation prevents stale work becoming live again
  I27  stale output cannot execute after a correction
  I28  project A never modifies project B
  I36  no subsystem silently converts unknown into success

Run:  venv\\Scripts\\python.exe tests\\test_s15_handoff_task_goal.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, run  # noqa: E402

from memory.unifier import MemoryUnifier  # noqa: E402

check = Checks()


def _tmp():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


async def fresh(root: Path) -> MemoryUnifier:
    mem = MemoryUnifier(root / "memory_data", enable_chroma=False)
    await mem.initialize()
    return mem


async def a_goal(mem, project: str, title: str):
    return await mem.create_goal(project_name=project, title=title,
                                 objective=f"{title} objective")


def row_for(rows, task_id) -> dict:
    return next((r for r in rows if str(r["task_id"]) == str(task_id)), {})


async def gen_of(mem, goal_id) -> int:
    goals = await mem.list_goals(limit=50)
    g = next((g for g in goals if str(g["goal_id"]) == str(goal_id)), {})
    return int(g.get("generation", -1))


# ── coupling ───────────────────────────────────────────────────────────────

async def test_a_task_belongs_to_one_goal_and_one_project():
    check.section("I28 completing A's task leaves B's rows untouched")
    with _tmp() as td:
        mem = await fresh(Path(td))
        g_a = await a_goal(mem, "alpha", "goal A")
        g_b = await a_goal(mem, "bravo", "goal B")

        t_a = await mem.enqueue_goal_task(goal_id=g_a, project_name="alpha",
                                          tool_name="demo.one", args={"n": 1})
        t_b = await mem.enqueue_goal_task(goal_id=g_b, project_name="bravo",
                                          tool_name="demo.one", args={"n": 2})
        check(t_a and t_b, "both tasks were queued")

        before_b = row_for(await mem.list_goal_tasks(goal_id=str(g_b)), t_b)
        claimed = await mem.claim_next_goal_task()
        check(claimed is not None, "a task was claimed")
        applied = await mem.complete_goal_task(
            task_id=str(claimed["task_id"]), status="done",
            result={"ok": True}, expected_generation=int(claimed["generation"]))
        check(applied == "applied", f"and completed ({applied})")

        rows_a = await mem.list_goal_tasks(goal_id=str(g_a))
        rows_b = await mem.list_goal_tasks(goal_id=str(g_b))
        check(all(str(r["goal_id"]) == str(g_a) for r in rows_a),
              "A's task list contains only A's tasks")
        check(all(str(r["project_name"]) == "bravo" for r in rows_b),
              "and B's only B's project")

        after_b = row_for(rows_b, t_b)
        check(after_b.get("status") == before_b.get("status") == "queued",
              f"B's task is untouched ({before_b.get('status')} -> "
              f"{after_b.get('status')})")
        check(int(after_b.get("attempts", -1)) == int(before_b.get("attempts", -2)),
              "including its attempt count")


async def test_a_stale_generation_cannot_queue_or_finish_work():
    check.section("I21/I27 cancel bumps the run; the old run owns nothing")
    with _tmp() as td:
        mem = await fresh(Path(td))
        goal = await a_goal(mem, "alpha", "goal A")
        gen0 = await gen_of(mem, goal)

        task = await mem.enqueue_goal_task(goal_id=goal, project_name="alpha",
                                           tool_name="demo.one",
                                           expected_generation=gen0)
        check(task is not None, f"a task queues on the current run ({gen0})")
        claimed = await mem.claim_next_goal_task()
        check(claimed and str(claimed["task_id"]) == str(task),
              "and is claimable")

        # The user cancels. That opens a new run.
        await mem.cancel_goal(goal_id=goal)
        gen1 = await gen_of(mem, goal)
        check(gen1 == gen0 + 1, f"cancel advanced the run ({gen0} -> {gen1})")

        # 1. The old run cannot queue more work.
        late = await mem.enqueue_goal_task(goal_id=goal, project_name="alpha",
                                           tool_name="demo.two",
                                           expected_generation=gen0)
        check(late is None,
              f"a step decided in the old run is refused ({late})")

        # 2. The worker that was already running returns success. It must not
        #    be recorded as this goal having completed work.
        outcome = await mem.complete_goal_task(
            task_id=str(task), status="done", result={"ok": True},
            expected_generation=gen0)
        check(outcome == "superseded",
              f"its completion is superseded, not applied ({outcome})")

        row = row_for(await mem.list_goal_tasks(goal_id=str(goal)), task)
        check(row.get("status") != "done",
              f"and the row does NOT say done ({row.get('status')!r})")
        check("ok" in str(row.get("result") or row.get("result_json") or ""),
              f"while the result it reported is still kept "
              f"({str(row.get('result') or row.get('result_json'))[:40]!r})")


async def test_a_recorded_failure_cannot_be_overwritten_by_a_late_success():
    check.section("I36 FAILED stays FAILED")
    with _tmp() as td:
        mem = await fresh(Path(td))
        goal = await a_goal(mem, "alpha", "goal A")
        task = await mem.enqueue_goal_task(goal_id=goal, project_name="alpha",
                                           tool_name="demo.one")
        claimed = await mem.claim_next_goal_task()
        gen = int(claimed["generation"])

        first = await mem.complete_goal_task(
            task_id=str(task), status="failed", error="disk full",
            expected_generation=gen)
        check(first == "applied", f"the failure is recorded ({first})")

        second = await mem.complete_goal_task(
            task_id=str(task), status="done", result={"ok": True},
            expected_generation=gen)
        check(second == "ignored",
              f"a duplicate success callback is ignored ({second})")

        row = row_for(await mem.list_goal_tasks(goal_id=str(goal)), task)
        check(row.get("status") == "failed",
              f"the row still says failed ({row.get('status')!r})")
        check("disk full" in str(row.get("last_error") or ""),
              f"with the original error ({row.get('last_error')!r})")


async def test_an_attempt_cannot_reopen_a_finished_task():
    check.section("I9 retry metadata cannot manufacture another go")
    with _tmp() as td:
        mem = await fresh(Path(td))
        goal = await a_goal(mem, "alpha", "goal A")
        task = await mem.enqueue_goal_task(goal_id=goal, project_name="alpha",
                                           tool_name="demo.one")
        claimed = await mem.claim_next_goal_task()
        gen = int(claimed["generation"])
        await mem.complete_goal_task(task_id=str(task), status="failed",
                                     error="boom", expected_generation=gen)

        requeued = await mem.bump_goal_task_attempt(
            task_id=str(task), attempts=2, run_after_iso="1970-01-01T00:00:00Z",
            error="retrying", expected_generation=gen)
        check(requeued is False,
              f"a terminal task is not requeued ({requeued})")
        row = row_for(await mem.list_goal_tasks(goal_id=str(goal)), task)
        check(row.get("status") == "failed",
              f"and it is still failed ({row.get('status')!r})")
        check(await mem.claim_next_goal_task() is None,
              "with nothing left to claim")


async def test_a_task_whose_goal_does_not_exist_never_runs():
    """Two separate guards, and the first one hid the second.

    My first version queued an orphan task and asserted nothing was claimable.
    That passed for the wrong reason: `enqueue_goal_task` refuses an orphan at
    WRITE time and returns None, so there was no row to claim and the
    claim-time join was never exercised at all. Measured, then split in two.
    """
    check.section("I20 no authoritative goal, no execution")
    with _tmp() as td:
        mem = await fresh(Path(td))

        # 1. WRITE TIME: the row is never created.
        orphan_goal = uuid4()
        task = await mem.enqueue_goal_task(goal_id=orphan_goal,
                                           project_name="alpha",
                                           tool_name="demo.side_effect")
        check(task is None,
              f"an orphan task is refused when it is queued ({task})")
        rows = await mem.list_goal_tasks(goal_id=str(orphan_goal))
        check(not rows, f"and no row exists for it ({len(rows)})")

        # 2. CLAIM TIME: a row whose goal stops being active is skipped. This
        #    is the guard the first version claimed to test and did not.
        goal = await a_goal(mem, "alpha", "goal A")
        live = await mem.enqueue_goal_task(goal_id=goal, project_name="alpha",
                                           tool_name="demo.real")
        check(live is not None, "a task on a live goal is queued")
        await mem.cancel_goal(goal_id=goal)
        claimed = await mem.claim_next_goal_task()
        check(claimed is None,
              f"and once the goal is not active it is not claimable "
              f"({(claimed or {}).get('tool_name')})")
        row = row_for(await mem.list_goal_tasks(goal_id=str(goal)), live)
        check(row.get("status") != "running",
              f"so it never entered running ({row.get('status')!r})")

        # 3. And the claim path is genuinely live in this fixture -- otherwise
        #    step 2 would pass on a broken claim rather than on the guard.
        other = await a_goal(mem, "alpha", "goal B")
        await mem.enqueue_goal_task(goal_id=other, project_name="alpha",
                                    tool_name="demo.claimable")
        proof = await mem.claim_next_goal_task()
        check(proof is not None and str(proof["tool_name"]) == "demo.claimable",
              f"a runnable task IS claimed here ({(proof or {}).get('tool_name')})")


async def test_the_same_truth_is_read_back_by_a_new_reader():
    """Reconstruction from the store, with nothing carried in memory.

    A new `MemoryUnifier` on the same directory is a new connection and new
    caches, not a new process -- the genuine process boundary is exercised in
    the restart suite. What this pins is that the coupling lives in the ROWS.
    """
    check.section("task/goal/project/generation are read back from the store")
    with _tmp() as td:
        root = Path(td)
        mem = await fresh(root)
        goal = await a_goal(mem, "alpha", "goal A")
        task = await mem.enqueue_goal_task(goal_id=goal, project_name="alpha",
                                           tool_name="demo.one",
                                           args={"n": 7})
        claimed = await mem.claim_next_goal_task()
        gen = int(claimed["generation"])
        await mem.complete_goal_task(task_id=str(task), status="failed",
                                     error="boom", expected_generation=gen)

        later = await fresh(root)
        rows = await later.list_goal_tasks(goal_id=str(goal))
        row = row_for(rows, task)
        check(row.get("status") == "failed",
              f"the outcome is the same ({row.get('status')!r})")
        check(str(row.get("project_name")) == "alpha",
              f"against the same project ({row.get('project_name')!r})")
        check(str(row.get("goal_id")) == str(goal),
              "and the same goal")
        check(int(row.get("generation", -1)) == gen,
              f"on the same run ({row.get('generation')} vs {gen})")
        check("boom" in str(row.get("last_error") or ""),
              f"with the same error ({row.get('last_error')!r})")


async def main() -> None:
    await test_a_task_belongs_to_one_goal_and_one_project()
    await test_a_stale_generation_cannot_queue_or_finish_work()
    await test_a_recorded_failure_cannot_be_overwritten_by_a_late_success()
    await test_an_attempt_cannot_reopen_a_finished_task()
    await test_a_task_whose_goal_does_not_exist_never_runs()
    await test_the_same_truth_is_read_back_by_a_new_reader()
    check.finish()


if __name__ == "__main__":
    run(main)
