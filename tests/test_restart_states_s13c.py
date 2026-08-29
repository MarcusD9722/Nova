"""Every meaningful state, carried across a real process boundary (13C).

THE QUESTION

If the process dies and everything volatile goes with it, can Nova reconstruct
what is true from durable authority alone?

Each case here is built in ONE process, observed in a SECOND, and judged only
on what the second could read from disk. Nothing is carried across in Python:
the child is handed a directory path and nothing else.

WHAT IS ASSERTED, for every state:

  * the authoritative rows before the restart
  * the authoritative rows after it, read by a fresh interpreter
  * that nothing stale became runnable
  * that completed work was not replayed
  * that a cancellation did not become a pause or a resume
  * that ownership survived, including when the current-project pointer moved

Attribution is by goal_id / task_id / generation / project_name read back from
the store. Never by list position, arrival order, or timing.

Run:  venv\\Scripts\\python.exe tests\\test_restart_states_s13c.py
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
# Each case spawns two or three child interpreters; the default 180s watchdog
# would be catching honest work rather than a hang.
os.environ.setdefault("NOVA_IT_WATCHDOG_S", "1200")

from harness import Checks, run  # noqa: E402

from restart_harness import one, run_step  # noqa: E402

check = Checks()

GAME = "flappy-bird"
CALC = "quickcalc"

#: Read every task and goal, as a fresh process would.
SNAPSHOT = """
    tasks = await mem.list_goal_tasks(limit=200)
    goals = await mem.list_goals(limit=100)
    emit({"tasks": [(str(t["task_id"]), t["tool_name"], t["status"],
                     t["outcome"], t["generation"], t["project_name"])
                    for t in tasks],
          "goals": [(str(g["goal_id"]), g["status"], g["generation"],
                     g["project_name"]) for g in goals]})
"""

#: Boot recovery, then the same snapshot, then what the queue will hand out.
RECOVER = """
    rec = await mem.cancel_pending_background_work()
    tasks = await mem.list_goal_tasks(limit=200)
    goals = await mem.list_goals(limit=100)
    claimed = await mem.claim_next_goal_task()
    emit({"recovery": rec,
          "tasks": [(str(t["task_id"]), t["tool_name"], t["status"],
                     t["outcome"], t["generation"], t["project_name"])
                    for t in tasks],
          "goals": [(str(g["goal_id"]), g["status"], g["generation"],
                     g["project_name"]) for g in goals],
          "claimable": None if claimed is None else
                       [str(claimed["task_id"]), claimed["tool_name"],
                        claimed["generation"]]})
