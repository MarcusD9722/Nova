"""Streaming must actually stream, and a turn's answer must belong to that turn.

Three defects an independent reviewer found in the repeat-guard work, each of
which the previous suite missed because it asserted shape rather than timing or
concurrency:

  1. `_stream_guarded_reply` accumulated every token into a list and only
     yielded after the generation FINISHED, so the user waited the whole
     generation for the first character. `inspect.isasyncgenfunction` was true
     the entire time — syntax, not behaviour.

  2. The final text travelled through `self._last_stream_text` /
     `self._last_story_text` on a RuntimeManager that `backend/state.py` keeps
     as ONE process-wide singleton, so two concurrent turns overwrote each
     other's answer.

  3. `materially_different` existed but production never called it, so asking
     the same question twice could have its honest repeat answer rejected.

Run:  venv\\Scripts\\python.exe tests\\test_stream_and_turn_isolation.py
"""

from __future__ import annotations

import asyncio
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

check = Checks()


def _tmp():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


class _NullSem:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *a):
        return False


class _NoLLM:
    gpu_status = type("S", (), {"status": "stub"})()

    async def initialize(self):
        return None


async def _runtime(td: str, llm=None):
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
                        llm=llm or _NoLLM(), router=router, memory_dir=mem_dir)
    return rt, m


# ── CORRECTION 1: real streaming ────────────────────────────────────────────
class _BlockingModel:
    """Emits a prefix, then BLOCKS until released.

    The point of the block: if the consumer can see a token while this is still
    waiting, the production path is genuinely streaming. If it cannot, the
    implementation is buffering the whole generation.
    """

    def __init__(self, prefix: str, marker: str, tail: str) -> None:
        self._prefix = prefix
        self._marker = marker
        self._tail = tail
        self.release = asyncio.Event()
        self.reached_block = asyncio.Event()
        self.semaphore = _NullSem()

    class _Runtime:
        def __init__(self, outer):
            self._o = outer

        async def chat_stream(self, messages, **kw):
            for i in range(0, len(self._o._prefix), 20):
                yield self._o._prefix[i:i + 20]
            yield self._o._marker
            self._o.reached_block.set()
            await self._o.release.wait()          # <- still generating
            yield self._o._tail

    @property
    def runtime(self):
        return _BlockingModel._Runtime(self)


async def _drain(agen, sink: list, final: dict):
    async for ev in agen:
        if ev.get("type") == "token":
            sink.append(ev["text"])
        elif ev.get("type") in ("reply_final", "story_final"):
            final["text"] = ev.get("text", "")


async def test_tokens_arrive_before_generation_finishes():
    check.section("C1: a token is delivered while the model is still blocked")

    with _tmp() as td:
        rt, m = await _runtime(td)
        # Long enough to clear the repeat probe, so the guard has released.
        prefix = ("Here is a genuinely new answer that has nothing at all to do "
                  "with anything said before, and it keeps going for a while so "
                  "the guard's probe window is comfortably exceeded. ")
        model = _BlockingModel(prefix, "VISIBLE_NOW", " …and the rest arrives later.")

        seen: list[str] = []
        final: dict = {}
        task = asyncio.create_task(_drain(
            rt._stream_guarded_reply(
                model, [{"role": "user", "content": "tell me something new"}],
                budget=512, user_text="tell me something new",
                previous_replies=["An older unrelated reply about spreadsheets."],
            ), seen, final))

        # Wait for the model to reach its block, then look at what the consumer
        # already has — WITHOUT releasing it.
        await asyncio.wait_for(model.reached_block.wait(), timeout=10.0)
        for _ in range(50):                     # let the loop drain what exists
            await asyncio.sleep(0)
        got_early = "".join(seen)

        check("VISIBLE_NOW" in got_early,
              f"the marker token reached the consumer while the model was still "
              f"blocked ({len(got_early)} chars so far)")
        check(not model.release.is_set(),
              "and the generation was genuinely still incomplete at that moment")
        check("the rest arrives later" not in got_early,
              "the unfinished tail had not been produced yet")

        model.release.set()
        await asyncio.wait_for(task, timeout=10.0)
        whole = "".join(seen)
        check("the rest arrives later" in whole,
              "after release, the remainder arrives too")
        check(final.get("text", "").endswith("later."),
              f"and the final text is reported to the caller "
              f"({final.get('text', '')[-24:]!r})")


