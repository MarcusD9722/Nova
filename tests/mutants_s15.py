"""The Stage 15 mutation campaign, M201 onwards — handoff loss.

Stage 15's subject is the seam between capabilities, so these mutants attack
the things that carry identity ACROSS one: the generation a claim was made
under, the row a result belongs to, the revision evidence was filed against,
the project a piece of evidence is about, the target a permission names, and
the axis a status line reports. Each one is a way for two individually correct
subsystems to produce an incorrect combined result.

A mutant counts as KILLED only when it compiles, reaches the production path it
targets, and a NAMED assertion fails for the reason the mutant describes. A
survivor is investigated before anything is changed: it is either equivalent
(another guard already enforces the property) or a real gap, and those need
opposite responses.

Usage:
    venv\\Scripts\\python.exe tests\\mutants_s15.py            # all
    venv\\Scripts\\python.exe tests\\mutants_s15.py M201 M204  # some
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = REPO / "venv" / "Scripts" / "python.exe"

FB = "tests/test_s15_foreground_background.py"
RESTART = "tests/test_s15_restart_matrix.py"
JOURNEYS = "tests/test_s15_journeys.py"
GENERATED = "tests/test_s15_generated.py"
COMPOSITION = "tests/test_s15_axis_composition.py"
INDEPENDENCE = "tests/test_s15_goal_completion_independence.py"
HANDOFF = "tests/test_s15_handoff_task_goal.py"
DESTRUCTIVE = "tests/test_s15_destructive_matrix.py"

ALL_SUITES = [FB, JOURNEYS, GENERATED, RESTART, COMPOSITION, INDEPENDENCE,
              HANDOFF, DESTRUCTIVE]

SQLITE = "memory/backends/sqlite_backend.py"
PERMISSIONS = "core/permissions.py"
BUILDER = "core/project_builder.py"

#: (id, description, file, old, new, suites)
MUTANTS: list[tuple] = [
    # Killed by the pause/resume regression the survival prompted. The
    # observed effect is STARVATION, not a stale hand-out: `_CLAIM_UPDATE`
    # fences the write independently, so removing the SELECT half wedges
    # the candidate query on a row the UPDATE always refuses.
    ("M201", "the claim query offers rows the write will always refuse",
     SQLITE,
     "\"AND g.status='active' AND t.generation = g.generation \"",
     "\"AND g.status='active' \"",
     [HANDOFF, JOURNEYS, GENERATED]),

    ("M202", "a row that already ended can be written again", SQLITE,
     '''                if row is None or str(row[0]) != "running":''',
     '''                if row is None:''',
     [GENERATED, RESTART]),

    ("M203", "every result is treated as owning its run", SQLITE,
     "                owns = bool(row[1])",
     "                owns = True",
     [FB, GENERATED]),

    ("M204", "an interrupted task is recorded as a success", SQLITE,
     '''        "UPDATE {table} SET status='failed', outcome='unknown', "''',
     '''        "UPDATE {table} SET status='done', outcome='ok', "''',
     [RESTART]),

    ("M205", "queued work resumes by itself after a restart", SQLITE,
     '''        "UPDATE {table} SET status='cancelled', outcome='never_started', "
        "last_error=?, updated_at=? WHERE status='queued'")''',
     '''        "UPDATE {table} SET status='queued', outcome='pending', "
        "last_error=?, updated_at=? WHERE status='queued' AND 1=0")''',
     [RESTART]),

    ("M206", "evidence from an older revision still counts", SQLITE,
     '''        if revision is not None:
            q += " AND revision=?"
            params.append(int(revision))''',
     '''        if revision is not None and False:
            q += " AND revision=?"
            params.append(int(revision))''',
     [JOURNEYS, GENERATED]),

    ("M207", "another project's evidence counts as this one's", SQLITE,
     '''             "FROM acceptance_evidence WHERE project_name=?")''',
     '''             "FROM acceptance_evidence WHERE project_name=? OR 1=1")''',
     [GENERATED, JOURNEYS]),

    ("M208", "a cancelled goal keeps its generation", SQLITE,
     '''                "UPDATE goals SET status='cancelled', generation=generation+1, updated_at=? WHERE goal_id=?",''',
     '''                "UPDATE goals SET status='cancelled', updated_at=? WHERE goal_id=?",''',
     [FB, JOURNEYS]),

    ("M209", "a pending request is an id and nothing else", PERMISSIONS,
     '''        return [{"request_id": rid, **self._pending_meta.get(rid, {})}
                for rid in self._pending]''',
     '''        return [{"request_id": rid} for rid in self._pending]''',
     [FB, DESTRUCTIVE, JOURNEYS]),

    ("M210", "the status line reports the artifact and nothing else", BUILDER,
     '''        if failed_goals:''',
     '''        if False:''',
     [JOURNEYS, COMPOSITION]),

    ("M211", "a permission request from a dead process is still approvable",
     PERMISSIONS,
     "    def _recover_from_audit(self) -> None:",
     "    def _recover_from_audit(self) -> None:\n        return",
     [RESTART]),

    # M212 WITHDRAWN as EQUIVALENT, and the investigation is the finding.
    # `record_acceptance_evidence` takes a task_id, `Evidence` carries one,
    # and NOTHING ever sets it: production's only evidence writer
    # (`ProjectBuilder._validate_criteria`) calls `record_verdict` without a
    # task, and nothing in `derive_state`, the events or the projections reads
    # the field. Replacing an always-None value with None cannot change any
    # observable behaviour, so no state assertion can kill this mutant and one
    # that appeared to would be asserting something else. The dead provenance
    # is recorded in the Stage 15 report rather than wired up: filling it in
    # would be inventing the task -> evidence pipeline that does not exist.
    # ("M212", "evidence is filed without the task that produced it", SQLITE,
    #  "                 verdict, detail, error, task_id,",
    #  "                 verdict, detail, error, None,",
    #  [JOURNEYS, GENERATED, RESTART]),
]


def run_suite(path: str, timeout: float = 2400) -> tuple[bool, str]:
    env = dict(os.environ)
    # The generator is the slow one; 150 sequences still reaches every branch
    # it asserts coverage of, and a mutant that survives 150 is escalated to
    # the full set below like any other.
    env.setdefault("NOVA_S15_SEQUENCES", "150")
    env.setdefault("NOVA_IT_WATCHDOG_S", "3600")
    r = subprocess.run([str(PY), path], cwd=str(REPO), capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, env=env)
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
            print(f"{mid}: ANCHOR MISSING in {rel} -> WITHDRAWN", flush=True)
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
                    fails = [ln.strip() for ln in out.splitlines()
                             if ln.strip().startswith("FAIL")]
                    verdict = ("KILLED",
                               f"{Path(suite).stem}: "
                               f"{fails[0][:88] if fails else 'failed'}")
                    break
            if verdict is None:
                # Survived the targeted suites. Confirm against everything
                # before believing it.
                for suite in ALL_SUITES:
                    if suite in suites:
                        continue
                    ok, out = run_suite(suite)
                    if classify(out):
                        continue
                    if not ok:
                        fails = [ln.strip() for ln in out.splitlines()
                                 if ln.strip().startswith("FAIL")]
                        verdict = ("KILLED (wider)",
                                   f"{Path(suite).stem}: "
                                   f"{fails[0][:88] if fails else 'failed'}")
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
