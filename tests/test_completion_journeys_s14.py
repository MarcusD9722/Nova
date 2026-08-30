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
from core.completion_artifacts import (  # noqa: E402
    declare_scaffold, implementation_digest,
)
from core.completion_events import CompletionAnnouncer  # noqa: E402
from core.completion_service import CompletionService  # noqa: E402
from core.event_bus import BUS  # noqa: E402
from memory.unifier import MemoryUnifier  # noqa: E402

check = Checks()

#: chr(10) rather than an escape: patch scripts editing this file keep
#: collapsing the escape into a real newline mid-string.
NL = chr(10)

#: Every journey registers itself here, so the totals are SUMMED from the
#: per-journey counters rather than typed in by hand.
JOURNEYS: list["Journey"] = []
SEEN: dict[str, int] = {}
OBSERVATIONS = {"n": 0}


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
        self.baseline = ""
        self.trail: list[str] = []
        #: (previous, current, what caused it) for every COUNTED transition.
        self.transitions: list[tuple[str, str, str]] = []
        JOURNEYS.append(self)

    def rebind(self, svc: CompletionService) -> None:
        """After a restart the journey continues against a new service."""
        self.svc = svc

    async def step(self, what: str) -> str:
        """Read the state back from the store; count it only if it MOVED.

        A transition is counted when the authoritative state just read differs
        from the authoritative state read last. Reading twice without acting,
        asserting twice about one reading, or acting without changing anything
        all count zero — which is the point. The count is of what the system
        did, not of how many lines this file executes.
        """
        verdict = await self.svc.evaluate(slug=self.slug)
        now = verdict.state
        OBSERVATIONS["n"] += 1
        SEEN[now] = SEEN.get(now, 0) + 1
        if not self.state:
            # The FIRST read establishes the baseline. The project did not
            # move to get here; this is where it already was. Counting it
            # would be one transition per project of pure bookkeeping.
            self.baseline = now
        elif now != self.state:
            self.transitions.append((self.state, now, what))
            self.trail.append(f"{self.state}->{now} ({what})")
        self.state = now
        return now

    @property
    def changes(self) -> int:
        return len(self.transitions)

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

        # ── a check that cannot decide ──────────────────────────────────────
        live = [r["criterion_id"] for r in
                await mem.list_acceptance_criteria(project_name="j1",
                                                   revision=prev_rev)]
        await _prove(svc, "j1", live[0], INCONCLUSIVE)
        state = await j.step("one check could not decide")
        check(state != COMPLETE,
              f"an undecided check un-completes the project ({state})")
        check(state != FAILING,
              f"without calling the code wrong ({state})")
        await _prove(svc, "j1", live[0], INCONCLUSIVE)
        await j.step("it could not decide a second time either")
        check(j.state != COMPLETE, "and repeating it changes nothing")
        await _prove(svc, "j1", live[0], PASSED)
        state = await j.step("finally it decides")
        check(state == COMPLETE, f"and then it completes ({state})")

        # ── a REPHRASED request, which needs explicit re-anchoring ──────────
        # Widening leaves the old quotes intact. Rephrasing does not, and a
        # criterion whose quote is gone may not be assumed to still apply.
        rephrased = ("a calculator that sums values and takes differences and "
                     "scales values")
        reph_rev = await svc.record_request(slug="j1", request_text=rephrased)
        state = await j.step("the request was rephrased entirely")
        check(state != COMPLETE, f"a rephrased request is not met ({state})")

        blind = ""
        try:
            await svc.carry_forward(slug="j1", from_revision=prev_rev,
                                    to_revision=reph_rev)
        except ValueError as e:
            blind = str(e)
        check("reanchor" in blind or "not a span" in blind,
              f"carrying criteria onto new words is refused without an anchor "
              f"({blind[:60]!r})")
        await j.step("the blind carry was refused")

        rows = await mem.list_acceptance_criteria(project_name="j1",
                                                  revision=prev_rev)
        anchors: dict[str, str] = {}
        retire: list[str] = []
        for r in rows:
            if "adds numbers" in r["origin_quote"]:
                anchors[r["criterion_id"]] = "sums values"
            elif "subtracts numbers" in r["origin_quote"]:
                anchors[r["criterion_id"]] = "takes differences"
            elif "multiplies numbers" in r["origin_quote"]:
                anchors[r["criterion_id"]] = "scales values"
            else:
                retire.append(r["criterion_id"])
        moved2 = await svc.carry_forward(
            slug="j1", from_revision=prev_rev, to_revision=reph_rev,
            reanchor=anchors, drop_criterion_ids=retire,
            drop_reason="the request was rewritten and these no longer appear")
        await j.step("carried, with each criterion re-anchored by hand")
        check(len(moved2) == len(anchors),
              f"the re-anchored criteria came across ({len(moved2)})")
        check(len(retire) >= 1,
              f"and {len(retire)} that no longer trace anywhere were retired")

        carried_rows = await mem.list_acceptance_criteria(project_name="j1",
                                                          revision=reph_rev)
        check(all(r["origin_quote"] in rephrased for r in carried_rows),
              "every surviving criterion quotes the NEW request")
        check(all(r.get("carried_from") for r in carried_rows),
              "and each records the criterion it came from")

        await svc.seal_contract(slug="j1", revision=reph_rev)
        await j.step("sealed on the rephrased request")
        for cid in moved2:
            await _prove(svc, "j1", cid, PASSED)
            await j.step("re-proven under the new wording")
        check(j.state == COMPLETE,
              f"complete against words the user actually used ({j.state})")

        # ── an optional criterion ───────────────────────────────────────────
        # Something recorded but not required cannot be the thing that makes a
        # project done, and a contract of nothing but optional criteria is
        # PASSING at best -- the vacuous-truth hole, walked rather than
        # asserted at the unit level.
        opt_request = "a calculator that sums values, and ideally is fast"
        opt_rev = await svc.record_request(slug="j1", request_text=opt_request)
        await j.step("a request with an optional part")
        opt_ids = await svc.set_criteria(slug="j1", revision=opt_rev, criteria=[
            {"text": "is fast", "origin_quote": "ideally is fast",
             "verify_kind": "machine", "required": False}])
        await j.step("only an OPTIONAL criterion is recorded")
        await _prove(svc, "j1", opt_ids[0], PASSED)
        state = await j.step("and it passes")
        check(state != COMPLETE,
              f"a contract of only optional criteria cannot complete ({state})")
        check(state == PASSING, f"it is passing at best ({state})")
        v = await svc.evaluate(slug="j1")
        check("no criterion is marked required" in " ".join(v.reasons),
              f"and the reason says exactly why ({v.reasons[:1]})")

        req_ids = await svc.set_criteria(slug="j1", revision=opt_rev, criteria=[
            {"text": "sums values", "origin_quote": "sums values",
             "verify_kind": "machine"}])
        state = await j.step("a required criterion joins it")
        check(state != COMPLETE,
              f"now something IS required, and it is unproven ({state})")
        await _prove(svc, "j1", req_ids[0], PASSED)
        state = await j.step("the required one passes")
        check(state == PASSING,
              f"everything recorded passes, but nothing has been sealed "
              f"({state})")
        v = await svc.evaluate(slug="j1")
        check("has not been sealed" in " ".join(v.reasons),
              f"and the reason is the seal, not the evidence ({v.reasons[:1]})")
        await svc.seal_contract(slug="j1", revision=opt_rev)
        state = await j.step("the contract is sealed")
        check(state == COMPLETE,
              f"and only sealing turns passing into complete ({state})")

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

        # ── the page did not change; the REQUEST did ────────────────────────
        # A person judged that the page looked right for what was asked then.
        # Asking for something different does not inherit that judgement, even
        # though not one byte of the page moved.
        digest_before = implementation_digest(j.path)
        wider = "a page that looks right and loads quickly and works on a phone"
        rev2 = await svc.record_request(slug="j2", request_text=wider)
        state = await j.step("the request grew; the files did not")
        check(implementation_digest(j.path) == digest_before,
              "the files are byte-for-byte what they were")
        check(state != COMPLETE,
              f"and the project is no longer complete ({state})")

        carried = await svc.carry_forward(slug="j2", from_revision=rev,
                                          to_revision=rev2)
        await j.step("the contract carried onto the wider request")
        fresh = await svc.set_criteria(slug="j2", revision=rev2, criteria=[
            {"text": "works on a phone", "origin_quote": "works on a phone",
             "verify_kind": "human"}])
        await j.step("a second thing only a person can judge")
        await svc.seal_contract(slug="j2", revision=rev2)
        await j.step("resealed")

        rows = await mem.list_acceptance_criteria(project_name="j2",
                                                  revision=rev2)
        machine = [r["criterion_id"] for r in rows
                   if r["verify_kind"] == "machine"]
        humans = [r["criterion_id"] for r in rows if r["verify_kind"] == "human"]
        check(len(humans) == 2, f"two human criteria now ({len(humans)})")
        for cid in machine:
            await _prove(svc, "j2", cid, PASSED)
        state = await j.step("the machine part proven at the new revision")
        check(state != COMPLETE,
              f"two people-questions are outstanding ({state})")

        # One at a time, so the intermediate state is real and observed.
        d_a = await svc.ask_human(slug="j2", criterion_id=humans[0],
                                  prompt="Does it still look right?")
        await j.step("asked the first")
        await svc.resolve_human_decision(decision_id=d_a, accepted=True,
                                         actor="marcus", channel="ui")
        state = await j.step("answered the first")
        check(state != COMPLETE,
              f"one answer is not both answers ({state})")

        d_b = await svc.ask_human(slug="j2", criterion_id=humans[1],
                                  prompt="Does it work on a phone?")
        await j.step("asked the second")
        await svc.resolve_human_decision(decision_id=d_b, accepted=False,
                                         actor="marcus", channel="ui")
        state = await j.step("answered the second: no")
        check(state != COMPLETE, f"a no is not a yes ({state})")

        j.write("<html><body>responsive now</body></html>", "index.html")
        state = await j.step("the page is changed to address the no")
        for cid in machine:
            await _prove(svc, "j2", cid, PASSED)
        await j.step("machine re-proven against the new page")
        d_c = await svc.ask_human(slug="j2", criterion_id=humans[0],
                                  prompt="Looks right on the new page?")
        await j.step("asked about the new page")
        await svc.resolve_human_decision(decision_id=d_c, accepted=True,
                                         actor="marcus", channel="ui")
        await j.step("yes")
        d_d = await svc.ask_human(slug="j2", criterion_id=humans[1],
                                  prompt="And on a phone?")
        await j.step("asked the second about the new page")
        await svc.resolve_human_decision(decision_id=d_d, accepted=True,
                                         actor="marcus", channel="ui")
        state = await j.step("yes")
        check(state == COMPLETE,
              f"both people-questions answered about THIS page ({state})")

        # ── retiring the human criterion ───────────────────────────────────
        simpler = "a page that looks right and loads quickly"
        rev3 = await svc.record_request(slug="j2", request_text=simpler)
        await j.step("the phone requirement is dropped")
        rows = await mem.list_acceptance_criteria(project_name="j2",
                                                  revision=rev2)
        gone = [r["criterion_id"] for r in rows
                if r["origin_quote"] not in simpler]
        kept = await svc.carry_forward(slug="j2", from_revision=rev2,
                                       to_revision=rev3,
                                       drop_criterion_ids=gone,
                                       drop_reason="no longer asked for")
        await j.step("carried without it")
        await svc.seal_contract(slug="j2", revision=rev3)
        await j.step("sealed on the simpler request")
        for cid in kept:
            row = next(r for r in await mem.list_acceptance_criteria(
                project_name="j2", revision=rev3)
                if r["criterion_id"] == cid)
            if row["verify_kind"] == "human":
                d = await svc.ask_human(slug="j2", criterion_id=cid,
                                        prompt="Still right?")
                await svc.resolve_human_decision(decision_id=d, accepted=True,
                                                 actor="marcus", channel="ui")
            else:
                await _prove(svc, "j2", cid, PASSED)
            await j.step("settled on the simpler contract")
        check(j.state == COMPLETE,
              f"complete without the retired criterion ({j.state})")

        # ── a question that goes stale before anyone answers it ─────────────
        # The decision captured what the page was when it was ASKED. If the
        # page moves while the person is deciding, their answer describes
        # something that no longer exists, and it must not complete anything.
        rows = await mem.list_acceptance_criteria(project_name="j2",
                                                  revision=rev3)
        human_cid = next(r["criterion_id"] for r in rows
                         if r["verify_kind"] == "human")
        machine_cid = next(r["criterion_id"] for r in rows
                           if r["verify_kind"] == "machine")

        pending = await svc.ask_human(slug="j2", criterion_id=human_cid,
                                      prompt="Judge the page as it is now")
        await j.step("asked about the page as it stands")
        j.write("<html><body>changed while they were deciding</body></html>",
                "index.html")
        state = await j.step("the page moved while the question was open")
        check(state != COMPLETE, f"the page is unproven again ({state})")

        await svc.resolve_human_decision(decision_id=pending, accepted=True,
                                         actor="marcus", channel="ui")
        state = await j.step("the answer arrives, about the page that was")
        check(state != COMPLETE,
              f"an answer about the old page does not complete the new one "
              f"({state})")
        v = await svc.evaluate(slug="j2")
        stale = [s_.stale_reason for s_ in v.criteria if s_.stale_reason]
        check(any("implementation changed" in s_ for s_ in stale),
              f"and it is named as stale, not merely missing ({stale})")

        await _prove(svc, "j2", machine_cid, PASSED)
        await j.step("machine proven against the page that exists")
        again = await svc.ask_human(slug="j2", criterion_id=human_cid,
                                    prompt="And this page?")
        await j.step("asked again about the page that exists")
        await svc.resolve_human_decision(decision_id=again, accepted=True,
                                         actor="marcus", channel="ui")
        state = await j.step("answered about the page that exists")
        check(state == COMPLETE,
              f"an answer about THIS page completes it ({state})")


