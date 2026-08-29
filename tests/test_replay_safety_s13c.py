"""A restart must not do anything twice (Stage 13C, §8/§9/§14).

REPLAY SAFETY is the property that a restart is not a second chance to run
work that already ran. Every case here restarts a durable store repeatedly and
asserts that the answer stops changing: the same rows, the same counts, the
same outcomes, no matter how many times Nova is killed and started.

DEATH BY A THOUSAND RESTARTS (§14) is the same idea taken to its limit - one
store, restarted many times at different points, with the full invariant set
checked after EVERY boot rather than only at the end. A defect that needs three
restarts to appear is invisible to a test that restarts once.

PAUSE / CANCEL / RESUME ACROSS RESTART (§9) is where replay safety and
lifecycle meet: a paused goal must resume only the work that is still valid, a
cancelled one must not resurrect at all, and neither may multiply.

Every observation is read by a FRESH interpreter from the durable store.

Run:  venv\\Scripts\\python.exe tests\\test_replay_safety_s13c.py
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

#: Boot recovery plus the whole authoritative picture, as any reader would see
#: it. Returned as sorted tuples so two boots can be compared directly.
BOOT = """
    rec = await mem.cancel_pending_background_work()
    tasks = await mem.list_goal_tasks(limit=200)
    autos = await mem.list_tasks(limit=100)
    goals = await mem.list_goals(limit=50)
    claim = await mem.claim_next_goal_task()
    aclaim = await mem.claim_next_task()
    emit({"recovery": rec,
          "tasks": sorted((str(t["task_id"]), t["tool_name"], t["status"],
                           t["outcome"], t["generation"], t["attempts"])
                          for t in tasks),
          "autos": sorted((str(t["task_id"]), t["status"], t["outcome"])
                          for t in autos),
          "goals": sorted((str(g["goal_id"]), g["status"], g["generation"])
                          for g in goals),
          "claimable": None if claim is None else str(claim["task_id"]),
          "auto_claimable": None if aclaim is None else str(aclaim["task_id"])})
