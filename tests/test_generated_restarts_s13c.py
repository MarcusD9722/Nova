"""Sequences nobody thought of, across real process boundaries (§19).

Hand-written restart tests check the situations someone imagined. This one
generates them: a seeded random walk over the actions that actually happen to
a goal - enqueue, claim, finish, fail, cancel, resume, a stale worker
reporting, a progress line - with RESTART_CLEAN and RESTART_CRASH as ordinary
actions in the alphabet rather than something arranged around.

A RESTART IS A REAL RESTART. A sequence is cut into LIVES at every restart,
and each life runs in its own interpreter against the same directory. A clean
restart ends its life normally; a crashed one calls CRASH(), so nothing drains
and only what reached the database survives. Nothing carries over in memory,
because after a real restart nothing does.

WHAT IS CHECKED. Not a full replica of the state machine - a second
implementation of the thing under test is mostly a way of testing the second
implementation. What is checked is what must be true of ANY ordering, read
back from authoritative rows after every life:

  * every (status, outcome) pair is a coherent statement;
  * a step nobody ever claimed never reports that its work succeeded;
  * a completion the fence refused never leaves the step finished;
  * a goal's revision number never goes backwards, and no step claims to
    belong to a revision its goal has not reached;
  * a life that begins after a restart finds nothing still in flight;
  * and no step ever disappears.

The actions of each life are appended to a log ON DISK as they happen, flushed
per line, so a crashed life still leaves an account of what it had done - the
same position Nova herself is in afterwards.

On failure the seed and the whole action history are printed, so any failure
here is replayable exactly.

Run:      venv\\Scripts\\python.exe tests\\test_generated_restarts_s13c.py
Soak:     NOVA_S13C_SEQS=1000 venv\\Scripts\\python.exe tests\\...
One seed: NOVA_S13C_SEED=41 NOVA_S13C_SEQS=1 venv\\Scripts\\python.exe tests\\...
"""

from __future__ import annotations

import json
import os
import random
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")
os.environ.setdefault("NOVA_IT_WATCHDOG_S", "3600")

from harness import Checks, run  # noqa: E402

from restart_harness import one, run_step  # noqa: E402

check = Checks()

SEQUENCES = int(os.getenv("NOVA_S13C_SEQS", "300"))
BASE_SEED = int(os.getenv("NOVA_S13C_SEED", "0"))
WORKERS = int(os.getenv("NOVA_S13C_WORKERS", "6"))
P = "flappy-bird"

VALID_PAIRS = {
    ("queued", "pending"), ("running", "pending"), ("blocked", "pending"),
    ("done", "succeeded"), ("done", "unknown"),
    ("failed", "failed"), ("failed", "unknown"),
    ("cancelled", "never_started"), ("cancelled", "unknown"),
    ("superseded", "never_started"), ("superseded", "unknown"),
    ("superseded", "succeeded"), ("superseded", "failed"),
}

#: The alphabet. Restarts are first-class members of it, not scaffolding.
ACTIONS = ["CREATE_GOAL", "ENQUEUE", "ENQUEUE", "CLAIM", "CLAIM",
           "COMPLETE_OK", "COMPLETE_FAIL", "STALE_COMPLETE",
           "CLAIM_THEN_STALE", "CLAIM_THEN_STALE",
           "CANCEL", "RESUME", "PROGRESS",
           "RESTART_CLEAN", "RESTART_CRASH"]

