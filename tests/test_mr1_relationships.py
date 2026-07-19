import asyncio
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from core.dates import parse_month_day
from memory.unifier import MemoryUnifier

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


async def main():
    # parse_month_day
    check(parse_month_day("1990-04-12") == (4, 12), "parse ISO date")
    check(parse_month_day("April 12") == (4, 12), "parse 'Month DD'")
    check(parse_month_day("12 April") == (4, 12), "parse 'DD Month'")
    check(parse_month_day("4/12") == (4, 12), "parse 'M/D'")
    check(parse_month_day("not a date") is None, "garbage returns None")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem = MemoryUnifier(Path(td), enable_chroma=False)
        await mem.initialize()

        # --- merge-on-write for upsert_person ---
        await mem.upsert_person(name="Liam", attributes={"relation": "son"})
        await mem.upsert_person(name="Liam", attributes={"birthday": "April 12"})
        person = await mem.recall_person("Liam")
        check(person is not None, "recall_person finds Liam")
        check(person["attributes"].get("relation") == "son", "earlier attribute (relation) survived merge")
        check(person["attributes"].get("birthday") == "April 12", "later attribute (birthday) also present")
        check(len(person["important_dates"]) == 1 and person["important_dates"][0]["month"] == 4, "birthday parsed into important_dates")

        # --- last_mentioned via turns ---
        check(person["last_mentioned"] is None, "no turns yet -> last_mentioned None")

        # --- upcoming dates window ---
        soon = (datetime.now() + timedelta(days=2))
        await mem.upsert_person(name="TestSoon", attributes={"birthday": f"{soon.month}-{soon.day}"})
        far = (datetime.now() + timedelta(days=200))
        await mem.upsert_person(name="TestFar", attributes={"birthday": f"{far.month}-{far.day}"})
        upcoming = await mem.list_people_with_upcoming_dates(within_days=3)
        names = {u["name"] for u in upcoming}
        check("TestSoon" in names, "birthday in 2 days is in the upcoming(3) window")
        check("TestFar" not in names, "birthday in 200 days is NOT in the upcoming(3) window")

        # --- interest drift ---
        drift_empty = await mem.recent_interest_drift(weeks=6)
        check(drift_empty == "", "no interest_focus facts yet -> empty drift string")
        await mem.record_interest_focus("woodworking", week="2026-W20")
        await mem.record_interest_focus("the Nova project", week="2026-W28")
        drift = await mem.recent_interest_drift(weeks=6)
        check("woodworking" in drift and "Nova project" in drift, f"drift mentions both topics (got: {drift!r})")

        # --- CH1 sanity: person hits should be high confidence ---
        hits = await mem.search(q="Liam birthday", limit=8)
        best = max((h.score for h in hits), default=0.0)
        check(best >= 0.80, f"person search scores >=0.80 (got {best})")

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


asyncio.run(main())
