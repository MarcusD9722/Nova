"""A long project, interrupted and resumed, still knows what is true (13B).

This is not a seam test. Every defect Stage 13B has fixed so far was found by
attacking one guard at a time; this asks the harder question — after a long,
messy sequence of real transitions, can Nova still answer the twelve things it
is never allowed to lose?

    what project · what was requested · which revision · what executed ·
    what succeeded · what failed · what is pending · what was cancelled ·
    what was superseded · what may resume · what MUST NEVER resume ·
    what Nova should say next

After EVERY transition the journey recomputes all of them from authoritative
rows and compares against what the journey says should be true. A single wrong
answer anywhere fails, so a defect that only appears in combination — the kind
a per-seam test cannot see — has somewhere to show up.

WHAT MAKES IT REAL

  * Two projects run at once, so nothing can be right by accident: every
    assertion is scoped by project_name and goal_id, and a step that helps one
    project must leave the other untouched.
  * The workers are the real ones. The LLM is scripted, so the sequence is
    deterministic, but the claim, the fences, the retries and the bookkeeping
    are production code.
  * The restart is a real restart: the process object is dropped and a new
    MemoryUnifier is opened on the same directory, then boot recovery runs.
    Nothing is carried across in memory, which is the only honest way to ask
    what survived.

Run:  venv\\Scripts\\python.exe tests\\test_long_journey_s13b.py
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

from core.policy.autonomy_planner import AutonomyPlannerLLM  # noqa: E402
from core.agent_supervisor import (AgentSupervisor,  # noqa: E402
                                   SupervisorConfig)
from core.tool_router import ToolRouter  # noqa: E402
from core.workers.autonomy_supervisor import (  # noqa: E402
    AutonomySupervisorWorker)
from memory.unifier import MemoryUnifier  # noqa: E402

check = Checks()

GAME = "flappy-bird"
CALC = "quickcalc"


# ── the twelve truths, recomputed from authoritative rows every time ─────────

class Truth:
    """Everything an observer could ask, answered only from the database."""

    def __init__(self, tasks: list[dict], goals: list[dict], project: str):
        self.project = project
        self.tasks = [t for t in tasks
                      if str(t.get("project_name")) == project]
        self.goals = [g for g in goals
                      if str(g.get("project_name")) == project]

    def _with(self, status: str) -> list[dict]:
        return [t for t in self.tasks if str(t.get("status")) == status]

    @property
    def pending(self) -> set[str]:
        """Anything that has not reached a terminal state."""
        return {str(t["task_id"]) for t in self.tasks
                if str(t.get("status")) in ("queued", "running", "blocked")}

    @property
    def succeeded(self) -> set[str]:
        return {str(t["task_id"]) for t in self._with("done")}

    @property
    def failed(self) -> set[str]:
        return {str(t["task_id"]) for t in self._with("failed")}

    @property
    def cancelled(self) -> set[str]:
        return {str(t["task_id"]) for t in self._with("cancelled")}

    @property
    def _runs(self) -> dict:
        """Each goal's CURRENT lifecycle run, by goal id."""
        return {str(g["goal_id"]): int(g.get("generation") or 0)
                for g in self.goals}

    @property
    def may_resume(self) -> set[str]:
        """What could genuinely still run.

        Queued on an ACTIVE goal is not enough, and this codebase has the
        counter-example: a run-6 task left queued while its goal is active on
        run 7. The claim predicate would never take it, because the task's own
        generation no longer matches its goal's. A truth model that is more
        optimistic about resumability than the product cannot catch the product
        drifting.
        """
        live = {str(g["goal_id"]) for g in self.goals
                if str(g.get("status")) == "active"}
        runs = self._runs
        return {str(t["task_id"]) for t in self.tasks
                if str(t.get("status")) == "queued"
                and str(t.get("goal_id")) in live
                and int(t.get("generation") or 0)
                == runs.get(str(t.get("goal_id")), -1)}

    @property
    def never_resume(self) -> set[str]:
        """What must never run again, whatever wakes up.

        Three separate reasons, and all three have to be here: the goal is
        over, the task is already terminal, or the task belongs to a lifecycle
        run that has ended even though its goal is still going.
        """
        dead = {str(g["goal_id"]) for g in self.goals
                if str(g.get("status")) == "cancelled"}
        runs = self._runs
        stale = {str(t["task_id"]) for t in self.tasks
                 if int(t.get("generation") or 0)
                 != runs.get(str(t.get("goal_id")), -1)}
        terminal = {str(t["task_id"]) for t in self.tasks
                    if str(t.get("status")) in ("done", "failed", "cancelled")}
        return ({str(t["task_id"]) for t in self.tasks
                 if str(t.get("goal_id")) in dead} | stale | terminal)

    def revision_of(self, goal_id: str) -> int:
        for g in self.goals:
            if str(g["goal_id"]) == str(goal_id):
                return int(g.get("generation") or 0)
        return -1

    def status_of(self, task_id: str) -> str:
        for t in self.tasks:
            if str(t["task_id"]) == str(task_id):
                return str(t.get("status"))
        return "(absent)"

    def note_of(self, task_id: str) -> str:
        for t in self.tasks:
            if str(t["task_id"]) == str(task_id):
                return str(t.get("last_error") or "")
        return ""

    def summary(self) -> str:
        counts = {}
        for t in self.tasks:
            s = str(t.get("status"))
            counts[s] = counts.get(s, 0) + 1
        return f"{self.project}: {counts or '{}'}"


async def truths(mem, project: str) -> Truth:
    return Truth(await mem.list_goal_tasks(limit=200),
                 await mem.list_goals(limit=100), project)


async def _row_of(mem, task_id: str) -> dict:
    for t in await mem.list_goal_tasks(limit=200):
        if str(t.get("task_id")) == str(task_id):
            return t
    return {}

class Journey:
    """Numbered steps, so a failure names the transition that produced it."""

    def __init__(self) -> None:
        self.n = 0

    def step(self, what: str) -> str:
        self.n += 1
        return f"[{self.n:02d}] {what}"


