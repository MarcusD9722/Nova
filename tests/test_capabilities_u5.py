"""U5: conversational memory correction.

The semantic response cache this file also used to cover was deleted in the
2026-11 cleanup: it was never in the chat path, and it keyed on the question
embedding alone with no speaker scoping, so it could not be wired without
breaking P5 memory isolation. Correction is tested end-to-end against real
memory, and `correct_fact` is live production code.
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory.unifier import MemoryUnifier

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


async def main():
    # ── CONVERSATIONAL MEMORY CORRECTION ──
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem = MemoryUnifier(Path(td), enable_chroma=False)
        await mem.initialize()

        # Nova remembered something wrong.
        await mem.add_fact(entity="user", attribute="anniversary", value="June 12", confidence=0.8)
        check(len(await mem.get_facts(entity="user", attribute="anniversary")) == 1, "original fact stored")

        res = await mem.correct_fact("user", "anniversary", "June 14")
        check(res["ok"] and res["previous"] == "June 12" and res["now"] == "June 14",
              f"correction reports what changed (got {res})")

        rows = await mem.get_facts(entity="user", attribute="anniversary")
        check(len(rows) == 1, "correction SUPERSEDES — not two contradictory facts")
        check(rows[0].value == "June 14", "the corrected value is what remains")
        check(rows[0].verification_status == "stated", "correction is recorded as user-stated")
        check("June 12" in (rows[0].evidence or ""), "the superseded value is kept as evidence (auditable)")

        # correcting a list-valued attribute with an explicit old value
        await mem.add_fact(entity="user", attribute="friend", value="Dave", confidence=0.8)
        await mem.add_fact(entity="user", attribute="friend", value="Sam", confidence=0.8)
        await mem.correct_fact("user", "friend", "David", old_value="Dave")
        friends = {r.value for r in await mem.get_facts(entity="user", attribute="friend")}
        check("David" in friends and "Dave" not in friends, "targeted correction replaces only the named value")
        check("Sam" in friends, "other values in a list-valued attribute are untouched")

        bad = await mem.correct_fact("user", "", "x")
        check(bad["ok"] is False, "correction rejects missing fields")

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


asyncio.run(main())
