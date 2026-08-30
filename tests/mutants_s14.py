"""The Stage 14 mutation campaign, M119 onwards.

A mutant counts as KILLED only when it compiles, reaches the production path it
targets, and a NAMED assertion fails for the reason the mutant describes. A
mutant that survives is investigated before anything is changed: it is usually
either equivalent (some other guard enforces the same property) or a real gap
in the tests, and those need opposite responses.

Each entry names the cheapest suite that should catch it. Survival there
escalates to the full set before survival is believed, because "the fast suite
did not notice" and "nothing notices" are different claims.

Usage:
    venv\\Scripts\\python.exe tests\\mutants_s14.py            # all
    venv\\Scripts\\python.exe tests\\mutants_s14.py M119 M120  # some
"""

from __future__ import annotations

import io
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = REPO / "venv" / "Scripts" / "python.exe"

MODEL = "tests/test_completion_model_s14.py"
SERVICE = "tests/test_completion_service_s14.py"
FENCING = "tests/test_completion_fencing_s14.py"
TRUTH = "tests/test_completion_truth_s14.py"
EVENTS = "tests/test_completion_events_s14.py"
PROJECTIONS = "tests/test_completion_projections_s14.py"
CHAT = "tests/test_completion_chat_s14.py"
SEMANTICS = "tests/test_completion_semantics_s14.py"
ISOLATION = "tests/test_completion_isolation_s14.py"

#: Everything, for confirming a survival rather than assuming one.
ALL_SUITES = [MODEL, SERVICE, FENCING, TRUTH, EVENTS, PROJECTIONS, CHAT,
              SEMANTICS, ISOLATION]

COMPLETION = "core/completion.py"
SERVICE_PY = "core/completion_service.py"
ARTIFACTS = "core/completion_artifacts.py"
EVENTS_PY = "core/completion_events.py"
BUILDER = "core/project_builder.py"
CONTRACT = "core/completion_contract.py"

