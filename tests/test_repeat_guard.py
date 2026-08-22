"""An accidental replay of a previous reply must not reach the user.

Live: Nova answered a capability question, Marcus then said something new —

    "She's enjoying preparing everything. She has decided on themes for their
     parties already."

— and she replied with essentially the previous capability answer.

Ruled out first, per the brief: `core/semantic_cache.py` is imported by no
production module (only by a test), so the response cache is NOT in the chat
path and this was not a stale cache hit.

Run:  venv\\Scripts\\python.exe tests\\test_repeat_guard.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, run  # noqa: E402

from core.repetition import (  # noqa: E402
    is_near_duplicate, materially_different, similarity, wants_repeat,
)

check = Checks()

PRIOR = (
    "I can help with quite a lot: remembering things you tell me, building and "
    "changing projects, reading and analysing code, searching the web, weather "
    "and directions, reminders and planning, and controlling this computer."
)
NEW_MESSAGE = ("She's enjoying preparing everything. She has decided on themes "
               "for their parties already.")


class _ScriptedModel:
    """A model whose successive generations are scripted."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[list[dict]] = []
        self.semaphore = _NullSem()

    class _Runtime:
        def __init__(self, outer):
            self._outer = outer

        async def chat_stream(self, messages, **kw):
            self._outer.calls.append(messages)
            text = (self._outer._replies.pop(0) if self._outer._replies else "")
            # Stream in small pieces, like the real one.
            for i in range(0, len(text), 24):
                yield text[i:i + 24]

    @property
    def runtime(self):
        return _ScriptedModel._Runtime(self)


class _NullSem:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *a):
        return False


class _NoLLM:
    gpu_status = type("S", (), {"status": "stub"})()

    async def initialize(self):
        return None


def _tmp():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


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
    return RuntimeManager(repo_root=root, projects_dir=projects, memory=m,
                          llm=_NoLLM(), router=router, memory_dir=mem_dir)


#: The final text now arrives as an EVENT rather than on a RuntimeManager
#: attribute — see core/runtime.py: one shared STATE.runtime means two
#: concurrent turns would overwrite each other's answer.
_LAST_FINAL: dict = {}


async def _collect(rt, model, *, user_text, previous, previous_user_text=""):
    out: list[str] = []
    _LAST_FINAL.clear()
    async for ev in rt._stream_guarded_reply(
        model, [{"role": "user", "content": user_text}],
        budget=512, user_text=user_text, previous_replies=previous,
        previous_user_text=previous_user_text,
    ):
        if ev.get("type") == "token":
            out.append(ev["text"])
        elif ev.get("type") == "reply_final":
            _LAST_FINAL["text"] = ev.get("text", "")
    return "".join(out)


async def test_similarity_is_deterministic():
    check.section("Phase 4: the similarity measure")

    check(similarity(PRIOR, PRIOR) == 1.0, "identical text scores 1.0")
    near = PRIOR.replace("quite a lot", "a lot").replace("and directions", "and maps")
    check(similarity(PRIOR, near) >= 0.90,
          f"a near-verbatim edit still scores high ({similarity(PRIOR, near):.3f})")
    check(similarity(PRIOR, NEW_MESSAGE) < 0.5,
          f"unrelated text scores low ({similarity(PRIOR, NEW_MESSAGE):.3f})")

    check(is_near_duplicate(PRIOR, [PRIOR]) is not None, "an exact replay is caught")
    check(is_near_duplicate(near, [PRIOR]) is not None, "a near-verbatim replay is caught")
    check(is_near_duplicate("Sure, happy to help with that.", [PRIOR]) is None,
          "an unrelated reply is not")
    check(is_near_duplicate("Yes.", ["Yes."]) is None,
          "and very short replies are left alone")


async def test_explicit_repeat_requests_are_allowed():
    check.section("Phase 4: 'say that again' still repeats")

    for ask in ("Say that again.", "Repeat your last answer.",
                "What did you just say?", "Can you repeat that?"):
        check(wants_repeat(ask), f"{ask!r} is an explicit repeat request")
    check(not wants_repeat(NEW_MESSAGE),
          "but a new statement is not")
    check(materially_different(NEW_MESSAGE, "What are you capable of?"),
          "and the new message IS materially different from the old one")

    # The FALSE direction is the one the guard actually depends on: asking the
    # same thing twice must be allowed to get the same answer, so a function
    # that only ever says "yes, different" would silently re-enable the bug.
    for same in ("Explain RAID 5.", "what are you capable of?",
                 "How does a heat pump work?"):
        check(not materially_different(same, same),
              f"the identical question is NOT materially different ({same!r})")
    check(not materially_different("Explain RAID 5.", "explain raid 5"),
          "and neither is the same question typed differently")


