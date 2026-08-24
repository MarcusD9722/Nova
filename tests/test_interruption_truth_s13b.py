"""An interruption must not be recorded as a non-event (Stage 13B).

INVARIANTS UNDER TEST

  I15  An unknown outcome is never represented as success.
  I16  An unknown outcome is never represented as a definite non-event either
       -- the same defect, pointing the other way.
  I34  A restart never invents history it cannot support.
  I46  Terminal writes are idempotent: the first one wins.

THE DEFECT THIS WAS WRITTEN AGAINST

`AutonomySupervisorWorker` ended its loop with:

    except asyncio.CancelledError:
        return

The claim was taken (`claim_next_task` writes status='running'), so a
cancellation anywhere after that left the row in 'running' with an empty
last_error, and the next boot ran:

    UPDATE autonomy_tasks SET status='cancelled',
           last_error='cancelled_on_startup'
    WHERE status IN ('queued','running')

Measured on f929606, all five of these ended byte-identical:

    cancelled after the claim, before planning      nothing ran
    cancelled during planning                       nothing ran
    cancelled inside execute, before the tool body  a side effect is POSSIBLE
    cancelled DURING the tool                       a side effect is POSSIBLE
    cancelled after the tool, before bookkeeping    the tool DID succeed

Three different truths collapsed into one false one. "cancelled" asserts the
work never happened; for the last three that is a claim Nova is in no position
to make. It is the S13B-1 defect pointing the other way: there, an unknown
outcome became success; here, an unknown outcome becomes a definite non-event.

WHAT IS ASSERTED. The authoritative `autonomy_tasks` row, plus a side-effect
ledger kept by the tool itself -- so "did it really run?" is answered by what
the tool recorded, never inferred from timing or ordering.

COUNTER-TESTS. `test_a_queued_task_at_boot_is_still_just_cancelled` and
`test_an_uninterrupted_task_still_completes_normally` exist so that a "fix"
which marks everything unknown, or everything failed, cannot pass.

Run:  venv\\Scripts\\python.exe tests\\test_interruption_truth_s13b.py
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


async def _row(mem, task_id: str) -> dict:
    for t in await mem.list_tasks(limit=50):
        if str(t.get("task_id")) == str(task_id):
            return t
    return {}


def _note(row: dict) -> str:
    return str(row.get("last_error") or "")


async def _build(tmp: Path, tool, *, gate_at: str, entered, hold):
    """A worker whose loop can be frozen at one exact point."""
    mem = MemoryUnifier(tmp, enable_chroma=False)
    await mem.initialize()

    async def gate():
        entered.set()
        await hold.wait()

    if gate_at == "before_planning":
        real = mem.search

        async def gated_search(**kw):
            await gate()
            return await real(**kw)
        mem.search = gated_search

    router = ToolRouter({"work.step": tool}, {})
    if gate_at == "inside_execute":
        real_exec = router.execute

        async def gated_exec(call, **kw):
            await gate()
            return await real_exec(call, **kw)
        router.execute = gated_exec

    if gate_at == "bookkeeping":
        real_done = mem.mark_task_done

        async def gated_done(**kw):
            await gate()
            return await real_done(**kw)
        mem.mark_task_done = gated_done

    llm = ScriptedLLM()
    llm.default_reply = TOOL_PLAN
    worker = AutonomySupervisorWorker(
        memory=mem,
        planner=AutonomyPlannerLLM(llm, llm_semaphore=asyncio.Semaphore(1)),
        router=router,
        tick_seconds=0.05,
    )
    return mem, worker


async def _interrupt_at(td: Path, *, gate_at: str, tool):
    """Run until the loop reaches `gate_at`, then cancel it there."""
    entered, hold = asyncio.Event(), asyncio.Event()
    mem, worker = await _build(td, tool, gate_at=gate_at, entered=entered,
                               hold=hold)
    task_id = str(await mem.enqueue_task(
        title="write the file", details="step one",
        project_name="flappy-bird", initiated_by_user=True))
    worker.start()
    try:
        await asyncio.wait_for(entered.wait(), timeout=25)
    except asyncio.TimeoutError:
        await worker.stop()
        return mem, task_id, False
    worker._task.cancel()
    try:
        await worker._task
    except BaseException:
        pass
    return mem, task_id, True


async def test_an_interruption_before_any_tool_ran_says_so():
    check.section("interruption: nothing ran, and it is safe to say so")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        effects: list[str] = []

        async def tool(_a):
            effects.append("ran")
            return {"ok": True}

        mem, task_id, ok = await _interrupt_at(Path(td) / "a",
                                               gate_at="before_planning",
                                               tool=tool)
        check(ok, "the loop reached the cancellation point")
        row = await _row(mem, task_id)
        check(not effects, f"no tool ran ({effects})")
        check(str(row.get("status")) != "running",
              f"the claim is not stranded in running ({row.get('status')!r})")
        check(str(row.get("status")) != "done",
              f"and is not recorded as done ({row.get('status')!r})")
        check("nothing was executed" in _note(row),
              f"the record says nothing was executed ({_note(row)!r})")


async def test_an_interruption_mid_tool_is_recorded_as_unknown():
    """The one that matters: a side effect may exist and cannot be checked."""
    check.section("interruption: mid-tool is UNKNOWN, not 'never happened'")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        entered_tool, hold_tool = asyncio.Event(), asyncio.Event()
        effects: list[str] = []

        async def slow_tool(_a):
            effects.append("partial")      # a real, observable side effect
            entered_tool.set()
            await hold_tool.wait()
            effects.append("finished")
            return {"ok": True}

        mem = MemoryUnifier(Path(td) / "b", enable_chroma=False)
        await mem.initialize()
        llm = ScriptedLLM()
        llm.default_reply = TOOL_PLAN
        worker = AutonomySupervisorWorker(
            memory=mem,
            planner=AutonomyPlannerLLM(llm, llm_semaphore=asyncio.Semaphore(1)),
            router=ToolRouter({"work.step": slow_tool}, {}),
            tick_seconds=0.05)
        task_id = str(await mem.enqueue_task(
            title="write the file", details="step one",
            project_name="flappy-bird", initiated_by_user=True))
        worker.start()
        await asyncio.wait_for(entered_tool.wait(), timeout=25)
        worker._task.cancel()
        try:
            await worker._task
        except BaseException:
            pass

        row = await _row(mem, task_id)
        note = _note(row)
        check(effects == ["partial"],
              f"the tool really was mid-flight ({effects})")
        check(str(row.get("status")) != "done",
              f"it is not recorded as done ({row.get('status')!r})")
        check(str(row.get("status")) != "cancelled",
              f"and NOT as cancelled, which would assert it never ran "
              f"({row.get('status')!r})")
        check("cannot tell whether it completed" in note,
              f"the record says the outcome is unknown ({note!r})")
        check("work.step" in note,
              f"and names what was in flight ({note!r})")


async def test_an_interruption_after_the_tool_keeps_what_is_known():
    check.section("interruption: a finished call is not thrown away")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        effects: list[str] = []

        async def tool(_a):
            effects.append("ran")
            return {"ok": True, "data": {"wrote": "game.js"}}

        mem, task_id, ok = await _interrupt_at(Path(td) / "c",
                                               gate_at="bookkeeping", tool=tool)
        check(ok, "the loop reached the cancellation point")
        row = await _row(mem, task_id)
        note = _note(row)
        check(effects == ["ran"], f"the tool completed ({effects})")
        check("had completed" in note,
              f"the record says the call completed ({note!r})")
        check("cannot tell whether it completed" not in note,
              f"and does NOT call a known outcome unknown ({note!r})")


async def test_a_restart_does_not_call_an_interrupted_task_a_non_event():
    """A hard crash leaves 'running' with nothing recorded. The boot decides."""
    check.section("restart: a task caught mid-flight is not 'never started'")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem = MemoryUnifier(Path(td) / "d", enable_chroma=False)
        await mem.initialize()
        running_id = str(await mem.enqueue_task(
            title="the one that was in flight", details="d",
            project_name="flappy-bird", initiated_by_user=True))
        claimed = await mem.claim_next_task()
        check(claimed is not None
              and str(claimed.get("task_id")) == running_id,
              f"the task was claimed ({str((claimed or {}).get('task_id'))[:8]})")
        # No terminal write: this is the process dying, not stopping.

        cleared = await mem.cancel_pending_background_work()
        row = await _row(mem, running_id)
        note = _note(row)
        check(str(row.get("status")) != "cancelled",
              f"it is not recorded as cancelled ({row.get('status')!r})")
        check("unknown" in note.lower(),
              f"the record says the outcome is unknown ({note!r})")
        check("may have acted" in note,
              f"and that it may have acted ({note!r})")
        check(int(cleared.get("interrupted") or 0) == 1,
              f"the boot counts it as interrupted ({cleared})")


async def test_a_queued_task_at_boot_is_still_just_cancelled():
    """COUNTER-TEST. A task that never started really did never start."""
    check.section("restart: a task that never started is plainly cancelled")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem = MemoryUnifier(Path(td) / "e", enable_chroma=False)
        await mem.initialize()
        queued_id = str(await mem.enqueue_task(
            title="never claimed", details="e",
            project_name="flappy-bird", initiated_by_user=True))

        cleared = await mem.cancel_pending_background_work()
        row = await _row(mem, queued_id)
        check(str(row.get("status")) == "cancelled",
              f"it is cancelled ({row.get('status')!r})")
        check("cancelled_on_startup" in _note(row),
              f"with the plain startup note ({_note(row)!r})")
        check(int(cleared.get("interrupted") or 0) == 0,
              f"and nothing is claimed to have been in flight ({cleared})")


async def test_a_finished_task_is_not_overwritten_by_the_interruption():
    """First terminal write wins.

    The interruption handler runs from `except CancelledError`, which can fire
    immediately after a successful `mark_task_done` has already committed.
    Unguarded, it would replace a genuine `done` with `failed: interrupted` --
    fixing one false record by writing another.
    """
    check.section("interruption: a recorded outcome is not overwritten")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem = MemoryUnifier(Path(td) / "f", enable_chroma=False)
        await mem.initialize()
        task_id = str(await mem.enqueue_task(
            title="already finished", details="f",
            project_name="flappy-bird", initiated_by_user=True))
        await mem.claim_next_task()
        await mem.mark_task_done(task_id=task_id, result={"status": "tools_done"})
        first = await _row(mem, task_id)
        check(str(first.get("status")) == "done",
              f"the real outcome landed ({first.get('status')!r})")

        # The cancellation arrives a moment too late.
        await mem.mark_task_failed(task_id=task_id, error="interrupted",
                                   result={"status": "interrupted"})
        after = await _row(mem, task_id)
        check(str(after.get("status")) == "done",
              f"and is not overwritten ({after.get('status')!r})")
        check("interrupted" not in _note(after),
              f"nor is its error rewritten ({_note(after)!r})")

        # A write that did not land must not be announced either. Attributed by
        # the task_id on the event, never by counting what arrived.
        q = BUS.subscribe()
        try:
            await mem.mark_task_failed(task_id=task_id, error="interrupted",
                                       result={"status": "interrupted"})
            seen = []
            while True:
                try:
                    seen.append(q.get_nowait())
                except asyncio.QueueEmpty:
                    break
        finally:
            BUS.unsubscribe(q)
        mine = [e.type for e in seen
                if str((e.data or {}).get("task_id")) == str(task_id)]
        check(not mine,
              f"and the no-op write announces nothing ({mine})")


async def test_an_uninterrupted_task_still_completes_normally():
    """COUNTER-TEST. The fix must not turn ordinary work into 'unknown'."""
    check.section("counter: an undisturbed task still finishes cleanly")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        effects: list[str] = []

        async def tool(_a):
            effects.append("ran")
            return {"ok": True, "data": {"wrote": "game.js"}}

        mem = MemoryUnifier(Path(td) / "g", enable_chroma=False)
        await mem.initialize()
        llm = ScriptedLLM()
        llm.default_reply = TOOL_PLAN
        worker = AutonomySupervisorWorker(
            memory=mem,
            planner=AutonomyPlannerLLM(llm, llm_semaphore=asyncio.Semaphore(1)),
            router=ToolRouter({"work.step": tool}, {}),
            tick_seconds=0.05)
        task_id = str(await mem.enqueue_task(
            title="an ordinary task", details="g",
            project_name="flappy-bird", initiated_by_user=True))
        worker.start()
        try:
            for _ in range(300):
                await asyncio.sleep(0.05)
                row = await _row(mem, task_id)
                if row and str(row.get("status")) not in ("queued", "running"):
                    break
        finally:
            await worker.stop()

        row = await _row(mem, task_id)
        check(effects == ["ran"], f"the tool ran ({effects})")
        check(str(row.get("status")) == "done",
              f"and the task is done ({row.get('status')!r})")
        check(_note(row) == "",
              f"with no error on the row ({_note(row)!r})")
        payload = {}
        try:
            payload = json.loads(row.get("result_json") or "{}")
        except Exception:
            pass
        check(str(payload.get("status")) == "tools_done",
              f"and the real outcome recorded ({payload.get('status')!r})")


async def main():
    await test_an_interruption_before_any_tool_ran_says_so()
    await test_an_interruption_mid_tool_is_recorded_as_unknown()
    await test_an_interruption_after_the_tool_keeps_what_is_known()
    await test_a_restart_does_not_call_an_interrupted_task_a_non_event()
    await test_a_queued_task_at_boot_is_still_just_cancelled()
    await test_a_finished_task_is_not_overwritten_by_the_interruption()
    await test_an_uninterrupted_task_still_completes_normally()
    check.finish()


if __name__ == "__main__":
    run(main)