async def test_first_reply_streams_when_there_is_no_history():
    check.section("C1: a first reply is never withheld")

    with _tmp() as td:
        rt, m = await _runtime(td)
        model = _BlockingModel("Hello there, this is the very first thing I say. ",
                               "EARLY", " Rest.")
        seen: list[str] = []
        final: dict = {}
        task = asyncio.create_task(_drain(
            rt._stream_guarded_reply(
                model, [{"role": "user", "content": "hi"}],
                budget=512, user_text="hi", previous_replies=[]),
            seen, final))
        await asyncio.wait_for(model.reached_block.wait(), timeout=10.0)
        for _ in range(50):
            await asyncio.sleep(0)
        check("EARLY" in "".join(seen),
              f"with no history the guard is off and tokens flow immediately "
              f"({len(''.join(seen))} chars)")
        model.release.set()
        await asyncio.wait_for(task, timeout=10.0)


async def test_a_stale_prefix_still_never_reaches_the_screen():
    check.section("C1: the guard still blocks a replay before it is visible")

    PRIOR = ("I can help with quite a lot: remembering things you tell me, "
             "building and changing projects, reading and analysing code, "
             "searching the web, and keeping track of reminders and plans.")

    class _Replay:
        """Replays PRIOR, then a fresh answer on the retry."""

        def __init__(self):
            self.calls = 0
            self.semaphore = _NullSem()

        class _Runtime:
            def __init__(self, outer):
                self._o = outer

            async def chat_stream(self, messages, **kw):
                self._o.calls += 1
                text = PRIOR if self._o.calls == 1 else "Something genuinely new instead."
                for i in range(0, len(text), 16):
                    yield text[i:i + 16]

        @property
        def runtime(self):
            return _Replay._Runtime(self)

    with _tmp() as td:
        rt, m = await _runtime(td)
        model = _Replay()
        seen: list[str] = []
        final: dict = {}
        await _drain(rt._stream_guarded_reply(
            model, [{"role": "user", "content": "new topic"}],
            budget=512, user_text="She has decided on themes for the parties.",
            previous_replies=[PRIOR]), seen, final)

        whole = "".join(seen)
        check(PRIOR[:60] not in whole,
              f"not one character of the stale reply was emitted ({whole[:50]!r})")
        check("genuinely new" in whole, f"the regenerated reply is shown ({whole[:40]!r})")
        check(model.calls == 2, f"exactly one regeneration ({model.calls})")


# ── CORRECTION 2: turn-local state ──────────────────────────────────────────
class _FixedModel:
    """Emits one fixed body, with a barrier so turns can be interleaved."""

    def __init__(self, body: str, barrier: asyncio.Barrier | None = None) -> None:
        self._body = body
        self._barrier = barrier
        self._synced = False        # one-shot: continuations must not re-wait
        self.semaphore = _NullSem()

    class _Runtime:
        def __init__(self, outer):
            self._o = outer

        async def chat_stream(self, messages, **kw):
            mid = len(self._o._body) // 2
            yield self._o._body[:mid]
            if self._o._barrier is not None and not self._o._synced:
                self._o._synced = True
                await self._o._barrier.wait()     # force the turns to interleave
            yield self._o._body[mid:]

        async def chat_stream_ex(self, messages, **kw):
            async for t in self.chat_stream(messages, **kw):
                yield {"type": "token", "text": t}
            yield {"type": "done", "finish_reason": "stop"}

    @property
    def runtime(self):
        return _FixedModel._Runtime(self)