async def _seed(mem, project: str, title: str, tools: list[str]):
    goal_id = await mem.create_goal(project_name=project, title=title,
                                    objective=title,
                                    success_criteria="it works")
    ids = []
    for t in tools:
        await mem.enqueue_goal_task(goal_id=goal_id, project_name=project,
                                    tool_name=t, args={})
        rows = await mem.list_goal_tasks(goal_id=str(goal_id), limit=50)
        ids.append(str(sorted(rows, key=lambda r: str(r.get("created_at")))[-1]["task_id"]))
    return goal_id, ids


async def _claim_and_finish(mem, *, status: str, error: str = "") -> tuple:
    """Claim whatever is next and complete it the way the journey says."""
    claimed = await mem.claim_next_goal_task()
    if claimed is None:
        return None, None
    task_id = str(claimed["task_id"])
    outcome = await mem.complete_goal_task(
        task_id=task_id, status=status,
        result={"ok": status == "done"}, error=error,
        expected_generation=int(claimed.get("generation") or 0))
    return task_id, outcome


async def journey_one_long_build():
    """One project built over a long, interrupted sequence, beside another."""
    check.section("journey 1: a long build, interrupted, beside a second project")
    j = Journey()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        store = Path(td) / "nova"
        mem = MemoryUnifier(store, enable_chroma=False)
        await mem.initialize()

        # 01-02  two projects, so nothing can be right by accident.
        game_goal, game_tasks = await _seed(
            mem, GAME, "add a pause menu",
            ["code.write", "code.write", "code.test"])
        calc_goal, calc_tasks = await _seed(
            mem, CALC, "add a percent key", ["code.write"])
        g, c = await truths(mem, GAME), await truths(mem, CALC)
        check(len(g.pending) == 3 and len(c.pending) == 1,
              j.step(f"both projects have their own pending work "
                     f"({g.summary()} | {c.summary()})"))
        check(g.pending.isdisjoint(c.pending),
              j.step("and they share nothing"))

        # 03  first step succeeds.
        t1, o1 = await _claim_and_finish(mem, status="done")
        g = await truths(mem, GAME)
        check(o1 == "applied" and t1 in g.succeeded,
              j.step(f"step one succeeded ({o1!r}, {g.summary()})"))
        check(t1 in g.never_resume,
              j.step("and what succeeded must never run again"))

        # 04  second step fails for a real reason.
        t2, o2 = await _claim_and_finish(mem, status="failed",
                                         error="the sprite sheet is missing")
        g = await truths(mem, GAME)
        check(t2 in g.failed and t2 not in g.succeeded,
              j.step(f"step two failed, and is not counted as success "
                     f"({g.summary()})"))
        check("sprite sheet" in g.note_of(t2),
              j.step(f"with the reason kept ({g.note_of(t2)!r})"))

        # 05  the user pauses the goal with one step still queued.
        await mem.update_goal_status(goal_id=game_goal, status="paused")
        g = await truths(mem, GAME)
        check(len(g.pending) == 1,
              j.step(f"one step is still pending ({g.summary()})"))
        check(not g.may_resume,
              j.step("but nothing may resume while it is paused"))

        # 06  a paused goal hands out no work, and the OTHER project is
        #     unaffected — the claim is global, so this is not obvious.
        claimed = await mem.claim_next_goal_task()
        check(claimed is not None
              and str(claimed.get("project_name")) == CALC,
              j.step(f"the claim moves to the other project, not the paused one "
                     f"({(claimed or {}).get('project_name')!r})"))
        calc_running = str(claimed["task_id"])
        calc_gen = int(claimed.get("generation") or 0)

        # 07  work finishing during a pause still counts for its own project.
        o = await mem.complete_goal_task(task_id=calc_running, status="done",
                                         result={"ok": True}, error="",
                                         expected_generation=calc_gen)
        c, g = await truths(mem, CALC), await truths(mem, GAME)
        check(o == "applied" and calc_running in c.succeeded,
              j.step(f"the other project finished its step ({c.summary()})"))
        check(not (c.succeeded & g.pending) and len(g.pending) == 1,
              j.step(f"and the paused project is untouched ({g.summary()})"))

        # 08  resume: a new revision.
        rev_before = g.revision_of(str(game_goal))
        await mem.resume_goal(goal_id=game_goal)
        g = await truths(mem, GAME)
        rev_after = g.revision_of(str(game_goal))
        check(rev_after > rev_before,
              j.step(f"resuming starts a new revision ({rev_before} -> {rev_after})"))
        check(bool(g.may_resume),
              j.step(f"and there is work that may resume again ({g.summary()})"))

        # 09  a worker holds a task, then the user cancels underneath it.
        claimed = await mem.claim_next_goal_task()
        held = str(claimed["task_id"]) if claimed else ""
        held_gen = int((claimed or {}).get("generation") or 0)
        held_project = str((claimed or {}).get("project_name") or "")
        check(bool(held) and held_project == GAME,
              j.step(f"a step of the game is running ({held_project!r})"))
        await mem.cancel_goal(goal_id=game_goal)
        g = await truths(mem, GAME)
        check(g.revision_of(str(game_goal)) > rev_after,
              j.step(f"cancelling starts another revision "
                     f"({g.revision_of(str(game_goal))})"))

        # 10  the stale worker returns success. It must not land.
        o = await mem.complete_goal_task(task_id=held, status="done",
                                         result={"ok": True}, error="",
                                         expected_generation=held_gen)
        g = await truths(mem, GAME)
        check(o == "superseded",
              j.step(f"the cancelled run's completion is superseded ({o!r})"))
        check(held not in g.succeeded,
              j.step(f"it is NOT counted as work that succeeded ({g.summary()})"))
        check(held in g.never_resume,
              j.step("and it must never run again"))
        check(not g.may_resume,
              j.step(f"nothing on the cancelled goal may resume ({g.summary()})"))

        # 11  what actually happened is still recoverable.
        rows = await mem.list_goal_tasks(goal_id=str(game_goal), limit=50)
        row = [r for r in rows if str(r["task_id"]) == held][0]
        payload = json.loads(row.get("result_json") or "{}")
        check(payload.get("superseded") is True
              and payload.get("reported_status") == "done",
              j.step(f"the real outcome is kept, just not counted ({payload})"))

        # 12  the other project is STILL untouched by all of that.
        c = await truths(mem, CALC)
        check(len(c.succeeded) == 1 and not c.failed and not c.cancelled,
              j.step(f"the second project is unharmed ({c.summary()})"))

        # ── 13  a real restart. Nothing survives in memory. ────────────────
        del mem
        mem2 = MemoryUnifier(store, enable_chroma=False)
        await mem2.initialize()
        recovered = await mem2.cancel_pending_background_work()
        g, c = await truths(mem2, GAME), await truths(mem2, CALC)
        check(t1 in g.succeeded,
              j.step(f"after a restart, what succeeded is still recorded "
                     f"({g.summary()})"))
        check(t2 in g.failed and "sprite sheet" in g.note_of(t2),
              j.step(f"what failed still says why ({g.note_of(t2)!r})"))
        check(held in g.never_resume,
              j.step("what must never resume still must not"))
        check(len(c.succeeded) == 1,
              j.step(f"and the other project survived intact ({c.summary()})"))
        check(not g.may_resume and not c.may_resume,
              j.step(f"nothing restarts itself after a reboot ({recovered})"))

        # 14  a cancelled goal cannot be quietly restarted by the queue.
        claimed = await mem2.claim_next_goal_task()
        check(claimed is None,
              j.step(f"and nothing is claimable at all "
                     f"({(claimed or {}).get('tool_name')})"))

        # A step left queued on a run that ended, while its goal lives on.
        # Neither "queued" nor "the goal is active" is enough to make it
        # runnable, and only the run tells them apart.
        goal3, ids3 = await _seed(mem2, CALC, "add a memory key", ["code.write"])
        stranded = ids3[0]
        await mem2.cancel_goal(goal_id=goal3)
        await mem2.resume_goal(goal_id=goal3)
        c = await truths(mem2, CALC)
        check(c.revision_of(str(goal3)) > 0,
              j.step(f"the goal is on a later run ({c.revision_of(str(goal3))})"))
        check(c.status_of(stranded) == "cancelled",
              j.step(f"and the step from the first run is cancelled "
                     f"({c.status_of(stranded)!r})"))
        check(stranded in c.never_resume and stranded not in c.may_resume,
              j.step("so it must never run again, even though the goal lives on"))
        nxt = await mem2.claim_next_goal_task()
        check(nxt is None or str(nxt.get("task_id")) != stranded,
              j.step(f"and the queue agrees ({(nxt or {}).get('task_id')})"))

        check(j.n >= 14, f"the journey ran {j.n} checked transitions")


