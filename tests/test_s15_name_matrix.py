"""Stage 15 — the project-name matching matrix the review asked for.

Ten shapes, each a way a project name can appear or appear to appear:

    exact slug                  "is flappy-bird frozen?"
    spaced slug                 "how is flappy bird doing?"
    compact slug                "is flappybird ok?"
    substring false positive    "Is it done?"          (project `one`)
    plural form                 "the notepads are cheap"
    longer word containing it   "unflappybirds everywhere"
    two similar names           `cat` vs `cat-tracker`
    name in unrelated prose     "the therapist called"
    question vs command         "is cat done?" / "delete cat"
    A named while B is current  named beats the pointer, always

The last one is the one that matters most: a named project OUTRANKS the current
one at every call site, so a false match does not add noise -- it redirects the
turn. That is why the substring cases are not cosmetic.

Run:  venv\\Scripts\\python.exe tests\\test_s15_name_matrix.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, boot, run  # noqa: E402

from core.completion import FAILED, PASSED  # noqa: E402

check = Checks()

COMPLETION_HEADER = "The completion state of the work"


def make(nova, name: str) -> Path:
    p = nova.projects_dir / name
    p.mkdir(parents=True, exist_ok=True)
    (p / "main.py").write_text(f"# {name}\n", encoding="utf-8")
    nova.runtime._project_builder._write_project_md(
        name, brief=f"{name} brief", status="building")
    return p


async def contract(nova, slug: str, verdict: str) -> None:
    svc = nova.runtime.completion
    rev = await svc.record_request(slug=slug,
                                   request_text="a tool that adds numbers")
    ids = await svc.set_criteria(slug=slug, revision=rev, criteria=[
        {"text": "adds numbers", "origin_quote": "adds numbers",
         "verify_kind": "machine"}])
    await svc.seal_contract(slug=slug, revision=rev)
    ctx = await svc.begin_check(slug=slug, criterion_id=ids[0])
    await svc.record_verdict(context=ctx, verdict=verdict,
                             error="boom" if verdict == FAILED else "")


async def test_the_ten_shapes():
    check.section("§15 ten ways a project name appears, or seems to")
    async with boot(default_reply="Sure.") as nova:
        pb = nova.runtime._project_builder
        for n in ("flappy-bird", "one", "note-pad", "cat", "cat-tracker",
                  "api"):
            make(nova, n)

        cases = [
            # (text, expected slug or None, what shape this is)
            ("is flappy-bird frozen?", "flappy-bird", "exact slug"),
            ("how is flappy bird doing?", "flappy-bird", "spaced slug"),
            ("is flappybird ok?", "flappy-bird", "compact slug"),
            ("Is it done?", None, "substring false positive"),
            ("the notepads are cheap", None, "plural form"),
            ("unflappybirds everywhere", None, "longer word"),
            ("the therapist called", None, "unrelated prose"),
            ("what a catastrophe", None, "unrelated prose"),
            ("is cat done?", "cat", "question naming a project"),
            ("delete cat", "cat", "command naming a project"),
            ("how is cat-tracker doing?", "cat-tracker", "two similar names"),
            ("open the cat tracker", "cat-tracker", "two similar, spaced"),
            ("check the api project", "api", "short name, real mention"),
            ("the application is slow", None, "short name inside a word"),
        ]
        for text, want, shape in cases:
            got = pb.known_slug_in_text(text)
            check(got == want,
                  f"{shape:<26} {text!r} -> {want!r} (got {got!r})")


async def test_the_longer_of_two_similar_names_wins():
    """`cat` and `cat-tracker` both appear in "how is cat-tracker doing?".

    The resolver returns the longest exact match, which is the only defensible
    answer: the shorter name is a prefix of the longer one, and a person who
    typed the longer one meant it.
    """
    check.section("two similarly named projects")
    async with boot(default_reply="Sure.") as nova:
        pb = nova.runtime._project_builder
        for n in ("cat", "cat-tracker"):
            make(nova, n)

        check(pb.known_slug_in_text("how is cat-tracker doing?") == "cat-tracker",
              "the longer name wins when both match")
        check(pb.known_slug_in_text("how is cat doing?") == "cat",
              "and the shorter one is still reachable on its own")
        # `cat` must not be found inside the hyphenated longer name when the
        # longer project does not exist as a separate word run.
        check(pb.known_slug_in_text("cattracker is odd") == "cat-tracker",
              "the compact form of the longer name still resolves")


async def test_a_named_project_beats_the_pointer_through_the_turn():
    """The consequence, not the resolver: named outranks current, end to end."""
    check.section("I4 project A named while project B is current")
    async with boot(default_reply="Sure.") as nova:
        make(nova, "alpha")
        make(nova, "bravo")
        await contract(nova, "alpha", PASSED)
        await contract(nova, "bravo", FAILED)
        await nova.memory.add_fact(entity="projects", attribute="last_active",
                                   value="bravo", confidence=0.99)

        seen: list[str] = []

        def capture(prompt: str) -> str:
            seen.append(prompt)
            return "Sure."

        nova.llm.when(lambda _p: True, capture, label="capture")
        nova.llm.rules.insert(0, nova.llm.rules.pop())

        await nova.brain.chat("Is alpha done?", conversation_id=str(uuid4()))
        ground = next((p for p in seen if COMPLETION_HEADER in p), "")
        check("project 'alpha'" in ground,
              "the NAMED project is what the record describes")
        check("project 'bravo'" not in ground,
              "not the one that merely happens to be current")

        # And the reverse: no name, so the pointer decides.
        seen.clear()
        await nova.brain.chat("Is it done?", conversation_id=str(uuid4()))
        ground2 = next((p for p in seen if COMPLETION_HEADER in p), "")
        check("project 'bravo'" in ground2,
              "with no name, the current project decides")
        check("project 'alpha'" not in ground2,
              "and the other one stays out of it")


async def test_a_mention_inside_another_mention_loses():
    """The span rule, and the case it must not break.

    With `cat` and `cat-tracker` both on disk, "open the cat tracker" matched
    `cat` as an exact token and `cat-tracker` as a spaced phrase, and tier
    priority picked the exact one. Since a named project outranks the current
    one everywhere, "DELETE the cat tracker" resolved to `cat` -- a destructive
    command pointed at the wrong project.

    Length alone cannot fix it: "flappy-bird is still frozen" must resolve to
    `flappy-bird`, not to a project called `still-frozen` whose spaced form
    also appears and which is the longer name. The difference is that `cat`
    sits INSIDE the mention of `cat-tracker`, while `still frozen` sits
    elsewhere in the sentence.
    """
    check.section("a name inside another name's mention loses to it")
    async with boot(default_reply="Sure.") as nova:
        pb = nova.runtime._project_builder
        for n in ("cat", "cat-tracker", "flappy-bird", "still-frozen"):
            make(nova, n)

        for text in ("open the cat tracker", "delete the cat tracker",
                     "how is the cat tracker going?",
                     "the cat tracker crashed"):
            got = pb.known_slug_in_text(text)
            check(got == "cat-tracker",
                  f"{text!r} -> cat-tracker (got {got!r})")

        check(pb.known_slug_in_text("delete cat") == "cat",
              "and the shorter name still resolves on its own")

        # The case tier priority was built for: two matches that do NOT
        # overlap, where the explicitly named project must win over prose that
        # happens to spell another project's name.
        got = pb.known_slug_in_text("flappy-bird is still frozen and I am annoyed")
        check(got == "flappy-bird",
              f"an exact name beats unrelated prose elsewhere in the sentence "
              f"(got {got!r})")


async def main() -> None:
    await test_the_ten_shapes()
    await test_the_longer_of_two_similar_names_wins()
    await test_a_mention_inside_another_mention_loses()
    await test_a_named_project_beats_the_pointer_through_the_turn()
    check.finish()


if __name__ == "__main__":
    run(main)
