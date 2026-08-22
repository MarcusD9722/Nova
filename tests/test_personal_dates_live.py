"""Stored dates are answered from the store, not from a generation.

The live failure:

    Marcus: "When is Leslie's birthday and when is my birthday?"
    Nova:   "Sorry — I came up empty on that one."

…immediately followed by both birthdays answering correctly when asked one at a
time. Memory knew them. The combined question went through semantic search ->
prompt -> generation, the generation produced nothing visible, and the turn fell
through to the runtime's generic apology.

These tests use GENERIC fixture people, never Marcus's real names or database,
and they fail if the production path returns that apology while the facts are
present.

Run:  venv\\Scripts\\python.exe tests\\test_personal_dates_live.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, run  # noqa: E402

from core.personal_dates import (  # noqa: E402
    age_on, derive_birth_year, parse_date_query, parse_stored_date,
)

check = Checks()

APOLOGY = "Sorry — I came up empty on that one."


def _tmp():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


class _NoLLM:
    """Reaching the model at all means the structured path did not answer."""

    gpu_status = type("S", (), {"status": "stub"})()

    async def initialize(self):
        return None

    async def chat(self, *a, **k):
        raise AssertionError("a stored date must not require a generation")

    async def chat_stream(self, *a, **k):
        raise AssertionError("a stored date must not require a generation")
        yield ""

    async def generate(self, *a, **k):
        raise AssertionError("a stored date must not require a generation")


async def _runtime(td: str):
    from core.runtime import RuntimeManager
    from core.tooling import build_tool_router
    from memory.unifier import MemoryUnifier

    root = Path(td)
    projects = root / "projects"
    projects.mkdir(parents=True, exist_ok=True)
    mem_dir = root / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    m = MemoryUnifier(mem_dir, enable_chroma=False)
    await m.initialize()
    router = build_tool_router(repo_root=root, projects_dir=projects, memory=m)
    rt = RuntimeManager(repo_root=root, projects_dir=projects, memory=m,
                        llm=_NoLLM(), router=router, memory_dir=mem_dir)
    return rt, m


async def test_the_question_is_parsed_as_a_whole():
    check.section("Phase 2: a combined date question keeps BOTH subjects")

    q = parse_date_query("When is Robin's birthday and when is my birthday?")
    check(q is not None and q.subjects == ["Robin", ""],
          f"'Robin's and my birthday' -> both subjects ({q and q.subjects})")

    q = parse_date_query("What are Alex and Sam's birthdays?")
    check(q is not None and q.subjects == ["Alex", "Sam"],
          f"one possessive, two people ({q and q.subjects})")

    q = parse_date_query("When is my birthday?")
    check(q is not None and q.subjects == [""], f"just the speaker ({q and q.subjects})")

    for not_a_query in ("What are you capable of?", "Tell me a story about a dinosaur",
                        "Improve flappy-bird's collision handling."):
        check(parse_date_query(not_a_query) is None,
              f"{not_a_query[:34]!r} is not a date question")


async def test_stored_dates_answer_without_the_model():
    check.section("Phase 2: both dates come back, with no generation")

    with _tmp() as td:
        rt, m = await _runtime(td)
        await m.upsert_person("Robin", {"relation": "partner", "birthday": "March 14"})
        await m.add_fact(entity="user", attribute="birthday", value="1990-07-02",
                         confidence=0.95)

        reply = await rt._personal_date_reply(
            "When is Robin's birthday and when is my birthday?")
        check(reply is not None, f"the question is answered structurally ({reply})")
        check(reply != APOLOGY, "and NOT with the generic apology")
        check("March 14" in (reply or ""), f"Robin's date is present ({reply})")
        check("July 2" in (reply or ""), f"and the speaker's ({reply})")

        # One at a time still works.
        one = await rt._personal_date_reply("When is Robin's birthday?")
        check("March 14" in (one or "") and "July" not in (one or ""),
              f"a single subject answers only that subject ({one})")

        # No unrelated person leaks in.
        await m.upsert_person("Casey", {"birthday": "December 25"})
        again = await rt._personal_date_reply("When is Robin's birthday?")
        check("December" not in (again or ""),
              f"an unrelated person's date is not returned ({again})")


async def test_missing_data_is_admitted_not_invented():
    check.section("Phase 2: a missing date is said plainly")

    with _tmp() as td:
        rt, m = await _runtime(td)
        await m.upsert_person("Robin", {"birthday": "March 14"})

        reply = await rt._personal_date_reply("When is Jordan's birthday?")
        check(reply is not None and "Jordan" in reply,
              f"the missing subject is named ({reply})")
        check("don't have" in (reply or "").lower(),
              f"and reported as missing rather than invented ({reply})")
        check(not any(mon in (reply or "") for mon in ("January", "March", "December")),
              f"no date is fabricated ({reply})")

        mixed = await rt._personal_date_reply(
            "When is Robin's birthday and when is Jordan's birthday?")
        check("March 14" in (mixed or "") and "Jordan" in (mixed or ""),
              f"a mixed query gives what is known AND admits what is not ({mixed})")


async def test_an_unverified_speaker_gets_no_personal_record():
    check.section("Phase 2: privacy scoping is unchanged")

    from core.turn_identity import active_turn

    with _tmp() as td:
        rt, m = await _runtime(td)
        await m.add_fact(entity="user", attribute="birthday", value="1990-07-02",
                         confidence=0.95)

        try:
            from core.turn_identity import ident_status
            unknown = ident_status("unknown")
        except Exception:
            unknown = None

        if unknown is not None:
            with active_turn(unknown):
                reply = await rt._personal_date_reply("When is my birthday?")
            check(reply is None,
                  f"an unverified speaker gets no stored personal date ({reply})")
        else:
            check(True, "identity helper unavailable in this build — not asserted")


async def test_age_does_not_go_stale():
    check.section("Phase 2: age is an observation, not a timeless scalar")

    # "Mateo is three years old and he turns four on September 16th",
    # said on 2026-08-19.
    observed = date(2026, 8, 19)
    year = derive_birth_year(stated_age=3, birth_month=9, birth_day=16,
                             observed_on=observed, turns_next=4)
    check(year == 2022, f"the birth year is derived unambiguously ({year})")

    # The whole point: the SAME record still answers correctly later.
    check(age_on(2022, 9, 16, date(2026, 8, 19)) == 3,
          "age on the day it was stated is 3")
    check(age_on(2022, 9, 16, date(2026, 9, 16)) == 4,
          "on the birthday it is 4")
    check(age_on(2022, 9, 16, date(2027, 1, 1)) == 4,
          "months later it is still 4, not the stored 3")
    check(age_on(2022, 9, 16, date(2030, 9, 15)) == 7,
          "and years later it keeps up (7 the day before turning 8)")

    # Contradictory input is refused rather than guessed.
    check(derive_birth_year(stated_age=3, birth_month=9, birth_day=16,
                            observed_on=observed, turns_next=6) is None,
          "'is three and turns six' derives nothing")

    # A stored MM-DD stays a MM-DD; no year is invented.
    check(parse_stored_date("September 16") == (None, 9, 16),
          f"a bare birthday keeps no year ({parse_stored_date('September 16')})")
    check(parse_stored_date("2022-09-16") == (2022, 9, 16),
          "a full birth date keeps its year")


async def test_a_derived_year_is_not_reported_as_stated():
    check.section("Phase 2: a derived year is not presented as a stated fact")

    with _tmp() as td:
        rt, m = await _runtime(td)
        # Stored the way the extractor should store it: the day is stated, the
        # year is derived, and the record says which is which.
        await m.upsert_person("Mateo", {
            "birthday": "09-16",
            "birth_date": "2022-09-16",
            "birth_date_source": "derived",
            "age_observation": "3",
            "age_observed_on": "2026-08-19",
        })
        reply = await rt._personal_date_reply("When is Mateo's birthday?")
        check(reply is not None and "September 16" in reply,
              f"the stated day is answered ({reply})")
        check("2022" not in (reply or ""),
              f"the DERIVED year is not stated back as fact ({reply})")


async def main():
    await test_the_question_is_parsed_as_a_whole()
    await test_stored_dates_answer_without_the_model()
    await test_missing_data_is_admitted_not_invented()
    await test_an_unverified_speaker_gets_no_personal_record()
    await test_age_does_not_go_stale()
    await test_a_derived_year_is_not_reported_as_stated()
    check.finish()


if __name__ == "__main__":
    run(main)