#: (id, description, file, old, new, suites)
MUTANTS: list[tuple] = [
    # ── the shapes Stage 14 exists to make impossible ──────────────────────
    ("M119", "any implementation file at all means COMPLETE", COMPLETION,
     '''    if not outstanding:
        if not contract_sealed:''',
     '''    if has_implementation:
        return out(COMPLETE, "there are files")
    if not outstanding:
        if not contract_sealed:''',
     [MODEL, TRUTH]),

    ("M120", "a criterion that FAILED is not counted as failing", COMPLETION,
     '''    failing = tuple(s for s in required if s.verdict == FAILED)''',
     '''    failing = ()''',
     [MODEL, TRUTH]),

    ("M121", "an unproven criterion is not counted as outstanding", COMPLETION,
     '''    outstanding = tuple(s for s in required if s.verdict not in SATISFYING
                        and s.verdict != FAILED)''',
     '''    outstanding = ()''',
     [MODEL, TRUTH]),

    ("M122", "human_pending counts as satisfied", COMPLETION,
     '''SATISFYING = frozenset({PASSED, WAIVED})''',
     '''SATISFYING = frozenset({PASSED, WAIVED, HUMAN_PENDING})''',
     [MODEL, CHAT]),

    ("M123", "an inconclusive check counts as satisfied", COMPLETION,
     '''SATISFYING = frozenset({PASSED, WAIVED})''',
     '''SATISFYING = frozenset({PASSED, WAIVED, INCONCLUSIVE})''',
     [MODEL, TRUTH]),

    ("M124", "an empty criteria set completes", COMPLETION,
     '''    if not crits:
        return out(IDEA, "a requirement exists but no acceptance criteria have "
                         "been agreed for it")''',
     '''    if not crits:
        return out(COMPLETE, "nothing was required")''',
     [MODEL, SERVICE]),

    ("M125", "no REQUIRED criteria still completes", COMPLETION,
     '''    if not required:''',
     '''    if False:''',
     [MODEL]),

    # ── the fences ─────────────────────────────────────────────────────────
    ("M126", "evidence from an earlier revision still counts", COMPLETION,
     '''        if int(ev.revision) != int(revision):''',
     '''        if False:''',
     [MODEL, FENCING]),

    ("M127", "evidence about a different artifact still counts", COMPLETION,
     '''        if ev.artifact_digest != artifact_digest:''',
     '''        if False:''',
     [MODEL, FENCING]),

    ("M128", "an empty digest matches any artifact", COMPLETION,
     '''        if ev.artifact_digest != artifact_digest:''',
     '''        if ev.artifact_digest and artifact_digest and ev.artifact_digest != artifact_digest:''',
     [FENCING, MODEL]),

    ("M129", "a verdict is stamped when recorded, not when checked", SERVICE_PY,
     '''            revision=context.revision, artifact_digest=context.artifact_digest,''',
     '''            revision=(await self._memory.current_requirement(
                project_name=context.slug))["revision"],
            artifact_digest=implementation_digest(
                self.project_path(context.slug)),''',
     [FENCING]),

    ("M130", "an unsealed contract can complete", COMPLETION,
     '''        if not contract_sealed:''',
     '''        if False:''',
     [SEMANTICS, MODEL]),

    ("M131", "a sealed contract can still be extended", SERVICE_PY,
     '''        if req.get("sealed_at"):''',
     '''        if False:''',
     [SEMANTICS, SERVICE]),

    ("M132", "sealing ignores uncovered clauses", SERVICE_PY,
     '''        if missing:''',
     '''        if False:''',
     [SEMANTICS, FENCING]),

    ("M133", "an origin quote need not be in the request", SERVICE_PY,
     '''            if not is_span_of(quote, req["request_text"]):''',
     '''            if False:''',
     [FENCING]),

    ("M134", "a waiver needs no human decision behind it", COMPLETION,
     '''    if verdict == WAIVED and not latest.decision_id:''',
     '''    if False:''',
     [FENCING]),

    ("M135", "a machine check can satisfy a human criterion", COMPLETION,
     '''    if criterion.verify_kind == "human" and verdict == PASSED:''',
     '''    if False:''',
     [MODEL, FENCING]),

    # ── events ─────────────────────────────────────────────────────────────
    ("M136", "project.completed fires for any state", EVENTS_PY,
     '''        if verdict.state == COMPLETE:''',
     '''        if True:''',
     [EVENTS]),

    ("M137", "the announcement ledger is ignored", EVENTS_PY,
     '''        previous = await self._claim(slug, verdict)
        if previous is None:
            return verdict.state''',
     '''        previous = await self._claim(slug, verdict)
        if previous is None:
            previous = ""''',
     [EVENTS]),

    ("M138", "the ledger is not scoped to the project", EVENTS_PY,
     '''        return await self._memory.claim_state_announcement(
            project_name=slug, revision=int(verdict.revision),
            state=verdict.state)''',
     '''        return await self._memory.claim_state_announcement(
            project_name="shared", revision=int(verdict.revision),
            state=verdict.state)''',
     [ISOLATION]),

    # ── projections ────────────────────────────────────────────────────────
    ("M139", "the status tool reads PROJECT.md again", BUILDER,
     '''        verdict = await self._completion.evaluate(slug=slug)
        status = verdict.state''',
     '''        verdict = await self._completion.evaluate(slug=slug)
        status = section("Status") or verdict.state''',
     [PROJECTIONS, ISOLATION]),

    ("M140", "PROJECT.md shows the passed-in status, not the verdict", BUILDER,
     '''        shown = getattr(verdict, "state", None) or status''',
     '''        shown = status''',
     [PROJECTIONS, TRUTH]),

    ("M141", "chat grounding omits the failing criterion's error", SERVICE_PY,
     '''            if st.verdict == "failed" and st.evidence:
                extra = f" — {str(st.evidence.error or st.evidence.detail)[:140]}"''',
     '''            if False:
                extra = ""''',
     [CHAT]),

    ("M142", "chat is never given the completion record", SERVICE_PY,
     '''        return ("\\nThe completion state of the work, from acceptance criteria "''',
     '''        return "" if True else ("\\nThe completion state of the work, from acceptance criteria "''',
     [CHAT]),

    # ── the builder ────────────────────────────────────────────────────────
    ("M143", "the builder assigns complete when the run check passes", BUILDER,
     '''            verdict = await self._completion.evaluate(slug=slug)
            build_log.extend(self._evidence_log(verdict))''',
     '''            verdict = await self._completion.evaluate(slug=slug)
            if run_note and run_note.startswith("Run check passed"):
                from dataclasses import replace as _replace
                verdict = _replace(verdict, state="complete")
            build_log.extend(self._evidence_log(verdict))''',
     [TRUTH]),

    ("M144", "skipped and reverted work is dropped from the log again", BUILDER,
     '''            if fail_reasons:''',
     '''            if False:''',
     [TRUTH]),

    ("M145", "criteria are derived from the finished code, not the request",
     "core/project_acceptance.py",
     '''        if not is_span_of(quote, request):''',
     '''        if False:''',
     [TRUTH]),

    # ── artifact identity ──────────────────────────────────────────────────
    ("M146", "declared scaffolding is not excluded from the fence", ARTIFACTS,
     '''        if rel in scaffold:
            continue''',
     '''        if False:
            continue''',
     [MODEL, FENCING]),

    ("M147", "PROJECT.md counts as implementation", ARTIFACTS,
     '''        if parts[-1] in _DERIVED_NAMES:
            continue''',
     '''        if False:
            continue''',
     [MODEL, PROJECTIONS]),

    ("M148", "a clause split on 'and' is not required to be covered", CONTRACT,
     '''        if any(p in clause or clause in p for p in pieces):
            continue
        missing.append(clause)''',
     '''        continue''',
     [SEMANTICS, FENCING]),
]


