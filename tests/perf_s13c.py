"""What Stage 13C cost at startup (§25).

Two of this stage's fixes run on every boot, so both deserve a number rather
than an assurance:

  * `PermissionBroker.__init__` now reads the tail of its own audit file. That
    file is append-only and grows for the life of the machine, so the honest
    question is not "is it fast on an empty file" but "what does it cost on a
    file with ten thousand lines in it".
  * `_apply_migrations` now replays history for an unstamped database that
    already existed. That path is rare - it fires once, for a database written
    before versioning - but when it fires a person is waiting for it.

Everything else measured here is the ordinary boot, so a regression anywhere
else in the stage would show up too.

Run on BOTH sides to get a before and after:

    venv\\Scripts\\python.exe tests\\perf_s13c.py

The numbers are P50 and P90 over N runs, not means: a mean hides the slow boot
that a person actually notices, and it is the slow one they complain about.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import statistics
import sys
import tempfile
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

RUNS = int(os.getenv("NOVA_PERF_RUNS", "9"))


def pcts(samples: list[float]) -> str:
    s = sorted(samples)
    p50 = s[len(s) // 2]
    p90 = s[min(len(s) - 1, int(len(s) * 0.9))]
    return f"P50 {p50 * 1000:7.1f}ms   P90 {p90 * 1000:7.1f}ms   (n={len(s)})"


def audit_file(path: Path, lines: int) -> None:
    """A plausible trail: mostly settled requests, a few still open."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for i in range(lines):
            rid = uuid.uuid4().hex[:12]
            fh.write(json.dumps({"ts": "2026-01-01T00:00:00+00:00",
                                 "capability": "computer.click",
                                 "tier": "standard", "details": {"i": i},
                                 "outcome": "pending",
                                 "request_id": rid}) + "\n")
            if i % 10 != 0:          # nine in ten got an answer
                fh.write(json.dumps({"ts": "2026-01-01T00:00:01+00:00",
                                     "outcome": "approved",
                                     "request_id": rid, "by": "user"}) + "\n")


def time_broker(lines: int) -> list[float]:
    from core.permissions import PermissionBroker
    out = []
    for _ in range(RUNS):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            p = Path(td) / "permission_audit.jsonl"
            if lines:
                audit_file(p, lines)
            t0 = time.perf_counter()
            PermissionBroker(mode="guarded", audit_path=p)
            out.append(time.perf_counter() - t0)
    return out


OLD_SCHEMA = """
CREATE TABLE goals (
    goal_id TEXT PRIMARY KEY, project_name TEXT NOT NULL, title TEXT NOT NULL,
    objective TEXT NOT NULL, success_criteria TEXT NOT NULL, status TEXT NOT NULL,
    priority INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY, goal_id TEXT, project_name TEXT NOT NULL,
    tool_name TEXT NOT NULL, args_json TEXT NOT NULL, status TEXT NOT NULL,
    attempts INTEGER NOT NULL, run_after TEXT NOT NULL, last_error TEXT NOT NULL,
    result_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
"""


async def time_initialize(*, old_db: bool, rows: int = 0) -> list[float]:
    from memory.unifier import MemoryUnifier
    out = []
    for _ in range(RUNS):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            mem_dir = Path(td) / "memory_data"
            db = mem_dir / "sqlite" / "nova.sqlite3"
            db.parent.mkdir(parents=True, exist_ok=True)
            if old_db:
                con = sqlite3.connect(db)
                con.executescript(OLD_SCHEMA)
                for i in range(rows):
                    con.execute("INSERT INTO goals VALUES (?,?,?,?,?,?,?,?,?)",
                                (str(uuid.uuid4()), "p", f"g{i}", "o", "c",
                                 "active", 50, "1970-01-01T00:00:00+00:00",
                                 "1970-01-01T00:00:00+00:00"))
                con.commit()
                con.close()
            t0 = time.perf_counter()
            m = MemoryUnifier(mem_dir, enable_chroma=False)
            await m.initialize()
            out.append(time.perf_counter() - t0)
    return out


async def time_second_open() -> list[float]:
    """The ordinary case: a database this machine has opened before."""
    from memory.unifier import MemoryUnifier
    out = []
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem_dir = Path(td) / "memory_data"
        m = MemoryUnifier(mem_dir, enable_chroma=False)
        await m.initialize()
        for _ in range(RUNS):
            t0 = time.perf_counter()
            m2 = MemoryUnifier(mem_dir, enable_chroma=False)
            await m2.initialize()
            out.append(time.perf_counter() - t0)
    return out


async def time_full_boot() -> list[float]:
    from harness import boot
    out = []
    for _ in range(max(3, RUNS // 3)):
        t0 = time.perf_counter()
        async with boot(default_reply="Sure."):
            out.append(time.perf_counter() - t0)
    return out


async def main() -> None:
    print(f"Stage 13C startup cost — {RUNS} runs each, "
          f"git {os.popen('git rev-parse --short HEAD').read().strip()}")
    print("=" * 74)

    print("\nPermissionBroker construction (reads its own audit tail)")
    for lines in (0, 100, 2_000, 10_000):
        label = f"  audit {lines:>6,} requests"
        print(f"{label:<32} {pcts(time_broker(lines))}")

    print("\nMemoryUnifier.initialize()")
    print(f"{'  brand-new database':<32} {pcts(await time_initialize(old_db=False))}")
    print(f"{'  re-opening a known one':<32} {pcts(await time_second_open())}")
    for rows in (0, 500):
        print(f"{'  pre-versioning db, ' + str(rows) + ' goals':<32} "
              f"{pcts(await time_initialize(old_db=True, rows=rows))}")

    print("\nFull backend boot (harness)")
    print(f"{'  boot()':<32} {pcts(await time_full_boot())}")
    print()


if __name__ == "__main__":
    asyncio.run(main())
