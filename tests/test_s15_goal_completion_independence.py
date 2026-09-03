"""Stage 15 — goal/task success cannot manufacture completion, and vice versa.

WHAT THIS SEAM ACTUALLY IS, measured before anything was written. The only
production writer of acceptance evidence is `ProjectBuilder._validate_criteria`,
which runs a check script per criterion against the artifact. Goal rows and task
rows never feed it. So there is no "task result -> goal evidence -> criterion"
pipeline to test; the two axes are independent, and the thing worth proving is
that the independence holds in BOTH directions:

    goal done + tasks done + tools ok + files on disk  -> completion: idea
    goal failed                                        -> completion: complete

Both were measured. The first is the important one: a whole goal succeeding,
with real files written, manufactures no completion at all. The second is the
mirror -- a goal is a plan of work, and a plan failing does not un-deliver
something the acceptance criteria demonstrate.

Every assertion inspects the goal row, the task rows, the criterion and
evidence identifiers, the artifact digest, the completion state AND its reason,
because a suite that reads only the final state cannot tell a correct
derivation from two mistakes cancelling out.

  I10  tool success does not imply task/goal/project completion
  I11  completion requires current acceptance evidence
  I12  stale evidence cannot affect current completion
  I28  project A never satisfies project B
  I37  no subsystem silently converts partial into complete

Run:  venv\\Scripts\\python.exe tests\\test_s15_goal_completion_independence.py
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

from harness import Checks, run  # noqa: E402

from core.completion import COMPLETE, FAILED, FAILING, PASSED  # noqa: E402
from core.completion_events import CompletionAnnouncer  # noqa: E402
from core.completion_service import CompletionService  # noqa: E402
from memory.unifier import MemoryUnifier  # noqa: E402
from s15_bus import Recorder  # noqa: E402

check = Checks()


def _tmp():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


class World:
    """A store, a projects dir, and a completion service over both."""

    def __init__(self, root: Path):
        self.root = root
        self.projects = root / "projects"

    async def open(self) -> "World":
        self.mem = MemoryUnifier(self.root / "memory_data", enable_chroma=False)
        await self.mem.initialize()
        self.svc = CompletionService(memory=self.mem, projects_dir=self.projects)
        return self

    def write(self, slug: str, body: str, name: str = "main.py") -> None:
        (self.projects / slug).mkdir(parents=True, exist_ok=True)
        (self.projects / slug / name).write_text(body, encoding="utf-8")

    async def succeed_a_whole_goal(self, slug: str, steps: int = 3):
        """A goal whose every task ran and succeeded."""
        goal = await self.mem.create_goal(project_name=slug, title="ship it",
                                          objective="ship it")
        for n in range(steps):
            await self.mem.enqueue_goal_task(goal_id=goal, project_name=slug,
                                             tool_name=f"demo.step{n}")
        for _ in range(steps):
            c = await self.mem.claim_next_goal_task()
            await self.mem.complete_goal_task(
                task_id=str(c["task_id"]), status="done",
                result={"ok": True, "wrote": "main.py"},
                expected_generation=int(c["generation"]))
        await self.mem.update_goal_status(goal_id=goal, status="done")
        return goal

    async def goal_row(self, goal_id):
        rows = await self.mem.list_goals(limit=50)
        return next((g for g in rows if str(g["goal_id"]) == str(goal_id)), {})

    async def contract(self, slug: str, request: str, criteria: list[str]):
        rev = await self.svc.record_request(slug=slug, request_text=request)
        ids = await self.svc.set_criteria(slug=slug, revision=rev, criteria=[
            {"text": c, "origin_quote": c, "verify_kind": "machine"}
            for c in criteria])
        await self.svc.seal_contract(slug=slug, revision=rev)
        return rev, ids

    async def prove(self, slug: str, cid: str, verdict: str = PASSED):
        ctx = await self.svc.begin_check(slug=slug, criterion_id=cid)
        await self.svc.record_verdict(context=ctx, verdict=verdict,
                                      error="did not hold" if verdict == FAILED else "")
        return ctx


# ── the independence, both ways ────────────────────────────────────────────

async def test_a_whole_successful_goal_completes_nothing():
    check.section("I10 every task done, files written, completion untouched")
    with _tmp() as td:
        w = await World(Path(td)).open()
        w.write("alpha", "def add(a, b):\n    return a + b\n")
        goal = await w.succeed_a_whole_goal("alpha")

        g = await w.goal_row(goal)
        tasks = await w.mem.list_goal_tasks(goal_id=str(goal))
        check(str(g.get("status")) == "done",
              f"the goal really is done ({g.get('status')!r})")
        check(len(tasks) == 3 and all(t["status"] == "done" for t in tasks),
              f"and every task really succeeded "
              f"({[t['status'] for t in tasks]})")
        check(all("ok" in str(t.get("result") or t.get("result_json") or "")
                  for t in tasks),
              "with a successful tool result recorded on each")
        check((w.projects / "alpha" / "main.py").exists(),
              "and the file it claims to have written is on disk")

        v = await w.svc.evaluate(slug="alpha")
        check(v.state != COMPLETE,
              f"and the project is NOT complete ({v.state})")
        check("no durable requirement" in " ".join(v.reasons),
              f"because nobody agreed what done means ({v.reasons[:1]})")


async def test_criteria_without_evidence_are_not_satisfied_by_the_goal():
    check.section("I11 a sealed contract still needs its evidence")
    with _tmp() as td:
        w = await World(Path(td)).open()
        w.write("alpha", "def add(a, b):\n    return a + b\n")
        goal = await w.succeed_a_whole_goal("alpha")
        rev, ids = await w.contract(
            "alpha", "a tool that adds numbers and subtracts numbers",
            ["adds numbers", "subtracts numbers"])

        v = await w.svc.evaluate(slug="alpha")
        check(v.state != COMPLETE,
              f"a done goal does not satisfy the contract ({v.state})")
        outstanding = [s.criterion.text for s in v.outstanding]
        check(len(outstanding) == 2,
              f"both criteria are outstanding ({outstanding})")

        # One of the two proven: still not complete, and the reason NAMES the
        # other. A successful substep is not the whole thing.
        await w.prove("alpha", ids[0])
        v = await w.svc.evaluate(slug="alpha")
        check(v.state != COMPLETE,
              f"one of two proven is not complete ({v.state})")
        named = [s.criterion.text for s in v.outstanding + v.failing]
        check(any("subtracts" in t for t in named),
              f"and what is missing is named ({named})")

        # The failing case names it too, and is a different state.
        await w.prove("alpha", ids[1], verdict=FAILED)
        v = await w.svc.evaluate(slug="alpha")
        check(v.state == FAILING,
              f"a refuted criterion is failing, not merely incomplete "
              f"({v.state})")
        check(any("subtracts" in s.criterion.text for s in v.failing),
              f"naming the refuted one ({[s.criterion.text for s in v.failing]})")

        # And only when both hold does it complete.
        await w.prove("alpha", ids[1], verdict=PASSED)
        v = await w.svc.evaluate(slug="alpha")
        check(v.state == COMPLETE, f"both proven completes it ({v.state})")


async def test_a_failed_goal_does_not_revoke_a_demonstrated_contract():
    """The mirror direction, measured and asserted deliberately.

    A goal is a plan of work. A plan failing does not un-deliver something the
    acceptance criteria demonstrate about the artifact that exists. Both facts
    are true at once, and the honest requirement is that BOTH are available --
    not that one silently overrides the other.
    """
    check.section("I10 a failed plan does not un-deliver a proven artifact")
    with _tmp() as td:
        w = await World(Path(td)).open()
        w.write("alpha", "def add(a, b):\n    return a + b\n")
        goal = await w.succeed_a_whole_goal("alpha")
        rev, ids = await w.contract("alpha", "a tool that adds numbers",
                                    ["adds numbers"])
        await w.prove("alpha", ids[0])
        check((await w.svc.evaluate(slug="alpha")).state == COMPLETE,
              "the contract is demonstrated")

        await w.mem.update_goal_status(goal_id=goal, status="failed")
        g = await w.goal_row(goal)
        v = await w.svc.evaluate(slug="alpha")
        check(str(g.get("status")) == "failed",
              f"the goal is now failed ({g.get('status')!r})")
        check(v.state == COMPLETE,
              f"and the completion state is unchanged ({v.state})")

        # Both facts must be reachable, so an answer cannot be built from half.
        work = await w.mem.describe_work_state(project_name="alpha")
        record = await w.svc.describe_for_chat(slug="alpha")
        check("failed" in work,
              f"the work summary says the goal failed ({work[:60]!r})")
        check("complete" in record,
              f"and the completion record says complete ({record[:60]!r})")


# ── the seam's edges ───────────────────────────────────────────────────────

async def test_another_projects_evidence_cannot_satisfy_this_one():
    check.section("I28 identical work in two projects stays separate")
    with _tmp() as td:
        w = await World(Path(td)).open()
        for slug in ("alpha", "bravo"):
            w.write(slug, "def add(a, b):\n    return a + b\n")
            await w.succeed_a_whole_goal(slug)
        _, a_ids = await w.contract("alpha", "a tool that adds numbers",
                                    ["adds numbers"])
        _, b_ids = await w.contract("bravo", "a tool that adds numbers",
                                    ["adds numbers"])
        check(a_ids[0] != b_ids[0],
              "the two projects' criteria have different identities")

        await w.prove("alpha", a_ids[0])
        va = await w.svc.evaluate(slug="alpha")
        vb = await w.svc.evaluate(slug="bravo")
        check(va.state == COMPLETE, f"alpha is complete ({va.state})")
        check(vb.state != COMPLETE,
              f"and bravo is not, despite identical files and an identical "
              f"criterion ({vb.state})")

        ev_b = await w.mem.list_acceptance_evidence(project_name="bravo")
        check(not ev_b, f"bravo has no evidence of its own ({len(ev_b)})")


async def test_a_correction_invalidates_what_was_proven():
    check.section("I12 old evidence cannot satisfy a new revision")
    with _tmp() as td:
        w = await World(Path(td)).open()
        w.write("alpha", "def add(a, b):\n    return a + b\n")
        goal = await w.succeed_a_whole_goal("alpha")
        rev1, ids = await w.contract("alpha", "a tool that adds numbers",
                                     ["adds numbers"])
        await w.prove("alpha", ids[0])
        check((await w.svc.evaluate(slug="alpha")).state == COMPLETE,
              "complete at the first revision")

        rev2 = await w.svc.record_request(
            slug="alpha", request_text="a tool that adds numbers and "
                                       "subtracts numbers")
        v = await w.svc.evaluate(slug="alpha")
        check(int(v.revision) == rev2,
              f"the requirement moved on ({v.revision} vs {rev2})")
        check(v.state != COMPLETE,
              f"and the old evidence does not satisfy it ({v.state})")

        # The goal is still 'done' throughout -- it cannot rescue this.
        g = await w.goal_row(goal)
        check(str(g.get("status")) == "done",
              f"even though the goal still says done ({g.get('status')!r})")

        moved = await w.svc.carry_forward(slug="alpha", from_revision=rev1,
                                          to_revision=rev2)
        fresh_ids = await w.svc.set_criteria(
            slug="alpha", revision=rev2,
            criteria=[{"text": "subtracts numbers",
                       "origin_quote": "subtracts numbers",
                       "verify_kind": "machine"}])
        await w.svc.seal_contract(slug="alpha", revision=rev2)
        for cid in list(moved) + list(fresh_ids):
            await w.prove("alpha", cid)
        v = await w.svc.evaluate(slug="alpha")
        check(v.state == COMPLETE,
              f"only new evidence at the new revision completes it ({v.state})")


async def test_artifact_drift_beats_a_done_goal():
    check.section("I12 the file changed; the goal has no say in it")
    with _tmp() as td:
        w = await World(Path(td)).open()
        w.write("alpha", "def add(a, b):\n    return a + b\n")
        goal = await w.succeed_a_whole_goal("alpha")
        _, ids = await w.contract("alpha", "a tool that adds numbers",
                                  ["adds numbers"])
        ctx = await w.prove("alpha", ids[0])
        digest_when_proven = ctx.artifact_digest
        check((await w.svc.evaluate(slug="alpha")).state == COMPLETE,
              "complete against the file as it was")

        w.write("alpha", "def add(a, b):\n    return a - b   # wrong now\n")
        v = await w.svc.evaluate(slug="alpha")
        check(v.state != COMPLETE,
              f"an edit invalidates it ({v.state})")
        stale = [s.stale_reason for s in v.criteria if s.stale_reason]
        check(any(digest_when_proven[:8] in s for s in stale),
              f"naming the artifact it was proven against "
              f"({[s[:70] for s in stale]})")
        g = await w.goal_row(goal)
        check(str(g.get("status")) == "done",
              "while the goal still says done, and does not matter")


async def test_the_announcement_follows_the_evidence_not_the_goal():
    check.section("I38 announced once, and not by the goal")
    with _tmp() as td:
        root = Path(td)
        w = await World(root).open()
        w.write("alpha", "def add(a, b):\n    return a + b\n")
        goal = await w.succeed_a_whole_goal("alpha")
        _, ids = await w.contract("alpha", "a tool that adds numbers",
                                  ["adds numbers"])

        # A done goal, before any evidence: announcing must claim nothing.
        with Recorder() as rec:
            v = await w.svc.evaluate(slug="alpha")
            await CompletionAnnouncer(memory=w.mem).announce(slug="alpha",
                                                             verdict=v)
            check(not rec.for_project("project.completed", "alpha"),
                  f"a done goal announces no completion "
                  f"({len(rec.for_project('project.completed', 'alpha'))})")
            # LIVENESS. An empty list is also what a dead recorder returns, so
            # prove this one was receiving: the same announce publishes a
            # state_changed, and that must be here.
            check(rec.for_project("project.state_changed", "alpha"),
                  f"and the recorder really was receiving ({rec.kinds()})")

        await w.prove("alpha", ids[0])
        with Recorder() as rec:
            v = await w.svc.evaluate(slug="alpha")
            for _ in range(4):
                await CompletionAnnouncer(memory=w.mem).announce(slug="alpha",
                                                                  verdict=v)
            done = rec.for_project("project.completed", "alpha")
            check(len(done) == 1,
                  f"evidence announces it exactly once, however many "
                  f"announcers ask ({len(done)})")
            check(str(done[0].data.get("state")) == COMPLETE if done else False,
                  "and the payload says complete")

        # A correction: the ledger must not leave the old announcement looking
        # current for the NEW revision.
        rev2 = await w.svc.record_request(
            slug="alpha", request_text="a tool that adds numbers and more")
        with Recorder() as rec:
            v2 = await w.svc.evaluate(slug="alpha")
            await CompletionAnnouncer(memory=w.mem).announce(slug="alpha",
                                                              verdict=v2)
            check(v2.state != COMPLETE,
                  f"the new revision is not complete ({v2.state})")
            check(not rec.for_project("project.completed", "alpha"),
                  "so nothing announces completion for it")
            check(rec.for_project("project.state_changed", "alpha"),
                  f"and again the recorder was live ({rec.kinds()})")


async def main() -> None:
    await test_a_whole_successful_goal_completes_nothing()
    await test_criteria_without_evidence_are_not_satisfied_by_the_goal()
    await test_a_failed_goal_does_not_revoke_a_demonstrated_contract()
    await test_another_projects_evidence_cannot_satisfy_this_one()
    await test_a_correction_invalidates_what_was_proven()
    await test_artifact_drift_beats_a_done_goal()
    await test_the_announcement_follows_the_evidence_not_the_goal()
    check.finish()


if __name__ == "__main__":
    run(main)
