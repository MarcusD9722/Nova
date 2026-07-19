"""WS3 verification: lesson capture patterns + memory store/get + dedup."""
import asyncio, sys, tempfile
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from core.runtime import RuntimeManager  # noqa: E402
from memory.unifier import MemoryUnifier, LESSON_ENTITY  # noqa: E402

fails = []
def check(c, m):
    print(("  OK  " if c else " FAIL ") + m);
    if not c: fails.append(m)

def _matches(msg: str) -> bool:
    if RuntimeManager._LESSON_SKIP.search(msg):
        return False
    return any(p.search(msg) for p in RuntimeManager._LESSON_PATTERNS)

# --- pattern detection (should capture) ---
should = [
    "From now on, keep your replies short.",
    "I prefer you call me Marcus, not sir.",
    "Please always greet me by name.",
    "Never bring up work unless I do.",
    "Remember to ask about the kids.",
    "Stop apologizing so much.",
    "No, that's wrong — the capital is different.",
    "Don't use bullet points when we chat.",
]
# --- should NOT capture (questions / build reqs / normal chat) ---
should_not = [
    "How are you doing tonight?",
    "Make a snake game called Cobra",
    "What is the weather today?",
    "You always know how to help.",   # 'always' mid-sentence, not a directive
    "Can you build me a calculator?",
    "I had a long day at work.",
]
for m in should:
    check(_matches(m), f"captures: {m!r}")
for m in should_not:
    check(not _matches(m), f"skips: {m!r}")

async def mem_tests():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem = MemoryUnifier(Path(td), enable_chroma=False)
        await mem.initialize()
        await mem.add_lesson("Keep replies short.", topic="style")
        await mem.add_lesson("Keep replies short.", topic="style")  # exact dup
        await mem.add_lesson("Call me Marcus, not sir.", topic="preference")
        lessons = await mem.get_lessons(limit=10)
        check(len(lessons) == 2, f"dedup: 2 distinct lessons stored (got {len(lessons)})")
        check(any("short" in l for l in lessons) and any("Marcus" in l for l in lessons), "both lessons retrievable")
        recs = await mem.lesson_records()
        check(len(recs) == 2 and all("topic" in r and "text" in r for r in recs), "lesson_records shaped for UI")
        # Lessons are searchable. This suite runs with enable_chroma=False, so
        # only keyword (LIKE) matching is available — a pure paraphrase like
        # "how should I address you" structurally CANNOT match (that requires
        # the semantic index). Use a query sharing real tokens with the lesson.
        hits = await mem.search(q="call me Marcus not sir", limit=8)
        check(any("Marcus" in h.text for h in hits), "lessons surface in keyword search (semantic needs Chroma)")

asyncio.run(mem_tests())
print("\nRESULT:", "ALL PASS" if not fails else f"{len(fails)} FAILURES")
sys.exit(1 if fails else 0)