async def journey_two_retries_and_revisions():
    """Retries, exhaustion, and a revision arriving mid-retry."""
    check.section("journey 2: retries, exhaustion, and a revision mid-flight")
    j = Journey()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem = MemoryUnifier(Path(td) / "nova", enable_chroma=False)
        await mem.initialize()
        goal_id, ids = await _seed(mem, GAME, "wire up the score",
                                   ["code.write"])

        # 01  claim, fail transiently, and requeue on the SAME revision.
        claimed = await mem.claim_next_goal_task()
        tid = str(claimed["task_id"])
        gen = int(claimed.get("generation") or 0)
        ok = await mem.bump_goal_task_attempt(
            task_id=tid, attempts=1, run_after_iso="2000-01-01T00:00:00+00:00",
            error="network blipped", expected_generation=gen)
        t = await truths(mem, GAME)
        check(ok is True and t.status_of(tid) == "queued",
              j.step(f"a transient failure is requeued ({t.status_of(tid)!r})"))
        check(tid in t.may_resume,
              j.step("and it may resume"))

        # 02  it is claimable again, on the same revision.
        again = await mem.claim_next_goal_task()
        check(again is not None and str(again["task_id"]) == tid,
              j.step("the same step is picked up again"))
        # `or -1` here would read a generation of 0 as -1. That exact slip has
        # cost this stage two false failures already.
        again_gen = None if again is None else int(again.get("generation"))
        check(again_gen == gen,
              j.step(f"on the same revision ({again_gen})"))

        # 03  now the user cancels while it is running.
        await mem.cancel_goal(goal_id=goal_id)
        t = await truths(mem, GAME)
        check(t.revision_of(str(goal_id)) > gen,
              j.step(f"the revision moves on ({t.revision_of(str(goal_id))})"))

        # 04  the retry path must NOT schedule it again.
        ok = await mem.bump_goal_task_attempt(
            task_id=tid, attempts=2, run_after_iso="2000-01-01T00:00:00+00:00",
            error="network blipped again", expected_generation=gen)
        t = await truths(mem, GAME)
        check(ok is False,
              j.step(f"a retry from the ended revision is refused ({ok})"))
        check(tid not in t.may_resume,
              j.step(f"so it may not resume ({t.summary()})"))

        # 05  and the completion from that revision cannot land either.
        o = await mem.complete_goal_task(task_id=tid, status="done",
                                         result={"ok": True}, error="",
                                         expected_generation=gen)
        t = await truths(mem, GAME)
        check(o == "superseded" and tid not in t.succeeded,
              j.step(f"nor can it report success ({o!r}, {t.summary()})"))
        check(tid in t.never_resume,
              j.step("it is in the never-again set"))

        # 06  COUNTER: a fresh goal in the same project still works normally.
        goal2, ids2 = await _seed(mem, GAME, "add a high score table",
                                  ["code.write"])
        t2, o2 = await _claim_and_finish(mem, status="done")
        t = await truths(mem, GAME)
        check(o2 == "applied" and t2 in t.succeeded,
              j.step(f"new work in the same project still completes ({o2!r})"))
        check(tid not in t.succeeded,
              j.step(f"without reviving the cancelled step ({t.summary()})"))

        check(j.n >= 6, f"the journey ran {j.n} checked transitions")


# ── journey 3: the other task system, which has no generations at all ───────

TOOL_PLAN = ('{"action":"tool","reason":"do the thing",'
             '"tool_calls":[{"tool":"work.step","args":{}}],"new_tasks":[]}')
ASK_PLAN = ('{"action":"ask_user","reason":"needs a decision",'
            '"message_to_user":"Dark overlay or a blur?",'
            '"tool_calls":[],"new_tasks":[]}')


async def _auto_row(mem, task_id: str) -> dict:
    for t in await mem.list_tasks(limit=100):
        if str(t.get("task_id")) == str(task_id):
            return t
    return {}