async def test_two_concurrent_replies_do_not_cross():
    check.section("C2: two turns on ONE RuntimeManager keep their own answers")

    A = "A" * 400
    B = "B" * 400

    with _tmp() as td:
        rt, m = await _runtime(td)
        barrier = asyncio.Barrier(2)

        async def turn(body: str):
            seen: list[str] = []
            final: dict = {}
            await _drain(rt._stream_guarded_reply(
                _FixedModel(body, barrier),
                [{"role": "user", "content": "go"}],
                budget=512, user_text="go", previous_replies=[]), seen, final)
            return "".join(seen), final.get("text", "")

        (a_seen, a_final), (b_seen, b_final) = await asyncio.gather(
            turn(A), turn(B))

        check(set(a_seen) == {"A"}, f"turn A emitted only A ({sorted(set(a_seen))})")
        check(set(b_seen) == {"B"}, f"turn B emitted only B ({sorted(set(b_seen))})")
        check(a_final == A, f"turn A's FINAL text is A only ({len(a_final)} chars, "
                            f"{sorted(set(a_final))})")
        check(b_final == B, f"turn B's FINAL text is B only ({len(b_final)} chars, "
                            f"{sorted(set(b_final))})")
        crossed = sum(1 for x in (a_final, b_final) if len(set(x)) != 1)
        check(crossed == 0, f"cross-contamination count: {crossed}")

        check(not hasattr(rt, "_last_stream_text") or not getattr(rt, "_last_stream_text", ""),
              "and no answer is parked on the shared RuntimeManager")


async def test_two_concurrent_stories_do_not_cross():
    """Two stories on one RuntimeManager, interleaved between segments.

    No barrier inside generation: `_stream_story` holds the shared GPU
    semaphore for each segment, so a barrier in there would deadlock two
    stories against each other — that is the semaphore doing its job, not a
    defect. The semaphore is acquired PER SEGMENT, so a two-segment story
    yields the GPU between segments and the two turns genuinely interleave.
    """
    check.section("C2: two stories keep their own text")

    class _Segments:
        """Two segments: the first stops on `length`, so a continuation runs."""

        def __init__(self, tag: str) -> None:
            self.tag = tag
            self.calls = 0

        async def chat_stream_ex(self, messages, **kw):
            self.calls += 1
            first = self.calls == 1
            body = (f"{self.tag} opening. " * 12) if first else (f"{self.tag} ending. " * 6)
            for i in range(0, len(body), 20):
                yield {"type": "token", "text": body[i:i + 20]}
                await asyncio.sleep(0)      # give the other turn a chance
            yield {"type": "done", "finish_reason": "length" if first else "stop"}

    with _tmp() as td:
        rt, m = await _runtime(td)

        async def story(tag: str):
            seen: list[str] = []
            final: dict = {}
            await _drain(rt._stream_story(
                [{"role": "user", "content": "story"}], budget=256,
                llm=_Segments(tag)), seen, final)
            return "".join(seen), final.get("text", "")

        (a_seen, a_final), (b_seen, b_final) = await asyncio.gather(
            story("Aardvark"), story("Bumblebee"))

        check("Bumblebee" not in a_seen, f"story A has no B text ({a_seen[:28]!r})")
        check("Aardvark" not in b_seen, f"story B has no A text ({b_seen[:28]!r})")
        check("Aardvark opening" in a_final and "Aardvark ending" in a_final,
              f"story A's bible text has BOTH its segments ({len(a_final)} chars)")
        check("Bumblebee" not in a_final,
              f"and none of story B ({a_final[:28]!r})")
        check("Bumblebee opening" in b_final and "Bumblebee ending" in b_final,
              f"story B's bible text has both of its segments ({len(b_final)} chars)")
        check("Aardvark" not in b_final,
              f"and none of story A ({b_final[:28]!r})")


