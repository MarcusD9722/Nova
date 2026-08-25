"""Generated transition sequences against an independent model (13B closure).

WHY A MODEL AND NOT MORE HAND-WRITTEN CASES

Three of Stage 13B's defects appeared only when valid components were
COMBINED, which is the argument for exploring the state space rather than
enumerating it by hand. This drives the real persistence and lifecycle methods
through hundreds of seeded sequences and compares the whole task/goal state
against a model derived from stated truths after EVERY transition.

HOW THE ORACLE STAYS INDEPENDENT

The Stage 13B-5 lesson was that a test model weaker than - or copied from -
the implementation only reproduces the implementation's defects. So the model
here is written from semantics, not from the SQL:

  * it never asks "would the claim predicate take this row?". It states what a
    runnable task IS (wanted work, not yet started, belonging to the plan the
    goal is currently on) and checks production's answer against that.
  * when production makes a choice the model deliberately does not predict -
    WHICH of several runnable tasks a claim returns, what id a resume's
    continuation gets - the model verifies the choice was LEGAL and absorbs
    it, rather than duplicating the tie-break and calling that agreement.
  * every terminal transition is predicted on both axes, because `status` and
    `outcome` answer different questions and a model that tracked only one
    could not see the other drift.

No model call is involved anywhere: the expected state is computed, never
generated.

ON FAILURE the seed and the full transition history print, so any failure is
replayable exactly.

Run:  venv\\Scripts\\python.exe tests\\test_generated_transitions_s13b.py
      venv\\Scripts\\python.exe tests\\test_generated_transitions_s13b.py 40 7
        (sequences, steps-per-sequence)
"""

from __future__ import annotations

import os
import random
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")
# This suite legitimately runs for ~2 minutes: 250 sequences each open
# their own store. The default 180s watchdog exists to catch a HANG, and
# at 180 it would be catching honest work instead - a suite killed by its
# own guard reports nothing at all. Raised, not removed.
os.environ.setdefault("NOVA_IT_WATCHDOG_S", "600")

from harness import Checks, run  # noqa: E402

from memory.unifier import MemoryUnifier  # noqa: E402

check = Checks()

PROJECTS = ("flappy-bird", "quickcalc")

ACTIONS = (
    "CREATE_GOAL", "QUEUE_TASK", "CLAIM",
    "COMPLETE_SUCCESS", "COMPLETE_FAILURE", "RETRY",
    "PAUSE", "RESUME", "CANCEL",
    "STALE_COMPLETION", "DUPLICATE_COMPLETION", "DUPLICATE_RETRY",
    "RESTART", "SWITCH_PROJECT",
)


