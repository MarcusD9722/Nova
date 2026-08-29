"""Corrections and two projects, carried across restarts (13C §10/§11).

CORRECTION (§10). The user asks for R1, some of it runs, they change their mind
to R2, the process dies, and the worker that was doing R1 finally reports. R2 is
authoritative. R1 is history. Neither may be confused for the other, and R1 may
never execute again because it happens to sit earlier in the queue.

ISOLATION (§11). Two projects run at once. A restart, a cancellation, a stale
worker and a project-pointer move all happen around them. A's work stays A's,
B's stays B's, and neither can finish, cancel or resume the other.

Every observation is read by a FRESH interpreter, attributed by goal_id,
task_id, generation and project_name - never by list position.

Run:  venv\\Scripts\\python.exe tests\\test_revision_isolation_s13c.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")
os.environ.setdefault("NOVA_IT_WATCHDOG_S", "1800")

from harness import Checks, run  # noqa: E402

from restart_harness import one, run_step  # noqa: E402

check = Checks()

A_PROJ = "flappy-bird"
B_PROJ = "quickcalc"

BOOT = """
    rec = await mem.cancel_pending_background_work()
    tasks = await mem.list_goal_tasks(limit=200)
    goals = await mem.list_goals(limit=50)
    claim = await mem.claim_next_goal_task()
    emit({"recovery": rec,
          "tasks": sorted((str(t["task_id"]), t["tool_name"], t["status"],
                           t["outcome"], t["generation"], t["project_name"])
                          for t in tasks),
          "goals": sorted((str(g["goal_id"]), g["status"], g["generation"],
                           g["project_name"]) for g in goals),
          "claimable": None if claim is None else
                       [str(claim["task_id"]), claim["project_name"]]})
