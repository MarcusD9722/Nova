"""P5 verification: date parsing, turn search, date-range recall, dated digests,
people/events activation."""
import asyncio, sys, tempfile
from datetime import datetime, timedelta
from pathlib import Path
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from core.dates import parse_date_range
from memory.unifier import MemoryUnifier

fails = []
def check(c, m):
    print(("  OK  " if c else " FAIL ") + m)
    if not c: fails.append(m)

# ── date parsing ─────────────────────────────────────────────────────────────
print("== parse_date_range ==")
now = datetime(2026, 7, 15, 20, 0)  # a Wednesday
r = parse_date_range("what did we talk about yesterday", now)
check(r and r[0].date() == datetime(2026, 7, 14).date(), "yesterday")
r = parse_date_range("what did we discuss last tuesday", now)
check(r and r[0].date() == datetime(2026, 7, 14).date(), "last tuesday -> 7/14")
r = parse_date_range("anything from last week?", now)
check(r and r[0].date() == datetime(2026, 7, 6).date(), "last week starts Mon 7/6")
r = parse_date_range("what happened in June", now)
check(r and r[0].month == 6, "in June")
check(parse_date_range("how are you doing?", now) is None, "no date phrase -> None")

async def main():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem = MemoryUnifier(Path(td), enable_chroma=False)
        await mem.initialize()
        conv = __import__("uuid").uuid4()

        # ── seed turns across two days ───────────────────────────────────────
        # (ingest_turn stamps created_at=now; to test date range we write turns
        #  directly via the sqlite backend with explicit timestamps)
        import uuid as _uuid
        today = datetime.now()
        yday = today - timedelta(days=1)
        await mem._sqlite.add_turn(turn_id=_uuid.uuid4(), conversation_id=conv, role="user",
                                   content="Let's plan the Hawaii trip for December", created_at_iso=yday.isoformat())
        await mem._sqlite.add_turn(turn_id=_uuid.uuid4(), conversation_id=conv, role="assistant",
                                   content="Sounds great, I'll help you plan Hawaii", created_at_iso=yday.isoformat())
        await mem._sqlite.add_turn(turn_id=_uuid.uuid4(), conversation_id=conv, role="user",
                                   content="Today I want to work on the game", created_at_iso=today.isoformat())

        print("\n== search_turns / recall_conversation ==")
        rows = await mem.recall_conversation(term="Hawaii", limit=10)
        check(any("Hawaii" in r["content"] for r in rows), "keyword recall finds Hawaii turn")
        # date range: only yesterday
        since = datetime.combine(yday.date(), datetime.min.time())
        until = since + timedelta(days=1) - timedelta(microseconds=1)
        rows = await mem.recall_conversation(since_iso=since.isoformat(), until_iso=until.isoformat(), limit=10)
        contents = " ".join(r["content"] for r in rows)
        check("Hawaii" in contents and "work on the game" not in contents, "date-range recall isolates the day")
        check(any(r["speaker"] == "Marcus" for r in rows), "speaker labeled")

        print("\n== dated digests accumulate ==")
        cid = f"conversation:{conv}:digest"
        await mem.add_fact(entity=cid, attribute="2026-07-14", value="[2026-07-14] talked about Hawaii", confidence=0.75)
        await mem.add_fact(entity=cid, attribute="2026-07-15", value="[2026-07-15] worked on the game", confidence=0.75)
        await mem.add_fact(entity=cid, attribute="2026-07-15", value="[2026-07-15] worked on the game and memory", confidence=0.75)  # same day updates
        digests = await mem.get_facts(entity=cid, limit=20)
        days = sorted(d.attribute for d in digests)
        check(days == ["2026-07-14", "2026-07-15"], f"two days retained, same-day superseded (got {days})")
        check(any("memory" in d.value for d in digests), "same-day digest updated to latest")

        print("\n== people / events activated ==")
        await mem.upsert_person(name="Sarah", attributes={"relation": "coworker", "how_met": "at the office"})
        await mem.add_event(date="2026-12-10", note="Hawaii vacation")
        ppl = await mem.search(q="Sarah coworker", limit=8)
        check(any("Sarah" in h.text for h in ppl), "person recallable via search")
        evs = await mem.search(q="Hawaii vacation", limit=8)
        check(any("Hawaii" in h.text for h in evs), "event recallable via search")

    print("\nRESULT:", "ALL PASS" if not fails else f"{len(fails)} FAILURES")
    return 1 if fails else 0

sys.exit(asyncio.run(main()))
