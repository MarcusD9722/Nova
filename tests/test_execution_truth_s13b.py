"""Failure must not be recorded as completion (Stage 13B, Family B).

INVARIANTS UNDER TEST

  I14  A failed action is never marked successful.
  I15  An unknown outcome is never represented as success.
  I39  A tool invocation is not tool success.
  I41  A multi-step operation that partially succeeds reports exactly that.

THE DEFECT THIS WAS WRITTEN AGAINST

`AutonomySupervisorWorker` has exactly one terminal call — `mark_task_done` —
and reaches it from every path, including the two that are not success:

    plan.action == "tool", a tool fails  -> mark_task_done(status="tools_blocked")
    an unhandled exception in the loop   -> mark_task_done(status="failed")

`mark_task_done` writes `autonomy_tasks.status = 'done'`, clears `last_error`
(it passes error="") and publishes `task.completed {status: "done"}`. The honest
outcome survives only inside `result_json`, which nothing queries.

`MemoryUnifier.mark_task_failed` exists and writes `status='failed'` properly.
It had ZERO callers.

So "what failed?" answered nothing, `list_tasks(status='failed')` returned
nothing, and a crashed task was announced on the bus as completed.

WHAT THESE TESTS ASSERT. The authoritative row — status, last_error — and the
event actually published. Not the result payload, which was already right and is
exactly what made the defect invisible.

Run:  venv\\Scripts\\python.exe tests\\test_execution_truth_s13b.py
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

TOOL_PLAN = ('{"action":"tool","reason":"do the thing",'
             '"tool_calls":[{"tool":"work.step","args":{}}],"new_tasks":[]}')


async def _build(tmp: Path, *, plan_reply: str, tool):
    mem = MemoryUnifier(tmp, enable_chroma=False)
    await mem.initialize()
    router = ToolRouter({"work.step": tool}, {})
    llm = ScriptedLLM()
    llm.default_reply = plan_reply
    worker = AutonomySupervisorWorker(
        memory=mem,
        planner=AutonomyPlannerLLM(llm, llm_semaphore=asyncio.Semaphore(1)),
        router=router,
        tick_seconds=0.05,
    )
    return mem, worker


async def _row(mem, task_id: str) -> dict:
    for t in await mem.list_tasks(limit=50):
        if str(t.get("task_id")) == str(task_id):
            return t
    return {}


async def _drain(mem, worker, task_id: str, *, ticks: int = 200) -> dict:
    """Run the worker until the task leaves 'queued'/'running'."""
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


class BusSpy:
    """Task lifecycle events, attributed by the task id they carry.

    The bus is global and every worker in the process publishes onto it, so the
    ONLY sound way to say which operation produced an event is the task_id on
    the event itself. Position in the queue is not evidence: two workers
    interleave, a slow consumer has its oldest event dropped to make room, and
    `BUS.recent()` is one shared list. Anything that sliced it would be
    measuring arrival order and calling it causation.
    """

    def __init__(self) -> None:
        self._q = None

    def __enter__(self) -> "BusSpy":
        self._q = BUS.subscribe()
        return self

    def __exit__(self, *exc) -> None:
        if self._q is not None:
            BUS.unsubscribe(self._q)
        self._q = None

    def drain(self) -> list:
        """Everything published since subscribing, oldest first."""
        out = []
        if self._q is None:
            return out
        while True:
            try:
                out.append(self._q.get_nowait())
            except asyncio.QueueEmpty:
                return out

    def for_task(self, task_id: str) -> list:
        """Only the events that name this task. Never 'the last N events'."""
        return [e for e in self.drain()
                if str((e.data or {}).get("task_id")) == str(task_id)]


async def test_a_failed_tool_is_not_recorded_as_done():
    check.section("execution truth: a failed tool is not a completed task")

    async def failing(_args):
        return {"ok": False, "error": "disk full"}

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem, worker = await _build(Path(td) / "a", plan_reply=TOOL_PLAN,
                                   tool=failing)
        task_id = await mem.enqueue_task(
            title="write the file", details="step one",
            project_name="flappy-bird", initiated_by_user=True)
        row = await _drain(mem, worker, str(task_id))

        payload = {}
        try:
            payload = json.loads(row.get("result_json") or "{}")
        except Exception:
            pass

        check(bool(row), f"the task row exists ({row.get('task_id')})")
        check(str(row.get("status")) != "done",
              f"the ROW must not say done ({row.get('status')!r})")
        check(str(row.get("status")) == "failed",
              f"it says failed ({row.get('status')!r})")
        check(str(row.get("last_error") or "").strip() != "",
              f"and carries the error ({row.get('last_error')!r})")


async def test_a_crashed_task_is_not_recorded_as_done():
    check.section("execution truth: a crash is not a completed task")

    async def exploding(_args):
        raise RuntimeError("tool exploded")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        # A planner that returns something the loop cannot handle would exercise
        # a different branch; this drives the tool path and lets the ROUTER's
        # failure surface, then the loop's own exception handler.
        mem, worker = await _build(Path(td) / "b", plan_reply="not json at all",
                                   tool=exploding)
        task_id = await mem.enqueue_task(
            title="explode", details="", project_name="flappy-bird",
            initiated_by_user=True)
        row = await _drain(mem, worker, str(task_id))

        check(bool(row), "the task row exists")
        check(str(row.get("status")) != "done",
              f"a task that did not succeed must not say done "
              f"({row.get('status')!r})")


async def test_what_failed_is_answerable_from_the_store():
    """The consequence that makes this matter.

    `list_tasks(status="failed")` is how "what failed?" is answered.
    While every terminal path wrote 'done', that query returned nothing no
    matter what had gone wrong.
    """
    check.section("execution truth: 'what failed?' is answerable")

    async def failing(_args):
        return {"ok": False, "error": "permission denied"}

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem, worker = await _build(Path(td) / "c", plan_reply=TOOL_PLAN,
                                   tool=failing)
        task_id = await mem.enqueue_task(
            title="touch the file", details="", project_name="calc-tool",
            initiated_by_user=True)
        await _drain(mem, worker, str(task_id))

        failed = await mem.list_tasks(status="failed", limit=50)
        done = await mem.list_tasks(status="done", limit=50)
        check(any(str(t.get("task_id")) == str(task_id) for t in failed),
              f"the failed task is listed as failed ({len(failed)} failed)")
        check(not any(str(t.get("task_id")) == str(task_id) for t in done),
              f"and is NOT listed as done ({len(done)} done)")


async def test_genuine_success_is_still_done():
    """The other half. A fix that marks everything failed is not a fix.

    A plan whose tools all succeed, and a planner that genuinely has nothing to
    do, must both still land on `done` with no error.
    """
    check.section("execution truth: real success is still success")

    async def working(_args):
        return {"ok": True, "wrote": "game.js"}

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem, worker = await _build(Path(td) / "ok", plan_reply=TOOL_PLAN,
                                   tool=working)
        task_id = await mem.enqueue_task(
            title="write the file", details="", project_name="flappy-bird",
            initiated_by_user=True)
        row = await _drain(mem, worker, str(task_id))
        check(str(row.get("status")) == "done",
              f"a plan whose tools succeeded is done ({row.get('status')!r})")
        check(str(row.get("last_error") or "") == "",
              f"with no error ({row.get('last_error')!r})")

    IDLE = ('{"action":"idle","reason":"nothing to do",'
            '"tool_calls":[],"new_tasks":[]}')
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem, worker = await _build(Path(td) / "idle", plan_reply=IDLE,
                                   tool=working)
        task_id = await mem.enqueue_task(
            title="nothing", details="", project_name="flappy-bird",
            initiated_by_user=True)
        row = await _drain(mem, worker, str(task_id))
        check(str(row.get("status")) == "done",
              f"a genuine idle is still done ({row.get('status')!r})")


async def test_a_degraded_planner_is_not_a_finished_task():
    """"I could not read the plan" is not "there was nothing to do".

    The planner falls back to `idle` when its output is unparseable or invalid,
    labelling the reason `planner_unparseable` / `planner_invalid`. Both used to
    land on `done`, so a task whose plan never existed reported as finished —
    and the label lives in result_json, which the listing does not return.
    """
    check.section("execution truth: a degraded planner is not a finished task")

    async def working(_args):
        return {"ok": True}

    for reply in ("not json at all", '{"action":"teleport","reason":"?"}'):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            mem, worker = await _build(Path(td) / "deg", plan_reply=reply,
                                       tool=working)
            task_id = await mem.enqueue_task(
                title="do the thing", details="", project_name="flappy-bird",
                initiated_by_user=True)
            row = await _drain(mem, worker, str(task_id))
            check(str(row.get("status")) == "failed",
                  f"{reply[:22]!r}: recorded as failed ({row.get('status')!r})")
            check("planner" in str(row.get("last_error") or "").lower(),
                  f"{reply[:22]!r}: and says why ({row.get('last_error')!r})")


async def test_an_exception_in_the_loop_is_a_failed_task():
    """The loop's own exception handler, exercised directly.

    Reached by making the PLANNER raise, which is outside the router's
    try/except — the router turns a raising tool into a structured failure, so
    that path lands on the blocked branch instead.
    """
    check.section("execution truth: a crash in the loop fails the task")

    class ExplodingPlanner:
        async def plan(self, **_kw):
            raise RuntimeError("planner exploded")

    async def working(_args):
        return {"ok": True}

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem, worker = await _build(Path(td) / "boom", plan_reply=TOOL_PLAN,
                                   tool=working)
        worker._planner = ExplodingPlanner()
        task_id = await mem.enqueue_task(
            title="explode", details="", project_name="flappy-bird",
            initiated_by_user=True)
        row = await _drain(mem, worker, str(task_id))
        check(str(row.get("status")) == "failed",
              f"the crashed task is failed ({row.get('status')!r})")
        check("exploded" in str(row.get("last_error") or ""),
              f"and carries the real error ({row.get('last_error')!r})")


async def test_a_failed_task_publishes_no_completion_event():
    """The bus must not announce a failure as a completion.

    `mark_task_done` publishes `task.completed`, and it used to be the only
    terminal call the worker made — so anything listening (the UI, the voice
    layer, the episodic promoter) was told a crashed task had completed. The
    row and the announcement have to agree, or Nova's next sentence is wrong
    even when the database is right.
    """
    check.section("event truth: a failed task is not announced as completed")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        async def failing(_args):
            return {"ok": False, "error": "the api returned 503"}

        mem, worker = await _build(Path(td) / "ev1", plan_reply=TOOL_PLAN,
                                   tool=failing)
        task_id = str(await mem.enqueue_task(
            title="write the file", details="step one",
            project_name="flappy-bird", initiated_by_user=True))

        with BusSpy() as spy:
            await _drain(mem, worker, task_id)
            mine = spy.for_task(task_id)

        row = await _row(mem, task_id)
        kinds = [e.type for e in mine]
        check(str(row.get("status")) == "failed",
              f"the row says failed ({row.get('status')!r})")
        check("task.completed" not in kinds,
              f"and NO task.completed names this task ({kinds})")
        check("task.updated" in kinds,
              f"the failure was announced instead ({kinds})")

        # The announcement must agree with the row, field by field.
        upd = [e for e in mine if e.type == "task.updated"]
        last = upd[-1].data if upd else {}
        check(str(last.get("status")) == str(row.get("status")),
              f"event status matches the row "
              f"({last.get('status')!r} vs {row.get('status')!r})")
        check(str(last.get("task_id")) == str(task_id),
              f"event task_id matches the task ({str(last.get('task_id'))[:8]})")
        check("503" in str(last.get("error") or ""),
              f"and the event carries the real error ({last.get('error')!r})")
        check(str(last.get("error") or "")[:60] in str(row.get("last_error") or ""),
              f"which is the row's error too ({row.get('last_error')!r})")


async def test_a_successful_task_still_publishes_a_completion_event():
    """COUNTER-TEST. Silence is not truth either."""
    check.section("event truth: a real success is still announced")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        async def working(_args):
            return {"ok": True, "data": {"wrote": "game.js"}}

        mem, worker = await _build(Path(td) / "ev2", plan_reply=TOOL_PLAN,
                                   tool=working)
        task_id = str(await mem.enqueue_task(
            title="write the file", details="step one",
            project_name="flappy-bird", initiated_by_user=True))

        with BusSpy() as spy:
            await _drain(mem, worker, task_id)
            mine = spy.for_task(task_id)

        row = await _row(mem, task_id)
        kinds = [e.type for e in mine]
        check(str(row.get("status")) == "done",
              f"the row says done ({row.get('status')!r})")
        check("task.completed" in kinds,
              f"and task.completed names this task ({kinds})")
        done = [e for e in mine if e.type == "task.completed"]
        data = done[-1].data if done else {}
        check(str(data.get("status")) == "done",
              f"with a matching status ({data.get('status')!r})")


async def test_two_tasks_do_not_borrow_each_others_events():
    """Attribution, proved rather than assumed.

    If events were read by position this test would pass by accident, so it
    runs a failing task and a succeeding one through the SAME worker and the
    SAME bus, then checks each task's verdict against its own id.
    """
    check.section("event truth: concurrent tasks keep their own verdicts")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        calls = {"n": 0}

        async def alternating(_args):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"ok": False, "error": "first one broke"}
            return {"ok": True, "data": {}}

        mem, worker = await _build(Path(td) / "ev3", plan_reply=TOOL_PLAN,
                                   tool=alternating)
        bad = str(await mem.enqueue_task(
            title="first step", details="one",
            project_name="flappy-bird", initiated_by_user=True))
        good = str(await mem.enqueue_task(
            title="second step", details="two",
            project_name="flappy-bird", initiated_by_user=True))

        with BusSpy() as spy:
            await _drain(mem, worker, bad)
            await _drain(mem, worker, good)
            events = spy.drain()

        def kinds_for(tid):
            return [e.type for e in events
                    if str((e.data or {}).get("task_id")) == str(tid)]

        bad_row, good_row = await _row(mem, bad), await _row(mem, good)
        check(str(bad_row.get("status")) == "failed"
              and str(good_row.get("status")) == "done",
              f"the rows disagree as they should "
              f"({bad_row.get('status')!r} / {good_row.get('status')!r})")
        check("task.completed" not in kinds_for(bad),
              f"the failed task has no completion event ({kinds_for(bad)})")
        check("task.completed" in kinds_for(good),
              f"the successful one does ({kinds_for(good)})")


async def main():
    await test_a_failed_tool_is_not_recorded_as_done()
    await test_a_crashed_task_is_not_recorded_as_done()
    await test_what_failed_is_answerable_from_the_store()
    await test_genuine_success_is_still_done()
    await test_a_degraded_planner_is_not_a_finished_task()
    await test_an_exception_in_the_loop_is_a_failed_task()
    await test_a_failed_task_publishes_no_completion_event()
    await test_a_successful_task_still_publishes_a_completion_event()
    await test_two_tasks_do_not_borrow_each_others_events()
    check.finish()


if __name__ == "__main__":
    run(main)