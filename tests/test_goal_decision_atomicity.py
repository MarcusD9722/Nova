"""A supervisor decision applies completely, or not at all (V3 P10 C5).

THE DEFECT on `cea3e25`. The generation fence was correct but the APPLICATION
was still four separate transactions:

    fenced UPDATE goal -> paused      COMMIT
    create_proposal(...)              COMMIT
    complete_goal_task(...)           COMMIT
    add_progress_event(...)           COMMIT

A cancel landing between them left a database that looked coherent and
described something that never happened: a pending proposal from a lifecycle
run the user had already cancelled, a `complete` event arriving after the goal
had moved to a new generation, or a scheduled tool with no continuation.

Two properties are proven here:

  ATOMICITY   while the apply is mid-flight NOTHING it writes is visible, and
              a concurrent cancel is ordered after it rather than through it.
  AUTHORITY   the gate is `generation = N AND status = 'active'`. Generation
              equality alone says "the same run", not "a run still accepting
              decisions" — a PAUSED goal on generation 4 must still refuse a
              decision carrying generation 4.

Barriers and events only; no sleep establishes any race.

Run:  venv\\Scripts\\python.exe tests\\test_goal_decision_atomicity.py
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

from harness import Checks, ScriptedLLM, run  # noqa: E402

from core.agent_supervisor import AgentSupervisor, SupervisorConfig  # noqa: E402
from core.tool_router import ToolRouter  # noqa: E402
from memory.unifier import MemoryUnifier  # noqa: E402

check = Checks()


def _tmp():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


async def _mem(td):
    m = MemoryUnifier(Path(td), enable_chroma=False)
    await m.initialize()
    return m


async def _goal(m, project="alpha"):
    return await m.create_goal(project_name=project, title="t", objective="o",
                               success_criteria="c")


async def _state(m, gid, project="alpha"):
    """Everything an observer could see about this goal, in one shot."""
    goal = await m.get_goal(goal_id=gid)
    tasks = await m.list_goal_tasks(goal_id=str(gid), limit=50)
    prop = await m.latest_pending_proposal(project_name=project)
    events = await m.fetch_unacked_progress(project_name=project, limit=50)
    return {
        "status": (goal or {}).get("status"),
        "generation": int((goal or {}).get("generation") or 0),
        "tasks": tasks,
        "proposal": prop,
        "events": events,
        "kinds": [e["kind"] for e in events],
    }


# ── ATOMICITY: block INSIDE the transaction and look ─────────────────────────

async def _blocked_apply(kind: str, at: str = "complete"):
    """Start an apply, stop it mid-transaction, and report what is visible.

    The block is installed on an inner helper that runs INSIDE the single
    transaction, after the fenced gate has already updated the goal row. If the
    writes were still separate transactions, the earlier ones would already be
    visible here — which is exactly the state the defect produced.

    `at` picks HOW FAR through the transaction to stop:

        "complete"  after the gate (and a question's proposal), before the
                    decision task is finished
        "event"     after all of that, immediately before the progress event

    Both are needed. Stopping only at "complete" cannot see a split that
    happens after it — measured: a mutant committing between the task
    completion and the `complete` event survived a probe that only blocked
    early.
    """
    with _tmp() as td:
        m = await _mem(td)
        gid = await _goal(m)
        tid = await m.enqueue_goal_task(goal_id=gid, project_name="alpha",
                                        tool_name="__decide__", args={})
        rows = await m.list_goal_tasks(goal_id=str(gid), limit=10)
        task_id = rows[0]["task_id"]

        inside = asyncio.Event()
        release = asyncio.Event()
        backend = m._sqlite
        attr = "_complete_decision_task" if at == "complete" else "_add_event"
        original = getattr(backend, attr)

        async def gated(db, **kw):
            inside.set()
            await release.wait()
            return await original(db, **kw)

        setattr(backend, attr, gated)
        try:
            if kind == "question":
                apply = asyncio.create_task(m.apply_question_decision(
                    goal_id=gid, project_name="alpha", expected_generation=0,
                    task_id=task_id, message="Which database?"))
            elif kind == "final":
                apply = asyncio.create_task(m.apply_final_decision(
                    goal_id=gid, project_name="alpha", expected_generation=0,
                    task_id=task_id, message="All done."))
            else:
                apply = asyncio.create_task(m.apply_tool_decision(
                    goal_id=gid, project_name="alpha", expected_generation=0,
                    task_id=task_id, tool_name="demo.spy", args={}))

            await asyncio.wait_for(inside.wait(), timeout=15.0)

            # A concurrent cancel, launched while the apply holds the boundary.
            cancel = asyncio.create_task(m.cancel_goal(goal_id=gid))
            for _ in range(50):
                await asyncio.sleep(0)

            mid = await _state(m, gid)
            cancel_done_midway = cancel.done()

            release.set()
            applied = await asyncio.wait_for(apply, timeout=15.0)
            await asyncio.wait_for(cancel, timeout=15.0)
            end = await _state(m, gid)
            return mid, end, applied, cancel_done_midway, tid
        finally:
            setattr(backend, attr, original)


async def test_a_question_apply_is_one_indivisible_act():
    check.section("C5-A: nothing of a question decision is visible mid-apply")

    mid, end, applied, cancel_midway, _ = await _blocked_apply("question")

    # THE defect: the pause committed on its own and the rest followed later.
    check(mid["status"] == "active",
          f"mid-transaction the goal is NOT yet paused ({mid['status']})")
    check(mid["proposal"] is None,
          f"and no proposal is visible yet ({mid['proposal']})")
    check("question" not in mid["kinds"],
          f"and no question event yet ({mid['kinds']})")
    check(not cancel_midway,
          "a concurrent cancel is ordered AFTER the decision, not through it")

    check(applied is True, "the decision then applies")
    check(end["proposal"] is not None, "and its proposal exists")
    check("question" in end["kinds"], f"with its event ({end['kinds']})")
    check(end["status"] == "cancelled",
          f"and the user's cancel still took effect afterwards ({end['status']})")
    check(end["generation"] == 1,
          f"on a new lifecycle run ({end['generation']})")


async def test_a_final_apply_is_one_indivisible_act():
    check.section("C5-C: nothing of a final decision is visible mid-apply")

    mid, end, applied, cancel_midway, task_id = await _blocked_apply("final")

    check(mid["status"] == "active",
          f"mid-transaction the goal is NOT yet completed ({mid['status']})")
    check("complete" not in mid["kinds"],
          f"and the authoritative complete event has not landed ({mid['kinds']})")
    check(not cancel_midway, "the cancel is ordered after the decision")
    check(applied is True, "the decision then applies")
    check("complete" in end["kinds"], f"and its event exists ({end['kinds']})")


async def test_nothing_escapes_late_in_the_transaction_either():
    check.section("C5: the LAST write is inside the boundary too")

    # Blocking immediately before the progress event: by now the gate, any
    # proposal and the decision-task completion have all executed. If any of
    # them had its own commit, an observer would see it here.
    for kind, expect_status in (("question", "paused"),
                                ("final", "completed"),
                                ("tool", "active")):
        mid, end, applied, cancel_midway, _ = await _blocked_apply(kind, at="event")

        check(mid["status"] != expect_status or kind == "tool",
              f"{kind}: the goal has NOT yet moved to {expect_status} "
              f"({mid['status']})")
        check(not mid["kinds"],
              f"{kind}: no progress event of any kind is visible ({mid['kinds']})")
        done = [t for t in mid["tasks"] if t["status"] == "done"]
        check(not done,
              f"{kind}: the decision task is not yet marked done ({len(done)})")
        if kind == "question":
            check(mid["proposal"] is None,
                  f"question: and no proposal escaped ({mid['proposal']})")
        if kind == "tool":
            queued = [t for t in mid["tasks"] if t["tool_name"] == "demo.spy"]
            check(not queued,
                  f"tool: and no tool task escaped ({len(queued)})")
        check(not cancel_midway, f"{kind}: the cancel is still ordered after")
        check(applied is True, f"{kind}: and the decision then applies whole")


async def test_a_tool_apply_schedules_both_halves_or_neither():
    check.section("C5-B: a tool decision and its continuation land together")

    mid, end, applied, cancel_midway, _ = await _blocked_apply("tool")

    mid_queued = [t for t in mid["tasks"] if t["status"] == "queued"]
    check(not any(t["tool_name"] == "demo.spy" for t in mid_queued),
          f"mid-transaction the tool is not yet runnable ({len(mid_queued)})")
    check("plan" not in mid["kinds"],
          f"and no 'Next: demo.spy' has been announced ({mid['kinds']})")
    check(not cancel_midway, "the cancel is ordered after the decision")

    check(applied is True, "the decision then applies")
    names = sorted(t["tool_name"] for t in end["tasks"])
    check("demo.spy" in names, f"the tool was scheduled ({names})")
    check(names.count("__decide__") >= 2,
          f"and so was its continuation, in the same act ({names})")
    check("plan" in end["kinds"], f"with one plan event ({end['kinds']})")


# ── STALENESS: the decision loses the race entirely ──────────────────────────

class _GatedSupervisor:
    """A supervisor whose FIRST decision blocks until the test releases it."""

    def __init__(self, mem, decision: str, spy_calls: list) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        llm = ScriptedLLM()
        llm.default_reply = decision

        async def spy(_args):
            spy_calls.append(1)
            return {"ok": True}

        self.router = ToolRouter({"demo.spy": spy}, {"demo.spy": "a spy tool"})
        self.sup = AgentSupervisor(
            memory=mem, llm=llm, router=self.router,
            tool_descriptions={"demo.spy": "spy"},
            cfg=SupervisorConfig(tick_seconds=0.05, max_retries=1,
                                 max_steps_per_goal=8),
        )
        original = self.sup._decide_next
        self.calls = 0

        async def gated(**kw):
            self.calls += 1
            if self.calls == 1:
                self.entered.set()
                await self.release.wait()
                return await original(**kw)
            return {"type": "__inert_for_test__"}

        self.sup._decide_next = gated


async def _stale_race(decision: str, *, blind_precheck: bool = False):
    """Cancel + resume while an old decision is being made, then release it.

    `blind_precheck` forces the cheap pre-check to pass, so what has to stop
    the stale decision is the gate inside the applying transaction itself.
    """
    spy_calls: list = []
    with _tmp() as td:
        m = await _mem(td)
        gated = _GatedSupervisor(m, decision, spy_calls)
        if blind_precheck:
            async def _always(_g, _n):
                return True
            gated.sup._generation_is_current = _always

        gid = await _goal(m)
        await m.enqueue_goal_task(goal_id=gid, project_name="alpha",
                                  tool_name="__decide__", args={})
        gated.sup.start()
        try:
            await asyncio.wait_for(gated.entered.wait(), timeout=15.0)
            await m.cancel_goal(goal_id=gid)
            await m.resume_goal(goal_id=gid)
            gated.release.set()
            for _ in range(120):
                await asyncio.sleep(0.05)
                rows = await m.list_goal_tasks(goal_id=str(gid), limit=50)
                if any(t["status"] in ("failed", "superseded") for t in rows):
                    break
        finally:
            await gated.sup.stop()
        return await _state(m, gid), spy_calls


async def test_a_stale_question_leaves_no_proposal_and_no_event():
    check.section("C5-A: a superseded question applies NOTHING")

    for blind in (False, True):
        end, _ = await _stale_race(
            '{"type": "question", "message": "Which database?"}',
            blind_precheck=blind)
        tag = "(blind pre-check)" if blind else ""
        check(end["proposal"] is None,
              f"no stale pending proposal {tag} ({end['proposal']})")
        check("question" not in end["kinds"],
              f"no stale question event {tag} ({end['kinds']})")
        stale = [t for t in end["tasks"]
                 if t["status"] == "superseded" and "discarded" in (t["last_error"] or "")]
        check(len(stale) == 1,
              f"the old decision is recorded as discarded, not done {tag} ({len(stale)})")
        check(end["status"] == "active",
              f"and the resumed run survives {tag} ({end['status']})")


async def test_a_stale_final_never_completes_the_resumed_goal():
    check.section("C5-C: a superseded final applies NOTHING")

    for blind in (False, True):
        end, _ = await _stale_race('{"type": "final", "message": "All done."}',
                                   blind_precheck=blind)
        tag = "(blind pre-check)" if blind else ""
        check(end["status"] != "completed",
              f"the resumed goal is not completed {tag} ({end['status']})")
        check("complete" not in end["kinds"],
              f"and no authoritative complete event landed {tag} ({end['kinds']})")


async def test_a_stale_tool_schedules_nothing_and_announces_nothing():
    check.section("C5-B: a superseded tool applies NOTHING")

    for blind in (False, True):
        end, spy = await _stale_race(
            '{"type": "tool", "name": "demo.spy", "args": {}}',
            blind_precheck=blind)
        tag = "(blind pre-check)" if blind else ""
        runnable = [t for t in end["tasks"]
                    if t["tool_name"] == "demo.spy" and t["status"] in ("queued", "running")]
        check(not runnable,
              f"no orphan runnable stale tool {tag} ({len(runnable)})")
        check("plan" not in end["kinds"],
              f"and no false 'Next: demo.spy' {tag} ({end['kinds']})")
        check(not spy, f"and the tool never executed {tag} ({len(spy)})")


# ── AUTHORITY: generation equality alone is not enough ───────────────────────

async def test_the_same_generation_but_paused_is_refused():
    check.section("C5: generation equality is not authority to apply")

    with _tmp() as td:
        m = await _mem(td)
        gid = await _goal(m)
        for _ in range(4):
            await m.cancel_goal(goal_id=gid)
            await m.resume_goal(goal_id=gid)
        await m.update_goal_status(goal_id=gid, status="paused")
        g = await m.get_goal(goal_id=gid)
        check(g["generation"] == 4 and g["status"] == "paused",
              f"the goal is paused on generation 4 ({g['generation']}/{g['status']})")

        ok = await m.update_goal_status(goal_id=gid, status="completed",
                                        expected_generation=4)
        check(ok is False,
              "a decision-status update fenced to generation 4 is REFUSED")
        after = await m.get_goal(goal_id=gid)
        check(after["status"] == "paused",
              f"and the goal is untouched ({after['status']})")

        for label, coro in (
            ("question", m.apply_question_decision(
                goal_id=gid, project_name="alpha", expected_generation=4,
                task_id="none", message="?")),
            ("final", m.apply_final_decision(
                goal_id=gid, project_name="alpha", expected_generation=4,
                task_id="none", message="done")),
            ("tool", m.apply_tool_decision(
                goal_id=gid, project_name="alpha", expected_generation=4,
                task_id="none", tool_name="demo.spy", args={})),
        ):
            check(await coro is False, f"apply_{label}_decision refuses it too")

        end = await _state(m, gid)
        check(end["proposal"] is None, "and nothing was written by any of them")
        check(not [t for t in end["tasks"] if t["tool_name"] == "demo.spy"],
              "no tool task appeared")
        check(end["status"] == "paused", f"goal still paused ({end['status']})")


async def test_an_unfenced_transition_still_works():
    check.section("C5: cancel and resume are not decisions and are unaffected")

    with _tmp() as td:
        m = await _mem(td)
        gid = await _goal(m)
        await m.update_goal_status(goal_id=gid, status="paused")
        # No expected_generation: the operator/lifecycle routes must still be
        # able to move a goal out of a non-active state.
        ok = await m.update_goal_status(goal_id=gid, status="active")
        check(ok is True, "an unfenced status change from paused still applies")
        cancelled = await m.cancel_goal(goal_id=gid)
        check(cancelled is not None, "cancel still works")
        resumed = await m.resume_goal(goal_id=gid)
        check(resumed is not None, "and resume still works")


async def main():
    await test_a_question_apply_is_one_indivisible_act()
    await test_a_final_apply_is_one_indivisible_act()
    await test_a_tool_apply_schedules_both_halves_or_neither()
    await test_nothing_escapes_late_in_the_transaction_either()
    await test_a_stale_question_leaves_no_proposal_and_no_event()
    await test_a_stale_final_never_completes_the_resumed_goal()
    await test_a_stale_tool_schedules_nothing_and_announces_nothing()
    await test_the_same_generation_but_paused_is_refused()
    await test_an_unfenced_transition_still_works()
    check.finish()


if __name__ == "__main__":
    run(main)