#: Run once at the top of every life. Loads what the previous lives left - on
#: disk, since nothing else survives - and recovers, exactly as a boot does.
_LIFE_PREFIX = """
    import json as _json
    LOG = Path(r"@ROOT@") / "steps.jsonl"

    def log(d):
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(_json.dumps(d) + chr(10))
            fh.flush()

    history = []
    if LOG.exists():
        for _line in LOG.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if _line:
                try:
                    history.append(_json.loads(_line))
                except ValueError:
                    pass

    recovered = await mem.cancel_pending_background_work()

    async def state():
        gs = await mem.list_goals(limit=100)
        ts = await mem.list_goal_tasks(limit=400)
        return gs, ts

    async def snapshot(tag):
        gs, ts = await state()
        pes = []
        for g in gs:
            pes.extend(await mem.list_progress_events(goal_id=str(g["goal_id"]),
                                                      limit=100))
        return {"tag": tag,
                "goals": sorted((str(g["goal_id"]), g["status"],
                                 int(g["generation"])) for g in gs),
                "tasks": sorted((str(t["task_id"]), str(t["goal_id"]),
                                 t["status"], t["outcome"],
                                 int(t["generation"])) for t in ts),
                "progress": sorted((str(e.get("goal_id")),
                                    None if e.get("generation") is None
                                    else int(e["generation"])) for e in pes)}

    emit({"at_boot": await snapshot("at_boot"), "recovered": recovered})

    def pick_goal(gs, i):
        return None if not gs else gs[i % len(gs)]

    def claimed_tasks():
        return [h for h in history if h.get("a") == "claim" and h.get("task")]

    def settled_tasks():
        return {h["task"] for h in history
                if h.get("a") in ("complete", "stale") and h.get("applied")}
"""

