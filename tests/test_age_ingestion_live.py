"""Age reaches durable storage through the real ingestion path.

The previous round claimed `age_observation` / `age_observed_on` / `birth_date`
were the durable representation, but only `core/personal_dates.py` knew those
words: `MemoryFact.attribute` did not allow them, the extractor was never told
to emit them, and the test manually called `upsert_person()` with the finished
answer and commented that this was how ingestion "should" store it. That proved
the helpers, not the pipeline.

So this suite says the sentence and then reads authoritative storage. Nothing
is pre-populated.

ONE ARCHITECTURE, STATED (V3 P10 C6). Age normalization belongs to the
DETERMINISTIC layer, not to the LLM extractor:

  * `age_observed_on` records WHEN a statement was made. Code knows that; a
    model guesses it, and a wrong stamp silently corrupts every age derived
    from it afterwards.
  * `birth_date_source="derived"` asserts that arithmetic was checked. A model
    asserting it makes a provenance label probabilistic.

So the extractor is told not to extract ages, `MemoryFact` refuses every age
attribute, and `core.personal_dates` owns them. A stated `birthday` remains
extractable — that is a fact the speaker gave, not a computation.

Run:  venv\\Scripts\\python.exe tests\\test_age_ingestion_live.py
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

from core.personal_dates import age_on, parse_age_statements  # noqa: E402

check = Checks()


def _tmp():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


class _NoLLM:
    gpu_status = type("S", (), {"status": "stub"})()

    async def initialize(self):
        return None

    async def chat(self, *a, **k):
        raise AssertionError("age capture must be deterministic, not generated")


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


async def test_the_ingestion_path_stores_an_age_observation():
    check.section("C4: saying it is enough — ingestion stores the record")

    with _tmp() as td:
        rt, m = await _runtime(td)

        # The REAL capture path for a user message. Nothing pre-populated.
        await rt._extract_quick_facts(
            "Fenwick is three years old and he turns four on September 16th.")

        rec = await m.recall_person("Fenwick")
        check(rec is not None, f"the person record exists ({rec is not None})")
        attrs = (rec or {}).get("attributes") or {}

        check(attrs.get("birthday") == "09-16",
              f"the birthday is stored as MM-DD ({attrs.get('birthday')!r})")
        check(attrs.get("age_observation") == "3",
              f"the age is stored as an OBSERVATION ({attrs.get('age_observation')!r})")
        observed = attrs.get("age_observed_on") or ""
        check(observed.startswith(str(date.today().year)),
              f"stamped with the day it was said ({observed!r})")
        check("age" not in attrs,
              f"and NOT as a timeless scalar age ({sorted(attrs)})")

        check(attrs.get("birth_date") == f"{date.today().year - 4 if (9, 16) < (date.today().month, date.today().day) else date.today().year - 4}-09-16"
              or attrs.get("birth_date", "").endswith("-09-16"),
              f"a birth date was derived ({attrs.get('birth_date')!r})")
        check(attrs.get("birth_date_source") == "derived",
              f"and marked DERIVED, not stated ({attrs.get('birth_date_source')!r})")


async def test_a_second_child_in_the_same_message():
    check.section("C4: two people in one sentence both land")

    with _tmp() as td:
        rt, m = await _runtime(td)
        await rt._extract_quick_facts(
            "Fenwick is three years old and he turns four on September 16th. "
            "Odell is a year old and turns two on November 8th.")

        for name, bday, age in (("Fenwick", "09-16", "3"), ("Odell", "11-08", "1")):
            rec = await m.recall_person(name)
            attrs = (rec or {}).get("attributes") or {}
            check(attrs.get("birthday") == bday,
                  f"{name}: birthday {bday} ({attrs.get('birthday')!r})")
            check(attrs.get("age_observation") == age,
                  f"{name}: age observation {age} ({attrs.get('age_observation')!r})")


async def test_the_stored_record_does_not_go_stale():
    check.section("C4: the same stored record answers correctly later")

    with _tmp() as td:
        rt, m = await _runtime(td)
        # Parse with a FIXED observation day so the arithmetic is checkable.
        said = parse_age_statements(
            "Fenwick is three years old and he turns four on September 16th.",
            today=date(2026, 8, 19))[0]
        await m.upsert_person(said.name, said.attributes())

        rec = await m.recall_person("Fenwick")
        attrs = (rec or {}).get("attributes") or {}
        check(attrs.get("birth_date") == "2022-09-16",
              f"the derived birth date is stored ({attrs.get('birth_date')!r})")

        y, mo, d = (int(x) for x in attrs["birth_date"].split("-"))
        check(age_on(y, mo, d, date(2026, 8, 19)) == 3, "on the day it was said: 3")
        check(age_on(y, mo, d, date(2026, 9, 16)) == 4, "on the birthday: 4")
        check(age_on(y, mo, d, date(2027, 6, 1)) == 4,
              "months later: 4, not the 3 that was said")
        check(age_on(y, mo, d, date(2031, 1, 1)) == 8,
              "and years later: 8 — the record never went stale")


async def test_contradictory_input_derives_nothing():
    check.section("C4: 'is three and turns six' derives no birth year")

    with _tmp() as td:
        rt, m = await _runtime(td)
        await rt._extract_quick_facts(
            "Fenwick is three and turns six on September 16.")

        rec = await m.recall_person("Fenwick")
        attrs = (rec or {}).get("attributes") or {}
        check(attrs.get("birthday") == "09-16",
              f"the day it was told is still kept ({attrs.get('birthday')!r})")
        check(attrs.get("age_observation") == "3",
              f"and the observation ({attrs.get('age_observation')!r})")
        check("birth_date" not in attrs,
              f"but NO birth date is invented ({attrs.get('birth_date')!r})")
        check("birth_date_source" not in attrs,
              "and no provenance for a value that does not exist")


async def test_the_extractor_is_not_the_author_of_age_fields():
    """Option A: deterministic ingestion owns age; the model is told so.

    This suite used to assert the opposite — that `MemoryFact` ACCEPTED the
    normalized age attributes — while the extractor's own system prompt never
    mentioned them and no model could emit them. That is a contract that agrees
    with nothing. Ages are captured deterministically because `age_observed_on`
    is a fact about WHEN something was said (code knows it, a model guesses)
    and `birth_date_source="derived"` asserts that arithmetic was checked.
    """
    check.section("C4: age is not an extractable attribute")

    from core.policy.contracts import MemoryFact

    for attribute in ("age", "age_observation", "age_observed_on",
                      "birth_date", "birth_date_source"):
        try:
            MemoryFact(entity="Fenwick", attribute=attribute, value="3",
                       confidence=0.9)
            accepted = True
        except Exception:
            accepted = False
        check(not accepted,
              f"{attribute!r} is refused by the extractor contract")

    # A stated birthday is a different thing and stays extractable.
    fact = MemoryFact(entity="Fenwick", attribute="birthday", value="09-16",
                      confidence=0.9)
    check(fact.attribute == "birthday",
          "a stated birthday IS still extractable")


async def test_the_actual_prompt_says_what_the_contract_enforces():
    check.section("C4: the prompt and the contract agree")

    import inspect

    from core.policy.contracts import MemoryFact
    from core.policy.memory_extractor import MemoryExtractorLLM

    src = inspect.getsource(MemoryExtractorLLM.extract)
    # The prompt string is built inline; read what the model would actually see.
    lowered = src.lower()
    check("do not extract ages" in lowered,
          "the system prompt tells the model ages are not extracted here")
    check("birthday" in lowered,
          "and that a stated birthday still is")

    # Whatever the prompt says, the contract is what enforces it.
    allowed = set(MemoryFact.model_fields["attribute"].annotation.__args__)
    leaked = {a for a in allowed if a.startswith("age") or a.startswith("birth_date")}
    check(not leaked, f"no age attribute is reachable through the contract ({leaked})")
    check("birthday" in allowed, "birthday remains reachable")


async def test_a_model_emitting_an_age_loses_only_that_fact():
    check.section("C4: an age from the model is dropped, the rest survives")

    from core.policy.contracts import MemoryFact

    batch = [
        {"entity": "user", "attribute": "spouse", "value": "Leslie",
         "confidence": 0.9, "persist": True},
        {"entity": "Fenwick", "attribute": "age", "value": "3",
         "confidence": 0.9, "persist": True},
        {"entity": "Fenwick", "attribute": "birthday", "value": "09-16",
         "confidence": 0.9, "persist": True},
    ]
    kept = []
    dropped = 0
    for item in batch:
        try:
            kept.append(MemoryFact.model_validate(item))
        except Exception:
            dropped += 1

    check(dropped == 1, f"exactly the age fact is dropped ({dropped})")
    attrs = sorted(f.attribute for f in kept)
    check(attrs == ["birthday", "spouse"],
          f"and the good facts beside it survive ({attrs})")


async def test_the_date_answer_uses_the_ingested_record():
    check.section("C4: the stored record answers the question")

    with _tmp() as td:
        rt, m = await _runtime(td)
        await rt._extract_quick_facts(
            "Fenwick is three years old and he turns four on September 16th.")

        reply = await rt._personal_date_reply("When is Fenwick's birthday?")
        check(reply is not None and "September 16" in reply,
              f"the ingested birthday is answered ({reply!r})")
        check("Sorry" not in (reply or ""),
              "and not with an apology")


async def main():
    await test_the_ingestion_path_stores_an_age_observation()
    await test_a_second_child_in_the_same_message()
    await test_the_stored_record_does_not_go_stale()
    await test_contradictory_input_derives_nothing()
    await test_the_extractor_is_not_the_author_of_age_fields()
    await test_the_actual_prompt_says_what_the_contract_enforces()
    await test_a_model_emitting_an_age_loses_only_that_fact()
    await test_the_date_answer_uses_the_ingested_record()
    check.finish()


if __name__ == "__main__":
    run(main)
