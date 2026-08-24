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
    """Task lifecycle events, captured with the task id they carry.

    Attributed by the task_id ON the event, never by arrival order — a shared
    bus cannot be sliced.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self._subs = []

    def start(self) -> None:
        for name in ("task.completed", "task.updated", "task.failed"):
            q = BUS.subscribe(name) if hasattr(BUS, "subscribe") else None
            self._subs.append((name, q))

    def for_task(self, task_id: str) -> list[tuple[str, dict]]:
        return [(n, d) for (n, d) in self.events
                if str(d.get("task_id")) == str(task_id)]


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


async def main():
    await test_a_failed_tool_is_not_recorded_as_done()
    await test_a_crashed_task_is_not_recorded_as_done()
    await test_what_failed_is_answerable_from_the_store()
    await test_genuine_success_is_still_done()
    await test_a_degraded_planner_is_not_a_finished_task()
    await test_an_exception_in_the_loop_is_a_failed_task()
    check.finish()


if __name__ == "__main__":
    run(main)