_BODIES = {
    "CREATE_GOAL": """
        _g = await mem.create_goal(project_name=PROJ, title=f"goal {IDX}",
                                   objective="o", success_criteria="c")
        log({"a": "create_goal", "goal": str(_g)})
    """,
    "ENQUEUE": """
        _gs, _ = await state()
        _g = pick_goal(_gs, IDX)
        if _g is not None:
            _t = await mem.enqueue_goal_task(
                goal_id=_g["goal_id"], project_name=PROJ,
                tool_name=f"code.s{IDX}", args={})
            log({"a": "enqueue", "goal": str(_g["goal_id"]),
                 "task": None if _t is None else str(_t),
                 "goal_gen": int(_g["generation"])})
    """,
    "CLAIM": """
        _c = await mem.claim_next_goal_task()
        if _c is not None:
            log({"a": "claim", "task": str(_c["task_id"]),
                 "goal": str(_c["goal_id"]), "gen": int(_c["generation"])})
    """,
    "COMPLETE_OK": """
        _open = [h for h in claimed_tasks() if h["task"] not in settled_tasks()]
        if _open:
            _h = _open[IDX % len(_open)]
            _v = await mem.complete_goal_task(
                task_id=_h["task"], status="done", result={"ok": True},
                error="", expected_generation=int(_h["gen"]))
            log({"a": "complete", "task": _h["task"], "want": "done",
                 "gen": int(_h["gen"]), "verdict": _v,
                 "applied": _v not in ("ignored", "superseded")})
    """,
    "COMPLETE_FAIL": """
        _open = [h for h in claimed_tasks() if h["task"] not in settled_tasks()]
        if _open:
            _h = _open[IDX % len(_open)]
            _v = await mem.complete_goal_task(
                task_id=_h["task"], status="failed", result={},
                error="generated failure", expected_generation=int(_h["gen"]))
            log({"a": "complete", "task": _h["task"], "want": "failed",
                 "gen": int(_h["gen"]), "verdict": _v,
                 "applied": _v not in ("ignored", "superseded")})
    """,
    "STALE_COMPLETE": """
        # Aim at a step that is RUNNING RIGHT NOW. A step recovery already
        # terminated would be refused by first-write-wins whatever the fence
        # did, and an action that is refused for the wrong reason proves
        # nothing about the reason being tested.
        _gs, _ts = await state()
        _live = [t for t in _ts if t["status"] == "running"]
        if _live:
            _t = _live[IDX % len(_live)]
            # A revision BELOW the one the step belongs to. Revisions only ever
            # go up, so this one was never current and never will be - which
            # makes "it must be refused" a fact about the sequence rather than
            # a restatement of whatever the product answered.
            _wrong = int(_t["generation"]) - 1
            _v = await mem.complete_goal_task(
                task_id=str(_t["task_id"]), status="done", result={"ok": True},
                error="", expected_generation=_wrong)
            log({"a": "stale", "task": str(_t["task_id"]), "gen": _wrong,
                 "must_refuse": True, "verdict": _v,
                 "applied": _v not in ("ignored", "superseded")})
    """,
    "CLAIM_THEN_STALE": """
        # Self-sufficient on purpose. Every life begins with recovery, which
        # terminates whatever was runnable, so a bare claim finds nothing
        # almost every time and the fence goes untested. This makes the work
        # it needs, claims it, and then reports against a revision that was
        # never current.
        _gs, _ = await state()
        _g = pick_goal(_gs, IDX)
        if _g is not None:
            if _g["status"] == "paused":
                await mem.resume_goal(goal_id=_g["goal_id"])
                _after = await mem.get_goal(goal_id=_g["goal_id"])
                log({"a": "resume", "goal": str(_g["goal_id"]),
                     "was": "paused", "gen_before": int(_g["generation"]),
                     "gen_after": int(_after["generation"])})
                _g = _after
            _t = await mem.enqueue_goal_task(
                goal_id=_g["goal_id"], project_name=PROJ,
                tool_name=f"code.f{IDX}", args={})
            if _t is not None:
                log({"a": "enqueue", "goal": str(_g["goal_id"]),
                     "task": str(_t), "goal_gen": int(_g["generation"])})
                _mine = None
                for _ in range(60):
                    _x = await mem.claim_next_goal_task()
                    if _x is None:
                        break
                    log({"a": "claim", "task": str(_x["task_id"]),
                         "goal": str(_x["goal_id"]),
                         "gen": int(_x["generation"])})
                    if str(_x["task_id"]) == str(_t):
                        _mine = _x
                        break
                if _mine is not None:
                    _wrong = int(_mine["generation"]) - 1
                    _v = await mem.complete_goal_task(
                        task_id=str(_t), status="done", result={"ok": True},
                        error="", expected_generation=_wrong)
                    log({"a": "stale", "task": str(_t), "gen": _wrong,
                         "must_refuse": True, "verdict": _v,
                         "applied": _v not in ("ignored", "superseded")})
    """,
    "CANCEL": """
        _gs, _ = await state()
        _g = pick_goal(_gs, IDX)
        if _g is not None:
            await mem.cancel_goal(goal_id=_g["goal_id"])
            _after = await mem.get_goal(goal_id=_g["goal_id"])
            log({"a": "cancel", "goal": str(_g["goal_id"]),
                 "was": str(_g["status"]),
                 "gen_before": int(_g["generation"]),
                 "gen_after": int(_after["generation"])})
    """,
    "RESUME": """
        _gs, _ = await state()
        _g = pick_goal(_gs, IDX)
        if _g is not None:
            await mem.resume_goal(goal_id=_g["goal_id"])
            _after = await mem.get_goal(goal_id=_g["goal_id"])
            log({"a": "resume", "goal": str(_g["goal_id"]),
                 "was": str(_g["status"]),
                 "gen_before": int(_g["generation"]),
                 "gen_after": int(_after["generation"])})
    """,
    "PROGRESS": """
        _gs, _ = await state()
        _g = pick_goal(_gs, IDX)
        if _g is not None:
            await mem.add_progress_event(
                goal_id=_g["goal_id"], project_name=PROJ, kind="note",
                message=f"line {IDX}", generation=int(_g["generation"]))
            log({"a": "progress", "goal": str(_g["goal_id"]),
                 "gen": int(_g["generation"])})
    """,
}


def make_sequence(rng: random.Random) -> list[str]:
    """A walk that always ends in a life which can be observed."""
    n = rng.randint(6, 16)
    seq = ["CREATE_GOAL"]
    for _ in range(n):
        seq.append(rng.choice(ACTIONS))
    # An observable ending: a crash as the last action would leave the sequence
    # with nothing to read, which tests the harness rather than the product.
    while seq and seq[-1] in ("RESTART_CLEAN", "RESTART_CRASH"):
        seq.pop()
    seq.append("PROGRESS")
    return seq


