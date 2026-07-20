"""Phase 4 / #11: semantic world model — sourced general-knowledge triples,
reinforcement, freshness, and the refusal to store unsourced facts."""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("NOVA_WORLD_MODEL", "1")

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

        # ── stores a sourced triple ──
        ok = await mem.world_learn("Python", "is_a", "programming language", source="https://python.org")
        check(ok, "sourced world fact stored")

        # ── refuses an UNSOURCED fact (world knowledge is never an assumption) ──
        ok2 = await mem.world_learn("Python", "created_by", "Guido van Rossum", source="")
        check(ok2 is False, "unsourced world fact refused")

        # ── recall returns known facts + freshness ──
        recall = await mem.world_recall("Python")
        check(recall["known"] if "known" in recall else bool(recall["facts"]), "recall finds the stored fact")
        check(recall["facts"][0]["source"] == "https://python.org", "source attribution preserved")
        check(recall["fresh"] is True, "just-learned fact reads as fresh")

        # ── reinforcement: re-learning bumps confidence, doesn't duplicate ──
        c0 = recall["facts"][0]["confidence"]
        await mem.world_learn("Python", "is_a", "programming language", source="https://docs.python.org")
        recall2 = await mem.world_recall("Python")
        check(len(recall2["facts"]) == 1, "re-learning the same triple doesn't duplicate")
        check(recall2["facts"][0]["confidence"] > c0, "re-learning reinforces confidence")
        check(recall2["facts"][0]["source"] == "https://docs.python.org", "latest source wins on reinforce")

        # ── unknown subject: honest empty ──
        unknown = await mem.world_recall("Klingon warp theory")
        check(not unknown["facts"], "unknown subject returns no facts (honest)")

        # ── web finding folds in with the URL as source ──
        await mem.remember_web_finding("RTX 5080", "Nvidia GPU, Blackwell architecture, 16GB GDDR7", "https://nvidia.com/5080")
        gpu = await mem.world_recall("RTX 5080")
        check(gpu["facts"] and gpu["facts"][0]["source"] == "https://nvidia.com/5080", "web finding stored with URL source")

        # ── separation from the personal graph: world facts are NOT graph edges ──
        rel = await mem.related("Python")
        check(not rel["neighbors"], "world facts don't leak into the personal knowledge graph")

        st = await mem.world.stats()
        check(st["triples"] >= 2 and st["subjects"] >= 2, f"stats reflect stored triples (got {st})")

        # ── feature flag OFF -> disabled, no writes ──
        os.environ["NOVA_WORLD_MODEL"] = "0"
        off = await mem.world_learn("X", "is", "y", source="s")
        check(off is False, "flag off -> world_learn refuses")
        r_off = await mem.world_recall("Python")
        check(r_off.get("enabled") is False, "flag off -> world_recall reports disabled")
        os.environ["NOVA_WORLD_MODEL"] = "1"

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


asyncio.run(main())
