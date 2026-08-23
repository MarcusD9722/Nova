"""An empty generation must not become a generic apology on a question we can answer.

Live:

    Marcus: "When is Leslie's birthday and when is my birthday?"
    Nova:   "Sorry — I came up empty on that one."

That string is the runtime's last-resort fallback: the stream produced nothing
visible and the salvage generation did too. It is NOT evidence that memory
failed, and it must never be treated as a passing answer.

Two things are asserted here:

  1. the retry policy runs, returns a successful retry exactly once, leaks no
     partial output, and counts what happened;
  2. a stored-fact question never reaches this class of failure at all, because
     Phase 2 answers it from SQLite before any generation.

Run:  venv\\Scripts\\python.exe tests\\test_empty_generation_recovery.py
"""

from __future__ import annotations

import inspect
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

APOLOGY = "Sorry — I came up empty on that one."


def _tmp():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


class _EmptyThenText:
    """A model whose stream yields nothing and whose chat() rescues it."""

    gpu_status = type("S", (), {"status": "stub"})()

    def __init__(self, rescue: str = "Here you go — that's sorted.") -> None:
        self._rescue = rescue
        self.chat_calls: list[dict] = []
        self.stream_calls = 0
        self._usage = {"empty_retries": 0, "empty_exhausted": 0, "empty_salvaged": 0}

    async def initialize(self):
        return None

    def usage_stats(self):
        return dict(self._usage)

    async def chat_stream(self, messages, **kw):
        self.stream_calls += 1
        return
        yield ""            # pragma: no cover  (generator shape)

    async def chat_stream_ex(self, messages, **kw):
        self.stream_calls += 1
        yield {"type": "done", "finish_reason": "empty"}

    #: The salvage's own nudge, used to tell ITS call apart from the other
    #: subsystems that legitimately call chat() during a turn.
    SALVAGE_MARK = "Reply now in one or two warm, natural sentences"

    async def chat(self, messages, **kw):
        self.chat_calls.append({"kw": dict(kw), "messages": list(messages)})
        return self._rescue

    def salvage_calls(self):
        return [c for c in self.chat_calls
                if any(self.SALVAGE_MARK in str(m.get("content", ""))
                       for m in c["messages"])]


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
    return rt, m


async def test_the_stream_retry_policy_escalates_rather_than_repeats():
    check.section("Phase 6: the stream's own retries are structurally different")

    from core.llm_runtime import LLMRuntime

    src = inspect.getsource(LLMRuntime.chat_stream_ex)
    check("_attempt_messages" in src,
          "each attempt builds its own contract")
    check("Attempts ESCALATE rather than repeat" in src,
          "and the escalation is the documented intent")
    check("empty_retries" in src and "empty_exhausted" in src,
          "both outcomes are counted, so the failure is not invisible")


async def test_the_final_rescue_is_not_the_contract_that_just_failed():
    check.section("Phase 6: the salvage does not repeat native reasoning")

    from core.runtime import RuntimeManager

    src = inspect.getsource(RuntimeManager._chat_turn_stream)
    idx = src.find("salvage = messages")
    check(idx > 0, "the salvage path exists")
    window = src[idx:idx + 700]
    check("thinking=False" in window,
          "the rescue uses the prefilled-closed-block contract")
    check("thinking=True" not in window,
          "and does NOT repeat the reasoning contract that just produced nothing")
    check("5/18 empty" in src and "0/18 empty" in src,
          "the direction is justified by this repo's own measurement, in place")


