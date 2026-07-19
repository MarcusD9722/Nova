import asyncio
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from memory.unifier import MemoryUnifier
from memory.backends.sqlite_backend import SQLiteMemoryBackend

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

        # No usage yet
        habit0 = await mem.detect_habit("weather.current")
        check(habit0 is None, "no logged calls -> no habit detected")

        # Simulate weather.current called around 7-8am on 5 distinct recent days
        now = datetime.now(timezone.utc)
        for i in range(5):
            day = now - timedelta(days=i)
            ts = day.replace(hour=7, minute=15, second=0, microsecond=0).isoformat()
            async with __import__("aiosqlite").connect(mem._sqlite._db_path) as db:
                await db.execute("INSERT INTO tool_usage_log(tool_name, called_at) VALUES(?, ?)", ("weather.current", ts))
                await db.commit()

        habit = await mem.detect_habit("weather.current", min_distinct_days=4)
        check(habit is not None, "5 same-hour distinct days -> habit detected")
        check(habit is not None and habit["distinct_days"] >= 4, f"distinct_days >= 4 (got {habit})")

        # Sparse/random calls should NOT trigger a habit
        for i, h in enumerate([3, 14, 22, 9]):
            day = now - timedelta(days=i * 3)
            ts = day.replace(hour=h, minute=0, second=0, microsecond=0).isoformat()
            async with __import__("aiosqlite").connect(mem._sqlite._db_path) as db:
                await db.execute("INSERT INTO tool_usage_log(tool_name, called_at) VALUES(?, ?)", ("web.search", ts))
                await db.commit()
        habit_random = await mem.detect_habit("web.search", min_distinct_days=4)
        check(habit_random is None, f"scattered hours across days -> no habit (got {habit_random})")

        # Suggest-once guard
        should1 = await mem.should_suggest_habit("weather.current")
        check(should1 is True, "should_suggest_habit True before marking")
        await mem.mark_habit_suggested("weather.current")
        should2 = await mem.should_suggest_habit("weather.current")
        check(should2 is False, "should_suggest_habit False after marking")

        # distinct_logged_tools
        tools = await mem.distinct_logged_tools(window_days=14)
        check("weather.current" in tools and "web.search" in tools, f"distinct_logged_tools sees both (got {tools})")

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


asyncio.run(main())
