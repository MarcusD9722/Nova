"""A person talking to Nova while Nova is stopping her own work (§18).

Recovery is not only something that happens at startup, before anyone can
speak to Nova. `POST /autonomy/stop` runs the SAME routine while the app is
live and serving `/chat` - it is the kill switch, and the moment a person is
most likely to press it is the moment they are also asking what is going on.

So two things run over the same rows at once: a foreground turn reading the
work to describe it, and a background sweep terminating that work. What must
not happen is a sentence that was never true: "it's running" about a step the
sweep had already stopped, or "it finished" about one whose tool never ran.

WHAT IS AND IS NOT CLAIMED HERE. Real interleaving cannot be dictated, only
observed, so this runs the collision many times and checks a property that
holds for every ordering: whatever the answer was given must be a state the
row ACTUALLY WAS IN - the one before the sweep or the one after it, never a
third. The test reports how the orderings actually fell rather than asserting
one, and it fails if the collision never happened at all, because a property
that was never exercised has not been tested.

Run:  venv\\Scripts\\python.exe tests\\test_foreground_background_s13c.py
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

P = "flappy-bird"
ROUNDS = 20

#: Every pair of (lifecycle status, work outcome) that is a coherent statement
#: about a step. Anything outside this is a row describing something that
#: cannot have happened, whoever wrote it and whenever.
VALID = {
    ("queued", "pending"), ("running", "pending"), ("blocked", "pending"),
    ("done", "succeeded"), ("done", "unknown"),
    ("failed", "failed"), ("failed", "unknown"),
    ("cancelled", "never_started"), ("cancelled", "unknown"),
    ("superseded", "never_started"), ("superseded", "unknown"),
    ("superseded", "succeeded"), ("superseded", "failed"),
}

#: Pick the ANSWER prompt out of a turn by what it is, never by where it sits.
ASK = '''
    async def ask(message):
        before = len(nova.llm.prompts)
        r = await nova.http.post("/chat", json={"message": message})
        new = nova.llm.prompts[before:]
        answers = [p for p in new
                   if "You are Nova" in p and "agent brain for Nova" not in p]
        return answers[-1] if answers else ""
'''


def _tmp():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


def _bad_pairs(rows) -> list:
    return [r for r in rows if (r[1], r[2]) not in VALID]


async def test_a_the_first_words_after_a_restart_are_already_recovered():
    check.section("§18 nobody is served a half-recovered world")
    with _tmp() as td:
        root = Path(td) / "n"
        made = run_step(root, f"""
            goal = await mem.create_goal(project_name="{P}", title="pause menu",
                                         objective="o", success_criteria="c")
            for tool in ("code.write", "code.test"):
                await mem.enqueue_goal_task(goal_id=goal, project_name="{P}",
                                            tool_name=tool, args={{}})
            claimed = await mem.claim_next_goal_task()
            emit({{"goal": str(goal), "claimed": str(claimed["task_id"])}})
        """)
        check(one(made, "claimed"), "a step is left mid-flight and one queued")

        run_step(root, "CRASH()", expect_crash=True)

        # A brand-new process, and the VERY FIRST thing that happens in it is a
        # person asking. If the gate did not hold, this turn could be handed
        # rows the sweep had not reached yet.
        seen = run_step(root, ASK + '''
    ground = await ask("What happened?")
    rows = await nova.memory.list_goal_tasks(limit=50)
    claim = await nova.memory.claim_next_goal_task()
    emit({"ground": ground,
          "rows": sorted((str(t["task_id"]), t["status"], t["outcome"])
                         for t in rows),
          "claimable": None if claim is None else str(claim["task_id"])})
''', full=True)
        rows = one(seen, "rows") or []
        ground = one(seen, "ground") or ""
        check(len(rows) == 2 and not any(r[1] in ("queued", "running")
                                        for r in rows),
              f"both steps are there and neither is still in flight ({rows})")
        check(not _bad_pairs(rows),
              f"and no row describes an impossible state ({_bad_pairs(rows)})")
        work_block = ground.split("The work you are actually tracking", 1)
        step_lines = [l.strip() for l in (work_block[1] if len(work_block) > 1
                                          else "").splitlines()
                      if l.strip().startswith("- code.")]
        check(len(work_block) == 2 and step_lines,
              f"the answer really was handed the work record ({step_lines})")
        # The STATUS token, not the line: the honest explanation of an
        # interrupted step contains the word "running", and matching prose
        # instead of state is how a check like this passes for the wrong reason.
        told = [l.split(":", 1)[1].strip().split(" ", 1)[0] for l in step_lines]
        check(not any(t in ("running", "queued") for t in told),
              f"so it cannot be told work is under way ({told})")
        check(set(told) <= {"failed", "cancelled", "done", "superseded"},
              f"every step it names is finished with ({told})")
        check(one(seen, "claimable") is None,
              f"and asking a question started nothing ({one(seen, 'claimable')})")


async def test_b_the_kill_switch_during_a_question():
    check.section(f"§18 /autonomy/stop and a question, {ROUNDS} times over")
    with _tmp() as td:
        root = Path(td) / "n"
        out = run_step(root, ASK + f'''
    import asyncio

    results = []
    for i in range({ROUNDS}):
        goal = await nova.memory.create_goal(
            project_name="{P}", title=f"round {{i}}", objective="o",
            success_criteria="c")
        await nova.memory.enqueue_goal_task(
            goal_id=goal, project_name="{P}", tool_name=f"code.step{{i}}",
            args={{}})
        # Claim THIS goal's step, by identity - a bare claim takes the oldest
        # runnable row anywhere, which in round 7 is not the one we made.
        claimed = None
        for _ in range(50):
            c = await nova.memory.claim_next_goal_task()
            if c is None:
                break
            if str(c["goal_id"]) == str(goal):
                claimed = c
                break
        if claimed is None:
            continue
        tid = str(claimed["task_id"])
        gen = int(claimed["generation"])

        def row_of(rows):
            for t in rows:
                if str(t["task_id"]) == tid:
                    return [t["status"], t["outcome"]]
            return None

        pre = row_of(await nova.memory.list_goal_tasks(goal_id=str(goal)))

        # THE COLLISION. Neither is given a head start.
        stop_call = nova.http.post("/autonomy/stop")
        ask_call = ask(f"What is the status of round {{i}}?")
        stopped, ground = await asyncio.gather(stop_call, ask_call)

        post = row_of(await nova.memory.list_goal_tasks(goal_id=str(goal)))
        # The worker that was holding it reports afterwards, as it would.
        verdict = await nova.memory.complete_goal_task(
            task_id=tid, status="done", result={{"ok": True}}, error="",
            expected_generation=gen)
        final = row_of(await nova.memory.list_goal_tasks(goal_id=str(goal)))
        described = ""
        for line in ground.splitlines():
            if f"code.step{{i}}:" in line:
                described = line.split(f"code.step{{i}}:", 1)[1].strip()
                break
        results.append({{"pre": pre, "post": post, "final": final,
                         "described": described, "verdict": verdict,
                         "stop_ok": stopped.status_code}})

    claim = await nova.memory.claim_next_goal_task()
    rows = await nova.memory.list_goal_tasks(limit=400)
    emit({{"results": results,
           "left_runnable": None if claim is None else str(claim["task_id"]),
           "all_rows": sorted((str(t["task_id"]), t["status"], t["outcome"])
                              for t in rows)}})
''', full=True, timeout=900)

        results = one(out, "results") or []
        check(len(results) == ROUNDS,
              f"every round ran the collision ({len(results)}/{ROUNDS})")
        check(all(r["stop_ok"] == 200 for r in results),
              "the kill switch answered every time")

        # THE PROPERTY. What the answer was told must be a state the row was
        # really in - before the sweep, or after it. Never a third thing.
        wrong = [r for r in results if r["described"]
                 and not r["described"].startswith(str(r["pre"][0]))
                 and not r["described"].startswith(str(r["post"][0]))]
        check(not wrong,
              f"no answer was given a state the step was never in ({wrong[:2]})")

        # How the orderings actually fell. Reported, not assumed - and stated
        # plainly rather than implied: `/autonomy/stop` is a single sweeping
        # UPDATE while the answering turn does considerably more before it
        # reads anything, so in practice the sweep wins every time. That makes
        # the post-sweep side the one genuinely exercised here, and saying so
        # is worth more than a claim that both were.
        saw_pre = sum(1 for r in results
                      if r["pre"][0] != r["post"][0]
                      and r["described"].startswith(str(r["pre"][0])))
        saw_post = sum(1 for r in results
                       if r["pre"][0] != r["post"][0]
                       and r["described"].startswith(str(r["post"][0])))
        described = sum(1 for r in results if r["described"])
        check(described == ROUNDS,
              f"the step was described in every round ({described}/{ROUNDS})")
        check(saw_pre + saw_post == ROUNDS,
              f"and every description was one of the two real states "
              f"(before {saw_pre}, after {saw_post}, of {ROUNDS})")
        print(f"      (observed: the sweep landed first in {saw_post}/{ROUNDS} "
              f"rounds, the read first in {saw_pre})")

        # The sweep's own guarantees, unaffected by anyone talking.
        stopped_wrong = [r for r in results if r["post"][0] in ("queued", "running")]
        check(not stopped_wrong,
              f"the sweep terminated every step it swept ({stopped_wrong[:2]})")
        check(all(r["post"][1] in ("unknown", "never_started") for r in results),
              f"and never claimed to know how an interrupted step went "
              f"({sorted({tuple(r['post']) for r in results})})")
        late = [r for r in results if r["verdict"] not in ("ignored", "superseded")]
        check(not late,
              f"the worker's later report is refused every time "
              f"({[r['verdict'] for r in late][:3]})")
        check(all(r["final"][0] != "done" for r in results),
              f"so nothing the user stopped is recorded as finished "
              f"({[r['final'] for r in results if r['final'][0] == 'done'][:2]})")

        check(one(out, "left_runnable") is None,
              f"nothing was left runnable by any of it ({one(out, 'left_runnable')})")
        bad = _bad_pairs(one(out, "all_rows") or [])
        check(not bad, f"and no impossible row survived {ROUNDS} collisions ({bad[:3]})")


async def test_c_a_question_during_the_sweep_starts_no_work():
    check.section("§18 asking is not doing")
    with _tmp() as td:
        root = Path(td) / "n"
        out = run_step(root, ASK + f'''
    import asyncio

    goal = await nova.memory.create_goal(project_name="{P}", title="menu",
                                         objective="o", success_criteria="c")
    for tool in ("code.write", "code.test", "code.ship"):
        await nova.memory.enqueue_goal_task(goal_id=goal, project_name="{P}",
                                            tool_name=tool, args={{}})

    before = await nova.memory.list_goal_tasks(goal_id=str(goal))
    stop_call = nova.http.post("/autonomy/stop")
    asks = [ask(q) for q in ("What's left to do?", "Is anything running?",
                             "Can you carry on with the pause menu?",
                             "What happened to the pause menu?")]
    await asyncio.gather(stop_call, *asks)
    after = await nova.memory.list_goal_tasks(goal_id=str(goal))
    claim = await nova.memory.claim_next_goal_task()
    g = await nova.memory.get_goal(goal_id=goal)
    emit({{"before": len(before), "after": len(after),
           "statuses": sorted({{t["status"] for t in after}}),
           "goal_status": g["status"], "goal_gen": int(g["generation"]),
           "claimable": None if claim is None else str(claim["task_id"])}})
''', full=True, timeout=600)

        check(one(out, "before") == one(out, "after") == 3,
              f"four questions during the sweep created no new work "
              f"({one(out, 'before')} -> {one(out, 'after')})")
        check(one(out, "statuses") == ["cancelled"],
              f"and all of it was stopped, none of it resumed "
              f"({one(out, 'statuses')})")
        check(one(out, "claimable") is None,
              f"nothing became runnable ({one(out, 'claimable')})")
        # Asking to carry on is a REQUEST, not a resume: only an explicit
        # resume opens a new run, and the sweep left the goal paused.
        check(one(out, "goal_status") in ("paused", "active"),
              f"the goal is left in a state a person can act on "
              f"({one(out, 'goal_status')})")


async def main() -> None:
    await test_a_the_first_words_after_a_restart_are_already_recovered()
    await test_b_the_kill_switch_during_a_question()
    await test_c_a_question_during_the_sweep_starts_no_work()
    check.finish()


if __name__ == "__main__":
    run(main)
