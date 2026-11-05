"""Audit 2026-11-02, H15 + H16: planning and naming a project.

H15 — `_python_stack_note` told the planner "ONLY the standard library ...
third-party packages are NOT installed" whatever was installed. pytest IS
installed, and `_run_test_file` now REQUIRES pytest-style tests, so the planner
was being told the opposite of the truth; with thinking on it looped on the
contradiction (138 mentions of pytest in 25k characters, then finish=length on
every attempt). Fixed by probing what can actually be imported, and by letting
the planner's attempts escalate to thinking-off instead of repeating.

H16 — three separate defects in naming:
  1. "a script called word-counter that counts words" asked "what should I call
     the project?" when the name was in the sentence.
  2. "called Serpent as a single HTML file" became
     `serpent-as-a-single-html-file`.
  3. Answering the question did NOTHING. There was no pending-name state, so
     the reply was ordinary chat and the model improvised "I'm spinning up the
     word-counter project now" while nothing started (that improvisation is
     C4).

(3) is the one that needs a real conversation, so it is driven through the
harness rather than asserted against a regex.
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

from core.project_builder import (  # noqa: E402
    NEEDS_NAME, ProjectBuilder, _installed_packages, _python_stack_note,
    plausible_project_name, slugify,
)
from harness import Checks, boot, run  # noqa: E402

check = Checks()


def _guest(status="known", pid="p-alice", name="Alice", role="guest"):
    """A non-owner speaker, built the way the speaker suites build one."""
    from core.speaker.matcher import SpeakerMatch
    from core.turn_identity import TurnIdentity

    class _P:
        pass
    prof = _P()
    prof.role = role
    return TurnIdentity.from_match(
        SpeakerMatch(status=status, profile_id=pid, display_name=name,
                     attempted=True),
        profile=(prof if pid else None))


def _name(text: str):
    r = ProjectBuilder.extract_start_request(text)
    return r[0] if r else None


# ── H15: the planner is told the truth ───────────────────────────────────────

def test_the_stack_note_matches_this_machine() -> None:
    check.section("H15: the stack note states what is actually importable")
    note = _python_stack_note()
    installed = _installed_packages()

    import importlib.util
    for pkg in ("pygame", "numpy", "flask", "pytest"):
        present = importlib.util.find_spec(pkg) is not None
        claimed_absent = f"{pkg} is NOT installed" in note
        named = pkg in note
        if present:
            check(named and not claimed_absent,
                  f"{pkg} is installed and the note does not deny it")
        else:
            check(not named or claimed_absent,
                  f"{pkg} is absent and the note does not offer it")

    # The contradiction itself: never claim a bare standard library while
    # naming packages that are installed.
    check(not ("ONLY the standard library" in note and installed),
          "no 'ONLY the standard library' claim while packages are installed")
    # pytest drives the test runner, so the note must agree with the runner.
    has_pytest = importlib.util.find_spec("pytest") is not None
    check(("pytest-style" in note) == has_pytest,
          f"the note's test style matches the installed runner ({has_pytest})")
    check("do not import anything not named here" in note,
          "and the planner is told not to invent imports")


async def test_planner_attempts_escalate_rather_than_repeat() -> None:
    """A think that eats the budget must not be retried identically."""
    check.section("H15: the planner's attempts escalate to thinking-off")
    import inspect

    from core.project_builder import ProjectBuilder as PB
    src = inspect.getsource(PB._llm_json)
    check("thinking=thinking" in src,
          "the thinking flag is per-attempt, not hard-coded True")
    check("(max_tokens, True)" in src and "(max_tokens, False)" in src,
          "the attempts include a thinking-off attempt")
    check("planner_json_failed" in src,
          "and a failed generation is logged instead of silently retried")

    # The escalation itself, driven: a fake runtime that only produces JSON
    # once thinking is off is the model that looped.
    class _Sem:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return False

    class _Runtime:
        def __init__(self):
            self.calls = []

        async def chat(self, messages, max_tokens=512, temperature=0.2,
                       stop=None, thinking=False):
            self.calls.append({"max_tokens": max_tokens, "thinking": thinking})
            if thinking:
                return "<think>" + ("pytest " * 400)  # never closes, no JSON
            return '{"summary": "ok", "language": "python", ' \
                   '"files": [{"path": "main.py", "purpose": "x"}], "run": "python main.py"}'

    runtime = _Runtime()
    builder = PB.__new__(PB)
    builder._handle = lambda role: (runtime, _Sem())  # type: ignore[attr-defined]
    obj = await PB._llm_json(builder, "plan something", max_tokens=1400)

    check(obj is not None and obj.get("language") == "python",
          f"a model that only answers without thinking still gets planned ({obj})")
    check([c["thinking"] for c in runtime.calls] == [True, True, False],
          f"the attempts escalated: {[c['thinking'] for c in runtime.calls]}")
    check(runtime.calls[1]["max_tokens"] > runtime.calls[0]["max_tokens"],
          f"and the budget was raised on the way "
          f"({runtime.calls[0]['max_tokens']} -> {runtime.calls[1]['max_tokens']})")


# ── H16 (1) and (2): what a name is ──────────────────────────────────────────

def test_a_marked_name_is_not_a_question() -> None:
    check.section("H16: 'called X that <does something>' is a name")
    for text, want in (
        # The finding's own sentence.
        ("Build a Python script called word-counter that counts words in a text file",
         "word-counter"),
        ("create a project called Serpent that tracks spending", "Serpent"),
        ("Make an app called Budget Buddy which tracks my spending", "Budget Buddy"),
        # Requirement clauses that already worked, still working.
        ("Build a game called Last Stand and I want you to use pygame", "Last Stand"),
        ("Create a tool named QuickCalc", "QuickCalc"),
        ('Make an app called "Rock & Roll Tracker"', "Rock & Roll Tracker"),
    ):
        got = _name(text)
        check(got == want,
              f"{text[:52]!r}… -> {('ASK' if got == NEEDS_NAME else got)!r}"
              + ("" if got == want else f"  EXPECTED {want!r}"))


def test_a_junk_slug_is_never_invented() -> None:
    check.section("H16: 'as a single HTML file' is not part of the name")
    got = _name("Make a game called Serpent as a single HTML file")
    check(got == NEEDS_NAME,
          f"it asks instead of inventing one ({got!r})")
    check(got != "Serpent as a single HTML file",
          "the file format is not part of the title")
    if got not in (None, NEEDS_NAME):
        check(slugify(got) != "serpent-as-a-single-html-file",
              f"and no junk slug ({slugify(got)})")

    # Prepositions stay genuinely ambiguous: "Man with a Plan" and "Python for
    # Beginners" are real titles, so these must keep asking. (Pinned here
    # because H16's suggested fix — treat 'called X' as authoritative — would
    # have named these after their first word.)
    for text in ("create a project called Man with a Plan",
                 "create a project called Python for Beginners",
                 "create a project called Serpent with a dark theme",
                 "Build an app called Cobra in python"):
        got = _name(text)
        check(got == NEEDS_NAME, f"{text[24:56]!r} still asks ({got!r})")
    # …and quoting resolves it exactly.
    check(_name('create a project called "Man with a Plan"') == "Man with a Plan",
          "a quoted title is honoured whole")


def test_what_counts_as_an_answer() -> None:
    check.section("H16: which replies are a project name")
    for t in ("word-counter", "Serpent", "Budget Buddy", "QuickCalc",
              "My Personal Finance Dashboard 2026", "The Last Stand",
              "to-do-list", "Flappy Bird"):
        check(plausible_project_name(t), f"{t!r} is a name")
    for t in ("sounds good", "go ahead", "yes", "ok", "thanks", "that one",
              "whatever you think", "up to you", "you decide", "surprise me",
              "what do you suggest?", "and then make it blue", "the game",
              "project", "with a dark theme", "that tracks spending",
              "a really long name that goes on and on forever"):
        check(not plausible_project_name(t), f"{t!r} is NOT a name")


# ── H16 (3): the answer has to start the build ───────────────────────────────

async def test_answering_the_question_starts_the_build() -> None:
    check.section("H16: answering 'what should I call it?' starts the build")
    async with boot() as nova:
        conv = uuid4().hex
        r1 = await nova.http.post("/chat", json={
            "message": "Build me a tool that counts the words in a text file",
            "conversation_id": conv})
        said = (r1.json().get("assistant") or "")
        check("call the project" in said.lower() or "call it" in said.lower(),
              f"she asks what to call it ({said[:70]!r})")

        before = {p.name for p in (nova.root / "projects").iterdir()
                  if p.is_dir()} if (nova.root / "projects").exists() else set()

        r2 = await nova.http.post("/chat", json={"message": "word-counter",
                                                 "conversation_id": conv})
        reply = (r2.json().get("assistant") or "")
        check("word-counter" in reply,
              f"the one-word answer is taken as the name ({reply[:80]!r})")
        check("starting project" in reply.lower() or "building" in reply.lower(),
              "and the reply says the build is starting")

        after = {p.name for p in (nova.root / "projects").iterdir() if p.is_dir()}
        check("word-counter" in after and "word-counter" not in before,
              f"a word-counter project directory now exists ({sorted(after - before)})")

        md = nova.root / "projects" / "word-counter" / "PROJECT.md"
        check(md.exists(), "with a PROJECT.md")
        if md.exists():
            body = md.read_text(encoding="utf-8", errors="replace")
            check("counts the words" in body or "word" in body.lower(),
                  "built from the ORIGINAL brief, not from the one word")


async def test_a_name_is_not_taken_from_an_unrelated_reply() -> None:
    """The pending question must not name a project after the next sentence."""
    check.section("H16: an unrelated reply drops the pending build")
    async with boot() as nova:
        conv = uuid4().hex
        await nova.http.post("/chat", json={
            "message": "Build me a tool that counts the words in a text file",
            "conversation_id": conv})

        r = await nova.http.post("/chat", json={
            "message": "Actually, what's the capital of France?",
            "conversation_id": conv})
        reply = (r.json().get("assistant") or "")
        dirs = {p.name for p in (nova.root / "projects").iterdir()
                if p.is_dir()} if (nova.root / "projects").exists() else set()
        check(not dirs,
              f"no project was created from an unrelated question ({sorted(dirs)})")
        check("starting project" not in reply.lower(),
              f"and she did not claim to start one ({reply[:70]!r})")

        # The question has gone; answering it later is not a build.
        r2 = await nova.http.post("/chat", json={"message": "word-counter",
                                                 "conversation_id": conv})
        dirs2 = {p.name for p in (nova.root / "projects").iterdir()
                 if p.is_dir()} if (nova.root / "projects").exists() else set()
        check(not dirs2,
              f"a stale answer does not start it either ({sorted(dirs2)})")


async def test_declining_the_name_cancels_the_build() -> None:
    check.section("H16: 'never mind' withdraws the build")
    async with boot() as nova:
        conv = uuid4().hex
        await nova.http.post("/chat", json={
            "message": "Build me a tool that counts the words in a text file",
            "conversation_id": conv})
        r = await nova.http.post("/chat", json={"message": "never mind",
                                                "conversation_id": conv})
        reply = (r.json().get("assistant") or "")
        dirs = {p.name for p in (nova.root / "projects").iterdir()
                if p.is_dir()} if (nova.root / "projects").exists() else set()
        check(not dirs, f"nothing was created ({sorted(dirs)})")
        check("dropped" in reply.lower() or "no problem" in reply.lower(),
              f"and she says so ({reply[:60]!r})")


async def test_only_marcus_can_start_a_project() -> None:
    """Found while fixing H16: starting a project had no identity check.

    Every other project write path was scoped to the owner in P5.1d and again
    for H10. This one wrote a directory, a PROJECT.md and memory facts for
    whoever was speaking.
    """
    check.section("a guest voice cannot start a project")
    async with boot() as nova:
        from core.turn_identity import TurnIdentity, active_turn

        for label, who in (("a known guest", _guest()),
                           ("an unrecognised voice",
                            _guest(status="unknown", pid=None, name=None))):
            with active_turn(who):
                reply = await nova.runtime._project_prepass(
                    "Build a game called Serpent", conversation_id=uuid4().hex)
            dirs = ({p.name for p in (nova.root / "projects").iterdir() if p.is_dir()}
                    if (nova.root / "projects").exists() else set())
            check("serpent" not in dirs,
                  f"{label}: no project directory was created ({sorted(dirs)})")
            check(reply is not None and "marcus" in (reply or "").lower(),
                  f"{label}: and she says whose projects these are "
                  f"({(reply or '')[:60]!r})")

        # …and the pending-name answer is scoped the same way.
        conv = uuid4().hex
        with active_turn(TurnIdentity.typed()):
            await nova.runtime._project_prepass(
                "Build me a tool that counts words", conversation_id=conv)
        with active_turn(_guest()):
            reply = await nova.runtime._project_prepass("word-counter",
                                                        conversation_id=conv)
        dirs = ({p.name for p in (nova.root / "projects").iterdir() if p.is_dir()}
                if (nova.root / "projects").exists() else set())
        check("word-counter" not in dirs,
              f"a guest answering the name question starts nothing ({sorted(dirs)})")
        check(reply is not None and "marcus" in (reply or "").lower(),
              f"and is told why ({(reply or '')[:60]!r})")

        # The owner, same message, still works — the point is a scope, not a
        # disabled feature.
        with active_turn(TurnIdentity.typed()):
            reply = await nova.runtime._project_prepass(
                "Build a game called Serpent", conversation_id=uuid4().hex)
        check(reply is not None and "serpent" in (reply or "").lower(),
              f"Marcus still starts it ({(reply or '')[:60]!r})")


async def main() -> None:
    test_the_stack_note_matches_this_machine()
    await test_planner_attempts_escalate_rather_than_repeat()
    test_a_marked_name_is_not_a_question()
    test_a_junk_slug_is_never_invented()
    test_what_counts_as_an_answer()
    await test_answering_the_question_starts_the_build()
    await test_a_name_is_not_taken_from_an_unrelated_reply()
    await test_declining_the_name_cancels_the_build()
    await test_only_marcus_can_start_a_project()
    check.finish()


if __name__ == "__main__":
    run(main)
