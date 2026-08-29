"""The completion service against a real database (Stage 14 §4/§6/§7).

The pure derivation is proved in test_completion_model_s14.py. This proves the
durable half: that criteria and evidence survive as rows, that the fences work
when the digest comes from real files rather than a constant, and that the
three constraints this design was given actually hold.

  1. criteria trace to the durable request, and are recorded before the code
  2. recording a verdict does not invalidate that verdict
  3. a pre-Stage-14 "complete" string is history, never authority

Run:  venv\\Scripts\\python.exe tests\\test_completion_service_s14.py
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

from core.completion import (  # noqa: E402
    COMPLETE, FAILED, FAILING, HUMAN_PENDING, PARTIALLY_IMPLEMENTED, PASSED,
    PASSING, SCAFFOLDED, WAIVED,
)
from core.completion_service import CompletionService  # noqa: E402
from memory.unifier import MemoryUnifier  # noqa: E402

check = Checks()

SLUG = "calculator"
REQUEST = "a calculator that can add and subtract two numbers"


class World:
    """A temp root with a real database and a real project directory."""

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

    async def seed(self):
        """The ordinary opening: request first, criteria second, code third."""
        rev = await self.svc.record_request(slug=SLUG, request_text=REQUEST)
        ids = await self.svc.set_criteria(slug=SLUG, revision=rev, criteria=[
            {"text": "adds two numbers", "origin_quote": "can add"},
            {"text": "subtracts two numbers", "origin_quote": "and subtract"},
        ])
        return rev, ids


def _tmp():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


async def test_a_criteria_are_recorded_before_any_code_exists():
    check.section("§4 the contract exists before the implementation")
    with _tmp() as td:
        w = await World(td).start()
        rev, ids = await w.seed()
        check(rev == 1 and len(ids) == 2, f"a request and two criteria ({rev}, {len(ids)})")

        v = await w.svc.evaluate(slug=SLUG)
        check(v.state == "planned",
              f"with criteria and no code the state is PLANNED ({v.state})")

        rows = await w.mem.list_acceptance_criteria(project_name=SLUG)
        check(all(r["origin_quote"] for r in rows),
              "every criterion traces to a span of the request")
        check({r["origin_quote"] for r in rows} == {"can add", "and subtract"},
              f"and to the right spans ({sorted(r['origin_quote'] for r in rows)})")

        req = await w.mem.current_requirement(project_name=SLUG)
        check(req["request_text"] == REQUEST,
              "the user's own words are stored verbatim, not a paraphrase")


async def test_b_the_stage14_calculator_cannot_be_complete():
    check.section("§7 the S14-2 reproduction, under the new model")
    with _tmp() as td:
        w = await World(td).start()
        rev, (add_id, sub_id) = await w.seed()
        # Exactly what the old builder produced: adds, does not subtract,
        # starts cleanly.
        w.write("main.py", "def add(a, b):\n    return a + b\n")

        v = await w.svc.evaluate(slug=SLUG)
        check(v.state == SCAFFOLDED,
              f"files alone do not demonstrate anything ({v.state})")

        await w.svc.record_verdict(slug=SLUG, criterion_id=add_id,
                                   verdict=PASSED, detail="add(2,3) == 5")
        v = await w.svc.evaluate(slug=SLUG)
        check(v.state == PARTIALLY_IMPLEMENTED,
              f"proving addition does not complete the project ({v.state})")
        check([s.criterion.text for s in v.outstanding] == ["subtracts two numbers"],
              f"and subtraction is named as what remains "
              f"({[s.criterion.text for s in v.outstanding]})")

        # The old rule's inputs, recorded honestly, still cannot complete it.
        await w.svc.record_verdict(slug=SLUG, criterion_id=sub_id,
                                   verdict="inconclusive",
                                   detail="no automated logic tests were applicable")
        v = await w.svc.evaluate(slug=SLUG)
        check(v.state != COMPLETE,
              f"'no tests were applicable' is not a pass ({v.state})")

        # Only actually demonstrating it completes it.
        w.write("main.py", "def add(a, b):\n    return a + b\n\n\n"
                           "def subtract(a, b):\n    return a - b\n")
        await w.svc.record_verdict(slug=SLUG, criterion_id=add_id, verdict=PASSED)
        await w.svc.record_verdict(slug=SLUG, criterion_id=sub_id,
                                   verdict=PASSED, detail="subtract(5,3) == 2")
        v = await w.svc.evaluate(slug=SLUG)
        check(v.state == COMPLETE,
              f"both criteria demonstrated on the current code is COMPLETE ({v.state})")


async def test_c_recording_a_verdict_does_not_invalidate_it():
    check.section("§6 constraint 2: the fence must not eat its own evidence")
    with _tmp() as td:
        w = await World(td).start()
        rev, (add_id, sub_id) = await w.seed()
        w.write("main.py", "def add(a, b):\n    return a + b\n\n"
                           "def subtract(a, b):\n    return a - b\n")
        await w.svc.record_verdict(slug=SLUG, criterion_id=add_id, verdict=PASSED)
        await w.svc.record_verdict(slug=SLUG, criterion_id=sub_id, verdict=PASSED)
        check((await w.svc.evaluate(slug=SLUG)).state == COMPLETE, "COMPLETE")

        # Everything Nova writes while RECORDING that verdict.
        w.write("PROJECT.md", "## Status\ncomplete\n## Progress log\n- done\n")
        w.write("test_main.py", "from main import add\nassert add(1, 1) == 2\n")
        w.write("nova_check.py", "assert True\n")
        (w.path / ".nova").mkdir(exist_ok=True)
        (w.path / ".nova" / "evidence.json").write_text("{}", encoding="utf-8")

        v = await w.svc.evaluate(slug=SLUG)
        check(v.state == COMPLETE,
              f"writing the status, the generated test, the repro check and the "
              f"evidence file leaves it COMPLETE ({v.state})")

        # A real change to the implementation still invalidates it.
        w.write("main.py", "def add(a, b):\n    return a + b\n")
        v = await w.svc.evaluate(slug=SLUG)
        check(v.state != COMPLETE,
              f"but deleting subtract does invalidate it ({v.state})")
        check(any("implementation changed" in s.stale_reason for s in v.criteria),
              "and the reason names the drift")


async def test_d_a_correction_invalidates_the_old_contract():
    check.section("§6 constraint 1: a new revision needs new evidence")
    with _tmp() as td:
        w = await World(td).start()
        rev1, (add_id, sub_id) = await w.seed()
        w.write("main.py", "def add(a, b):\n    return a + b\n\n"
                           "def subtract(a, b):\n    return a - b\n")
        await w.svc.record_verdict(slug=SLUG, criterion_id=add_id, verdict=PASSED)
        await w.svc.record_verdict(slug=SLUG, criterion_id=sub_id, verdict=PASSED)
        check((await w.svc.evaluate(slug=SLUG)).state == COMPLETE, "COMPLETE at R1")

        # The user adds a requirement.
        rev2 = await w.svc.record_request(
            slug=SLUG, request_text=REQUEST + ", and multiply")
        carried = await w.svc.carry_forward(slug=SLUG, from_revision=rev1,
                                            to_revision=rev2)
        mul = await w.svc.set_criteria(slug=SLUG, revision=rev2, criteria=[
            {"text": "multiplies two numbers", "origin_quote": "and multiply"}])
        check(rev2 == 2 and len(carried) == 2 and len(mul) == 1,
              f"R2 carries the two old criteria and adds one ({rev2}, "
              f"{len(carried)}, {len(mul)})")

        v = await w.svc.evaluate(slug=SLUG)
        check(v.state != COMPLETE,
              f"adding a requirement takes it out of COMPLETE ({v.state})")
        check(len(v.outstanding) == 3,
              f"and ALL THREE need current evidence again, not just the new one "
              f"({len(v.outstanding)})")
        check(any("revision" in s.stale_reason for s in v.criteria),
              "the old evidence is named as belonging to an earlier revision")

        # A stale R1 pass arriving late cannot satisfy R2.
        await w.mem.record_acceptance_evidence(
            criterion_id=add_id, project_name=SLUG, revision=rev1,
            artifact_digest="whatever", verdict=PASSED, detail="late R1 result")
        v = await w.svc.evaluate(slug=SLUG)
        check(v.state != COMPLETE and len(v.outstanding) == 3,
              f"a late R1 pass does not move R2 ({v.state}, {len(v.outstanding)})")


async def test_e_a_criterion_cannot_vanish_by_being_forgotten():
    check.section("§4 dropping a requirement takes an explicit act")
    with _tmp() as td:
        w = await World(td).start()
        rev1, (add_id, sub_id) = await w.seed()
        rev2 = await w.svc.record_request(slug=SLUG,
                                          request_text="just addition now")
        # Carrying forward while explicitly retiring subtraction.
        await w.svc.carry_forward(slug=SLUG, from_revision=rev1, to_revision=rev2,
                                  drop_criterion_ids=[sub_id],
                                  drop_reason="the user removed subtraction")
        rows = await w.mem.list_acceptance_criteria(project_name=SLUG, revision=rev2)
        check([r["text"] for r in rows] == ["adds two numbers"],
              f"only the surviving criterion is on R2 ({[r['text'] for r in rows]})")

        gone = await w.mem.list_acceptance_criteria(
            project_name=SLUG, include_superseded=True)
        retired = [r for r in gone if r["criterion_id"] == sub_id]
        check(retired and retired[0]["superseded_by_revision"] == rev2,
              "the retired criterion records which revision retired it")
        check(retired and "removed subtraction" in retired[0]["supersede_reason"],
              f"and why ({retired[0]['supersede_reason'] if retired else ''!r})")

        w.write("main.py", "def add(a, b):\n    return a + b\n")
        await w.svc.record_verdict(
            slug=SLUG, criterion_id=rows[0]["criterion_id"], verdict=PASSED)
        v = await w.svc.evaluate(slug=SLUG)
        check(v.state == COMPLETE,
              f"and the narrowed contract can be completed ({v.state})")


async def test_f_legacy_complete_is_not_authority():
    check.section("§3 constraint 3: pre-Stage-14 'complete' needs revalidation")
    with _tmp() as td:
        w = await World(td).start()
        # A project as it exists today: files, a PROJECT.md saying complete,
        # and no Stage 14 rows at all.
        w.write("main.py", "print('an old project')\n")
        w.write("PROJECT.md", "# calculator\n## Status\ncomplete\n")

        v = await w.svc.evaluate(slug=SLUG, legacy_status="complete")
        check(v.state != COMPLETE,
              f"the old string does not make it COMPLETE ({v.state})")
        check(v.legacy_status == "complete" and "history only" in v.legacy_note,
              "it is preserved as history")
        check("revalidation" in v.legacy_note,
              "and the note says revalidation is what would earn it back")

        # Revalidation is exactly the ordinary path: record what was asked,
        # record what would prove it, then prove it.
        rev = await w.svc.record_request(slug=SLUG, request_text=REQUEST)
        ids = await w.svc.set_criteria(slug=SLUG, revision=rev, criteria=[
            {"text": "adds two numbers", "origin_quote": "can add"}])
        v = await w.svc.evaluate(slug=SLUG, legacy_status="complete")
        check(v.state == SCAFFOLDED,
              f"recording the contract alone does not restore it ({v.state})")
        await w.svc.record_verdict(slug=SLUG, criterion_id=ids[0], verdict=PASSED)
        v = await w.svc.evaluate(slug=SLUG, legacy_status="complete")
        check(v.state == COMPLETE,
              f"only current evidence does ({v.state})")


async def test_g_evidence_is_stamped_by_the_service_not_the_caller():
    check.section("§5 a caller cannot attribute evidence to what it did not check")
    with _tmp() as td:
        w = await World(td).start()
        rev, (add_id, _) = await w.seed()
        w.write("main.py", "def add(a, b):\n    return a + b\n")
        await w.svc.record_verdict(slug=SLUG, criterion_id=add_id, verdict=PASSED)
        rows = await w.mem.list_acceptance_evidence(project_name=SLUG)
        check(len(rows) == 1, f"one row recorded ({len(rows)})")
        check(rows[0]["revision"] == rev,
              "stamped with the revision the service read, not one passed in")
        check(len(rows[0]["artifact_digest"]) == 64,
              f"and with the digest of what was actually on disk "
              f"({rows[0]['artifact_digest'][:12]}...)")

        bad = None
        try:
            await w.svc.record_verdict(slug=SLUG, criterion_id=add_id,
                                       verdict="looks about right")
        except ValueError as e:
            bad = str(e)
        check(bad and "not a recordable verdict" in bad,
              f"an invented verdict is refused ({str(bad)[:50]!r})")

        missing = None
        try:
            await w.svc.set_criteria(slug=SLUG, revision=rev, criteria=[
                {"text": "it should feel nice"}])
        except ValueError as e:
            missing = str(e)
        check(missing and "cannot point at the request" in missing,
              f"and a criterion with no origin quote is refused "
              f"({str(missing)[:50]!r})")


async def test_h_human_criteria_survive_the_round_trip():
    check.section("§5 a human judgement stays a human judgement in storage")
    with _tmp() as td:
        w = await World(td).start()
        rev = await w.svc.record_request(slug=SLUG, request_text="a nice layout")
        ids = await w.svc.set_criteria(slug=SLUG, revision=rev, criteria=[
            {"text": "the layout looks right to Marcus",
             "origin_quote": "a nice layout", "verify_kind": "human"}])
        w.write("main.py", "print('ui')\n")

        await w.svc.record_verdict(slug=SLUG, criterion_id=ids[0], verdict=PASSED,
                                   detail="a model thought it looked fine")
        v = await w.svc.evaluate(slug=SLUG)
        check(v.state == PASSING and v.criteria[0].verdict == HUMAN_PENDING,
              f"a machine pass leaves it awaiting a person ({v.state})")

        await w.svc.record_verdict(slug=SLUG, criterion_id=ids[0], verdict=WAIVED,
                                   detail="Marcus said it looks right")
        v = await w.svc.evaluate(slug=SLUG)
        check(v.state == COMPLETE,
              f"an explicit human acceptance completes it ({v.state})")


async def main() -> None:
    await test_a_criteria_are_recorded_before_any_code_exists()
    await test_b_the_stage14_calculator_cannot_be_complete()
    await test_c_recording_a_verdict_does_not_invalidate_it()
    await test_d_a_correction_invalidates_the_old_contract()
    await test_e_a_criterion_cannot_vanish_by_being_forgotten()
    await test_f_legacy_complete_is_not_authority()
    await test_g_evidence_is_stamped_by_the_service_not_the_caller()
    await test_h_human_criteria_survive_the_round_trip()
    check.finish()


if __name__ == "__main__":
    run(main)
