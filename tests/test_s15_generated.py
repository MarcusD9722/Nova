"""Stage 15 — a thousand generated cross-capability sequences.

Each sequence builds its own project and then applies randomly chosen
operations drawn from every capability at once: requirements and criteria,
machine verdicts and human decisions, goals, tasks, claims, generations, and
permission requests. After every operation the suite checks what the STORE
says against what the sequence knows it did.

Two rules this file exists to enforce, both learned the hard way:

  * COVERAGE IS ASSERTED. A generator that passes a thousand times while never
    reaching COMPLETE has proved nothing about COMPLETE. Every outcome this
    file claims to exercise is counted, and the run FAILS if any of them was
    never reached -- including the ones that only happen when something is
    refused.
  * A WITNESS IS WATCHED. One project is set up at the start, never touched
    again, and re-read after every single operation. If any operation on any
    other project moves it, that is a leak, and the sequence and seed that did
    it are printed.

Seeded and reproducible: the seed is printed, and rerunning with
NOVA_S15_SEED=<n> replays the identical run.

Run:  venv\\Scripts\\python.exe tests\\test_s15_generated.py
"""

from __future__ import annotations

import os
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, boot, run  # noqa: E402

from core.completion import (  # noqa: E402
    COMPLETE, FAILED, INCONCLUSIVE, PASSED,
)

check = Checks()

SEQUENCES = int(os.getenv("NOVA_S15_SEQUENCES", "1000"))
OPS_PER_SEQUENCE = (4, 10)

#: Everything this file claims to exercise. Each must be reached at least once
#: or the run fails: an unexercised branch is an untested one, however many
#: sequences ran.
COVER: dict[str, int] = {
    "claim:hit": 0, "claim:miss": 0,
    "complete:applied": 0, "complete:superseded": 0, "complete:ignored": 0,
    "verdict:passed": 0, "verdict:failed": 0, "verdict:inconclusive": 0,
    "human:accepted": 0, "human:refused": 0,
    "goal:cancelled": 0, "goal:completed": 0, "goal:failed": 0,
    "requirement:first": 0, "requirement:correction": 0,
    "permission:approved": 0, "permission:rejected": 0,
    "state:complete": 0, "state:failing": 0, "state:planned": 0,
    "state:scaffolded": 0, "state:idea": 0,
}

FAILURES: list[str] = []


def cover(key: str) -> None:
    COVER[key] = COVER.get(key, 0) + 1


