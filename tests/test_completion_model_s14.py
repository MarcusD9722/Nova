"""The completion evaluator, on its own (Stage 14 §3/§5/§6).

This suite tests the pure derivation and the artifact set with no database, no
project builder and no model in the way. That isolation is the point: if the
seven states are ever wrong, this says so without a hundred other things also
going red, and it can enumerate cases an end-to-end test could never afford.

The end-to-end behaviour lives in test_completion_truth_s14.py. This is the
part that has to be right first.

Run:  venv\\Scripts\\python.exe tests\\test_completion_model_s14.py
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
    COMPLETE, FAILING, HUMAN_PENDING, IDEA, INCONCLUSIVE, PARTIALLY_IMPLEMENTED,
    PASSED, PASSING, PENDING, PLANNED, SCAFFOLDED, WAIVED,
    Criterion, Evidence, derive_state,
)
from core.completion_artifacts import (  # noqa: E402
    declare_scaffold, implementation_digest, implementation_files,
)

check = Checks()

DIGEST = "artifact-hash-1"
OTHER = "artifact-hash-2"


def crit(cid: str, *, required=True, kind="machine", rev=1, text=None) -> Criterion:
    return Criterion(criterion_id=cid, text=text or f"criterion {cid}",
                     origin_quote="the user asked for it", source="user",
                     required=required, verify_kind=kind, revision=rev)


def ev(cid: str, verdict: str, *, rev=1, digest=DIGEST, decision=None) -> Evidence:
    # A waiver carries the human decision it answers. Defaulted here so the
    # ordinary cases stay readable, but never defaulted in production: an
    # acceptance with no question behind it is refused by the derivation.
    if verdict == WAIVED and decision is None:
        decision = f"decision-for-{cid}"
    return Evidence(criterion_id=cid, verdict=verdict, revision=rev,
                    artifact_digest=digest, decision_id=decision)


def state(criteria, evidence, *, rev=1, digest=DIGEST, impl=True,
          requirement=True, legacy=""):
    return derive_state(has_requirement=requirement, criteria=criteria,
                        evidence=evidence, revision=rev, artifact_digest=digest,
                        has_implementation=impl, legacy_status=legacy)


async def test_a_the_seven_states_are_reachable_and_distinct():
    check.section("§3 each state, from the facts that produce it")
    cases = [
        ("no requirement at all", state([], [], requirement=False, impl=False), IDEA),
        ("a request with no agreed criteria", state([], [], impl=False), IDEA),
        ("criteria but nothing built", state([crit("a")], [], impl=False), PLANNED),
        ("files exist, nothing demonstrated",
         state([crit("a")], []), SCAFFOLDED),
        ("one of two demonstrated",
         state([crit("a"), crit("b")], [ev("a", PASSED)]), PARTIALLY_IMPLEMENTED),
        ("a required criterion refuted",
         state([crit("a"), crit("b")], [ev("a", PASSED), ev("b", "failed")]), FAILING),
        ("machine work done, a person still owes an answer",
         state([crit("a"), crit("b", kind="human")], [ev("a", PASSED)]), PASSING),
        ("everything required demonstrated",
         state([crit("a"), crit("b")], [ev("a", PASSED), ev("b", PASSED)]), COMPLETE),
    ]
    for name, verdict, expected in cases:
        check(verdict.state == expected,
              f"{name} -> {expected} ({verdict.state})")
    check(len({v.state for _, v, _ in cases}) == 7,
          f"all seven states are reachable "
          f"({sorted({v.state for _, v, _ in cases})})")


async def test_b_failing_outranks_everything():
    check.section("§3 a current failure is the headline, not a footnote")
    v = state([crit("a"), crit("b"), crit("c")],
              [ev("a", PASSED), ev("b", PASSED), ev("c", "failed")])
    check(v.state == FAILING,
          f"two passes and one failure is FAILING, not partial ({v.state})")
    check(v.failing and v.failing[0].criterion.criterion_id == "c",
          "and the failing criterion is named")
    check("failing" in v.summary(),
          f"the one-line summary leads with it ({v.summary()[:60]!r})")


async def test_c_silence_is_not_a_pass():
    check.section("§5 what was never checked is not passed")
    for verdict, label in ((PENDING, "nothing said"),
                           (INCONCLUSIVE, "a check that could not decide")):
        rows = [] if verdict == PENDING else [ev("b", INCONCLUSIVE)]
        v = state([crit("a"), crit("b")], [ev("a", PASSED)] + rows)
        check(v.state != COMPLETE,
              f"{label} does not complete the project ({v.state})")
    # The specific hole Stage 14 was called to close: "no tests were
    # applicable" reaching the same verdict as passing.
    v = state([crit("a")], [ev("a", INCONCLUSIVE)])
    check(v.state == SCAFFOLDED,
          f"an inconclusive check leaves it undemonstrated ({v.state})")


async def test_d_a_machine_cannot_satisfy_a_human_criterion():
    check.section("§5 machine evidence for a human judgement is refused")
    human = crit("look", kind="human", text="the layout looks right to me")
    v = state([human], [ev("look", PASSED)])
    check(v.state == PASSING and v.criteria[0].verdict == HUMAN_PENDING,
          f"a PASSED row against a human criterion does not satisfy it "
          f"({v.state}/{v.criteria[0].verdict})")
    check("only a person can judge" in v.criteria[0].stale_reason,
          f"and the reason says why ({v.criteria[0].stale_reason[:50]!r})")
    # An explicit human acceptance does satisfy it.
    v2 = state([human], [ev("look", WAIVED)])
    check(v2.state == COMPLETE,
          f"an explicit acceptance completes it ({v2.state})")
    # ...but only one that answers a question somebody was actually asked.
    v3 = state([human], [ev("look", WAIVED, decision="")])
    check(v3.state != COMPLETE,
          f"an acceptance with no decision behind it is not honoured ({v3.state})")
    check("answers no question" in v3.criteria[0].stale_reason,
          f"and the reason says why ({v3.criteria[0].stale_reason[:45]!r})")


async def test_e_evidence_is_fenced_to_its_revision_and_artifact():
    check.section("§6 evidence cannot speak for what it never saw")
    cs = [crit("a"), crit("b")]
    both = [ev("a", PASSED), ev("b", PASSED)]
    check(state(cs, both).state == COMPLETE, "baseline is COMPLETE")

    moved_on = state(cs, both, rev=2)
    check(moved_on.state != COMPLETE,
          f"a new requirement revision invalidates old evidence ({moved_on.state})")
    check("revision" in moved_on.criteria[0].stale_reason,
          f"and says so ({moved_on.criteria[0].stale_reason[:60]!r})")

    drifted = state(cs, both, digest=OTHER)
    check(drifted.state != COMPLETE,
          f"a changed implementation invalidates old evidence ({drifted.state})")
    check("implementation changed" in drifted.criteria[0].stale_reason,
          f"and says so ({drifted.criteria[0].stale_reason[:60]!r})")


async def test_f_the_latest_admissible_observation_wins():
    check.section("§6 repair and regression both take effect")
    c = [crit("a")]
    check(state(c, [ev("a", "failed"), ev("a", PASSED)]).state == COMPLETE,
          "a failure then a repair is COMPLETE")
    check(state(c, [ev("a", PASSED), ev("a", "failed")]).state == FAILING,
          "a pass then a regression is FAILING")
    # A stale row cannot overturn a current one, whichever order they arrive in.
    late_stale = state(c, [ev("a", PASSED), ev("a", "failed", rev=0)])
    check(late_stale.state == COMPLETE,
          f"a stale failure arriving last does not overturn a current pass "
          f"({late_stale.state})")
    late_pass = state(c, [ev("a", "failed"), ev("a", PASSED, digest=OTHER)])
    check(late_pass.state == FAILING,
          f"nor a stale pass a current failure ({late_pass.state})")


async def test_g_an_empty_required_set_cannot_certify_anything():
    check.section("§3 vacuous completion is refused")
    v = state([crit("a", required=False)], [ev("a", PASSED)])
    check(v.state != COMPLETE,
          f"a project with no REQUIRED criteria is not COMPLETE ({v.state})")
    check("no criterion is marked required" in " ".join(v.reasons),
          f"and the reason says why ({v.reasons})")
    # And the degenerate case that would make every all(...) true.
    check(state([], []).state == IDEA,
          "no criteria at all is IDEA, never COMPLETE")


async def test_h_legacy_status_is_history_not_evidence():
    check.section("§3 a pre-Stage-14 'complete' does not carry over")
    v = state([], [], legacy="complete", impl=True)
    check(v.state != COMPLETE,
          f"a legacy complete string does not produce COMPLETE ({v.state})")
    check(v.legacy_status == "complete" and v.legacy_note,
          "the old value is preserved as history")
    check("revalidation" in v.legacy_note,
          f"and says what it would take to earn it back "
          f"({v.legacy_note[-60:]!r})")
    # With criteria recorded but unproven, it is still not complete.
    v2 = state([crit("a")], [], legacy="complete")
    check(v2.state == SCAFFOLDED,
          f"and criteria without evidence stay undemonstrated ({v2.state})")


async def test_i_outstanding_work_is_enumerable():
    check.section("§10 'what's left?' has a structured answer")
    cs = [crit("a", text="adds two numbers"),
          crit("b", text="subtracts two numbers"),
          crit("c", text="shows a history", required=False)]
    v = state(cs, [ev("a", PASSED)])
    texts = [s.criterion.text for s in v.outstanding]
    check(texts == ["subtracts two numbers"],
          f"exactly the unproven REQUIRED criteria are listed ({texts})")
    check(all(s.criterion.required for s in v.outstanding),
          "optional criteria are not reported as blocking")


async def test_j_the_artifact_set_excludes_what_recording_a_verdict_writes():
    check.section("§6 recording a verdict must not invalidate it")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        proj = Path(td) / "p"
        (proj / "sub").mkdir(parents=True)
        (proj / "main.py").write_text("print('hi')\n", encoding="utf-8")
        (proj / "sub" / "engine.py").write_text("X = 1\n", encoding="utf-8")
        before = implementation_digest(proj)
        check(bool(before), "an implementation has a digest")
        check(implementation_files(proj) == ["main.py", "sub/engine.py"],
              f"and the set is the source files ({implementation_files(proj)})")

        # Everything below is written BY the act of checking or recording.
        # Nova DECLARES the files she writes; she does not get to assume that
        # anything named like a test is hers, because a user asking for a test
        # runner owns test_engine.py and tests.py.
        (proj / "PROJECT.md").write_text("## Status\ncomplete\n", encoding="utf-8")
        (proj / "test_main.py").write_text("assert True\n", encoding="utf-8")
        (proj / "nova_check.py").write_text("assert True\n", encoding="utf-8")
        declare_scaffold(proj, ["test_main.py", "nova_check.py"])
        (proj / ".nova" / "evidence.json").write_text("{}", encoding="utf-8")
        (proj / "__pycache__").mkdir()
        (proj / "__pycache__" / "main.cpython-311.pyc").write_bytes(b"\x00\x01")

        after = implementation_digest(proj)
        check(after == before,
              "writing the status, a DECLARED generated test, a declared repro "
              "check and the evidence file leaves the digest untouched")

        # ...and the same files, undeclared, ARE the implementation.
        (proj / "mine_test_helper.py").write_text("HELPER = 1\n", encoding="utf-8")
        check(implementation_digest(proj) != after,
              "while an undeclared file counts, whatever it is called")
        (proj / "mine_test_helper.py").unlink()

        # ...but a real change to the implementation does move it.
        (proj / "sub" / "engine.py").write_text("X = 2\n", encoding="utf-8")
        check(implementation_digest(proj) != before,
              "while editing an imported module does move it")
        # ...as does adding a new source file, or renaming one.
        digest_after_edit = implementation_digest(proj)
        (proj / "extra.py").write_text("Y = 3\n", encoding="utf-8")
        check(implementation_digest(proj) != digest_after_edit,
              "and so does adding a source file")


async def test_k_an_empty_project_has_no_digest():
    check.section("§6 nothing implemented is not a digest that matches nothing")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        proj = Path(td) / "p"
        proj.mkdir()
        (proj / "PROJECT.md").write_text("# p\n", encoding="utf-8")
        check(implementation_digest(proj) == "",
              "a project with only metadata has an empty digest")
        check(implementation_files(proj) == [],
              "and no implementation files")


async def main() -> None:
    await test_a_the_seven_states_are_reachable_and_distinct()
    await test_b_failing_outranks_everything()
    await test_c_silence_is_not_a_pass()
    await test_d_a_machine_cannot_satisfy_a_human_criterion()
    await test_e_evidence_is_fenced_to_its_revision_and_artifact()
    await test_f_the_latest_admissible_observation_wins()
    await test_g_an_empty_required_set_cannot_certify_anything()
    await test_h_legacy_status_is_history_not_evidence()
    await test_i_outstanding_work_is_enumerable()
    await test_j_the_artifact_set_excludes_what_recording_a_verdict_writes()
    await test_k_an_empty_project_has_no_digest()
    check.finish()


if __name__ == "__main__":
    run(main)
