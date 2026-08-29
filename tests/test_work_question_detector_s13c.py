"""Which turns get the durable record attached, and which do not (§12).

`asks_about_work` decides one thing: whether the answer step is handed
authoritative state. After a restart that decision is the whole ballgame,
because the transcript begins empty — a turn that should have had the record
and did not is answered from nothing at all.

WHY THIS SUITE EXISTS SEPARATELY. The end-to-end suites drive real `/chat`
through real processes, which is the right way to prove the record ARRIVES —
but each turn costs a process, so they can only afford a handful of phrasings.
The breadth was checked by hand when the detector was widened and then pinned
nowhere, which means a later edit could narrow it back to the two sentences the
E2E suites happen to use and leave every test green. That is the exact shape of
"my tests pass while proving nothing", so the breadth is pinned here, where a
phrasing costs a function call instead of an interpreter.

Both directions matter equally. Attaching the record to an unrelated turn is
prompt bloat; failing to attach it to a real one is a confident wrong answer.

Run:  venv\\Scripts\\python.exe tests\\test_work_question_detector_s13c.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, run  # noqa: E402

from core.intent import asks_about_work  # noqa: E402

check = Checks()

#: QUESTIONS about the work (Stage 13B's original shapes).
QUESTIONS = [
    "What happened?",
    "What went wrong?",
    "What failed?",
    "What broke?",
    "What is still pending?",
    "What's outstanding?",
    "What's left?",
    "What was cancelled?",
    "What can resume?",
    "Can we resume?",
    "What should happen next?",
    "What's next?",
    "Where are we?",
    "What are you working on?",
    "What's the status?",
    "How's it going with the pause menu?",
    "Did it all work?",
    "Did that finish?",
]

#: CLAIMS about the work (S13C-1). The dangerous form: a question invites Nova
#: to look something up, a premise invites her to agree — and with no state in
#: front of it, agreeing is what a model does.
CLAIMS = [
    "So everything finished successfully, right?",
    "Everything worked, right?",
    "That all went through, yes?",
    "It all worked out then?",
    "The tasks are done, right?",
    "Everything completed?",
    "So that all succeeded?",
    "They all finished?",
    "Those all went through?",
    "The work is done then?",
    "So it finished?",
    "That worked, right?",
    "The steps completed?",
    "Everything is sorted?",
]

#: "Is anything still running?" (S13C-4) — the first thing a person asks on
#: coming back to a machine that restarted.
STILL_RUNNING = [
    "Is anything still running?",
    "Is anything running?",
    "Anything still running?",
    "Are the tasks still running?",
    "Is it still running?",
    "Are those still going?",
    "Is anything in progress?",
    "Is the build still underway?",
    "Are you still working on the pause menu?",
    "Are you working on anything?",
    "Are you working on it?",
    "Is nova still working on the sprite sheet?",
    "What's going on with my work?",
    "What's going on with the project?",
]

#: Ordinary conversation. Each of these is here because it is a NEAR MISS for
#: one of the shapes above, not because it is obviously unrelated — a negative
#: list of unrelated sentences would pass against almost any regex.
ORDINARY = [
    # "still running", but about a tap
    ("Is the tap still running?", "a subject that is not the work"),
    ("Is the water still running?", "same"),
    ("Is anyone else running the marathon?", "'running' as an activity"),
    # "working on", but about a diary
    ("Are you working on Sunday?", "a time, not a thing"),
    ("Are you working on the weekend?", "same"),
    ("Are you working on holidays?", "same"),
    ("Are you working on tomorrow?", "same"),
    # "going on with", but not about work
    ("What's going on with your day?", "no work noun"),
    # outcome words with no scope, or scope with no outcome
    ("Everything is fine, thanks.", "a scope word, but no outcome claim"),
    ("Everything is lovely.", "same"),
    ("I finished my coffee.", "an outcome word about something else"),
    ("I worked out at the gym.", "same"),
    ("Does that work for you?", "'work' as agreement — the reason a bare "
                                "'work' alternative was rejected"),
    ("That works for me.", "same"),
    # general chat
    ("Is anything good on TV?", "unrelated"),
    ("How are you going?", "unrelated"),
    ("Are you going out later?", "unrelated"),
    # A deliberate boundary: "all set" is an outcome phrase, but with nothing
    # scoping it this is as likely to mean "are you ready?" as "did the work
    # finish?". Recorded here so the boundary is a decision, not an accident.
    ("All set then?", "an outcome word with nothing scoping it"),
]


async def test_questions_about_the_work_get_the_record():
    check.section("§12 questions about the work")
    missed = [q for q in QUESTIONS if not asks_about_work(q)]
    check(not missed, f"all {len(QUESTIONS)} question shapes attach the record "
                      f"({missed or 'none missed'})")


async def test_claims_about_the_work_get_the_record():
    check.section("§12 claims about the work (S13C-1)")
    missed = [q for q in CLAIMS if not asks_about_work(q)]
    check(not missed, f"all {len(CLAIMS)} premise shapes attach the record "
                      f"({missed or 'none missed'})")


async def test_is_anything_still_running_gets_the_record():
    check.section("§12 'is anything still running?' (S13C-4)")
    missed = [q for q in STILL_RUNNING if not asks_about_work(q)]
    check(not missed, f"all {len(STILL_RUNNING)} shapes attach the record "
                      f"({missed or 'none missed'})")


async def test_ordinary_talk_is_left_alone():
    check.section("§12 near misses that must stay ordinary")
    caught = [(q, why) for q, why in ORDINARY if asks_about_work(q)]
    check(not caught,
          f"all {len(ORDINARY)} near misses stay ordinary conversation "
          f"({[q for q, _ in caught] or 'none caught'})")


async def test_the_negatives_are_actually_near_misses():
    """A negative list of unrelated sentences would pass against almost any
    regex, including a broken one. Each negative must share vocabulary with a
    shape that IS recognised, or it is not evidence of anything."""
    check.section("§12 the negative list is load-bearing")
    vocabulary = {"running", "working on", "going on with", "everything",
                  "finished", "work", "all set", "completed", "going"}
    weak = [q for q, _ in ORDINARY
            if not any(v in q.lower() for v in vocabulary)]
    check(len(weak) <= 3,
          f"the negatives share vocabulary with the recognised shapes "
          f"({len(ORDINARY) - len(weak)} of {len(ORDINARY)} are near misses; "
          f"unrelated: {weak})")


async def main() -> None:
    await test_questions_about_the_work_get_the_record()
    await test_claims_about_the_work_get_the_record()
    await test_is_anything_still_running_gets_the_record()
    await test_ordinary_talk_is_left_alone()
    await test_the_negatives_are_actually_near_misses()
    check.finish()


if __name__ == "__main__":
    run(main)
