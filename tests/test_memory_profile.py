"""What Nova is able to remember about Marcus, and what she must not.

Two gaps this pins, both found by auditing her live database:

  1. The extractor's attribute set was family/identity ONLY, so a favourite
     food, a hobby or which days he works were parsed and then dropped. She
     was structurally incapable of remembering them.

  2. The name-list patterns capture `(.+)$` — everything to end of line — and
     then .capitalize() manufactured the very capitalization that would have
     exposed the mistake. Live result: user.child held "A Bed Time Story About
     A Dinosaur Named Rex", "July St" and "Called" next to Mateo and Liam, and
     all five were fed into the grounding context on every single turn.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Checks, ScriptedLLM

from core.policy.contracts import MemoryFact
from core.policy.memory_extractor import MemoryExtractorLLM
from core.runtime import RuntimeManager
from memory.unifier import MemoryUnifier

check = Checks()


async def test_name_filter() -> None:
    check.section("Name lists reject sentence fragments")
    split = RuntimeManager._split_name_list

    check(split("mateo and liam") == ["Mateo", "Liam"], "a plain list still works")
    check(split("Mateo, Liam and Sofia") == ["Mateo", "Liam", "Sofia"], "commas and 'and' both split")
    check(split("O'Brien") == ["O'Brien"], "an apostrophe name keeps its capital (was O'brien)")
    check(split("Mary-Jane") == ["Mary-Jane"], "a hyphenated name keeps both capitals")

    # The three that were actually in his database.
    for junk in ("a bed time story about a dinosaur named Rex", "July St", "called"):
        check(split(junk) == [], f"rejects the real junk: {junk[:38]!r}")
    for junk in ("tell them a story", "going to the store", "at 5 o'clock"):
        check(split(junk) == [], f"rejects a sentence fragment: {junk!r}")

    check(RuntimeManager._looks_like_person_name("Liam") is True, "a bare name passes")
    check(RuntimeManager._looks_like_person_name("Mary Jane Watson") is True, "three words still pass")
    check(RuntimeManager._looks_like_person_name("a b c d") is False, "four words are rejected")
    check(RuntimeManager._looks_like_person_name("") is False, "empty is rejected")


async def test_widened_attributes() -> None:
    check.section("The categories that used to be dropped")
    for attr, value in [
        ("favorite_food", "sushi"), ("hobby", "woodworking"), ("work_days", "Monday to Thursday"),
        ("trip", "Japan, spring 2025"), ("allergy", "penicillin"), ("appearance", "red hair"),
        ("job", "software engineer"), ("birthday", "April 12"), ("dislikes", "cilantro"),
    ]:
        try:
            f = MemoryFact(entity="user", attribute=attr, value=value, confidence=0.9)
            ok = f.attribute == attr
        except Exception:
            ok = False
        check(ok, f"can now store {attr} = {value!r}")

    rejected = False
    try:
        MemoryFact(entity="user", attribute="feeling_right_now", value="tired", confidence=0.9)
    except Exception:
        rejected = True
    check(rejected, "an attribute outside the list is still rejected (the set stays curated)")


async def test_extractor_keeps_mixed_batch() -> None:
    check.section("A mixed message keeps every supported fact")
    m = ScriptedLLM()
    m.default_reply = (
        '{"facts":['
        '{"entity":"user","attribute":"favorite_food","value":"sushi","confidence":0.9,"persist":true},'
        '{"entity":"user","attribute":"work_days","value":"Monday to Thursday","confidence":0.9,"persist":true},'
        '{"entity":"Leslie","attribute":"appearance","value":"red hair","confidence":0.8,"persist":true},'
        '{"entity":"user","attribute":"vibe_today","value":"good","confidence":0.9,"persist":true}'
        ']}'
    )
    out = await MemoryExtractorLLM(m, llm_semaphore=asyncio.Semaphore(1)).extract(user_text="x")
    got = {(f.entity, f.attribute) for f in out.facts}
    check(("user", "favorite_food") in got, "the food survives")
    check(("user", "work_days") in got, "the schedule survives")
    check(("Leslie", "appearance") in got, "a trait about somebody ELSE is attributed to them")
    check(("user", "vibe_today") not in got, "the unsupported attribute is dropped")
    check(len(out.facts) == 3, f"only the unsupported one is lost ({len(out.facts)}/4 kept)")


async def test_preferences_supersede(tmp: Path) -> None:
    """A changed preference must replace the old one, not sit beside it."""
    check.section("Single-valued preferences supersede")
    mem = MemoryUnifier(tmp, enable_chroma=False)
    await mem.initialize()

    await mem.add_fact(entity="user", attribute="favorite_food", value="sushi", confidence=0.9)
    await mem.add_fact(entity="user", attribute="favorite_food", value="ramen", confidence=0.9)
    foods = [f.value for f in await mem.get_facts(entity="user", attribute="favorite_food")]
    check(foods == ["ramen"], f"the newer favourite replaces the older ({foods})")

    await mem.add_fact(entity="user", attribute="work_days", value="Mon-Thu", confidence=0.9)
    await mem.add_fact(entity="user", attribute="work_days", value="Mon-Fri", confidence=0.9)
    days = [f.value for f in await mem.get_facts(entity="user", attribute="work_days")]
    check(days == ["Mon-Fri"], f"a changed schedule replaces the old one ({days})")

    # Multi-valued attributes must still accumulate.
    for h in ("woodworking", "cycling"):
        await mem.add_fact(entity="user", attribute="hobby", value=h, confidence=0.9)
    hobbies = sorted(f.value for f in await mem.get_facts(entity="user", attribute="hobby"))
    check(hobbies == ["cycling", "woodworking"], f"hobbies still accumulate ({hobbies})")

    for c in ("Mateo", "Liam"):
        await mem.add_fact(entity="user", attribute="child", value=c, confidence=0.9)
    kids = sorted(f.value for f in await mem.get_facts(entity="user", attribute="child"))
    check(kids == ["Liam", "Mateo"], f"children still accumulate ({kids})")


async def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        await test_name_filter()
        await test_widened_attributes()
        await test_extractor_keeps_mixed_batch()
        await test_preferences_supersede(Path(td))
    check.finish()


asyncio.run(main())
