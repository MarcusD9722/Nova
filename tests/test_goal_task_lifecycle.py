"""A cancelled run's work never becomes the resumed run's work (V3 P10 C7).

THE DEFECT on `31150da`. The decision path was fenced by generation, but the
TASK path was not:

  * `bump_task_attempt` requeued a running task unconditionally, so a tool that
    failed transiently AFTER the user cancelled was put back on the queue;
  * `claim_next_task` had no `t.generation = g.generation` predicate, so that
    stale row was runnable again once the goal was resumed;
  * and it returned `COALESCE(g.generation, 0)` — the goal's CURRENT generation
    — so the stale task was handed to the supervisor LABELLED as the new run.

Measured before the fix: tool invoked 3 times, the run-0 row sitting `queued`
while the goal was cancelled, a `retry` event announcing a retry that should
never have been scheduled.

THE INVARIANT

    A task created under run N may finish and report what already happened.
    It may NEVER create future runnable work in run N+1.

An in-flight side effect cannot be undone; nobody claims otherwise. The bug is
FUTURE EXECUTION being resurrected after cancellation.

Barriers and events only; no sleep establishes any race.

Run:  venv\\Scripts\\python.exe tests\\test_goal_task_lifecycle.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

from pathlib import Path
from uuid import UUID, uuid4

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

import aiosqlite  # noqa: E402

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


async def _rows(m, gid):
    """Task rows WITH their generation — list_goal_tasks does not carry it."""
    async with aiosqlite.connect(m._sqlite._db_path) as db:
        cur = await db.execute(
            "SELECT task_id, tool_name, status, attempts, generation, last_error "
            "FROM tasks WHERE goal_id=? ORDER BY created_at", (str(gid),))
        return [dict(task_id=r[0], tool_name=r[1], status=r[2], attempts=r[3],
                     generation=int(r[4] or 0), last_error=r[5] or "")
                for r in await cur.fetchall()]


class _EventLog:
    """Accumulating drain of progress events.

    `fetch_unacked_progress` ACKNOWLEDGES what it returns, so polling it inside
    a wait loop consumes the very event the test is waiting for. Everything
    drained is remembered here instead.
    """

    def __init__(self, m, project="alpha") -> None:
        self._m = m
        self._project = project
        self.kinds: list[str] = []

    async def drain(self) -> list[str]:
        evs = await self._m.fetch_unacked_progress(project_name=self._project,
                                                   limit=100)
        self.kinds.extend(e["kind"] for e in evs)
        return self.kinds


async def _kinds(m, project="alpha"):
    evs = await m.fetch_unacked_progress(project_name=project, limit=100)
    return [e["kind"] for e in evs]


def _claim_select_sql() -> str:
    """The candidate SELECT itself — the SQL, not the prose around it."""
    from memory.backends.sqlite_backend import SQLiteMemoryBackend
    return SQLiteMemoryBackend._CLAIM_SELECT


def _supervisor(m, *, router, decide=None, max_retries=3, max_steps=8):
    llm = ScriptedLLM()
    llm.default_reply = '{"type": "final", "message": "done"}'
    sup = AgentSupervisor(
        memory=m, llm=llm, router=router,
        tool_descriptions={n: n for n in router.list_tools()},
        cfg=SupervisorConfig(tick_seconds=0.05, max_retries=max_retries,
                             max_steps_per_goal=max_steps))
    if decide is not None:
        sup._decide_next = decide
    return sup


async def _inert(**kw):
    """A decision the supervisor cannot act on, so only the path under test runs."""
    return {"type": "__inert_for_test__"}


# ── BLOCKER 1: the stale retry race ─────────────────────────────────────────

async def test_a_transient_failure_after_cancel_is_not_requeued():
    check.section("C7: a cancelled run's tool is never rescheduled")

    with _tmp() as td:
        m = await _mem(td)
        entered, release = asyncio.Event(), asyncio.Event()
        calls: list[int] = []

        async def flaky(_args):
            calls.append(1)
            entered.set()
            await release.wait()
            raise RuntimeError("transient network blip")

        router = ToolRouter({"demo.flaky": flaky}, {"demo.flaky": "flaky"})
        router.set_retry_safe("demo.flaky", True)
        sup = _supervisor(m, router=router, decide=_inert)

        gid = await _goal(m)
        await m.enqueue_goal_task(goal_id=gid, project_name="alpha",
                                  tool_name="demo.flaky", args={})
        sup.start()
        try:
            await asyncio.wait_for(entered.wait(), timeout=20.0)
            await m.cancel_goal(goal_id=gid)
            g = await m.get_goal(goal_id=gid)
            check(g["status"] == "cancelled" and g["generation"] == 1,
                  f"cancel opened run 1 ({g['status']}/{g['generation']})")

            release.set()
            for _ in range(200):
                await asyncio.sleep(0.05)
                rows = await _rows(m, gid)
                tool = [t for t in rows if t["tool_name"] == "demo.flaky"]
                if tool and tool[0]["status"] != "running":
                    break

            rows = await _rows(m, gid)
            tool = [t for t in rows if t["tool_name"] == "demo.flaky"][0]
            kinds = await _kinds(m)

            # THE defect: this row was 'queued', ready to run again.
            check(tool["status"] != "queued",
                  f"the run-0 tool is NOT queued for retry ({tool['status']})")
            check("retry" not in kinds,
                  f"and no retry was announced ({kinds})")
            check("cancelled" in tool["last_error"] or "not retried" in tool["last_error"],
                  f"the reason is truthful ({tool['last_error'][:70]!r})")

            before_resume = len(calls)
            await m.resume_goal(goal_id=gid)
            g = await m.get_goal(goal_id=gid)
            check(g["status"] == "active" and g["generation"] == 1,
                  f"resume reactivates run 1 ({g['status']}/{g['generation']})")

            # Give the supervisor every chance to pick the stale row back up.
            for _ in range(60):
                await asyncio.sleep(0.05)
                if len(calls) > before_resume:
                    break
            check(len(calls) == before_resume,
                  f"the run-0 tool never executes again after resume "
                  f"({len(calls) - before_resume} extra invocations)")

            rows = await _rows(m, gid)
            tool = [t for t in rows if t["tool_name"] == "demo.flaky"][0]
            check(tool["generation"] == 0,
                  f"and the stale row never became a run-1 task ({tool['generation']})")
            fresh = [t for t in rows if t["generation"] == 1]
            check(all(t["tool_name"] == "__decide__" for t in fresh),
                  f"the resumed run has only its own continuation "
                  f"({sorted(t['tool_name'] for t in fresh)})")
        finally:
            release.set()
            await sup.stop()


async def test_a_transient_failure_on_a_live_goal_still_retries():
    check.section("C7: an ordinary transient failure still retries")

    with _tmp() as td:
        m = await _mem(td)
        calls: list[int] = []

        async def flaky(_args):
            calls.append(1)
            raise RuntimeError("transient network blip")

        router = ToolRouter({"demo.flaky": flaky}, {"demo.flaky": "flaky"})
        router.set_retry_safe("demo.flaky", True)
        sup = _supervisor(m, router=router, decide=_inert)

        gid = await _goal(m)
        await m.enqueue_goal_task(goal_id=gid, project_name="alpha",
                                  tool_name="demo.flaky", args={})
        log = _EventLog(m)
        sup.start()
        try:
            for _ in range(200):
                await asyncio.sleep(0.05)
                if "retry" in await log.drain():
                    break
        finally:
            await sup.stop()

        kinds = await log.drain()
        rows = await _rows(m, gid)
        tool = [t for t in rows if t["tool_name"] == "demo.flaky"][0]
        check("retry" in kinds, f"a live goal still announces a retry ({kinds})")
        check(tool["attempts"] >= 1, f"and the attempt was counted ({tool['attempts']})")


# ── The direct claim contract ───────────────────────────────────────────────

async def _seed_task(m, gid, *, tool, status, generation, created):
    tid = str(uuid4())
    async with aiosqlite.connect(m._sqlite._db_path) as db:
        await db.execute(
            "INSERT INTO tasks(task_id, goal_id, project_name, tool_name, args_json, "
            "status, attempts, run_after, last_error, result_json, created_at, "
            "updated_at, generation) VALUES(?, ?, 'alpha', ?, '{}', ?, 0, ?, '', "
            "'{}', ?, ?, ?)",
            (tid, str(gid), tool, status, "2000-01-01T00:00:00+00:00",
             created, created, int(generation)))
        await db.commit()
    return tid


async def test_only_a_task_of_the_current_run_is_claimable():
    check.section("C7: the claim compares the TASK's run to the goal's")

    with _tmp() as td:
        m = await _mem(td)
        gid = await _goal(m)
        # Drive the goal to generation 7, active.
        async with aiosqlite.connect(m._sqlite._db_path) as db:
            await db.execute(
                "UPDATE goals SET generation=7, status='active' WHERE goal_id=?",
                (str(gid),))
            await db.commit()

        # A is OLDER, so ORDER BY updated_at would pick it first.
        a = await _seed_task(m, gid, tool="stale.six", status="queued",
                             generation=6, created="2020-01-01T00:00:00+00:00")
        b = await _seed_task(m, gid, tool="fresh.seven", status="queued",
                             generation=7, created="2030-01-01T00:00:00+00:00")

        claimed = await m.claim_next_goal_task()
        check(claimed is not None and claimed["task_id"] == b,
              f"the run-7 task is claimed, not the older run-6 one "
              f"({(claimed or {}).get('tool_name')})")
        check(claimed is not None and int(claimed["generation"]) == 7,
              f"and the generation comes from the TASK row ({(claimed or {}).get('generation')})")

        await m.complete_goal_task(task_id=b, status="done", result={},
                                   expected_generation=7)
        again = await m.claim_next_goal_task()
        check(again is None,
              f"the stale run-6 task stays unclaimable ({(again or {}).get('tool_name')})")

        rows = await _rows(m, gid)
        stale = [t for t in rows if t["task_id"] == a][0]
        check(stale["status"] == "queued" and stale["generation"] == 6,
              f"and is left alone rather than relabelled "
              f"({stale['status']}/{stale['generation']})")


async def test_the_claimed_generation_is_not_the_goals_current_one():
    check.section("C7: a claim never launders a task into the current run")

    with _tmp() as td:
        m = await _mem(td)
        gid = await _goal(m)
        await m.enqueue_goal_task(goal_id=gid, project_name="alpha",
                                  tool_name="demo.spy", args={})
        # Move the GOAL on without touching the task row.
        async with aiosqlite.connect(m._sqlite._db_path) as db:
            await db.execute(
                "UPDATE goals SET generation=5, status='active' WHERE goal_id=?",
                (str(gid),))
            await db.commit()

        claimed = await m.claim_next_goal_task()
        check(claimed is None,
              f"a run-0 task under a run-5 goal is not runnable "
              f"({(claimed or {}).get('tool_name')})")

        # Put the task on the goal's run and it becomes claimable again.
        async with aiosqlite.connect(m._sqlite._db_path) as db:
            await db.execute("UPDATE tasks SET generation=5 WHERE goal_id=?",
                             (str(gid),))
            await db.commit()
        claimed = await m.claim_next_goal_task()
        check(claimed is not None and int(claimed["generation"]) == 5,
              f"matched runs claim normally ({(claimed or {}).get('generation')})")


async def test_a_goalless_task_never_runs():
    """No authoritative goal row, no execution (V3 P10 C8).

    This previously asserted the opposite — that an orphan stayed claimable —
    kept as compatibility. But this is the GOAL task queue, and the supervisor
    hands a claimed real tool straight to `ToolRouter.execute` without ever
    looking the parent goal up. So a row naming a nonexistent goal and a
    side-effecting tool executed on nothing's authority. Measured on e9183c2:
    an orphan `demo.spy` ran.
    """
    check.section("C8: a task whose goal does not exist is never runnable")

    with _tmp() as td:
        m = await _mem(td)
        orphan = uuid4()
        tid = await _seed_task(m, orphan, tool="orphan.tool", status="queued",
                               generation=0, created="2020-01-01T00:00:00+00:00")
        claimed = await m.claim_next_goal_task()
        check(claimed is None,
              f"an orphan row is not claimable ({(claimed or {}).get('tool_name')})")

        rows = await _rows(m, orphan)
        check(rows and rows[0]["status"] == "queued",
              f"and is left as evidence rather than deleted ({rows[0]['status']})")

        # BOTH halves of the claim refuse it independently. With both in place
        # either one alone keeps the row out, which is the point of having two
        # — and also means a defect in one is invisible through
        # `claim_next_goal_task`, so each is exercised on its own.
        now = m._sqlite._now_iso()
        async with aiosqlite.connect(m._sqlite._db_path) as db:
            cur = await db.execute(m._sqlite._CLAIM_SELECT, (now,))
            check(await cur.fetchone() is None,
                  "the candidate SELECT does not offer it")
            cur = await db.execute(m._sqlite._CLAIM_UPDATE, (now, tid))
            await db.commit()
            check(int(cur.rowcount or 0) == 0,
                  "and the claiming UPDATE refuses it by itself")


async def test_an_orphan_real_tool_never_reaches_the_router():
    check.section("C8: an orphan side-effecting tool never executes")

    with _tmp() as td:
        m = await _mem(td)
        spy: list[int] = []

        async def demo_spy(_args):
            spy.append(1)
            return {"ok": True}

        router = ToolRouter({"demo.spy": demo_spy}, {"demo.spy": "spy"})
        sup = _supervisor(m, router=router, decide=_inert)

        orphan = uuid4()
        await _seed_task(m, orphan, tool="demo.spy", status="queued",
                         generation=0, created="2020-01-01T00:00:00+00:00")
        sup.start()
        try:
            for _ in range(60):
                await asyncio.sleep(0.05)
                if spy:
                    break
        finally:
            await sup.stop()

        check(not spy, f"demo.spy was never invoked ({len(spy)})")
        kinds = await _kinds(m)
        check("tool" not in kinds and "plan" not in kinds,
              f"and no success or plan event was fabricated ({kinds})")


async def test_enqueueing_against_a_missing_goal_is_refused():
    check.section("C8: storage refuses work for a goal that does not exist")

    with _tmp() as td:
        m = await _mem(td)
        made = await m.enqueue_goal_task(goal_id=uuid4(), project_name="alpha",
                                         tool_name="demo.spy", args={})
        check(made is None, f"enqueue on a nonexistent goal is refused ({made})")

        # A real goal still works, and a paused one still accepts work: pause
        # is a hold, not an ending, and resume continues the plan already laid.
        gid = await _goal(m)
        check(await m.enqueue_goal_task(goal_id=gid, project_name="alpha",
                                        tool_name="demo.spy", args={}) is not None,
              "an active goal still accepts work")
        await m.update_goal_status(goal_id=gid, status="paused")
        check(await m.enqueue_goal_task(goal_id=gid, project_name="alpha",
                                        tool_name="demo.spy", args={}) is not None,
              "and a paused goal does too")


async def test_the_claim_projects_the_tasks_own_generation():
    """Guards against future drift rather than a live difference.

    Once the runnable predicate requires `t.generation = g.generation`, any
    claimable task satisfies both, so projecting either column yields the same
    number — the two are now provably equal and no behavioural test can
    separate them. It mattered when orphans were claimable, and it would matter
    again the moment the predicate were loosened, so the projection is pinned
    here explicitly.
    """
    check.section("C8: the claim still reads generation from the task row")

    sql = _claim_select_sql()
    check("t.generation" in sql,
          "the claim SELECT projects t.generation")
    check("COALESCE(g.generation" not in sql,
          "and not the goal's current generation")


async def test_the_claim_update_re_checks_the_condition_itself():
    """The SELECT only chooses a candidate; the UPDATE must decide.

    Driven directly against the statement, because the window between the two
    is internal to `claim_next_task`. A row is left `queued` and its goal is
    then moved on — exactly the state a cancel arriving mid-claim produces.
    """
    check.section("C7: the claiming UPDATE carries its own guard")

    with _tmp() as td:
        m = await _mem(td)
        gid = await _goal(m)
        tid = await _seed_task(m, gid, tool="demo.spy", status="queued",
                               generation=0, created="2020-01-01T00:00:00+00:00")
        sql = m._sqlite._CLAIM_UPDATE
        now = m._sqlite._now_iso()

        async def try_claim() -> int:
            async with aiosqlite.connect(m._sqlite._db_path) as db:
                cur = await db.execute(sql, (now, tid))
                await db.commit()
                return int(cur.rowcount or 0)

        async def set_goal(status: str, generation: int) -> None:
            async with aiosqlite.connect(m._sqlite._db_path) as db:
                await db.execute(
                    "UPDATE goals SET status=?, generation=? WHERE goal_id=?",
                    (status, generation, str(gid)))
                await db.commit()

        await set_goal("cancelled", 1)
        check(await try_claim() == 0,
              "the UPDATE refuses a row whose goal was cancelled mid-claim")

        await set_goal("active", 1)          # resumed, but the row is run 0
        check(await try_claim() == 0,
              "and one whose goal moved to a new run mid-claim")

        await set_goal("active", 0)
        check(await try_claim() == 1,
              "while a genuinely runnable row is claimed")


async def test_each_retry_requeue_condition_refuses_on_its_own():
    """Three separate reasons a retry may not be scheduled.

    The end-to-end race only ever produces ONE of them (cancelled goal, new
    generation), so it cannot tell whether all three predicates are carrying
    their weight — measured: removing any single one individually left the race
    test green. Each gets the state that isolates it.
    """
    check.section("C7: every retry-requeue condition is load-bearing")

    later = "2099-01-01T00:00:00+00:00"

    async def _case(label, *, goal_status, goal_gen, task_status, task_gen,
                    expected_generation, should_requeue):
        with _tmp() as td:
            m = await _mem(td)
            gid = await _goal(m)
            tid = await _seed_task(m, gid, tool="demo.spy", status=task_status,
                                   generation=task_gen,
                                   created="2020-01-01T00:00:00+00:00")
            async with aiosqlite.connect(m._sqlite._db_path) as db:
                await db.execute(
                    "UPDATE goals SET status=?, generation=? WHERE goal_id=?",
                    (goal_status, goal_gen, str(gid)))
                await db.commit()

            ok = await m.bump_goal_task_attempt(
                task_id=tid, attempts=1, run_after_iso=later, error="blip",
                expected_generation=expected_generation)
            rows = await _rows(m, gid)
            row = [t for t in rows if t["task_id"] == tid][0]
            check(ok is should_requeue,
                  f"{label}: requeue -> {ok} (want {should_requeue})")
            # `attempts`, not status: one case starts out already `queued`, so
            # status cannot tell a refusal from a success there. Every seeded
            # row starts at 0 and only a successful requeue writes 1.
            check((row["attempts"] == 1) is should_requeue,
                  f"{label}: attempts={row['attempts']} status={row['status']!r}")

    # The generation predicate, ISOLATED: the goal is ACTIVE, so an
    # active-status check alone would let this through.
    await _case("goal active on a LATER run",
                goal_status="active", goal_gen=1, task_status="running",
                task_gen=0, expected_generation=0, should_requeue=False)

    # The active-status predicate, ISOLATED: generations agree, so a
    # generation check alone would let this through.
    await _case("goal PAUSED on the same run",
                goal_status="paused", goal_gen=0, task_status="running",
                task_gen=0, expected_generation=0, should_requeue=False)
    await _case("goal CANCELLED on the same run",
                goal_status="cancelled", goal_gen=0, task_status="running",
                task_gen=0, expected_generation=0, should_requeue=False)

    # The running-ownership predicate, ISOLATED: goal and generations all
    # agree; only the task is no longer the one the caller was running.
    await _case("task already finished",
                goal_status="active", goal_gen=0, task_status="done",
                task_gen=0, expected_generation=0, should_requeue=False)
    await _case("task already requeued by someone else",
                goal_status="active", goal_gen=0, task_status="queued",
                task_gen=0, expected_generation=0, should_requeue=False)

    # And the one state where a retry IS legitimate.
    await _case("everything agrees",
                goal_status="active", goal_gen=0, task_status="running",
                task_gen=0, expected_generation=0, should_requeue=True)


# ── BLOCKER 2: the step-budget pause ────────────────────────────────────────

async def _budget_race(cancel_first: bool):
    """Claim a __decide__ with the budget already spent, then race a cancel."""
    with _tmp() as td:
        m = await _mem(td)
        entered, release = asyncio.Event(), asyncio.Event()

        router = ToolRouter({}, {})
        sup = _supervisor(m, router=router, decide=_inert, max_steps=2)

        gid = await _goal(m)
        # Spend the budget: two finished tasks.
        for i in range(2):
            t = await _seed_task(m, gid, tool="spent", status="done",
                                 generation=0, created=f"2020-01-0{i+1}T00:00:00+00:00")
        await m.enqueue_goal_task(goal_id=gid, project_name="alpha",
                                  tool_name="__decide__", args={})

        # Stop the supervisor between the claim and the budget transition.
        original = m.apply_step_budget_pause

        async def gated(**kw):
            entered.set()
            await release.wait()
            return await original(**kw)

        m.apply_step_budget_pause = gated
        sup.start()
        try:
            await asyncio.wait_for(entered.wait(), timeout=20.0)
            if cancel_first:
                await m.cancel_goal(goal_id=gid)
                release.set()
            else:
                release.set()
                # Let the transaction commit, then cancel.
                for _ in range(200):
                    await asyncio.sleep(0.05)
                    g = await m.get_goal(goal_id=gid)
                    if g["status"] == "paused":
                        break
                await m.cancel_goal(goal_id=gid)
            for _ in range(200):
                await asyncio.sleep(0.05)
                rows = await _rows(m, gid)
                decide = [t for t in rows if t["tool_name"] == "__decide__"]
                if decide and decide[0]["status"] != "running":
                    break
        finally:
            release.set()
            await sup.stop()
            m.apply_step_budget_pause = original

        return (await m.get_goal(goal_id=gid), await _rows(m, gid),
                await _kinds(m))


async def test_a_cancel_beats_a_step_budget_pause():
    check.section("C7: cancel first — the budget pause applies NOTHING")

    goal, rows, kinds = await _budget_race(cancel_first=True)

    check(goal["status"] == "cancelled",
          f"the goal is still CANCELLED, not paused ({goal['status']})")
    check(goal["generation"] == 1,
          f"on the run cancel opened ({goal['generation']})")
    check("paused" not in kinds,
          f"and no 'paused after N steps' event became authoritative ({kinds})")
    decide = [t for t in rows if t["tool_name"] == "__decide__"][0]
    check(decide["status"] == "failed" and "discarded" in decide["last_error"],
          f"the claimed task is finalised as stale ({decide['status']})")


async def test_nothing_of_the_step_budget_pause_is_visible_mid_apply():
    """It is one act: pause + task completion + event, or none of them.

    Blocking immediately before the progress event: the gate and the task
    completion have both executed by then. If either had its own commit, an
    observer on another connection would see it here — which is exactly the
    split the unfenced three-write version had.
    """
    check.section("C7: the step-budget pause is one indivisible act")

    with _tmp() as td:
        m = await _mem(td)
        gid = await _goal(m)
        tid = await _seed_task(m, gid, tool="__decide__", status="running",
                               generation=0, created="2020-01-01T00:00:00+00:00")

        inside, release = asyncio.Event(), asyncio.Event()
        backend = m._sqlite
        original = backend._add_event

        async def gated(db, **kw):
            inside.set()
            await release.wait()
            return await original(db, **kw)

        backend._add_event = gated
        try:
            apply = asyncio.create_task(m.apply_step_budget_pause(
                goal_id=gid, project_name="alpha", expected_generation=0,
                task_id=tid, message="Goal paused after 2 steps."))
            await asyncio.wait_for(inside.wait(), timeout=15.0)
            for _ in range(50):
                await asyncio.sleep(0)

            goal = await m.get_goal(goal_id=gid)
            rows = await _rows(m, gid)
            kinds = await _kinds(m)
            check(goal["status"] == "active",
                  f"mid-transaction the goal is NOT yet paused ({goal['status']})")
            check(rows[0]["status"] == "running",
                  f"and the decision task is not yet done ({rows[0]['status']})")
            check(not kinds, f"and no event is visible ({kinds})")

            release.set()
            check(await asyncio.wait_for(apply, timeout=15.0) is True,
                  "then the whole transition applies")
            goal = await m.get_goal(goal_id=gid)
            rows = await _rows(m, gid)
            check(goal["status"] == "paused", f"goal paused ({goal['status']})")
            check(rows[0]["status"] == "done", f"task done ({rows[0]['status']})")
        finally:
            release.set()
            backend._add_event = original


async def test_the_step_budget_gate_needs_active_as_well_as_generation():
    """The status half of the gate, isolated.

    Cancel bumps the generation, so the end-to-end race always fails BOTH
    halves at once and cannot tell which one did the work — measured: dropping
    the status check left that race green. A goal that is non-active on the
    SAME generation separates them.
    """
    check.section("C7: generation equality alone cannot pause a goal")

    for status in ("paused", "cancelled", "completed"):
        with _tmp() as td:
            m = await _mem(td)
            gid = await _goal(m)
            tid = await _seed_task(m, gid, tool="__decide__", status="running",
                                   generation=0,
                                   created="2020-01-01T00:00:00+00:00")
            # Same generation, not active.
            async with aiosqlite.connect(m._sqlite._db_path) as db:
                await db.execute(
                    "UPDATE goals SET status=?, generation=0 WHERE goal_id=?",
                    (status, str(gid)))
                await db.commit()

            ok = await m.apply_step_budget_pause(
                goal_id=gid, project_name="alpha", expected_generation=0,
                task_id=tid, message="Goal paused after 2 steps.")
            goal = await m.get_goal(goal_id=gid)
            rows = await _rows(m, gid)
            kinds = await _kinds(m)

            check(ok is False,
                  f"a {status} goal on run 0 refuses a run-0 budget pause ({ok})")
            check(goal["status"] == status,
                  f"and is left {status} ({goal['status']})")
            check(rows[0]["status"] == "running",
                  f"the claimed task is untouched ({rows[0]['status']})")
            check("paused" not in kinds, f"and no pause event landed ({kinds})")


async def test_a_step_budget_pause_that_wins_applies_whole():
    check.section("C7: budget first — it applies atomically, cancel lands after")

    goal, rows, kinds = await _budget_race(cancel_first=False)

    check("paused" in kinds, f"the pause event exists ({kinds})")
    decide = [t for t in rows if t["tool_name"] == "__decide__"][0]
    check(decide["status"] == "done",
          f"and its task completed with it ({decide['status']})")
    check(goal["status"] == "cancelled",
          f"the user's later cancel then landed ({goal['status']})")


# ── CORRECTION 3: a stale decision error ────────────────────────────────────

async def _decide_failure_race(*, stale: bool):
    with _tmp() as td:
        m = await _mem(td)
        entered, release = asyncio.Event(), asyncio.Event()
        router = ToolRouter({}, {})

        calls = {"n": 0}

        async def failing(**kw):
            calls["n"] += 1
            if calls["n"] == 1:
                entered.set()
                await release.wait()
                raise RuntimeError("old planner failed")
            return {"type": "__inert_for_test__"}

        sup = _supervisor(m, router=router, decide=failing)
        gid = await _goal(m)
        await m.enqueue_goal_task(goal_id=gid, project_name="alpha",
                                  tool_name="__decide__", args={})
        sup.start()
        try:
            await asyncio.wait_for(entered.wait(), timeout=20.0)
            if stale:
                await m.cancel_goal(goal_id=gid)
                await m.resume_goal(goal_id=gid)
            release.set()
            for _ in range(200):
                await asyncio.sleep(0.05)
                rows = await _rows(m, gid)
                if any(t["status"] == "failed" for t in rows):
                    break
        finally:
            release.set()
            await sup.stop()
        return (await m.get_goal(goal_id=gid), await _rows(m, gid),
                await _kinds(m))


async def test_a_stale_planner_error_does_not_reach_the_resumed_run():
    check.section("C7: a superseded planner failure is not current-run output")

    goal, rows, kinds = await _decide_failure_race(stale=True)

    check(goal["status"] == "active",
          f"the resumed goal is still active ({goal['status']})")
    check("error" not in kinds,
          f"no ordinary 'Could not decide next step' error is published ({kinds})")
    failed = [t for t in rows if t["status"] == "failed"]
    check(failed and "discarded" in failed[0]["last_error"],
          f"the old task is terminal AND marked stale "
          f"({(failed[0]['last_error'][:60] if failed else None)!r})")


async def test_a_planner_failure_cannot_be_overtaken_between_check_and_write():
    """The window the pre-check cannot close (V3 P10 C8).

    The previous round cancelled BEFORE releasing the exception, which proves
    the pre-check works. It does not prove the check->write race is closed:
    `_generation_is_current` is a SELECT, and it documents itself that the goal
    can be cancelled before the write lands. Measured on e9183c2 by blocking
    in exactly that window: a run-0 planner failure published "Could not decide
    next step" into the run-1 lifecycle the user had just resumed.

    The block now sits immediately before the atomic apply, which IS that
    window.
    """
    check.section("C8: a superseded planner failure loses the race at the write")

    with _tmp() as td:
        m = await _mem(td)
        router = ToolRouter({}, {})
        entered, release = asyncio.Event(), asyncio.Event()
        calls = {"n": 0}

        async def planner(**kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("old planner failed")
            # Later runs must NOT raise, or their own legitimate failure would
            # be mistaken for the stale one leaking through.
            return {"type": "question", "message": "run 1 is fine"}

        sup = _supervisor(m, router=router, decide=planner)
        original = m.apply_decision_error

        async def gated(**kw):
            entered.set()
            await release.wait()
            return await original(**kw)

        m.apply_decision_error = gated

        gid = await _goal(m)
        await m.enqueue_goal_task(goal_id=gid, project_name="alpha",
                                  tool_name="__decide__", args={})
        log = _EventLog(m)
        sup.start()
        try:
            await asyncio.wait_for(entered.wait(), timeout=20.0)
            await m.cancel_goal(goal_id=gid)
            await m.resume_goal(goal_id=gid)
            release.set()
            for _ in range(200):
                await asyncio.sleep(0.05)
                await log.drain()
                if any(t["status"] == "failed" for t in await _rows(m, gid)):
                    break
            for _ in range(20):
                await asyncio.sleep(0.05)
                await log.drain()
        finally:
            release.set()
            await sup.stop()
            m.apply_decision_error = original

        goal = await m.get_goal(goal_id=gid)
        rows = await _rows(m, gid)
        kinds = log.kinds

        check("error" not in kinds,
              f"no ordinary planner-error event from the superseded run ({kinds})")
        stale = [t for t in rows if t["generation"] == 0]
        check(stale and stale[0]["status"] == "failed"
              and "discarded" in stale[0]["last_error"],
              f"the run-0 task is terminal and marked stale "
              f"({(stale[0]['last_error'][:50] if stale else None)!r})")
        check(goal["status"] in ("active", "paused"),
              f"the resumed run is untouched by it ({goal['status']})")
        check(goal["generation"] == 1, f"and is run 1 ({goal['generation']})")


async def test_a_planner_error_that_wins_applies_whole_then_cancel_lands():
    check.section("C8: error first — it applies atomically, cancel follows")

    with _tmp() as td:
        m = await _mem(td)
        router = ToolRouter({}, {})
        inside, release = asyncio.Event(), asyncio.Event()
        backend = m._sqlite
        original = backend._add_event

        async def gated(db, **kw):
            inside.set()
            await release.wait()
            return await original(db, **kw)

        backend._add_event = gated
        gid = await _goal(m)
        tid = await _seed_task(m, gid, tool="__decide__", status="running",
                               generation=0, created="2020-01-01T00:00:00+00:00")
        try:
            apply = asyncio.create_task(m.apply_decision_error(
                goal_id=gid, project_name="alpha", expected_generation=0,
                task_id=tid, error="boom", message="Could not decide next step: boom"))
            await asyncio.wait_for(inside.wait(), timeout=15.0)
            for _ in range(50):
                await asyncio.sleep(0)

            # Mid-transaction: none of it is visible.
            mid_rows = await _rows(m, gid)
            mid_kinds = await _kinds(m)
            check(mid_rows[0]["status"] == "running",
                  f"mid-transaction the task is not yet failed ({mid_rows[0]['status']})")
            check(not mid_kinds, f"and no error event is visible ({mid_kinds})")

            cancel = asyncio.create_task(m.cancel_goal(goal_id=gid))
            for _ in range(50):
                await asyncio.sleep(0)
            check(not cancel.done(),
                  "a concurrent cancel is ordered after the error, not through it")

            release.set()
            check(await asyncio.wait_for(apply, timeout=15.0) is True,
                  "the error then applies whole")
            await asyncio.wait_for(cancel, timeout=15.0)
        finally:
            release.set()
            backend._add_event = original

        goal = await m.get_goal(goal_id=gid)
        rows = await _rows(m, gid)
        kinds = await _kinds(m)
        check(rows[0]["status"] == "failed", f"task failed ({rows[0]['status']})")
        check("error" in kinds, f"with its event ({kinds})")
        check(goal["status"] == "cancelled",
              f"and the cancel landed afterwards ({goal['status']})")


async def test_the_planner_error_gate_needs_generation_and_active():
    check.section("C8: the planner-error gate is fenced like a decision")

    with _tmp() as td:
        m = await _mem(td)
        gid = await _goal(m)
        tid = await _seed_task(m, gid, tool="__decide__", status="running",
                               generation=0, created="2020-01-01T00:00:00+00:00")

        async def _set(status, generation):
            async with aiosqlite.connect(m._sqlite._db_path) as db:
                await db.execute(
                    "UPDATE goals SET status=?, generation=? WHERE goal_id=?",
                    (status, generation, str(gid)))
                await db.commit()

        async def _try(expected):
            return await m.apply_decision_error(
                goal_id=gid, project_name="alpha", expected_generation=expected,
                task_id=tid, error="boom", message="Could not decide next step: boom")

        await _set("active", 1)                 # right status, wrong run
        check(await _try(0) is False, "a run-0 error is refused on run 1")

        await _set("paused", 0)                 # right run, not active
        check(await _try(0) is False, "and refused while the goal is paused")

        await _set("cancelled", 0)
        check(await _try(0) is False, "and while it is cancelled")

        rows = await _rows(m, gid)
        check(rows[0]["status"] == "running", "nothing was written by any refusal")
        check(not await _kinds(m), "and no event escaped")

        await _set("active", 0)
        check(await _try(0) is True, "while the matching live run records it")


async def test_a_current_planner_error_is_still_recorded():
    check.section("C7: an ordinary planner failure still reports normally")

    goal, rows, kinds = await _decide_failure_race(stale=False)

    check("error" in kinds, f"the error event is published ({kinds})")
    failed = [t for t in rows if t["status"] == "failed"]
    check(failed and "old planner failed" in failed[0]["last_error"],
          f"with the real reason "
          f"({(failed[0]['last_error'][:50] if failed else None)!r})")


# ── The step budget must actually be resumable ──────────────────────────────

async def test_a_budget_pause_can_really_be_resumed():
    """Nova offers "resume or cancel it", so resume has to mean something.

    Measured on e9183c2: budget reached -> paused -> resume -> the supervisor
    counted the SAME finished steps (the budget spanned all runs, and resume
    did not open one) -> paused again, zero planner calls. Resume could only
    ever mean cancel.
    """
    check.section("C8: resuming after the step budget gets a fresh window")

    with _tmp() as td:
        m = await _mem(td)
        router = ToolRouter({}, {})
        calls = {"n": 0}

        async def planner(**kw):
            calls["n"] += 1
            return {"type": "final", "message": "continued successfully"}

        sup = _supervisor(m, router=router, decide=planner, max_steps=2)
        gid = await _goal(m)
        for i in range(2):                       # spend the budget of run 0
            await _seed_task(m, gid, tool=f"spent{i}", status="done",
                             generation=0, created=f"2020-01-0{i+1}T00:00:00+00:00")
        await m.enqueue_goal_task(goal_id=gid, project_name="alpha",
                                  tool_name="__decide__", args={})
        log = _EventLog(m)
        sup.start()
        try:
            for _ in range(200):
                await asyncio.sleep(0.05)
                await log.drain()
                if (await m.get_goal(goal_id=gid))["status"] == "paused":
                    break
            # The pause and its event commit together, so the drain that ran
            # BEFORE the status check on the breaking iteration missed it.
            await log.drain()
            goal = await m.get_goal(goal_id=gid)
            check(goal["status"] == "paused", f"the budget pauses it ({goal['status']})")
            check(log.kinds.count("paused") == 1,
                  f"with one paused event ({log.kinds})")
            check(calls["n"] == 0,
                  f"and without spending a planner call ({calls['n']})")

            resumed = await m.resume_goal(goal_id=gid)
            check(resumed is not None, "resume succeeds")
            goal = await m.get_goal(goal_id=gid)
            check(goal["generation"] == 1,
                  f"and opens exactly one new run ({goal['generation']})")

            for _ in range(200):
                await asyncio.sleep(0.05)
                await log.drain()
                if (await m.get_goal(goal_id=gid))["status"] == "completed":
                    break
            await log.drain()
        finally:
            await sup.stop()

        await log.drain()
        goal = await m.get_goal(goal_id=gid)
        rows = await _rows(m, gid)
        check(calls["n"] >= 1,
              f"the planner is ACTUALLY consulted after resume ({calls['n']})")
        check(log.kinds.count("paused") == 1,
              f"the goal does not immediately re-pause ({log.kinds})")
        check(goal["status"] == "completed",
              f"and the goal can reach completion ({goal['status']})")
        fresh = [t for t in rows if t["generation"] == 1]
        check(any(t["tool_name"] == "__decide__" for t in fresh),
              f"the continuation belongs to the fresh run ({len(fresh)})")


async def test_the_budget_still_bounds_each_run():
    check.section("C8: a resumed run is bounded too, not unlimited")

    with _tmp() as td:
        m = await _mem(td)
        gid = await _goal(m)
        for i in range(2):
            await _seed_task(m, gid, tool=f"old{i}", status="done", generation=0,
                             created=f"2020-01-0{i+1}T00:00:00+00:00")
        await m.resume_goal(goal_id=gid)          # goal was active; no bump
        await m.update_goal_status(goal_id=gid, status="paused")
        await m.resume_goal(goal_id=gid)          # paused -> run 1
        goal = await m.get_goal(goal_id=gid)
        check(goal["generation"] == 1, f"on run 1 ({goal['generation']})")

        check(await m.count_finished_goal_steps(goal_id=gid, generation=1) == 0,
              "run 1 starts with a clean budget")
        check(await m.count_finished_goal_steps(goal_id=gid, generation=0) == 2,
              "and run 0's history is still counted against run 0")

        for i in range(2):                        # spend run 1 as well
            await _seed_task(m, gid, tool=f"new{i}", status="failed", generation=1,
                             created=f"2021-01-0{i+1}T00:00:00+00:00")
        check(await m.count_finished_goal_steps(goal_id=gid, generation=1) == 2,
              "a resumed run spends its own budget and can pause again")


async def test_concurrent_resumes_are_idempotent_per_state():
    check.section("C8: eight concurrent resumes, three starting states")

    async def _matrix(start: str):
        with _tmp() as td:
            m = await _mem(td)
            gid = await _goal(m)
            if start == "paused":
                await m.update_goal_status(goal_id=gid, status="paused")
            elif start == "cancelled":
                await m.cancel_goal(goal_id=gid)
            before = (await m.get_goal(goal_id=gid))["generation"]

            await asyncio.gather(*[m.resume_goal(goal_id=gid) for _ in range(8)])

            goal = await m.get_goal(goal_id=gid)
            rows = await _rows(m, gid)
            runnable = [t for t in rows
                        if t["tool_name"] == "__decide__"
                        and t["status"] in ("queued", "running")]
            return before, goal, runnable

    before, goal, runnable = await _matrix("paused")
    check(goal["generation"] == before + 1,
          f"PAUSED: the generation moves exactly once ({before} -> {goal['generation']})")
    check(len(runnable) == 1,
          f"PAUSED: exactly one continuation ({len(runnable)})")
    check(runnable and runnable[0]["generation"] == goal["generation"],
          "PAUSED: and it belongs to the new run")
    check(goal["status"] == "active", f"PAUSED: goal is active ({goal['status']})")

    before, goal, runnable = await _matrix("cancelled")
    check(goal["generation"] == before,
          f"CANCELLED: cancel's run is retained, not bumped again "
          f"({before} -> {goal['generation']})")
    check(len(runnable) == 1,
          f"CANCELLED: exactly one continuation ({len(runnable)})")

    before, goal, runnable = await _matrix("active")
    check(goal["generation"] == before,
          f"ACTIVE: no generation churn ({before} -> {goal['generation']})")
    check(len(runnable) == 1,
          f"ACTIVE: no duplicate continuation ({len(runnable)})")


async def main():
    await test_a_transient_failure_after_cancel_is_not_requeued()
    await test_a_transient_failure_on_a_live_goal_still_retries()
    await test_only_a_task_of_the_current_run_is_claimable()
    await test_the_claimed_generation_is_not_the_goals_current_one()
    await test_a_goalless_task_never_runs()
    await test_an_orphan_real_tool_never_reaches_the_router()
    await test_enqueueing_against_a_missing_goal_is_refused()
    await test_the_claim_projects_the_tasks_own_generation()
    await test_the_claim_update_re_checks_the_condition_itself()
    await test_each_retry_requeue_condition_refuses_on_its_own()
    await test_a_cancel_beats_a_step_budget_pause()
    await test_nothing_of_the_step_budget_pause_is_visible_mid_apply()
    await test_the_step_budget_gate_needs_active_as_well_as_generation()
    await test_a_step_budget_pause_that_wins_applies_whole()
    await test_a_stale_planner_error_does_not_reach_the_resumed_run()
    await test_a_planner_failure_cannot_be_overtaken_between_check_and_write()
    await test_a_planner_error_that_wins_applies_whole_then_cancel_lands()
    await test_the_planner_error_gate_needs_generation_and_active()
    await test_a_current_planner_error_is_still_recorded()
    await test_a_budget_pause_can_really_be_resumed()
    await test_the_budget_still_bounds_each_run()
    await test_concurrent_resumes_are_idempotent_per_state()
    check.finish()


if __name__ == "__main__":
    run(main)