class Model:
    """What SHOULD be true, derived from semantics rather than from the code.

    goals: gid -> {status, gen}
    tasks: tid -> {gid, gen, status, outcome}
    """

    def __init__(self) -> None:
        self.goals: dict[str, dict] = {}
        self.tasks: dict[str, dict] = {}

    # -- the semantic definitions everything else is derived from -----------

    def wanted(self, gid: str) -> bool:
        """Is the goal's work still wanted? Cancelled and completed are not."""
        return self.goals.get(gid, {}).get("status") in ("active", "paused")

    def current_plan(self, gid: str) -> int:
        """Which revision of the plan the goal is on now."""
        return int(self.goals.get(gid, {}).get("gen", -1))

    def belongs_to_current_plan(self, tid: str) -> bool:
        t = self.tasks[tid]
        return int(t["gen"]) == self.current_plan(t["gid"])

    def runnable(self, tid: str) -> bool:
        """Work that is wanted, not yet started, and part of the live plan.

        Stated as three separate conditions on purpose: each is a claim about
        meaning, and any one of them failing is a different kind of bug.
        """
        t = self.tasks[tid]
        return (t["status"] == "queued"
                and self.goals.get(t["gid"], {}).get("status") == "active"
                and self.belongs_to_current_plan(tid))

    def terminal(self, tid: str) -> bool:
        return self.tasks[tid]["status"] in (
            "done", "failed", "cancelled", "superseded")

    # -- transitions ---------------------------------------------------------

    def create_goal(self, gid: str) -> None:
        self.goals[gid] = {"status": "active", "gen": 0}

    def queue_task(self, gid: str, tid: str) -> None:
        if not self.wanted(gid):
            return                      # refused; no row appears
        self.tasks[tid] = {"gid": gid, "gen": self.current_plan(gid),
                           "status": "queued", "outcome": "pending"}

    def claimed(self, tid: str) -> None:
        self.tasks[tid]["status"] = "running"

    def complete(self, tid: str, reported: str, expected_gen: int) -> None:
        """A terminal write, owned or not.

        Owned means: this is still the running task, on the revision the writer
        thinks it is, for a goal that has not ended. Anything else either
        supersedes (the work happened, the run did not survive) or is ignored
        (the row is already finished, and the first answer stands).
        """
        t = self.tasks[tid]
        if t["status"] != "running":
            return                                   # first write wins
        outcome = "succeeded" if reported == "done" else "failed"
        owns = (int(expected_gen) == int(t["gen"])
                and self.belongs_to_current_plan(tid)
                and self.wanted(t["gid"]))
        t["status"] = reported if owns else "superseded"
        t["outcome"] = outcome

    def retry(self, tid: str, expected_gen: int) -> None:
        t = self.tasks[tid]
        if t["status"] != "running":
            return
        if (int(expected_gen) != int(t["gen"])
                or not self.belongs_to_current_plan(tid)
                or self.goals[t["gid"]]["status"] != "active"):
            return
        t["status"] = "queued"
        t["outcome"] = "pending"

    def pause(self, gid: str) -> None:
        if self.goals.get(gid, {}).get("status") == "active":
            self.goals[gid]["status"] = "paused"

    def resume(self, gid: str) -> None:
        g = self.goals.get(gid)
        if not g or g["status"] not in ("paused", "cancelled"):
            return
        if g["status"] == "paused":
            g["gen"] += 1          # a pause resumes into a NEW bounded run
        g["status"] = "active"

    def cancel(self, gid: str) -> None:
        g = self.goals.get(gid)
        if not g or g["status"] == "cancelled":
            return
        g["status"] = "cancelled"
        g["gen"] += 1
        for t in self.tasks.values():
            if t["gid"] == gid and t["status"] == "queued":
                t["status"] = "cancelled"
                t["outcome"] = "never_started"

    def restart(self) -> None:
        """Nothing runs unasked after a restart, and nothing is left claiming
        to be running."""
        for t in self.tasks.values():
            if t["status"] == "queued":
                t["status"] = "cancelled"
                t["outcome"] = "never_started"
            elif t["status"] == "running":
                t["status"] = "failed"
                t["outcome"] = "unknown"
        for g in self.goals.values():
            if g["status"] == "active":
                g["status"] = "paused"

    # -- projection used for comparison -------------------------------------

    def projection(self) -> dict:
        return {
            "goals": {g: (v["status"], v["gen"]) for g, v in self.goals.items()},
            "tasks": {t: (v["status"], v["outcome"])
                      for t, v in self.tasks.items()},
        }


async def _actual(mem) -> dict:
    goals = await mem.list_goals(limit=200)
    tasks = await mem.list_goal_tasks(limit=500)
    return {
        "goals": {str(g["goal_id"]): (str(g["status"]), int(g["generation"]))
                  for g in goals},
        "tasks": {str(t["task_id"]): (str(t["status"]), str(t["outcome"]))
                  for t in tasks},
    }


def _diff(model: dict, actual: dict) -> list[str]:
    out = []
    for kind in ("goals", "tasks"):
        for key in sorted(set(model[kind]) | set(actual[kind])):
            m, a = model[kind].get(key), actual[kind].get(key)
            if m != a:
                out.append(f"{kind[:-1]} {key[:8]}: model={m} actual={a}")
    return out


