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

        await mem.add_fact(entity="user", attribute="location", value="Denver, CO", confidence=0.95)

        # Strong match: literal fact lookup -> should be high confidence (>=0.80 bucket)
        hits_strong = await mem.search(q="Denver location", limit=8)
        best_strong = max((h.score for h in hits_strong), default=0.0)
        check(best_strong >= 0.80, f"fact hit scores >=0.80 (got {best_strong})")

        # Weak/no match: nothing relevant indexed -> low confidence
        hits_weak = await mem.search(q="what did we discuss about kayaking last month", limit=8)
        best_weak = max((h.score for h in hits_weak), default=0.0)
        check(best_weak < 0.80, f"unrelated query scores <0.80 (got {best_weak})")

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


asyncio.run(main())