def split_lives(seq: list[str]) -> list[tuple[list[tuple[int, str]], bool]]:
    """(actions, ends_in_crash) per life, actions carrying their step index."""
    lives, cur = [], []
    for i, act in enumerate(seq):
        if act == "RESTART_CLEAN":
            lives.append((cur, False))
            cur = []
        elif act == "RESTART_CRASH":
            lives.append((cur, True))
            cur = []
        else:
            cur.append((i, act))
    lives.append((cur, False))
    return lives


def build_body(root: Path, actions: list[tuple[int, str]], crash: bool) -> str:
    parts = [f'    PROJ = "{P}"',
             _LIFE_PREFIX.replace("@ROOT@", str(root))]
    for idx, act in actions:
        frag = _BODIES[act].replace("IDX", str(idx))
        parts.append("\n".join(line[4:] if line.startswith("        ") else line
                               for line in frag.strip("\n").splitlines()))
    if crash:
        parts.append("    CRASH()")
    else:
        parts.append('    emit({"final": await snapshot("final")})')
    return "\n".join(parts) + "\n"


def check_invariants(seq: list[str], seed: int, root: Path,
                     observations: list[dict]) -> tuple[list[str], int]:
    """Everything that must hold whatever order the actions fell in."""
    problems: list[str] = []
    history: list[dict] = []
    log = root / "steps.jsonl"
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    history.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    ever_claimed = {h["task"] for h in history if h.get("a") == "claim"}
    enqueued = {h["task"] for h in history
                if h.get("a") == "enqueue" and h.get("task")}
    refused = {h["task"] for h in history
               if h.get("a") in ("complete", "stale") and not h.get("applied")}
    applied = {h["task"] for h in history
               if h.get("a") in ("complete", "stale") and h.get("applied")}
    high_water: dict[str, int] = {}
    seen_task_ids: set[str] = set()

    for obs in observations:
        tag = obs.get("tag", "?")
        goals = {g[0]: (g[1], g[2]) for g in obs.get("goals", [])}
        tasks = obs.get("tasks", [])

        for gid, (_status, gen) in goals.items():
            if gen < high_water.get(gid, 0):
                problems.append(f"[{tag}] goal {gid[:8]} went back from "
                                f"{high_water[gid]} to {gen}")
            high_water[gid] = max(high_water.get(gid, 0), gen)

        for tid, gid, status, outcome, gen in tasks:
            seen_task_ids.add(tid)
            if (status, outcome) not in VALID_PAIRS:
                problems.append(f"[{tag}] {tid[:8]} is {status}/{outcome}, "
                                f"which describes nothing that can happen")
            if gid in goals and gen > goals[gid][1]:
                problems.append(f"[{tag}] {tid[:8]} claims revision {gen} but "
                                f"its goal has only reached {goals[gid][1]}")
            if tid not in ever_claimed and outcome in ("succeeded", "failed"):
                problems.append(f"[{tag}] {tid[:8]} was never claimed yet "
                                f"reports work {outcome}")
            if tid in refused and tid not in applied and status == "done":
                problems.append(f"[{tag}] {tid[:8]} is done, but every "
                                f"completion it received was refused")

        if tag == "at_boot":
            live = [t for t in tasks if t[2] in ("queued", "running")]
            if live:
                problems.append(f"[at_boot] {len(live)} step(s) still in "
                                f"flight after recovery: {live[:2]}")

        for gid, gen in obs.get("progress", []):
            if gen is not None and gid in goals and gen > goals[gid][1]:
                problems.append(f"[{tag}] a progress line claims revision "
                                f"{gen} on a goal at {goals[gid][1]}")

    # A completion aimed at a revision that was never current must never be
    # applied - whatever the product reported about it.
    for h in history:
        if h.get("a") == "stale" and h.get("must_refuse") and h.get("applied"):
            problems.append(f"[log] {str(h['task'])[:8]} accepted a completion "
                            f"for revision {h['gen']}, which was never current")

    # Revisions are a one-way count. Checked at the moment each one changes,
    # not only where a snapshot happened to land.
    walked: dict[str, int] = {}
    for h in history:
        gid = str(h.get("goal") or "")
        if not gid or "gen_after" not in h:
            continue
        before, after = int(h["gen_before"]), int(h["gen_after"])
        if after < before:
            problems.append(f"[log] {h['a']} took goal {gid[:8]} from "
                            f"revision {before} back to {after}")
        if h["a"] == "cancel" and h.get("was") != "cancelled" and after <= before:
            problems.append(f"[log] cancelling goal {gid[:8]} (it was "
                            f"{h.get('was')}) left it on revision {after}; "
                            f"a cancel opens a new one")
        if h["a"] == "resume" and h.get("was") == "paused" and after <= before:
            problems.append(f"[log] resuming paused goal {gid[:8]} left it on "
                            f"revision {after}; a resume opens a new run")
        if after < walked.get(gid, 0):
            problems.append(f"[log] goal {gid[:8]} fell from revision "
                            f"{walked[gid]} to {after} at {h['a']}")
        walked[gid] = max(walked.get(gid, 0), after)

    final = observations[-1] if observations else {}
    final_ids = {t[0] for t in final.get("tasks", [])}
    vanished = (enqueued | ever_claimed) - final_ids
    if vanished:
        problems.append(f"[final] {len(vanished)} step(s) disappeared: "
                        f"{sorted(vanished)[:2]}")
    fenced = sum(1 for h in history if h.get("a") == "stale")
    return problems, fenced