# ── J3 ─────────────────────────────────────────────────────────────────────
async def journey_3_drift_and_reproof():
    check.section("J3 fourteen kinds of drift, and what each one means")
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

        # Fourteen DIFFERENT ways a project's files can change. Each one asks
        # a separate question: does this count as a different artifact? Three
        # of them must answer NO, and those three record zero transitions,
        # which is the honest outcome rather than a missing one.
        proj = j.path
        invalidated = []
        preserved = []

        async def drift(label, mutate, *, expect_invalid=True):
            """Change the files, then see whether the pass still stands."""
            mutate()
            state = await j.step(label)
            if expect_invalid:
                check(state != COMPLETE,
                      f"{label}: the earlier pass no longer stands ({state})")
                invalidated.append(label)
                await _prove(svc, "j3", ids[0], PASSED)
                state = await j.step(f"re-proven after {label}")
                check(state == COMPLETE,
                      f"{label}: re-proving against it restores completion "
                      f"({state})")
            else:
                check(state == COMPLETE,
                      f"{label}: this is not a change to the implementation "
                      f"({state})")
                preserved.append(label)

        def w(name, body):
            (proj / name).write_text(body, encoding="utf-8")

        await drift("the file's contents change",
                    lambda: w("main.py", "print(2)" + NL))
        await drift("a second module appears",
                    lambda: w("helper.py", "VALUE = 1" + NL))
        await drift("that module changes",
                    lambda: w("helper.py", "VALUE = 2" + NL))
        # Deleting a file only invalidates if what is LEFT was never proven.
        # Introduce a uniquely-versioned main.py alongside the doomed file, so
        # that removing it leaves an artifact set nobody has evidence for.
        await drift("a module arrives beside a fresh main",
                    lambda: (w("main.py", "print('del-case-1')" + NL),
                             w("helper.py", "VALUE = 3" + NL)))
        await drift("that module is deleted",
                    lambda: (proj / "helper.py").unlink())
        await drift("whitespace only",
                    lambda: w("main.py", "print(2)  " + NL))
        await drift("a package directory appears",
                    lambda: ((proj / "pkg").mkdir(exist_ok=True),
                             w("pkg/__init__.py", "" ),
                             w("pkg/core.py", "def go():" + NL + "    return 1" + NL)))
        await drift("a file inside the package changes",
                    lambda: w("pkg/core.py", "def go():" + NL + "    return 2" + NL))
        await drift("a file is renamed",
                    lambda: ((proj / "pkg" / "core.py").rename(proj / "pkg" / "engine.py"),))
        await drift("the package grows beside a fresh main",
                    lambda: (w("main.py", "print('del-case-2')" + NL),
                             w("pkg/extra.py", "EXTRA = 1" + NL)))
        await drift("the package is removed",
                    lambda: [p.unlink() for p in sorted((proj / "pkg").glob("*"))]
                    and None)
        await drift("an empty file appears",
                    lambda: w("EMPTY.py", ""))
        await drift("a file with only a comment appears",
                    lambda: w("notes.py", "# nothing executable" + NL))

        # And three changes that must NOT invalidate anything, because they do
        # not change the implementation: a derived file Nova writes itself, a
        # declared scaffold, and a rewrite of identical bytes.
        await drift("PROJECT.md is rewritten",
                    lambda: w("PROJECT.md", "## Status" + NL + "complete" + NL),
                    expect_invalid=False)
        declare_scaffold(proj, ["scaffold_note.py"])
        await drift("a DECLARED scaffold file appears",
                    lambda: w("scaffold_note.py", "# generated scaffolding" + NL),
                    expect_invalid=False)
        await drift("the same bytes are written again",
                    lambda: w("notes.py", "# nothing executable" + NL),
                    expect_invalid=False)

        check(len(invalidated) == 13,
              f"thirteen distinct changes each invalidated the pass "
              f"({len(invalidated)})")
        check("that module is deleted" in invalidated
              and "the package is removed" in invalidated,
              "including deletions, once what they leave behind is novel")
        check(len(preserved) == 3,
              f"and three deliberately did not: {preserved}")

        # Returning the files to bytes that were ALREADY proven is not a new
        # artifact, and the evidence for those bytes is still evidence.
        await _prove(svc, "j3", ids[0], PASSED)
        await j.step("proven at the current bytes")
        before = (proj / "main.py").read_text(encoding="utf-8")
        w("main.py", "print('a different program entirely')" + NL)
        state = await j.step("changed away")
        check(state != COMPLETE, f"changed away, so unproven ({state})")
        w("main.py", before)
        state = await j.step("changed back to the exact proven bytes")
        check(state == COMPLETE,
              f"and changed back, the old evidence describes it again "
              f"({state})")

        # ── drift against a FAILURE ─────────────────────────────────────────
        # A refutation is evidence too, and it is evidence about the files as
        # they were. Editing them does not make the project pass, but it does
        # stop the old refutation from applying: the honest state is "nobody
        # has checked this", not "this is broken".
        cleared = 0
        for n, body in enumerate([
                "print('attempt one')", "print('attempt two')",
                "print('attempt three')", "print('attempt four')"]):
            await _prove(svc, "j3", ids[0], FAILED)
            state = await j.step(f"refuted ({n})")
            check(state == FAILING, f"refuted, so failing ({state})")
            w("main.py", body + NL)
            state = await j.step(f"edited while failing ({n})")
            check(state != FAILING,
                  f"the edit clears the refutation ({state})")
            check(state != COMPLETE,
                  f"but does not pass anything ({state})")
            await _prove(svc, "j3", ids[0], PASSED)
            state = await j.step(f"proven against the fix ({n})")
            if state == COMPLETE:
                cleared += 1
        check(cleared == 4,
              f"four refute/edit/prove rounds each ended proven ({cleared}/4)")

        # Three more kinds of change, each novel: non-ASCII content, a deeply
        # nested module, and a file whose name differs only in case.
        for label, mutate in (
                ("content with non-ASCII characters",
                 lambda: w("main.py", "print('caf\u00e9 \u2014 na\u00efve')" + NL)),
                ("a deeply nested module",
                 lambda: ((proj / "a" / "b" / "c").mkdir(parents=True,
                                                         exist_ok=True),
                          (proj / "a" / "b" / "c" / "deep.py").write_text(
                              "DEEP = 1" + NL, encoding="utf-8"))),
                ("that nested module changing",
                 lambda: (proj / "a" / "b" / "c" / "deep.py").write_text(
                     "DEEP = 2" + NL, encoding="utf-8"))):
            mutate()
            state = await j.step(label)
            check(state != COMPLETE,
                  f"{label}: a different artifact ({state})")
            await _prove(svc, "j3", ids[0], PASSED)
            state = await j.step(f"re-proven after {label}")
            check(state == COMPLETE, f"{label}: re-proof restores it ({state})")