class Sequence:
    """One generated project, and the truth about what was done to it."""

    def __init__(self, nova, rng: random.Random, n: int) -> None:
        self.nova = nova
        self.rng = rng
        self.slug = f"gen{n:04d}"
        self.path = nova.projects_dir / self.slug
        self.path.mkdir(parents=True, exist_ok=True)
        (self.path / "PROJECT.md").write_text(
            f"# {self.slug}\n\n## Status\nidea\n", encoding="utf-8")
        # A fifth of the projects have NO code, so a sealed contract
        # with nothing built (`planned`) is reachable at all.
        self.has_code = rng.random() > 0.2
        if self.has_code:
            (self.path / "main.py").write_text(
                "def add(a, b):\n    return a + b\n",
                encoding="utf-8")
        # What this sequence knows to be true.
        self.revision = 0
        self.sealed = False
        self.criteria: list[str] = []
        self.proved: set[str] = set()
        self.refuted: set[str] = set()
        self.goal: str | None = None
        self.generation = 0
        self.goal_live = False
        self.queued: list[str] = []
        self.claimed_rows: list[dict] = []
        self.last_finished: str | None = None

    # ── operations ─────────────────────────────────────────────────────────

    async def op_request(self) -> str:
        svc = self.nova.runtime.completion
        wanted = ["adds numbers", "subtracts numbers", "prints a total",
                  "exports a csv"][:1 + len(self.criteria)]
        text = " and ".join(wanted)
        rev = await svc.record_request(slug=self.slug, request_text=text)
        cover("requirement:correction" if self.revision else "requirement:first")
        if rev != self.revision + 1:
            FAILURES.append(f"{self.slug}: revision {self.revision} -> {rev}")
        self.revision = rev
        ids = await svc.set_criteria(slug=self.slug, revision=rev, criteria=[
            {"text": t, "origin_quote": t, "verify_kind": "machine"}
            for t in wanted])
        await svc.seal_contract(slug=self.slug, revision=rev)
        self.sealed = True
        self.criteria = [str(i) for i in ids]
        self.proved.clear()
        self.refuted.clear()
        return "request"

    async def op_verdict(self) -> str:
        if not self.criteria:
            return ""
        svc = self.nova.runtime.completion
        cid = self.rng.choice(self.criteria)
        value = self.rng.choice([PASSED, PASSED, FAILED, INCONCLUSIVE])
        ctx = await svc.begin_check(slug=self.slug, criterion_id=cid)
        await svc.record_verdict(context=ctx, verdict=value,
                                 error=("nope" if value != PASSED else ""))
        cover(f"verdict:{value}")
        if value == PASSED:
            self.proved.add(cid)
            self.refuted.discard(cid)
        else:
            self.proved.discard(cid)
            if value == FAILED:
                self.refuted.add(cid)
            else:
                self.refuted.discard(cid)
        return "verdict"

    async def op_human(self) -> str:
        if not self.criteria:
            return ""
        svc = self.nova.runtime.completion
        cid = self.rng.choice(self.criteria)
        decision = await svc.ask_human(slug=self.slug, criterion_id=cid,
                                       prompt="ok?")
        accepted = self.rng.random() < 0.5
        await svc.resolve_human_decision(decision_id=str(decision),
                                         accepted=accepted, actor="marcus",
                                         channel="chat")
        cover("human:accepted" if accepted else "human:refused")
        if accepted:
            self.proved.add(cid)
            self.refuted.discard(cid)
        else:
            self.proved.discard(cid)
            self.refuted.add(cid)
        return "human"

    async def op_plan(self) -> str:
        mem = self.nova.memory
        if self.goal is None:
            self.goal = str(await mem.create_goal(
                project_name=self.slug, title=f"{self.slug} work",
                objective="work"))
            self.goal_live = True
        tid = await mem.enqueue_goal_task(goal_id=self.goal,
                                          project_name=self.slug,
                                          tool_name="demo.step")
        if self.goal_live and tid is None:
            FAILURES.append(f"{self.slug}: a live goal refused a task")
        if tid is not None:
            self.queued.append(str(tid))
        return "plan"

    async def op_claim(self) -> str:
        mem = self.nova.memory
        c = await mem.claim_next_goal_task()
        # What SHOULD be claimable: a queued task on a goal that is still
        # live and still on the generation the task was created under.
        expected = bool(self.queued) and self.goal_live
        if c is None:
            cover("claim:miss")
            if expected:
                FAILURES.append(
                    f"{self.slug}: {len(self.queued)} queued on a live goal, "
                    f"nothing claimable")
            return "claim"
        cover("claim:hit")
        # The other direction matters just as much, and nothing checked it
        # until a mutation asked: work handed out that should NOT have been
        # is how a cancelled goal keeps running.
        if not expected:
            FAILURES.append(
                f"{self.slug}: claimed {str(c['task_id'])[:8]} when nothing "
                f"should have been runnable (queued={len(self.queued)}, "
                f"goal_live={self.goal_live})")
        if str(c["project_name"]) != self.slug:
            FAILURES.append(
                f"{self.slug}: claimed {c['project_name']}'s task instead")
        if str(c["task_id"]) in self.queued:
            self.queued.remove(str(c["task_id"]))
        self.claimed_rows.append(c)
        return "claim"

    async def op_finish(self) -> str:
        if not self.claimed_rows:
            return ""
        mem = self.nova.memory
        claimed = self.claimed_rows.pop()
        claimed_gen = int(claimed["generation"])
        # Sometimes report against the generation the worker started on even
        # though the world has moved: that is the whole point of the fence.
        gen = claimed_gen if self.rng.random() < 0.8 else claimed_gen + 5
        status = self.rng.choice(["done", "done", "failed"])
        outcome = await mem.complete_goal_task(
            task_id=str(claimed["task_id"]), status=status,
            result=({"ok": True} if status == "done" else None),
            error=("broke" if status == "failed" else ""),
            expected_generation=gen)
        cover(f"complete:{outcome}")
        owns = (gen == claimed_gen == self.generation) and self.goal_live
        if owns and outcome != "applied":
            FAILURES.append(
                f"{self.slug}: live work returned {outcome!r}")
        if not owns and outcome == "applied":
            FAILURES.append(
                f"{self.slug}: stale work was APPLIED "
                f"(gen {gen} vs {self.generation})")
        self.last_finished = str(claimed["task_id"])
        return "finish"

    async def op_report_again(self) -> str:
        """A worker reporting a second time for work that already ended.

        This is the duplicate-callback hazard the row guard exists for: the
        row is no longer `running`, so the report must be refused outright.
        """
        if self.last_finished is None:
            return ""
        outcome = await self.nova.memory.complete_goal_task(
            task_id=self.last_finished, status="done", result={"ok": True},
            expected_generation=self.generation)
        cover(f"complete:{outcome}")
        if outcome == "applied":
            FAILURES.append(
                f"{self.slug}: a duplicate report was APPLIED to "
                f"{self.last_finished[:8]}")
        return "report_again"

    async def op_cancel(self) -> str:
        if self.goal is None or not self.goal_live:
            return ""
        await self.nova.memory.cancel_goal(goal_id=self.goal)
        self.generation += 1
        self.goal_live = False
        self.queued.clear()
        cover("goal:cancelled")
        # A task claimed BEFORE the cancel is now a worker from a dead run.
        # It is deliberately left claimed: reporting it is how `superseded`
        # gets exercised at all.
        return "cancel"

    async def op_goal_status(self) -> str:
        if self.goal is None or not self.goal_live:
            return ""
        status = self.rng.choice(["completed", "failed"])
        await self.nova.memory.update_goal_status(goal_id=self.goal,
                                                  status=status)
        self.goal_live = False
        self.queued.clear()
        cover(f"goal:{status}")
        return "goal_status"

    async def op_permission(self) -> str:
        broker = self.nova.runtime._permission_broker
        raised = await broker.request("project.delete",
                                      details={"project": self.slug,
                                               "name": self.slug})
        rid = str(raised.get("request_id") or "")
        if not rid:
            FAILURES.append(f"{self.slug}: permission was not raised")
            return "permission"
        approve = self.rng.random() < 0.5
        broker.resolve(rid, approve)
        cover("permission:approved" if approve else "permission:rejected")
        settled = broker.settled_as(rid)
        if settled != ("approved" if approve else "rejected"):
            FAILURES.append(f"{self.slug}: decision recorded as {settled!r}")
        if not (self.path / "PROJECT.md").exists():
            FAILURES.append(f"{self.slug}: approving a request deleted it")
        return "permission"

    # ── the invariant checked after EVERY operation ────────────────────────

    async def audit(self, op: str) -> None:
        v = await self.nova.runtime.completion.evaluate(slug=self.slug)
        cover(f"state:{v.state}") if f"state:{v.state}" in COVER else None
        satisfied = self.sealed and self.criteria and all(
            c in self.proved for c in self.criteria)
        if v.state == COMPLETE and not satisfied:
            FAILURES.append(
                f"{self.slug} after {op}: COMPLETE with "
                f"{len(self.proved)}/{len(self.criteria)} proved, "
                f"sealed={self.sealed}")
        if satisfied and self.has_code and v.state != COMPLETE:
            FAILURES.append(
                f"{self.slug} after {op}: every criterion proved but state is "
                f"{v.state}")
        # MEASURED, and the generator found it: a project with NOTHING BUILT
        # stays `planned` however much evidence is filed against it. Proof
        # without an artifact is not completion, and the model said COMPLETE
        # here until production said otherwise -- 11 violations, all of them
        # the code-less fifth of the projects.
        if satisfied and not self.has_code:
            if v.state == COMPLETE:
                FAILURES.append(
                    f"{self.slug} after {op}: COMPLETE with nothing built")
            elif v.state != "planned":
                FAILURES.append(
                    f"{self.slug} after {op}: nothing built, everything "
                    f"proved, expected planned but got {v.state}")
        if self.refuted and v.state == COMPLETE:
            FAILURES.append(
                f"{self.slug} after {op}: COMPLETE with a refuted criterion")


