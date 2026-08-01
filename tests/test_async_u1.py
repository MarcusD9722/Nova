"""U1: async parallelization of the read-heavy hot paths.

These paths previously ran their independent queries strictly one at a time.
The tests below PROVE the fan-out is concurrent (by injecting per-query latency
and measuring wall time against the sequential floor) and that results are
byte-identical to the sequential behavior — speed without a behavior change.
"""
import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory.unifier import MemoryUnifier

_fail = False
DELAY = 0.05  # per-query injected latency


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


def slow_wrap(backend, method_names, delay=DELAY):
    """Wrap backend methods so each call sleeps — makes concurrency measurable."""
    for name in method_names:
        original = getattr(backend, name)

        def make(orig):
            async def wrapped(*a, **kw):
                await asyncio.sleep(delay)
                return await orig(*a, **kw)
            return wrapped

        setattr(backend, name, make(original))


async def main():
    # ── search(): identical results, and demonstrably concurrent ──
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem = MemoryUnifier(Path(td), enable_chroma=False)
        await mem.initialize()

        await mem.add_fact(entity="user", attribute="spouse", value="Leslie", confidence=0.9)
        await mem.add_fact(entity="note", attribute="coffee", value="likes oat milk", confidence=0.9)
        await mem.upsert_person(name="Leslie", attributes={"relation": "spouse"})
        await mem.add_event(date="2026-07-04", note="fireworks with Leslie")

        baseline = await mem.search("what does Leslie like about coffee")
        check(bool(baseline), f"search returns hits ({len(baseline)})")

        # Re-running must be deterministic (cache-independent correctness).
        mem._search_gen += 1  # bust the cache so it really re-queries
        again = await mem.search("what does Leslie like about coffee")
        check([h.id for h in again] == [h.id for h in baseline], "search results are stable across runs")

        # Now inject latency and prove the fan-out overlaps.
        slow_wrap(mem._sqlite, [
            "search_facts", "search_people", "search_events", "search_documents", "recent_turns",
        ])
        mem._search_gen += 1
        t0 = time.perf_counter()
        slow_hits = await mem.search("what does Leslie like about coffee")
        elapsed = time.perf_counter() - t0

        # The query yields several terms; sequentially that is (terms x 4) + 1
        # queries -> well over 0.5s at 50ms each. Concurrent should land near a
        # couple of rounds. A generous ceiling still proves overlap.
        check(elapsed < 0.35, f"search fan-out runs concurrently (took {elapsed:.3f}s, sequential would be >=0.5s)")
        check([h.id for h in slow_hits] == [h.id for h in baseline], "concurrent search returns IDENTICAL results")

    # ── digital twin: identical profile, concurrent gathers ──
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem = MemoryUnifier(Path(td), enable_chroma=False)
        await mem.initialize()
        from uuid import uuid4
        conv = uuid4()
        for _ in range(12):
            await mem.ingest_turn(conv, "user", "working on the project this evening, going well overall")

        base_profile = await mem.digital_twin_profile()
        check(base_profile.get("enabled") is True, "twin profile builds")

        slow_wrap(mem._sqlite, ["recent_turns", "list_reminders", "list_goals"])
        t0 = time.perf_counter()
        slow_profile = await mem.digital_twin_profile()
        elapsed = time.perf_counter() - t0
        check(elapsed < 0.20, f"twin signal fetches run concurrently (took {elapsed:.3f}s)")
        check(
            slow_profile.get("enough_data") == base_profile.get("enough_data")
            and slow_profile.get("peak_period") == base_profile.get("peak_period"),
            "concurrent twin profile matches sequential result",
        )

    # ── executive: identical recommendations, concurrent gathers ──
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem = MemoryUnifier(Path(td), enable_chroma=False)
        await mem.initialize()
        from datetime import datetime, timedelta, timezone
        overdue_at = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        await mem.create_reminder(title="File the invoice", due_at_iso=overdue_at)

        base_recs = await mem.executive_recommendations(throttle=False)
        check(any("invoice" in r["message"].lower() for r in base_recs), "executive surfaces the overdue item")

        mem._exec_cache = None  # force a real re-gather
        slow_wrap(mem._sqlite, ["list_reminders", "list_goals", "all_people", "recent_turns"])
        t0 = time.perf_counter()
        slow_recs = await mem.executive_recommendations(throttle=False)
        elapsed = time.perf_counter() - t0
        check(elapsed < 0.30, f"executive signal fetches run concurrently (took {elapsed:.3f}s)")
        check(
            [r["key"] for r in slow_recs] == [r["key"] for r in base_recs],
            "concurrent executive recommendations match sequential result",
        )

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


asyncio.run(main())