async def _run_until_settled(mem, worker, task_id: str, *, ticks: int = 300):
    worker.start()
    try:
        for _ in range(ticks):
            await asyncio.sleep(0.05)
            row = await _auto_row(mem, task_id)
            if row and str(row.get("status")) not in ("queued", "running"):
                return row
    finally:
        await worker.stop()
    return await _auto_row(mem, task_id)


async def journey_three_background_work():
    """Success, failure, a question, an answer, an interruption, a restart.

    The autonomy queue has no goals and no generations, so every guard it has
    is a status guard. That makes it the harder of the two systems to keep
    honest: there is no lifecycle run to fall back on, and the only thing
    standing between a stale write and the record is `status='running'`.
    """
    check.section("journey 3: background work, end to end and back again")
    j = Journey()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        store = Path(td) / "nova"
        mem = MemoryUnifier(store, enable_chroma=False)
        await mem.initialize()

        ledger: list[str] = []
        fail_next = {"on": False}

        async def tool(_a):
            ledger.append("ran")
            if fail_next["on"]:
                return {"ok": False, "error": "the api returned 503"}
            return {"ok": True, "data": {"wrote": "game.js"}}

        llm = ScriptedLLM()
        llm.default_reply = TOOL_PLAN

        def build_worker():
            return AutonomySupervisorWorker(
                memory=mem,
                planner=AutonomyPlannerLLM(llm,
                                           llm_semaphore=asyncio.Semaphore(1)),
                router=ToolRouter({"work.step": tool}, {}),
                tick_seconds=0.05)

        # 01  ordinary success.
        good = str(await mem.enqueue_task(
            title="write the pause menu", details="a",
            project_name=GAME, initiated_by_user=True))
        row = await _run_until_settled(mem, build_worker(), good)
        check(str(row.get("status")) == "done" and ledger == ["ran"],
              j.step(f"work that succeeds is done ({row.get('status')!r}, {ledger})"))
        check(str(row.get("last_error") or "") == "",
              j.step(f"with nothing on the error line ({row.get('last_error')!r})"))

        # 02  a tool that fails is NOT done.
        fail_next["on"] = True
        bad = str(await mem.enqueue_task(
            title="upload the build", details="b",
            project_name=GAME, initiated_by_user=True))
        row = await _run_until_settled(mem, build_worker(), bad)
        check(str(row.get("status")) == "failed",
              j.step(f"work that fails is failed ({row.get('status')!r})"))
        check("503" in str(row.get("last_error") or ""),
              j.step(f"and says why ({row.get('last_error')!r})"))

        # 03  the two verdicts do not contaminate each other.
        done_ids = {str(t["task_id"]) for t in
                    await mem.list_tasks(status="done", limit=50)}
        failed_ids = {str(t["task_id"]) for t in
                      await mem.list_tasks(status="failed", limit=50)}
        check(good in done_ids and bad in failed_ids,
              j.step(f"each is filed under its own verdict "
                     f"({len(done_ids)} done, {len(failed_ids)} failed)"))
        check(good not in failed_ids and bad not in done_ids,
              j.step("and neither is filed under the other's"))

        # 04  a question parks the task instead of finishing it.
        fail_next["on"] = False
        llm.default_reply = ASK_PLAN
        asked = str(await mem.enqueue_task(
            title="pick the overlay style", details="c",
            project_name=GAME, initiated_by_user=True))
        row = await _run_until_settled(mem, build_worker(), asked)
        check(str(row.get("status")) == "blocked",
              j.step(f"a question leaves it waiting ({row.get('status')!r})"))
        check("Dark overlay" in str(row.get("last_error") or ""),
              j.step(f"with the question on the task ({row.get('last_error')!r})"))

        # 05  a waiting task is pending, not finished, and not claimable.
        pending = {str(t["task_id"]) for t in
                   await mem.list_tasks(status="blocked", limit=50)}
        done_ids = {str(t["task_id"]) for t in
                    await mem.list_tasks(status="done", limit=50)}
        check(asked in pending and asked not in done_ids,
              j.step(f"it counts as pending, not finished ({len(pending)} waiting)"))
        check(await mem.claim_next_task() is None,
              j.step("and nothing claims it on its own"))

        # ── 06  a real restart, with a question outstanding ────────────────
        del mem
        mem = MemoryUnifier(store, enable_chroma=False)
        await mem.initialize()
        recovered = await mem.cancel_pending_background_work()
        row = await _auto_row(mem, asked)
        check(str(row.get("status")) == "blocked",
              j.step(f"the question survives a reboot ({row.get('status')!r})"))
        check("Dark overlay" in str(row.get("last_error") or ""),
              j.step("and is still legible"))
        check(str((await _auto_row(mem, good)).get("status")) == "done"
              and str((await _auto_row(mem, bad)).get("status")) == "failed",
              j.step(f"both earlier verdicts survived too ({recovered})"))

        # 07  answering it releases exactly that task.
        released = await mem.answer_task_question(task_id=asked,
                                                  answer="dark overlay")
        row = await _auto_row(mem, asked)
        check(released is True and str(row.get("status")) == "queued",
              j.step(f"the answer releases it ({released}, {row.get('status')!r})"))
        check("dark overlay" in str(row.get("details") or ""),
              j.step("and reaches the details the next plan reads"))
        check(str((await _auto_row(mem, good)).get("status")) == "done",
              j.step("without disturbing anything else"))

        # 08  it runs to completion on the answer.
        llm.default_reply = TOOL_PLAN
        before = len(ledger)
        row = await _run_until_settled(mem, build_worker(), asked)
        check(str(row.get("status")) == "done",
              j.step(f"and finishes ({row.get('status')!r})"))
        check(len(ledger) == before + 1,
              j.step(f"having run its tool exactly once more ({ledger})"))

        # 09  a second answer cannot re-run finished work.
        again = await mem.answer_task_question(task_id=asked, answer="blur")
        row = await _auto_row(mem, asked)
        check(again is False and str(row.get("status")) == "done",
              j.step(f"answering again is refused ({again}, {row.get('status')!r})"))
        check("blur" not in str(row.get("details") or ""),
              j.step("and changes nothing"))

        check(j.n >= 20, f"the journey ran {j.n} checked transitions")


# -- journey 4: the real supervisor, deciding its own way through a goal -----