OPS = ("op_request", "op_verdict", "op_human", "op_plan", "op_claim",
       "op_finish", "op_report_again", "op_cancel", "op_goal_status",
       "op_permission")


def applicable(seq: "Sequence") -> list[str]:
    """The operations that can do something from here.

    Choosing uniformly from every operation spends almost the whole run on
    no-ops -- measured at 40 sequences: 24 empty claims to 1 hit, so every
    completion outcome went unreached and the coverage gate caught it. This
    walks the reachable state space instead. The refusal paths are NOT lost:
    a sixth of the time an operation is chosen with no regard for its
    preconditions, which is what still produces empty claims, duplicate
    reports and stale generations.
    """
    ops = ["op_request", "op_permission"]
    if seq.criteria:
        ops += ["op_verdict", "op_verdict", "op_human"]
    if seq.goal is None or seq.goal_live:
        ops += ["op_plan", "op_plan"]
    if seq.queued and seq.goal_live:
        ops += ["op_claim", "op_claim", "op_claim"]
    if seq.claimed_rows:
        ops += ["op_finish", "op_finish"]
    if seq.last_finished is not None:
        ops.append("op_report_again")
    if seq.goal_live:
        ops += ["op_cancel", "op_goal_status"]
    return ops


async def main() -> None:
    seed = int(os.getenv("NOVA_S15_SEED", "20260903"))
    rng = random.Random(seed)
    check.section(f"{SEQUENCES} generated sequences (seed {seed})")

    async with boot(default_reply="Sure.") as nova:
        svc = nova.runtime.completion
        # The witness: set up once, never touched again, re-read after every
        # operation of every sequence.
        wpath = nova.projects_dir / "witness"
        wpath.mkdir(parents=True, exist_ok=True)
        (wpath / "PROJECT.md").write_text("# witness\n\n## Status\nidea\n",
                                          encoding="utf-8")
        (wpath / "main.py").write_text("def add(a, b):\n    return a + b\n",
                                       encoding="utf-8")
        wrev = await svc.record_request(slug="witness",
                                        request_text="adds numbers")
        wids = await svc.set_criteria(slug="witness", revision=wrev, criteria=[
            {"text": "adds numbers", "origin_quote": "adds numbers",
             "verify_kind": "machine"}])
        await svc.seal_contract(slug="witness", revision=wrev)
        ctx = await svc.begin_check(slug="witness", criterion_id=wids[0])
        await svc.record_verdict(context=ctx, verdict=PASSED)
        witness = await svc.evaluate(slug="witness")
        check(witness.state == COMPLETE,
              f"the witness starts complete ({witness.state})")

        ops_run = 0
        for n in range(SEQUENCES):
            seq = Sequence(nova, rng, n)
            # Nothing may leak in from the sequence before this one.
            leaked = await nova.memory.claim_next_goal_task()
            if leaked is not None:
                FAILURES.append(
                    f"seq {n}: claimed {leaked['project_name']}'s task before "
                    f"doing anything")
            for _ in range(rng.randint(*OPS_PER_SEQUENCE)):
                name = (rng.choice(OPS) if rng.random() < 1 / 6
                        else rng.choice(applicable(seq)))
                did = await getattr(seq, name)()
                if not did:
                    continue
                ops_run += 1
                await seq.audit(did)
                w = await svc.evaluate(slug="witness")
                if w.state != witness.state:
                    FAILURES.append(
                        f"seq {n} {did}: the witness moved "
                        f"{witness.state} -> {w.state}")
                    witness = w
            # Leave nothing runnable behind, so the next sequence starts clean.
            if seq.goal is not None and seq.goal_live:
                await nova.memory.cancel_goal(goal_id=seq.goal)

        check(not FAILURES,
              f"{len(FAILURES)} invariant violation(s) across {ops_run} "
              f"operations" + ("" if not FAILURES
                               else ": " + "; ".join(FAILURES[:5])))
        check(ops_run >= SEQUENCES * 2,
              f"{ops_run} operations across {SEQUENCES} sequences")

        check.section("coverage — every branch this file claims to exercise")
        missing = sorted(k for k, v in COVER.items() if v == 0)
        for key in sorted(COVER):
            if COVER[key] == 0:
                print(f"  NEVER REACHED: {key}")
        print("  " + "  ".join(f"{k}={v}" for k, v in sorted(COVER.items())
                               if v))
        check(not missing,
              f"every counted branch was reached at least once"
              + ("" if not missing else f"; never reached: {missing}"))
        final = await svc.evaluate(slug="witness")
        check(final.state == COMPLETE,
              f"and the witness is exactly where it started ({final.state})")
    check.finish()


if __name__ == "__main__":
    run(main)