"""


def _tmp():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


def _invariants(boot: list, label: str) -> list[str]:
    """Everything that must be true after ANY boot. Returns violations.

    `boot` is what the child emitted, so every field is looked up by NAME.
    Reading it positionally would make the checks depend on emission order,
    which is exactly the attribution mistake this campaign keeps finding.
    """
    bad = []
    forbidden = {("queued", "succeeded"), ("running", "never_started"),
                 ("done", "pending"), ("done", "failed"),
                 ("cancelled", "succeeded"), ("blocked", "succeeded")}
    for t in one(boot, "tasks") or []:
        if (t[2], t[3]) in forbidden:
            bad.append(f"{label}: impossible pair {t[2]}/{t[3]} on {t[0][:8]}")
        if t[2] in ("queued", "running"):
            bad.append(f"{label}: {t[0][:8]} is {t[2]} AFTER boot recovery")
    for a in one(boot, "autos") or []:
        if (a[1], a[2]) in forbidden:
            bad.append(f"{label}: impossible pair {a[1]}/{a[2]} on {a[0][:8]}")
    for g in one(boot, "goals") or []:
        if g[1] == "active":
            bad.append(f"{label}: goal {g[0][:8]} left ACTIVE by a restart")
    return bad


async def test_a_completed_work_is_never_replayed():
    check.section("A. a completed step, restarted three times")
    with _tmp() as td:
        root = Path(td) / "n"
        run_step(root, f"""
            goal = await mem.create_goal(project_name="{P}", title="ship it",
                                         objective="o", success_criteria="c")
            await mem.enqueue_goal_task(goal_id=goal, project_name="{P}",
                                        tool_name="deploy.push", args={{}})
            c = await mem.claim_next_goal_task()
            await mem.complete_goal_task(task_id=str(c["task_id"]), status="done",
                                         result={{"ok": True}}, error="",
                                         expected_generation=int(c["generation"]))
            emit({{"task": str(c["task_id"])}})
        """)
        boots = [one(run_step(root, BOOT), "tasks") for _ in range(3)]
        claims = [one(run_step(root, BOOT), "claimable") for _ in range(3)]
        check(boots[0] == boots[1] == boots[2],
              f"three boots see identical rows ({len(boots[0])} rows)")
        check(boots[0][0][2] == "done" and boots[0][0][3] == "succeeded",
              f"still done/succeeded ({boots[0][0][2]}/{boots[0][0][3]})")
        check(len(claims) == 3 and all(c is None for c in claims),
              f"and never offered for execution again, at any of the three "
              f"boots ({claims})")


async def test_b_one_retry_stays_one_retry():
    check.section("B. a scheduled retry, restarted twice")
    with _tmp() as td:
        root = Path(td) / "n"
        run_step(root, f"""
            goal = await mem.create_goal(project_name="{P}", title="flaky",
                                         objective="o", success_criteria="c")
            await mem.enqueue_goal_task(goal_id=goal, project_name="{P}",
                                        tool_name="deploy.push", args={{}})
            c = await mem.claim_next_goal_task()
            await mem.bump_goal_task_attempt(
                task_id=str(c["task_id"]), attempts=1,
                run_after_iso="2000-01-01T00:00:00+00:00", error="blip",
                expected_generation=int(c["generation"]))
            emit({{"task": str(c["task_id"])}})
        """)
        first, second = run_step(root, BOOT), run_step(root, BOOT)
        check(len(one(first, "tasks")) == 1,
              f"one row ({len(one(first, 'tasks'))})")
        check(one(first, "tasks") == one(second, "tasks"),
              "and the second restart changes nothing")
        attempts = {t[5] for t in one(second, "tasks")}
        check(attempts == {1}, f"the attempt count did not inflate ({attempts})")
        check(one(second, "claimable") is None,
              "exactly zero retries become runnable unasked")


async def test_c_boot_recovery_creates_no_replacement_work():
    check.section("C. repeated boot recovery invents nothing")
    with _tmp() as td:
        root = Path(td) / "n"
        run_step(root, f"""
            for i in range(3):
                goal = await mem.create_goal(project_name="{P}", title=f"g{{i}}",
                                             objective="o", success_criteria="c")
                await mem.enqueue_goal_task(goal_id=goal, project_name="{P}",
                                            tool_name="code.write", args={{}})
            emit({{"seeded": 3}})
        """)
        counts = []
        for _ in range(5):
            b = run_step(root, BOOT)
            counts.append((len(one(b, "tasks")), len(one(b, "goals"))))
        check(len(set(counts)) == 1,
              f"five boots, always the same row counts ({set(counts)})")
        check(counts[0] == (3, 3), f"and no duplicates ({counts[0]})")


async def test_d_a_blocked_question_survives_repeated_restarts():
    check.section("D. one blocked task, restarted five times")
    with _tmp() as td:
        root = Path(td) / "n"
        run_step(root, f"""
            tid = str(await mem.enqueue_task(
                title="pick the overlay", details="d", project_name="{P}",
                initiated_by_user=True))
            await mem.claim_next_task()
            await mem.mark_task_blocked(task_id=tid,
                                        question="Dark overlay or a blur?")
            emit({{"task": tid}})
        """)
        seen = []
        for _ in range(5):
            b = run_step(root, BOOT)
            seen.append(one(b, "autos"))
        check(all(s == seen[0] for s in seen),
              f"five boots, one unchanged blocked task ({len(seen[0])})")
        check(seen[0] and seen[0][0][1] == "blocked",
              f"still blocked ({seen[0][0][1] if seen[0] else None})")
        detail = run_step(root, """
            rows = await mem.list_tasks(status="blocked", limit=20)
            emit({"n": len(rows),
                  "q": [str(r.get("last_error") or "") for r in rows]})
        """)
        check(int(one(detail, "n")) == 1,
              f"exactly one, never duplicated ({one(detail, 'n')})")
        check(any("Dark overlay" in q for q in one(detail, "q")),
              f"and the question is intact ({one(detail, 'q')})")


async def test_e_f_g_paused_cancelled_completed_do_not_drift():
    check.section("E/F/G. paused, cancelled and completed goals x5 restarts")
    with _tmp() as td:
        root = Path(td) / "n"
        run_step(root, f"""
            paused = await mem.create_goal(project_name="{P}", title="paused",
                                           objective="o", success_criteria="c")
            await mem.update_goal_status(goal_id=paused, status="paused")
            killed = await mem.create_goal(project_name="{P}", title="killed",
                                           objective="o", success_criteria="c")
            await mem.cancel_goal(goal_id=killed)
            done = await mem.create_goal(project_name="{P}", title="done",
                                         objective="o", success_criteria="c")
            await mem.update_goal_status(goal_id=done, status="completed")
            emit({{"seeded": True}})
        """)
        snaps = []
        for _ in range(5):
            snaps.append(one(run_step(root, BOOT), "goals"))
        check(all(s == snaps[0] for s in snaps),
              f"five boots, identical goal states ({snaps[0]})")
        statuses = sorted(g[1] for g in snaps[0])
        check(statuses == ["cancelled", "completed", "paused"],
              f"nothing drifted between them ({statuses})")
        gens = {g[0]: g[2] for g in snaps[0]}
        again = {g[0]: g[2] for g in snaps[-1]}
        check(gens == again, "and no revision inflated across restarts")


async def test_h_superseded_rows_never_become_current():
    check.section("H. superseded rows, restarted five times")
    with _tmp() as td:
        root = Path(td) / "n"
        run_step(root, f"""
            goal = await mem.create_goal(project_name="{P}", title="revised",
                                         objective="o", success_criteria="c")
            await mem.enqueue_goal_task(goal_id=goal, project_name="{P}",
                                        tool_name="deploy.push", args={{}})
            c = await mem.claim_next_goal_task()
            await mem.cancel_goal(goal_id=goal)
            await mem.complete_goal_task(task_id=str(c["task_id"]), status="done",
                                         result={{"ok": True}}, error="",
                                         expected_generation=int(c["generation"]))
            emit({{"task": str(c["task_id"])}})
        """)
        for i in range(5):
            b = run_step(root, BOOT)
            rows = one(b, "tasks")
            bad = _invariants(b, f"boot {i}")
            check(not bad, f"boot {i}: invariants hold ({bad or 'clean'})")
            check(rows[0][2] == "superseded" and rows[0][3] == "succeeded",
                  f"boot {i}: still superseded/succeeded ({rows[0][2]}/{rows[0][3]})")
            check(one(b, "claimable") is None,
                  f"boot {i}: never becomes current work")


async def test_pause_restart_resume_replays_nothing():
    check.section("§9 pause -> restart -> restart -> resume")
    with _tmp() as td:
        root = Path(td) / "n"
        made = run_step(root, f"""
            goal = await mem.create_goal(project_name="{P}", title="two steps",
                                         objective="o", success_criteria="c")
            for tool in ("code.write", "code.test"):
                await mem.enqueue_goal_task(goal_id=goal, project_name="{P}",
                                            tool_name=tool, args={{}})
            a = await mem.claim_next_goal_task()
            await mem.complete_goal_task(task_id=str(a["task_id"]), status="done",
                                         result={{"ok": True}}, error="",
                                         expected_generation=int(a["generation"]))
            await mem.update_goal_status(goal_id=goal, status="paused")
            emit({{"goal": str(goal), "done_task": str(a["task_id"])}})
        """)
        gid, done_task = one(made, "goal"), one(made, "done_task")

        run_step(root, BOOT)
        run_step(root, BOOT)

        after = run_step(root, scenario(f"""
            await mem.cancel_pending_background_work()
            await mem.resume_goal(goal_id=__import__("uuid").UUID("{gid}"))
            g = await mem.get_goal(goal_id=__import__("uuid").UUID("{gid}"))
            rows = await mem.list_goal_tasks(goal_id="{gid}", limit=20)
            claim = await mem.claim_next_goal_task()
            emit({{"status": g["status"], "gen": g["generation"],
                   "rows": [(str(t["task_id"]), t["tool_name"], t["status"],
                             t["outcome"]) for t in rows],
                   "claim": None if claim is None else
                            [str(claim["task_id"]), claim["tool_name"]]}})
        """))
        rows = one(after, "rows")
        completed = [r for r in rows if r[0] == done_task]
        check(one(after, "status") == "active",
              f"resume reactivates the goal ({one(after, 'status')})")
        check(completed and completed[0][2] == "done",
              f"the finished step is STILL done ({completed})")
        claim = one(after, "claim")
        check(claim is None or claim[0] != done_task,
              f"and resume does not hand it back out ({claim})")
        check(claim is None or claim[1] == "__decide__",
              f"what resumes is a fresh decision ({claim})")


async def test_cancel_restart_and_bare_continue_do_not_resurrect():
    check.section("§9 cancel -> restart x3 -> nothing resurrects")
    with _tmp() as td:
        root = Path(td) / "n"
        made = run_step(root, f"""
            goal = await mem.create_goal(project_name="{P}", title="cancelled work",
                                         objective="o", success_criteria="c")
            for tool in ("code.write", "code.test"):
                await mem.enqueue_goal_task(goal_id=goal, project_name="{P}",
                                            tool_name=tool, args={{}})
            c = await mem.claim_next_goal_task()
            await mem.cancel_goal(goal_id=goal)
            g = await mem.get_goal(goal_id=goal)
            emit({{"goal": str(goal), "gen": g["generation"]}})
        """)
        gid, gen = one(made, "goal"), one(made, "gen")

        for i in range(3):
            b = run_step(root, BOOT)
            goals = {g[0]: (g[1], g[2]) for g in one(b, "goals")}
            check(goals[gid][0] == "cancelled",
                  f"boot {i}: still cancelled ({goals[gid]})")
            check(goals[gid][1] == gen,
                  f"boot {i}: revision unmoved ({goals[gid][1]} vs {gen})")
            check(one(b, "claimable") is None,
                  f"boot {i}: nothing claimable")
            bad = _invariants(b, f"boot {i}")
            check(not bad, f"boot {i}: invariants hold ({bad or 'clean'})")

        # A bare "continue" is not a new instruction: resuming a cancelled goal
        # is a deliberate user action, and even then it must not re-run the
        # work that was cancelled.
        after = run_step(root, f"""
            rows = await mem.list_goal_tasks(goal_id="{gid}", limit=20)
            emit({{"statuses": sorted(t["status"] for t in rows)}})
        """)
        check("done" not in one(after, "statuses"),
              f"no cancelled step ever completed ({one(after, 'statuses')})")


async def test_death_by_a_thousand_restarts():
    check.section("§14 twelve restarts on one store, checked every boot")
    with _tmp() as td:
        root = Path(td) / "n"
        run_step(root, f"""
            a = await mem.create_goal(project_name="{P}", title="A",
                                      objective="o", success_criteria="c")
            await mem.enqueue_goal_task(goal_id=a, project_name="{P}",
                                        tool_name="code.write", args={{}})
            c = await mem.claim_next_goal_task()
            await mem.complete_goal_task(task_id=str(c["task_id"]), status="done",
                                         result={{"ok": True}}, error="",
                                         expected_generation=int(c["generation"]))
            b = await mem.create_goal(project_name="quickcalc", title="B",
                                      objective="o", success_criteria="c")
            await mem.enqueue_goal_task(goal_id=b, project_name="quickcalc",
                                        tool_name="code.write", args={{}})
            d = await mem.claim_next_goal_task()
            await mem.complete_goal_task(task_id=str(d["task_id"]), status="failed",
                                         result={{}}, error="disk full",
                                         expected_generation=int(d["generation"]))
            k = await mem.create_goal(project_name="{P}", title="killed",
                                      objective="o", success_criteria="c")
            await mem.enqueue_goal_task(goal_id=k, project_name="{P}",
                                        tool_name="code.write", args={{}})
            await mem.cancel_goal(goal_id=k)
            blocked = str(await mem.enqueue_task(
                title="a question", details="d", project_name="{P}",
                initiated_by_user=True))
            await mem.claim_next_task()
            await mem.mark_task_blocked(task_id=blocked, question="Which one?")
            emit({{"seeded": True}})
        """)

        baseline = None
        drifted = []
        for i in range(12):
            b = run_step(root, BOOT)
            bad = _invariants(b, f"boot {i}")
            if bad:
                drifted.extend(bad)
            snap = (one(b, "tasks"), one(b, "autos"), one(b, "goals"))
            if baseline is None:
                baseline = snap
            elif snap != baseline:
                drifted.append(f"boot {i}: state changed from the first boot")
        check(not drifted, f"twelve boots, no drift and no violations "
                           f"({drifted[:3] or 'clean'})")
        check(baseline is not None and len(baseline[0]) == 3,
              f"three goal tasks throughout ({len(baseline[0]) if baseline else 0})")
        outcomes = sorted(t[3] for t in baseline[0])
        check(outcomes == ["failed", "never_started", "succeeded"],
              f"and every outcome held its value ({outcomes})")
        check(baseline[1] and baseline[1][0][1] == "blocked",
              f"the question is still waiting ({baseline[1]})")


async def main() -> None:
    await test_a_completed_work_is_never_replayed()
    await test_b_one_retry_stays_one_retry()
    await test_c_boot_recovery_creates_no_replacement_work()
    await test_d_a_blocked_question_survives_repeated_restarts()
    await test_e_f_g_paused_cancelled_completed_do_not_drift()
    await test_h_superseded_rows_never_become_current()
    await test_pause_restart_resume_replays_nothing()
    await test_cancel_restart_and_bare_continue_do_not_resurrect()
    await test_death_by_a_thousand_restarts()
    check.finish()


if __name__ == "__main__":
    run(main)
