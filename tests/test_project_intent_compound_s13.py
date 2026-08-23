"""An explicit instruction is not downgraded to conversation (Stage 13A, I2).

THE DEFECT, measured on c5d7a88 through the real turn path:

    "Open the flappy-bird project and add a pause button."
        -> MutationVerdict(allowed=False, reason='no affirmative instruction')

`_IMPERATIVE_RE` only ever inspected the OPENING verb. `open` does not change
anything, so it is deliberately not an action verb — and the real instruction,
`add a pause button`, sat in the second clause where nothing looked. Nova then
discussed a change the user had plainly told her to make.

Every earlier round of this gate was about the opposite failure: casual talk
mutating a project. Nothing had ever checked that a plain instruction still
gets through, so this direction was unmeasured rather than broken by a
regression.

WHY THE FIX IS NARROW. The action verb has to appear directly after `and`/
`then` — in an imperative clause of its own. Merely requiring one SOMEWHERE in
the sentence would authorise

    "Open the project and tell me what you would change."

which contains "change" and is a question about intent. That sentence, and the
five other near misses below, are the reason this is a shaped pattern rather
than a keyword search.

Run:  venv\\Scripts\\python.exe tests\\test_project_intent_compound_s13.py
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

from core.project_intent import authorize_project_mutation  # noqa: E402

check = Checks()


def _allowed(text: str) -> bool:
    return bool(authorize_project_mutation(text, complaint=False).allowed)


async def test_a_compound_imperative_authorises():
    check.section("S13: the instruction in the SECOND clause still counts")

    for text in (
        "Open the flappy-bird project and add a pause button.",
        "Open flappy-bird and fix the collision bug.",
        "Pull up the calculator and add a memory button.",
        "Look at the menu and make the buttons bigger.",
        "please open the project and then update the readme",
        "Go to the calculator project and remove the dead code.",
        "Check the game and improve the collision handling.",
    ):
        check(_allowed(text), f"authorised: {text!r}")


async def test_the_near_misses_stay_refused():
    """The reason the fix is a shape and not a keyword search.

    Each of these opens something AND contains an action verb somewhere. None
    of them instructs a change.
    """
    check.section("S13: opening a project is not authority to change it")

    for text in (
        "Open the project and tell me what you would change.",
        "Open the project and let me know what you think.",
        "Look at the menu and I wish it were nicer.",
        "Open flappy-bird and see whether the pipes feel unfair.",
        "Read the code and explain how the scoring works.",
        "Should I open the project and add a button?",
        "Don't open the project and add anything.",
        "I opened the project and added a button yesterday.",
    ):
        check(not _allowed(text), f"refused: {text!r}")


async def test_the_original_refusals_are_untouched():
    """The closed defect class. These are what the gate exists for."""
    check.section("S13: casual conversation still authorises nothing")

    for text in (
        "I've been improving Nova.",
        "I need to fix my game sometime.",
        "You should see the project I made.",
        "I wish the menu looked better.",
        "I was thinking about deleting the old project.",
        "Deleting the old project was probably a bad idea.",
        "Don't change anything.",
        "What would you change?",
        "Tell me how you'd improve it.",
        "Actually don't.",
        "I think my Flappy Bird project is finally starting to look decent.",
        "I don't really like how unforgiving the pipes feel.",
    ):
        check(not _allowed(text), f"still refused: {text!r}")


async def test_the_plain_forms_are_untouched():
    check.section("S13: the shapes that already worked still work")

    for text in (
        "Okay, make that change.",
        "Go ahead and make that change.",
        "Please add a pause button to flappy-bird.",
        "Add a pause button.",
        "Make the vertical opening 15% larger.",
        "Update the pipe spacing now.",
        "Can you fix the collision bug?".replace("?", ""),
        "Let's add a restart button.",
    ):
        check(_allowed(text), f"still authorised: {text!r}")


async def test_a_gerund_subject_is_not_a_command():
    """The fail-OPEN defect, and the more serious of the two (Stage 13A).

    Measured on c5d7a88:

        "Deleting the old project was probably a bad idea."
            -> MutationVerdict(allowed=True, reason='affirmative: imperative')

    One pattern was answering two different questions. `_ACTION_VERB` is
    stem-plus-`\w*` because "does this sentence talk about changing something"
    is about TOPIC — "improving", "deleted" and "changes" all count. The
    imperative check reused it, so `delet\w*` matched "Deleting" and a
    start-anchored pattern read a gerund SUBJECT as a command. An entire class
    of retrospective remarks authorised a mutation.

    The fix is grammatical, not lexical. An English imperative is always the
    bare stem, so `_IMPERATIVE_VERB` is a CLOSED list of bare forms with no
    `\w*` at all, and gerunds are excluded by construction rather than
    subtracted afterwards. `test_bring_up_is_not_a_gerund` below is the reason
    that distinction matters and is not a stylistic preference.
    """
    check.section("S13: a gerund subject never authorises a mutation")

    for text in (
        "Deleting the old project was probably a bad idea.",
        "Deleting the old project was a mistake.",
        "Removing that feature turned out badly.",
        "Adding the menu was a mistake.",
        "Changing the physics broke it.",
        "Changing that broke it.",
        "Improving the UI took ages.",
        "Building that was harder than expected.",
        "Making it harder was the wrong call.",
        "Refactoring that module is on my list.",
        "Updating the readme is something I keep forgetting.",
        "Fixing the pipes would probably help.",
    ):
        check(not _allowed(text), f"gerund refused: {text!r}")

    # The bare-stem imperatives built from the very same verbs still authorise,
    # so this is a grammatical distinction and not a blanket ban on the verbs.
    for text in ("Delete the old project.", "Add the menu.",
                 "Change the physics.", "Change that.", "Improve the UI.",
                 "Update the readme.", "Fix the pipes."):
        check(_allowed(text), f"bare-stem imperative still authorised: {text!r}")

    # An -ing word elsewhere in the sentence is just a noun. A veto that matched
    # one anywhere would refuse ordinary instructions.
    for text in (
        "Add a loading screen.",
        "Fix the scrolling bug.",
        "Update the rendering code.",
        "Make the falling speed slower.",
        "Improve the starting menu.",
        "Open the project and fix the scrolling glitch.",
    ):
        check(_allowed(text),
              f"an -ing word later in the sentence changes nothing: {text!r}")


async def test_bring_up_is_not_a_gerund():
    """The regression that a LEXICAL gerund rule introduced, pinned forever.

    The first attempt at the fix above vetoed any opening word spelled
    `\w+ing`. That does not identify gerunds; it identifies spelling. `bring`
    ends in "ing", so

        "Bring up the flappy-bird project and add a pause button."

    was refused — and because the veto ran BEFORE the compound-imperative
    check, the new code made one of the module's own supported openers
    unreachable. No amount of special-casing "bring" fixes the shape of that
    mistake, which is why the vocabulary is split by grammar instead.
    """
    check.section("S13: an opener that merely SPELLS like a gerund still works")

    for text in (
        "Bring up the flappy-bird project and add a pause button.",
        "Bring up the project and add a pause button.",
        "Please bring up the project and fix collision.",
        "bring up flappy-bird and update the readme",
    ):
        check(_allowed(text), f"'bring up' authorised: {text!r}")

    # …and it is still only an opener on its own.
    for text in ("Bring up the flappy-bird project.",
                 "Bring up the project and tell me what you'd change."):
        check(not _allowed(text), f"'bring up' alone authorises nothing: {text!r}")


async def test_every_opener_verb_carries_a_compound_imperative():
    """EVERY opener, not a sample.

    `bring up` was broken while `open`, `pull up` and `look at` were fine, and a
    suite that spot-checked three of ten opener verbs stayed green through it.
    So this enumerates the real vocabulary from the module rather than a
    hand-picked subset: a future edit that breaks one opener cannot hide behind
    the others.
    """
    check.section("S13: every opener verb supports a compound imperative")

    openers = [
        "open", "look at", "go to", "pull up", "bring up", "check", "read",
        "review", "inspect", "load",
    ]
    # The list must BE the module's list, or this test drifts into fiction.
    from core.project_intent import _OPENER_VERB
    declared = {o.replace(r"\s+", " ") for o in
                _OPENER_VERB[3:-1].split("|")}
    check(declared == set(openers),
          f"the enumeration matches the module ({sorted(declared)})")

    for opener in openers:
        text = f"{opener.capitalize()} the flappy-bird project and add a pause button."
        check(_allowed(text), f"compound imperative authorised: {text!r}")
        check(not _allowed(f"{opener.capitalize()} the flappy-bird project."),
              f"but the opener alone is not authority: {opener!r}")


async def test_bare_approval_remains_closed_deliberately():
    """A KNOWN GAP, pinned so that closing it is a decision and not an accident.

    "Go ahead." after Nova proposes a change is a real authorisation, and this
    gate refuses it. It is context-free by design and the message alone cannot
    separate that from an idle "go ahead" in conversation, so it fails closed.
    Closing the gap needs a pending-proposal signal from conversation state —
    not a looser pattern here. This test exists so that a future loosening has
    to delete an assertion that says why.
    """
    check.section("S13: bare approval stays refused, and the pin says why")

    for text in ("Go ahead.", "Do that.", "Yes, do it.", "Sure.", "Okay."):
        check(not _allowed(text),
              f"bare approval still fails closed: {text!r}")

    # …while approval that names the action is honoured.
    check(_allowed("Go ahead and apply those improvements."),
          "approval WITH an action verb is authorised")


async def main():
    await test_a_compound_imperative_authorises()
    await test_the_near_misses_stay_refused()
    await test_the_original_refusals_are_untouched()
    await test_the_plain_forms_are_untouched()
    await test_a_gerund_subject_is_not_a_command()
    await test_bring_up_is_not_a_gerund()
    await test_every_opener_verb_carries_a_compound_imperative()
    await test_bare_approval_remains_closed_deliberately()
    check.finish()


if __name__ == "__main__":
    run(main)
