"""A long story must not stop mid-sentence because it ran out of budget.

Live: "Can you tell me a long story about a dinosaur named Rex" produced a
substantial story that ended abruptly, mid-sentence.

Story mode used one fixed budget and treated any non-empty stream as success —
and `LLMRuntime.chat_stream` discarded `finish_reason`, so "the model finished"
and "the model hit the token limit" were indistinguishable downstream. Only one
of those deserves a continuation.

Run:  venv\\Scripts\\python.exe tests\\test_story_continuation.py
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

check = Checks()

SEG1 = ("Rex was the smallest dinosaur in the valley, which everyone agreed was "
        "a problem, because the valley had a great many things that needed "
        "reaching. One grey morning he set out toward the ridge, and just as he "
        "reached the tall grass he heard something that made him stop and")
SEG2 = (" turn around slowly. It was Pip, the smallest pterosaur, tangled in a "
        "vine. Rex freed her, and they walked home together as the sun came up "
        "over the ridge. The end.")


class _ScriptedLLM:
    """Yields scripted segments with scripted finish reasons."""

    gpu_status = type("S", (), {"status": "stub"})()

    def __init__(self, segments: list[tuple[str, str]]) -> None:
        self._segments = list(segments)
        self.calls: list[list[dict]] = []

    async def initialize(self):
        return None

    async def chat_stream_ex(self, messages, **kw):
        self.calls.append(list(messages))
        if not self._segments:
            yield {"type": "done", "finish_reason": "stop"}
            return
        text, reason = self._segments.pop(0)
        for i in range(0, len(text), 32):
            yield {"type": "token", "text": text[i:i + 32]}
        yield {"type": "done", "finish_reason": reason}

    async def chat(self, *a, **k):
        return ""


def _tmp():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


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
    return RuntimeManager(repo_root=root, projects_dir=projects, memory=m,
                          llm=llm, router=router, memory_dir=mem_dir)


async def _tell(rt, messages=None):
    msgs = messages or [{"role": "user", "content": "Tell me a long story about Rex"}]
    out: list[str] = []
    async for ev in rt._stream_story(msgs, budget=1200):
        if ev.get("type") == "token":
            out.append(ev["text"])
    return "".join(out)


async def test_a_length_stop_is_continued():
    check.section("Phase 5: a story cut off by the budget is continued")

    with _tmp() as td:
        llm = _ScriptedLLM([(SEG1, "length"), (SEG2, "stop")])
        rt = await _runtime(td, llm)
        story = await _tell(rt)

        check(story == SEG1 + SEG2,
              f"both segments arrive, in order, exactly once ({len(story)} chars)")
        check(story.count("Rex was the smallest dinosaur") == 1,
              "the opening is not repeated")
        check(story.rstrip().endswith("The end."),
              f"and the story actually ends ({story[-24:]!r})")
        check(len(llm.calls) == 2, f"one continuation was needed ({len(llm.calls)})")

        # The continuation must carry the story so far, and say not to restart.
        cont = llm.calls[1]
        check(any(m["role"] == "assistant" and SEG1[:40] in m["content"] for m in cont),
              "the continuation prompt contains the story so far")
        check("Continue the story" in cont[-1]["content"]
              and "Do not restart" in cont[-1]["content"],
              f"and tells it not to restart ({cont[-1]['content'][:60]!r})")

        check(rt._last_story_text == SEG1 + SEG2,
              "the story bible receives the COMPLETE story, not the first segment")


async def test_a_natural_stop_is_left_alone():
    check.section("Phase 5: a finished story is not padded")

    with _tmp() as td:
        llm = _ScriptedLLM([(SEG1 + SEG2, "stop"), ("SHOULD NOT RUN", "stop")])
        rt = await _runtime(td, llm)
        story = await _tell(rt)

        check(len(llm.calls) == 1, f"exactly one generation ({len(llm.calls)})")
        check("SHOULD NOT RUN" not in story, "no continuation was appended")
        check(story == SEG1 + SEG2, "and the story is delivered as written")


async def test_repeated_length_cannot_loop_forever():
    check.section("Phase 5: continuation is bounded")

    with _tmp() as td:
        # Always "length": a model that never emits a natural stop.
        llm = _ScriptedLLM([(f"segment {i}. ", "length") for i in range(20)])
        rt = await _runtime(td, llm)
        story = await _tell(rt)

        bound = rt._STORY_MAX_CONTINUATIONS + 1
        check(len(llm.calls) == bound,
              f"it stops at the bound ({len(llm.calls)} generations, bound {bound})")
        check(story.startswith("segment 0."),
              f"and returns what it did produce ({story[:40]!r})")
        check(rt._last_story_text == story.strip(),
              f"with the accumulated text recorded ({rt._last_story_text[-20:]!r})")


async def test_an_empty_segment_is_safe():
    check.section("Phase 5: an empty continuation does not spin or crash")

    with _tmp() as td:
        llm = _ScriptedLLM([(SEG1, "length"), ("", "length"), ("never", "stop")])
        rt = await _runtime(td, llm)
        story = await _tell(rt)

        check(story == SEG1, f"the story is what was actually produced ({len(story)})")
        check(len(llm.calls) == 2,
              f"an empty segment stops the loop ({len(llm.calls)} generations)")
        check("never" not in story, "and nothing further is appended")


async def test_ordinary_chat_streaming_is_unchanged():
    check.section("Phase 5: chat_stream's contract is untouched")

    import inspect

    from core.llm_runtime import LLMRuntime

    check(hasattr(LLMRuntime, "chat_stream_ex"),
          "the metadata variant exists")
    check(inspect.isasyncgenfunction(LLMRuntime.chat_stream),
          "and chat_stream is still an async generator")

    src = inspect.getsource(LLMRuntime.chat_stream)
    check('yield event["text"]' in src,
          "chat_stream still yields plain text deltas, not event dicts")

    # Every existing caller uses chat_stream; none should have to change.
    class _Ex(LLMRuntime):
        def __init__(self):  # noqa: D107  (no model in this test)
            pass

        async def chat_stream_ex(self, messages, **kw):
            yield {"type": "token", "text": "hello "}
            yield {"type": "token", "text": "world"}
            yield {"type": "done", "finish_reason": "stop"}

    got = []
    async for tok in _Ex().chat_stream([{"role": "user", "content": "hi"}]):
        got.append(tok)
    check(got == ["hello ", "world"],
          f"a plain-text consumer sees only text ({got})")


async def main():
    await test_a_length_stop_is_continued()
    await test_a_natural_stop_is_left_alone()
    await test_repeated_length_cannot_loop_forever()
    await test_an_empty_segment_is_safe()
    await test_ordinary_chat_streaming_is_unchanged()
    check.finish()


if __name__ == "__main__":
    run(main)
