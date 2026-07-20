"""Phase 5 / #9: autonomous research — topic store + sourced findings.

The worker's live search/summarize is network+model-driven (verified at runtime);
here we test the deterministic parts: tracking topics, least-recently-checked
ordering, and that findings are the world model's SOURCED entries.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("NOVA_WORLD_MODEL", "1")

from core.workers.research_worker import research_enabled
from memory.unifier import MemoryUnifier

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


async def main():
    # research is opt-in / off by default
    os.environ.pop("NOVA_RESEARCH", None)
    check(research_enabled() is False, "autonomous research is OFF by default (network calls)")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem = MemoryUnifier(Path(td), enable_chroma=False)
        await mem.initialize()

        # ── track topics ──
        check(await mem.track_research_topic("GPU technology"), "topic tracked")
        check(await mem.track_research_topic("Snowboarding gear"), "second topic tracked")
        check(await mem.track_research_topic("   ") is False, "blank topic refused")

        topics = await mem.list_research_topics()
        check(len(topics) == 2, f"both topics listed (got {len(topics)})")
        check(all(t["last_checked"] is None for t in topics), "new topics have no last_checked yet")

        # ── tracking the same topic again doesn't duplicate ──
        await mem.track_research_topic("GPU technology")
        check(len(await mem.list_research_topics()) == 2, "re-tracking a topic doesn't duplicate")

        # ── least-recently-checked ordering ──
        nxt = await mem.next_research_topic()
        check(nxt in ("GPU technology", "Snowboarding gear"), "next topic is an unchecked one")
        await mem.mark_research_checked("GPU technology")
        # GPU is now checked; the never-checked one should come next
        check(await mem.next_research_topic() == "Snowboarding gear", "checked topic drops behind the unchecked one")

        # ── findings are the SOURCED world-model entries ──
        await mem.remember_web_finding("GPU technology", "Blackwell succeeds Ada Lovelace.", "https://nvidia.com")
        findings = await mem.research_findings("GPU technology")
        check(findings and findings[0]["source"] == "https://nvidia.com", "findings carry their source citation")

        # ── untrack removes the topic and its checked stamp ──
        removed = await mem.untrack_research_topic("GPU technology")
        check(removed >= 1, "untrack removes the topic")
        remaining = {t["topic"] for t in await mem.list_research_topics()}
        check("GPU technology" not in remaining, "untracked topic no longer listed")

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


asyncio.run(main())
