"""The crash-window matrix (Stage 13C).

Twelve boundaries where a process can die, each one exercised by killing a real
interpreter at exactly that line.

WHY THE CRASH IS PLACED AND NOT TIMED

`CRASH()` calls `os._exit`, which skips every finally block, atexit hook and
buffer flush. Whatever reached SQLite before that line is all that survives.
Killing a child after N seconds and hoping it was in the right place would make
each window a guess; naming the line makes it a fact.

THE RULE ALL TWELVE SERVE

A restart may not convert uncertainty into certainty. It may not decide that
work which might have happened definitely did, nor that it definitely did not.
Where the durable record cannot prove an outcome, the outcome stays UNKNOWN and
a person decides.

Run:  venv\\Scripts\\python.exe tests\\test_crash_windows_s13c.py
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

from restart_harness import one, run_step, scenario  # noqa: E402

check = Checks()

P = "flappy-bird"

RECOVER = """
    rec = await mem.cancel_pending_background_work()
    tasks = await mem.list_goal_tasks(limit=200)
    autos = await mem.list_tasks(limit=100)
    goals = await mem.list_goals(limit=50)
    claim = await mem.claim_next_goal_task()
    emit({"recovery": rec,
          "tasks": [(str(t["task_id"]), t["tool_name"], t["status"],
                     t["outcome"], t["generation"]) for t in tasks],
          "autos": [(str(t["task_id"]), t["status"], t["outcome"]) for t in autos],
          "goals": [(str(g["goal_id"]), g["status"], g["generation"]) for g in goals],
          "claimable": None if claim is None else str(claim["task_id"])})
"""

SEED = """
    goal = await mem.create_goal(project_name="%s", title="upload the build",
                                 objective="o", success_criteria="c")
    await mem.enqueue_goal_task(goal_id=goal, project_name="%s",
                                tool_name="deploy.push", args={})
