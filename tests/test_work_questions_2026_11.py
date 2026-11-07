"""Asking about the work by its own name (test program layer 2, item 3).

`asks_about_work` decides whether the answer step is handed the durable record.
On 2026-11-06 it returned False for all eight of these:

    "Did the build finish?"          "did the project build ok?"
    "Did the planned work succeed?"  "Has the build finished yet?"
    "did the tests pass?"            "Did the job run?"
    "Did everything work?"           "Did my game build work?"

Measured before the fix, 43 of 51 natural ways of asking whether the work went
right matched nothing. Every shape took a pronoun or "everything" as its
subject; none took the work's own name. Marcus approved broadening the grammar
on 2026-11-07.

A false match is not free. A matched turn carries the record, and a reply that
then claims completion is corrected against it (the audit's C4 fix). So "did
the laundry finish?" answered "it's all done!" would be told that a project is
failing. Two such false matches were already live before this change, and both
are pinned here as fixed:

    "They finished their homework."             (the premise read "they ... finished")
    "Everything worked out with the dentist."   (the premise read "everything ... worked")

The end-to-end checks go through real `/chat` with a project whose
authoritative state is FAILING. Until this suite, nothing tested that the C4
correction fires at all.

Run:  venv\\Scripts\\python.exe tests\\test_work_questions_2026_11.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, boot, run  # noqa: E402

from core.completion import FAILED, FAILING, PASSED  # noqa: E402
from core.intent import asks_about_work  # noqa: E402

check = Checks()

#: The eight misses measured on 2026-11-06 (docs/TEST_PROGRAM.md, item 3).
MISSES = [
    "Did the build finish?",
    "Did the planned work succeed?",
    "did the tests pass?",
    "Did everything work?",
    "did the project build ok?",
    "Has the build finished yet?",
    "Did the job run?",
    "Did my game build work?",
]

#: The same question, asked about the work by what it is called.
BY_NAME = [
    "Did the build work?", "Did the build fail?", "Did the build go through?",
    "Is the build finished?", "Is the build complete?",
    "Was the build successful?", "Did the new build break anything?",
    "Did that build work?", "Has the project been built?",
    "Did the code compile?", "Is the code ready?",
    "Did all the tests pass?", "Have the tests passed?", "Did the tests fail?",
    "Are the tests passing?", "Are the tests still failing?",
    "Did the test suite pass?", "How did the tests go?",
    "Did the steps work?", "Did the next step work?", "Did your work finish?",
    "Did the changes work?", "Did the fix work?", "Did that fix work?",
    "How did the fix go?", "Did the improvements go through?",
    "Did the script run?", "Has the background job run yet?",
    "Did the project finish?", "Is the project finished?",
    "How did your project go?", "Did the game build?",
    "Does the game work now?", "Is the app working now?",
]

#: Pronoun questions that took only work/finish/succeed/run before.
PRONOUNS = [
    "Did it build?", "Did it compile?", "Did it pass?", "Did that fail?",
    "Did it crash?", "Does it work now?",
]

#: Questions about all of it at once.
SURVEYS = [
    "Did everything go through?", "Did everything build?",
    "Did anything fail?", "Did anything break?", "Did anything else fail?",
    "Did anything go wrong?", "Did something go wrong?",
    "Did anything fail in the build?", "Did something go wrong with the tests?",
    "Were there any errors?", "Any errors?", "Any errors in the build?",
    "Did it throw any errors?", "Did everything work with Chrome?",
]

#: CLAIMS by name: the dangerous form, because it invites Nova to agree.
CLAIMS = [
    "So the build finished, right?", "The build worked?",
    "The tests passed, right?", "So all the tests have passed?",
    "The project is done then?", "My project is finished!",
    "So the planned work succeeded?", "My game build worked, right?",
    "The job ran fine?", "The fix worked!", "My code works now.",
    "The changes went through?",
    # An outcome followed by a place or a person is refused, but not when the
    # thing it names is the work, and not a time.
    "It all worked out with the build.", "It all worked out in the end, right?",
    "It all went through with your new build?",
]

#: Each is here because it is a NEAR MISS for a shape above, sharing its
#: vocabulary. A list of unrelated sentences would pass against any regex.
NEAR_MISSES = [
    # A finish/run/done verb whose subject is the house.
    ("Did the laundry finish?", "the subject is the laundry"),
    ("Did the dishwasher run?", "the subject is the dishwasher"),
    ("Did the washing machine finish?", "same"),
    ("Did the bread finish baking?", "same"),
    ("Is the turkey done?", "same"),
    ("Has the movie finished?", "a film"),
    ("Did the party finish late?", "a party"),
    ("Did the meeting run long?", "a meeting"),
    # Work nouns that are as often somebody else's.
    ("Did the job interview go well?", "somebody's interview"),
    ("Did the new job work out?", "somebody's new job"),
    ("How did the job go?", "somebody's working day"),
    ("Did your workout go well?", "'work' inside another word"),
    ("Did the road work finish?", "the road crew's work"),
    ("Is the school project finished?", "a school project"),
    ("Did the promo code work?", "a discount code"),
    # Tests and games that are not software.
    ("Is the pregnancy test done?", "'done' is not a verb tests take"),
    ("Did the tests come back?", "medical tests"),
    ("Are the tests back yet?", "same"),
    ("How did the test go?", "one test is as often a school one"),
    ("Did your driving test go well?", "a driving test"),
    ("Did the Chiefs game finish?", "a football game"),
    ("Is the game done?", "same"),
    ("Did the game come out yet?", "a game release"),
    ("Did the TV program finish?", "a programme"),
    # The outcome belongs to someone or somewhere else. The first five
    # matched before this change.
    ("They finished their homework.", "their homework"),
    ("Did they all finish their dinner?", "their dinner"),
    ("They finished my car today.", "the garage's work on Marcus's car"),
    # DECIDED: the verbs added to "did everything ...?" were not added to
    # "did they all ...?", which is as often the kids. ("Did they all
    # finish?" is still read as work, by Stage 13C's premise "they ...
    # finished", and that decision is left as it was.)
    ("Did they all run the race?", "'they all' is as often the kids"),
    ("Everything worked out with the dentist.", "the dentist's"),
    ("Everything went through at the bank.", "the bank's"),
    ("I got everything done at work today.", "Marcus's job, not Nova's work"),
    ("Did something break in the kitchen?", "the kitchen"),
    ("Were there any errors on my bill?", "a bill"),
    # "Does it work now?" asks about a fix; this is agreement.
    ("Does that work for you?", "agreement, not a question about the work"),
    # Said in passing; agreeing with them must not be corrected.
    ("The Windows update finally finished.", "the computer, not a project"),
    ("The changes to my schedule are done.", "a schedule"),
    ("The tests are done.", "tests take only 'passed' as a claim"),
    # DECIDED, not missed: what people say to check a microphone. A voice
    # assistant hears these constantly, and "yes, it's working!" must not be
    # corrected against a project.
    ("Is it working?", "a microphone check"),
    ("Is this working?", "same"),
    ("Can you hear me, is it working?", "same"),
]

VOCABULARY = ("build", "test", "job", "work", "project", "code", "game",
              "program", "finish", "done", "run", "pass", "everything",
              "anything", "something", "error", "update", "change")


def test_the_eight_misses_now_get_the_record() -> None:
    check.section("item 3: the eight measured misses")
    missed = [q for q in MISSES if not asks_about_work(q)]
    check(not missed, f"all {len(MISSES)} attach the record "
                      f"({missed or 'none missed'})")


def test_the_work_by_its_own_name() -> None:
    check.section("the work named by what it is")
    for label, items in (("questions by name", BY_NAME),
                         ("pronoun questions", PRONOUNS),
                         ("surveys", SURVEYS),
                         ("claims by name", CLAIMS)):
        missed = [q for q in items if not asks_about_work(q)]
        check(not missed, f"all {len(items)} {label} attach the record "
                          f"({missed or 'none missed'})")


def test_near_misses_stay_ordinary() -> None:
    check.section("near misses that must stay ordinary")
    caught = [(q, why) for q, why in NEAR_MISSES if asks_about_work(q)]
    check(not caught, f"all {len(NEAR_MISSES)} stay ordinary "
                      f"({caught or 'none caught'})")


def test_the_near_misses_are_load_bearing() -> None:
    check.section("each near miss shares vocabulary with a recognised shape")
    weak = [q for q, _ in NEAR_MISSES
            if not any(v in q.lower() for v in VOCABULARY)]
    check(not weak, f"{len(NEAR_MISSES) - len(weak)} of {len(NEAR_MISSES)} "
                    f"are near misses (unrelated: {weak or 'none'})")


# -- end to end, through /chat ------------------------------------------------

REQUEST = "a calculator that adds and subtracts"
ERROR = "there is no subtract"
#: What a model says when it agrees. `_claims_completion` reads "it's all
#: done" and "working" as a claim that the work is finished.
AGREEING = "It's all done and working!"
CORRECTION = "To be accurate"


async def seed_failing(nova) -> None:
    """A real project whose authoritative state is FAILING, as Stage 14 seeds it."""
    pb = nova.runtime._project_builder
    svc = pb.completion
    (nova.projects_dir / "calculator").mkdir(parents=True, exist_ok=True)
    rev = await svc.record_request(slug="calculator", request_text=REQUEST)
    ids = await svc.set_criteria(slug="calculator", revision=rev, criteria=[
        {"text": "adds two numbers", "origin_quote": "adds"},
        {"text": "subtracts two numbers", "origin_quote": "subtracts"}])
    await svc.seal_contract(slug="calculator", revision=rev)
    (nova.projects_dir / "calculator" / "main.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8")
    pb._write_project_md("calculator", brief=REQUEST, status="building",
                         summary="a calculator")
    ok = await svc.begin_check(slug="calculator", criterion_id=ids[0])
    await svc.record_verdict(context=ok, verdict=PASSED)
    bad = await svc.begin_check(slug="calculator", criterion_id=ids[1])
    await svc.record_verdict(context=bad, verdict=FAILED, error=ERROR)
    await nova.memory.add_fact(entity="projects", attribute="last_active",
                               value="calculator", confidence=0.95)
    verdict = await svc.evaluate(slug="calculator")
    check(verdict.state == FAILING,
          f"the seeded project really is failing ({verdict.state})")


async def turn(nova, message: str) -> tuple[str, str]:
    """One real /chat turn: (what the answer step was given, what was said)."""
    before = len(nova.llm.prompts)
    r = await nova.http.post("/chat", json={"message": message})
    answers = [p for p in nova.llm.prompts[before:]
               if "You are Nova" in p and "agent brain for Nova" not in p]
    said = (r.json().get("assistant") or "") if r.status_code == 200 else ""
    return (answers[-1] if answers else ""), said


def completion_block(prompt: str) -> str:
    marker = "The completion state of the work"
    return prompt.split(marker, 1)[1] if marker in prompt else ""


async def test_the_record_reaches_the_answer_and_corrects_it() -> None:
    check.section("end to end: the misses carry the failing record")
    async with boot(default_reply=AGREEING) as nova:
        await seed_failing(nova)

        missing = []
        for q in MISSES:
            prompt, _ = await turn(nova, q)
            block = completion_block(prompt)
            if FAILING not in block or ERROR not in block:
                missing.append(q)
        check(not missing, f"all {len(MISSES)} misses put the failing state and "
                           f"its error in front of the model ({missing or 'none'})")

        check.section("end to end: a reply claiming completion is corrected")
        prompt, said = await turn(nova, "Did the build finish?")
        check(said.startswith(CORRECTION),
              f"the agreeing reply is corrected first ({said[:90]!r})")
        check("calculator" in said and "failing" in said,
              f"naming the project and its state ({said[:90]!r})")
        check(AGREEING in said,
              "and nothing the model wrote is removed")

        check.section("end to end: near misses get neither the record nor a correction")
        for q in ("Did the laundry finish?", "They finished their homework.",
                  "Everything worked out with the dentist.", "Is it working?",
                  "The Windows update finally finished."):
            prompt, said = await turn(nova, q)
            check(not completion_block(prompt),
                  f"{q!r} carries no completion record")
            check(CORRECTION not in said,
                  f"{q!r} is answered without a correction ({said[:60]!r})")


async def main() -> None:
    test_the_eight_misses_now_get_the_record()
    test_the_work_by_its_own_name()
    test_near_misses_stay_ordinary()
    test_the_near_misses_are_load_bearing()
    await test_the_record_reaches_the_answer_and_corrects_it()
    check.finish()


if __name__ == "__main__":
    run(main)
