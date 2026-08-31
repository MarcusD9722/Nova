"""Stage 15 — a project name inside a longer word is not a project mention.

`known_slug_in_text` matched a slug as a bare substring. With a project called
`one` on disk:

    "Is it done?"              -> one     (d-ONE)
    "what a catastrophe"       -> cat     (CAT-astrophe)
    "the application is slow"  -> cat     (appli-CAT-ion)
    "that's a scone"           -> one     (sc-ONE)

Found while investigating why a Stage 15 test saw completion grounding for a
project it had deliberately made unreachable. The suspicion was a stale pointer;
the cause was the word "done".

WHY IT MATTERS BEYOND ONE PROMPT. This resolver is the front of seven call
sites in `core/runtime.py` alone -- project selection, resume, status, the
mutation path, and the completion record attached to an answer -- and a named
project deliberately OUTRANKS the current one everywhere. So a false name match
does not merely add noise: it redirects the turn to a project the person never
mentioned, and it does so more often the shorter and more ordinary the project
name is. `one`, `cat`, `api`, `app`, `dev`, `web`, `bot` are all names a person
would plausibly use.

  I3   project identity persists across topic changes
  I4   explicit project names override current-project context
  I40  identical concepts use consistent identity keys across subsystems

Run:  venv\\Scripts\\python.exe tests\\test_s15_project_identity_tokens.py
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


async def test_a_slug_must_be_a_whole_token():
    check.section("I40 a project name inside a word is not a mention")
    async with boot(default_reply="Sure.") as nova:
        pb = nova.runtime._project_builder
        for n in ("one", "cat", "api", "flappy-bird"):
            make(nova, n)

        # Ordinary English that happens to contain a project name.
        for text in ("Is it done?", "Are we done?", "I'm done for today",
                     "what a catastrophe", "the application is slow",
                     "that's a scone", "the therapist called",
                     "capital of Canada"):
            got = pb.known_slug_in_text(text)
            check(got is None,
                  f"{text!r} names no project (got {got!r})")

        # And the mentions that ARE mentions still resolve.
        for text, want in (("Is one done?", "one"),
                           ("how is cat going?", "cat"),
                           ("check the api project", "api"),
                           ("is flappy-bird frozen?", "flappy-bird"),
                           ("how is flappy bird doing?", "flappy-bird")):
            got = pb.known_slug_in_text(text)
            check(got == want,
                  f"{text!r} still resolves to {want!r} (got {got!r})")


async def test_an_ordinary_question_does_not_redirect_the_turn():
    """The consequence, through the production path rather than the resolver.

    A named project outranks the current one by design. So a false name match
    does not add noise -- it changes which project the answer is about.
    """
    check.section("I4 'is it done?' is about the current project, not 'one'")
    async with boot(default_reply="Sure.") as nova:
        svc = nova.runtime.completion
        make(nova, "one")
        make(nova, "current")

        async def contract(slug: str, verdict: str) -> None:
            rev = await svc.record_request(slug=slug,
                                           request_text="a tool that adds numbers")
            ids = await svc.set_criteria(slug=slug, revision=rev, criteria=[
                {"text": "adds numbers", "origin_quote": "adds numbers",
                 "verify_kind": "machine"}])
            await svc.seal_contract(slug=slug, revision=rev)
            ctx = await svc.begin_check(slug=slug, criterion_id=ids[0])
            await svc.record_verdict(context=ctx, verdict=verdict,
                                     error="boom" if verdict == FAILED else "")

        await contract("one", FAILED)
        await contract("current", PASSED)
        await nova.memory.add_fact(entity="projects", attribute="last_active",
                                   value="current", confidence=0.99)

        seen: list[str] = []

        def capture(prompt: str) -> str:
            seen.append(prompt)
            return "Sure."

        nova.llm.when(lambda _p: True, capture, label="capture")
        nova.llm.rules.insert(0, nova.llm.rules.pop())

        await nova.brain.chat("Is it done?", conversation_id=str(uuid4()))
        ground = next((p for p in seen if COMPLETION_HEADER in p), "")

        check(bool(ground), "the answer prompt carries a completion record")
        check("project 'current'" in ground,
              "about the project actually being worked on")
        check("project 'one'" not in ground,
              f"and NOT about the project whose name hides in the word 'done' "
              f"({'leaked' if chr(39) + 'one' + chr(39) in ground else 'clean'})")


async def main() -> None:
    await test_a_slug_must_be_a_whole_token()
    await test_an_ordinary_question_does_not_redirect_the_turn()
    check.finish()


if __name__ == "__main__":
    run(main)
