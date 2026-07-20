"""Phase 6 / #5: persistent agent society — routing + durable state.

Selection and per-specialist state are deterministic and tested here; the LLM
council deliberation is the thin orchestration on top (verified at runtime).
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.orchestrator.society import COORDINATOR_ID, SPECIALISTS, roster, select_specialists
from memory.unifier import MemoryUnifier

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


async def main():
    # ── Roster integrity ──
    r = roster()
    check(len(r) == 11, f"roster has 11 specialists (got {len(r)})")
    coords = [s for s in r if s["coordinator"]]
    check(len(coords) == 1 and coords[0]["id"] == COORDINATOR_ID, "exactly one coordinator (chief_executive)")
    check("licensed" in SPECIALISTS["financial_planner"].persona.lower(),
          "financial planner persona carries the not-a-licensed-advisor guardrail")

    # ── Routing: single-domain ──
    check("chief_engineer" in select_specialists("my server keeps crashing with a GPU memory error"),
          "engineering query routes to the Chief Engineer")
    check(select_specialists("recommend a movie to watch tonight") == ["media_curator"],
          "media query routes to the Media Curator alone")
    check("security_specialist" in select_specialists("is my password storage secure?"),
          "security query routes to the Security Specialist")
    check("financial_planner" in select_specialists("how should I budget my money each month?"),
          "budget query routes to the Financial Planner")
    check("snowboard_coach" in select_specialists("help me improve my carving on the mountain"),
          "snowboarding query routes to the Snowboard Coach")

    # ── Routing: multi-domain adds the coordinator to synthesize ──
    multi = select_specialists("I'm stressed about my workout routine and sleep")
    check(len(multi) >= 2, f"multi-domain query engages multiple specialists (got {multi})")
    check(COORDINATOR_ID in multi, "a multi-specialist council includes the coordinator to synthesize")

    # ── Routing: no match -> coordinator answers alone ──
    check(select_specialists("qwerty zxcvb flubber") == [COORDINATOR_ID], "unmatched query falls back to the coordinator")

    # ── Durable per-specialist state ──
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem = MemoryUnifier(Path(td), enable_chroma=False)
        await mem.initialize()

        s0 = await mem.agent_state("chief_engineer")
        check(s0["confidence"] == 0.5 and s0["consulted"] == 0 and s0["experience"] == "new", "fresh specialist defaults")

        await mem.record_consultation("chief_engineer")
        s1 = await mem.agent_state("chief_engineer")
        check(s1["consulted"] == 1, "consultation increments experience")

        await mem.record_consultation("chief_engineer", helpful=True)
        s2 = await mem.agent_state("chief_engineer")
        check(s2["consulted"] == 2 and s2["helpful"] == 1, "helpful consultation tracked")
        check(s2["confidence"] > 0.5, f"helpful outcome nudges confidence up (got {s2['confidence']})")

        await mem.record_consultation("chief_engineer", helpful=False)
        s3 = await mem.agent_state("chief_engineer")
        check(s3["confidence"] < s2["confidence"], "unhelpful outcome nudges confidence down")

        # experience thresholds
        for _ in range(4):
            await mem.record_consultation("chief_engineer")
        check((await mem.agent_state("chief_engineer"))["experience"] == "practiced", "5+ consultations -> practiced")

        # ── Agent-scoped memory (learning history) ──
        await mem.agent_remember("research_scientist", "Marcus prefers primary sources over blog posts.", topic="preferences")
        notes = await mem.agent_recall("research_scientist")
        check(notes and "primary sources" in notes[0], "specialist recalls its own stored note")
        check(await mem.agent_recall("research_scientist", topic="preferences"), "specialist memory filterable by topic")
        check(await mem.agent_recall("psychologist") == [], "a different specialist has its own separate memory")

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


asyncio.run(main())