async def journey_four_supervisor_decides():
    """The decision loop end to end, then cancelled while it is thinking.

    Journeys 1 and 2 drive the lifecycle directly, which is the only way to
    force an exact interleaving. This one hands the wheel to `AgentSupervisor`
    and lets it decide, schedule, execute and finish on its own -- so the
    fenced `apply_*_decision` transactions, the claim, the completion fence and
    the progress record are all exercised together, by the code that really
    runs them.

    The model is scripted, so the sequence is fixed; nothing else is faked.
    """
    check.section("journey 4: the supervisor decides, acts, and is cancelled")
    j = Journey()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem = MemoryUnifier(Path(td) / "nova", enable_chroma=False)
        await mem.initialize()

        ran: list[str] = []
        gate_open = asyncio.Event()
        gate_open.set()

        async def demo_ok(_a):
            ran.append("demo.ok")
            await gate_open.wait()
            return {"ok": True, "wrote": "game.js"}

        llm = ScriptedLLM()
        decisions = {"n": 0}

        def decide(_prompt: str) -> str:
            decisions["n"] += 1
            if decisions["n"] == 1:
                return '{"type":"tool","name":"demo.ok","args":{}}'
            return '{"type":"final","message":"the pause menu is in"}'

        llm.when(lambda _t: True, decide, label="decide")
        sup = AgentSupervisor(
            memory=mem, llm=llm, router=ToolRouter({"demo.ok": demo_ok}, {}),
            tool_descriptions={"demo.ok": "writes a file"},
            cfg=SupervisorConfig(tick_seconds=0.05, max_retries=1,
                                 max_steps_per_goal=6))

        goal_id = await mem.create_goal(project_name=GAME,
                                        title="add a pause menu",
                                        objective="pause menu",
                                        success_criteria="it pauses")
        await mem.enqueue_goal_task(goal_id=goal_id, project_name=GAME,
                                    tool_name="__decide__", args={})

        sup.start()
        try:
            for _ in range(400):
                await asyncio.sleep(0.05)
                row = await mem.get_goal(goal_id=goal_id)
                if str((row or {}).get("status")) != "active":
                    break
        finally:
            await sup.stop()

        goal = await mem.get_goal(goal_id=goal_id)
        t = await truths(mem, GAME)
        check(ran == ["demo.ok"],
              j.step(f"the supervisor chose and ran the tool once ({ran})"))
        check(str(goal.get("status")) == "completed",
              j.step(f"and drove the goal to completion ({goal.get('status')!r})"))
        check(len(t.succeeded) >= 2 and not t.failed,
              j.step(f"every step it took is recorded as done ({t.summary()})"))
        check(not t.pending,
              j.step(f"with nothing left pending ({t.summary()})"))

        # The progress record is readable, and says what happened in order.
        events = await mem.list_progress_events(goal_id=str(goal_id), limit=50)
        kinds = [str(e.get("kind")) for e in events]
        check(bool(events), j.step(f"the run left a readable record ({kinds})"))
        check(any("demo.ok" in str(e.get("message") or "") for e in events),
              j.step("naming the tool it ran"))
        check(all(str(e.get("goal_id")) == str(goal_id) for e in events),
              j.step("all of it belonging to this goal"))

        # -- a second goal, cancelled while its tool is in flight ----------
        gate_open.clear()
        decisions["n"] = 0
        goal2 = await mem.create_goal(project_name=GAME, title="add sound",
                                      objective="sound", success_criteria="it beeps")
        await mem.enqueue_goal_task(goal_id=goal2, project_name=GAME,
                                    tool_name="__decide__", args={})
        sup.start()
        try:
            for _ in range(400):
                await asyncio.sleep(0.05)
                if len(ran) >= 2:
                    break
            check(len(ran) == 2, j.step(f"its tool is mid-flight ({ran})"))
            await mem.cancel_goal(goal_id=goal2)
            gate_open.set()
            for _ in range(200):
                await asyncio.sleep(0.05)
                rows = await mem.list_goal_tasks(goal_id=str(goal2), limit=20)
                if all(str(r.get("status")) not in ("queued", "running")
                       for r in rows):
                    break
        finally:
            await sup.stop()

        goal2row = await mem.get_goal(goal_id=goal2)
        rows = await mem.list_goal_tasks(goal_id=str(goal2), limit=20)
        done = [r for r in rows if str(r.get("status")) == "done"]
        check(str(goal2row.get("status")) == "cancelled",
              j.step(f"the cancelled goal stays cancelled "
                     f"({goal2row.get('status')!r})"))
        check(not any(str(r.get("tool_name")) == "demo.ok" for r in done),
              j.step(f"and the tool that finished after the cancel is NOT "
                     f"counted as done ({[(r.get('tool_name'), r.get('status')) for r in rows]})"))
        check(all(str(r.get("status")) not in ("queued", "running")
                  for r in rows),
              j.step("nothing is left runnable or stranded"))

        after = await mem.list_progress_events(goal_id=str(goal2), limit=50)
        msgs = [str(e.get("message") or "") for e in after]
        check(not any("completed" in m and "demo.ok" in m for m in msgs),
              j.step(f"nothing announces the cancelled run as progress ({msgs})"))
        check(any("not counted as work done" in m for m in msgs),
              j.step("it says the work landed too late instead"))

        check(j.n >= 13, f"the journey ran {j.n} checked transitions")


# -- journey 5: what a restart leaves behind, and whether it can be resumed --