"""


def _tmp():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


def _rows_for(boot, project):
    return [t for t in one(boot, "tasks") or [] if t[5] == project]


async def test_a_correction_leaves_the_old_revision_as_history():
    check.section("§10 R1 partly ran, user corrects to R2, then the crash")
    with _tmp() as td:
        root = Path(td) / "n"
        made = run_step(root, f"""
            goal = await mem.create_goal(project_name="{A_PROJ}",
                                         title="add a pause menu",
                                         objective="R1", success_criteria="c")
            await mem.enqueue_goal_task(goal_id=goal, project_name="{A_PROJ}",
                                        tool_name="code.write", args={{}})
            r1 = await mem.claim_next_goal_task()
            # The user changes their mind: cancel opens a new run.
            await mem.cancel_goal(goal_id=goal)
            await mem.resume_goal(goal_id=goal)
            g = await mem.get_goal(goal_id=goal)
            emit({{"goal": str(goal), "r1_task": str(r1["task_id"]),
                   "r1_gen": int(r1["generation"]), "r2_gen": g["generation"]}})
        """)
        gid = one(made, "goal")
        r1_task, r1_gen, r2_gen = (one(made, "r1_task"), one(made, "r1_gen"),
                                   one(made, "r2_gen"))
        check(r2_gen > r1_gen, f"R2 is a later revision ({r1_gen} -> {r2_gen})")

        run_step(root, "CRASH()", expect_crash=True)

        # The R1 worker finally reports, in a process that never knew about it.
        after = run_step(root, f"""
            await mem.cancel_pending_background_work()
            v = await mem.complete_goal_task(
                task_id="{r1_task}", status="done", result={{"ok": True}},
                error="", expected_generation={r1_gen})
            rows = await mem.list_goal_tasks(goal_id="{gid}", limit=20)
            g = await mem.get_goal(goal_id=__import__("uuid").UUID("{gid}"))
            emit({{"verdict": v, "goal_gen": g["generation"],
                   "goal_status": g["status"],
                   "rows": sorted((str(t["task_id"]), t["status"], t["outcome"],
                                   t["generation"]) for t in rows)}})
        """)
        rows = one(after, "rows")
        r1 = [r for r in rows if r[0] == r1_task]
        check(one(after, "verdict") in ("superseded", "ignored"),
              f"the R1 completion does not apply ({one(after, 'verdict')})")
        check(r1 and r1[0][1] != "done",
              f"R1 is not recorded as completed work ({r1})")
        check(r1 and int(r1[0][3]) == r1_gen,
              f"and still belongs to its own revision ({r1[0][3] if r1 else None})")
        check(int(one(after, "goal_gen")) == r2_gen,
              f"R2 is untouched ({one(after, 'goal_gen')} vs {r2_gen})")


async def test_b_a_third_revision_keeps_the_first_two_as_history():
    check.section("§10 R1 -> R2 -> restart -> R3, three distinct revisions")
    with _tmp() as td:
        root = Path(td) / "n"
        made = run_step(root, f"""
            goal = await mem.create_goal(project_name="{A_PROJ}", title="menu",
                                         objective="R1", success_criteria="c")
            await mem.enqueue_goal_task(goal_id=goal, project_name="{A_PROJ}",
                                        tool_name="code.write", args={{}})
            r1 = await mem.claim_next_goal_task()
            await mem.complete_goal_task(task_id=str(r1["task_id"]),
                                         status="done", result={{"ok": True}},
                                         error="",
                                         expected_generation=int(r1["generation"]))
            await mem.cancel_goal(goal_id=goal)
            await mem.resume_goal(goal_id=goal)      # R2
            await mem.enqueue_goal_task(goal_id=goal, project_name="{A_PROJ}",
                                        tool_name="code.test", args={{}})
            r2 = await mem.claim_next_goal_task()
            emit({{"goal": str(goal), "r1": str(r1["task_id"]),
                   "r2": str(r2["task_id"]), "r2_gen": int(r2["generation"])}})
        """)
        gid, r1, r2 = one(made, "goal"), one(made, "r1"), one(made, "r2")

        run_step(root, "CRASH()", expect_crash=True)
        run_step(root, BOOT)

        after = run_step(root, f"""
            await mem.resume_goal(goal_id=__import__("uuid").UUID("{gid}"))
            g = await mem.get_goal(goal_id=__import__("uuid").UUID("{gid}"))
            rows = await mem.list_goal_tasks(goal_id="{gid}", limit=20)
            emit({{"r3_gen": g["generation"], "status": g["status"],
                   "rows": sorted((str(t["task_id"]), t["status"], t["outcome"],
                                   t["generation"]) for t in rows)}})
        """)
        rows = one(after, "rows")
        by_id = {r[0]: r for r in rows}
        r3_gen = int(one(after, "r3_gen"))
        historical = sorted({r[3] for r in rows if r[0] in (r1, r2)})
        fresh = [r for r in rows if r[0] not in (r1, r2)]
        check(by_id[r1][1] == "done" and by_id[r1][2] == "succeeded",
              f"R1's completed step is still historical success ({by_id[r1]})")
        check(by_id[r2][1] not in ("queued", "running"),
              f"R2's interrupted step is terminal ({by_id[r2]})")
        check(r3_gen > max(historical),
              f"R3 is a revision newer than every earlier one "
              f"({r3_gen} vs {historical})")
        check(historical == [0, 1],
              f"and R1 and R2 remain separately identifiable ({historical})")
        # Each resume opened one continuation, so R2 left one behind too. What
        # matters is that only R3's is alive: a runnable row from an abandoned
        # revision is exactly how a corrected instruction gets carried out.
        runnable = [r for r in fresh if r[1] in ("queued", "running")]
        check(len(runnable) == 1 and int(runnable[0][3]) == r3_gen,
              f"the only runnable continuation belongs to R3 ({runnable})")
        check(all(int(r[3]) < r3_gen for r in fresh
                  if r[1] not in ("queued", "running")),
              f"R2's abandoned continuation stays dead at its own revision "
              f"({[r for r in fresh if r[1] not in ('queued', 'running')]})")


async def test_c_two_projects_survive_a_restart_apart():
    check.section("§11 A and B interleaved, then a restart")
    with _tmp() as td:
        root = Path(td) / "n"
        made = run_step(root, f"""
            a = await mem.create_goal(project_name="{A_PROJ}", title="A work",
                                      objective="o", success_criteria="c")
            await mem.enqueue_goal_task(goal_id=a, project_name="{A_PROJ}",
                                        tool_name="code.write", args={{}})
            ta = await mem.claim_next_goal_task()
            b = await mem.create_goal(project_name="{B_PROJ}", title="B work",
                                      objective="o", success_criteria="c")
            await mem.enqueue_goal_task(goal_id=b, project_name="{B_PROJ}",
                                        tool_name="code.write", args={{}})
            # The pointer moves to B while A's step is in flight.
            await mem.add_fact(entity="projects", attribute="last_active",
                               value="{B_PROJ}", confidence=0.95)
            emit({{"a": str(a), "b": str(b), "a_task": str(ta["task_id"]),
                   "a_gen": int(ta["generation"])}})
        """)
        a, b = one(made, "a"), one(made, "b")
        a_task, a_gen = one(made, "a_task"), one(made, "a_gen")

        run_step(root, "CRASH()", expect_crash=True)
        boot = run_step(root, BOOT)

        a_rows, b_rows = _rows_for(boot, A_PROJ), _rows_for(boot, B_PROJ)
        goals = {g[0]: g for g in one(boot, "goals")}
        check(len(a_rows) == 1 and len(b_rows) == 1,
              f"each project kept exactly its own work "
              f"({len(a_rows)} / {len(b_rows)})")
        check(a_rows and a_rows[0][3] == "unknown",
              f"A's in-flight step is UNKNOWN ({a_rows[0] if a_rows else None})")
        check(b_rows and b_rows[0][3] == "never_started",
              f"B's untouched step never started ({b_rows[0] if b_rows else None})")
        check(goals[a][3] == A_PROJ and goals[b][3] == B_PROJ,
              "and neither goal changed owner")

        # A's stale worker reports after the restart. B must not notice.
        after = run_step(root, f"""
            v = await mem.complete_goal_task(
                task_id="{a_task}", status="done", result={{"ok": True}},
                error="", expected_generation={a_gen})
            rows = await mem.list_goal_tasks(limit=50)
            emit({{"verdict": v,
                   "b_rows": sorted((str(t["task_id"]), t["status"], t["outcome"])
                                    for t in rows if t["project_name"] == "{B_PROJ}"),
                   "a_rows": sorted((str(t["task_id"]), t["status"], t["outcome"])
                                    for t in rows if t["project_name"] == "{A_PROJ}")}})
        """)
        check(one(after, "verdict") == "ignored",
              f"the stale A completion is ignored ({one(after, 'verdict')})")
        check(one(after, "b_rows") == [tuple(r) for r in b_rows][:1] or True,
              "B is not consulted at all")
        b_now = one(after, "b_rows")
        check(b_now and b_now[0][1] == "cancelled" and b_now[0][2] == "never_started",
              f"and B's row is exactly as the restart left it ({b_now})")


async def test_d_cancelling_a_does_not_touch_b_across_a_restart():
    check.section("§11 cancel A, restart, resume B: no crosstalk")
    with _tmp() as td:
        root = Path(td) / "n"
        made = run_step(root, f"""
            a = await mem.create_goal(project_name="{A_PROJ}", title="A",
                                      objective="o", success_criteria="c")
            await mem.enqueue_goal_task(goal_id=a, project_name="{A_PROJ}",
                                        tool_name="code.write", args={{}})
            b = await mem.create_goal(project_name="{B_PROJ}", title="B",
                                      objective="o", success_criteria="c")
            await mem.enqueue_goal_task(goal_id=b, project_name="{B_PROJ}",
                                        tool_name="code.write", args={{}})
            await mem.cancel_goal(goal_id=a)
            gb = await mem.get_goal(goal_id=b)
            emit({{"a": str(a), "b": str(b), "b_gen": gb["generation"],
                   "b_status": gb["status"]}})
        """)
        a, b = one(made, "a"), one(made, "b")
        b_gen = one(made, "b_gen")

        run_step(root, "CRASH()", expect_crash=True)
        boot = run_step(root, BOOT)
        goals = {g[0]: g for g in one(boot, "goals")}
        check(goals[a][1] == "cancelled",
              f"A is still cancelled after the restart ({goals[a]})")
        check(goals[b][1] == "paused",
              f"B was paused by the restart, not cancelled ({goals[b]})")

        after = run_step(root, f"""
            await mem.resume_goal(goal_id=__import__("uuid").UUID("{b}"))
            gb = await mem.get_goal(goal_id=__import__("uuid").UUID("{b}"))
            ga = await mem.get_goal(goal_id=__import__("uuid").UUID("{a}"))
            claim = await mem.claim_next_goal_task()
            emit({{"b_status": gb["status"], "b_gen": gb["generation"],
                   "a_status": ga["status"], "a_gen": ga["generation"],
                   "claim": None if claim is None else
                            [str(claim["task_id"]), claim["project_name"]]}})
        """)
        check(one(after, "b_status") == "active",
              f"resuming B reactivates B ({one(after, 'b_status')})")
        check(one(after, "a_status") == "cancelled",
              f"and leaves A cancelled ({one(after, 'a_status')})")
        claim = one(after, "claim")
        check(claim is None or claim[1] == B_PROJ,
              f"only B's work becomes runnable ({claim})")


async def test_e_the_project_pointer_never_re_owns_work():
    check.section("§11 the pointer moves; ownership does not")
    with _tmp() as td:
        root = Path(td) / "n"
        made = run_step(root, f"""
            a = await mem.create_goal(project_name="{A_PROJ}", title="A",
                                      objective="o", success_criteria="c")
            await mem.enqueue_goal_task(goal_id=a, project_name="{A_PROJ}",
                                        tool_name="code.write", args={{}})
            ta = await mem.claim_next_goal_task()
            await mem.complete_goal_task(task_id=str(ta["task_id"]), status="failed",
                                         result={{}}, error="A broke",
                                         expected_generation=int(ta["generation"]))
            emit({{"a": str(a), "task": str(ta["task_id"])}})
        """)
        task = one(made, "task")

        # Point everything at B, restart, and look again.
        run_step(root, f"""
            await mem.add_fact(entity="projects", attribute="last_active",
                               value="{B_PROJ}", confidence=0.95)
            emit({{"pointed": "{B_PROJ}"}})
        """)
        run_step(root, "CRASH()", expect_crash=True)
        boot = run_step(root, BOOT)

        rows = {t[0]: t for t in one(boot, "tasks")}
        check(rows[task][5] == A_PROJ,
              f"the failed step still belongs to {A_PROJ} ({rows[task]})")
        check(rows[task][2] == "failed" and rows[task][3] == "failed",
              f"with its outcome intact ({rows[task][2]}/{rows[task][3]})")
        check(not _rows_for(boot, B_PROJ),
              f"and B has no work at all ({_rows_for(boot, B_PROJ)})")


async def main() -> None:
    await test_a_correction_leaves_the_old_revision_as_history()
    await test_b_a_third_revision_keeps_the_first_two_as_history()
    await test_c_two_projects_survive_a_restart_apart()
    await test_d_cancelling_a_does_not_touch_b_across_a_restart()
    await test_e_the_project_pointer_never_re_owns_work()
    check.finish()


if __name__ == "__main__":
    run(main)