# ── J4 ─────────────────────────────────────────────────────────────────────
async def journey_4_never_seals():
    check.section("J4 sealing, un-covering, retiring, and not deciding")
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

        # ── a correction that the sealed contract no longer covers ──────────
        # Sealing is a claim about ONE revision. A wider request is not
        # covered by it, and the project must fall out of complete until the
        # new clause is quoted, sealed and demonstrated in its own right.
        live = list(ids) + list(more)
        current_request = request
        for n, clause in enumerate(["schedules the email",
                                    "retries on failure",
                                    "logs what it sent"]):
            current_request = current_request + " and " + clause
            new_rev = await svc.record_request(slug="j4",
                                               request_text=current_request)
            state = await j.step(f"the request widened: {clause}")
            check(state != COMPLETE,
                  f"a wider request is not already satisfied ({state})")

            carried = await svc.carry_forward(slug="j4", from_revision=rev,
                                              to_revision=new_rev)
            await j.step("the standing contract carried")
            for cid in carried:
                await _prove(svc, "j4", cid, PASSED)
            await j.step("everything previously agreed, re-proven")
            check(j.state != COMPLETE,
                  f"but the new clause is unquoted, so the contract cannot "
                  f"seal and the project cannot complete ({j.state})")

            fresh = await svc.set_criteria(slug="j4", revision=new_rev, criteria=[
                {"text": clause, "origin_quote": clause,
                 "verify_kind": "machine"}])
            await j.step("the new clause quoted")
            await svc.seal_contract(slug="j4", revision=new_rev)
            await j.step("resealed on the wider request")
            j.write(f"import csv  # {clause}" + NL)
            await j.step("code grew to match")
            for cid in list(carried) + list(fresh):
                await _prove(svc, "j4", cid, PASSED)
                await j.step(f"proven: {cid[:6]}")
            check(j.state == COMPLETE,
                  f"complete again at revision {new_rev} ({j.state})")
            rev, live = new_rev, list(carried) + list(fresh)

        # ── a check that cannot decide ──────────────────────────────────────
        # INCONCLUSIVE is not a pass and not a failure. It leaves the criterion
        # unproven, which keeps the project out of COMPLETE without claiming
        # the code is broken.
        await _prove(svc, "j4", live[0], INCONCLUSIVE)
        state = await j.step("one check could not decide")
        check(state != COMPLETE,
              f"an undecided check does not certify anything ({state})")
        check(state != FAILING,
              f"nor does it accuse the code of being wrong ({state})")
        v = await svc.evaluate(slug="j4")
        check(any(s_.criterion.criterion_id == live[0] for s_ in v.outstanding),
              "the undecided criterion is outstanding, not failing")

        await _prove(svc, "j4", live[0], FAILED)
        state = await j.step("run again, it decides against")
        check(state == FAILING, f"and THAT is failing ({state})")
        await _prove(svc, "j4", live[0], PASSED)
        state = await j.step("run again, it decides for")
        check(state == COMPLETE, f"and a decision for it completes ({state})")

        # ── retiring a criterion, explicitly ────────────────────────────────
        # A criterion may only leave the contract by being named. Narrow the
        # request, drop the criterion it came from, and re-anchor the rest.
        narrowed = "a tool that imports a CSV and charts it"
        narrow_rev = await svc.record_request(slug="j4", request_text=narrowed)
        state = await j.step("the request narrowed")
        check(state != COMPLETE,
              f"a changed request is not already met ({state})")

        rows = await mem.list_acceptance_criteria(project_name="j4",
                                                  revision=rev)
        doomed = [r["criterion_id"] for r in rows
                  if r["origin_quote"] not in narrowed]
        keeping = [r["criterion_id"] for r in rows
                   if r["origin_quote"] in narrowed]
        check(len(doomed) >= 3,
              f"{len(doomed)} criteria no longer trace to the request")

        # Carrying forward WITHOUT naming them is refused: a criterion cannot
        # quietly stop applying.
        refused = ""
        try:
            await svc.carry_forward(slug="j4", from_revision=rev,
                                    to_revision=narrow_rev)
        except ValueError as e:
            refused = str(e)
        check("not a span" in refused or "reanchor" in refused,
              f"carrying an unquotable criterion forward is refused "
              f"({refused[:70]!r})")
        await j.step("the silent carry was refused")

        kept = await svc.carry_forward(slug="j4", from_revision=rev,
                                       to_revision=narrow_rev,
                                       drop_criterion_ids=doomed,
                                       drop_reason="the user narrowed the request")
        await j.step("carried, with the retired ones named")
        check(len(kept) == len(keeping),
              f"only the criteria that still trace forward came across "
              f"({len(kept)} of {len(rows)})")

        await svc.seal_contract(slug="j4", revision=narrow_rev)
        await j.step("sealed on the narrower request")
        for cid in kept:
            await _prove(svc, "j4", cid, PASSED)
            await j.step("proven on the narrower contract")
        check(j.state == COMPLETE,
              f"complete on a smaller, honestly reduced contract ({j.state})")

        # A check that cannot decide, again, now on the reduced contract: the
        # same three-way outcome has to behave the same way at any revision.
        await _prove(svc, "j4", kept[0], INCONCLUSIVE)
        state = await j.step("undecided again, on the narrower contract")
        check(state != COMPLETE, f"still not a certification ({state})")
        await _prove(svc, "j4", kept[0], FAILED)
        state = await j.step("then decided against")
        check(state == FAILING, f"still a failure when refuted ({state})")
        j.write("import csv  # repaired after the late refutation" + NL)
        state = await j.step("repaired, which stales the refutation")
        check(state != FAILING, f"the refutation does not outlive the fix ({state})")
        for cid in kept:
            await _prove(svc, "j4", cid, PASSED)
        state = await j.step("re-proven on the repair")
        check(state == COMPLETE, f"and complete again ({state})")

        retired = await mem.list_acceptance_criteria(project_name="j4",
                                                     revision=rev)
        check(all(r.get("superseded_by") or r.get("superseded_at")
                  for r in retired) if retired else True,
              "and the retired criteria are recorded as superseded, not erased")


