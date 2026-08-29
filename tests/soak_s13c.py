"""Run the Stage 13C suites over and over, and say what actually happened (§21).

A suite that passes once has shown that a thing can work. A suite that passes
twenty times in a row has shown something weaker but more useful: that it does
not depend on which order the operating system felt like scheduling things in.

Every repeat is a fresh process with a fresh temporary directory, so a run that
only passes because of state left by the run before it fails here.

WHAT A FAILURE MEANS. The whole point is to catch the run that fails once in
twenty, so a single red repeat is a finding, not noise to be re-run away. The
failing repeat's output is kept and printed.

Usage:
    venv\\Scripts\\python.exe tests\\soak_s13c.py              # the full soak
    venv\\Scripts\\python.exe tests\\soak_s13c.py --quick      # 3 of each
    venv\\Scripts\\python.exe tests\\soak_s13c.py restart      # matching suites
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = REPO / "venv" / "Scripts" / "python.exe"

#: (suite, repeats, what it is soaking). The counts come from §21.
PLAN: list[tuple[str, int, str]] = [
    ("test_restart_states_s13c.py", 20, "every goal/task state across a restart"),
    ("test_crash_windows_s13c.py", 20, "the twelve crash windows"),
    ("test_replay_safety_s13c.py", 20, "replay, pause/cancel, repeated restarts"),
    ("test_revision_isolation_s13c.py", 20, "corrections and two-project isolation"),
    ("test_permission_durability_s13c.py", 20, "permission requests across a restart"),
    ("test_migration_s13c.py", 10, "old databases"),
    ("test_reconstruction_s13c.py", 10, "answering from the record"),
    ("test_foreground_background_s13c.py", 10, "chat against recovery"),
    ("test_journeys_s13c.py", 10, "the six journeys"),
]

#: The generated model is soaked by SEEDS, not by repeats: running the same 300
#: sequences again proves only that they are deterministic. Fresh seeds are the
#: only thing that finds a sequence nobody has run yet.
SEED_BATCHES = [(0, 300), (100_000, 500), (250_000, 300), (500_000, 300)]


def run_once(suite: str, env: dict[str, str] | None = None) -> tuple[bool, str, float]:
    t0 = time.time()
    proc = subprocess.run(
        [str(PY), str(REPO / "tests" / suite)], cwd=str(REPO),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8", **(env or {})},
        timeout=7200)
    out = (proc.stdout or "") + (proc.stderr or "")
    ok = proc.returncode == 0 and "RESULT: ALL PASS" in out
    return ok, out, time.time() - t0


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    quick = "--quick" in sys.argv
    pattern = args[0] if args else ""

    plan = [(s, 3 if quick else n, w) for s, n, w in PLAN
            if not pattern or pattern in s]
    failures: list[tuple[str, int, str]] = []
    total_runs = 0
    started = time.time()

    for suite, repeats, what in plan:
        print(f"\n== {suite} x{repeats} — {what}")
        times = []
        for i in range(1, repeats + 1):
            ok, out, secs = run_once(suite)
            times.append(secs)
            total_runs += 1
            print(f"   {i:>3}/{repeats}  {'ok ' if ok else 'FAIL'}  {secs:5.1f}s",
                  flush=True)
            if not ok:
                failures.append((suite, i, out[-3000:]))
        if times:
            print(f"   -> {len(times)} runs, "
                  f"median {sorted(times)[len(times) // 2]:.1f}s, "
                  f"slowest {max(times):.1f}s")

    if not pattern or "generated" in pattern:
        for seed, count in (SEED_BATCHES[:1] if quick else SEED_BATCHES):
            print(f"\n== generated sequences: seed {seed} x{count}")
            ok, out, secs = run_once(
                "test_generated_restarts_s13c.py",
                {"NOVA_S13C_SEED": str(seed), "NOVA_S13C_SEQS": str(count)})
            total_runs += 1
            tail = [l for l in out.splitlines() if "sequences held" in l]
            print(f"   {'ok ' if ok else 'FAIL'}  {secs:5.1f}s  "
                  f"{tail[0].strip() if tail else ''}")
            if not ok:
                failures.append((f"generated seed={seed}", 1, out[-3000:]))

    mins = (time.time() - started) / 60
    print(f"\n{'=' * 66}")
    print(f"{total_runs} runs in {mins:.1f} minutes")
    if failures:
        print(f"FAILURES: {len(failures)}")
        for suite, i, out in failures[:3]:
            print(f"\n--- {suite} repeat {i} ---")
            print(out)
        return 1
    print("no failures")
    return 0


if __name__ == "__main__":
    sys.exit(main())