async def test_the_live_failure_is_rejected_and_regenerated():
    check.section("Phase 4: the repeated reply is rejected, then replaced")

    with _tmp() as td:
        rt = await _runtime(td)
        fresh = "That's lovely — has she picked a theme for each of them yet?"
        model = _ScriptedModel([PRIOR, fresh])      # attempt 1 repeats

        got = await _collect(rt, model, user_text=NEW_MESSAGE, previous=[PRIOR])

        check(got.strip() == fresh,
              f"the user receives the NEW reply ({got[:60]!r})")
        check(PRIOR[:60] not in got,
              "and none of the stale answer leaks through")
        check(len(model.calls) == 2,
              f"exactly one regeneration happened ({len(model.calls)} generations)")
        nudge = model.calls[1][-1]["content"]
        check("repeated" in nudge and NEW_MESSAGE in nudge,
              f"the retry names the repeat AND the current message ({nudge[:70]!r})")
        check(_LAST_FINAL.get("text", "").strip() == fresh,
              f"and the committed text is the new reply, not the stale one "
              f"({_LAST_FINAL.get('text', '')[:40]!r})")


async def test_a_second_repeat_fails_honestly():
    check.section("Phase 4: it does not regenerate forever")

    with _tmp() as td:
        rt = await _runtime(td)
        model = _ScriptedModel([PRIOR, PRIOR, "a third one that never runs"])

        got = await _collect(rt, model, user_text=NEW_MESSAGE, previous=[PRIOR])

        check(len(model.calls) == 2,
              f"it stops after ONE retry ({len(model.calls)} generations)")
        check(PRIOR[:60] not in got,
              f"the stale text is still not served ({got[:60]!r})")
        check("repeating myself" in got,
              f"and it says so honestly instead ({got[:70]!r})")


async def test_an_explicit_repeat_request_bypasses_the_guard():
    check.section("Phase 4: an asked-for repeat is delivered")

    with _tmp() as td:
        rt = await _runtime(td)
        model = _ScriptedModel([PRIOR])
        got = await _collect(rt, model, user_text="Say that again.", previous=[PRIOR])
        check(got.strip() == PRIOR.strip(),
              f"the same answer is returned on request ({got[:50]!r})")
        check(len(model.calls) == 1, "with no regeneration")


async def test_a_repeated_fact_is_not_suppressed():
    check.section("Phase 4: the same fact twice is fine")

    with _tmp() as td:
        rt = await _runtime(td)
        # Same fact, different sentence — this must NOT be treated as a replay.
        first = "Robin's birthday is March 14."
        second = "It's March 14 — Robin's birthday is coming up in a few weeks."
        model = _ScriptedModel([second])
        got = await _collect(rt, model, user_text="When is Robin's birthday again?",
                             previous=[first],
                             previous_user_text="Tell me about Robin.")
        check(got.strip() == second,
              f"a re-stated fact is delivered ({got[:50]!r})")
        check(len(model.calls) == 1, "with no regeneration")


async def test_normal_replies_stream_untouched():
    check.section("Phase 4: ordinary streaming is unchanged")

    with _tmp() as td:
        rt = await _runtime(td)
        answer = ("Sure — I put the leaderboard behind the pause menu so it "
                  "doesn't crowd the HUD, and it saves the top ten scores.")
        model = _ScriptedModel([answer])
        got = await _collect(rt, model, user_text="How did you do the leaderboard?",
                             previous=[PRIOR])
        check(got == answer, f"the reply arrives intact ({got[:40]!r})")
        check(len(model.calls) == 1, "in one generation")

        # With no history at all the guard cannot fire.
        model2 = _ScriptedModel([answer])
        got2 = await _collect(rt, model2, user_text="hello", previous=[])
        check(got2 == answer, "and a first reply is never withheld")


async def main():
    await test_similarity_is_deterministic()
    await test_explicit_repeat_requests_are_allowed()
    await test_the_live_failure_is_rejected_and_regenerated()
    await test_a_second_repeat_fails_honestly()
    await test_an_explicit_repeat_request_bypasses_the_guard()
    await test_a_repeated_fact_is_not_suppressed()
    await test_normal_replies_stream_untouched()
    check.finish()


if __name__ == "__main__":
    run(main)
