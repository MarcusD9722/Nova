"""What completion costs, measured rather than assumed (§17).

Two questions, and the second is the one that matters:

  1. Is deriving a project's state fast enough to do on every chat turn?
  2. Does that cost GROW with the length of the project's history?

Question 2 exists because `evaluate()` calls `list_acceptance_evidence(
project_name=slug)` with no bound on it, and every check ever run appends a
row. Stage 13C found exactly this shape once already -- boot reading an audit
trail that only ever gets longer -- so it is measured here rather than assumed
to be fine. If the answer is linear, that is a finding to report with numbers,
not a thing to discover in a year.

Reported as P50/P90/max over many calls, because a mean hides the run that
made someone wait.

Run:  venv\\Scripts\\python.exe tests\\perf_completion_s14.py
"""

from __future__ import annotations

import asyncio
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")
os.environ.setdefault("NOVA_IT_WATCHDOG_S", "3600")

from harness import Checks, run  # noqa: E402

from core.completion import COMPLETE, PASSED  # noqa: E402
from core.completion_service import CompletionService  # noqa: E402
from memory.unifier import MemoryUnifier  # noqa: E402

check = Checks()

CALLS = 120
LADDER = [100, 500, 2000, 8000]


def _mb(path: Path) -> float:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1e6


async def _timed(fn, calls: int = CALLS) -> tuple[float, float, float]:
    times = []
    for _ in range(calls):
        t0 = time.perf_counter()
        await fn()
        times.append((time.perf_counter() - t0) * 1000)
    times.sort()
    return (statistics.median(times),
            times[min(len(times) - 1, int(len(times) * 0.9))],
            max(times))


async def test_evaluate_cost_against_history_length():
    check.section("§17 what it costs to say whether a project is done")
    rows: list[tuple[int, float, float, float, float]] = []

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        store = root / "memory_data"
        proj = root / "projects" / "perf"
        proj.mkdir(parents=True)
        mem = MemoryUnifier(store, enable_chroma=False)
        await mem.initialize()
        svc = CompletionService(memory=mem, projects_dir=root / "projects")

        request = ("a tool that adds numbers and subtracts numbers and "
                   "multiplies numbers and divides numbers")
        rev = await svc.record_request(slug="perf", request_text=request)
        ids = await svc.set_criteria(slug="perf", revision=rev, criteria=[
            {"text": c, "origin_quote": c, "verify_kind": "machine"}
            for c in ("adds numbers", "subtracts numbers", "multiplies numbers",
                      "divides numbers")])
        await svc.seal_contract(slug="perf", revision=rev)
        (proj / "main.py").write_text("# a tool\n", encoding="utf-8")

        written = 0
        for target in LADDER:
            # Fill the evidence table to the next rung. Every row is a real
            # recorded verdict through the public path.
            while written < target:
                for cid in ids:
                    ctx = await svc.begin_check(slug="perf", criterion_id=cid)
                    await svc.record_verdict(context=ctx, verdict=PASSED,
                                             detail="soak row")
                    written += 1
                    if written >= target:
                        break

            async def evaluate():
                await svc.evaluate(slug="perf")

            p50, p90, worst = await _timed(evaluate)
            size = _mb(store)
            rows.append((written, p50, p90, worst, size))
            print(f"    {written:>6} evidence rows   p50 {p50:7.2f} ms   "
                  f"p90 {p90:7.2f} ms   max {worst:7.2f} ms   "
                  f"store {size:5.1f} MB", flush=True)

        v = await svc.evaluate(slug="perf")
        check(v.state == COMPLETE,
              f"the project is still correctly complete after "
              f"{written} rows ({v.state})")

        first, last = rows[0], rows[-1]
        growth = last[2] / max(first[2], 0.001)
        factor = last[0] / first[0]
        print(f"\n    evidence grew {factor:.0f}x, p90 grew {growth:.1f}x")
        check(last[2] < 400,
              f"p90 stays usable at {last[0]} rows ({last[2]:.1f} ms)")
        check(growth < factor,
              f"p90 grows more slowly than the history does "
              f"({growth:.1f}x for {factor:.0f}x the rows)")

        per_row_kb = (last[4] - first[4]) * 1000 / max(last[0] - first[0], 1)
        check(per_row_kb < 4,
              f"each recorded check costs {per_row_kb:.2f} KB on disk")


async def test_many_projects():
    check.section("§17 a hundred projects in one store")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        store = root / "memory_data"
        (root / "projects").mkdir(parents=True)
        mem = MemoryUnifier(store, enable_chroma=False)
        await mem.initialize()
        svc = CompletionService(memory=mem, projects_dir=root / "projects")

        t0 = time.perf_counter()
        for i in range(100):
            slug = f"p{i:03d}"
            (root / "projects" / slug).mkdir(parents=True)
            (root / "projects" / slug / "main.py").write_text(
                f"# {slug}\n", encoding="utf-8")
            rev = await svc.record_request(slug=slug,
                                           request_text="a tool that adds numbers")
            cid = (await svc.set_criteria(slug=slug, revision=rev, criteria=[
                {"text": "adds numbers", "origin_quote": "adds numbers",
                 "verify_kind": "machine"}]))[0]
            await svc.seal_contract(slug=slug, revision=rev)
            ctx = await svc.begin_check(slug=slug, criterion_id=cid)
            await svc.record_verdict(context=ctx, verdict=PASSED)
        build = time.perf_counter() - t0

        async def evaluate_one():
            await svc.evaluate(slug="p050")

        p50, p90, worst = await _timed(evaluate_one)
        size = _mb(store)
        print(f"    built 100 projects in {build:.1f}s   "
              f"evaluate p50 {p50:.2f} ms  p90 {p90:.2f} ms  max {worst:.2f} ms   "
              f"store {size:.1f} MB")
        check(p90 < 100,
              f"one project's state is unaffected by the other 99 "
              f"({p90:.1f} ms p90)")
        check(size < 50, f"a hundred projects cost {size:.1f} MB")

        v = await svc.evaluate(slug="p050")
        check(v.state == COMPLETE, f"and the answer is still right ({v.state})")


async def main() -> None:
    await test_evaluate_cost_against_history_length()
    await test_many_projects()
    check.finish()


if __name__ == "__main__":
    run(main)
