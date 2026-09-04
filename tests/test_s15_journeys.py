"""Stage 15 — long integrated journeys across every authoritative axis.

Nova holds two independent truths: what the WORK did (goals, tasks,
generations) and what the ARTIFACT proved (requirements, criteria, evidence).
Stage 15 established that there is no pipeline between them. These journeys
therefore drive both at once and, on purpose, drive them APART -- successful
work with no contract, failed work with complete evidence, a correction that
demolishes a completed artifact while the work plan is untouched -- and then
ask whether Nova can still describe its world without one axis quietly
answering for the other.

COUNTING. A transition is counted when an authoritative value READ BACK from
storage differs from the last value read for that same key. Keys are
identities, not labels: `completion:alpha`, `goal:<uuid>:status`,
`task:<uuid>:status`, `revision:bravo`, `permission:<rid>`. Reading twice
counts nothing. Asserting twice counts nothing. Acting without changing
anything counts nothing. The total is computed by diffing snapshots, never
typed by hand, and printed at the end with its breakdown by axis.

  J1  work succeeds; nothing was ever promised
  J2  work succeeds; the contract is only half proved
  J3  work fails; the artifact is complete anyway
  J4  a correction demolishes a completed artifact
  J5  background failure beside an independently complete foreground artifact
  J6  a restart with the two axes already disagreeing
  J7  two projects, step for step, with a cancellation between them
  J8  one project up the whole ladder, with work running alongside
  J9  a destructive request, a deferral, and a permission, across two projects
  J10 a four-goal sprint, with the request changing halfway
  J11 three projects, three different endings, side by side
  J12 six decisions, two projects, and work between every one
  J13 ten corrections, each demolishing the proof before it
  J14 ten goals ending four different ways

Run:  venv\\Scripts\\python.exe tests\\test_s15_journeys.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, boot, run  # noqa: E402

from core.completion import (  # noqa: E402
    COMPLETE, FAILED, HUMAN_PENDING, INCONCLUSIVE, PASSED, WAIVED,
)

check = Checks()


# ── the ledger ─────────────────────────────────────────────────────────────

class World:
    """Authoritative values, by identity, and every move between them."""

    def __init__(self, nova, name: str) -> None:
        self.nova = nova
        self.name = name
        self.seen: dict[str, str] = {}
        #: (key, before, after, cause) for every COUNTED move.
        self.moves: list[tuple[str, str, str, str]] = []

    async def read(self) -> dict[str, str]:
        n = self.nova
        out: dict[str, str] = {}
        for slug in sorted(p.name for p in n.projects_dir.iterdir()
                           if p.is_dir()):
            out[f"project:{slug}:exists"] = str(
                (n.projects_dir / slug / "PROJECT.md").exists())
            v = await n.runtime.completion.evaluate(slug=slug)
            out[f"completion:{slug}"] = str(v.state)
            out[f"reason:{slug}"] = (str(v.reasons[0])
                                     if getattr(v, "reasons", None) else "")
            req = await n.memory.current_requirement(project_name=slug)
            out[f"revision:{slug}"] = str(req["revision"]) if req else "-"
            out[f"sealed:{slug}"] = str(bool(req and req.get("sealed_at")))
        for g in await n.memory.list_goals(limit=50):
            gid = str(g["goal_id"])
            out[f"goal:{gid}:status"] = str(g["status"])
            out[f"goal:{gid}:generation"] = str(g["generation"])
            for t in await n.memory.list_goal_tasks(goal_id=gid):
                tid = str(t["task_id"])
                out[f"task:{tid}:status"] = str(t["status"])
                out[f"task:{tid}:outcome"] = str(t.get("outcome") or "")
        broker = n.runtime._permission_broker
        for r in broker.pending():
            out[f"permission:{r['request_id']}"] = "pending"
        for rid in list(self.seen):
            if rid.startswith("permission:") and rid not in out:
                out[rid] = broker.settled_as(rid.split(":", 1)[1]) or "gone"
        return out

    async def step(self, cause: str) -> int:
        """Read everything back; count only what actually moved."""
        now = await self.read()
        moved = 0
        for key, value in now.items():
            was = self.seen.get(key)
            if was is None:
                # First sight of an identity is where it already was, not a
                # move. Counting it would make every creation worth a free
                # transition per field.
                self.seen[key] = value
                continue
            if was != value:
                self.moves.append((key, was, value, cause))
                self.seen[key] = value
                moved += 1
        return moved

    def rebind(self, nova) -> None:
        """A restart continues the same journey against a new process state."""
        self.nova = nova

    @staticmethod
    def entity(key: str) -> str:
        """The thing that moved, not the field that showed it.

        `goal:<id>:status` and `goal:<id>:generation` changing in one action
        are ONE transition of one goal, counted once. `reason:<slug>` is a
        projection of the same evaluation as `completion:<slug>` and is
        evidence, never a transition of its own. `sealed` belongs to the
        requirement act that moved the revision.
        """
        head, _, rest = key.partition(":")
        if head == "reason":
            return ""                       # never counted
        if head in ("revision", "sealed"):
            return f"requirement:{rest}"
        if head in ("goal", "task"):
            return f"{head}:{rest.split(':', 1)[0]}"
        if head == "project":
            return f"project:{rest.split(':', 1)[0]}"
        return key

    @property
    def n(self) -> int:
        """Distinct (entity, action) pairs: what MOVED, once each."""
        return len({(self.entity(k), cause)
                    for k, _, _, cause in self.moves if self.entity(k)})

    def by_axis(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e, _ in {(self.entity(k), c)
                     for k, _, _, c in self.moves if self.entity(k)}:
            axis = e.split(":", 1)[0]
            out[axis] = out.get(axis, 0) + 1
        return out

    def crossings(self) -> list[tuple[str, tuple[str, ...]]]:
        """Actions where MORE THAN ONE capability moved at the same time.

        The strictest reading of "cross-capability transition": not a journey
        that happens to touch several subsystems over its life, but a single
        authoritative action after which two or more of them were observed in
        a new state. Reported separately so the headline count is never doing
        this number's work.
        """
        by_cause: dict[str, set[str]] = {}
        for key, _, _, cause in self.moves:
            e = self.entity(key)
            if e:
                by_cause.setdefault(cause, set()).add(e.split(":", 1)[0])
        return [(c, tuple(sorted(a))) for c, a in by_cause.items()
                if len(a) > 1]

    def of(self, prefix: str) -> list[tuple[str, str, str, str]]:
        return [m for m in self.moves if m[0].startswith(prefix)]


WORLDS: list[World] = []


def ledger(nova, name: str) -> World:
    w = World(nova, name)
    WORLDS.append(w)
    return w


# ── shared fixtures ────────────────────────────────────────────────────────

def seed(nova, slug: str, *, body: str = "def add(a, b):\n    return a + b\n"):
    p = nova.projects_dir / slug
    p.mkdir(parents=True, exist_ok=True)
    (p / "PROJECT.md").write_text(f"# {slug}\n\n## Status\nidea\n",
                                  encoding="utf-8")
    (p / "main.py").write_text(body, encoding="utf-8")
    return p


async def contract(nova, slug: str, texts: list[str], *,
                   kinds: list[str] | None = None, seal: bool = True):
    svc = nova.runtime.completion
    rev = await svc.record_request(
        slug=slug, request_text=" and ".join(texts) or "a tool")
    kinds = kinds or ["machine"] * len(texts)
    ids = await svc.set_criteria(slug=slug, revision=rev, criteria=[
        {"text": t, "origin_quote": t, "verify_kind": k}
        for t, k in zip(texts, kinds)])
    if seal:
        await svc.seal_contract(slug=slug, revision=rev)
    return rev, ids


async def verdict(nova, slug: str, criterion: str, value: str, *,
                  error: str = "") -> None:
    svc = nova.runtime.completion
    ctx = await svc.begin_check(slug=slug, criterion_id=criterion)
    await svc.record_verdict(context=ctx, verdict=value, error=error)


async def plan(nova, slug: str, titles: list[str]):
    """A goal with one task per title, all queued."""
    g = await nova.memory.create_goal(project_name=slug, title=titles[0],
                                      objective=f"{slug}: {titles[0]}")
    ids = []
    for t in titles:
        ids.append(await nova.memory.enqueue_goal_task(
            goal_id=g, project_name=slug, tool_name=f"demo.{t}"))
    return g, ids


async def do_next(nova, w=None, *, status: str = "done", error: str = ""):
    """Claim the next task and finish it, honestly, on its own generation.

    The claim is its own authoritative state -- `running`, owned by a
    generation, and the thing every fence in the system reasons about -- so
    the ledger is stepped through it rather than over it.
    """
    c = await nova.memory.claim_next_goal_task()
    if c is None:
        return None, "nothing claimable"
    if w is not None:
        await w.step(f"claimed {c['tool_name']}")
    outcome = await nova.memory.complete_goal_task(
        task_id=str(c["task_id"]), status=status,
        result=({"ok": True} if status == "done" else None),
        error=error, expected_generation=int(c["generation"]))
    if w is not None:
        await w.step(f"finished {c['tool_name']} ({status})")
    return c, outcome


# ── J1: work succeeds; nothing was ever promised ───────────────────────────

async def journey_1_work_without_a_contract() -> None:
    check.section("J1 the work succeeded; nothing was ever promised")
    async with boot(default_reply="Sure.") as nova:
        w = ledger(nova, "J1")
        seed(nova, "alpha")
        await w.step("seeded")

        g, tids = await plan(nova, "alpha",
                           ["design", "build", "check", "document",
                            "review", "ship"])
        await w.step("three tasks planned")
        for _ in range(6):
            c, outcome = await do_next(nova, w)
            check(outcome == "applied",
                  f"each step completes on its own generation ({outcome})")
            await w.step(f"{c['tool_name']} done")
        await nova.memory.update_goal_status(goal_id=g, status="completed")
        await w.step("goal completed")

        v = await nova.runtime.completion.evaluate(slug="alpha")
        check(v.state != COMPLETE,
              f"successful work does not make the artifact complete "
              f"({v.state})")
        check("no durable requirement" in (v.reasons[0] if v.reasons else ""),
              f"and the reason names the missing promise "
              f"({v.reasons[0] if v.reasons else ''!r})")

        said = await nova.runtime._project_builder.status_text("alpha")
        check("complete" not in said.split(".")[0].lower(),
              f"chat does not call it complete ({said[:80]!r})")
        work_state = await nova.memory.describe_work_state(project_name="alpha")
        check("completed" in work_state.lower() or "done" in work_state.lower(),
              f"while the work axis says the plan finished "
              f"({' '.join(work_state.split())[:90]!r})")
        check(not w.of("completion:"),
              f"no completion state moved in this whole journey "
              f"({w.of('completion:')})")
        check(w.n >= 8, f"J1 recorded {w.n} authoritative moves")


# ── J2: work succeeds; the contract is half proved ─────────────────────────

async def journey_2_partial_evidence() -> None:
    check.section("J2 the work succeeded; the contract is half proved")
    async with boot(default_reply="Sure.") as nova:
        w = ledger(nova, "J2")
        seed(nova, "alpha")
        rev, ids = await contract(nova, "alpha",
                                  ["adds numbers", "subtracts numbers",
                                   "reads nicely"],
                                  kinds=["machine", "machine", "human"])
        await w.step("contract sealed")

        g, _ = await plan(nova, "alpha",
                          ["build", "test", "lint", "package"])
        await w.step("work planned")
        for _ in range(4):
            _, outcome = await do_next(nova, w)
            check(outcome == "applied", f"work step applied ({outcome})")
            await w.step("work step done")
        await nova.memory.update_goal_status(goal_id=g, status="completed")
        await w.step("goal completed")

        await verdict(nova, "alpha", ids[0], PASSED)
        await w.step("first criterion proved")
        v = await nova.runtime.completion.evaluate(slug="alpha")
        check(v.state != COMPLETE,
              f"one proof out of three is not complete ({v.state})")
        check(len(v.outstanding) >= 1,
              f"and the outstanding ones are named ({len(v.outstanding)})")

        await verdict(nova, "alpha", ids[1], PASSED)
        await w.step("second criterion proved")
        v = await nova.runtime.completion.evaluate(slug="alpha")
        check(v.state != COMPLETE,
              f"the human one still stands in the way ({v.state})")

        # A machine may not waive a criterion -- measured: record_verdict
        # refuses WAIVED outright ("accepting one on a person's behalf is not
        # a machine's to do"). The only route is a question Nova asked and a
        # person redeemed, so that is the route this journey takes.
        svc = nova.runtime.completion
        decision = await svc.ask_human(slug="alpha", criterion_id=ids[2],
                                       prompt="Does it read nicely?")
        await w.step("human check requested")
        v = await nova.runtime.completion.evaluate(slug="alpha")
        check(v.state != COMPLETE,
              f"asking a person is not the same as an answer ({v.state})")

        await svc.resolve_human_decision(decision_id=decision, accepted=True,
                                         actor="marcus", channel="chat")
        await w.step("human accepted it")
        v = await nova.runtime.completion.evaluate(slug="alpha")
        check(v.state == COMPLETE,
              f"and now, with every criterion satisfied, it is ({v.state})")
        check(len(w.of("completion:alpha")) >= 2,
              f"the completion axis moved under its own evidence "
              f"({[m[1:3] for m in w.of('completion:alpha')]})")
        check(w.n >= 12, f"J2 recorded {w.n} authoritative moves")


# ── J3: the work failed; the artifact is complete ──────────────────────────

async def journey_3_failed_work_complete_artifact() -> None:
    check.section("J3 the work failed; the artifact is complete anyway")
    async with boot(default_reply="Sure.") as nova:
        w = ledger(nova, "J3")
        seed(nova, "alpha")
        rev, ids = await contract(nova, "alpha", ["adds numbers"])
        await w.step("contract sealed")

        g, _ = await plan(nova, "alpha",
                          ["generate", "polish", "retry", "retry-again"])
        await w.step("work planned")
        c, outcome = await do_next(nova, w, status="failed",
                                   error="the generator crashed")
        check(outcome == "applied",
              f"the failure is recorded as a real outcome ({outcome})")
        await w.step("first step failed")
        _, outcome = await do_next(nova, w, status="failed", error="gave up")
        await w.step("second step failed")
        await nova.memory.update_goal_status(goal_id=g, status="failed")
        await w.step("goal failed")

        # The artifact is nonetheless demonstrated.
        await verdict(nova, "alpha", ids[0], PASSED)
        await w.step("the criterion is proved")

        v = await nova.runtime.completion.evaluate(slug="alpha")
        goals = await nova.memory.list_goals(project_name="alpha")
        check(v.state == COMPLETE,
              f"the artifact satisfies what was agreed ({v.state})")
        check(str(goals[0]["status"]) == "failed",
              f"while the plan to build it failed ({goals[0]['status']})")

        said = await nova.runtime._project_builder.status_text("alpha")
        check("complete" in said.lower(),
              f"status reports the artifact truthfully ({said[:60]!r})")
        check("did not all succeed" in said or "failed" in said.lower(),
              f"AND says the work did not all succeed, in the same breath "
              f"({said[:150]!r})")
        check(w.n >= 6, f"J3 recorded {w.n} authoritative moves")


# ── J4: a correction demolishes a completed artifact ───────────────────────

async def journey_4_correction_after_complete() -> None:
    check.section("J4 a correction demolishes a completed artifact")
    async with boot(default_reply="Sure.") as nova:
        w = ledger(nova, "J4")
        seed(nova, "alpha")
        rev1, ids1 = await contract(nova, "alpha", ["adds numbers"])
        await w.step("first contract")
        g, _ = await plan(nova, "alpha", ["build", "verify", "tidy"])
        await do_next(nova, w)
        await w.step("the work is done")
        await verdict(nova, "alpha", ids1[0], PASSED)
        await w.step("proved")
        v = await nova.runtime.completion.evaluate(slug="alpha")
        check(v.state == COMPLETE, f"complete, on its own evidence ({v.state})")

        # "Actually, it should subtract too."
        svc = nova.runtime.completion
        rev2 = await svc.record_request(
            slug="alpha", request_text="adds numbers and subtracts numbers")
        ids2 = await svc.set_criteria(slug="alpha", revision=rev2, criteria=[
            {"text": "adds numbers", "origin_quote": "adds numbers",
             "verify_kind": "machine"},
            {"text": "subtracts numbers", "origin_quote": "subtracts numbers",
             "verify_kind": "machine"}])
        await svc.seal_contract(slug="alpha", revision=rev2)
        await w.step("the request changed")

        v = await nova.runtime.completion.evaluate(slug="alpha")
        check(rev2 == rev1 + 1, f"a new revision ({rev1} -> {rev2})")
        check(v.state != COMPLETE,
              f"and yesterday's proof no longer settles today's promise "
              f"({v.state})")
        goals = await nova.memory.list_goals(project_name="alpha")
        check(str(goals[0]["status"]) == "active",
              f"while the work plan is untouched by the correction "
              f"({goals[0]['status']})")

        await verdict(nova, "alpha", ids2[0], PASSED)
        await w.step("re-proved the old criterion")
        v = await nova.runtime.completion.evaluate(slug="alpha")
        check(v.state != COMPLETE,
              f"half of the new promise is still not all of it ({v.state})")
        await verdict(nova, "alpha", ids2[1], PASSED)
        await w.step("proved the new one")
        v = await nova.runtime.completion.evaluate(slug="alpha")
        check(v.state == COMPLETE,
              f"and the new contract is met on new evidence ({v.state})")
        check(len(w.of("revision:alpha")) == 1,
              f"exactly one revision move was recorded "
              f"({w.of('revision:alpha')})")
        check(w.n >= 6, f"J4 recorded {w.n} authoritative moves")


# ── J5: background failure beside a complete foreground artifact ───────────

async def journey_5_background_failure_foreground_complete() -> None:
    check.section("J5 B's work collapses; A's artifact is complete regardless")
    async with boot(default_reply="Sure.") as nova:
        w = ledger(nova, "J5")
        seed(nova, "alpha")
        seed(nova, "bravo")
        rev_a, ids_a = await contract(nova, "alpha", ["adds numbers"])
        rev_b, ids_b = await contract(nova, "bravo", ["draws a chart"])
        await w.step("two contracts")

        g_a, _ = await plan(nova, "alpha", ["build"])
        g_b, _ = await plan(nova, "bravo",
                            ["render", "export", "upload"])
        await w.step("two plans")

        # A finishes and proves itself.
        c, _ = await do_next(nova, w)
        check(str(c["project_name"]) == "alpha",
              f"A's task came first ({c['project_name']})")
        await w.step("A's work done")
        await nova.memory.update_goal_status(goal_id=g_a, status="completed")
        await verdict(nova, "alpha", ids_a[0], PASSED)
        await w.step("A proved")

        # B's collapses, twice, and its evidence refutes it.
        for note in ("renderer missing", "export died"):
            c, outcome = await do_next(nova, w, status="failed", error=note)
            check(str(c["project_name"]) == "bravo",
                  f"B's failures are B's ({c['project_name']})")
            await w.step(f"B failed: {note}")
        await nova.memory.update_goal_status(goal_id=g_b, status="failed")
        await verdict(nova, "bravo", ids_b[0], FAILED, error="no chart")
        await w.step("B refuted")

        v_a = await nova.runtime.completion.evaluate(slug="alpha")
        v_b = await nova.runtime.completion.evaluate(slug="bravo")
        check(v_a.state == COMPLETE and v_b.state != COMPLETE,
              f"the two artifacts end in different places "
              f"({v_a.state} vs {v_b.state})")
        said_a = await nova.runtime._project_builder.status_text("alpha")
        said_b = await nova.runtime._project_builder.status_text("bravo")
        check("bravo" not in said_a and "alpha" not in said_b,
              f"and neither borrows the other's name "
              f"({said_a[:40]!r} / {said_b[:40]!r})")
        alpha_moves = [m for m in w.moves if "alpha" in m[0]]
        bravo_moves = [m for m in w.moves if "bravo" in m[0]]
        check(alpha_moves and bravo_moves,
              f"both projects moved ({len(alpha_moves)} / {len(bravo_moves)})")
        check(w.n >= 10, f"J5 recorded {w.n} authoritative moves")


# ── J6: a restart with the two axes already disagreeing ────────────────────

async def journey_6_restart_with_disagreement() -> None:
    check.section("J6 a restart, with the axes already disagreeing")
    root = Path(tempfile.mkdtemp(prefix="nova-s15-j6-"))
    try:
        # First life. (Genuine separate INTERPRETERS are proved by
        # test_s15_restart_matrix.py; this is a real shutdown/startup cycle
        # against the same durable root, continuing one journey across it.)
        async with boot(root=root, default_reply="Sure.") as nova:
            w = ledger(nova, "J6")
            seed(nova, "alpha")
            rev, ids = await contract(nova, "alpha", ["adds numbers"])
            g, _ = await plan(nova, "alpha", ["build", "verify"])
            await w.step("planned and contracted")
            _, outcome = await do_next(nova, w, status="failed", error="crashed")
            await w.step("the work failed")
            await nova.memory.update_goal_status(goal_id=g, status="failed")
            await verdict(nova, "alpha", ids[0], PASSED)
            await w.step("the artifact is proved anyway")
            before = await nova.runtime.completion.evaluate(slug="alpha")
            check(before.state == COMPLETE,
                  f"complete before the restart ({before.state})")
            goal_id, criterion = str(g), str(ids[0])

        # Second life, same disk.
        async with boot(root=root, default_reply="Sure.") as nova2:
            w.rebind(nova2)
            moved = await w.step("restarted")
            after = await nova2.runtime.completion.evaluate(slug="alpha")
            goals = await nova2.memory.list_goals(project_name="alpha")
            check(after.state == before.state,
                  f"the artifact reconstructs to the same state "
                  f"({before.state} -> {after.state})")
            check(after.reasons[:1] == before.reasons[:1],
                  "for the same recorded reason")
            check(str(goals[0]["goal_id"]) == goal_id
                  and str(goals[0]["status"]) == "failed",
                  f"the failed plan is still that plan, still failed "
                  f"({goals[0]['status']})")
            check(moved == 0 or all("task:" in m[0] or "goal:" in m[0]
                                    for m in w.moves[-moved:]),
                  f"nothing but work rows moved across the restart "
                  f"({[m[0].split(':')[0] for m in w.moves[-moved:]]})")
            said = await nova2.runtime._project_builder.status_text("alpha")
            check("complete" in said.lower()
                  and ("did not all succeed" in said or "failed" in said.lower()),
                  f"and it still tells both halves of the truth "
                  f"({said[:120]!r})")
            check(w.n >= 5, f"J6 recorded {w.n} authoritative moves")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ── J7: two projects, step for step, with a cancellation ───────────────────

async def journey_7_two_projects_step_for_step() -> None:
    check.section("J7 two projects, step for step, one of them cancelled")
    async with boot(default_reply="Sure.") as nova:
        w = ledger(nova, "J7")
        seed(nova, "alpha")
        seed(nova, "bravo")
        rev_a, ids_a = await contract(nova, "alpha", ["adds numbers"])
        rev_b, ids_b = await contract(nova, "bravo", ["draws a chart"])
        g_a, _ = await plan(nova, "alpha", ["a1", "a2", "a3", "a4"])
        g_b, _ = await plan(nova, "bravo", ["b1", "b2", "b3", "b4"])
        await w.step("two worlds, side by side")

        c1, _ = await do_next(nova, w)
        await w.step("A's first step")
        gen_a = int(c1["generation"])

        await nova.memory.cancel_goal(goal_id=g_a)
        await w.step("A is cancelled mid-plan")
        goals = {str(g["project_name"]): g
                 for g in await nova.memory.list_goals(limit=10)}
        check(int(goals["alpha"]["generation"]) == gen_a + 1,
              f"A moves to a new generation "
              f"({gen_a} -> {goals['alpha']['generation']})")
        check(int(goals["bravo"]["generation"]) == gen_a,
              f"B stays on its own ({goals['bravo']['generation']})")

        # B's work carries on, untouched, and is the only thing claimable.
        for _ in range(4):
            c, outcome = await do_next(nova, w)
            check(c is not None and str(c["project_name"]) == "bravo",
                  f"only B's work is claimable now "
                  f"({(c or {}).get('project_name')})")
            check(outcome == "applied", f"and it applies ({outcome})")
            await w.step("B's step")
        c, why = await do_next(nova, w)
        check(c is None,
              f"nothing else is claimable at all ({why})")

        await nova.memory.update_goal_status(goal_id=g_b, status="completed")
        await verdict(nova, "bravo", ids_b[0], PASSED)
        await w.step("B finishes and proves itself")
        await verdict(nova, "alpha", ids_a[0], FAILED, error="never built")
        await w.step("A's criterion is refuted")

        v_a = await nova.runtime.completion.evaluate(slug="alpha")
        v_b = await nova.runtime.completion.evaluate(slug="bravo")
        check(v_b.state == COMPLETE and v_a.state != COMPLETE,
              f"the cancelled one is not complete; the finished one is "
              f"({v_a.state} / {v_b.state})")
        cross = [m for m in w.moves
                 if "alpha" in m[0] and "bravo" in (m[3] or "")]
        check(not cross, f"no move on A was caused by B ({cross})")
        check(w.n >= 15, f"J7 recorded {w.n} authoritative moves")


# ── J8: the whole ladder, with work running alongside ──────────────────────

async def journey_8_the_whole_ladder() -> None:
    check.section("J8 one project the whole way up, work running alongside")
    async with boot(default_reply="Sure.") as nova:
        w = ledger(nova, "J8")
        svc = nova.runtime.completion
        pb = nova.runtime._project_builder
        path = nova.projects_dir / "alpha"
        path.mkdir(parents=True, exist_ok=True)
        (path / "PROJECT.md").write_text("# alpha\n\n## Status\nidea\n",
                                         encoding="utf-8")
        await w.step("an empty project")
        v = await svc.evaluate(slug="alpha")
        check(v.state == "idea", f"nothing promised, nothing built ({v.state})")

        rev = await svc.record_request(
            slug="alpha", request_text="adds numbers and subtracts numbers")
        await w.step("a request is recorded")
        ids = await svc.set_criteria(slug="alpha", revision=rev, criteria=[
            {"text": "adds numbers", "origin_quote": "adds numbers",
             "verify_kind": "machine"},
            {"text": "subtracts numbers", "origin_quote": "subtracts numbers",
             "verify_kind": "machine"}])
        await svc.seal_contract(slug="alpha", revision=rev)
        await w.step("criteria agreed and sealed")
        v = await svc.evaluate(slug="alpha")
        check(v.state == "planned",
              f"promised, not yet built ({v.state})")

        g, _ = await plan(nova, "alpha",
                          ["scaffold", "implement", "fix", "harden"])
        await w.step("the work is planned")

        (path / "main.py").write_text("def add(a, b):\n    return a + b\n",
                                      encoding="utf-8")
        await do_next(nova, w)
        await w.step("scaffolded")
        v = await svc.evaluate(slug="alpha")
        check(v.state in ("scaffolded", "partially_implemented"),
              f"files exist, nothing proved ({v.state})")

        await verdict(nova, "alpha", ids[0], FAILED, error="off by one")
        await do_next(nova, w, status="failed", error="off by one")
        await w.step("a check refutes it, and the step fails")
        v = await svc.evaluate(slug="alpha")
        check(v.state == "failing",
              f"refuted evidence is failing ({v.state})")

        (path / "main.py").write_text(
            "def add(a, b):\n    return a + b\n\n"
            "def sub(a, b):\n    return a - b\n", encoding="utf-8")
        await verdict(nova, "alpha", ids[0], PASSED)
        await w.step("repaired and re-proved")
        v = await svc.evaluate(slug="alpha")
        check(v.state not in ("failing",),
              f"the refutation is answered ({v.state})")

        await verdict(nova, "alpha", ids[1], PASSED)
        await do_next(nova, w)
        await nova.memory.update_goal_status(goal_id=g, status="completed")
        await w.step("everything proved, work finished")
        v = await svc.evaluate(slug="alpha")
        check(v.state == COMPLETE, f"complete ({v.state})")

        said = await pb.status_text("alpha")
        check("complete" in said.lower(),
              f"and it says so ({said[:60]!r})")
        ladder = [m for m in w.of("completion:alpha")]
        check(len(ladder) >= 4,
              f"the state climbed under its own evidence "
              f"({[f'{a}->{b}' for _, a, b, _ in ladder]})")
        check(w.n >= 14, f"J8 recorded {w.n} authoritative moves")


# ── J9: a destructive request, a deferral, a permission ────────────────────

async def journey_9_destructive_deferral_permission() -> None:
    check.section("J9 a deletion asked for, deferred, and finally refused")
    async with boot(default_reply="Sure.") as nova:
        w = ledger(nova, "J9")
        seed(nova, "alpha")
        seed(nova, "bravo")
        await contract(nova, "alpha", ["adds numbers"])
        await plan(nova, "alpha", ["a1"])
        await plan(nova, "bravo", ["b1"])
        await w.step("two projects with work")

        broker = nova.runtime._permission_broker
        raised = await broker.request("project.delete",
                                      details={"project": "bravo",
                                               "name": "bravo"})
        rid = str(raised["request_id"])
        await w.step("deletion of B is proposed")
        check(broker.pending() and str(broker.pending()[0]["request_id"]) == rid,
              f"the request is outstanding and named ({broker.pending()})")
        check(str((broker.pending()[0].get("details") or {}).get("project"))
              == "bravo",
              f"against B specifically "
              f"({(broker.pending()[0].get('details') or {})})")

        # Work continues while a person thinks about it.
        c, outcome = await do_next(nova, w)
        await w.step("work carries on while it waits")
        check(outcome == "applied",
              f"the pending question does not freeze the work ({outcome})")

        check(broker.resolve(rid, False) is True, "the answer is no")
        await w.step("the deletion is refused")
        check(broker.settled_as(rid) == "rejected",
              f"recorded as a refusal ({broker.settled_as(rid)})")
        check((nova.projects_dir / "bravo" / "PROJECT.md").exists(),
              "and B is still there")
        check(broker.pending() == [],
              f"with nothing left outstanding ({broker.pending()})")

        # LIVENESS: an approval genuinely goes the other way.
        second = await broker.request("project.delete",
                                      details={"project": "bravo",
                                               "name": "bravo"})
        rid2 = str(second["request_id"])
        await w.step("asked again")
        check(broker.resolve(rid2, True) is True, "this time the answer is yes")
        await w.step("approved")
        check(broker.settled_as(rid2) == "approved",
              f"and that is what the trail says ({broker.settled_as(rid2)})")
        check(broker.settled_as(rid) == "rejected",
              f"while the first refusal still stands ({broker.settled_as(rid)})")
        check(len(w.of("permission:")) >= 2,
              f"both requests moved through their own lifecycles "
              f"({[m[0][-6:] + ':' + m[2] for m in w.of('permission:')]})")
        check(w.n >= 4, f"J9 recorded {w.n} authoritative moves")


# ── J10: a long sprint, four goals deep, with a correction halfway ─────────

async def journey_10_a_long_sprint() -> None:
    check.section("J10 four goals, twenty-four steps, and a correction")
    async with boot(default_reply="Sure.") as nova:
        w = ledger(nova, "J10")
        svc = nova.runtime.completion
        seed(nova, "alpha")
        rev1, ids1 = await contract(nova, "alpha",
                                    ["adds numbers", "subtracts numbers",
                                     "prints a total"])
        await w.step("the contract")

        goals = []
        for n, name in enumerate(("groundwork", "features", "hardening",
                                  "release")):
            g, _ = await plan(nova, "alpha",
                              [f"{name}-{i}" for i in range(1, 7)])
            goals.append(g)
        await w.step("four goals planned")

        # Goal 1: everything works.
        for _ in range(6):
            _, outcome = await do_next(nova, w)
            check(outcome == "applied", f"groundwork step applied ({outcome})")
        await nova.memory.update_goal_status(goal_id=goals[0],
                                             status="completed")
        await w.step("groundwork done")
        await verdict(nova, "alpha", ids1[0], PASSED)
        await w.step("first criterion proved")

        # Goal 2: two of six fail, and the failures are recorded as failures.
        for i in range(6):
            _, outcome = await do_next(
                nova, w, status=("failed" if i in (2, 4) else "done"),
                error=("the generator crashed" if i in (2, 4) else ""))
            check(outcome == "applied",
                  f"every honest outcome is written ({outcome})")
        await nova.memory.update_goal_status(goal_id=goals[1], status="failed")
        await w.step("features partly failed")
        await verdict(nova, "alpha", ids1[1], FAILED, error="off by one")
        await w.step("a criterion is refuted")
        v = await svc.evaluate(slug="alpha")
        check(v.state == "failing",
              f"refuted evidence is failing, whatever the plan says "
              f"({v.state})")

        # The request changes mid-sprint.
        rev2 = await svc.record_request(
            slug="alpha",
            request_text="adds numbers and subtracts numbers and prints a "
                         "total and exports a csv")
        ids2 = await svc.set_criteria(slug="alpha", revision=rev2, criteria=[
            {"text": t, "origin_quote": t, "verify_kind": "machine"}
            for t in ("adds numbers", "subtracts numbers", "prints a total",
                      "exports a csv")])
        await svc.seal_contract(slug="alpha", revision=rev2)
        await w.step("the request grew")
        check(rev2 == rev1 + 1, f"one revision on ({rev1} -> {rev2})")
        v = await svc.evaluate(slug="alpha")
        check(v.state != COMPLETE,
              f"and none of the old evidence settles it ({v.state})")

        # Goals 3 and 4 carry the new promise home.
        for _ in range(12):
            c, outcome = await do_next(nova, w)
            check(c is not None, "there is still work to claim")
        for g in goals[2:]:
            await nova.memory.update_goal_status(goal_id=g, status="completed")
        await w.step("the last two goals finish")
        for cid in ids2:
            await verdict(nova, "alpha", cid, PASSED)
            await w.step(f"proved {cid[:8]}")

        v = await svc.evaluate(slug="alpha")
        check(v.state == COMPLETE,
              f"the new contract is met on new evidence ({v.state})")
        said = await nova.runtime._project_builder.status_text("alpha")
        check("complete" in said.lower(),
              f"and it says so ({said[:60]!r})")
        check("did not all succeed" in said or "failed" in said.lower(),
              f"while still owning the goal that failed ({said[:160]!r})")
        check(len(w.of("task:")) >= 40,
              f"the work axis moved {len(w.of('task:'))} times")
        check(w.n >= 45, f"J10 recorded {w.n} authoritative moves")


# ── J11: three projects, three endings ─────────────────────────────────────

async def journey_11_three_projects_three_endings() -> None:
    check.section("J11 three projects, three different truths, side by side")
    async with boot(default_reply="Sure.") as nova:
        w = ledger(nova, "J11")
        svc = nova.runtime.completion
        for slug in ("alpha", "bravo", "charlie"):
            seed(nova, slug)
        ids = {}
        for slug, texts in (("alpha", ["adds numbers"]),
                            ("bravo", ["draws a chart"]),
                            ("charlie", ["sends an email"])):
            _, cid = await contract(nova, slug, texts)
            ids[slug] = cid
        await w.step("three contracts")

        goals = {}
        for slug in ("alpha", "bravo", "charlie"):
            g, _ = await plan(nova, slug, [f"{slug}-{i}" for i in range(1, 6)])
            goals[slug] = g
        await w.step("three plans")

        # Round robin: each project takes a step, in the order the queue hands
        # them out, and every claim is checked against the project it belongs
        # to.
        seen_projects: list[str] = []
        for _ in range(9):
            c, outcome = await do_next(nova, w)
            check(c is not None, "work is available")
            seen_projects.append(str(c["project_name"]))
            check(outcome == "applied",
                  f"{c['project_name']}'s step applied ({outcome})")
        check(len(set(seen_projects)) >= 2,
              f"more than one project actually moved ({set(seen_projects)})")

        # alpha: cancelled outright.
        await nova.memory.cancel_goal(goal_id=goals["alpha"])
        await w.step("alpha is cancelled")
        # bravo: finishes its work and proves itself.
        while True:
            c, _ = await do_next(nova, w)
            if c is None:
                break
        await nova.memory.update_goal_status(goal_id=goals["bravo"],
                                             status="completed")
        await w.step("bravo's plan is done")
        await verdict(nova, "bravo", ids["bravo"][0], PASSED)
        await w.step("bravo proved")
        # charlie: work done, evidence refuted.
        await nova.memory.update_goal_status(goal_id=goals["charlie"],
                                             status="completed")
        await verdict(nova, "charlie", ids["charlie"][0], FAILED,
                      error="the mail server said no")
        await w.step("charlie refuted")
        # alpha: nothing was ever proved.
        await verdict(nova, "alpha", ids["alpha"][0], INCONCLUSIVE,
                      error="the check could not decide")
        await w.step("alpha undecided")

        states = {slug: (await svc.evaluate(slug=slug)).state
                  for slug in ("alpha", "bravo", "charlie")}
        check(states["bravo"] == COMPLETE,
              f"bravo is complete ({states['bravo']})")
        check(states["charlie"] == "failing",
              f"charlie is failing ({states['charlie']})")
        check(states["alpha"] not in (COMPLETE, "failing"),
              f"alpha is neither ({states['alpha']})")
        check(len(set(states.values())) == 3,
              f"three projects, three distinct states ({states})")
        said = {slug: await nova.runtime._project_builder.status_text(slug)
                for slug in states}
        for slug, text in said.items():
            others = [o for o in states if o != slug]
            check(not any(o in text for o in others),
                  f"{slug}'s status names only itself ({text[:60]!r})")
        check(w.n >= 35, f"J11 recorded {w.n} authoritative moves")


# ── J12: asked, refused, asked again, approved, and never confused ─────────

async def journey_12_permission_pressure() -> None:
    check.section("J12 six decisions, two projects, and work throughout")
    async with boot(default_reply="Sure.") as nova:
        w = ledger(nova, "J12")
        broker = nova.runtime._permission_broker
        for slug in ("alpha", "bravo"):
            seed(nova, slug)
            await contract(nova, slug, ["adds numbers"])
            await plan(nova, slug, [f"{slug}-{i}" for i in range(1, 5)])
        await w.step("two projects, two plans, two contracts")

        answers = [("alpha", False), ("bravo", False), ("alpha", True),
                   ("bravo", True), ("alpha", False), ("bravo", False)]
        ids: list[tuple[str, str, bool]] = []
        for slug, approve in answers:
            raised = await broker.request("project.delete",
                                          details={"project": slug,
                                                   "name": slug})
            rid = str(raised["request_id"])
            await w.step(f"asked about {slug}")
            pend = broker.pending()
            check(len(pend) == 1 and str(pend[0]["request_id"]) == rid,
                  f"one question at a time, and it is this one ({len(pend)})")
            check(str((pend[0].get("details") or {}).get("project")) == slug,
                  f"aimed at {slug} ({(pend[0].get('details') or {})})")

            # A step of real work happens between each question and answer.
            c, outcome = await do_next(nova, w)
            check(outcome == "applied",
                  f"work continues while a person decides ({outcome})")

            check(broker.resolve(rid, approve) is True, "the answer lands")
            await w.step(f"{slug}: {'approved' if approve else 'refused'}")
            check(broker.settled_as(rid)
                  == ("approved" if approve else "rejected"),
                  f"recorded as given ({broker.settled_as(rid)})")
            ids.append((rid, slug, approve))

        # Every decision still says what it said, in order, by id.
        for rid, slug, approve in ids:
            check(broker.settled_as(rid)
                  == ("approved" if approve else "rejected"),
                  f"{slug}'s decision {rid[:6]} still stands "
                  f"({broker.settled_as(rid)})")
        check(len({rid for rid, _, _ in ids}) == len(ids),
              "no two questions shared an id")
        check(broker.pending() == [],
              f"and nothing is left waiting ({broker.pending()})")
        for slug in ("alpha", "bravo"):
            check((nova.projects_dir / slug / "PROJECT.md").exists(),
                  f"{slug} is still on disk: approval is not deletion")
        check(len(w.of("permission:")) >= 6,
              f"six lifecycles were observed ({len(w.of('permission:'))})")
        check(w.n >= 18, f"J12 recorded {w.n} authoritative moves")


# ── J13: a project that keeps being asked for something else ───────────────

async def journey_13_a_long_series_of_corrections() -> None:
    check.section("J13 ten corrections, each demolishing the last proof")
    async with boot(default_reply="Sure.") as nova:
        w = ledger(nova, "J13")
        svc = nova.runtime.completion
        seed(nova, "alpha")
        g, _ = await plan(nova, "alpha",
                          [f"step-{i}" for i in range(1, 12)])
        await w.step("a project and a plan")

        wanted = ["adds numbers"]
        previous_revision = 0
        for round_no in range(1, 11):
            rev, ids = await contract(nova, "alpha", list(wanted))
            await w.step(f"round {round_no}: the request is restated")
            check(rev == previous_revision + 1,
                  f"round {round_no} is one revision on "
                  f"({previous_revision} -> {rev})")
            previous_revision = rev

            v = await svc.evaluate(slug="alpha")
            check(v.state != COMPLETE,
                  f"round {round_no} starts unproved ({v.state})")

            # A step of real work, then the proof of everything asked for.
            _, outcome = await do_next(nova, w)
            check(outcome == "applied",
                  f"round {round_no}'s work applied ({outcome})")
            for cid in ids:
                await verdict(nova, "alpha", cid, PASSED)
            await w.step(f"round {round_no}: everything proved")

            v = await svc.evaluate(slug="alpha")
            check(v.state == COMPLETE,
                  f"round {round_no} ends complete ({v.state})")
            wanted.append(f"handles case {round_no}")

        check(len(w.of("revision:")) == 10,
              f"ten revisions, counted from the store "
              f"({[m[2] for m in w.of('revision:')]})")
        check(len(w.of("completion:alpha")) >= 14,
              f"and the state fell and rose with each one "
              f"({len(w.of('completion:alpha'))} moves)")
        req = await nova.memory.current_requirement(project_name="alpha")
        check(int(req["revision"]) == 10,
              f"the current requirement is the tenth ({req['revision']})")
        check(w.n >= 30, f"J13 recorded {w.n} authoritative moves")


# ── J14: eight goals, and not one of them ends the same way ────────────────

async def journey_14_eight_goals_eight_endings() -> None:
    check.section("J14 ten goals: finished, failed, cancelled, left alone")
    async with boot(default_reply="Sure.") as nova:
        w = ledger(nova, "J14")
        seed(nova, "alpha")
        await contract(nova, "alpha", ["adds numbers"])
        await w.step("one project, one promise")

        # TWO MEASURED FACTS shape this journey, and both cost a wrong version
        # of it first:
        #   * `claim_next_goal_task` reads ONE GLOBAL QUEUE ordered by
        #     updated_at across every ACTIVE goal -- not per goal, and not in
        #     creation order. Planning all the goals up front and then "running
        #     goal 1's steps" actually claims whatever is oldest.
        #   * a queued task whose goal is no longer active is not claimable at
        #     all, which is what makes one-goal-at-a-time unambiguous here.
        # So each goal is planned, driven and ENDED before the next exists, and
        # the two goals nobody ends are created last.
        endings: dict[str, str] = {}
        for i in range(1, 9):
            g, _ = await plan(nova, "alpha",
                              [f"g{i}-t{j}" for j in range(1, 5)])
            await w.step(f"g{i} planned")
            how = ("completed", "failed", "cancelled")[(i - 1) % 3]

            if how == "completed":
                for _ in range(4):
                    c, outcome = await do_next(nova, w)
                    check(c is not None and str(c["goal_id"]) == str(g),
                          f"g{i}'s claim belongs to g{i} "
                          f"({str((c or {}).get('goal_id'))[:8]})")
                    check(outcome == "applied",
                          f"g{i} step applied ({outcome})")
                await nova.memory.update_goal_status(goal_id=g,
                                                     status="completed")
            elif how == "failed":
                for j in range(4):
                    c, outcome = await do_next(
                        nova, w, status=("failed" if j >= 2 else "done"),
                        error=("it broke" if j >= 2 else ""))
                    check(c is not None and str(c["goal_id"]) == str(g),
                          f"g{i}'s failing claim is still g{i}'s "
                          f"({str((c or {}).get('goal_id'))[:8]})")
                await nova.memory.update_goal_status(goal_id=g, status="failed")
            else:
                await do_next(nova, w)
                before = [x for x in await nova.memory.list_goals(limit=30)
                          if str(x["goal_id"]) == str(g)][0]
                await nova.memory.cancel_goal(goal_id=g)
                after = [x for x in await nova.memory.list_goals(limit=30)
                         if str(x["goal_id"]) == str(g)][0]
                check(int(after["generation"]) == int(before["generation"]) + 1,
                      f"g{i}'s cancel moved its generation "
                      f"({before['generation']} -> {after['generation']})")
            endings[str(g)] = how
            await w.step(f"g{i} ends as {how}")

            # Nothing of an ended goal is claimable, whatever it has left.
            leftover, why = await do_next(nova, w)
            check(leftover is None,
                  f"after g{i} ended, nothing of its remains runnable "
                  f"({(leftover or {}).get('tool_name', why)})")

        # The two nobody ends, created last so nothing older is in the way.
        untouched = []
        for i in (9, 10):
            g, _ = await plan(nova, "alpha",
                              [f"g{i}-t{j}" for j in range(1, 5)])
            untouched.append(str(g))
            endings[str(g)] = "active"
        await w.step("two goals left alone")

        rows = {str(x["goal_id"]): x
                for x in await nova.memory.list_goals(limit=30)}
        for gid, expected in endings.items():
            check(str(rows[gid]["status"]) == expected,
                  f"{gid[:8]} ended as {expected} ({rows[gid]['status']})")
        check(len(set(endings.values())) == 4,
              f"four different endings were reached ({set(endings.values())})")

        claimed_rows = []
        while True:
            c, _ = await do_next(nova, w)
            if c is None:
                break
            claimed_rows.append(c)
        check(len(claimed_rows) == 8,
              f"exactly the untouched goals' eight tasks were runnable "
              f"({len(claimed_rows)})")
        check(all(str(c["goal_id"]) in untouched for c in claimed_rows),
              f"and every one belonged to a goal nobody ended "
              f"({sorted({str(c['goal_id'])[:8] for c in claimed_rows})})")

        v = await nova.runtime.completion.evaluate(slug="alpha")
        check(v.state != COMPLETE,
              f"none of that work proved anything about the artifact "
              f"({v.state})")
        check(len(w.of("goal:")) >= 8,
              f"the goal axis moved {len(w.of('goal:'))} times")
        check(w.n >= 60, f"J14 recorded {w.n} authoritative moves")


async def main() -> None:
    await journey_1_work_without_a_contract()
    await journey_2_partial_evidence()
    await journey_3_failed_work_complete_artifact()
    await journey_4_correction_after_complete()
    await journey_5_background_failure_foreground_complete()
    await journey_6_restart_with_disagreement()
    await journey_7_two_projects_step_for_step()
    await journey_8_the_whole_ladder()
    await journey_9_destructive_deferral_permission()
    await journey_10_a_long_sprint()
    await journey_11_three_projects_three_endings()
    await journey_12_permission_pressure()
    await journey_13_a_long_series_of_corrections()
    await journey_14_eight_goals_eight_endings()

    total = sum(w.n for w in WORLDS)
    axes: dict[str, int] = {}
    for world in WORLDS:
        for axis, n in world.by_axis().items():
            axes[axis] = axes.get(axis, 0) + n
    check.section("counted, not claimed")
    for world in WORLDS:
        print(f"  {world.name}: {world.n:>3} moves  {world.by_axis()}")
    crossings = [c for world in WORLDS for c in world.crossings()]
    non_task = total - axes.get("task", 0)
    print(f"  TOTAL: {total} authoritative transitions across "
          f"{len(WORLDS)} journeys")
    print(f"  by axis: {axes}")
    print(f"  of which NOT on the work axis: {non_task}")
    print(f"  actions that moved more than one capability at once: "
          f"{len(crossings)}")
    for cause, axes_moved in crossings[:8]:
        print(f"     {cause!r} -> {list(axes_moved)}")
    check(len(axes) >= 5,
          f"the journeys moved every axis, not one of them repeatedly ({axes})")
    check(total > 300,
          f"{total} counted authoritative transitions (target: >300)")
    check(non_task >= 80,
          f"{non_task} of them are off the work axis, so the total is not "
          f"one axis repeated")
    check(len(crossings) >= 20,
          f"{len(crossings)} single actions moved two or more capabilities "
          f"at once")
    check.finish()


if __name__ == "__main__":
    run(main)