"""


def _task(rows, task_id):
    for t in rows or []:
        if t[0] == str(task_id):
            return t
    return None


def _goal(rows, goal_id):
    for g in rows or []:
        if g[0] == str(goal_id):
            return g
    return None


def _case(name: str):
    check.section(name)
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


async def test_a_queued_survives_as_never_started():
    with _case("A. queued at the moment of death") as td:
        root = Path(td) / "nova"
        made = run_step(root, f"""
            goal = await mem.create_goal(project_name="{GAME}", title="pause menu",
                                         objective="o", success_criteria="c")
            await mem.enqueue_goal_task(goal_id=goal, project_name="{GAME}",
                                        tool_name="code.write", args={{}})
            rows = await mem.list_goal_tasks(goal_id=str(goal), limit=5)
            emit({{"goal": str(goal), "task": str(rows[0]["task_id"]),
                   "status": rows[0]["status"]}})
        """)
        gid, tid = one(made, "goal"), one(made, "task")
        check(one(made, "status") == "queued", f"queued before ({one(made,'status')})")

        after = run_step(root, RECOVER)
        row = _task(one(after, "tasks"), tid)
        goal = _goal(one(after, "goals"), gid)
        check(row is not None and row[2] == "cancelled",
              f"a fresh process finds it cancelled ({row})")
        check(row is not None and row[3] == "never_started",
              f"and provably never started ({row[3] if row else None})")
        check(one(after, "claimable") is None,
              f"nothing is runnable ({one(after, 'claimable')})")
        check(goal is not None and goal[1] == "paused",
              f"its goal is paused, not active ({goal})")


async def test_b_running_before_the_tool_is_unknown():
    with _case("B/C. running when the process died") as td:
        root = Path(td) / "nova"
        made = run_step(root, f"""
            goal = await mem.create_goal(project_name="{GAME}", title="upload",
                                         objective="o", success_criteria="c")
            await mem.enqueue_goal_task(goal_id=goal, project_name="{GAME}",
                                        tool_name="deploy.push", args={{}})
            c = await mem.claim_next_goal_task()
            emit({{"goal": str(goal), "task": str(c["task_id"]),
                   "status": "running"}})
        """)
        gid, tid = one(made, "goal"), one(made, "task")

        seen = run_step(root, SNAPSHOT)
        row = _task(one(seen, "tasks"), tid)
        check(row is not None and row[2] == "running",
              f"the row is still running when the next process opens it ({row})")

        after = run_step(root, RECOVER)
        row = _task(one(after, "tasks"), tid)
        check(row is not None and row[2] == "failed",
              f"recovery finalises it ({row[2] if row else None})")
        check(row is not None and row[3] == "unknown",
              f"as UNKNOWN, because it may have acted ({row[3] if row else None})")
        check(row is not None and row[3] not in ("succeeded", "never_started"),
              "neither claimed nor denied")
        check(int(one(after, "recovery")["interrupted"]) == 1,
              f"and the restart counts it as interrupted ({one(after,'recovery')})")


async def test_d_blocked_survives_with_its_question():
    with _case("D. blocked, waiting on a person") as td:
        root = Path(td) / "nova"
        made = run_step(root, f"""
            tid = str(await mem.enqueue_task(
                title="pick the overlay", details="d", project_name="{GAME}",
                initiated_by_user=True))
            await mem.claim_next_task()
            await mem.mark_task_blocked(task_id=tid,
                                        question="Dark overlay or a blur?")
            emit({{"task": tid}})
        """)
        tid = one(made, "task")

        after = run_step(root, """
            rec = await mem.cancel_pending_background_work()
            rows = [t for t in await mem.list_tasks(limit=50)]
            claim = await mem.claim_next_task()
            emit({"rows": [(str(t["task_id"]), t["status"], t["outcome"],
                            str(t.get("last_error") or "")) for t in rows],
                  "claim": None if claim is None else str(claim["task_id"]),
                  "recovery": rec})
        """)
        rows = one(after, "rows")
        mine = [r for r in rows if r[0] == tid]
        check(mine and mine[0][1] == "blocked",
              f"still blocked after a restart ({mine})")
        check(mine and "Dark overlay" in mine[0][3],
              f"with the same question ({mine[0][3][:40] if mine else None!r})")
        check(one(after, "claim") is None,
              f"and nothing claims it ({one(after, 'claim')})")
        check(int(one(after, "recovery")["interrupted"]) == 0,
              "the restart does not pretend it was in flight")


async def test_e_f_paused_and_cancelled_do_not_move():
    with _case("E/F. a paused goal and a cancelled goal") as td:
        root = Path(td) / "nova"
        made = run_step(root, f"""
            paused = await mem.create_goal(project_name="{GAME}", title="paused one",
                                           objective="o", success_criteria="c")
            await mem.enqueue_goal_task(goal_id=paused, project_name="{GAME}",
                                        tool_name="code.write", args={{}})
            await mem.update_goal_status(goal_id=paused, status="paused")
            killed = await mem.create_goal(project_name="{GAME}", title="cancelled one",
                                           objective="o", success_criteria="c")
            await mem.enqueue_goal_task(goal_id=killed, project_name="{GAME}",
                                        tool_name="code.write", args={{}})
            await mem.cancel_goal(goal_id=killed)
            g = await mem.get_goal(goal_id=killed)
            emit({{"paused": str(paused), "killed": str(killed),
                   "killed_gen": g["generation"]}})
        """)
        pid, kid = one(made, "paused"), one(made, "killed")
        kgen = one(made, "killed_gen")

        after = run_step(root, RECOVER)
        p, k = _goal(one(after, "goals"), pid), _goal(one(after, "goals"), kid)
        check(p is not None and p[1] == "paused",
              f"the paused goal is still paused ({p})")
        check(k is not None and k[1] == "cancelled",
              f"the cancelled goal is still cancelled ({k})")
        check(k is not None and k[2] == kgen,
              f"and its revision did not move ({k[2] if k else None} vs {kgen})")
        check(one(after, "claimable") is None,
              f"neither offers work ({one(after, 'claimable')})")


async def test_g_h_terminal_rows_are_not_reopened():
    with _case("G/H. a failed task and a completed task") as td:
        root = Path(td) / "nova"
        made = run_step(root, f"""
            goal = await mem.create_goal(project_name="{GAME}", title="two steps",
                                         objective="o", success_criteria="c")
            for tool in ("code.write", "code.test"):
                await mem.enqueue_goal_task(goal_id=goal, project_name="{GAME}",
                                            tool_name=tool, args={{}})
            a = await mem.claim_next_goal_task()
            await mem.complete_goal_task(task_id=str(a["task_id"]), status="done",
                                         result={{"ok": True}}, error="",
                                         expected_generation=int(a["generation"]))
            b = await mem.claim_next_goal_task()
            await mem.complete_goal_task(task_id=str(b["task_id"]), status="failed",
                                         result={{}}, error="the sprite sheet is missing",
                                         expected_generation=int(b["generation"]))
            emit({{"done": str(a["task_id"]), "failed": str(b["task_id"])}})
        """)
        done_id, failed_id = one(made, "done"), one(made, "failed")

        after = run_step(root, RECOVER)
        d = _task(one(after, "tasks"), done_id)
        f = _task(one(after, "tasks"), failed_id)
        check(d is not None and (d[2], d[3]) == ("done", "succeeded"),
              f"the completed step is still done/succeeded ({d})")
        check(f is not None and (f[2], f[3]) == ("failed", "failed"),
              f"the failed step is still failed/failed ({f})")
        check(one(after, "claimable") is None,
              f"and neither is offered again ({one(after, 'claimable')})")


async def test_i_a_scheduled_retry_does_not_multiply():
    with _case("I. a retry scheduled when the process died") as td:
        root = Path(td) / "nova"
        made = run_step(root, f"""
            goal = await mem.create_goal(project_name="{GAME}", title="flaky",
                                         objective="o", success_criteria="c")
            await mem.enqueue_goal_task(goal_id=goal, project_name="{GAME}",
                                        tool_name="deploy.push", args={{}})
            c = await mem.claim_next_goal_task()
            ok = await mem.bump_goal_task_attempt(
                task_id=str(c["task_id"]), attempts=1,
                run_after_iso="2000-01-01T00:00:00+00:00", error="network blipped",
                expected_generation=int(c["generation"]))
            emit({{"task": str(c["task_id"]), "requeued": ok}})
        """)
        tid = one(made, "task")
        check(one(made, "requeued") is True, "the retry was scheduled")

        after = run_step(root, RECOVER)
        row = _task(one(after, "tasks"), tid)
        check(row is not None and row[2] == "cancelled",
              f"a restart does not let it run unasked ({row})")
        check(row is not None and row[3] == "never_started",
              f"and it had not started ({row[3] if row else None})")
        check(one(after, "claimable") is None,
              f"there is no retry waiting to fire ({one(after, 'claimable')})")

        # A SECOND restart must not multiply anything.
        again = run_step(root, RECOVER)
        rows_1 = one(after, "tasks")
        rows_2 = one(again, "tasks")
        check(len(rows_1) == len(rows_2),
              f"a second restart adds no rows ({len(rows_1)} -> {len(rows_2)})")
        check(rows_1 == rows_2, "and changes nothing at all")


async def test_j_k_superseded_and_unknown_survive_as_themselves():
    with _case("J/K. a superseded row and an unknown outcome") as td:
        root = Path(td) / "nova"
        made = run_step(root, f"""
            goal = await mem.create_goal(project_name="{GAME}", title="superseded",
                                         objective="o", success_criteria="c")
            await mem.enqueue_goal_task(goal_id=goal, project_name="{GAME}",
                                        tool_name="deploy.push", args={{}})
            c = await mem.claim_next_goal_task()
            await mem.cancel_goal(goal_id=goal)
            v = await mem.complete_goal_task(
                task_id=str(c["task_id"]), status="done", result={{"ok": True}},
                error="", expected_generation=int(c["generation"]))
            unknown = str(await mem.enqueue_task(
                title="upload", details="d", project_name="{GAME}",
                initiated_by_user=True))
            await mem.claim_next_task()
            await mem.mark_task_failed(task_id=unknown, outcome="unknown",
                                       error="interrupted mid-call",
                                       result={{"status": "interrupted_tool_unknown"}})
            emit({{"sup": str(c["task_id"]), "verdict": v, "unknown": unknown}})
        """)
        sup, unk = one(made, "sup"), one(made, "unknown")
        check(one(made, "verdict") == "superseded", "the completion was superseded")

        after = run_step(root, """
            await mem.cancel_pending_background_work()
            g = await mem.list_goal_tasks(limit=100)
            a = await mem.list_tasks(limit=50)
            claim_g = await mem.claim_next_goal_task()
            claim_a = await mem.claim_next_task()
            emit({"goal_tasks": [(str(t["task_id"]), t["status"], t["outcome"])
                                 for t in g],
                  "auto": [(str(t["task_id"]), t["status"], t["outcome"])
                           for t in a],
                  "claimable": [claim_g is not None, claim_a is not None]})
        """)
        s = [r for r in one(after, "goal_tasks") if r[0] == sup]
        u = [r for r in one(after, "auto") if r[0] == unk]
        check(s and (s[0][1], s[0][2]) == ("superseded", "succeeded"),
              f"the superseded row reloads intact ({s})")
        check(u and u[0][2] == "unknown",
              f"and the unknown outcome is still unknown ({u})")
        check(one(after, "claimable") == [False, False],
              f"neither becomes runnable ({one(after, 'claimable')})")


async def test_n_stale_generation_rows_never_run_for_the_new_one():
    with _case("N. run-N rows while N+1 is current") as td:
        root = Path(td) / "nova"
        made = run_step(root, f"""
            goal = await mem.create_goal(project_name="{GAME}", title="revised",
                                         objective="o", success_criteria="c")
            await mem.enqueue_goal_task(goal_id=goal, project_name="{GAME}",
                                        tool_name="code.write", args={{}})
            c = await mem.claim_next_goal_task()
            await mem.cancel_goal(goal_id=goal)
            await mem.resume_goal(goal_id=goal)
            g = await mem.get_goal(goal_id=goal)
            emit({{"goal": str(goal), "stale": str(c["task_id"]),
                   "stale_gen": int(c["generation"]), "now": g["generation"]}})
        """)
        gid, stale = one(made, "goal"), one(made, "stale")
        check(one(made, "now") > one(made, "stale_gen"),
              f"the goal moved to run {one(made, 'now')}")

        after = run_step(root, RECOVER)
        row = _task(one(after, "tasks"), stale)
        claimable = one(after, "claimable")
        check(row is not None and row[4] == one(made, "stale_gen"),
              f"the stale row still belongs to its own run ({row})")
        check(row is not None and row[2] not in ("queued", "running"),
              f"and is not runnable ({row[2] if row else None})")
        check(claimable is None or claimable[0] != stale,
              f"the queue never offers it ({claimable})")


async def test_o_ownership_survives_the_project_pointer_moving():
    with _case("O. the current project moved away from the work") as td:
        root = Path(td) / "nova"
        made = run_step(root, f"""
            a = await mem.create_goal(project_name="{GAME}", title="A work",
                                      objective="o", success_criteria="c")
            await mem.enqueue_goal_task(goal_id=a, project_name="{GAME}",
                                        tool_name="code.write", args={{}})
            b = await mem.create_goal(project_name="{CALC}", title="B work",
                                      objective="o", success_criteria="c")
            await mem.enqueue_goal_task(goal_id=b, project_name="{CALC}",
                                        tool_name="code.write", args={{}})
            # The pointer says B; the work still belongs to whoever owns it.
            await mem.add_fact(entity="projects", attribute="last_active",
                               value="{CALC}", confidence=0.95)
            emit({{"a": str(a), "b": str(b)}})
        """)
        a, b = one(made, "a"), one(made, "b")

        after = run_step(root, RECOVER)
        tasks = one(after, "tasks")
        a_rows = [t for t in tasks if t[5] == GAME]
        b_rows = [t for t in tasks if t[5] == CALC]
        check(len(a_rows) == 1 and len(b_rows) == 1,
              f"each project keeps exactly its own work "
              f"({len(a_rows)} / {len(b_rows)})")
        check(_goal(one(after, "goals"), a)[3] == GAME,
              "A's goal is still A's")
        check(_goal(one(after, "goals"), b)[3] == CALC,
              "B's goal is still B's")
        check(len(tasks) >= 2 and all(t[5] in (GAME, CALC) for t in tasks),
              f"nothing was reattributed anywhere else "
              f"({len(tasks)} steps across {sorted({t[5] for t in tasks})})")


async def main() -> None:
    await test_a_queued_survives_as_never_started()
    await test_b_running_before_the_tool_is_unknown()
    await test_d_blocked_survives_with_its_question()
    await test_e_f_paused_and_cancelled_do_not_move()
    await test_g_h_terminal_rows_are_not_reopened()
    await test_i_a_scheduled_retry_does_not_multiply()
    await test_j_k_superseded_and_unknown_survive_as_themselves()
    await test_n_stale_generation_rows_never_run_for_the_new_one()
    await test_o_ownership_survives_the_project_pointer_moving()
    check.finish()


if __name__ == "__main__":
    run(main)
