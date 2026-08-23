"""A decision made before a cancel must not land on the goal you resumed.

The race, which survived the cancel/resume work in PR #60:

    a __decide__ is claimed
    the model call blocks
    the user CANCELS the goal
    the user immediately RESUMES it        -> the goal is active again
    the old model result is released       -> and is applied

Because the goal was active again, a decision from before the cancellation
could enqueue a tool into the resumed run, pause it with a stale question, or
complete it with a stale final. The existing protections only refused work
while the goal was *cancelled*, and by then it no longer was.

Every goal now carries a generation. CANCEL opens a new one, so anything
decided in the interrupted run is fenced out — checked INSIDE each write, not
by a SELECT that could go stale before the UPDATE. Resume deliberately does not
bump: eight concurrent resumes would otherwise open eight runs and create eight
continuations.

Barriers and events throughout; no sleep is used for synchronisation.

Run:  venv\\Scripts\\python.exe tests\\test_goal_generation_fence.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from uuid import UUID

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


class _GatedSupervisor:
    """A supervisor whose decision call blocks until the test releases it."""

    def __init__(self, mem, decision: str, spy_calls: list) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self._decision = decision
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
                # The FIRST decision is the one made before the cancel. The
                # model is "thinking" from here…
                self.entered.set()
                await self.release.wait()      # …until the test says otherwise
                return await original(**kw)
            # Every later decision belongs to the resumed run and must be
            # INERT, otherwise the fresh continuation would apply the same
            # scripted answer and the test could not tell which one landed.
            return {"type": "__inert_for_test__"}

        self.sup._decide_next = gated


async def _run_race(decision: str, *, blind_precheck: bool = False):
    """cancel -> resume -> release an old decision. Returns the end state.

    `blind_precheck` forces `_generation_is_current` to answer True, simulating
    the genuine race the cheap pre-check cannot close: it passes, and THEN the
    goal is cancelled and resumed before the write lands. What must stop the
    stale decision at that point is the fence inside each write itself.
    """
    spy_calls: list = []
    with _tmp() as td:
        mem = MemoryUnifier(Path(td), enable_chroma=False)
        await mem.initialize()
        gated = _GatedSupervisor(mem, decision, spy_calls)
        if blind_precheck:
            async def _always_current(_goal_id, _generation):
                return True
            gated.sup._generation_is_current = _always_current

        gid = await mem.create_goal(project_name="alpha", title="t",
                                    objective="o", success_criteria="c")
        await mem.enqueue_goal_task(goal_id=gid, project_name="alpha",
                                    tool_name="__decide__", args={})
        before = await mem.get_goal(goal_id=gid)

        gated.sup.start()
        try:
            # 1. the old decide is claimed and the model blocks
            await asyncio.wait_for(gated.entered.wait(), timeout=15.0)

            # 2. cancel, then IMMEDIATELY resume
            await mem.cancel_goal(goal_id=gid)
            resumed = await mem.resume_goal(goal_id=gid)

            after_resume = await mem.get_goal(goal_id=gid)
            fresh = [t for t in await mem.list_goal_tasks(goal_id=str(gid), limit=50)
                     if t["tool_name"] == "__decide__" and t["status"] == "queued"]

            # 3. release the pre-cancel decision
            gated.release.set()
            for _ in range(80):
                await asyncio.sleep(0.05)
                rows = await mem.list_goal_tasks(goal_id=str(gid), limit=50)
                if any(t["status"] == "failed" for t in rows):
                    break
        finally:
            await gated.sup.stop()

        return {
            "goal_before": before,
            "goal_after_resume": after_resume,
            "goal_final": await mem.get_goal(goal_id=gid),
            "tasks": await mem.list_goal_tasks(goal_id=str(gid), limit=50),
            "fresh_decides": fresh,
            "resumed": resumed,
            "spy_calls": len(spy_calls),
            "mem": None,
        }


async def test_generation_moves_on_cancel_and_resume():
    check.section("C6: cancel and resume each open a new lifecycle run")

    with _tmp() as td:
        mem = MemoryUnifier(Path(td), enable_chroma=False)
        await mem.initialize()
        gid = await mem.create_goal(project_name="alpha", title="t",
                                    objective="o", success_criteria="c")
        g0 = await mem.get_goal(goal_id=gid)
        check(g0.get("generation") == 0, f"a new goal starts at 0 ({g0.get('generation')})")

        await mem.cancel_goal(goal_id=gid)
        g1 = await mem.get_goal(goal_id=gid)
        check(g1["generation"] == 1, f"cancel bumps it ({g1['generation']})")

        await mem.resume_goal(goal_id=gid)
        g2 = await mem.get_goal(goal_id=gid)
        # Resume does NOT bump: cancel already opened the new run, and bumping
        # per resume would give eight concurrent resumes eight generations —
        # and eight continuations.
        check(g2["generation"] == 1,
              f"resume stays in the run cancel opened ({g2['generation']})")

        # The fresh continuation belongs to the NEW run.
        rows = await mem.list_goal_tasks(goal_id=str(gid), limit=20)
        live = [t for t in rows if t["status"] == "queued"]
        check(len(live) == 1, f"exactly one runnable continuation ({len(live)})")

        # A write fenced to the OLD generation is refused; the current one works.
        stale = await mem.enqueue_goal_task(goal_id=gid, project_name="alpha",
                                            tool_name="demo.spy", args={},
                                            expected_generation=0)
        check(stale is None, f"a task fenced to run 0 is refused ({stale})")
        current = await mem.enqueue_goal_task(goal_id=gid, project_name="alpha",
                                              tool_name="demo.spy", args={},
                                              expected_generation=1)
        check(current is not None, "a task fenced to the current run is accepted")

        check(await mem.update_goal_status(goal_id=gid, status="completed",
                                           expected_generation=0) is False,
              "a status change fenced to run 0 is refused")
        check(await mem.update_goal_status(goal_id=gid, status="completed",
                                           expected_generation=1) is True,
              "and to the current run is applied")


async def test_a_stale_tool_decision_never_runs():
    check.section("C6: a pre-cancel TOOL decision is discarded")

    out = await _run_race('{"type":"tool","name":"demo.spy","args":{}}')

    check(out["spy_calls"] == 0,
          f"the stale tool NEVER executed ({out['spy_calls']} calls)")
    spy_tasks = [t for t in out["tasks"] if t["tool_name"] == "demo.spy"]
    check(not spy_tasks,
          f"and was never even queued ({[t['tool_name'] for t in out['tasks']]})")
    check(len(out["fresh_decides"]) == 1,
          f"exactly one fresh continuation existed after resume "
          f"({len(out['fresh_decides'])})")
    stale = [t for t in out["tasks"]
             if t["tool_name"] == "__decide__" and t["status"] == "failed"]
    check(bool(stale), f"the old decide is finished, not left running "
                       f"({[(t['tool_name'], t['status']) for t in out['tasks']]})")
    check(any("discarded" in (t.get("last_error") or "") for t in stale),
          f"and says why ({[(t.get('last_error') or '')[:48] for t in stale]})")
    running = [t for t in out["tasks"] if t["status"] == "running"]
    check(not running, f"nothing is stranded running ({len(running)})")


async def test_a_stale_question_does_not_pause_the_resumed_goal():
    check.section("C6: a pre-cancel QUESTION is discarded")

    out = await _run_race('{"type":"question","message":"stale question"}')

    final = out["goal_final"]
    check(final["status"] != "paused",
          f"the resumed goal was NOT paused by stale output ({final['status']})")
    check(final["status"] == "active",
          f"it is still the run the user resumed ({final['status']})")
    stale = [t for t in out["tasks"]
             if t["tool_name"] == "__decide__" and t["status"] == "failed"]
    check(bool(stale), "the stale decision is finished as discarded")


async def test_a_stale_final_does_not_complete_the_resumed_goal():
    check.section("C6: a pre-cancel FINAL is discarded")

    out = await _run_race('{"type":"final","message":"stale final"}')

    final = out["goal_final"]
    check(final["status"] != "completed",
          f"the resumed goal was NOT completed by stale output ({final['status']})")
    check(final["status"] == "active",
          f"the resumed run is intact ({final['status']})")
    stale = [t for t in out["tasks"]
             if t["tool_name"] == "__decide__" and t["status"] == "failed"]
    check(bool(stale), "the stale decision is finished as discarded")


async def test_the_old_decide_is_not_the_resumed_continuation():
    check.section("C6: the resumed run gets its OWN continuation")

    out = await _run_race('{"type":"tool","name":"demo.spy","args":{}}')

    decides = [t for t in out["tasks"] if t["tool_name"] == "__decide__"]
    check(len(decides) >= 2,
          f"the old decide and the fresh one are different rows ({len(decides)})")
    fresh_ids = {t["task_id"] for t in out["fresh_decides"]}
    # Identified by the discard REASON, not merely by having failed: the fresh
    # continuation also finishes as failed here, because this test feeds the
    # resumed run a deliberately inert decision.
    discarded_ids = {t["task_id"] for t in decides
                     if "discarded" in (t.get("last_error") or "")}
    check(len(discarded_ids) == 1,
          f"exactly one decision was discarded as stale ({len(discarded_ids)})")
    check(fresh_ids and not (fresh_ids & discarded_ids),
          f"and it is NOT the continuation resume created "
          f"(fresh={len(fresh_ids)}, discarded={len(discarded_ids)})")
    check(out["resumed"]["continuation_enqueued"] is True,
          f"and resume really created it ({out['resumed']})")
    check(all(t["project_name"] == "alpha" for t in out["tasks"]),
          f"every task stayed in the goal's own project "
          f"({sorted({t['project_name'] for t in out['tasks']})})")


async def test_the_write_fence_holds_without_the_precheck():
    """The pre-check is a courtesy; the write is the boundary.

    A mutation run proved this was needed: removing `expected_generation` from
    the three writes left the suite green, because the cheap pre-check
    discarded the decision first. But that check cannot be the boundary — the
    goal can change between it and the write, which is the whole race. So here
    the pre-check is forced to answer "current" and the writes have to hold on
    their own.
    """
    check.section("C6: each write refuses a stale generation by itself")

    tool = await _run_race('{"type":"tool","name":"demo.spy","args":{}}',
                           blind_precheck=True)
    check(tool["spy_calls"] == 0,
          f"TOOL: the stale tool still never ran ({tool['spy_calls']})")
    check(not [t for t in tool["tasks"] if t["tool_name"] == "demo.spy"],
          f"and was never queued "
          f"({[t['tool_name'] for t in tool['tasks']]})")

    question = await _run_race('{"type":"question","message":"stale"}',
                               blind_precheck=True)
    check(question["goal_final"]["status"] != "paused",
          f"QUESTION: the resumed goal was not paused "
          f"({question['goal_final']['status']})")

    final = await _run_race('{"type":"final","message":"stale"}',
                            blind_precheck=True)
    check(final["goal_final"]["status"] != "completed",
          f"FINAL: the resumed goal was not completed "
          f"({final['goal_final']['status']})")


async def test_resume_is_still_idempotent_under_concurrency():
    check.section("C6: concurrent resumes still make ONE continuation")

    with _tmp() as td:
        mem = MemoryUnifier(Path(td), enable_chroma=False)
        await mem.initialize()
        gid = await mem.create_goal(project_name="alpha", title="t",
                                    objective="o", success_criteria="c")
        await mem.cancel_goal(goal_id=gid)

        results = await asyncio.gather(*[mem.resume_goal(goal_id=gid)
                                         for _ in range(8)])
        created = [r for r in results if r and r["continuation_enqueued"]]
        rows = await mem.list_goal_tasks(goal_id=str(gid), limit=50)
        live = [t for t in rows if t["tool_name"] == "__decide__"
                and t["status"] in ("queued", "running")]
        check(len(created) == 1,
              f"exactly one resume created the continuation ({len(created)})")
        check(len(live) == 1, f"and exactly one is runnable ({len(live)})")
        check(all(r["project_name"] == "alpha" for r in results if r),
              "all of them report the goal's own project")


async def main():
    await test_generation_moves_on_cancel_and_resume()
    await test_a_stale_tool_decision_never_runs()
    await test_a_stale_question_does_not_pause_the_resumed_goal()
    await test_a_stale_final_does_not_complete_the_resumed_goal()
    await test_the_old_decide_is_not_the_resumed_continuation()
    await test_the_write_fence_holds_without_the_precheck()
    await test_resume_is_still_idempotent_under_concurrency()
    check.finish()


if __name__ == "__main__":
    run(main)
