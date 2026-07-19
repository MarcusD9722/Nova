import asyncio
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from memory.unifier import MemoryUnifier

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


async def main():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem = MemoryUnifier(Path(td), enable_chroma=False)
        await mem.initialize()

        # First ever call -> no prior last_active -> gap is None
        gap0 = await mem.check_and_mark_session_gap()
        check(gap0 is None, "first-ever call -> gap is None")

        # Immediately calling again -> tiny gap
        gap1 = await mem.check_and_mark_session_gap()
        check(gap1 is not None and gap1.total_seconds() < 5, f"second call right after -> tiny gap (got {gap1})")

        # Simulate a big gap by backdating last_active directly
        old = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
        await mem.add_fact(entity="session", attribute="last_active", value=old, confidence=1.0)
        gap2 = await mem.check_and_mark_session_gap()
        check(gap2 is not None and gap2.total_seconds() >= 9 * 3600, f"backdated 10h -> gap >= 9h (got {gap2})")

        # build_catchup_summary with nothing changed -> empty
        since = datetime.now(timezone.utc).isoformat()
        empty_summary = await mem.build_catchup_summary(since)
        check(empty_summary == "", f"nothing changed since 'now' -> empty summary (got {empty_summary!r})")

        # build_catchup_summary with an indexed doc + fired reminder -> non-empty
        since_past = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        await mem.index_document(path=str(Path(td) / "test.txt"), excerpt="hello world", mtime=0.0)
        rid = await mem.create_reminder(title="pack for trip", details="pack for trip", due_at_iso=datetime.now().isoformat(), recurrence="none")
        await mem.complete_reminder(reminder_id=str(rid))
        summary = await mem.build_catchup_summary(since_past)
        check("indexed" in summary.lower(), f"catchup mentions indexed file (got {summary!r})")
        check("reminder" in summary.lower(), f"catchup mentions fired reminder (got {summary!r})")

        # Internal __nova_ reminders should be excluded from the fired-reminder count
        since_past2 = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        rid2 = await mem.create_reminder(title="__nova_habit_weather.current__", details="internal", due_at_iso=datetime.now().isoformat(), recurrence="none")
        await mem.complete_reminder(reminder_id=str(rid2))
        summary2 = await mem.build_catchup_summary(since_past2)
        # should still say "1 reminder(s)" not 2, since the internal one is filtered
        check("1 reminder" in summary2, f"internal __nova_ reminder excluded from count (got {summary2!r})")

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


asyncio.run(main())
