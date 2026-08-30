"""Run the Stage 14 suites repeatedly, and say what actually happened (§15).

Passing once shows a thing can work. Passing twenty times, each in a fresh
process against a fresh temporary directory, shows something weaker and more
useful: that it does not depend on the order the operating system felt like
scheduling things in, or on state left behind by the run before.

ONE RED RUN IN TWENTY IS A FINDING. That is the entire reason to soak. A repeat
that fails is kept, printed, and counted — never re-run until it agrees.

The generated model is soaked by SEEDS rather than repeats: running the same
500 sequences again only proves they are deterministic. New seeds are the only
thing that reaches a sequence nobody has run.

Usage:
    venv\\Scripts\\python.exe tests\\soak_s14.py            # the full soak
    venv\\Scripts\\python.exe tests\\soak_s14.py --quick    # 3 of each
    venv\\Scripts\\python.exe tests\\soak_s14.py restart    # matching suites
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = REPO / "venv" / "Scripts" / "python.exe"

#: (suite, repeats, what it is soaking)
PLAN: list[tuple[str, int, str]] = [
    ("test_completion_model_s14.py", 20, "the pure derivation and its fences"),
    ("test_completion_service_s14.py", 20, "contracts, checks, human decisions"),
    ("test_completion_fencing_s14.py", 20, "stale evidence of every kind"),
    ("test_completion_truth_s14.py", 20, "the three original reproductions"),
    ("test_completion_events_s14.py", 20, "what is announced, and once"),
    ("test_completion_projections_s14.py", 20, "PROJECT.md, facts, status"),
    ("test_completion_chat_s14.py", 20, "what chat is told about done-ness"),
    ("test_completion_semantics_s14.py", 15, "quoted but not proven"),
    ("test_completion_isolation_s14.py", 15, "two projects, no leakage"),
    ("test_completion_endpoint_matrix_s14.py", 15, "the fourteen rejections"),
    ("test_completion_journeys_s14.py", 10, "the six journeys"),
    ("test_completion_restart_s14.py", 10, "real process boundaries"),
]

#: Fresh seeds, not repeats.
SEED_BATCHES = [(0, 300), (100_000, 500), (250_000, 300), (750_000, 300)]


def run_once(suite: str, env: dict[str, str] | None = None) -> tuple[bool, str, float]:
    t0 = time.time()
    proc = subprocess.run(
        [str(PY), str(REPO / "tests" / suite)], cwd=str(REPO),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8",
             "NOVA_IT_WATCHDOG_S": "3600", **(env or {})},
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
        print(f"\n== {suite} x{repeats} — {what}", flush=True)
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
            ordered = sorted(times)
            p90 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))]
            print(f"   -> {len(times)} runs, median {ordered[len(ordered) // 2]:.1f}s, "
                  f"p90 {p90:.1f}s, slowest {max(times):.1f}s", flush=True)

    if not pattern or "generated" in pattern:
        for seed, count in (SEED_BATCHES[:1] if quick else SEED_BATCHES):
            print(f"\n== generated sequences: seed {seed} x{count}", flush=True)
            ok, out, secs = run_once(
                "test_completion_generated_s14.py",
                {"NOVA_S14_SEED": str(seed), "NOVA_S14_SEQS": str(count)})
            total_runs += 1
            tail = [l for l in out.splitlines() if "sequences held" in l]
            print(f"   {'ok ' if ok else 'FAIL'}  {secs:5.1f}s  "
                  f"{tail[0].strip() if tail else ''}", flush=True)
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