def run_sequence(seed: int) -> tuple[int, list[str], list[str], int, int]:
    """One generated sequence, start to finish. Returns problems found."""
    rng = random.Random(seed)
    seq = make_sequence(rng)
    lives = split_lives(seq)
    observations: list[dict] = []
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td) / "n"
        for actions, crash in lives:
            body = build_body(root, actions, crash)
            try:
                out = run_step(root, body, expect_crash=crash, timeout=240)
            except Exception as e:  # noqa: BLE001
                return seed, seq, [f"life failed: {str(e)[-400:]}"], len(lives), 0
            for row in out:
                for key in ("at_boot", "final"):
                    if key in row:
                        observations.append(row[key])
        problems, fenced = check_invariants(seq, seed, root, observations)
    return seed, seq, problems, len(lives), fenced


async def test_generated_restart_sequences():
    check.section(f"§19 {SEQUENCES} generated sequences across real restarts")
    failures: list[tuple[int, list[str], list[str]]] = []
    lives_total = 0
    restarts = 0
    crashes = 0
    fenced_attempts = 0

    seeds = [BASE_SEED + i for i in range(SEQUENCES)]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for seed, seq, problems, lives, fenced in pool.map(run_sequence, seeds):
            lives_total += lives
            fenced_attempts += fenced
            restarts += seq.count("RESTART_CLEAN") + seq.count("RESTART_CRASH")
            crashes += seq.count("RESTART_CRASH")
            if problems:
                failures.append((seed, seq, problems))

    for seed, seq, problems in failures[:5]:
        print(f"\n  SEED {seed} — replay with "
              f"NOVA_S13C_SEED={seed} NOVA_S13C_SEQS=1")
        print(f"  actions: {' '.join(seq)}")
        for pr in problems[:6]:
            print(f"    - {pr}")

    check(not failures,
          f"{SEQUENCES - len(failures)}/{SEQUENCES} sequences held every "
          f"invariant" + (f" ({len(failures)} failed)" if failures else ""))
    check(lives_total >= SEQUENCES,
          f"and they really were separate processes "
          f"({lives_total} lives over {SEQUENCES} sequences)")
    check(fenced_attempts >= SEQUENCES // 4,
          f"the fence was actually put to the test "
          f"({fenced_attempts} stale completions attempted)")
    check(crashes > 0 and restarts > crashes,
          f"with both kinds of ending exercised "
          f"({crashes} crashes, {restarts - crashes} clean restarts)")
    print(f"      ({restarts} restarts over {lives_total} interpreter lives)")


async def main() -> None:
    await test_generated_restart_sequences()
    check.finish()


if __name__ == "__main__":
    run(main)