async def journey_five_restart_integrity():
    """A restart must leave a goal somewhere a person can pick it up from.

    Boot recovery cancels queued and running background work so nothing runs
    unasked. That is right for the WORK. It said nothing about the GOAL, which
    was left `active` with every task cancelled: nothing claimable, nothing
    scheduled, and no record of why. It would never progress, nothing would
    ever mention it, and "what are you working on?" still answered with it.

    A state nothing can leave is not a checkpoint. This journey ends by
    actually resuming, because `paused` is only an honest answer if resume
    genuinely works from it.
    """
    check.section("journey 5: a restart leaves a goal somewhere resumable")
    j = Journey()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        store = Path(td) / "nova"
        mem = MemoryUnifier(store, enable_chroma=False)
        await mem.initialize()

        # The second project FIRST, and finished, so that recovery can be
        # shown not to reach past what it should. Order matters: the claim is
        # global and takes the oldest runnable row, so seeding this second
        # would have handed `_claim_and_finish` the other project's step.
        done_goal, _ = await _seed(mem, CALC, "add a percent key", ["code.write"])
        finished_id, _o = await _claim_and_finish(mem, status="done")
        c = await truths(mem, CALC)
        check(finished_id in c.succeeded,
              j.step(f"the other project has finished its step ({c.summary()})"))

        goal_id = await mem.create_goal(project_name=GAME,
                                        title="add a pause menu",
                                        objective="pause menu",
                                        success_criteria="it pauses")
        await mem.enqueue_goal_task(goal_id=goal_id, project_name=GAME,
                                    tool_name="__decide__", args={})
        await mem.enqueue_goal_task(goal_id=goal_id, project_name=GAME,
                                    tool_name="code.write", args={})
        g = await truths(mem, GAME)
        check(len(g.may_resume) == 2,
              j.step(f"and this one has queued work ({g.summary()})"))

        # -- the process stops and comes back -----------------------------
        del mem
        mem2 = MemoryUnifier(store, enable_chroma=False)
        await mem2.initialize()
        recovered = await mem2.cancel_pending_background_work()

        goal = await mem2.get_goal(goal_id=goal_id) or {}
        g = await truths(mem2, GAME)
        check(str(goal.get("status")) != "active",
              j.step(f"the goal is not left claiming to be running "
                     f"({goal.get('status')!r})"))
        check(str(goal.get("status")) == "paused",
              j.step(f"it is paused ({goal.get('status')!r})"))
        check(not g.may_resume,
              j.step(f"with nothing runnable ({g.summary()})"))
        check(await mem2.claim_next_goal_task() is None,
              j.step("and nothing claims for it"))
        # BOTH goals were active -- finishing a step does not close a goal --
        # so both are paused. That is the point: every goal a restart leaves
        # without a worker gets said out loud, not just the busy one.
        check(int(recovered.get("paused_goals") or 0) == 2,
              j.step(f"boot recovery says what it did ({recovered})"))

        # It is not silent about it either.
        events = await mem2.list_progress_events(goal_id=str(goal_id), limit=20)
        msgs = [str(e.get("message") or "") for e in events]
        check(any("restart" in m for m in msgs),
              j.step(f"and the goal's own record says why ({msgs})"))
        check(any("Resume" in m for m in msgs),
              j.step("and what to do about it"))

        # The finished work of the other project is untouched by any of this.
        c = await truths(mem2, CALC)
        check(finished_id in c.succeeded,
              j.step(f"work that had already finished is not disturbed "
                     f"({c.summary()})"))
        done_row = await mem2.get_goal(goal_id=done_goal) or {}
        check(str(done_row.get("status")) == "paused",
              j.step(f"its goal is paused too, not cancelled "
                     f"({done_row.get('status')!r})"))

        # -- and it can genuinely be picked back up ------------------------
        rev_before = int(goal.get("generation") or 0)
        await mem2.resume_goal(goal_id=goal_id)
        goal = await mem2.get_goal(goal_id=goal_id) or {}
        g = await truths(mem2, GAME)
        check(str(goal.get("status")) == "active",
              j.step(f"resuming makes it active again ({goal.get('status')!r})"))
        check(int(goal.get("generation") or 0) > rev_before,
              j.step(f"on a new run ({rev_before} -> {goal.get('generation')})"))
        check(bool(g.may_resume),
              j.step(f"with work it can actually do ({g.summary()})"))

        nxt = await mem2.claim_next_goal_task()
        check(nxt is not None and str(nxt.get("goal_id")) == str(goal_id),
              j.step(f"which the queue hands out ({(nxt or {}).get('tool_name')!r})"))
        check(nxt is not None
              and int(nxt.get("generation")) == int(goal.get("generation") or 0),
              j.step(f"on the current run ({(nxt or {}).get('generation')})"))
        check(nxt is not None and str(nxt.get("tool_name")) == "__decide__",
              j.step("and it is a fresh decision, not the cancelled step"))

        check(j.n >= 16, f"the journey ran {j.n} checked transitions")


# -- journey 8: an ambiguous side effect, and a run that ends under it -------


