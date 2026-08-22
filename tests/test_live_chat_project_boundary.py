"""Ordinary conversation must not cross the filesystem side-effect boundary.

Every message in the NO-MUTATION corpus below was typed at Nova in a real
conversation. Three of them made her start editing a project:

    "I had worked on your code these last few days trying to improve your
     overall performance and sturdiness."
    "I wasnt trying to have you upgrade anything. I was just making small talk."
    "Stop making false improvements. Im trying to run tests on you."

The third is the worst of the three: it is Marcus telling her to STOP, and it
started another edit.

The mechanism was `_project_prepass`: any word matching IMPROVE_WORDS_RE
(improv*/enhanc*/upgrad*/polish*/refactor*/fix*/extend*) counted as
"continuation intent", and with no project named it fell back to
`last_active()` and called `ProjectBuilder.improve()`. A keyword was therefore
sufficient authority to modify files on disk.

These tests drive the REAL `RuntimeManager._project_prepass` over a REAL
`ProjectBuilder` in a temporary projects directory. Only `_improve` — the
background LLM worker — is stubbed, so `improve()` itself still runs, still
sets last_active and still publishes `project.started` if it is authorised.
What is asserted is therefore the production authorisation decision, not a
regex.

Run:  venv\\Scripts\\python.exe tests\\test_live_chat_project_boundary.py
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, run  # noqa: E402

from core.event_bus import BUS  # noqa: E402
from core.project_builder import ProjectBuilder  # noqa: E402

check = Checks()


def _tmp():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


class _StubLLM:
    """No model in this suite. Reaching it at all is a failure."""

    gpu_status = type("S", (), {"status": "stub"})()

    async def initialize(self):
        return None

    async def generate(self, *a, **k):
        raise AssertionError("the project pre-pass must not call the model")

    async def chat(self, *a, **k):
        raise AssertionError("the project pre-pass must not call the model")


#: The legacy project from the live failure. Its malformed name is a artefact of
#: a project-name parsing defect fixed in #59; the identity contract deliberately
#: preserves such names, so this suite must NOT rename or delete it — the bug
#: under test is that conversation resolves to it at all.
LEGACY = "blue-and-tower-defense-and-i-want-you-to"

#: EXACT strings from the live conversation, plus the phrasings that share their
#: shape. None of these authorises modifying anything.
NO_MUTATION = [
    # ── the three live failures, verbatim ──────────────────────────────────
    "I had worked on your code these last few days trying to improve your overall performance and sturdiness.",
    "I wasnt trying to have you upgrade anything. I was just making small talk.",
    "Stop making false improvements. Im trying to run tests on you.",
    # ── retrospective: a report of past work is not a command ──────────────
    "I improved the project yesterday.",
    "We improved the project yesterday.",
    "I improved your code yesterday.",
    # ── negated / corrective ───────────────────────────────────────────────
    "I wasn't asking you to fix flappy-bird.",
    "I did not ask you to improve the project.",
    "Don't improve anything.",
    "Do not modify that.",
    "Never upgrade that automatically.",
    # ── deliberative / hypothetical ────────────────────────────────────────
    "What improvements could we make to flappy-bird?",
    "Should we refactor flappy-bird?",
    "Tell me how you would improve flappy-bird.",
    "How could this project be improved?",
    "Do you think we should refactor it?",
    "I think improving it might help.",
    # ── Nova herself is the target, not a project ──────────────────────────
    "I want to discuss improving your overall performance.",
    "Your runtime could probably be improved.",
    "I was testing your upgrade behavior.",
    "I'm checking whether you falsely interpret the word improvement.",
    "I was talking about improving your performance.",
    # ── a bare mention is not an instruction ───────────────────────────────
    "flappy-bird is a project I made earlier.",
]

#: These are real instructions and MUST still work.
MUTATION = [
    "Improve flappy-bird's collision handling.",
    "Fix the collision bug in flappy-bird.",
    "Add a restart button to flappy-bird.",
    "Refactor the flappy-bird enemy movement.",
    "Go ahead and apply those improvements.",
    "Implement those three suggestions.",
    "Continue working on flappy-bird.",
]


def _snapshot(root: Path) -> dict[str, str]:
    """Every file under `root`, content-hashed. The side-effect ground truth."""
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.blake2b(
                p.read_bytes(), digest_size=8).hexdigest()
    return out


async def _runtime(td: str):
    """A real RuntimeManager over temporary directories."""
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
                        llm=_StubLLM(), router=router, memory_dir=mem_dir)

    # Two real projects: the legacy one that got edited in the live failure,
    # and an ordinary one to name explicitly.
    for slug, title in ((LEGACY, "Legacy"), ("flappy-bird", "Flappy Bird")):
        d = projects / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "PROJECT.md").write_text(f"# {title}\n\n## Next steps / suggestions\n- [ ] something\n",
                                      encoding="utf-8")
        (d / "main.py").write_text("print('hello')\n", encoding="utf-8")

    # The live conversation's starting condition: the legacy project is the one
    # `last_active()` would return.
    await m.add_fact(entity="projects", attribute="last_active", value=LEGACY,
                     confidence=0.95)
    return rt, m, projects


class _Recorder:
    """Watches the production signals `improve()` emits when it is authorised."""

    def __init__(self) -> None:
        self.started: list[dict] = []
        self.improved: list[tuple[str, str]] = []

    def __enter__(self):
        self._real_worker = ProjectBuilder._improve
        self._real_improve = ProjectBuilder.improve
        rec = self

        async def _no_worker(self, slug, instructions):
            # The background LLM build. Stubbed so the decision is what is
            # measured, not a model call — `improve()` itself is untouched.
            return None

        async def _spy(self, *, slug, instructions):
            rec.improved.append((slug, instructions))
            return await rec._real_improve(self, slug=slug, instructions=instructions)

        # Patch the CLASS, not an instance: the pre-pass reaches its builder
        # through RuntimeManager, and an instance patch would miss it.
        ProjectBuilder._improve = _no_worker
        ProjectBuilder.improve = _spy
        self._q = BUS.subscribe()
        return self

    def drain(self) -> None:
        """Collect the production events published since the last drain."""
        while True:
            try:
                ev = self._q.get_nowait()
            except Exception:
                break
            if getattr(ev, "type", getattr(ev, "kind", "")) == "project.started":
                self.started.append(ev)

    def __exit__(self, *exc):
        ProjectBuilder._improve = self._real_worker
        ProjectBuilder.improve = self._real_improve
        BUS.unsubscribe(self._q)
        return False


async def test_conversation_never_mutates_a_project():
    check.section("Phase 1: ordinary conversation starts no project edit")

    with _tmp() as td:
        rt, m, projects = await _runtime(td)
        before_files = _snapshot(projects)

        with _Recorder() as rec:
            for msg in NO_MUTATION:
                rec.improved.clear()
                rec.started.clear()
                reply = await rt._project_prepass(msg)
                rec.drain()
                label = msg[:52] + ("…" if len(msg) > 52 else "")
                check(not rec.improved,
                      f"{label!r} -> improve() NOT called ({rec.improved})")
                check(not rec.started,
                      f"{label!r} -> no project.started event ({rec.started})")

        after_files = _snapshot(projects)
        changed = sorted(set(before_files.items()) ^ set(after_files.items()))
        check(not changed,
              f"and NOT ONE project file changed across all "
              f"{len(NO_MUTATION)} messages ({changed[:2]})")

        active = await m.get_latest_fact(entity="projects", attribute="last_active")
        check(active is not None and active.value == LEGACY,
              f"the last-active pointer is untouched ({active and active.value})")


async def test_real_instructions_still_work():
    check.section("Phase 1: real instructions still reach improve()")

    # A FRESH runtime per message. Sharing one would latch `is_building` after
    # the first instruction ("I'm still working on it" is correct production
    # behaviour, but it hides whether the second message was authorised) and
    # would let `last_active` drift to whatever the previous case edited.
    for msg in MUTATION:
        with _tmp() as td:
            rt, m, projects = await _runtime(td)
            with _Recorder() as rec:
                await rt._project_prepass(msg)
                rec.drain()
            label = msg[:46] + ("…" if len(msg) > 46 else "")
            check(bool(rec.improved),
                  f"{label!r} -> improve() ran ({rec.improved[:1]})")
            check(bool(rec.started),
                  f"{label!r} -> project.started was published ({len(rec.started)})")
            if rec.improved:
                slug = rec.improved[0][0]
                # Named project when named; otherwise the active one, which is
                # exactly what an affirmative follow-up should continue.
                expected = ("flappy-bird" if "flappy" in msg.lower() else LEGACY)
                check(slug == expected,
                      f"{label!r} -> on {expected} ({slug})")


#: One per veto: each of these DOES open like an instruction, so the default
#: "no affirmative instruction" refusal does not catch it. Only the named veto
#: stands between these sentences and a filesystem write, which is what makes
#: them the tests that actually prove the vetoes exist.
VETO_DISCRIMINATORS = [
    ("prohibition", "Go ahead and look at it, but don't change anything."),
    ("denial", "Go ahead and look — I wasn't asking you to change anything."),
    ("retrospective", "Add nothing else; I already updated the project myself."),
    ("self_target", "Improve your response speed."),
    ("self_target", "Fix your memory handling."),
    ("deliberative", "Add a restart button, or do you think we should refactor instead?"),
]


async def test_each_veto_is_load_bearing():
    """Imperative-shaped messages that must STILL be refused.

    Written after a mutation run: removing any single veto left the suite green,
    because every message in the main corpus is also caught by the default
    "no affirmative instruction" refusal. Redundant safety is good; UNPROVEN
    safety is not. Each case below opens affirmatively, so only the named veto
    can refuse it.
    """
    check.section("Phase 1: every veto is individually load-bearing")

    from core.project_intent import authorize_project_mutation

    for veto, msg in VETO_DISCRIMINATORS:
        verdict = authorize_project_mutation(msg)
        check(not verdict.allowed,
              f"[{veto}] {msg[:46]!r} -> refused ({verdict.reason})")
        check(veto in verdict.reason,
              f"[{veto}] and refused for THAT reason ({verdict.reason})")

    # …and the same messages through the real pre-pass, so this is a boundary
    # test rather than a unit test of a helper.
    for veto, msg in VETO_DISCRIMINATORS:
        with _tmp() as td:
            rt, m, projects = await _runtime(td)
            with _Recorder() as rec:
                await rt._project_prepass(msg)
                rec.drain()
            check(not rec.improved and not rec.started,
                  f"[{veto}] {msg[:40]!r} -> no edit through the pre-pass "
                  f"({rec.improved})")


async def test_the_correction_reaches_the_lesson_path():
    """"Stop making false improvements." is a correction ABOUT the pre-pass.

    It used to be intercepted by the very subsystem it was correcting: the
    pre-pass returned a reply, `_chat_turn_stream` returned early, and
    `_capture_lessons` never saw it. A correction must not be swallowed by
    what it is correcting.
    """
    check.section("Phase 1: the correction is not swallowed by the pre-pass")

    correction = "Stop making false improvements. Im trying to run tests on you."

    with _tmp() as td:
        rt, m, projects = await _runtime(td)
        with _Recorder() as rec:
            reply = await rt._project_prepass(correction)
            rec.drain()
        check(reply is None,
              f"the pre-pass declines the correction, so the turn continues "
              f"({str(reply)[:60]!r})")
        check(not rec.improved, f"and starts no edit ({rec.improved})")

        # It is a real lesson, and now reaches the capture the pre-pass used to
        # short-circuit.
        await rt._capture_lessons(correction)
        lessons = await m.get_lessons(limit=20)
        stored = [x for x in lessons if "false improvements" in str(x).lower()]
        check(bool(stored),
              f"the correction is captured as a durable lesson "
              f"({len(stored)} of {len(lessons)})")

        # An ordinary project command must NOT become a behavioural lesson.
        await rt._capture_lessons("Improve flappy-bird's collision handling.")
        after = await m.get_lessons(limit=20)
        check(len(after) == len(lessons),
              f"but an ordinary project command is not stored as one "
              f"({len(after)} vs {len(lessons)})")


async def main():
    await test_conversation_never_mutates_a_project()
    await test_real_instructions_still_work()
    await test_each_veto_is_load_bearing()
    await test_the_correction_reaches_the_lesson_path()
    check.finish()


if __name__ == "__main__":
    run(main)
