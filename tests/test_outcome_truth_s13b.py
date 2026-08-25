"""Lifecycle and execution are two questions, not one (Stage 13B closure).

THE HOLE THIS CLOSES

Stage 13B's own report recorded, as a known limitation:

    "there is still no task status for 'ran, but its run had ended'"

`status` was carrying two independent facts at once — where a row is in its
life, and what happened to the work — so these three were indistinguishable:

    a step cancelled before it ever ran
    a step whose tool SUCCEEDED, after the user cancelled the run
    a step whose tool was interrupted mid-call and may or may not have acted

All three came out `cancelled` or `failed`. Every reader that asked "did this
happen?" got the same answer for all three, and two of those answers were
false. `failed` says the work went wrong; `cancelled` says it never ran. A
superseded success is neither.

TWO AXES

  status   queued · running · blocked · done · failed · cancelled · superseded
  outcome  pending · never_started · succeeded · failed · unknown

`superseded` is what stops work from a run the user ended counting as
completed. `outcome` is what stops that same row lying about whether the tool
ran. Neither can be derived from the other, which is exactly why one column
could not hold both.

THE SIX CASES THE CLOSURE SUPPLEMENT NAMES are the six tests below, and the
seventh asserts all of it survives a real restart — a distinction that does not
reload is not a distinction.

Run:  venv\\Scripts\\python.exe tests\\test_outcome_truth_s13b.py
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

GAME = "flappy-bird"


async def _mem(tmp: Path) -> MemoryUnifier:
    m = MemoryUnifier(tmp, enable_chroma=False)
    await m.initialize()
    return m


async def _row(m, task_id: str) -> dict:
    for t in await m.list_goal_tasks(limit=100):
        if str(t.get("task_id")) == str(task_id):
            return t
    return {}


def _axes(row: dict) -> tuple[str, str]:
    return str(row.get("status")), str(row.get("outcome"))


async def _goal_with_task(m, title="add a pause menu"):
    goal_id = await m.create_goal(project_name=GAME, title=title,
                                  objective=title, success_criteria="works")
    await m.enqueue_goal_task(goal_id=goal_id, project_name=GAME,
                              tool_name="code.write", args={})
    return goal_id


async def test_1_cancelled_before_invocation_never_started():
    check.section("1. cancelled before it ran: definitely never executed")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        m = await _mem(Path(td) / "a")
        goal_id = await _goal_with_task(m)
        rows = await m.list_goal_tasks(goal_id=str(goal_id), limit=10)
        task_id = str(rows[0]["task_id"])

        await m.cancel_goal(goal_id=goal_id)
        status, outcome = _axes(await _row(m, task_id))
        check(status == "cancelled", f"lifecycle says cancelled ({status})")
        check(outcome == "never_started",
              f"and the work provably never started ({outcome})")


async def test_2_success_after_the_run_ended_keeps_its_success():
    check.section("2. tool SUCCEEDED after cancel: history keeps the success")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        m = await _mem(Path(td) / "b")
        goal_id = await _goal_with_task(m)
        claimed = await m.claim_next_goal_task()
        task_id, gen = str(claimed["task_id"]), int(claimed.get("generation"))

        await m.cancel_goal(goal_id=goal_id)
        verdict = await m.complete_goal_task(
            task_id=task_id, status="done", result={"ok": True}, error="",
            expected_generation=gen)

        status, outcome = _axes(await _row(m, task_id))
        check(verdict == "superseded", f"the fence supersedes it ({verdict})")
        check(status == "superseded",
              f"lifecycle says superseded, not failed ({status})")
        check(outcome == "succeeded",
              f"and the tool's success is NOT rewritten ({outcome})")
        check(status != "done",
              "it still does not count as completed work for the goal")

        goal = await m.get_goal(goal_id=goal_id) or {}
        check(str(goal.get("status")) == "cancelled",
              f"the current generation is untouched ({goal.get('status')!r})")


async def test_3_failure_after_the_run_ended_keeps_its_failure():
    check.section("3. tool FAILED after cancel: history keeps the failure")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        m = await _mem(Path(td) / "c")
        goal_id = await _goal_with_task(m)
        claimed = await m.claim_next_goal_task()
        task_id, gen = str(claimed["task_id"]), int(claimed.get("generation"))

        await m.cancel_goal(goal_id=goal_id)
        await m.complete_goal_task(task_id=task_id, status="failed", result={},
                                   error="the api returned 503",
                                   expected_generation=gen)

        row = await _row(m, task_id)
        status, outcome = _axes(row)
        check(status == "superseded", f"lifecycle says superseded ({status})")
        check(outcome == "failed",
              f"and the failure is preserved as a failure ({outcome})")
        check("503" in str(row.get("last_error") or ""),
              f"with its reason ({str(row.get('last_error'))[:60]!r})")


async def test_4_an_unprovable_side_effect_is_unknown():
    check.section("4. interrupted mid-call: UNKNOWN, not succeeded, not never")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        m = await _mem(Path(td) / "d")
        task_id = str(await m.enqueue_task(
            title="upload the build", details="d", project_name=GAME,
            initiated_by_user=True))
        await m.claim_next_task()

        # What the worker's own interruption handler does when a tool was in
        # flight: it cannot prove either way, and says so.
        await m.mark_task_failed(
            task_id=task_id,
            error="interrupted while 'work.step' was running. Nova cannot tell "
                  "whether it completed.",
            outcome="unknown",
            result={"status": "interrupted_tool_unknown"})

        rows = [t for t in await m.list_tasks(limit=50)
                if str(t.get("task_id")) == task_id]
        row = rows[0] if rows else {}
        status, outcome = _axes(row)
        check(outcome == "unknown",
              f"the outcome is unknown ({outcome})")
        check(outcome != "succeeded", "it is NOT recorded as success")
        check(outcome != "never_started",
              "and NOT as something that never happened")
        check(status == "failed",
              f"while the lifecycle says it did not deliver ({status})")


async def test_5_a_stale_worker_cannot_touch_the_newer_run():
    check.section("5. a stale return leaves generation N+1 alone")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        m = await _mem(Path(td) / "e")
        goal_id = await _goal_with_task(m)
        claimed = await m.claim_next_goal_task()
        task_id, gen = str(claimed["task_id"]), int(claimed.get("generation"))

        await m.cancel_goal(goal_id=goal_id)
        await m.resume_goal(goal_id=goal_id)
        goal_before = await m.get_goal(goal_id=goal_id) or {}
        current = int(goal_before.get("generation"))

        await m.complete_goal_task(task_id=task_id, status="done",
                                   result={"ok": True}, error="",
                                   expected_generation=gen)

        row = await _row(m, task_id)
        goal_after = await m.get_goal(goal_id=goal_id) or {}
        status, outcome = _axes(row)
        check(int(row.get("generation")) == gen,
              f"the row still belongs to run {gen} ({row.get('generation')})")
        check(status == "superseded" and outcome == "succeeded",
              f"superseded, and honest about having succeeded ({status}/{outcome})")
        check(int(goal_after.get("generation")) == current,
              f"the current run is unchanged ({goal_after.get('generation')})")
        check(str(goal_after.get("status")) == "active",
              f"and still active ({goal_after.get('status')!r})")

        # And nothing about the newer run was completed by it.
        live = [t for t in await m.list_goal_tasks(goal_id=str(goal_id), limit=20)
                if int(t.get("generation")) == current]
        check(all(str(t.get("status")) != "done" for t in live),
              f"nothing on run {current} was marked done "
              f"({[(t.get('tool_name'), t.get('status')) for t in live]})")


async def test_6_the_distinction_survives_a_restart():
    check.section("6. all of it reloads: a distinction that does not survive "
                  "a restart is not one")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        store = Path(td) / "nova"
        m = await _mem(store)

        # one of each, in one store
        g1 = await _goal_with_task(m, "never ran")
        never = str((await m.list_goal_tasks(goal_id=str(g1), limit=5))[0]["task_id"])
        await m.cancel_goal(goal_id=g1)

        g2 = await _goal_with_task(m, "superseded success")
        c2 = await m.claim_next_goal_task()
        sup = str(c2["task_id"])
        await m.cancel_goal(goal_id=g2)
        await m.complete_goal_task(task_id=sup, status="done",
                                   result={"ok": True}, error="",
                                   expected_generation=int(c2.get("generation")))

        g3 = await _goal_with_task(m, "real success")
        c3 = await m.claim_next_goal_task()
        good = str(c3["task_id"])
        await m.complete_goal_task(task_id=good, status="done",
                                   result={"ok": True}, error="",
                                   expected_generation=int(c3.get("generation")))

        before = {t: _axes(await _row(m, t)) for t in (never, sup, good)}

        # A real restart: nothing carried in memory.
        del m
        m2 = await _mem(store)
        after = {t: _axes(await _row(m2, t)) for t in (never, sup, good)}

        check(after[never] == ("cancelled", "never_started"),
              f"the one that never ran reloads as such ({after[never]})")
        check(after[sup] == ("superseded", "succeeded"),
              f"the superseded success reloads intact ({after[sup]})")
        check(after[good] == ("done", "succeeded"),
              f"and the real success is still a real success ({after[good]})")
        check(after == before,
              f"nothing changed across the restart ({before} -> {after})")

        # The three are genuinely distinguishable by a reader, which is the
        # entire point.
        check(len({after[never], after[sup], after[good]}) == 3,
              f"all three read differently ({sorted(set(after.values()))})")


async def test_7_an_ordinary_success_is_untouched():
    """COUNTER-TEST. The new axis must not relabel ordinary work."""
    check.section("counter: ordinary work still reads as plain success")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        m = await _mem(Path(td) / "g")
        goal_id = await _goal_with_task(m)
        claimed = await m.claim_next_goal_task()
        verdict = await m.complete_goal_task(
            task_id=str(claimed["task_id"]), status="done",
            result={"ok": True}, error="",
            expected_generation=int(claimed.get("generation")))

        status, outcome = _axes(await _row(m, str(claimed["task_id"])))
        check(verdict == "applied", f"the completion applies ({verdict})")
        check((status, outcome) == ("done", "succeeded"),
              f"and reads as done/succeeded ({status}/{outcome})")

        # A genuine failure on a live run is still a plain failure.
        goal2 = await _goal_with_task(m, "second")
        c2 = await m.claim_next_goal_task()
        await m.complete_goal_task(task_id=str(c2["task_id"]), status="failed",
                                   result={}, error="disk full",
                                   expected_generation=int(c2.get("generation")))
        status2, outcome2 = _axes(await _row(m, str(c2["task_id"])))
        check((status2, outcome2) == ("failed", "failed"),
              f"a live failure reads as failed/failed ({status2}/{outcome2})")


async def main():
    await test_1_cancelled_before_invocation_never_started()
    await test_2_success_after_the_run_ended_keeps_its_success()
    await test_3_failure_after_the_run_ended_keeps_its_failure()
    await test_4_an_unprovable_side_effect_is_unknown()
    await test_5_a_stale_worker_cannot_touch_the_newer_run()
    await test_6_the_distinction_survives_a_restart()
    await test_7_an_ordinary_success_is_untouched()
    check.finish()


if __name__ == "__main__":
    run(main)