def run_suite(path: str, timeout: float = 2400) -> tuple[bool, str]:
    r = subprocess.run([str(PY), path], cwd=str(REPO), capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       timeout=timeout)
    out = (r.stdout or "") + (r.stderr or "")
    return ("RESULT: ALL PASS" in out), out


def classify(out: str) -> str | None:
    if "SyntaxError" in out or "IndentationError" in out:
        return "INVALID (did not compile)"
    if "NameError" in out or "ImportError" in out:
        return "INVALID (import/name error)"
    return None


def main() -> int:
    wanted = {a for a in sys.argv[1:] if a.startswith("M")}
    killed, survived, invalid = [], [], []
    started = time.time()

    for mid, desc, rel, old, new, suites in MUTANTS:
        if wanted and mid not in wanted:
            continue
        p = REPO / rel
        src = io.open(p, encoding="utf-8").read()
        if old not in src:
            print(f"{mid}: ANCHOR MISSING in {rel} -> WITHDRAWN")
            invalid.append((mid, "anchor missing"))
            continue
        io.open(p, "w", encoding="utf-8", newline="\n").write(
            src.replace(old, new, 1))
        try:
            verdict = None
            for suite in suites:
                ok, out = run_suite(suite)
                bad = classify(out)
                if bad:
                    verdict = ("INVALID", bad)
                    break
                if not ok:
                    fails = [l.strip() for l in out.splitlines()
                             if l.strip().startswith("FAIL")]
                    verdict = ("KILLED",
                               f"{Path(suite).stem}: "
                               f"{fails[0][:88] if fails else 'failed'}")
                    break
            if verdict is None:
                # Survived the targeted suites. Confirm against everything
                # before believing it — a fast suite not noticing is not the
                # same as nothing noticing.
                for suite in ALL_SUITES:
                    if suite in suites:
                        continue
                    ok, out = run_suite(suite)
                    if classify(out):
                        continue
                    if not ok:
                        fails = [l.strip() for l in out.splitlines()
                                 if l.strip().startswith("FAIL")]
                        verdict = ("KILLED (wider)",
                                   f"{Path(suite).stem}: "
                                   f"{fails[0][:80] if fails else 'failed'}")
                        break
            if verdict is None:
                verdict = ("SURVIVED", "no suite noticed — investigate")
        finally:
            io.open(p, "w", encoding="utf-8", newline="\n").write(src)

        state, detail = verdict
        print(f"{mid}: {state} — {desc}\n      {detail}", flush=True)
        if state.startswith("KILLED"):
            killed.append(mid)
        elif state == "SURVIVED":
            survived.append((mid, desc))
        else:
            invalid.append((mid, detail))

    print("\n" + "=" * 70)
    print(f"killed: {len(killed)}   survived: {len(survived)}   "
          f"invalid/withdrawn: {len(invalid)}   "
          f"({(time.time() - started) / 60:.1f} min)")
    for mid, desc in survived:
        print(f"  SURVIVED {mid}: {desc}")
    for mid, why in invalid:
        print(f"  WITHDRAWN {mid}: {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