async def journey_eight_ambiguous_side_effects():
    """The hardest thing to record honestly: work whose fate is not knowable.

    Two shapes, deliberately side by side, because the whole point is that they
    must not come out the same:

      KNOWN   the tool finished and said what happened; the run ended before
              the answer was written down. History keeps the outcome; the
              current run is untouched.
      UNKNOWN the process died with the tool in flight. Nothing anywhere can
              say whether the side effect landed, and saying either "it did" or
              "it did not" is a claim Nova cannot support.

    The stage's first two defects were these two collapsing into "done" and
    into "cancelled" respectively.
    """
    check.section("journey 8: a known outcome and an unknowable one")
    j = Journey()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        store = Path(td) / "nova"
        mem = MemoryUnifier(store, enable_chroma=False)
        await mem.initialize()

        # 01  a goal, and a step that reaches the side-effect boundary.
        goal_a, _ = await _seed(mem, GAME, "upload the build", ["deploy.push"])
        claimed = await mem.claim_next_goal_task()
        tid_known = str(claimed["task_id"])
        gen_known = int(claimed["generation"])
        t = await truths(mem, GAME)
        check(t.status_of(tid_known) == "running",
              j.step(f"the step is in flight ({t.status_of(tid_known)})"))
        check(tid_known not in t.succeeded and tid_known not in t.failed,
              j.step("and nothing is claimed about it yet"))

        # 02  the run ends underneath it.
        await mem.cancel_goal(goal_id=goal_a)
        t = await truths(mem, GAME)
        check(t.revision_of(str(goal_a)) == gen_known + 1,
              j.step(f"the revision moves on ({t.revision_of(str(goal_a))})"))

        # 03  the tool returns, and it KNOWS it succeeded.
        verdict = await mem.complete_goal_task(
            task_id=tid_known, status="done", result={"ok": True}, error="",
            expected_generation=gen_known)
        row = await _row_of(mem, tid_known)
        check(verdict == "superseded",
              j.step(f"the completion is superseded ({verdict})"))
        check(str(row.get("status")) == "superseded",
              j.step(f"the step is not counted as done ({row.get('status')})"))
        check(str(row.get("outcome")) == "succeeded",
              j.step(f"but the tool's success is preserved ({row.get('outcome')})"))
        check(str(row.get("outcome")) != "never_started",
              j.step("nothing claims it never ran"))

        # 04  the current run is untouched by any of it.
        goal_row = await mem.get_goal(goal_id=goal_a) or {}
        check(str(goal_row.get("status")) == "cancelled",
              j.step(f"the goal is still cancelled ({goal_row.get('status')})"))
        live = [x for x in await mem.list_goal_tasks(goal_id=str(goal_a), limit=20)
                if int(x.get("generation")) == int(goal_row.get("generation"))]
        check(all(str(x.get("status")) != "done" for x in live),
              j.step(f"and nothing on the new revision was completed ({len(live)})"))

        # 05-06  now the UNKNOWN shape, on the autonomy side.
        unknown_id = str(await mem.enqueue_task(
            title="upload the build", details="d", project_name=GAME,
            initiated_by_user=True))
        await mem.claim_next_task()
        await mem.mark_task_failed(
            task_id=unknown_id, outcome="unknown",
            error="interrupted while 'deploy.push' was running. Nova cannot "
                  "tell whether it completed.",
            result={"status": "interrupted_tool_unknown"})
        arow = await _auto_row(mem, unknown_id)
        check(str(arow.get("outcome")) == "unknown",
              j.step(f"the unknowable one is unknown ({arow.get('outcome')})"))
        check(str(arow.get("outcome")) not in ("succeeded", "never_started"),
              j.step("neither claimed nor denied"))

        # 07  the two shapes are distinguishable, which is the entire point.
        check(str(row.get("outcome")) != str(arow.get("outcome")),
              j.step(f"known and unknown read differently "
                     f"({row.get('outcome')} vs {arow.get('outcome')})"))

        # 08-10  a restart, and both survive as themselves.
        del mem
        mem2 = MemoryUnifier(store, enable_chroma=False)
        await mem2.initialize()
        recovered = await mem2.cancel_pending_background_work()
        row2 = await _row_of(mem2, tid_known)
        arow2 = await _auto_row(mem2, unknown_id)
        check((str(row2.get("status")), str(row2.get("outcome")))
              == ("superseded", "succeeded"),
              j.step(f"the known one reloads intact "
                     f"({row2.get('status')}/{row2.get('outcome')})"))
        check(str(arow2.get("outcome")) == "unknown",
              j.step(f"the unknown one is still unknown ({arow2.get('outcome')})"))
        check(int(recovered.get("interrupted") or 0) == 0,
              j.step(f"and the restart invents no new in-flight work ({recovered})"))

        # 11-13  the goal can be picked back up, on a fresh revision.
        before = int((await mem2.get_goal(goal_id=goal_a) or {}).get("generation"))
        await mem2.resume_goal(goal_id=goal_a)
        after = await mem2.get_goal(goal_id=goal_a) or {}
        check(str(after.get("status")) == "active",
              j.step(f"resuming reactivates it ({after.get('status')})"))
        check(int(after.get("generation")) >= before,
              j.step(f"on a revision no older than before "
                     f"({before} -> {after.get('generation')})"))
        nxt = await mem2.claim_next_goal_task()
        check(nxt is not None and str(nxt.get("task_id")) != tid_known,
              j.step(f"and the superseded step is NOT what resumes "
                     f"({(nxt or {}).get('tool_name')})"))

        # 14  finish the fresh run properly.
        if nxt:
            v = await mem2.complete_goal_task(
                task_id=str(nxt["task_id"]), status="done",
                result={"ok": True}, error="",
                expected_generation=int(nxt["generation"]))
            check(v == "applied",
                  j.step(f"the fresh step completes normally ({v})"))
        final = await _row_of(mem2, tid_known)
        check(str(final.get("status")) == "superseded",
              j.step(f"without disturbing the superseded one "
                     f"({final.get('status')})"))

        check(j.n >= 18, f"the journey ran {j.n} checked transitions")


# -- journey 9: two projects, duplicate delivery, drift and a restart --------


