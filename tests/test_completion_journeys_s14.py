"""Six completion journeys, end to end, with every transition counted (§14).

Each journey drives the real service against a real database and reads the
state back from the evaluator after every authoritative action — never from a
variable the test is holding. A step is counted only when the state was
observed that way, so the total is a count of what the system said about
itself, not of how many lines the test executed.

  J1  build, fail, repair, complete — then a correction breaks it again
  J2  a human-verified criterion: asked, resolved, and asked a second time
  J3  drift after completion, and the re-proof that restores it
  J4  a contract that never seals, passing every check it has, forever
  J5  two projects interleaved step for step
  J6  restarts between every authoritative action, one spanning a pending
      human decision

Run:  venv\\Scripts\\python.exe tests\\test_completion_journeys_s14.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, run  # noqa: E402

from core.completion import (  # noqa: E402
    COMPLETE, FAILED, FAILING, IDEA, INCONCLUSIVE, PASSED, PASSING,
    PARTIALLY_IMPLEMENTED, PLANNED, SCAFFOLDED,
)
from core.completion_service import CompletionService  # noqa: E402
from memory.unifier import MemoryUnifier  # noqa: E402

check = Checks()

TOTAL = {"steps": 0, "changes": 0}
SEEN: dict[str, int] = {}


def _tmp():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


class Journey:
    """One project, walked. Every step re-reads the authoritative state."""

    def __init__(self, name: str, svc: CompletionService, slug: str,
                 root: Path):
        self.name = name
        self.svc = svc
        self.slug = slug
        self.path = root / "projects" / slug
        self.path.mkdir(parents=True, exist_ok=True)
        self.state = ""
        self.trail: list[str] = []

    def rebind(self, svc: CompletionService) -> None:
        """After a restart the journey continues against a new service."""
        self.svc = svc

    async def step(self, what: str) -> str:
        """Read the state back from the store and record the transition."""
        verdict = await self.svc.evaluate(slug=self.slug)
        now = verdict.state
        TOTAL["steps"] += 1
        SEEN[now] = SEEN.get(now, 0) + 1
        if now != self.state:
            TOTAL["changes"] += 1
            self.trail.append(f"{self.state or '-'}->{now} ({what})")
        self.state = now
        return now

    def write(self, body: str, name: str = "main.py") -> None:
        (self.path / name).write_text(body, encoding="utf-8")


async def _fresh(root: Path) -> tuple[MemoryUnifier, CompletionService]:
    mem = MemoryUnifier(root / "memory_data", enable_chroma=False)
    await mem.initialize()
    return mem, CompletionService(memory=mem, projects_dir=root / "projects")


async def _prove(svc: CompletionService, slug: str, cid: str,
                 verdict: str = PASSED) -> None:
    ctx = await svc.begin_check(slug=slug, criterion_id=cid)
    await svc.record_verdict(context=ctx, verdict=verdict,
                             error="did not hold" if verdict == FAILED else "")


# ── J1 ─────────────────────────────────────────────────────────────────────
async def journey_1_build_fail_repair_correct():
    check.section("J1 build, fail, repair, complete, then a correction")
    request = "a tool that adds numbers and subtracts numbers"
    with _tmp() as td:
        root = Path(td)
        mem, svc = await _fresh(root)
        j = Journey("J1", svc, "j1", root)

        await j.step("nothing yet")
        check(j.state == IDEA, f"nothing asked for is an idea ({j.state})")

        rev = await svc.record_request(slug="j1", request_text=request)
        await j.step("the request")
        ids = await svc.set_criteria(slug="j1", revision=rev, criteria=[
            {"text": "adds numbers", "origin_quote": "adds numbers",
             "verify_kind": "machine"},
            {"text": "subtracts numbers", "origin_quote": "subtracts numbers",
             "verify_kind": "machine"}])
        await j.step("criteria")
        check(j.state == PLANNED, f"criteria without code is planned ({j.state})")

        await svc.seal_contract(slug="j1", revision=rev)
        await j.step("sealed")
        j.write("def add(a, b):\n    return a + b\n")
        await j.step("some code")
        check(j.state == SCAFFOLDED,
              f"code with nothing demonstrated is scaffolded ({j.state})")

        await _prove(svc, "j1", ids[0], PASSED)
        await j.step("addition proven")
        check(j.state == PARTIALLY_IMPLEMENTED,
              f"one of two proven is partial ({j.state})")

        # The criterion is checked against code that cannot do it.
        await _prove(svc, "j1", ids[1], FAILED)
        await j.step("subtraction refuted")
        check(j.state == FAILING, f"a refuted criterion is failing ({j.state})")

        # Repair. The file changes, so BOTH results are stale again.
        j.write("def add(a, b):\n    return a + b\n\n\n"
                "def subtract(a, b):\n    return a - b\n")
        await j.step("repaired")
        check(j.state != FAILING,
              f"the old refutation does not survive the fix ({j.state})")
        for cid in ids:
            await _prove(svc, "j1", cid, PASSED)
            await j.step("re-proven")
        check(j.state == COMPLETE, f"and now it is complete ({j.state})")

        # A correction. Everything proven was proven about the old request.
        wider = request + " and multiplies numbers"
        rev2 = await svc.record_request(slug="j1", request_text=wider)
        await j.step("the correction")
        check(j.state != COMPLETE,
              f"a new requirement is not already met ({j.state})")
        moved = await svc.carry_forward(slug="j1", from_revision=rev,
                                        to_revision=rev2)
        await j.step("carried forward")
        new_ids = await svc.set_criteria(slug="j1", revision=rev2, criteria=[
            {"text": "multiplies numbers", "origin_quote": "multiplies numbers",
             "verify_kind": "machine"}])
        await j.step("the new criterion")
        await svc.seal_contract(slug="j1", revision=rev2)
        await j.step("resealed")
        for cid in moved:
            await _prove(svc, "j1", cid, PASSED)
            await j.step("the carried criteria re-proven")
        check(j.state != COMPLETE,
              f"multiplication is unwritten and unproven, so not complete "
              f"({j.state})")
        v = await svc.evaluate(slug="j1")
        check(any("multiplies" in s_.criterion.text for s_ in v.outstanding),
              "and multiplication is named as what is outstanding")

        j.write("def add(a, b):\n    return a + b\n\n\n"
                "def subtract(a, b):\n    return a - b\n\n\n"
                "def multiply(a, b):\n    return a * b\n")
        await j.step("multiplication written")
        for cid in list(moved) + list(new_ids):
            await _prove(svc, "j1", cid, PASSED)
            await j.step("proven against the code that has it")
        check(j.state == COMPLETE,
              f"complete again, at the wider request ({j.state})")
        # Five more corrections. Each widens the request, carries the standing
        # contract onto it, and must be re-proven against code that grew.
        request_now = wider
        # Built with chr(10) rather than escapes: patch scripts keep
        # collapsing the escape into a real newline mid-string.
        nl = chr(10)
        body = nl.join(["def add(a, b):", "    return a + b", "", "",
                        "def subtract(a, b):", "    return a - b", "", "",
                        "def multiply(a, b):", "    return a * b", ""])
        prev_rev = rev2
        for n, feature in enumerate(["divides numbers", "rounds numbers",
                                     "negates numbers", "doubles numbers",
                                     "halves numbers"]):
            request_now = request_now + " and " + feature
            new_rev = await svc.record_request(slug="j1",
                                               request_text=request_now)
            state = await j.step(f"correction {n}")
            check(state != COMPLETE,
                  f"correction {n} is not already satisfied ({state})")
            carried = await svc.carry_forward(slug="j1", from_revision=prev_rev,
                                              to_revision=new_rev)
            await j.step(f"carried {len(carried)}")
            fresh = await svc.set_criteria(slug="j1", revision=new_rev, criteria=[
                {"text": feature, "origin_quote": feature,
                 "verify_kind": "machine"}])
            await j.step("the new criterion")
            await svc.seal_contract(slug="j1", revision=new_rev)
            await j.step("resealed")
            body += nl + nl + f"def feature_{n}():" + nl + f"    return {n}" + nl
            j.write(body)
            await j.step("code grew")
            for cid in list(carried) + list(fresh):
                await _prove(svc, "j1", cid, PASSED)
            state = await j.step("everything re-proven")
            check(state == COMPLETE,
                  f"complete again after correction {n} ({state})")
            prev_rev = new_rev

        check(len(j.trail) >= 8, f"J1 changed state {len(j.trail)} times")


# ── J2 ─────────────────────────────────────────────────────────────────────
async def journey_2_human_verified():
    check.section("J2 a criterion only a person can settle")
    request = "a page that looks right and loads quickly"
    with _tmp() as td:
        root = Path(td)
        mem, svc = await _fresh(root)
        j = Journey("J2", svc, "j2", root)
        await j.step("start")

        rev = await svc.record_request(slug="j2", request_text=request)
        ids = await svc.set_criteria(slug="j2", revision=rev, criteria=[
            {"text": "looks right", "origin_quote": "looks right",
             "verify_kind": "human"},
            {"text": "loads quickly", "origin_quote": "loads quickly",
             "verify_kind": "machine"}])
        await svc.seal_contract(slug="j2", revision=rev)
        j.write("<html><body>hi</body></html>", "index.html")
        await j.step("a page exists")

        await _prove(svc, "j2", ids[1], PASSED)
        await j.step("the machine part is proven")

        # A machine result on a human criterion must not settle it.
        await _prove(svc, "j2", ids[0], PASSED)
        state = await j.step("a machine tried to settle the human one")
        check(state != COMPLETE,
              f"a machine pass does not stand in for a person ({state})")

        did = await svc.ask_human(slug="j2", criterion_id=ids[0],
                                  prompt="Does this look right?")
        await j.step("asked")
        check(j.state != COMPLETE, f"asking is not answering ({j.state})")

        await svc.resolve_human_decision(decision_id=did, accepted=True,
                                         actor="marcus", channel="ui")
        await j.step("answered yes")
        check(j.state == COMPLETE, f"and now it is complete ({j.state})")

        # Single use: the same answer cannot be redeemed twice.
        try:
            await svc.resolve_human_decision(decision_id=did, accepted=True,
                                             actor="marcus", channel="ui")
            twice = True
        except ValueError:
            twice = False
        await j.step("tried to reuse the answer")
        check(not twice, "the same answer cannot be spent twice")

        # Now the page changes. The person's judgement was about the old page.
        j.write("<html><body>completely different</body></html>", "index.html")
        state = await j.step("the page changed underneath it")
        check(state != COMPLETE,
              f"a judgement about the old page does not cover the new one "
              f"({state})")

        did2 = await svc.ask_human(slug="j2", criterion_id=ids[0],
                                   prompt="Does it still look right?")
        await j.step("asked again")
        await svc.resolve_human_decision(decision_id=did2, accepted=False,
                                         actor="marcus", channel="ui")
        state = await j.step("answered no")
        check(state != COMPLETE, f"a no keeps it incomplete ({state})")
        await _prove(svc, "j2", ids[1], PASSED)
        await j.step("machine part re-proven")
        did3 = await svc.ask_human(slug="j2", criterion_id=ids[0], prompt="Now?")
        await j.step("asked a third time")
        await svc.resolve_human_decision(decision_id=did3, accepted=True,
                                         actor="marcus", channel="ui")
        state = await j.step("answered yes")
        check(state == COMPLETE, f"and then yes completes it ({state})")

        # Eight more rounds. The page changes each time, so the person is
        # asked again each time, and the previous answer never carries.
        rounds = 0
        for n in range(8):
            j.write(f"<html><body>revision {n}</body></html>", "index.html")
            state = await j.step(f"page changed {n}")
            check(state != COMPLETE,
                  f"round {n}: the old judgement did not carry ({state})")
            await _prove(svc, "j2", ids[1], PASSED)
            await j.step(f"machine re-proven {n}")
            d = await svc.ask_human(slug="j2", criterion_id=ids[0],
                                    prompt=f"Round {n}: still right?")
            await j.step(f"asked {n}")
            await svc.resolve_human_decision(decision_id=d, accepted=True,
                                             actor="marcus", channel="ui")
            state = await j.step(f"answered {n}")
            if state == COMPLETE:
                rounds += 1
        check(rounds == 8,
              f"all eight rounds needed a fresh answer and got one ({rounds}/8)")


# ── J3 ─────────────────────────────────────────────────────────────────────
async def journey_3_drift_and_reproof():
    check.section("J3 drift after completion, ten times over")
    with _tmp() as td:
        root = Path(td)
        mem, svc = await _fresh(root)
        j = Journey("J3", svc, "j3", root)
        await j.step("start")
        rev = await svc.record_request(slug="j3",
                                       request_text="a script that prints a total")
        ids = await svc.set_criteria(slug="j3", revision=rev, criteria=[
            {"text": "prints a total", "origin_quote": "prints a total",
             "verify_kind": "machine"}])
        await svc.seal_contract(slug="j3", revision=rev)
        j.write("print(1)\n")
        await j.step("code")
        await _prove(svc, "j3", ids[0], PASSED)
        await j.step("proven")
        check(j.state == COMPLETE, f"complete ({j.state})")

        completes = 0
        for i in range(40):
            j.write(f"print({i} + 1)\n")
            state = await j.step(f"edit {i}")
            check_ok = state != COMPLETE
            if not check_ok:
                check(False, f"edit {i} left it complete without a re-check")
                break
            await _prove(svc, "j3", ids[0], PASSED)
            state = await j.step(f"re-proven {i}")
            if state == COMPLETE:
                completes += 1
        check(completes == 40,
              f"every edit dropped it and every re-proof restored it "
              f"({completes}/40)")


# ── J4 ─────────────────────────────────────────────────────────────────────
async def journey_4_never_seals():
    check.section("J4 a contract that never covers the request")
    request = "a tool that imports a CSV and charts it and emails the chart"
    with _tmp() as td:
        root = Path(td)
        mem, svc = await _fresh(root)
        j = Journey("J4", svc, "j4", root)
        await j.step("start")
        rev = await svc.record_request(slug="j4", request_text=request)
        ids = await svc.set_criteria(slug="j4", revision=rev, criteria=[
            {"text": "imports a CSV", "origin_quote": "imports a CSV",
             "verify_kind": "machine"}])
        j.write("import csv\n")
        await j.step("code")

        sealed = True
        try:
            await svc.seal_contract(slug="j4", revision=rev)
        except ValueError:
            sealed = False
        await j.step("tried to seal")
        check(not sealed, "sealing is refused while two clauses are unquoted")

        # Prove the one criterion it has, over and over. It can never complete.
        for i in range(20):
            await _prove(svc, "j4", ids[0], PASSED)
            state = await j.step(f"proven again {i}")
            if state == COMPLETE:
                check(False, f"an unsealed contract completed at pass {i}")
                break
        check(j.state != COMPLETE,
              f"twenty passes and it is still not complete ({j.state})")

        # Quoting the rest is what unlocks it.
        more = await svc.set_criteria(slug="j4", revision=rev, criteria=[
            {"text": "charts it", "origin_quote": "charts it",
             "verify_kind": "machine"},
            {"text": "emails the chart", "origin_quote": "emails the chart",
             "verify_kind": "machine"}])
        await j.step("the missing clauses quoted")
        await svc.seal_contract(slug="j4", revision=rev)
        await j.step("sealed")
        for cid in list(ids) + list(more):
            await _prove(svc, "j4", cid, PASSED)
            await j.step("proven")
        check(j.state == COMPLETE,
              f"and only then may it complete ({j.state})")


# ── J5 ─────────────────────────────────────────────────────────────────────
async def journey_5_two_projects_interleaved():
    check.section("J5 two projects, step for step")
    with _tmp() as td:
        root = Path(td)
        mem, svc = await _fresh(root)
        a = Journey("J5a", svc, "j5a", root)
        b = Journey("J5b", svc, "j5b", root)
        states: list[tuple[str, str]] = []

        ra = await svc.record_request(slug="j5a", request_text="a adds things")
        rb = await svc.record_request(slug="j5b", request_text="b adds things")
        ia = await svc.set_criteria(slug="j5a", revision=ra, criteria=[
            {"text": "adds things", "origin_quote": "adds things",
             "verify_kind": "machine"}])
        ib = await svc.set_criteria(slug="j5b", revision=rb, criteria=[
            {"text": "adds things", "origin_quote": "adds things",
             "verify_kind": "machine"}])
        await svc.seal_contract(slug="j5a", revision=ra)
        await svc.seal_contract(slug="j5b", revision=rb)

        # Everything that happens, happens to A. B is only ever observed.
        b_states = set()
        script = (["code", "prove", "edit", "prove", "refute", "repair",
                   "prove"] * 3) + ["correct", "prove", "code", "prove"]
        for i, act in enumerate(script):
            if act == "code":
                a.write("def add():\n    return 1\n")
            elif act == "edit":
                a.write(f"def add():\n    return {i}\n")
            elif act == "repair":
                a.write("def add():\n    return 2\n")
            elif act == "prove":
                await _prove(svc, "j5a", ia[0], PASSED)
            elif act == "refute":
                await _prove(svc, "j5a", ia[0], FAILED)
            elif act == "correct":
                await svc.record_request(slug="j5a",
                                         request_text="a adds things twice")
            sa = await a.step(act)
            sb = await b.step(f"observed while A did {act}")
            b_states.add(sb)
            states.append((sa, sb))

        check(len(b_states) == 1,
              f"B never moved while A did everything ({b_states})")
        check(len({s for s, _ in states}) >= 3,
              f"while A moved through {len({s for s, _ in states})} states")

        # And now B, which should still be exactly where it was left.
        b.write("def add():\n    return 1\n")
        await b.step("B finally gets code")
        await _prove(svc, "j5b", ib[0], PASSED)
        state = await b.step("B proven")
        check(state == COMPLETE, f"B completes on its own evidence ({state})")
        sa = await a.step("A unchanged by B completing")
        check(sa != COMPLETE, f"and A is untouched by it ({sa})")


# ── J6 ─────────────────────────────────────────────────────────────────────
async def journey_6_restart_between_every_step():
    check.section("J6 a restart between every authoritative action")
    with _tmp() as td:
        root = Path(td)
        mem, svc = await _fresh(root)
        j = Journey("J6", svc, "j6", root)
        request = "a service that starts up and answers a health check"

        async def restart(label: str) -> None:
            nonlocal mem, svc
            mem, svc = await _fresh(root)
            j.rebind(svc)
            await j.step(f"after restart: {label}")

        await j.step("cold start, nothing recorded")
        rev = await svc.record_request(slug="j6", request_text=request)
        await restart("the request")
        check(j.state == IDEA, f"a request with no criteria is an idea ({j.state})")

        ids = await svc.set_criteria(slug="j6", revision=rev, criteria=[
            {"text": "starts up", "origin_quote": "starts up",
             "verify_kind": "machine"},
            {"text": "answers a health check",
             "origin_quote": "answers a health check", "verify_kind": "human"}])
        await restart("criteria")
        check(j.state == PLANNED, f"criteria survive a restart ({j.state})")

        await svc.seal_contract(slug="j6", revision=rev)
        await restart("sealing")
        j.write("def main():\n    return 'ok'\n")
        await restart("the code")
        check(j.state == SCAFFOLDED, f"code with no proof ({j.state})")

        await _prove(svc, "j6", ids[0], PASSED)
        await restart("the machine proof")
        check(j.state == PASSING,
              f"every machine criterion passes, one awaits a person "
              f"({j.state})")
        v = await svc.evaluate(slug="j6")
        check(any("health check" in s_.criterion.text for s_ in v.outstanding),
              "and the health check is named as what is awaited")
        check(j.state != COMPLETE, "which is emphatically not complete")

        # The question is asked in one process and answered in another.
        did = await svc.ask_human(slug="j6", criterion_id=ids[1],
                                  prompt="Does the health check answer?")
        await restart("asking, before it is answered")
        check(j.state != COMPLETE,
              f"a pending question survives as pending ({j.state})")
        pending = await mem.list_human_decisions(project_name="j6",
                                                 open_only=True)
        check(len(pending) == 1,
              f"and the question itself is still there to answer ({len(pending)})")

        await svc.resolve_human_decision(decision_id=did, accepted=True,
                                         actor="marcus", channel="ui")
        await restart("the answer")
        check(j.state == COMPLETE, f"complete, across five restarts ({j.state})")

        # Drift while the process is down.
        j.write("def main():\n    return 'changed while nobody was running'\n")
        await restart("an edit made while nothing was running")
        check(j.state != COMPLETE,
              f"an edit made while down still invalidates it ({j.state})")

        for i in range(12):
            await _prove(svc, "j6", ids[0], PASSED)
            await restart(f"re-proof {i}")
        check(j.state != COMPLETE,
              f"the human criterion is stale too, and no machine pass fixes it "
              f"({j.state})")
        did2 = await svc.ask_human(slug="j6", criterion_id=ids[1], prompt="Again?")
        await restart("asked again")
        await svc.resolve_human_decision(decision_id=did2, accepted=True,
                                         actor="marcus", channel="ui")
        await restart("answered again")
        check(j.state == COMPLETE, f"and it completes again ({j.state})")


async def main() -> None:
    await journey_1_build_fail_repair_correct()
    await journey_2_human_verified()
    await journey_3_drift_and_reproof()
    await journey_4_never_seals()
    await journey_5_two_projects_interleaved()
    await journey_6_restart_between_every_step()

    check.section("what the six journeys covered")
    for state in sorted(SEEN):
        print(f"    {state:<26} {SEEN[state]}")
    check(TOTAL["steps"] > 225,
          f"{TOTAL['steps']} authoritative state observations "
          f"({TOTAL['changes']} of them transitions)")
    for state in (IDEA, PLANNED, SCAFFOLDED, PARTIALLY_IMPLEMENTED, FAILING,
                  PASSING, COMPLETE):
        check(SEEN.get(state, 0) > 0,
              f"{state} was reached ({SEEN.get(state, 0)})")
    check.finish()


if __name__ == "__main__":
    run(main)
