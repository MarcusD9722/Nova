"""The conversation that broke Nova, replayed end to end.

Every subsystem below already had tests, and every one of them passed while
this exact seven-message exchange went wrong. That is the reason this file
exists: it drives the REAL `RuntimeManager.chat_turn_stream` in sequence, in
one conversation, rather than testing each part in isolation.

The sequence:

  1. "I had worked on your code these last few days trying to improve your
      overall performance and sturdiness."   -> no project mutation
  2. "I wasnt trying to have you upgrade anything. I was just making small
      talk."                                 -> no project mutation
  3. "Stop making false improvements. Im trying to run tests on you."
                                             -> no mutation, still a lesson
  4. "What are you capable of?"              -> grounded in the real registry
  5. a new personal statement                -> does not repeat #4
  6. "When is <person>'s birthday and when is my birthday?"
                                             -> both dates, no empty apology
  7. "Tell me a long story about a dinosaur named Rex."
                                             -> continued, not cut off

Temporary storage and generic fixture people throughout; Marcus's real database
and projects folder are never touched.

Run:  venv\\Scripts\\python.exe tests\\test_live_conversation_regression.py
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, run  # noqa: E402

from core.project_builder import ProjectBuilder  # noqa: E402

check = Checks()

APOLOGY = "Sorry — I came up empty on that one."
LEGACY = "blue-and-tower-defense-and-i-want-you-to"

CAPABILITY_ANSWER = (
    "I can help with a lot of things: remembering what you tell me, building "
    "projects, reading code, searching the web, and keeping track of reminders."
)
STORY_1 = ("Rex was the smallest dinosaur in the valley, and on the morning this "
           "story begins he had decided to walk all the way to the ridge, which "
           "no one his size had ever done, and just as he reached the tall grass "
           "he heard something behind him that made him stop and")
STORY_2 = (" turn slowly around. It was only Pip, tangled in a vine. Rex freed "
           "her and they walked home together. The end.")


def _tmp():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


def _snapshot(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.blake2b(
                p.read_bytes(), digest_size=8).hexdigest()
    return out


class _ScriptedLLM:
    """A model that answers from a script, and records everything it was asked.

    Story segments carry finish reasons so the length-continuation path is
    exercised for real.
    """

    gpu_status = type("S", (), {"status": "stub"})()

    def __init__(self) -> None:
        self.chat_texts: list[str] = []
        self.stream_texts: list[str] = []
        #: Consumed ONLY by the spoken-reply stream. Other subsystems (the
        #: tool decider, summariser, salvage) also call chat() during a turn,
        #: and letting them eat the script made this test nondeterministic.
        self.stream_replies: list[str] = []
        self.story_segments: list[tuple[str, str]] = []

    async def initialize(self):
        return None

    async def chat(self, messages, **kw):
        text = "Mm — go on."
        self.chat_texts.append(text)
        return text

    async def chat_stream(self, messages, **kw):
        async for ev in self.chat_stream_ex(messages, **kw):
            if ev.get("type") == "token":
                yield ev["text"]

    async def chat_stream_ex(self, messages, **kw):
        if self.story_segments:
            text, reason = self.story_segments.pop(0)
            self.stream_texts.append(text)
            for i in range(0, len(text), 40):
                yield {"type": "token", "text": text[i:i + 40]}
            yield {"type": "done", "finish_reason": reason}
            return
        text = (self.stream_replies.pop(0) if self.stream_replies
                else "Mm — go on.")
        self.stream_texts.append(text)
        for i in range(0, len(text), 40):
            yield {"type": "token", "text": text[i:i + 40]}
        yield {"type": "done", "finish_reason": "stop"}


class _Watch:
    """Records any project mutation attempted during the whole conversation."""

    def __enter__(self):
        self.improved: list[tuple[str, str]] = []
        self._real_improve = ProjectBuilder.improve
        self._real_worker = ProjectBuilder._improve
        rec = self

        async def _no_worker(self, slug, instructions):
            return None

        async def _spy(self, *, slug, instructions):
            rec.improved.append((slug, instructions))
            return await rec._real_improve(self, slug=slug, instructions=instructions)

        ProjectBuilder.improve = _spy
        ProjectBuilder._improve = _no_worker
        return self

    def __exit__(self, *exc):
        ProjectBuilder.improve = self._real_improve
        ProjectBuilder._improve = self._real_worker
        return False


async def _runtime(td: str, llm):
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
                        llm=llm, router=router, memory_dir=mem_dir)

    for slug, title in ((LEGACY, "Legacy"), ("flappy-bird", "Flappy Bird")):
        d = projects / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "PROJECT.md").write_text(f"# {title}\n", encoding="utf-8")
        (d / "main.py").write_text("print('x')\n", encoding="utf-8")
    await m.add_fact(entity="projects", attribute="last_active", value=LEGACY,
                     confidence=0.95)
    return rt, m, projects


async def _say(rt, text: str, conversation_id) -> str:
    """One real turn through the production streaming entry point."""
    tokens: list[str] = []
    final = ""
    async for ev in rt.chat_turn_stream(user_text=text,
                                        conversation_id=conversation_id):
        if ev.get("type") == "token":
            tokens.append(ev["text"])
        elif ev.get("type") == "done":
            final = ev.get("full_text") or ""
    return (("".join(tokens)).strip() or final.strip())


async def test_the_whole_conversation():
    check.section("Phase 7: the live conversation, replayed in order")

    with _tmp() as td:
        llm = _ScriptedLLM()
        rt, m, projects = await _runtime(td, llm)
        conv = uuid4()
        before = _snapshot(projects)

        await m.upsert_person("Robin", {"relation": "partner", "birthday": "March 14"})
        await m.add_fact(entity="user", attribute="birthday", value="1990-07-02",
                         confidence=0.95)

        with _Watch() as watch:
            # 1-3: ordinary conversation must not touch the filesystem.
            for i, msg in enumerate((
                "I had worked on your code these last few days trying to improve "
                "your overall performance and sturdiness.",
                "I wasnt trying to have you upgrade anything. I was just making "
                "small talk.",
                "Stop making false improvements. Im trying to run tests on you.",
            ), start=1):
                llm.stream_replies.append(f"Understood — noted, message {i}.")
                reply = await _say(rt, msg, conv)
                check(bool(reply), f"{i}. got an ordinary reply ({reply[:40]!r})")
                check(not watch.improved,
                      f"{i}. and NO project mutation ({watch.improved})")

            # 3 (cont.): the correction is still learnable.
            lessons = await m.get_lessons(limit=20)
            check(any("false improvements" in str(x).lower() for x in lessons),
                  f"3. the correction was captured as a lesson ({len(lessons)})")

            # 4: capability answer, grounded in the real registry.
            cap = await _say(rt, "What are you capable of?", conv)
            check("Right now I can help with" in cap,
                  f"4. the capability answer is runtime-grounded ({cap[:60]!r})")
            check("remembering things you tell me" in cap,
                  f"4. and names a really-registered family ({cap[:80]!r})")
            check(not watch.improved, "4. still no project mutation")

            # 5: a new personal statement must not replay #4.
            # The model tries to replay turn 4 VERBATIM. Scripted from `cap`
            # itself rather than a lookalike constant, because turn 4's answer
            # is deterministic and a near-copy of something else would not
            # exercise the guard at all.
            llm.stream_replies.append(cap)
            llm.stream_replies.append("That's lovely — has she picked the themes yet?")
            follow = await _say(
                rt,
                "She's enjoying preparing everything. She has decided on themes "
                "for their parties already.",
                conv)
            check(follow.strip() != cap.strip(),
                  f"5. the new message does NOT get answer #4 back ({follow[:50]!r})")
            check("themes yet" in follow,
                  f"5. it gets a reply about what was actually said ({follow[:60]!r})")

            # 6: both stored dates, no empty apology.
            dates = await _say(
                rt, "When is Robin's birthday and when is my birthday?", conv)
            check(APOLOGY not in dates,
                  f"6. the live apology does not appear ({dates[:60]!r})")
            check("March 14" in dates and "July 2" in dates,
                  f"6. both dates are answered ({dates!r})")

            # 7: a long story is continued rather than cut off.
            llm.story_segments = [(STORY_1, "length"), (STORY_2, "stop")]
            story = await _say(rt, "Tell me a long story about a dinosaur named Rex.",
                               conv)
            check(story.startswith("Rex was the smallest dinosaur"),
                  f"7. the story starts as written ({story[:40]!r})")
            check(story.rstrip().endswith("The end."),
                  f"7. and is carried to an ending ({story[-30:]!r})")
            check(story.count("Rex was the smallest") == 1,
                  "7. without restarting itself")

            # Across the WHOLE conversation:
            check(not watch.improved,
                  f"nothing in seven turns started a project edit ({watch.improved})")

        after = _snapshot(projects)
        changed = sorted(set(before.items()) ^ set(after.items()))
        check(not changed, f"and no project file changed at all ({changed[:2]})")

        active = await m.get_latest_fact(entity="projects", attribute="last_active")
        check(active is not None and active.value == LEGACY,
              f"the last-active pointer is untouched ({active and active.value})")


async def test_a_real_instruction_still_works_in_the_same_conversation():
    """The boundary is a gate, not a wall: the next turn can still build."""
    check.section("Phase 7: an explicit instruction still works afterwards")

    with _tmp() as td:
        llm = _ScriptedLLM()
        rt, m, projects = await _runtime(td, llm)
        conv = uuid4()

        with _Watch() as watch:
            llm.stream_replies.append("Noted.")
            await _say(rt, "I wasnt trying to have you upgrade anything.", conv)
            check(not watch.improved, "the small talk changed nothing")

            reply = await _say(rt, "Improve flappy-bird's collision handling.", conv)
            check(bool(watch.improved),
                  f"but the explicit instruction runs ({watch.improved[:1]})")
            check(watch.improved[0][0] == "flappy-bird",
                  f"on the named project ({watch.improved[0][0]})")
            check("flappy-bird" in reply,
                  f"and Nova says so ({reply[:60]!r})")


async def main():
    await test_the_whole_conversation()
    await test_a_real_instruction_still_works_in_the_same_conversation()
    check.finish()


if __name__ == "__main__":
    run(main)
