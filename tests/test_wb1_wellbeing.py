import asyncio
import sys
import tempfile
from pathlib import Path

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

        # No signal yet
        trend0 = await mem.recent_wellbeing_trend(days=5)
        check(trend0 == "", "no wellbeing facts -> empty trend")

        # One late night is not a pattern yet
        await mem.record_wellbeing_signal("late_night", day="2026-07-14")
        trend1 = await mem.recent_wellbeing_trend(days=5)
        check(trend1 == "", f"single late_night day is not yet a pattern (got {trend1!r})")

        # Two+ late nights -> a trend
        await mem.record_wellbeing_signal("late_night", day="2026-07-15")
        await mem.record_wellbeing_signal("late_night", day="2026-07-16")
        trend2 = await mem.recent_wellbeing_trend(days=5)
        check("late" in trend2.lower(), f"multiple late_night days -> trend mentions it (got {trend2!r})")

        # Nudge guard: should nudge initially, then not again immediately after marking
        should1 = await mem.should_nudge_wellbeing(min_gap_days=3)
        check(should1 is True, "should_nudge_wellbeing True with no prior nudge")
        await mem.mark_wellbeing_nudged()
        should2 = await mem.should_nudge_wellbeing(min_gap_days=3)
        check(should2 is False, "should_nudge_wellbeing False right after marking (gap not yet elapsed)")

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


asyncio.run(main())