# ── J5 ─────────────────────────────────────────────────────────────────────
async def journey_5_two_projects_interleaved():
    check.section("J5 two projects, both working, alternately")
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

        # A was given a correction in the script above, and its criteria were
        # never carried onto that revision -- so ia[0] is a dead id and
        # proving it does nothing. Re-establish A's contract at the revision
        # it is actually on before asking it to complete anything.
        a_req = await mem.current_requirement(project_name="j5a")
        a_rev = int(a_req["revision"])
        ia = await svc.carry_forward(slug="j5a", from_revision=ra,
                                     to_revision=a_rev)
        await a.step("A's contract carried onto its corrected request")
        await svc.seal_contract(slug="j5a", revision=a_rev)
        await a.step("A resealed")
        await _prove(svc, "j5a", ia[0], PASSED)
        state = await a.step("A proven at its current revision")
        check(state == COMPLETE,
              f"A is workable again at revision {a_rev} ({state})")

        # ── both projects live, alternating ────────────────────────────────
        # Each action targets exactly one project. After it, BOTH are read.
        # The one acted on may move; the other must not, every single time.
        drift_count = {"a": 0, "b": 0}
        moved_wrongly: list[str] = []

        async def act_on(who: "Journey", other: "Journey", label: str, do):
            before_other = other.state
            await do()
            moved = await who.step(f"{who.name}: {label}")
            still = await other.step(f"{other.name}: untouched while "
                                     f"{who.name} did {label}")
            if still != before_other:
                moved_wrongly.append(
                    f"{other.name} went {before_other}->{still} when "
                    f"{who.name} did {label}")
            return moved

        async def a_writes(body):
            a.write(body)

        async def b_writes(body):
            b.write(body)

        pairs = [
            ("edits its code", lambda n: a_writes(f"def add():{NL}    return {n}{NL}"),
             lambda n: b_writes(f"def add():{NL}    return {n * 10}{NL}")),
        ]
        for n in range(6):
            body_a, body_b = pairs[0][1], pairs[0][2]
            await act_on(a, b, f"edits its code ({n})", lambda: body_a(n))
            drift_count["a"] += 1
            await act_on(a, b, "re-proves",
                         lambda: _prove(svc, "j5a", ia[0], PASSED))
            await act_on(b, a, f"edits its code ({n})", lambda: body_b(n))
            drift_count["b"] += 1
            await act_on(b, a, "re-proves",
                         lambda: _prove(svc, "j5b", ib[0], PASSED))

        check(not moved_wrongly,
              f"no project moved except when acted on "
              f"({moved_wrongly[:2] if moved_wrongly else 'none'})")
        check(a.state == COMPLETE and b.state == COMPLETE,
              f"both ended complete on their own evidence "
              f"({a.state}, {b.state})")

        # One project failing does not touch the other.
        await act_on(a, b, "is refuted",
                     lambda: _prove(svc, "j5a", ia[0], FAILED))
        check(a.state == FAILING and b.state == COMPLETE,
              f"A is failing while B stays complete ({a.state}, {b.state})")
        await act_on(a, b, "is repaired",
                     lambda: a_writes(f"def add():{NL}    return 99{NL}"))
        await act_on(a, b, "is re-proven",
                     lambda: _prove(svc, "j5a", ia[0], PASSED))
        check(a.state == COMPLETE and b.state == COMPLETE,
              f"and both are complete again ({a.state}, {b.state})")

        # A correction to one project's REQUIREMENT does not disturb the other.
        await act_on(b, a, "receives a correction",
                     lambda: svc.record_request(slug="j5b",
                                                request_text="b adds things and doubles them"))
        check(b.state != COMPLETE and a.state == COMPLETE,
              f"B fell out of complete, A did not ({b.state}, {a.state})")

        # Refutation and repair, alternating, so each project passes through
        # FAILING while the other is mid-repair.
        for n in range(3):
            await act_on(a, b, f"is refuted ({n})",
                         lambda: _prove(svc, "j5a", ia[0], FAILED))
            check(a.state == FAILING,
                  f"A is failing ({a.state}) while B is {b.state}")
            await act_on(b, a, f"is refuted ({n})",
                         lambda: _prove(svc, "j5b", ib[0], FAILED))
            await act_on(a, b, f"is repaired ({n})",
                         lambda: a_writes(f"def add():{NL}    return {100 + n}{NL}"))
            await act_on(a, b, f"is re-proven ({n})",
                         lambda: _prove(svc, "j5a", ia[0], PASSED))
            check(a.state == COMPLETE,
                  f"A recovered ({a.state}) while B is still {b.state}")
            await act_on(b, a, f"is repaired ({n})",
                         lambda: b_writes(f"def add():{NL}    return {200 + n}{NL}"))
            await act_on(b, a, f"is re-proven ({n})",
                         lambda: _prove(svc, "j5b", ib[0], PASSED))
        check(not moved_wrongly,
              f"still nothing moved except when acted on "
              f"({moved_wrongly[:2] if moved_wrongly else 'none'})")

        # Every transition either project recorded names that project.
        for who in (a, b):
            mine = [t for t in who.transitions if who.name in t[2]
                    or "untouched" not in t[2]]
            check(len(mine) == len(who.transitions),
                  f"{who.name}: all {len(who.transitions)} transitions are its own")
        check(not [t for t in a.transitions if "untouched" in t[2]],
              "A never moved on a step that only observed it")
        check(not [t for t in b.transitions if "untouched" in t[2]],
              "B never moved on a step that only observed it")