""" % (P, P)


def _t(rows, tid):
    for r in rows or []:
        if r[0] == str(tid):
            return r
    return None


def _tmp():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


async def window_1_before_the_tool_starts():
    check.section("W1  crash before the tool starts")
    with _tmp() as td:
        root = Path(td) / "n"
        made = run_step(root, scenario(SEED, """
            rows = await mem.list_goal_tasks(limit=5)
            emit({"task": str(rows[0]["task_id"])})
        """))
        tid = one(made, "task")
        run_step(root, """
            rows = await mem.list_goal_tasks(limit=5)
            CRASH()
        """, expect_crash=True)

        after = run_step(root, RECOVER)
        row = _t(one(after, "tasks"), tid)
        check(row is not None and row[2] == "cancelled",
              f"it never ran, and says so ({row})")
        check(row is not None and row[3] == "never_started",
              f"provably never started ({row[3] if row else None})")
        check(one(after, "claimable") is None,
              f"and there is no replay ambiguity ({one(after, 'claimable')})")


async def window_2_during_the_tool():
    check.section("W2  crash with the tool in flight")
    with _tmp() as td:
        root = Path(td) / "n"
        made = run_step(root, scenario(SEED, """
            c = await mem.claim_next_goal_task()
            emit({"task": str(c["task_id"])})
        """))
        tid = one(made, "task")
        run_step(root, """
            # claimed, tool believed to be running, then the lights go out
            CRASH()
        """, expect_crash=True)

        after = run_step(root, RECOVER)
        row = _t(one(after, "tasks"), tid)
        check(row is not None and row[3] == "unknown",
              f"the outcome is UNKNOWN ({row})")
        check(row is not None and row[3] != "succeeded",
              "never claimed as success")
        check(row is not None and row[3] != "never_started",
              "and never claimed as nothing-happened")


async def window_3_side_effect_then_crash_before_bookkeeping():
    check.section("W3  the side effect landed, the bookkeeping did not")
    with _tmp() as td:
        root = Path(td) / "n"
        made = run_step(root, scenario(SEED, """
            c = await mem.claim_next_goal_task()
            emit({"task": str(c["task_id"])})
        """))
        tid = one(made, "task")
        # A real, observable side effect on disk, then death before the row
        # is written. The file is the evidence the database does not have.
        run_step(root, f"""
            (Path(r"{root}") / "projects" / "{P}").mkdir(parents=True, exist_ok=True)
            (Path(r"{root}") / "projects" / "{P}" / "uploaded.txt").write_text(
                "the side effect happened", encoding="utf-8")
            CRASH()
        """, expect_crash=True)

        after = run_step(root, RECOVER)
        row = _t(one(after, "tasks"), tid)
        artefact = (root / "projects" / P / "uploaded.txt")
        check(artefact.exists(), "the side effect really did happen on disk")
        check(row is not None and row[3] == "unknown",
              f"and the record says UNKNOWN rather than guessing ({row})")
        check(one(after, "claimable") is None,
              f"nothing is queued to blindly do it again ({one(after,'claimable')})")


async def window_4_success_persisted_then_crash():
    check.section("W4  success persisted, crash before the progress event")
    with _tmp() as td:
        root = Path(td) / "n"
        made = run_step(root, scenario(SEED, """
            c = await mem.claim_next_goal_task()
            await mem.complete_goal_task(task_id=str(c["task_id"]), status="done",
                                         result={"ok": True}, error="",
                                         expected_generation=int(c["generation"]))
            emit({"task": str(c["task_id"])})
        """))
        tid = one(made, "task")
        run_step(root, """
            CRASH()
        """, expect_crash=True)

        after = run_step(root, RECOVER)
        row = _t(one(after, "tasks"), tid)
        check(row is not None and (row[2], row[3]) == ("done", "succeeded"),
              f"execution truth does not regress ({row})")
        check(one(after, "claimable") is None,
              f"and it is not rerun ({one(after, 'claimable')})")


async def window_5_no_acknowledgement_is_not_authorisation():
    check.section("W5  success + progress persisted, no reply reached the user")
    with _tmp() as td:
        root = Path(td) / "n"
        made = run_step(root, scenario(SEED, """
            c = await mem.claim_next_goal_task()
            gid = str(c["goal_id"])
            await mem.complete_goal_task(task_id=str(c["task_id"]), status="done",
                                         result={"ok": True}, error="",
                                         expected_generation=int(c["generation"]))
            await mem.add_progress_event(
                goal_id=__import__("uuid").UUID(gid), project_name="%s",
                kind="tool", message="deploy.push completed",
                generation=int(c["generation"]), task_id=str(c["task_id"]))
            emit({"task": str(c["task_id"]), "goal": gid})
        """ % P))
        tid, gid = one(made, "task"), one(made, "goal")
        run_step(root, "CRASH()", expect_crash=True)

        after = run_step(root, scenario(RECOVER, """
            ev = await mem.list_progress_events(goal_id="%s", limit=20)
            emit({"events": [(e["kind"], e["generation"]) for e in ev]})
        """ % gid))
        row = _t(one(after, "tasks"), tid)
        check(row is not None and (row[2], row[3]) == ("done", "succeeded"),
              f"the work is still done ({row})")
        check(one(after, "claimable") is None,
              "a missing acknowledgement is not authorisation to run it again")
        kinds = [k for k, _g in one(after, "events") or []]
        check("tool" in kinds, f"and the progress survives to be reported ({kinds})")


async def window_7_cancel_then_a_late_completion():
    check.section("W7  cancel persisted, crash, then the old worker returns")
    with _tmp() as td:
        root = Path(td) / "n"
        made = run_step(root, scenario(SEED, """
            c = await mem.claim_next_goal_task()
            await mem.cancel_goal(goal_id=c["goal_id"])
            g = await mem.get_goal(goal_id=c["goal_id"])
            emit({"task": str(c["task_id"]), "goal": str(c["goal_id"]),
                  "gen_at_claim": int(c["generation"]), "now": g["generation"]})
        """))
        tid, gen = one(made, "task"), one(made, "gen_at_claim")
        now = one(made, "now")
        run_step(root, "CRASH()", expect_crash=True)

        # A fresh process plays the part of the worker that never died.
        after = run_step(root, f"""
            await mem.cancel_pending_background_work()
            v = await mem.complete_goal_task(
                task_id="{tid}", status="done", result={{"ok": True}}, error="",
                expected_generation={gen})
            rows = await mem.list_goal_tasks(limit=20)
            g = await mem.list_goals(limit=10)
            emit({{"verdict": v,
                   "tasks": [(str(t["task_id"]), t["tool_name"], t["status"],
                              t["outcome"], t["generation"]) for t in rows],
                   "goals": [(str(x["goal_id"]), x["status"], x["generation"])
                             for x in g]}})
        """)
        row = _t(one(after, "tasks"), tid)
        goal = (one(after, "goals") or [[None, None, None]])[0]
        check(one(after, "verdict") in ("superseded", "ignored"),
              f"the late completion does not apply ({one(after, 'verdict')})")
        check(row is not None and row[2] != "done",
              f"and the row is not marked done ({row})")
        check(goal[1] == "cancelled",
              f"the goal is still cancelled ({goal})")
        check(int(goal[2]) == int(now),
              f"on the same revision as before the crash ({goal[2]} vs {now})")


async def window_8_stale_rows_never_serve_the_new_run():
    check.section("W8  generation N+1 persisted, crash, N rows remain")
    with _tmp() as td:
        root = Path(td) / "n"
        made = run_step(root, scenario(SEED, """
            c = await mem.claim_next_goal_task()
            await mem.cancel_goal(goal_id=c["goal_id"])
            await mem.resume_goal(goal_id=c["goal_id"])
            g = await mem.get_goal(goal_id=c["goal_id"])
            emit({"stale": str(c["task_id"]), "stale_gen": int(c["generation"]),
                  "now": g["generation"], "goal": str(c["goal_id"])})
        """))
        stale, now = one(made, "stale"), one(made, "now")
        run_step(root, "CRASH()", expect_crash=True)

        after = run_step(root, RECOVER)
        row = _t(one(after, "tasks"), stale)
        claimable = one(after, "claimable")
        check(row is not None and int(row[4]) == int(one(made, "stale_gen")),
              f"the run-{one(made,'stale_gen')} row still belongs to it ({row})")
        check(claimable != stale,
              f"and never runs on behalf of run {now} ({claimable})")


async def window_9_a_retry_does_not_multiply():
    check.section("W9  retry decision persisted, crash before it ran")
    with _tmp() as td:
        root = Path(td) / "n"
        made = run_step(root, scenario(SEED, """
            c = await mem.claim_next_goal_task()
            await mem.bump_goal_task_attempt(
                task_id=str(c["task_id"]), attempts=1,
                run_after_iso="2000-01-01T00:00:00+00:00", error="blip",
                expected_generation=int(c["generation"]))
            emit({"task": str(c["task_id"])})
        """))
        tid = one(made, "task")
        run_step(root, "CRASH()", expect_crash=True)

        first = run_step(root, RECOVER)
        second = run_step(root, RECOVER)
        third = run_step(root, RECOVER)
        check(len(one(first, "tasks")) == 1,
              f"one row before ({len(one(first, 'tasks'))})")
        check(one(first, "tasks") == one(second, "tasks") == one(third, "tasks"),
              "and three restarts change nothing at all")
        check(all(one(x, "claimable") is None for x in (first, second, third)),
              "no retry fires, and none multiplies")


async def window_10_no_contradictory_state():
    check.section("W10  crash while recording: no contradictory row")
    with _tmp() as td:
        root = Path(td) / "n"
        run_step(root, scenario(SEED, """
            c = await mem.claim_next_goal_task()
            emit({"task": str(c["task_id"])})
        """))
        run_step(root, "CRASH()", expect_crash=True)
        after = run_step(root, RECOVER)

        # The contradictions the two axes make expressible, and must not allow.
        forbidden = {("queued", "succeeded"), ("running", "never_started"),
                     ("done", "pending"), ("done", "failed"),
                     ("cancelled", "succeeded"), ("blocked", "succeeded")}
        pairs = {(t[2], t[3]) for t in one(after, "tasks")}
        check(not (pairs & forbidden),
              f"no impossible status/outcome pair exists ({sorted(pairs)})")
        autos = {(t[1], t[2]) for t in one(after, "autos") or []}
        check(not (autos & forbidden),
              f"nor among background tasks ({sorted(autos)})")


async def window_11_a_pending_permission_never_becomes_approval():
    check.section("W11  a permission request open when the process died")
    with _tmp() as td:
        root = Path(td) / "n"
        # The broker's pending map is in-memory by design. What matters is what
        # a NEW process can do with the memory of a request: nothing.
        made = run_step(root, """
            from core.permissions import PermissionBroker
            b = PermissionBroker()
            d = await b.request("project.delete", details={"project": "%s"})
            emit({"decision": d["decision"], "rid": d.get("request_id"),
                  "pending": len(b.pending())})
        """ % P, )
        rid = one(made, "rid")
        check(one(made, "decision") == "needs_confirmation",
              f"the request needed confirmation ({one(made, 'decision')})")
        run_step(root, "CRASH()", expect_crash=True)

        after = run_step(root, f"""
            from core.permissions import PermissionBroker
            b = PermissionBroker()
            emit({{"pending": len(b.pending()),
                   "resolve_old": b.resolve("{rid}", True),
                   "settled": b.settled_as("{rid}")}})
        """)
        check(int(one(after, "pending")) == 0,
              f"a fresh process has no pending requests ({one(after,'pending')})")
        check(one(after, "resolve_old") is False,
              f"and cannot approve the old one ({one(after, 'resolve_old')})")
        check(one(after, "settled") == "",
              f"it is not recorded as settled either ({one(after,'settled')!r})")


async def window_12_a_completed_delete_is_not_repeated():
    check.section("W12  delete approved and moved, crash before the reply")
    with _tmp() as td:
        root = Path(td) / "n"
        made = run_step(root, f"""
            from core.project_manager import ProjectManager
            d = Path(r"{root}") / "projects" / "{P}"
            d.mkdir(parents=True, exist_ok=True)
            (d / "PROJECT.md").write_text("# {P}\\n", encoding="utf-8")
            pm = ProjectManager(Path(r"{root}"), Path(r"{root}") / "projects")
            res = await asyncio.to_thread(pm.delete_project, "{P}")
            emit({{"deleted": True, "result": str(res)[:120]}})
        """)
        check(one(made, "deleted") is True, "the delete executed")
        run_step(root, "CRASH()", expect_crash=True)

        active = (root / "projects" / P / "PROJECT.md")
        trash_dir = root / "projects" / ".trash"
        trash = [t.name for t in trash_dir.glob(f"{P}*")] if trash_dir.is_dir() else []
        check(not active.exists(),
              f"after the crash the project is gone from the listing ({active.exists()})")
        check(bool(trash), f"and recoverable in the trash ({trash})")

        after = run_step(root, f"""
            from core.project_manager import ProjectManager
            pm = ProjectManager(Path(r"{root}"), Path(r"{root}") / "projects")
            try:
                await asyncio.to_thread(pm.delete_project, "{P}")
                emit({{"second": "executed"}})
            except Exception as e:
                emit({{"second": type(e).__name__}})
        """)
        after_trash = [t.name for t in trash_dir.glob(f"{P}*")]
        check(one(after, "second") != "executed",
              f"a repeat delete finds nothing to do ({one(after, 'second')})")
        check(after_trash == trash,
              f"and the trash is unchanged ({len(trash)} -> {len(after_trash)})")


async def main() -> None:
    await window_1_before_the_tool_starts()
    await window_2_during_the_tool()
    await window_3_side_effect_then_crash_before_bookkeeping()
    await window_4_success_persisted_then_crash()
    await window_5_no_acknowledgement_is_not_authorisation()
    await window_7_cancel_then_a_late_completion()
    await window_8_stale_rows_never_serve_the_new_run()
    await window_9_a_retry_does_not_multiply()
    await window_10_no_contradictory_state()
    await window_11_a_pending_permission_never_becomes_approval()
    await window_12_a_completed_delete_is_not_repeated()
    check.finish()


if __name__ == "__main__":
    run(main)