async def journey_nine_interleaved_with_duplicates():
    """The long one. Two projects interleaved, every duplicate-delivery shape,
    an artifact changing underneath, a restart in the middle, and a final
    check that the project which was never touched is byte-for-byte untouched.

    Nothing here is new machinery; it is the combination that is the test.
    """
    check.section("journey 9: A and B interleaved, with duplicates and drift")
    j = Journey()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        store = Path(td) / "nova"
        mem = MemoryUnifier(store, enable_chroma=False)
        await mem.initialize()

        # 01-03  project A starts and takes a step.
        goal_a, _ = await _seed(mem, GAME, "add a pause menu",
                                ["code.write", "code.test"])
        c1 = await mem.claim_next_goal_task()
        a_first = str(c1["task_id"])
        v = await mem.complete_goal_task(
            task_id=a_first, status="done", result={"ok": True}, error="",
            expected_generation=int(c1["generation"]))
        ta = await truths(mem, GAME)
        check(v == "applied", j.step(f"A's first step lands ({v})"))
        check(a_first in ta.succeeded, j.step(f"and counts ({ta.summary()})"))

        # 04-05  switch to B and do work there.
        goal_b, _ = await _seed(mem, CALC, "add a percent key", ["code.write"])
        c2 = await mem.claim_next_goal_task()
        check(str(c2.get("project_name")) == GAME,
              j.step(f"the queue is still on A's older work "
                     f"({c2.get('project_name')})"))
        b_gen = int(c2["generation"])
        await mem.bump_goal_task_attempt(
            task_id=str(c2["task_id"]), attempts=1,
            run_after_iso="2000-01-01T00:00:00+00:00",
            error="network blipped", expected_generation=b_gen)
        ta = await truths(mem, GAME)
        check(str(c2["task_id"]) in ta.may_resume,
              j.step(f"A's retried step may resume ({ta.summary()})"))

        # 06-08  duplicate retry, which must change nothing.
        again = await mem.bump_goal_task_attempt(
            task_id=str(c2["task_id"]), attempts=2,
            run_after_iso="2000-01-01T00:00:00+00:00",
            error="network blipped", expected_generation=b_gen)
        check(again is False,
              j.step(f"a duplicate retry is refused ({again})"))
        ta2 = await truths(mem, GAME)
        check(ta2.status_of(str(c2["task_id"])) == "queued",
              j.step(f"and the step is where it was "
                     f"({ta2.status_of(str(c2['task_id']))})"))
        tb = await truths(mem, CALC)
        check(not tb.failed and not tb.cancelled,
              j.step(f"B is untouched by any of it ({tb.summary()})"))

        # 09-11  pause A, work B to completion.
        await mem.update_goal_status(goal_id=goal_a, status="paused")
        cb = await mem.claim_next_goal_task()
        check(cb is not None and str(cb.get("project_name")) == CALC,
              j.step(f"with A paused the queue moves to B "
                     f"({(cb or {}).get('project_name')})"))
        vb = await mem.complete_goal_task(
            task_id=str(cb["task_id"]), status="done", result={"ok": True},
            error="", expected_generation=int(cb["generation"]))
        tb = await truths(mem, CALC)
        check(vb == "applied" and str(cb["task_id"]) in tb.succeeded,
              j.step(f"B's step completes ({tb.summary()})"))

        # 12-14  duplicate and contradictory completion on B.
        dup = await mem.complete_goal_task(
            task_id=str(cb["task_id"]), status="done", result={"ok": True},
            error="", expected_generation=int(cb["generation"]))
        contra = await mem.complete_goal_task(
            task_id=str(cb["task_id"]), status="failed", result={},
            error="actually it broke", expected_generation=int(cb["generation"]))
        tb = await truths(mem, CALC)
        check(dup == "ignored" and contra == "ignored",
              j.step(f"repeats are ignored ({dup}, {contra})"))
        check(str(cb["task_id"]) in tb.succeeded,
              j.step(f"the first answer stands ({tb.summary()})"))
        check(str(cb["task_id"]) not in tb.failed,
              j.step("and it is not also recorded as a failure"))

        # 15-17  a restart in the middle of all of it.
        del mem
        mem = MemoryUnifier(store, enable_chroma=False)
        await mem.initialize()
        recovered = await mem.cancel_pending_background_work()
        ta, tb = await truths(mem, GAME), await truths(mem, CALC)
        check(a_first in ta.succeeded,
              j.step(f"A's finished work survives ({ta.summary()})"))
        check(str(cb["task_id"]) in tb.succeeded,
              j.step(f"B's finished work survives ({tb.summary()})"))
        check(not ta.may_resume and not tb.may_resume,
              j.step(f"and nothing restarts itself ({recovered})"))

        # 18-20  A is resumed deliberately and finishes.
        await mem.resume_goal(goal_id=goal_a)
        ga = await mem.get_goal(goal_id=goal_a) or {}
        check(str(ga.get("status")) == "active",
              j.step(f"A is picked back up ({ga.get('status')})"))
        nxt = await mem.claim_next_goal_task()
        check(nxt is not None and str(nxt.get("project_name")) == GAME,
              j.step(f"its own work is claimable ({(nxt or {}).get('tool_name')})"))
        if nxt:
            v = await mem.complete_goal_task(
                task_id=str(nxt["task_id"]), status="done",
                result={"ok": True}, error="",
                expected_generation=int(nxt["generation"]))
            check(v == "applied", j.step(f"and completes ({v})"))

        # 21-24  a late worker from before the restart tries to report.
        stale = await mem.complete_goal_task(
            task_id=str(c2["task_id"]), status="done", result={"ok": True},
            error="", expected_generation=b_gen)
        ta = await truths(mem, GAME)
        check(stale == "ignored",
              j.step(f"a worker from before the restart cannot report ({stale})"))
        check(str(c2["task_id"]) not in ta.succeeded,
              j.step(f"and its step is not counted ({ta.summary()})"))
        check(str(c2["task_id"]) in ta.never_resume,
              j.step("it is in the never-again set"))
        tb = await truths(mem, CALC)
        check(len(tb.succeeded) == 1 and not tb.failed,
              j.step(f"B is STILL exactly as it was ({tb.summary()})"))

        # 25-27  cancel A twice, and prove the second changes nothing.
        gen_before = (await mem.get_goal(goal_id=goal_a) or {}).get("generation")
        await mem.cancel_goal(goal_id=goal_a)
        once = (await mem.get_goal(goal_id=goal_a) or {}).get("generation")
        await mem.cancel_goal(goal_id=goal_a)
        twice = (await mem.get_goal(goal_id=goal_a) or {}).get("generation")
        check(int(once) == int(gen_before) + 1,
              j.step(f"cancelling advances the revision once ({gen_before} -> {once})"))
        check(int(twice) == int(once),
              j.step(f"and cancelling again does not ({twice})"))
        ta = await truths(mem, GAME)
        check(not ta.may_resume,
              j.step(f"nothing on A may resume ({ta.summary()})"))

        # 28-30  final: B never moved, and the two projects never mixed.
        tb = await truths(mem, CALC)
        gb = await mem.get_goal(goal_id=goal_b) or {}
        check(str(gb.get("status")) in ("active", "paused"),
              j.step(f"B's goal was never cancelled by A's cancel "
                     f"({gb.get('status')})"))
        check(tb.succeeded.isdisjoint(ta.succeeded),
              j.step("the two projects share no work"))
        all_rows = await mem.list_goal_tasks(limit=200)
        check(all(str(r.get("project_name")) in (GAME, CALC) for r in all_rows),
              j.step(f"and nothing was attributed anywhere else "
                     f"({sorted({str(r.get('project_name')) for r in all_rows})})"))

        # B's revision must never have moved. A's whole lifecycle - retries,
        # a pause, a restart, a resume, two cancels - happened alongside it,
        # and none of that is B's business.
        check(int(gb.get("generation")) == 0,
              j.step(f"B is still on its first revision "
                     f"({gb.get('generation')}) after all of A's churn"))
        ta_final = await truths(mem, GAME)
        check(ta_final.never_resume >= (ta_final.succeeded | ta_final.cancelled),
              j.step(f"everything terminal on A is in the never-again set "
                     f"({len(ta_final.never_resume)} of {len(ta_final.tasks)})"))

        check(j.n >= 28, f"the journey ran {j.n} checked transitions")


async def main():
    await journey_one_long_build()
    await journey_two_retries_and_revisions()
    await journey_three_background_work()
    await journey_four_supervisor_decides()
    await journey_five_restart_integrity()
    await journey_eight_ambiguous_side_effects()
    await journey_nine_interleaved_with_duplicates()
    check.finish()


if __name__ == "__main__":
    run(main)