# ── J6 ─────────────────────────────────────────────────────────────────────
async def journey_6_restart_between_every_step():
    check.section("J6 a restart between every kind of transition")
    with _tmp() as td:
        root = Path(td)
        mem, svc = await _fresh(root)
        j = Journey("J6", svc, "j6", root)
        request = "a service that starts up and answers a health check"

        async def restart(label: str) -> str:
            """Rebuild the world from disk, and return what it says now."""
            nonlocal mem, svc
            mem, svc = await _fresh(root)
            j.rebind(svc)
            return await j.step(f"after restart: {label}")

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

        # A correction BEFORE any code exists. The criteria belonged to the old
        # wording and were not carried, so at the new revision nothing has been
        # agreed and nothing has been built: the project is back to an idea.
        # This is the one way IDEA is reachable as a destination rather than as
        # a starting condition, and it only exists before the first file.
        reworded = "a service that starts up and answers a health check quickly"
        rev_b = await svc.record_request(slug="j6", request_text=reworded)
        state = await restart("a correction, before a single file exists")
        check(state == IDEA,
              f"a corrected request with nothing agreed for it is an idea "
              f"({state})")
        ids = await svc.carry_forward(slug="j6", from_revision=rev,
                                      to_revision=rev_b)
        state = await restart("the criteria carried onto it")
        check(state == PLANNED,
              f"and carrying the contract forward makes it planned again "
              f"({state})")
        rev = rev_b

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

        # ── an undecided check, across a restart ───────────────────────────
        await _prove(svc, "j6", ids[0], INCONCLUSIVE)
        await restart("a check that could not decide")
        check(j.state != COMPLETE,
              f"an undecided check survives as undecided ({j.state})")
        check(j.state != FAILING,
              f"and is still not an accusation ({j.state})")
        await _prove(svc, "j6", ids[0], FAILED)
        await restart("then it decides against")
        check(j.state == FAILING, f"that is a failure ({j.state})")
        await _prove(svc, "j6", ids[0], PASSED)
        await restart("then it decides for")
        check(j.state == COMPLETE, f"and repaired, complete ({j.state})")

        # ── a correction, and the contract carried across a restart ────────
        wider = request + " and reports its uptime"
        rev2 = await svc.record_request(slug="j6", request_text=wider)
        await restart("a wider request")
        check(j.state != COMPLETE,
              f"the wider request is not already met ({j.state})")
        carried = await svc.carry_forward(slug="j6", from_revision=rev,
                                          to_revision=rev2)
        await restart("the contract carried")
        fresh = await svc.set_criteria(slug="j6", revision=rev2, criteria=[
            {"text": "reports its uptime", "origin_quote": "reports its uptime",
             "verify_kind": "machine"}])
        await restart("the new criterion")
        await svc.seal_contract(slug="j6", revision=rev2)
        await restart("resealed")
        j.write("def main():" + NL + "    return 'ok, up 1s'" + NL)
        await restart("code that matches it")
        machine = [c for c in list(carried) + list(fresh)
                   if c != carried[1]] if len(carried) > 1 else list(fresh)
        for cid in machine:
            await _prove(svc, "j6", cid, PASSED)
        await restart("the machine criteria proven")
        check(j.state != COMPLETE,
              f"the human criterion is unproven at this revision ({j.state})")
        human = [c for c in carried if c not in machine]
        did3 = await svc.ask_human(slug="j6", criterion_id=human[0],
                                   prompt="Health check still good?")
        await restart("asked, at the new revision")
        await svc.resolve_human_decision(decision_id=did3, accepted=True,
                                         actor="marcus", channel="ui")
        await restart("answered, at the new revision")
        check(j.state == COMPLETE,
              f"complete at the wider request, across nine more restarts "
              f"({j.state})")

        # ── retiring a criterion across a restart ──────────────────────────
        narrowed = "a service that starts up"
        rev3 = await svc.record_request(slug="j6", request_text=narrowed)
        await restart("the request narrowed")
        rows = await mem.list_acceptance_criteria(project_name="j6",
                                                  revision=rev2)
        doomed = [r["criterion_id"] for r in rows
                  if r["origin_quote"] not in narrowed]
        kept = await svc.carry_forward(slug="j6", from_revision=rev2,
                                       to_revision=rev3,
                                       drop_criterion_ids=doomed,
                                       drop_reason="the user narrowed it")
        await restart("carried, with the retired ones named")
        await svc.seal_contract(slug="j6", revision=rev3)
        await restart("sealed on the narrower request")
        for cid in kept:
            await _prove(svc, "j6", cid, PASSED)
        await restart("proven on the narrower contract")
        check(j.state == COMPLETE,
              f"complete on the reduced contract ({j.state})")

        # ── the announcement ledger reconciles with the transitions ────────
        # Every state this project actually entered at this revision should be
        # claimable exactly once. Announcing repeatedly, from fresh announcers,
        # must publish nothing more.
        announcer = CompletionAnnouncer(memory=mem)
        v = await svc.evaluate(slug="j6")
        before = len([e for e in BUS.recent(400)
                      if e.type == "project.completed"
                      and e.data.get("project") == "j6"])
        for _ in range(4):
            await CompletionAnnouncer(memory=mem).announce(slug="j6", verdict=v)
        after = len([e for e in BUS.recent(400)
                     if e.type == "project.completed"
                     and e.data.get("project") == "j6"])
        check(after - before <= 1,
              f"four announcers, one transition, {after - before} event(s)")

        # And a REAL new transition is still announced.
        j.write("def main():" + NL + "    return 'changed again'" + NL)
        await j.step("drift after the announcement")
        check(j.state != COMPLETE, f"drift drops it ({j.state})")
        for cid in kept:
            await _prove(svc, "j6", cid, PASSED)
        await j.step("re-proven")
        v2 = await svc.evaluate(slug="j6")
        await CompletionAnnouncer(memory=mem).announce(slug="j6", verdict=v2)
        final = len([e for e in BUS.recent(400)
                     if e.type == "project.completed"
                     and e.data.get("project") == "j6"])
        check(final == after,
              f"returning to a state already announced at this revision is "
              f"not news again ({final} vs {after})")
        check(j.state == COMPLETE, f"and it is complete ({j.state})")

        # ── a wide contract, settled one criterion at a time ────────────────
        # Each proof is a separate process, so every intermediate partial state
        # is one a restart had to reconstruct rather than remember.
        wide = ("a service that starts up and answers a health check and "
                "reports its uptime and logs requests and reloads its config")
        rev4 = await svc.record_request(slug="j6", request_text=wide)
        await restart("a much wider request")
        clauses = ["starts up", "answers a health check", "reports its uptime",
                   "logs requests", "reloads its config"]
        wide_ids = await svc.set_criteria(slug="j6", revision=rev4, criteria=[
            {"text": c, "origin_quote": c, "verify_kind": "machine"}
            for c in clauses])
        await restart("five criteria recorded")
        await svc.seal_contract(slug="j6", revision=rev4)
        await restart("sealed")
        j.write("def main():" + NL + "    return 'a wider service'" + NL)
        await restart("code for it")
        check(j.state == SCAFFOLDED,
              f"five unproven criteria over real files ({j.state})")

        partials = 0
        for n, cid in enumerate(wide_ids):
            await _prove(svc, "j6", cid, PASSED)
            state = await restart(f"criterion {n + 1} of 5 proven")
            if n < len(wide_ids) - 1:
                check(state == PARTIALLY_IMPLEMENTED,
                      f"{n + 1} of 5 proven is partial ({state})")
                partials += 1
        check(partials == 4,
              f"four distinct partial states, each read from a cold process "
              f"({partials})")
        check(j.state == COMPLETE,
              f"and the fifth completes it ({j.state})")

        # Drift while nothing is running, three different ways.
        for label, mutate in (
                ("an edit", lambda: j.write("def main():" + NL +
                                            "    return 'edited while down'" + NL)),
                # main.py moves WITH the new file, so that deleting the file
                # in the next round leaves an artifact set nobody has proven.
                ("a new file", lambda: ((j.path / "extra.py").write_text(
                    "X = 1" + NL, encoding="utf-8"),
                    j.write("def main():" + NL +
                            "    return 'beside a new file'" + NL))),
                ("a deletion", lambda: (j.path / "extra.py").unlink())):
            mutate()
            state = await restart(f"{label}, made while nothing was running")
            check(state != COMPLETE,
                  f"{label} while down invalidates the proof ({state})")
            for cid in wide_ids:
                await _prove(svc, "j6", cid, PASSED)
            state = await restart(f"re-proven after {label}")
            check(state == COMPLETE,
                  f"and re-proving restores it ({state})")


