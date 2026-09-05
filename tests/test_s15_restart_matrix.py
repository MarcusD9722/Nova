"""Stage 15 — twelve crash boundaries, each across a REAL process boundary.

Every "restart" here is a new interpreter (`tests/s15_restart_worker.py`) booted
against the same durable root. Nothing crosses but storage: no service objects,
no module state, no event bus, no broker memory. A crash is `os._exit(0)` mid-
flight, so no `finally`, no shutdown hook and no unflushed write survives.

What each restart has to prove:

  no duplicate side effect        a tool that already acted does not act twice
  no stale-generation mutation    a worker from the dead run cannot write
  no resurrection                 cancelled/interrupted work does not resume
  no fabricated success           unknown outcomes are recorded as unknown
  no foreground/background swap   in-flight work does not become a finished turn
  no cross-project contamination  A's state never lands on B
  no cross-conversation leak      a codeword stays in the thread that said it
  no false completion             an unfinished artifact is never `complete`
  identical reconstruction        the same facts, by identity, from disk alone

Run:  venv\\Scripts\\python.exe tests\\test_s15_restart_matrix.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from harness import Checks  # noqa: E402

check = Checks()

WORKER = REPO / "tests" / "s15_restart_worker.py"
MARK = "##NOVA##"


def life(root: Path, phase: str, *args, timeout: float = 420.0) -> dict:
    """One process. Boots, acts, reports, and for a `b*` phase, dies."""
    proc = subprocess.run(
        [sys.executable, str(WORKER), str(root), phase, *[str(a) for a in args]],
        capture_output=True, text=True, timeout=timeout)
    facts = [ln for ln in proc.stdout.splitlines() if ln.startswith(MARK)]
    if not facts:
        raise RuntimeError(
            f"{phase} produced no facts (exit {proc.returncode})\n"
            f"--- stdout ---\n{proc.stdout[-1500:]}\n"
            f"--- stderr ---\n{proc.stderr[-1500:]}")
    return json.loads(facts[-1][len(MARK):])


def fresh() -> Path:
    return Path(tempfile.mkdtemp(prefix="nova-s15-restart-"))


def task_of(facts: dict, task_id: str) -> dict:
    return next((t for t in facts["tasks"] if t["task_id"] == task_id), {})


def goal_of(facts: dict, goal_id: str) -> dict:
    return next((g for g in facts["goals"] if g["goal_id"] == goal_id), {})


# ── 1: a task exists, and nothing has touched it ───────────────────────────

def boundary_1_after_task_creation() -> None:
    check.section("1 crash after task creation")
    root = fresh()
    try:
        before = life(root, "b1_after_task_creation")
        after = life(root, "inspect")
        tid, gid = before["task_id"], before["goal_id"]

        check(task_of(before, tid).get("status") == "queued",
              f"the task was queued when the process died "
              f"({task_of(before, tid).get('status')})")
        check(task_of(after, tid) and goal_of(after, gid),
              "both rows reconstruct by their own ids after the restart")
        check(task_of(after, tid).get("status") == "cancelled",
              f"and the queued task is cancelled, not resumed "
              f"({task_of(after, tid).get('status')})")
        check(goal_of(after, gid).get("status") == "paused",
              f"its goal is paused for a person to pick up "
              f"({goal_of(after, gid).get('status')})")
        check(goal_of(after, gid).get("generation")
              == goal_of(before, gid).get("generation"),
              f"on the same generation ({goal_of(after, gid).get('generation')})")

        ran = life(root, "act_claim")
        check(ran["claimed"] is None,
              f"nothing is claimable, so nothing runs (claimed={ran['claimed']})")
        check(ran["side_effects"] == 0,
              f"and no side effect happened at all ({ran['side_effects']})")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ── 2 & 3: claimed, and possibly already acted ─────────────────────────────

def boundary_2_after_task_claim() -> None:
    check.section("2 crash after the claim, before the tool")
    root = fresh()
    try:
        before = life(root, "b2_after_task_claim")
        tid = before["task_id"]
        check(task_of(before, tid).get("status") == "running",
              f"the task was running when the process died "
              f"({task_of(before, tid).get('status')})")

        after = life(root, "inspect")
        row = task_of(after, tid)
        check(row.get("status") == "failed",
              f"a claimed task is not left running ({row.get('status')})")
        check(row.get("outcome") == "unknown",
              f"and its outcome is recorded as UNKNOWN, not as success "
              f"({row.get('outcome')})")
        check("interrupted" in row.get("error", ""),
              f"with the honest reason ({row.get('error')!r})")

        ran = life(root, "act_claim")
        check(ran["claimed"] is None,
              f"it cannot be claimed a second time ({ran['claimed']})")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def boundary_3_after_tool_invocation() -> None:
    check.section("3 crash after the tool ran")
    root = fresh()
    try:
        before = life(root, "b3_after_tool_invocation")
        tid = before["task_id"]
        check(before["side_effects"] == 1,
              f"the tool acted exactly once before the crash "
              f"({before['side_effects']})")

        after = life(root, "inspect")
        check(after["side_effects"] == 1,
              f"the restart alone repeats nothing ({after['side_effects']})")
        check(task_of(after, tid).get("outcome") == "unknown",
              f"and the row does not claim the tool succeeded "
              f"({task_of(after, tid).get('outcome')})")

        ran = life(root, "act_claim")
        check(ran["claimed"] is None and ran["ran_again"] is False,
              f"nor can a later life re-run it ({ran['claimed']})")
        check(ran["side_effects"] == 1,
              f"the side effect count is still one ({ran['side_effects']})")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ── 4: the act landed; the result never did ────────────────────────────────

def boundary_4_side_effect_without_result() -> None:
    check.section("4 crash between the side effect and the durable result")
    root = fresh()
    try:
        before = life(root, "b4_after_side_effect_before_result")
        tid = before["task_id"]
        artifact = root / "projects" / "alpha" / "generated.py"
        check(artifact.exists(),
              "the artifact the tool wrote is on disk")
        check(task_of(before, tid).get("outcome") == "pending",
              f"while the result never reached the row "
              f"({task_of(before, tid).get('outcome')})")

        after = life(root, "inspect")
        row = task_of(after, tid)
        check(row.get("status") != "done",
              f"the restart does not invent a result ({row.get('status')})")
        check(row.get("outcome") == "unknown",
              f"it says outright that the outcome is unknown "
              f"({row.get('outcome')})")
        check(artifact.exists(),
              "and it does not destroy what the tool actually produced")
        check(after["completion"]["alpha"]["state"] != "complete",
              f"a file on disk is not acceptance evidence "
              f"({after['completion']['alpha']['state']})")
        check(after["side_effects"] == 1,
              f"still one side effect ({after['side_effects']})")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ── 5 & 6: terminal states survive ─────────────────────────────────────────

def boundary_5_after_task_completion() -> None:
    check.section("5 crash after the task completed")
    root = fresh()
    try:
        before = life(root, "b5_after_task_completion")
        tid = before["task_id"]
        check(before["applied"] == "applied",
              f"the completion was applied before the crash "
              f"({before['applied']})")

        after = life(root, "inspect")
        row = task_of(after, tid)
        check(row.get("status") == "done",
              f"a finished task stays finished ({row.get('status')})")
        check(row.get("generation") == task_of(before, tid).get("generation"),
              f"on its own generation ({row.get('generation')})")
        ran = life(root, "act_claim")
        check(ran["claimed"] is None and ran["side_effects"] == 1,
              f"and it is never handed out again "
              f"({ran['claimed']}, effects={ran['side_effects']})")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def boundary_6_after_goal_advancement() -> None:
    check.section("6 crash after the goal advanced")
    root = fresh()
    try:
        before = life(root, "b6_after_goal_advancement")
        gid, tid = before["goal_id"], before["task_id"]
        check(goal_of(before, gid).get("status") == "completed",
              f"the goal was completed before the crash "
              f"({goal_of(before, gid).get('status')})")

        after = life(root, "inspect")
        check(goal_of(after, gid).get("status") == "completed",
              f"and it is still completed, not paused by the restart "
              f"({goal_of(after, gid).get('status')})")
        check(task_of(after, tid).get("status") == "done",
              f"with its task still done ({task_of(after, tid).get('status')})")
        check(goal_of(after, gid).get("generation")
              == goal_of(before, gid).get("generation"),
              "on an unchanged generation")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ── 7 & 8: the completion axis, evaluated and unannounced ──────────────────

def boundary_7_after_completion_evaluation() -> None:
    check.section("7 crash after the completion was evaluated")
    root = fresh()
    try:
        before = life(root, "b7_after_completion_evaluation")
        after = life(root, "inspect")
        check(before["evaluated"] == "complete",
              f"it evaluated to complete before the crash "
              f"({before['evaluated']})")
        check(after["completion"]["alpha"]["state"] == before["evaluated"],
              f"and reconstructs to the same state from disk alone "
              f"({after['completion']['alpha']['state']})")
        check(after["completion"]["alpha"]["reason"]
              == before["completion"]["alpha"]["reason"],
              f"for the same recorded reason "
              f"({after['completion']['alpha']['reason']!r})")
        check(after["completion"]["alpha"]["revision"]
              == before["completion"]["alpha"]["revision"],
              "against the same requirement revision")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def boundary_8_before_announcement() -> None:
    check.section("8 crash before the announcement went out")
    root = fresh()
    try:
        before = life(root, "b8_before_announcement")
        after = life(root, "inspect")
        # The announcement is the thing the crash destroyed. The STATE is a
        # function of recorded facts, so losing the announcement must not lose
        # the answer.
        check(after["completion"]["alpha"]["state"] == "complete",
              f"the state is still derivable without the announcement "
              f"({after['completion']['alpha']['state']})")
        check(after["completion"]["alpha"]["sealed"] is True,
              "the contract is still sealed")
        check(after["completion"]["alpha"]["revision"] == before["revision"],
              f"on the revision the evidence was filed against "
              f"({after['completion']['alpha']['revision']})")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ── 9: A and B, interleaved, then gone ─────────────────────────────────────

def boundary_9_interleaved_ab() -> None:
    check.section("9 crash with A and B both in flight")
    root = fresh()
    try:
        before = life(root, "b9_interleaved_ab")
        after = life(root, "inspect")
        t_a, t_b = before["task_a"], before["task_b"]

        check(task_of(after, t_a)["project"] == "alpha"
              and task_of(after, t_b)["project"] == "bravo",
              "each task reconstructs against its own project")
        check(task_of(after, t_a)["outcome"] == "unknown",
              f"A's claimed task is unknown, not successful "
              f"({task_of(after, t_a)['outcome']})")
        check(task_of(after, t_b)["status"] == "cancelled",
              f"B's queued task is cancelled, not run "
              f"({task_of(after, t_b)['status']})")
        check(after["completion"]["alpha"]["state"] == "complete"
              and after["completion"]["bravo"]["state"] != "complete",
              f"and the two completion states stay apart "
              f"({after['completion']['alpha']['state']} vs "
              f"{after['completion']['bravo']['state']})")
        check(after["side_effects"] == 1,
              f"A's single side effect is not multiplied by B "
              f"({after['side_effects']})")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def boundary_9b_two_conversations() -> None:
    check.section("9b crash with two live conversations")
    root = fresh()
    try:
        before = life(root, "b9b_two_conversations")
        check(before["landed"] >= 1,
              f"A's codeword was durable before the crash "
              f"({before['landed']})")
        after = life(root, "act_conversations", before["conv_a"],
                     before["conv_b"])
        s = after["scoped"]
        check(s["a:ZULUALPHA"] >= 1,
              f"A's codeword comes back in A's thread ({s['a:ZULUALPHA']})")
        check(s["b:ZULUBRAVO"] >= 1,
              f"B's comes back in B's ({s['b:ZULUBRAVO']})")
        check(s["a:ZULUBRAVO"] == 0 and s["b:ZULUALPHA"] == 0,
              f"and neither leaks into the other across the restart "
              f"(a:BRAVO={s['a:ZULUBRAVO']}, b:ALPHA={s['b:ZULUALPHA']})")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ── 10: a worker from the dead run comes back ──────────────────────────────

def boundary_10_stale_generation() -> None:
    check.section("10 crash with stale-generation work outstanding")
    root = fresh()
    try:
        before = life(root, "b10_stale_generation_queued")
        gid, tid, gen = before["goal_id"], before["task_id"], before["generation"]
        check(goal_of(before, gid)["generation"] == gen + 1,
              f"the cancel moved the goal on ({gen} -> "
              f"{goal_of(before, gid)['generation']})")

        after = life(root, "inspect")
        check(goal_of(after, gid)["generation"] == gen + 1,
              f"the new generation survives the restart "
              f"({goal_of(after, gid)['generation']})")

        # The dead run's worker reports success into the new life.
        late = life(root, "act_complete_stale", tid, gen)
        # MEASURED, and worth being exact about: this comes back `ignored`,
        # not `superseded`. Across a restart the generation fence never gets
        # to speak -- startup has already written a terminal outcome onto the
        # row, and `complete_task` refuses anything whose status is no longer
        # `running` before it ever compares generations. Two independent
        # guards, and the restart makes the stronger one fire first. (The
        # generation fence itself returns `superseded` in-process; that is
        # proved in test_s15_foreground_background.py.)
        check(late["outcome"] == "ignored",
              f"its late success is refused outright ({late['outcome']})")
        check(late["outcome"] != "applied",
              f"nothing from the dead run was written ({late['outcome']})")
        check(task_of(late, tid)["status"] != "done",
              f"and the row is not rewritten to done "
              f"({task_of(late, tid)['status']})")
        check(task_of(late, tid)["outcome"] == "unknown",
              f"it still says the outcome is unknown "
              f"({task_of(late, tid)['outcome']})")
        # POSITIVE CONTROL for all of that refusal.
        check(late["live_outcome"] == "applied",
              f"while live work in the same process completes normally "
              f"({late['live_outcome']})")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ── 11: nothing decided, and it stays that way ─────────────────────────────

def boundary_11_with_inconclusive() -> None:
    check.section("11 crash with an undecided check on record")
    root = fresh()
    try:
        before = life(root, "b11_with_inconclusive")
        after = life(root, "inspect")
        check(before["completion"]["alpha"]["state"] != "complete",
              f"undecided is not complete before the crash "
              f"({before['completion']['alpha']['state']})")
        check(after["completion"]["alpha"]["state"]
              == before["completion"]["alpha"]["state"],
              f"and the restart does not resolve it either way "
              f"({after['completion']['alpha']['state']})")

        # LIVENESS: a check that DOES decide, in a third process, still works.
        decided = life(root, "act_decide", "alpha", before["criterion"])
        check(decided["completion"]["alpha"]["state"] == "complete",
              f"a decided check moves it to complete "
              f"({decided['completion']['alpha']['state']})")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ── 12: a request nobody answered ──────────────────────────────────────────

def boundary_12_pending_permission() -> None:
    check.section("12 crash with a permission request outstanding")
    root = fresh()
    try:
        before = life(root, "b12_pending_permission")
        rid = before["request_id"]
        check(before["decision"] == "needs_confirmation"
              and before["pending_permissions"] == [rid],
              f"it was waiting on a person when the process died "
              f"({before['pending_permissions']})")

        after = life(root, "inspect", rid)
        check(after["pending_permissions"] == [],
              f"nothing is offered for approval after the restart "
              f"({after['pending_permissions']})")
        check(after["settled_as"] and after["settled_as"] != "pending",
              f"but the trail still says how it ended "
              f"({after['settled_as']!r})")

        acted = life(root, "act_permission", rid)
        check(acted["old_resolved"] is False,
              f"a late click cannot approve the dead request "
              f"({acted['old_resolved']})")
        check(acted["old_settled_as"] and acted["old_settled_as"] != "approved",
              f"and it is not recorded as approved "
              f"({acted['old_settled_as']!r})")
        # LIVENESS: the broker in the new process is not merely broken.
        check(acted["new_decision"] == "needs_confirmation"
              and acted["new_resolved"] is True,
              f"a NEW request is raised and answered normally "
              f"({acted['new_decision']}, resolved={acted['new_resolved']})")
        check(acted["new_settled_as"] == "rejected",
              f"with the answer that was given ({acted['new_settled_as']!r})")
        check((root / "projects" / "alpha" / "PROJECT.md").exists(),
              "and nothing was deleted by any of it")
    finally:
        shutil.rmtree(root, ignore_errors=True)


BOUNDARIES = (
    boundary_1_after_task_creation,
    boundary_2_after_task_claim,
    boundary_3_after_tool_invocation,
    boundary_4_side_effect_without_result,
    boundary_5_after_task_completion,
    boundary_6_after_goal_advancement,
    boundary_7_after_completion_evaluation,
    boundary_8_before_announcement,
    boundary_9_interleaved_ab,
    boundary_9b_two_conversations,
    boundary_10_stale_generation,
    boundary_11_with_inconclusive,
    boundary_12_pending_permission,
)


def main() -> None:
    for fn in BOUNDARIES:
        fn()
    check.finish()


if __name__ == "__main__":
    main()
