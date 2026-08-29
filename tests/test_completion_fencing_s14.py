"""The fences, attacked through the public service (Stage 14 §5/§6).

Every case here goes through `CompletionService`. None of them reaches into
the backend to manufacture the stale row it wants to see — a test that plants
its own evidence proves only that the planting worked, and would have passed
against every one of the six holes these cases were written to find.

  1. time-of-check vs time-of-record: a pass earned against H1 must not
     certify H2 just because H2 is what was on disk when the result returned
  2. an empty digest must not act as a wildcard over every future artifact
  3. a user's own `test_engine.py` IS the implementation
  4. an origin quote must be a real span of the request, and a decomposition
     that drops a requested capability must not be able to reach COMPLETE
  5. only a person may accept a criterion on a person's behalf
  6. a crash midway through building a contract must not leave a live one

Run:  venv\\Scripts\\python.exe tests\\test_completion_fencing_s14.py
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

from core.completion import COMPLETE, PASSED, WAIVED  # noqa: E402
from core.completion_artifacts import implementation_files  # noqa: E402
from core.completion_service import CompletionService  # noqa: E402
from memory.unifier import MemoryUnifier  # noqa: E402

check = Checks()

SLUG = "calculator"
REQUEST = "a calculator that can add and subtract two numbers"


class World:
    def __init__(self, td: str):
        self.root = Path(td)
        self.projects = self.root / "projects"
        self.path = self.projects / SLUG
        self.path.mkdir(parents=True)
        self.mem = MemoryUnifier(self.root / "memory_data", enable_chroma=False)
        self.svc = CompletionService(memory=self.mem, projects_dir=self.projects)

    async def start(self):
        await self.mem.initialize()
        return self

    def write(self, name: str, body: str):
        (self.path / name).write_text(body, encoding="utf-8")

    async def contract(self, request=REQUEST, criteria=None):
        rev = await self.svc.record_request(slug=SLUG, request_text=request)
        ids = await self.svc.set_criteria(slug=SLUG, revision=rev, criteria=(
            criteria if criteria is not None else [
                {"text": "adds two numbers", "origin_quote": "add"},
                {"text": "subtracts two numbers", "origin_quote": "subtract"}]))
        await self.svc.seal_contract(slug=SLUG, revision=rev)
        return rev, ids


def _tmp():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


async def test_1a_a_pass_earned_against_h1_does_not_certify_h2():
    check.section("§6 time of check is not time of record (artifact)")
    with _tmp() as td:
        w = await World(td).start()
        rev, (add_id, sub_id) = await w.contract()
        w.write("main.py", "def add(a, b):\n    return a + b\n\n"
                           "def subtract(a, b):\n    return a - b\n")

        # A check BEGINS against what is on disk now.
        ctx_add = await w.svc.begin_check(slug=SLUG, criterion_id=add_id)
        ctx_sub = await w.svc.begin_check(slug=SLUG, criterion_id=sub_id)

        # While it runs, the implementation changes underneath it.
        w.write("main.py", "def add(a, b):\n    return a * b   # wrong now\n")

        # The result comes back and is recorded.
        await w.svc.record_verdict(context=ctx_add, verdict=PASSED)
        await w.svc.record_verdict(context=ctx_sub, verdict=PASSED)

        v = await w.svc.evaluate(slug=SLUG)
        check(v.state != COMPLETE,
              f"a result earned against the OLD code does not complete the new "
              f"code ({v.state})")
        rows = await w.mem.list_acceptance_evidence(project_name=SLUG)
        check(rows and rows[0]["artifact_digest"] == ctx_add.artifact_digest,
              "the evidence is stamped with what the check actually examined")
        check(any("implementation changed" in s.stale_reason for s in v.criteria),
              "and is reported as belonging to a different implementation")


async def test_1b_a_pass_earned_under_r1_does_not_certify_r2():
    check.section("§6 time of check is not time of record (revision)")
    with _tmp() as td:
        w = await World(td).start()
        rev1, (add_id, sub_id) = await w.contract()
        w.write("main.py", "def add(a, b):\n    return a + b\n\n"
                           "def subtract(a, b):\n    return a - b\n")

        ctx_add = await w.svc.begin_check(slug=SLUG, criterion_id=add_id)
        ctx_sub = await w.svc.begin_check(slug=SLUG, criterion_id=sub_id)

        # The user corrects the requirement while the checks are in flight.
        rev2 = await w.svc.record_request(slug=SLUG,
                                          request_text=REQUEST + " and multiply")
        await w.svc.carry_forward(slug=SLUG, from_revision=rev1, to_revision=rev2)
        await w.svc.set_criteria(slug=SLUG, revision=rev2, criteria=[
            {"text": "multiplies two numbers", "origin_quote": "multiply"}])
        await w.svc.seal_contract(slug=SLUG, revision=rev2)

        await w.svc.record_verdict(context=ctx_add, verdict=PASSED)
        await w.svc.record_verdict(context=ctx_sub, verdict=PASSED)

        rows = await w.mem.list_acceptance_evidence(project_name=SLUG)
        check(all(r["revision"] == rev1 for r in rows),
              f"the late results belong to the revision they were run for "
              f"({sorted({r['revision'] for r in rows})})")
        v = await w.svc.evaluate(slug=SLUG)
        check(v.state != COMPLETE,
              f"and cannot complete the revision that replaced it ({v.state})")


async def test_2_an_empty_digest_is_not_a_wildcard():
    check.section("§6 evidence recorded against nothing certifies nothing")
    with _tmp() as td:
        w = await World(td).start()
        rev, (add_id, sub_id) = await w.contract()
        check(implementation_files(w.path) == [],
              "no implementation exists yet")

        # A check recorded while there is nothing to check.
        ctx_a = await w.svc.begin_check(slug=SLUG, criterion_id=add_id)
        ctx_b = await w.svc.begin_check(slug=SLUG, criterion_id=sub_id)
        await w.svc.record_verdict(context=ctx_a, verdict=PASSED)
        await w.svc.record_verdict(context=ctx_b, verdict=PASSED)

        # Now an implementation appears.
        w.write("main.py", "def add(a, b):\n    return a + b\n")
        v = await w.svc.evaluate(slug=SLUG)
        check(v.state != COMPLETE,
              f"evidence gathered against no implementation does not certify "
              f"the one that arrives later ({v.state})")


async def test_3_a_users_own_test_file_is_implementation():
    check.section("§6 scaffolding is known by provenance, not by its name")
    with _tmp() as td:
        w = await World(td).start()
        rev = await w.svc.record_request(
            slug=SLUG, request_text="a test runner with a tests.py entry point")
        ids = await w.svc.set_criteria(slug=SLUG, revision=rev, criteria=[
            {"text": "the runner runs", "origin_quote": "a test runner"}])
        await w.svc.seal_contract(slug=SLUG, revision=rev)

        # These are the USER's files. They happen to be named like tests.
        w.write("test_engine.py", "def run():\n    return 'the engine'\n")
        w.write("tests.py", "from test_engine import run\nprint(run())\n")
        files = implementation_files(w.path)
        check(set(files) == {"test_engine.py", "tests.py"},
              f"the user's own files are the implementation ({files})")

        ctx = await w.svc.begin_check(slug=SLUG, criterion_id=ids[0])
        await w.svc.record_verdict(context=ctx, verdict=PASSED)
        check((await w.svc.evaluate(slug=SLUG)).state == COMPLETE, "COMPLETE")

        w.write("test_engine.py", "def run():\n    return 'changed'\n")
        v = await w.svc.evaluate(slug=SLUG)
        check(v.state != COMPLETE,
              f"editing one of them moves the fence ({v.state})")

        # Nova's OWN scaffolding, declared as such, does not.
        w.write("test_engine.py", "def run():\n    return 'the engine'\n")
        await w.svc.declare_scaffold(slug=SLUG, paths=["check_generated.py"])
        w.write("check_generated.py", "assert True\n")
        v = await w.svc.evaluate(slug=SLUG)
        check(v.state == COMPLETE,
              f"while a file Nova declared as scaffolding does not ({v.state})")


async def test_4a_an_origin_quote_must_be_a_real_span_of_the_request():
    check.section("§4 a criterion must point at words the user actually wrote")
    with _tmp() as td:
        w = await World(td).start()
        rev = await w.svc.record_request(slug=SLUG, request_text=REQUEST)
        bad = None
        try:
            await w.svc.set_criteria(slug=SLUG, revision=rev, criteria=[
                {"text": "supports matrix inversion",
                 "origin_quote": "invert matrices"}])
        except ValueError as e:
            bad = str(e)
        check(bad and "not a span of" in bad,
              f"a quote the user never wrote is refused ({str(bad)[:60]!r})")

        ok = await w.svc.set_criteria(slug=SLUG, revision=rev, criteria=[
            {"text": "adds two numbers", "origin_quote": "can add"}])
        check(len(ok) == 1, "a real span is accepted")


async def test_4b_an_incomplete_decomposition_cannot_complete():
    check.section("§4 dropping a requested capability blocks completion")
    with _tmp() as td:
        w = await World(td).start()
        rev = await w.svc.record_request(slug=SLUG, request_text=REQUEST)
        # Only ADD is decomposed. Subtraction was requested and forgotten.
        ids = await w.svc.set_criteria(slug=SLUG, revision=rev, criteria=[
            {"text": "adds two numbers", "origin_quote": "add"}])
        refused = None
        try:
            await w.svc.seal_contract(slug=SLUG, revision=rev)
        except ValueError as e:
            refused = str(e)
        check(refused and "subtract" in refused,
              f"sealing names the requested capability nothing covers "
              f"({str(refused)[:80]!r})")

        w.write("main.py", "def add(a, b):\n    return a + b\n")
        ctx = await w.svc.begin_check(slug=SLUG, criterion_id=ids[0])
        await w.svc.record_verdict(context=ctx, verdict=PASSED)
        v = await w.svc.evaluate(slug=SLUG)
        check(v.state != COMPLETE,
              f"and an unsealed contract cannot reach COMPLETE however much of "
              f"it passes ({v.state})")

        # Covering the missing capability makes it sealable.
        await w.svc.set_criteria(slug=SLUG, revision=rev, criteria=[
            {"text": "subtracts two numbers", "origin_quote": "subtract"}])
        await w.svc.seal_contract(slug=SLUG, revision=rev)
        v = await w.svc.evaluate(slug=SLUG)
        check(v.state != COMPLETE and len(v.outstanding) == 1,
              f"and then the forgotten capability is what remains ({v.state}, "
              f"{[s.criterion.text for s in v.outstanding]})")


async def test_5_only_a_person_can_accept_on_a_persons_behalf():
    check.section("§5 machine code cannot waive a human criterion")
    with _tmp() as td:
        w = await World(td).start()
        rev = await w.svc.record_request(slug=SLUG, request_text="a nice layout")
        ids = await w.svc.set_criteria(slug=SLUG, revision=rev, criteria=[
            {"text": "the layout looks right", "origin_quote": "a nice layout",
             "verify_kind": "human"}])
        await w.svc.seal_contract(slug=SLUG, revision=rev)
        w.write("main.py", "print('ui')\n")

        ctx = await w.svc.begin_check(slug=SLUG, criterion_id=ids[0])
        refused = None
        try:
            await w.svc.record_verdict(context=ctx, verdict=WAIVED,
                                       detail="looks fine to me, says the model")
        except ValueError as e:
            refused = str(e)
        check(refused and "human" in refused.lower(),
              f"the machine path refuses to waive ({str(refused)[:60]!r})")
        check((await w.svc.evaluate(slug=SLUG)).state != COMPLETE,
              "so it cannot complete itself")

        await w.svc.record_human_decision(slug=SLUG, criterion_id=ids[0],
                                          accepted=True, actor="marcus",
                                          detail="looks right")
        v = await w.svc.evaluate(slug=SLUG)
        check(v.state == COMPLETE,
              f"an explicit person completes it ({v.state})")
        rows = await w.mem.list_acceptance_evidence(project_name=SLUG)
        accepted = [r for r in rows if r["verdict"] == WAIVED]
        check(accepted and "marcus" in (accepted[-1]["detail"] or ""),
              "and the acceptance records who gave it")


async def test_6_a_half_built_contract_never_goes_live():
    check.section("§4 a contract is atomic, or it is not a contract")
    with _tmp() as td:
        w = await World(td).start()
        rev1, _ = await w.contract()
        w.write("main.py", "def add(a, b):\n    return a + b\n\n"
                           "def subtract(a, b):\n    return a - b\n")

        rev2 = await w.svc.record_request(
            slug=SLUG, request_text=REQUEST + " and multiply and divide")

        # The contract for R2 is built, and the process dies partway through.
        boom = None
        try:
            await w.svc.set_criteria(slug=SLUG, revision=rev2, criteria=[
                {"text": "multiplies", "origin_quote": "multiply"},
                {"text": "divides", "origin_quote": "divide"},
                {"text": "explodes", "origin_quote": "no such words here"},
            ])
        except ValueError as e:
            boom = str(e)
        check(boom, "the bad criterion aborts the batch")

        rows = await w.mem.list_acceptance_criteria(project_name=SLUG,
                                                    revision=rev2)
        check(rows == [],
              f"and NOTHING from that batch was written ({[r['text'] for r in rows]})")

        v = await w.svc.evaluate(slug=SLUG)
        check(v.state != COMPLETE,
              f"the half-built revision is not complete ({v.state})")
        check(not v.is_complete,
              "and the old revision's evidence does not carry it")


async def main() -> None:
    await test_1a_a_pass_earned_against_h1_does_not_certify_h2()
    await test_1b_a_pass_earned_under_r1_does_not_certify_r2()
    await test_2_an_empty_digest_is_not_a_wildcard()
    await test_3_a_users_own_test_file_is_implementation()
    await test_4a_an_origin_quote_must_be_a_real_span_of_the_request()
    await test_4b_an_incomplete_decomposition_cannot_complete()
    await test_5_only_a_person_can_accept_on_a_persons_behalf()
    await test_6_a_half_built_contract_never_goes_live()
    check.finish()


if __name__ == "__main__":
    run(main)