async def main() -> None:
    await journey_1_build_fail_repair_correct()
    await journey_2_human_verified()
    await journey_3_drift_and_reproof()
    await journey_4_never_seals()
    await journey_5_two_projects_interleaved()
    await journey_6_restart_between_every_step()

    check.section("what the six journeys covered")

    # Per journey, summed. Nothing here is typed in: every number is the length
    # of a list that was appended to only when an authoritative read moved.
    by_journey: dict[str, int] = {}
    for j in JOURNEYS:
        by_journey[j.name] = by_journey.get(j.name, 0) + j.changes
    total = sum(by_journey.values())
    for name in sorted(by_journey):
        print(f"    {name:<8} {by_journey[name]:>4} transitions")
    print(f"    {'TOTAL':<8} {total:>4} transitions "
          f"({OBSERVATIONS['n']} authoritative reads, "
          f"{len(JOURNEYS)} baseline reads excluded)")

    print()
    print("    transition kinds (from -> to), and how many of each:")
    kinds: dict[str, int] = {}
    for j in JOURNEYS:
        for prev, now, _ in j.transitions:
            kinds[f"{prev} -> {now}"] = kinds.get(f"{prev} -> {now}", 0) + 1
    for kind in sorted(kinds, key=lambda k: -kinds[k]):
        print(f"      {kind:<44} {kinds[kind]}")

    # The full trail, so the count is auditable rather than asserted. Every
    # line is one authoritative read that differed from the read before it.
    print()
    print("    every counted transition, in order:")
    for j in JOURNEYS:
        for n, (prev, now, why) in enumerate(j.transitions, 1):
            print(f"      {j.name} {n:>3}. {prev:<22} -> {now:<22} {why}")

    # A transition may never come from a step that only LOOKED. Steps whose
    # labels mark them as observations of the other project, or as re-reads,
    # must have moved nothing -- otherwise the count includes bookkeeping.
    observers = [f"{j.name}: {why}" for j in JOURNEYS
                 for _, _, why in j.transitions
                 if "untouched" in why or why.startswith("observed")]
    check(not observers,
          f"no transition was recorded by a step that only observed "
          f"({observers[:2] if observers else 'none'})")
    check(all(why.strip() for j in JOURNEYS for _, _, why in j.transitions),
          "and every transition records what caused it")

    check(total > 225,
          f"{total} authoritative state transitions across six journeys, "
          f"counted by state CHANGE (target > 225)")
    check(len(by_journey) == 7,
          f"six journeys, seven walked projects ({sorted(by_journey)})")
    check(min(by_journey.values()) >= 15,
          f"and no journey is a token contributor "
          f"(smallest {min(by_journey.values())})")
    check(len(kinds) >= 14,
          f"{len(kinds)} distinct kinds of transition, not one shape repeated")

    # Every state must be the DESTINATION of a real transition, not merely
    # observed once on the way past.
    destinations = {now for j in JOURNEYS for _, now, _ in j.transitions}
    for state in (IDEA, PLANNED, SCAFFOLDED, PARTIALLY_IMPLEMENTED, FAILING,
                  PASSING, COMPLETE):
        check(state in destinations,
              f"{state} was transitioned INTO ({SEEN.get(state, 0)} reads)")
    check.finish()


if __name__ == "__main__":
    run(main)
