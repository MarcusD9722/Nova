"""Asking Nova whether it is done, over real HTTP (Stage 14 §10).

Every turn here goes through `POST /chat`. What is asserted is the GROUNDING —
what the answer step was actually handed — because with a scripted model the
reply text would only test the script, and what a real model receives is what
decides whether it can answer honestly at all.

The defect this suite exists for was measured, not imagined: asked "Is it
done?" about a project whose authoritative state was FAILING with a named
failing criterion, the answer step received nothing. `describe_work_state`
covers goals and tasks, and a project with acceptance criteria has no goal
rows, so it returned "" and the model answered from an empty prompt.

Run:  venv\\Scripts\\python.exe tests\\test_completion_chat_s14.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, boot, run  # noqa: E402

from core.completion import (  # noqa: E402
    COMPLETE, FAILED, FAILING, PARTIALLY_IMPLEMENTED, PASSED, PASSING,
    SCAFFOLDED,
)

check = Checks()

REQUEST = "a calculator that adds and subtracts"
MARKER = "there is no subtract"

QUESTIONS = [
    "Is it done?",
    "Did you finish it?",
    "Did everything pass?",
    "What's left?",
    "Which requirement is failing?",
    "Can I use it now?",
    "So everything is completely finished, right?",
    "There aren't any failures left, correct?",
]

#: Ordinary sentences that happen to contain a finishedness word. None of them
#: is a question about the project, and none should pull the record in.
NEAR_MISSES = [
    "Is dinner ready?",
    "I am done with my coffee.",
    "Did you finish your dinner?",
    "Are you ready to go out?",
    "That film was finished by midnight.",
    "I finished my book.",
]


async def seed(nova, *, subtract=False, human_criterion=False, prove=True):
    """Put a project into a known authoritative state, via the service."""
    pb = nova.runtime._project_builder
    svc = pb.completion
    proj = nova.projects_dir / "calculator"
    proj.mkdir(parents=True, exist_ok=True)
    rev = await svc.record_request(slug="calculator", request_text=REQUEST)
    specs = [{"text": "adds two numbers", "origin_quote": "adds"},
             {"text": "subtracts two numbers", "origin_quote": "subtracts"}]
    if human_criterion:
        specs[1]["verify_kind"] = "human"
    ids = await svc.set_criteria(slug="calculator", revision=rev, criteria=specs)
    await svc.seal_contract(slug="calculator", revision=rev)
    body = "def add(a, b):\n    return a + b\n"
    if subtract:
        body += "\n\ndef subtract(a, b):\n    return a - b\n"
    (proj / "main.py").write_text(body, encoding="utf-8")
    # A REAL project has a PROJECT.md. `last_active()` verifies that the
    # pointer names an actual project before trusting it, so without this the
    # seed built a directory Nova does not recognise: last_active returned
    # None, _completion_context bailed, and EVERY grounding block came back
    # empty. The instrumentation found that, not the assertions.
    pb._write_project_md("calculator", brief=REQUEST, status="building",
                         summary="a calculator")

    if prove:
        ctx = await svc.begin_check(slug="calculator", criterion_id=ids[0])
        await svc.record_verdict(context=ctx, verdict=PASSED)
        if subtract:
            ctx2 = await svc.begin_check(slug="calculator", criterion_id=ids[1])
            await svc.record_verdict(context=ctx2, verdict=PASSED)
        elif not human_criterion:
            ctx2 = await svc.begin_check(slug="calculator", criterion_id=ids[1])
            await svc.record_verdict(context=ctx2, verdict=FAILED, error=MARKER)
    await nova.memory.add_fact(entity="projects", attribute="last_active",
                               value="calculator", confidence=0.95)
    return svc, ids


async def ask(nova, message: str) -> str:
    """One real /chat turn. Returns what the ANSWER step was given."""
    before = len(nova.llm.prompts)
    await nova.http.post("/chat", json={"message": message})
    new = nova.llm.prompts[before:]
    answers = [p for p in new
               if "You are Nova" in p and "agent brain for Nova" not in p]
    return answers[-1] if answers else ""


def block(grounding: str) -> str:
    """Just the completion section, so a match cannot come from elsewhere."""
    marker = "The completion state of the work"
    return grounding.split(marker, 1)[1] if marker in grounding else ""


async def test_a_failing_is_named_in_every_phrasing():
    check.section("§10 FAILING, asked eight ways")
    async with boot(default_reply="Sure.") as nova:
        svc, _ = await seed(nova)
        v = await svc.evaluate(slug="calculator")
        check(v.state == FAILING, f"the authoritative state is failing ({v.state})")

        missing_state, missing_criterion, missing_error = [], [], []
        for q in QUESTIONS:
            b = block(await ask(nova, q))
            if FAILING not in b:
                missing_state.append(q)
            if "subtracts two numbers" not in b:
                missing_criterion.append(q)
            if MARKER not in b:
                missing_error.append(q)
        check(not missing_state,
              f"every question is told the state is failing ({missing_state})")
        check(not missing_criterion,
              f"and which criterion is failing ({missing_criterion})")
        check(not missing_error,
              f"and the failure's own words ({missing_error})")


async def test_b_the_false_premises_meet_the_record():
    check.section("§10 a confident wrong premise is contradicted")
    async with boot(default_reply="Sure.") as nova:
        await seed(nova)
        for claim in ("So everything is completely finished, right?",
                      "There aren't any failures left, correct?",
                      "Looks like we're done here.",
                      "You finished all of it, correct?"):
            b = block(await ask(nova, claim))
            check(FAILING in b and "subtracts two numbers" in b,
                  f"{claim!r} is answered with the failure in front of it")
            check("trust it over anything you remember saying" in b,
                  "and the record is labelled as the thing to trust")


async def test_c_passing_is_not_complete():
    check.section("§10 PASSING and COMPLETE are different sentences")
    async with boot(default_reply="Sure.") as nova:
        svc, _ = await seed(nova, human_criterion=True)
        v = await svc.evaluate(slug="calculator")
        check(v.state == PASSING, f"the state is passing ({v.state})")
        b = block(await ask(nova, "Is it done?"))
        check(PASSING in b, "the answer is told it is passing")
        check("waiting on the user" in b,
              f"and that a person still owes an answer ({b[:200]!r})")
        check(f"'calculator': {COMPLETE}" not in b,
              "and it is never described as complete")


async def test_d_complete_says_complete():
    check.section("§10 COMPLETE, and the contract's provenance with it")
    async with boot(default_reply="Sure.") as nova:
        svc, _ = await seed(nova, subtract=True)
        v = await svc.evaluate(slug="calculator")
        check(v.state == COMPLETE, f"the state is complete ({v.state})")
        b = block(await ask(nova, "Is it done?"))
        check(COMPLETE in b, "the answer is told it is complete")
        check("sealed automatically" in b,
              f"and how the contract was sealed ({b[:220]!r})")
        check("nobody has confirmed" in b,
              "including that nobody confirmed the criteria mean what the "
              "request meant")


async def test_e_scaffolded_shows_what_is_unproven():
    check.section("§10 SCAFFOLDED names what has not been shown")
    async with boot(default_reply="Sure.") as nova:
        svc, _ = await seed(nova, prove=False)
        v = await svc.evaluate(slug="calculator")
        check(v.state == SCAFFOLDED, f"the state is scaffolded ({v.state})")
        b = block(await ask(nova, "What's left?"))
        check(SCAFFOLDED in b, "the state is there")
        check("adds two numbers: not yet checked" in b
              and "subtracts two numbers: not yet checked" in b,
              f"and both criteria are listed as unchecked ({b[:250]!r})")


async def test_f_a_stale_projection_does_not_win():
    check.section("§10 chat corrects the projection, not the evidence")
    async with boot(default_reply="Sure.") as nova:
        svc, _ = await seed(nova)
        proj = nova.projects_dir / "calculator"
        # Everything a stale world would say.
        (proj / "PROJECT.md").write_text(
            "# calculator\n\n## Brief\n" + REQUEST + "\n\n## Status\ncomplete\n\n"
            "## Summary\nall finished\n\n## Progress log\n- done\n",
            encoding="utf-8")
        await nova.memory.add_fact(entity="project:calculator",
                                   attribute="status", value="complete",
                                   confidence=0.9)
        v = await svc.evaluate(slug="calculator")
        check(v.state == FAILING, f"the evidence still says failing ({v.state})")

        b = block(await ask(nova, "Is it done?"))
        check(FAILING in b,
              f"the grounding carries the derived state, not the file's "
              f"({b[:160]!r})")
        check("subtracts two numbers" in b and MARKER in b,
              "with the failing criterion and its error")


async def test_g_ordinary_talk_pulls_in_nothing():
    check.section("§10 near misses stay ordinary conversation")
    async with boot(default_reply="Sure.") as nova:
        await seed(nova)
        caught = []
        for phrase in NEAR_MISSES:
            if block(await ask(nova, phrase)):
                caught.append(phrase)
        check(not caught,
              f"none of the {len(NEAR_MISSES)} near misses attached the "
              f"completion record ({caught})")


async def test_h_naming_the_project_is_enough():
    check.section("§10 'is the calculator ready?' vs 'is the kettle ready?'")
    async with boot(default_reply="Sure.") as nova:
        await seed(nova)
        named = block(await ask(nova, "Is the calculator ready?"))
        check(FAILING in named,
              f"naming a real project asks about that project ({named[:120]!r})")
        kettle = block(await ask(nova, "Is the kettle ready?"))
        check(not kettle,
              f"while the same sentence about a kettle does not ({kettle[:80]!r})")


async def main() -> None:
    await test_a_failing_is_named_in_every_phrasing()
    await test_b_the_false_premises_meet_the_record()
    await test_c_passing_is_not_complete()
    await test_d_complete_says_complete()
    await test_e_scaffolded_shows_what_is_unproven()
    await test_f_a_stale_projection_does_not_win()
    await test_g_ordinary_talk_pulls_in_nothing()
    await test_h_naming_the_project_is_enough()
    check.finish()


if __name__ == "__main__":
    run(main)
