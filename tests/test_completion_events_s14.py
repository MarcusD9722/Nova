"""What the events say, and how often they say it (Stage 14 §8).

`project.completed` has one meaning now: the project became complete. This
suite proves the six states that are not COMPLETE never produce it, that a
real transition produces exactly one, and — the part that was measured rather
than assumed — that a restart produces none.

The restart cases run in REAL separate interpreters against a shared durable
root, because the defect they exist for was invisible in-process: the ledger
was a dict, and a dict is perfectly idempotent right up until the process ends.

Run:  venv\\Scripts\\python.exe tests\\test_completion_events_s14.py
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
os.environ.setdefault("NOVA_IT_WATCHDOG_S", "600")

from harness import Checks, run  # noqa: E402

from restart_harness import one, run_step  # noqa: E402

from core.completion import (  # noqa: E402
    COMPLETE, FAILED, FAILING, IDEA, PARTIALLY_IMPLEMENTED, PASSED, PASSING,
    PLANNED, SCAFFOLDED, Criterion, CriterionStatus, Verdict,
)
from core.completion_events import CompletionAnnouncer  # noqa: E402
from core.completion_service import CompletionService  # noqa: E402
from core.event_bus import BUS  # noqa: E402
from memory.unifier import MemoryUnifier  # noqa: E402

check = Checks()

SLUG = "calc"
REQUEST = "a thing that adds and subtracts"


def _tmp():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


def events(slug: str, kind: str):
    """Events of one type FOR ONE PROJECT. The bus is process-wide."""
    return [e for e in BUS.recent(limit=800)
            if e.type == kind and (e.data or {}).get("project") == slug]


class World:
    """A project of its own, per test.

    The event bus is process-wide and `BUS.recent()` spans the entire run, so
    two tests sharing a slug share their event history and the second one
    counts the first one's transitions. Distinct slugs make the counts mean
    what they say.
    """

    def __init__(self, td: str, slug: str = SLUG):
        self.slug = slug
        self.root = Path(td)
        self.projects = self.root / "projects"
        self.path = self.projects / slug
        self.path.mkdir(parents=True)
        self.mem = MemoryUnifier(self.root / "memory_data", enable_chroma=False)
        self.svc = CompletionService(memory=self.mem, projects_dir=self.projects)
        self.ann = CompletionAnnouncer(memory=self.mem)

    async def start(self):
        await self.mem.initialize()
        return self

    async def contract(self, request=REQUEST):
        rev = await self.svc.record_request(slug=self.slug, request_text=request)
        ids = await self.svc.set_criteria(slug=self.slug, revision=rev, criteria=[
            {"text": "adds two numbers", "origin_quote": "adds"},
            {"text": "subtracts two numbers", "origin_quote": "subtracts"}])
        await self.svc.seal_contract(slug=self.slug, revision=rev)
        return rev, ids

    async def prove(self, cid, verdict=PASSED):
        ctx = await self.svc.begin_check(slug=self.slug, criterion_id=cid)
        await self.svc.record_verdict(context=ctx, verdict=verdict)

    async def evaluate_and_announce(self, reason="evaluated"):
        v = await self.svc.evaluate(slug=self.slug)
        await self.ann.announce(slug=self.slug, verdict=v, reason=reason)
        return v


async def test_a_only_complete_produces_a_completion_event():
    check.section("§8 the six other states never say 'completed'")
    fired: dict[str, int] = {}
    for state in (IDEA, PLANNED, SCAFFOLDED, PARTIALLY_IMPLEMENTED, FAILING,
                  PASSING, COMPLETE):
        slug = f"proj-{state}"
        v = Verdict(state=state, revision=1, reasons=("because",),
                    criteria=(CriterionStatus(
                        criterion=Criterion("c", "does a thing", "thing"),
                        verdict=PASSED, evidence=None),))
        # No memory: this exercises the payload, not the ledger.
        await CompletionAnnouncer().announce(slug=slug, verdict=v)
        fired[state] = len(events(slug, "project.completed"))
    wrong = {k: n for k, n in fired.items() if (k == COMPLETE) != (n == 1)}
    check(not wrong, f"only COMPLETE emits project.completed ({wrong or fired})")
    check(all(len(events(f"proj-{s}", "project.state_changed")) == 1
              for s in fired),
          "while every state emits exactly one state_changed")


async def test_b_one_transition_is_one_event():
    check.section("§8 re-deriving the same answer is not news")
    with _tmp() as td:
        w = await World(td, "calc-b").start()
        rev, (add_id, sub_id) = await w.contract()
        w.path.joinpath("main.py").write_text(
            "def add(a,b): return a+b\ndef subtract(a,b): return a-b\n",
            encoding="utf-8")
        await w.prove(add_id)
        await w.prove(sub_id)

        v = await w.evaluate_and_announce()
        check(v.state == COMPLETE, f"the project completed ({v.state})")
        check(len(events('calc-b', 'project.completed')) == 1,
              f"one completion event ({len(events('calc-b', 'project.completed'))})")

        for _ in range(4):
            await w.evaluate_and_announce("re-derived")
        check(len(events('calc-b', 'project.completed')) == 1,
              f"four more evaluations add nothing "
              f"({len(events('calc-b', 'project.completed'))})")


async def test_c_a_restart_announces_nothing_new():
    check.section("§8 the ledger outlives the process — measured, not assumed")
    with _tmp() as td:
        root = Path(td) / "n"
        setup = '''
    from core.completion_events import CompletionAnnouncer
    from core.completion_service import CompletionService
    from core.event_bus import BUS
    projects = Path(r"@ROOT@") / "projects"
    (projects / "calc").mkdir(parents=True, exist_ok=True)
    svc = CompletionService(memory=mem, projects_dir=projects)
    ann = CompletionAnnouncer(memory=mem)
'''.replace("@ROOT@", str(root))

        first = run_step(root, setup + '''
    rev = await svc.record_request(slug="calc", request_text="a thing that adds")
    ids = await svc.set_criteria(slug="calc", revision=rev, criteria=[
        {"text": "adds", "origin_quote": "adds"}])
    await svc.seal_contract(slug="calc", revision=rev)
    (projects / "calc" / "main.py").write_text("def add(a,b): return a+b\\n",
                                               encoding="utf-8")
    ctx = await svc.begin_check(slug="calc", criterion_id=ids[0])
    await svc.record_verdict(context=ctx, verdict="passed")
    v = await svc.evaluate(slug="calc")
    await ann.announce(slug="calc", verdict=v, reason="built")
    emit({"state": v.state, "completed": len(
        [e for e in BUS.recent(limit=200) if e.type == "project.completed"])})
''')
        check(one(first, "state") == COMPLETE and one(first, "completed") == 1,
              f"the transition announced once ({one(first, 'state')}, "
              f"{one(first, 'completed')})")

        for n in (2, 3):
            later = run_step(root, setup + '''
    v = await svc.evaluate(slug="calc")
    await ann.announce(slug="calc", verdict=v, reason="restarted")
    emit({"state": v.state, "completed": len(
        [e for e in BUS.recent(limit=200) if e.type == "project.completed"]),
          "changed": len([e for e in BUS.recent(limit=200)
                          if e.type == "project.state_changed"])})
''')
            check(one(later, "state") == COMPLETE,
                  f"process {n} still derives COMPLETE ({one(later, 'state')})")
            check(one(later, "completed") == 0,
                  f"and announces nothing ({one(later, 'completed')} events)")
            check(one(later, "changed") == 0,
                  f"not even a state change ({one(later, 'changed')})")


async def test_d_a_real_later_transition_still_announces():
    check.section("§8 suppression must not swallow the next real transition")
    with _tmp() as td:
        w = await World(td, "calc-d").start()
        rev1, (add_id, sub_id) = await w.contract()
        w.path.joinpath("main.py").write_text(
            "def add(a,b): return a+b\ndef subtract(a,b): return a-b\n",
            encoding="utf-8")
        await w.prove(add_id)
        await w.prove(sub_id)
        await w.evaluate_and_announce()
        check(len(events('calc-d', 'project.completed')) == 1, "R1 completed once")

        # The user asks for more. R2 is a different transition identity.
        rev2 = await w.svc.record_request(
            slug="calc-d", request_text=REQUEST + " and multiplies")
        await w.svc.carry_forward(slug="calc-d", from_revision=rev1, to_revision=rev2)
        mul = await w.svc.set_criteria(slug="calc-d", revision=rev2, criteria=[
            {"text": "multiplies two numbers", "origin_quote": "multiplies"}])
        await w.svc.seal_contract(slug="calc-d", revision=rev2)
        v = await w.evaluate_and_announce("new requirement")
        check(v.state != COMPLETE,
              f"R2 is not complete yet ({v.state})")
        check(len(events('calc-d', 'project.completed')) == 1,
              "and no new completion event fired")

        rows = await w.mem.list_acceptance_criteria(project_name="calc-d",
                                                    revision=rev2)
        w.path.joinpath("main.py").write_text(
            "def add(a,b): return a+b\ndef subtract(a,b): return a-b\n"
            "def multiply(a,b): return a*b\n", encoding="utf-8")
        for r in rows:
            await w.prove(r["criterion_id"])
        v = await w.evaluate_and_announce("all proven")
        check(v.state == COMPLETE, f"R2 completes ({v.state})")
        check(len(events('calc-d', 'project.completed')) == 2,
              f"and THAT is announced — a different transition "
              f"({len(events('calc-d', 'project.completed'))})")


async def test_e_regression_and_repair_both_announce():
    check.section("§8 COMPLETE -> FAILING -> COMPLETE is two more transitions")
    with _tmp() as td:
        w = await World(td, "calc-e").start()
        rev, (add_id, sub_id) = await w.contract()
        code = "def add(a,b): return a+b\ndef subtract(a,b): return a-b\n"
        w.path.joinpath("main.py").write_text(code, encoding="utf-8")
        await w.prove(add_id)
        await w.prove(sub_id)
        await w.evaluate_and_announce()
        check(len(events('calc-e', 'project.completed')) == 1, "complete once")

        # A regression: the criterion is refuted against the current code.
        await w.prove(sub_id, verdict=FAILED)
        v = await w.evaluate_and_announce("regressed")
        check(v.state == FAILING, f"it is failing ({v.state})")
        check(len(events('calc-e', 'project.state_changed')) == 2,
              f"a second state change was announced "
              f"({len(events('calc-e', 'project.state_changed'))})")

        await w.prove(sub_id, verdict=PASSED)
        v = await w.evaluate_and_announce("repaired")
        check(v.state == COMPLETE, f"repaired back to complete ({v.state})")
        check(len(events('calc-e', 'project.completed')) == 2,
              f"and completing again IS news ({len(events('calc-e', 'project.completed'))})")


async def test_f_concurrent_announcements_produce_one_event():
    check.section("§8 two announcers racing emit one completion")
    import asyncio

    with _tmp() as td:
        w = await World(td, "calc-f").start()
        rev, (add_id, sub_id) = await w.contract()
        w.path.joinpath("main.py").write_text(
            "def add(a,b): return a+b\ndef subtract(a,b): return a-b\n",
            encoding="utf-8")
        await w.prove(add_id)
        await w.prove(sub_id)
        v = await w.svc.evaluate(slug="calc-f")
        check(v.state == COMPLETE, "the project is complete")

        second = CompletionAnnouncer(memory=w.mem)
        await asyncio.gather(
            w.ann.announce(slug="calc-f", verdict=v, reason="a"),
            second.announce(slug="calc-f", verdict=v, reason="b"))
        check(len(events('calc-f', 'project.completed')) == 1,
              f"exactly one completion event survives the race "
              f"({len(events('calc-f', 'project.completed'))})")


async def test_g_the_episodic_consumer_reads_the_state_it_is_sent():
    check.section("§8 the payload field the promoter reads is the one sent")
    with _tmp() as td:
        w = await World(td, "calc-g").start()
        rev, (add_id, sub_id) = await w.contract()
        w.path.joinpath("main.py").write_text(
            "def add(a,b): return a+b\ndef subtract(a,b): return a-b\n",
            encoding="utf-8")
        await w.prove(add_id)
        await w.prove(sub_id)
        await w.evaluate_and_announce()
        done = events('calc-g', 'project.completed')
        check(done, "a completion event exists")
        payload = done[-1].data or {}

        # The exact expression the promoter evaluates. Measured, because
        # reading `status` from a payload that carries `state` silently
        # produced the literal word "ok" for every completed project.
        read = str(payload.get("state") or payload.get("status") or "ok").strip()
        check(read == COMPLETE,
              f"the promoter reads the real state ({read!r})")
        check("status" not in payload,
              "and the old field name is genuinely gone from the payload")


async def main() -> None:
    await test_a_only_complete_produces_a_completion_event()
    await test_b_one_transition_is_one_event()
    await test_c_a_restart_announces_nothing_new()
    await test_d_a_real_later_transition_still_announces()
    await test_e_regression_and_repair_both_announce()
    await test_f_concurrent_announcements_produce_one_event()
    await test_g_the_episodic_consumer_reads_the_state_it_is_sent()
    check.finish()


if __name__ == "__main__":
    run(main)
