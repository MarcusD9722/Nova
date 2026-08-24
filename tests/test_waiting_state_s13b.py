"""Waiting for a person is a state, not a completion (Stage 13B).

INVARIANTS UNDER TEST

  I13  "What is pending?" includes everything that is genuinely pending.
  I14  A failed action is never marked successful.
  I17  A task that cannot proceed without a person says so durably.
  I35  A restart does not resolve a question nobody answered.

THE DEFECT THIS WAS WRITTEN AGAINST

`AutonomySupervisorWorker` handled `ask_user` by writing the question into a
fact and then calling `mark_task_done`:

    await self._memory.add_fact(entity=f"project:{project}",
                                attribute="autonomy_note", value=msg, ...)
    await self._memory.mark_task_done(task_id=task_id,
                                      result={"status": "needs_user", ...})

So a task that had asked a question and could not move until someone answered
was filed with the finished work. `list_tasks(status='queued')` did not
include it, `list_tasks(status='done')` did, and the question survived only as
a fact with nothing linking it back to the task that asked it.

The four statuses could not express it. `queued` says it will run on its own;
`running` says a worker holds it; `done` says it finished; `failed` says
something went wrong. None of those is "waiting for you".

`blocked` is that state. It is deliberately not claimable, and it deliberately
survives a restart -- an unanswered question is still unanswered after a
reboot. It also has a way out, because a state with no exit is not an
improvement on a state that lies.

FANOUT IS ANALYSED SEPARATELY and is NOT the same case, despite the shared
word "blocked" in the old result payload. Nothing is waiting on a person, and
nothing will ever unblock it: a planner-created task may not create more tasks,
the refusal is final, and re-running it would produce the same plan and the
same refusal. It is not `done` either -- the task's own work never happened.

Run:  venv\\Scripts\\python.exe tests\\test_waiting_state_s13b.py
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

from core.event_bus import BUS  # noqa: E402
from core.policy.autonomy_planner import AutonomyPlannerLLM  # noqa: E402
from core.tool_router import ToolRouter  # noqa: E402
from core.workers.autonomy_supervisor import AutonomySupervisorWorker  # noqa: E402
from memory.unifier import MemoryUnifier  # noqa: E402

check = Checks()

ASK_PLAN = ('{"action":"ask_user","reason":"needs a decision",'
            '"message_to_user":"Should the pause menu darken the whole screen?",'
            '"tool_calls":[],"new_tasks":[]}')
FANOUT_PLAN = ('{"action":"enqueue_task","reason":"split the work",'
               '"tool_calls":[],'
               '"new_tasks":[{"title":"sub one","details":"d","priority":3}]}')


async def _row(mem, task_id: str) -> dict:
    for t in await mem.list_tasks(limit=50):
        if str(t.get("task_id")) == str(task_id):
            return t
    return {}


def _payload(row: dict) -> dict:
    try:
        return json.loads(row.get("result_json") or "{}")
    except Exception:
        return {}


async def _drain(mem, worker, task_id: str, *, ticks: int = 300) -> dict:
    worker.start()
    try:
        for _ in range(ticks):
            await asyncio.sleep(0.05)
            row = await _row(mem, task_id)
            if row and str(row.get("status")) not in ("queued", "running"):
                return row
    finally:
        await worker.stop()
    return await _row(mem, task_id)


async def _build(tmp: Path, plan_reply: str):
    mem = MemoryUnifier(tmp, enable_chroma=False)
    await mem.initialize()

    async def noop(_a):
        return {"ok": True}

    llm = ScriptedLLM()
    llm.default_reply = plan_reply
    worker = AutonomySupervisorWorker(
        memory=mem,
        planner=AutonomyPlannerLLM(llm, llm_semaphore=asyncio.Semaphore(1)),
        router=ToolRouter({"work.step": noop}, {}),
        tick_seconds=0.05)
    return mem, worker


async def test_a_question_leaves_the_task_waiting_not_finished():
    check.section("waiting: asking a question is not finishing")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem, worker = await _build(Path(td) / "a", ASK_PLAN)
        task_id = str(await mem.enqueue_task(
            title="add a pause menu", details="step one",
            project_name="flappy-bird", initiated_by_user=True))
        row = await _drain(mem, worker, task_id)

        check(str(row.get("status")) != "done",
              f"the task is NOT done ({row.get('status')!r})")
        check(str(row.get("status")) == "blocked",
              f"it is waiting ({row.get('status')!r})")
        check("pause menu darken" in str(row.get("last_error") or ""),
              f"and the question is on the task itself "
              f"({str(row.get('last_error'))[:70]!r})")
        check("darken" in str(_payload(row).get("question") or ""),
              f"kept in the payload too ({_payload(row).get('question')!r})")

        # "What is pending?" must include it, and "what finished?" must not.
        pending = [t for t in await mem.list_tasks(status="blocked", limit=50)]
        done = [t for t in await mem.list_tasks(status="done", limit=50)]
        check(any(str(t.get("task_id")) == task_id for t in pending),
              f"it is listed as pending ({len(pending)})")
        check(not any(str(t.get("task_id")) == task_id for t in done),
              f"and not listed as finished ({len(done)})")


async def test_a_waiting_task_is_not_picked_up_on_its_own():
    check.section("waiting: nothing claims it until someone answers")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem, worker = await _build(Path(td) / "b", ASK_PLAN)
        task_id = str(await mem.enqueue_task(
            title="add a pause menu", details="step one",
            project_name="flappy-bird", initiated_by_user=True))
        await _drain(mem, worker, task_id)

        claimed = await mem.claim_next_task()
        check(claimed is None,
              f"a blocked task is not claimable ({(claimed or {}).get('task_id')})")

        # And a restart does not answer it either.
        cleared = await mem.cancel_pending_background_work()
        row = await _row(mem, task_id)
        check(str(row.get("status")) == "blocked",
              f"it is still waiting after a restart ({row.get('status')!r})")
        check("darken" in str(row.get("last_error") or ""),
              f"with the question intact ({str(row.get('last_error'))[:50]!r})")
        check(int(cleared.get("interrupted") or 0) == 0,
              f"and the restart does not claim it was in flight ({cleared})")


async def test_an_answer_lets_the_work_continue():
    check.section("waiting: an answer releases it, and reaches the plan")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem, worker = await _build(Path(td) / "c", ASK_PLAN)
        task_id = str(await mem.enqueue_task(
            title="add a pause menu", details="step one",
            project_name="flappy-bird", initiated_by_user=True))
        await _drain(mem, worker, task_id)

        ok = await mem.answer_task_question(task_id=task_id,
                                            answer="yes, darken it")
        row = await _row(mem, task_id)
        check(ok is True, f"the answer was accepted ({ok})")
        check(str(row.get("status")) == "queued",
              f"the task is runnable again ({row.get('status')!r})")
        check("yes, darken it" in str(row.get("details") or ""),
              f"and the answer is where the next plan will read it "
              f"({str(row.get('details'))[-60:]!r})")
        check(str(row.get("last_error") or "") == "",
              f"the question is no longer outstanding ({row.get('last_error')!r})")

        claimed = await mem.claim_next_task()
        check(claimed is not None
              and str(claimed.get("task_id")) == task_id,
              f"and it can be claimed ({str((claimed or {}).get('task_id'))[:8]})")


async def test_an_answer_cannot_restart_something_else():
    """COUNTER-TEST. The exit from `blocked` is not a general re-open."""
    check.section("waiting: an answer only releases a waiting task")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem = MemoryUnifier(Path(td) / "d", enable_chroma=False)
        await mem.initialize()
        finished = str(await mem.enqueue_task(
            title="already finished", details="d",
            project_name="flappy-bird", initiated_by_user=True))
        await mem.claim_next_task()
        await mem.mark_task_done(task_id=finished, result={"status": "ok"})

        ok = await mem.answer_task_question(task_id=finished, answer="go on")
        row = await _row(mem, finished)
        check(ok is False, f"the answer is refused ({ok})")
        check(str(row.get("status")) == "done",
              f"and the finished task stays finished ({row.get('status')!r})")
        check("go on" not in str(row.get("details") or ""),
              f"with its details untouched ({str(row.get('details'))!r})")


async def test_a_question_is_announced_as_an_update_not_a_completion():
    check.section("waiting: the bus is told a question, not a completion")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem, worker = await _build(Path(td) / "e", ASK_PLAN)
        task_id = str(await mem.enqueue_task(
            title="add a pause menu", details="step one",
            project_name="flappy-bird", initiated_by_user=True))

        q = BUS.subscribe()
        try:
            await _drain(mem, worker, task_id)
            seen = []
            while True:
                try:
                    seen.append(q.get_nowait())
                except asyncio.QueueEmpty:
                    break
        finally:
            BUS.unsubscribe(q)

        # Attributed by the task_id on the event, never by arrival order.
        mine = [e for e in seen
                if str((e.data or {}).get("task_id")) == str(task_id)]
        kinds = [e.type for e in mine]
        check("task.completed" not in kinds,
              f"nothing announces it as completed ({kinds})")
        check("task.updated" in kinds, f"it is announced as an update ({kinds})")
        blocked = [e for e in mine
                   if str((e.data or {}).get("status")) == "blocked"]
        check(bool(blocked),
              f"and the announcement says it is waiting ({[e.data for e in mine]})")


async def test_a_refused_fanout_is_not_waiting_and_not_done():
    """The separate analysis: refusal is final, and nothing is pending."""
    check.section("fanout: a refused plan is a refusal, not a question")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem, worker = await _build(Path(td) / "f", FANOUT_PLAN)
        # NOT user-initiated: this is the depth guard's case.
        task_id = str(await mem.enqueue_task(
            title="a task that spawned itself", details="f",
            project_name="flappy-bird", initiated_by_user=False))
        row = await _drain(mem, worker, task_id)

        check(str(row.get("status")) != "done",
              f"the refused task is not done ({row.get('status')!r})")
        check(str(row.get("status")) != "blocked",
              f"and not waiting on anyone -- nothing will unblock it "
              f"({row.get('status')!r})")
        check(str(row.get("status")) == "failed",
              f"it is a refusal ({row.get('status')!r})")
        check("nothing was run" in str(row.get("last_error") or ""),
              f"which says nothing was run ({row.get('last_error')!r})")
        check(str(_payload(row).get("status")) == "fanout_blocked",
              f"and keeps why ({_payload(row).get('status')!r})")

        # It must not appear in the queue of things a person owes an answer to.
        waiting = await mem.list_tasks(status="blocked", limit=50)
        check(not any(str(t.get("task_id")) == task_id for t in waiting),
              f"nothing is waiting on a person ({len(waiting)})")


async def test_a_user_initiated_fanout_still_works():
    """COUNTER-TEST. The guard is a depth guard, not a ban on subtasks."""
    check.section("counter: work you asked for may still spawn subtasks")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem, worker = await _build(Path(td) / "g", FANOUT_PLAN)
        task_id = str(await mem.enqueue_task(
            title="a task you asked for", details="g",
            project_name="flappy-bird", initiated_by_user=True))
        row = await _drain(mem, worker, task_id)

        check(str(row.get("status")) == "done",
              f"it completes ({row.get('status')!r})")
        check(str(_payload(row).get("status")) == "enqueued",
              f"having enqueued the subtask ({_payload(row).get('status')!r})")
        subs = [t for t in await mem.list_tasks(limit=50)
                if str(t.get("title")) == "sub one"]
        check(len(subs) == 1, f"which exists ({len(subs)})")


async def main():
    await test_a_question_leaves_the_task_waiting_not_finished()
    await test_a_waiting_task_is_not_picked_up_on_its_own()
    await test_an_answer_lets_the_work_continue()
    await test_an_answer_cannot_restart_something_else()
    await test_a_question_is_announced_as_an_update_not_a_completion()
    await test_a_refused_fanout_is_not_waiting_and_not_done()
    await test_a_user_initiated_fanout_still_works()
    check.finish()


if __name__ == "__main__":
    run(main)