async def _run_one(seed: int, steps: int) -> tuple[bool, list[str], list[str]]:
    """One generated sequence. Returns (ok, history, differences)."""
    rng = random.Random(seed)
    history: list[str] = []

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        store = Path(td) / "nova"
        mem = MemoryUnifier(store, enable_chroma=False)
        await mem.initialize()
        model = Model()
        project = PROJECTS[0]
        last_claim: dict | None = None
        last_completed: tuple[str, str, int] | None = None
        last_retried: tuple[str, int] | None = None

        for step in range(steps):
            action = rng.choice(ACTIONS)
            gids = list(model.goals)
            tids = list(model.tasks)

            if action == "SWITCH_PROJECT":
                project = PROJECTS[(PROJECTS.index(project) + 1) % len(PROJECTS)]
                history.append(f"{step}: SWITCH_PROJECT -> {project}")
                continue

            if action == "CREATE_GOAL" or not gids:
                gid = await mem.create_goal(project_name=project, title=f"g{step}",
                                            objective="o", success_criteria="c")
                model.create_goal(str(gid))
                history.append(f"{step}: CREATE_GOAL {str(gid)[:8]} ({project})")

            elif action == "QUEUE_TASK":
                gid = rng.choice(gids)
                before = set((await _actual(mem))["tasks"])
                await mem.enqueue_goal_task(goal_id=gid, project_name=project,
                                            tool_name=f"t{step}", args={})
                after = set((await _actual(mem))["tasks"])
                new = after - before
                if new:
                    model.queue_task(gid, next(iter(new)))
                history.append(f"{step}: QUEUE_TASK on {gid[:8]} -> {len(new)} row(s)")

            elif action == "CLAIM":
                got = await mem.claim_next_goal_task()
                if got:
                    tid = str(got["task_id"])
                    # The model does not predict WHICH row wins; it checks the
                    # one that did was legally claimable.
                    if tid not in model.tasks or not model.runnable(tid):
                        return False, history + [f"{step}: CLAIM took {tid[:8]}"], \
                            [f"claimed a task the model says was not runnable: "
                             f"{model.tasks.get(tid)}"]
                    model.claimed(tid)
                    last_claim = got
                else:
                    # Nothing claimed: the model must agree nothing was runnable.
                    runnable = [t for t in model.tasks if model.runnable(t)]
                    if runnable:
                        return False, history + [f"{step}: CLAIM got nothing"], \
                            [f"model says {len(runnable)} task(s) were runnable"]
                history.append(f"{step}: CLAIM -> "
                               f"{str((got or {}).get('task_id', 'none'))[:8]}")

            elif action in ("COMPLETE_SUCCESS", "COMPLETE_FAILURE",
                            "STALE_COMPLETION"):
                if last_claim is None:
                    history.append(f"{step}: {action} (nothing claimed)")
                    continue
                tid = str(last_claim["task_id"])
                gen = int(last_claim["generation"])
                if action == "STALE_COMPLETION":
                    gen = gen + rng.choice([-1, 1, 5])    # a wrong revision
                status = "failed" if action == "COMPLETE_FAILURE" else "done"
                await mem.complete_goal_task(
                    task_id=tid, status=status,
                    result={"ok": status == "done"}, error="",
                    expected_generation=gen)
                model.complete(tid, status, gen)
                last_completed = (tid, status, gen)
                history.append(f"{step}: {action} {tid[:8]} gen={gen}")

            elif action == "DUPLICATE_COMPLETION":
                if last_completed is None:
                    history.append(f"{step}: DUPLICATE_COMPLETION (none yet)")
                    continue
                tid, status, gen = last_completed
                await mem.complete_goal_task(
                    task_id=tid, status=status, result={}, error="",
                    expected_generation=gen)
                model.complete(tid, status, gen)
                history.append(f"{step}: DUPLICATE_COMPLETION {tid[:8]}")

            elif action == "RETRY":
                if last_claim is None:
                    history.append(f"{step}: RETRY (nothing claimed)")
                    continue
                tid = str(last_claim["task_id"])
                gen = int(last_claim["generation"])
                await mem.bump_goal_task_attempt(
                    task_id=tid, attempts=1,
                    run_after_iso="2000-01-01T00:00:00+00:00",
                    error="transient", expected_generation=gen)
                model.retry(tid, gen)
                last_retried = (tid, gen)
                history.append(f"{step}: RETRY {tid[:8]} gen={gen}")

            elif action == "DUPLICATE_RETRY":
                if last_retried is None:
                    history.append(f"{step}: DUPLICATE_RETRY (none yet)")
                    continue
                tid, gen = last_retried
                await mem.bump_goal_task_attempt(
                    task_id=tid, attempts=2,
                    run_after_iso="2000-01-01T00:00:00+00:00",
                    error="transient", expected_generation=gen)
                model.retry(tid, gen)
                history.append(f"{step}: DUPLICATE_RETRY {tid[:8]}")

            elif action == "PAUSE":
                gid = rng.choice(gids)
                await mem.update_goal_status(goal_id=gid, status="paused")
                model.pause(gid)
                history.append(f"{step}: PAUSE {gid[:8]}")

            elif action == "RESUME":
                gid = rng.choice(gids)
                before = set((await _actual(mem))["tasks"])
                await mem.resume_goal(goal_id=gid)
                model.resume(gid)
                after = await _actual(mem)
                # A resume may insert ONE continuation. The model does not
                # predict its id; it checks there is at most one and adopts it
                # at the goal's current revision.
                new = set(after["tasks"]) - before
                if len(new) > 1:
                    return False, history + [f"{step}: RESUME {gid[:8]}"], \
                        [f"resume inserted {len(new)} continuations, expected <= 1"]
                for tid in new:
                    model.tasks[tid] = {"gid": gid, "gen": model.current_plan(gid),
                                        "status": "queued", "outcome": "pending"}
                history.append(f"{step}: RESUME {gid[:8]} (+{len(new)})")

            elif action == "CANCEL":
                gid = rng.choice(gids)
                await mem.cancel_goal(goal_id=gid)
                model.cancel(gid)
                last_claim = None
                history.append(f"{step}: CANCEL {gid[:8]}")

            elif action == "RESTART":
                del mem
                mem = MemoryUnifier(store, enable_chroma=False)
                await mem.initialize()
                await mem.cancel_pending_background_work()
                model.restart()
                last_claim = None
                history.append(f"{step}: RESTART")

            # ── compare EVERYTHING after every transition ──────────────────
            diffs = _diff(model.projection(), await _actual(mem))
            if diffs:
                return False, history, diffs

            # ── and the invariants that are not just state equality ────────
            actual = await _actual(mem)
            for tid, (st, out) in actual["tasks"].items():
                if st == "done" and out == "never_started":
                    return False, history, [f"{tid[:8]} is done but never ran"]
                if st == "cancelled" and out == "succeeded":
                    return False, history, [f"{tid[:8]} cancelled yet succeeded"]

        return True, history, []


async def main() -> None:
    sequences = int(sys.argv[1]) if len(sys.argv) > 1 else 250
    steps = int(sys.argv[2]) if len(sys.argv) > 2 else 12

    check.section(f"generated sequences: {sequences} x {steps} transitions")

    failures: list[tuple[int, list[str], list[str]]] = []
    transitions = 0
    for seed in range(sequences):
        ok, history, diffs = await _run_one(seed, steps)
        transitions += len(history)
        if not ok:
            failures.append((seed, history, diffs))
            if len(failures) >= 3:
                break

    if failures:
        for seed, history, diffs in failures:
            print(f"\n  SEED {seed} diverged:")
            for h in history:
                print(f"      {h}")
            for d in diffs:
                print(f"    -> {d}")

    check(not failures,
          f"{sequences} sequences agreed with the model "
          f"({transitions} transitions; failing seeds: "
          f"{[s for s, _, _ in failures] or 'none'})")
    check(transitions >= sequences,
          f"and every sequence produced transitions ({transitions})")

    check.finish()


if __name__ == "__main__":
    run(main)
