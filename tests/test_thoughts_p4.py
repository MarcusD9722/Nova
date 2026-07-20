"""Phase 4 / #6: persistent internal thoughts — persist, dedup, filter, resolve,
and stay private (surfaced only via explicit recall)."""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("NOVA_INTERNAL_THOUGHTS", "1")

from memory.thoughts import normalize_kind
from memory.unifier import MemoryUnifier

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


async def main():
    # ── kind normalization ──
    check(normalize_kind("Improvement") == "improvement", "known kind normalizes")
    check(normalize_kind("banana") == "note", "unknown kind falls back to note")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem = MemoryUnifier(Path(td), enable_chroma=False)
        await mem.initialize()

        # ── record + recall ──
        tid = await mem.note_thought("idea", "Nova could pre-warm the model on boot.", topic="perf")
        check(bool(tid), "thought recorded")
        got = await mem.recall_thoughts()
        check(len(got) == 1 and got[0]["kind"] == "idea", "recall surfaces the open thought")

        # ── dedup: same content + kind doesn't duplicate ──
        await mem.note_thought("idea", "Nova could pre-warm the model on boot.", topic="perf")
        check(len(await mem.recall_thoughts()) == 1, "identical open thought is not duplicated")

        # ── kinds + topic filtering ──
        await mem.note_thought("question", "Does chunk overlap help synthesis recall?", topic="memory")
        await mem.note_thought("improvement", "Cache geocode lookups.", topic="maps")
        only_q = await mem.recall_thoughts(kind="question")
        check(len(only_q) == 1 and only_q[0]["kind"] == "question", "filter by kind works")
        by_topic = await mem.recall_thoughts(topic="maps")
        check(len(by_topic) == 1 and "geocode" in by_topic[0]["content"], "filter by topic works")

        # ── resolve removes it from open recall ──
        await mem.resolve_thought(tid)
        remaining = {t["content"] for t in await mem.recall_thoughts()}
        check("Nova could pre-warm the model on boot." not in remaining, "resolved thought drops out of open recall")
        stats = await mem.thoughts.stats()
        check(stats["total"] == 3 and stats["open"] == 2, f"stats track open vs total (got {stats})")

        # ── flag OFF: private system disabled cleanly ──
        os.environ["NOVA_INTERNAL_THOUGHTS"] = "0"
        check(await mem.note_thought("idea", "should not store") == "", "flag off -> note refuses")
        check(await mem.recall_thoughts() == [], "flag off -> recall empty")
        os.environ["NOVA_INTERNAL_THOUGHTS"] = "1"

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


asyncio.run(main())