# ── CORRECTION 3: material difference ───────────────────────────────────────
async def test_the_same_question_twice_keeps_its_answer():
    check.section("C3: repeating a question may repeat the answer")

    RAID = ("RAID 5 stripes data across at least three disks and stores a "
            "distributed parity block, so any single drive can fail without "
            "losing data, at the cost of one drive's worth of capacity and a "
            "write penalty from recomputing parity on every write.")

    class _Same:
        def __init__(self):
            self.calls = 0
            self.semaphore = _NullSem()

        class _Runtime:
            def __init__(self, outer):
                self._o = outer

            async def chat_stream(self, messages, **kw):
                self._o.calls += 1
                for i in range(0, len(RAID), 24):
                    yield RAID[i:i + 24]

        @property
        def runtime(self):
            return _Same._Runtime(self)

    with _tmp() as td:
        rt, m = await _runtime(td)
        model = _Same()
        seen: list[str] = []
        final: dict = {}
        await _drain(rt._stream_guarded_reply(
            model, [{"role": "user", "content": "Explain RAID 5."}],
            budget=512, user_text="Explain RAID 5.",
            previous_replies=[RAID],
            previous_user_text="Explain RAID 5."), seen, final)

        check("".join(seen).strip() == RAID.strip(),
              f"the same answer is delivered again ({''.join(seen)[:40]!r})")
        check(model.calls == 1,
              f"with NO regeneration, because the question did not change "
              f"({model.calls} generations)")


async def test_a_new_question_still_rejects_a_replay():
    check.section("C3: a materially different message still triggers the guard")

    PRIOR = ("I can help with quite a lot: remembering things, building "
             "projects, reading code, searching the web and keeping reminders.")

    class _Replay:
        def __init__(self):
            self.calls = 0
            self.semaphore = _NullSem()

        class _Runtime:
            def __init__(self, outer):
                self._o = outer

            async def chat_stream(self, messages, **kw):
                self._o.calls += 1
                text = PRIOR if self._o.calls == 1 else "Fresh answer about themes."
                for i in range(0, len(text), 20):
                    yield text[i:i + 20]

        @property
        def runtime(self):
            return _Replay._Runtime(self)

    with _tmp() as td:
        rt, m = await _runtime(td)
        model = _Replay()
        seen: list[str] = []
        final: dict = {}
        await _drain(rt._stream_guarded_reply(
            model, [{"role": "user", "content": "x"}], budget=512,
            user_text="She's decided on themes for their parties already.",
            previous_replies=[PRIOR],
            previous_user_text="What are you capable of?"), seen, final)

        check(model.calls == 2, f"the replay was rejected ({model.calls} generations)")
        check("Fresh answer" in "".join(seen), "and a new reply was produced")


async def test_an_explicit_repeat_request_bypasses():
    check.section("C3: 'say that again' still bypasses the guard")

    PRIOR = ("I can help with quite a lot: remembering things, building "
             "projects, reading code, searching the web and keeping reminders.")

    class _Same:
        def __init__(self):
            self.calls = 0
            self.semaphore = _NullSem()

        class _Runtime:
            def __init__(self, outer):
                self._o = outer

            async def chat_stream(self, messages, **kw):
                self._o.calls += 1
                for i in range(0, len(PRIOR), 20):
                    yield PRIOR[i:i + 20]

        @property
        def runtime(self):
            return _Same._Runtime(self)

    with _tmp() as td:
        rt, m = await _runtime(td)
        model = _Same()
        seen: list[str] = []
        final: dict = {}
        await _drain(rt._stream_guarded_reply(
            model, [{"role": "user", "content": "Say that again."}], budget=512,
            user_text="Say that again.", previous_replies=[PRIOR],
            previous_user_text="What are you capable of?"), seen, final)
        check("".join(seen).strip() == PRIOR.strip(),
              "the prior answer is repeated on request")
        check(model.calls == 1, f"with no regeneration ({model.calls})")


async def main():
    await test_tokens_arrive_before_generation_finishes()
    await test_first_reply_streams_when_there_is_no_history()
    await test_a_stale_prefix_still_never_reaches_the_screen()
    await test_two_concurrent_replies_do_not_cross()
    await test_two_concurrent_stories_do_not_cross()
    await test_the_same_question_twice_keeps_its_answer()
    await test_a_new_question_still_rejects_a_replay()
    await test_an_explicit_repeat_request_bypasses()
    check.finish()


if __name__ == "__main__":
    run(main)