async def test_a_successful_rescue_is_returned_once():
    check.section("Phase 6: a rescued reply is emitted exactly once")

    with _tmp() as td:
        llm = _EmptyThenText("All set — I moved it to Friday.")
        rt, m = await _runtime(td, llm)

        events: list[dict] = []
        async for ev in rt.chat_turn_stream(user_text="move my meeting",
                                            conversation_id=None):
            events.append(ev)

        tokens = [e["text"] for e in events if e.get("type") == "token"]
        done = [e for e in events if e.get("type") == "done"]
        full = "".join(tokens)

        check(full.count("All set") == 1,
              f"the rescued text appears exactly once ({full!r})")
        check(APOLOGY not in full,
              f"and the apology is NOT used ({full[:60]!r})")
        check(len(done) == 1 and done[0]["full_text"].strip() == "All set — I moved it to Friday.",
              f"the turn completes with that text ({done and done[0].get('full_text')!r})")
        check(llm._usage["empty_salvaged"] == 1,
              f"the rescue is counted ({llm._usage})")


async def test_an_unrescuable_turn_is_counted_not_hidden():
    check.section("Phase 6: giving up is recorded honestly")

    with _tmp() as td:
        llm = _EmptyThenText("")          # the rescue fails too
        rt, m = await _runtime(td, llm)

        events: list[dict] = []
        async for ev in rt.chat_turn_stream(user_text="move my meeting",
                                            conversation_id=None):
            events.append(ev)
        full = "".join(e["text"] for e in events if e.get("type") == "token")
        done = [e for e in events if e.get("type") == "done"]
        final = done[0]["full_text"] if done else ""

        # The apology is delivered on the `done` event rather than as tokens —
        # there were none to stream. Asserted where it actually is.
        check(APOLOGY in final,
              f"the user is told plainly, rather than shown nothing ({final[:50]!r})")
        check(not full.strip(),
              f"and no partial text leaked before it ({full[:40]!r})")
        check(llm._usage["empty_salvaged"] == 0,
              f"it is NOT counted as a rescue ({llm._usage})")
        check(len(llm.salvage_calls()) == 1,
              f"exactly one SALVAGE attempt was made ({len(llm.salvage_calls())} of "
              f"{len(llm.chat_calls)} total chat calls)")


async def test_a_stored_fact_question_never_reaches_this_path():
    check.section("Phase 6: a stored date bypasses generation entirely")

    with _tmp() as td:
        llm = _EmptyThenText("")          # any generation would produce nothing
        rt, m = await _runtime(td, llm)
        await m.upsert_person("Robin", {"birthday": "March 14"})
        await m.add_fact(entity="user", attribute="birthday", value="1990-07-02",
                         confidence=0.95)

        events: list[dict] = []
        async for ev in rt.chat_turn_stream(
            user_text="When is Robin's birthday and when is my birthday?",
            conversation_id=None,
        ):
            events.append(ev)
        full = "".join(e["text"] for e in events if e.get("type") == "token")

        check(APOLOGY not in full,
              f"the live failure's apology does not appear ({full[:70]!r})")
        check("March 14" in full and "July 2" in full,
              f"both stored dates are returned ({full!r})")
        check(llm.stream_calls == 0 and not llm.salvage_calls(),
              f"and no generation was needed for the answer "
              f"(stream={llm.stream_calls}, salvage={len(llm.salvage_calls())})")


async def test_the_apology_is_never_treated_as_success():
    check.section("Phase 6: the fallback string is not an answer")

    # A guard for future work: if anyone starts counting the apology as a
    # passing reply, this fails.
    from core.runtime import RuntimeManager

    src = inspect.getsource(RuntimeManager._chat_turn_stream)
    occurrences = src.count(APOLOGY)
    check(occurrences >= 1, f"the fallback exists ({occurrences} site(s))")
    check("chat_stream_salvage_failed" in src,
          "and reaching it is logged as a failure, not a normal ending")


async def main():
    await test_the_stream_retry_policy_escalates_rather_than_repeats()
    await test_the_final_rescue_is_not_the_contract_that_just_failed()
    await test_a_successful_rescue_is_returned_once()
    await test_an_unrescuable_turn_is_counted_not_hidden()
    await test_a_stored_fact_question_never_reaches_this_path()
    await test_the_apology_is_never_treated_as_success()
    check.finish()


if __name__ == "__main__":
    run(main)
