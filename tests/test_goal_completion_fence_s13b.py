"""A stale generation must not write a terminal outcome (Stage 13B).

INVARIANTS UNDER TEST

  I9   Cancelled work never resumes or completes merely because a worker wakes.
  I14  A failed action is never marked successful.
  I19  Generation fencing prevents stale workers from mutating current state.
  I20  A stale completion from run N cannot mark run N+1's work complete.
  I46  Repeated completion callbacks are idempotent.

THE SEAM

The goal-task lifecycle was fenced in three places and unfenced in a fourth:

    claim_next_task     t.status='queued' AND g.status='active'
                        AND t.generation = g.generation
    bump_task_attempt   ... AND status='running' AND generation=?
                        AND the goal is active on that run
    update_goal_status  optional expected_generation, checked IN the UPDATE
    complete_task       UPDATE tasks SET status=? WHERE task_id=?     <-- none

So a worker holding a task from run N could write a terminal status at any later
moment: after the user cancelled, after the goal moved to run N+1, or twice.

WHAT THE FENCE DECIDES

    applied     the caller still owns the run; its status is written.
    superseded  the row is running but the run ended underneath it. The work
                may genuinely have succeeded, so the outcome is kept in
                result_json and last_error -- but the reported STATUS is not
                written, because "what completed?" must not count work
                belonging to a run the user stopped. It resolves to `failed`,
                the label this codebase already uses for a step whose run
                ended (see `_discard_stale_decision`).
    ignored     the row is already terminal. The first write wins.

A pause is deliberately NOT a supersession: it does not bump the generation and
does not invalidate work already in flight, so a tool that finished during a
pause finished honestly and resume must not redo it.

MEASUREMENT. Every assertion reads the authoritative `tasks` row and reports
goal_id / task_id / generation / attempts / status. Nothing is attributed by
ordering, position or timing.

COUNTER-TESTS. `test_a_live_run_still_completes_normally` and the
`applied` assertions exist so that a "fix" which simply refuses every
completion, or cancels everything, cannot pass this suite.

Run:  venv\\Scripts\\python.exe tests\\test_goal_completion_fence_s13b.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, ScriptedLLM, run  # noqa: E402

from core.agent_supervisor import (AgentSupervisor,  # noqa: E402
                                   SupervisorConfig)
from core.tool_router import ToolRouter  # noqa: E402

from memory.unifier import MemoryUnifier  # noqa: E402

check = Checks()


async def _mem(tmp: Path) -> MemoryUnifier:
    m = MemoryUnifier(tmp, enable_chroma=False)
    await m.initialize()
    return m


async def _goal_with_running_task(m, *, project="flappy-bird"):
    """A goal with one task claimed and running, exactly as the supervisor does."""
    goal_id = await m.create_goal(project_name=project, title="add a pause menu",
                                  objective="pause menu", success_criteria="it pauses")
    await m.enqueue_goal_task(goal_id=goal_id, project_name=project,
                              tool_name="code.write", args={"path": "game.js"})
    claimed = await m.claim_next_goal_task()
    return goal_id, claimed


async def _task_row(m, task_id: str) -> dict:
    for t in await m.list_goal_tasks(limit=50):
        if str(t.get("task_id")) == str(task_id):
            return t
    return {}


async def _goal_row(m, goal_id) -> dict:
    return await m.get_goal(goal_id=goal_id) or {}


def _describe(task: dict, goal: dict) -> str:
    return (f"task={str(task.get('task_id'))[:8]} gen={task.get('generation')} "
            f"attempts={task.get('attempts')} status={task.get('status')!r} "
            f"| goal gen={goal.get('generation')} status={goal.get('status')!r}")


def _result(task: dict) -> dict:
    try:
        return json.loads(task.get("result_json") or "{}")
    except Exception:
        return {}


async def test_a_live_run_still_completes_normally():
    """COUNTER-TEST. The fence must not simply refuse everything."""
    check.section("counter: an owned run completes exactly as before")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        m = await _mem(Path(td) / "ok")
        goal_id, claimed = await _goal_with_running_task(m)
        task_id = str(claimed["task_id"])
        gen = int(claimed.get("generation") or 0)

        outcome = await m.complete_goal_task(
            task_id=task_id, status="done", result={"ok": True, "data": {"n": 1}},
            error="", expected_generation=gen)
        row, goal = await _task_row(m, task_id), await _goal_row(m, goal_id)
        check(outcome == "applied", f"the fence applies it ({outcome!r})")
        check(str(row.get("status")) == "done",
              f"and the task is done ({_describe(row, goal)})")
        check(_result(row).get("ok") is True,
              f"with the tool's own result kept ({_result(row)})")

        # A genuine failure on a live run is still recorded as a failure.
        goal2, claimed2 = await _goal_with_running_task(m, project="calc-tool")
        t2, g2 = str(claimed2["task_id"]), int(claimed2.get("generation") or 0)
        o2 = await m.complete_goal_task(task_id=t2, status="failed", result={},
                                        error="disk full", expected_generation=g2)
        r2 = await _task_row(m, t2)
        check(o2 == "applied" and str(r2.get("status")) == "failed",
              f"a live failure is still 'failed' ({o2!r}/{r2.get('status')!r})")
        check("disk full" in str(r2.get("last_error") or ""),
              f"with its error ({r2.get('last_error')!r})")


async def test_a_cancelled_run_cannot_be_completed_by_its_stale_worker():
    check.section("fence: a stale worker cannot complete a cancelled run")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        m = await _mem(Path(td) / "a")
        goal_id, claimed = await _goal_with_running_task(m)
        task_id = str(claimed["task_id"])
        gen_at_claim = int(claimed.get("generation") or 0)

        # The user cancels. Cancel bumps the goal to run N+1 and stops QUEUED
        # work; a task already claimed stays 'running', because a tool that is
        # already executing cannot be un-executed.
        await m.cancel_goal(goal_id=goal_id)
        goal = await _goal_row(m, goal_id)
        check(str(goal.get("status")) == "cancelled",
              f"the goal is cancelled ({goal.get('status')!r})")
        check(int(goal.get("generation") or 0) == gen_at_claim + 1,
              f"and the run advanced to {goal.get('generation')} "
              f"(claimed under {gen_at_claim})")
        mid = await _task_row(m, task_id)
        check(str(mid.get("status")) == "running",
              f"the in-flight task is still running ({_describe(mid, goal)})")

        # The stale worker from run N now returns success.
        outcome = await m.complete_goal_task(
            task_id=task_id, status="done", result={"ok": True}, error="",
            expected_generation=gen_at_claim)

        row = await _task_row(m, task_id)
        goal = await _goal_row(m, goal_id)
        check(outcome == "superseded", f"the fence supersedes it ({outcome!r})")
        check(str(row.get("status")) != "done",
              f"the cancelled run's task is NOT recorded as done "
              f"({_describe(row, goal)})")
        check(str(row.get("status")) == "superseded",
              f"it is finalised as superseded, which is neither done nor a "
              f"failure of the work ({_describe(row, goal)})")
        check(str(row.get("status")) != "running",
              f"and not stranded in running ({_describe(row, goal)})")
        check(str(goal.get("status")) == "cancelled",
              f"the goal is still cancelled ({_describe(row, goal)})")
        # The truth about what actually happened is not thrown away.
        res = _result(row)
        check(res.get("superseded") is True and res.get("reported_status") == "done",
              f"the real outcome is kept, not counted ({res})")


async def test_a_stale_failure_cannot_overwrite_a_newer_run():
    check.section("fence: a stale failure does not touch the resumed run")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        m = await _mem(Path(td) / "b")
        goal_id, claimed = await _goal_with_running_task(m)
        task_id = str(claimed["task_id"])
        gen_at_claim = int(claimed.get("generation") or 0)

        await m.cancel_goal(goal_id=goal_id)
        await m.resume_goal(goal_id=goal_id)
        goal = await _goal_row(m, goal_id)
        current_gen = int(goal.get("generation") or 0)
        check(str(goal.get("status")) == "active",
              f"the goal is active again ({goal.get('status')!r})")
        check(current_gen != gen_at_claim,
              f"on a later run ({current_gen} vs {gen_at_claim})")

        # The run-N worker finally fails, long after its run ended.
        outcome = await m.complete_goal_task(
            task_id=task_id, status="failed", result={}, error="network died",
            expected_generation=gen_at_claim)

        row = await _task_row(m, task_id)
        goal = await _goal_row(m, goal_id)
        check(outcome == "superseded", f"the fence supersedes it ({outcome!r})")
        check(int(row.get("generation") or 0) == gen_at_claim,
              f"the row still belongs to run {gen_at_claim} "
              f"({_describe(row, goal)})")
        check(int(goal.get("generation") or 0) == current_gen,
              f"the current run is unchanged ({_describe(row, goal)})")
        check(str(goal.get("status")) == "active",
              f"and still active ({_describe(row, goal)})")

        # The resumed run's own continuation must still be runnable.
        nxt = await m.claim_next_goal_task()
        check(nxt is not None, "the resumed run still has claimable work")
        if nxt:
            check(str(nxt.get("task_id")) != task_id,
                  f"and it is NOT the superseded task ({str(nxt.get('task_id'))[:8]})")
            check(int(nxt.get("generation") or 0) == current_gen,
                  f"it is on the CURRENT run ({nxt.get('generation')} "
                  f"vs {current_gen})")


async def test_naming_the_current_run_cannot_finish_an_older_ones_task():
    """The task-run half of the fence, on its own.

    M35 survived without this: in every other scenario the task's run and the
    goal's run went stale TOGETHER, so either half of the predicate alone
    caught it and neither was proved load-bearing.

    Here they diverge. The goal is active on run N+1, so the goal half says
    "yes"; the task still belongs to run N, so only the task half can refuse.
    A caller that reports the goal's CURRENT run for a task it claimed under an
    older one is exactly the mistake this codebase already made once, when the
    generation was read from the goal instead of from the task row.
    """
    check.section("fence: the run must be the TASK's, not the goal's latest")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        m = await _mem(Path(td) / "g")
        goal_id, claimed = await _goal_with_running_task(m)
        task_id = str(claimed["task_id"])
        gen_at_claim = int(claimed.get("generation") or 0)

        await m.cancel_goal(goal_id=goal_id)
        await m.resume_goal(goal_id=goal_id)
        goal = await _goal_row(m, goal_id)
        current_gen = int(goal.get("generation") or 0)
        row = await _task_row(m, task_id)
        check(str(goal.get("status")) == "active" and current_gen != gen_at_claim,
              f"the goal is active on a LATER run ({_describe(row, goal)})")
        check(int(row.get("generation")) == gen_at_claim,
              f"while the task still belongs to run {gen_at_claim} "
              f"({_describe(row, goal)})")

        # The caller names the run the GOAL is on, not the one it claimed.
        outcome = await m.complete_goal_task(
            task_id=task_id, status="done", result={"ok": True}, error="",
            expected_generation=current_gen)

        row = await _task_row(m, task_id)
        goal = await _goal_row(m, goal_id)
        check(outcome == "superseded",
              f"naming the current run does not confer ownership ({outcome!r})")
        check(str(row.get("status")) != "done",
              f"the run-{gen_at_claim} task is not completed by it "
              f"({_describe(row, goal)})")
        check(int(row.get("generation")) == gen_at_claim,
              f"and it is not relabelled onto the current run "
              f"({_describe(row, goal)})")


async def test_work_that_finished_during_a_pause_still_counts():
    check.section("fence: a pause does not invalidate work already in flight")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        m = await _mem(Path(td) / "c")
        goal_id, claimed = await _goal_with_running_task(m)
        task_id = str(claimed["task_id"])
        gen = int(claimed.get("generation") or 0)

        await m.update_goal_status(goal_id=goal_id, status="paused")
        outcome = await m.complete_goal_task(
            task_id=task_id, status="done", result={"ok": True}, error="",
            expected_generation=gen)

        row = await _task_row(m, task_id)
        goal = await _goal_row(m, goal_id)
        # A pause does not bump the run, so the worker still owns it. The tool
        # genuinely finished; recording it is what stops resume redoing it.
        check(outcome == "applied", f"the completion applies ({outcome!r})")
        check(str(row.get("status")) == "done",
              f"the finished work counts ({_describe(row, goal)})")
        check(str(goal.get("status")) == "paused",
              f"and the pause is NOT undone by it ({_describe(row, goal)})")


async def test_completion_is_idempotent():
    check.section("fence: a duplicate completion callback changes nothing")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        m = await _mem(Path(td) / "d")
        goal_id, claimed = await _goal_with_running_task(m)
        task_id = str(claimed["task_id"])
        gen = int(claimed.get("generation") or 0)

        first = await m.complete_goal_task(task_id=task_id, status="failed",
                                           result={}, error="disk full",
                                           expected_generation=gen)
        row = await _task_row(m, task_id)
        check(first == "applied" and str(row.get("status")) == "failed",
              f"the first completion applied ({first!r}/{row.get('status')!r})")

        # The SAME callback delivered twice, then a contradictory one.
        dup = await m.complete_goal_task(task_id=task_id, status="failed",
                                         result={}, error="disk full",
                                         expected_generation=gen)
        contra = await m.complete_goal_task(task_id=task_id, status="done",
                                            result={"ok": True}, error="",
                                            expected_generation=gen)
        after = await _task_row(m, task_id)
        goal = await _goal_row(m, goal_id)
        check(dup == "ignored" and contra == "ignored",
              f"repeats are ignored ({dup!r}, {contra!r})")
        check(str(after.get("status")) == "failed",
              f"a repeat cannot turn a failure into success "
              f"({_describe(after, goal)})")
        check("disk full" in str(after.get("last_error") or ""),
              f"and the original error survives ({after.get('last_error')!r})")


async def test_a_cancelled_task_is_not_resurrected_by_a_late_completion():
    check.section("fence: an already-cancelled task stays cancelled")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        m = await _mem(Path(td) / "e")
        project = "calc-tool"
        goal_id = await m.create_goal(project_name=project, title="percent key",
                                      objective="add %", success_criteria="works")
        await m.enqueue_goal_task(goal_id=goal_id, project_name=project,
                                  tool_name="code.write", args={})
        # Never claimed, so cancel marks it cancelled outright.
        await m.cancel_goal(goal_id=goal_id)
        tasks = await m.list_goal_tasks(goal_id=str(goal_id), limit=10)
        target = tasks[0] if tasks else {}
        task_id = str(target.get("task_id"))
        check(str(target.get("status")) == "cancelled",
              f"the queued task was cancelled ({target.get('status')!r})")
        cancel_note = str(target.get("last_error") or "")

        outcome = await m.complete_goal_task(
            task_id=task_id, status="done", result={"ok": True}, error="",
            expected_generation=int(target.get("generation") or 0))
        row = await _task_row(m, task_id)
        goal = await _goal_row(m, goal_id)
        check(outcome == "ignored", f"the fence ignores it ({outcome!r})")
        check(str(row.get("status")) == "cancelled",
              f"and it stays cancelled ({_describe(row, goal)})")
        check(str(row.get("last_error") or "") == cancel_note,
              f"the cancellation record is not overwritten "
              f"({row.get('last_error')!r})")


async def test_a_task_list_says_which_run_a_row_belongs_to():
    check.section("truth: a task row carries its own lifecycle run")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        m = await _mem(Path(td) / "f")
        goal_id, claimed = await _goal_with_running_task(m)
        task_id = str(claimed["task_id"])
        gen = int(claimed.get("generation") or 0)
        await m.complete_goal_task(task_id=task_id, status="done",
                                   result={"ok": True}, error="",
                                   expected_generation=gen)
        row = await _task_row(m, task_id)
        # Without this column nothing that reads a task list can answer
        # "which revision was that?" -- the listing dropped it entirely.
        check(row.get("generation") is not None,
              f"the listing exposes the run ({row.get('generation')!r})")
        check(int(row.get("generation")) == gen,
              f"and it is the run the task was claimed under ({gen})")


async def test_a_tool_that_finished_after_a_cancel_is_not_announced():
    """The supervisor half of the fence: what Nova says next.

    Getting the row right is not enough. The success path announced
    "<tool> completed" unconditionally, so a tool that returned ok after the
    user cancelled produced a progress event claiming the cancelled goal had
    made progress -- which is what the user actually reads.

    The tool here blocks until the goal has been cancelled, so the ordering is
    forced rather than raced: the completion genuinely arrives after the run
    ended, every time.
    """
    check.section("supervisor: a superseded tool is not announced as progress")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        m = await _mem(Path(td) / "announce")
        project = "flappy-bird"
        entered, release = asyncio.Event(), asyncio.Event()
        ran = {"n": 0}

        async def slow_tool(_args):
            ran["n"] += 1
            entered.set()
            await release.wait()
            return {"ok": True, "wrote": "game.js"}

        llm = ScriptedLLM()
        llm.default_reply = '{"type":"done","summary":"nothing to do"}'
        sup = AgentSupervisor(
            memory=m, llm=llm, router=ToolRouter({"demo.slow": slow_tool}, {}),
            tool_descriptions={"demo.slow": "writes a file"},
            cfg=SupervisorConfig(tick_seconds=0.05, max_retries=1,
                                 max_steps_per_goal=6))

        goal_id = await m.create_goal(project_name=project, title="pause menu",
                                      objective="add a pause menu",
                                      success_criteria="it pauses")
        await m.enqueue_goal_task(goal_id=goal_id, project_name=project,
                                  tool_name="demo.slow", args={})
        sup.start()
        try:
            await asyncio.wait_for(entered.wait(), timeout=10)
            # The tool is mid-flight. Now the user cancels.
            await m.cancel_goal(goal_id=goal_id)
            goal = await _goal_row(m, goal_id)
            check(str(goal.get("status")) == "cancelled",
                  f"the goal is cancelled while the tool runs "
                  f"({goal.get('status')!r})")
            release.set()

            task_id = ""
            for _ in range(200):
                await asyncio.sleep(0.05)
                rows = await m.list_goal_tasks(goal_id=str(goal_id), limit=10)
                hit = [t for t in rows if str(t.get("tool_name")) == "demo.slow"]
                if hit and str(hit[0].get("status")) not in ("queued", "running"):
                    task_id = str(hit[0].get("task_id"))
                    break
        finally:
            await sup.stop()

        row = await _task_row(m, task_id) if task_id else {}
        goal = await _goal_row(m, goal_id)
        check(ran["n"] == 1,
              f"the tool really executed once ({ran['n']})")
        check(str(row.get("status")) == "superseded",
              f"its task is superseded, not done ({_describe(row, goal)})")

        # Attributed by goal_id, not by taking the last few events.
        events = await m.fetch_unacked_progress(project_name=project, limit=50)
        mine = [e for e in events if str(e.get("goal_id")) == str(goal_id)]
        said = [f"{e.get('kind')}:{e.get('message')}" for e in mine]
        completed = [e for e in mine
                     if str(e.get("kind")) == "tool"
                     and "completed" in str(e.get("message") or "")]
        check(not completed,
              f"nothing announces the cancelled run as progress ({said})")
        blocked = [e for e in mine if str(e.get("kind")) == "blocked"
                   and "not counted" in str(e.get("message") or "")]
        check(bool(blocked),
              f"and it is not silent either -- it says the work landed too "
              f"late ({said})")


async def main():
    await test_a_live_run_still_completes_normally()
    await test_a_cancelled_run_cannot_be_completed_by_its_stale_worker()
    await test_a_stale_failure_cannot_overwrite_a_newer_run()
    await test_naming_the_current_run_cannot_finish_an_older_ones_task()
    await test_work_that_finished_during_a_pause_still_counts()
    await test_completion_is_idempotent()
    await test_a_cancelled_task_is_not_resurrected_by_a_late_completion()
    await test_a_task_list_says_which_run_a_row_belongs_to()
    await test_a_tool_that_finished_after_a_cancel_is_not_announced()
    check.finish()


if __name__ == "__main__":
    run(main)
