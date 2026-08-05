"""INTEGRATION (U10): a plain chat turn, end to end on a real backend.

Boots `backend.app` for real — memory, tool router, RuntimeManager, workers,
HTTP routes — and drives turns the way Marcus does. See tests/harness.py for
exactly what is real and what is substituted.

What this suite is here to catch (none of which a unit test can see):
  * a turn that returns nothing, or returns the model's raw plumbing
  * a turn that stops being recorded in conversation state or in SQLite
  * the /chat and /chat/stream routes drifting apart from the pipeline
  * the local model being driven CONCURRENTLY during one turn — one
    llama.cpp context cannot survive that (see test_it_gpu_serialization)
  * Nova's own per-turn overhead quietly growing into seconds
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Checks, boot, run

check = Checks()

REPLY = "That sounds like a solid evening. How did the kids take the news?"

# Nova's own work per turn, with the model answering instantly. This is a
# CEILING on overhead, not a model benchmark: memory reads, grounding, state
# writes. It caught nothing when written — its job is to fail the day someone
# reintroduces a serial fan-out on the hot path.
OVERHEAD_BUDGET_S = 3.0


async def main() -> None:
    async with boot(default_reply=REPLY) as nova:
        conv = uuid4()

        check.section("A plain chat turn")
        t0 = time.perf_counter()
        res = await nova.say("we all had dinner together and the kids actually helped clean up",
                             conversation_id=conv)
        elapsed = time.perf_counter() - t0

        check(res.assistant_text.strip() == REPLY, f"returns the reply text ({res.assistant_text[:40]!r})")
        check(res.tool_calls == [], "a chat turn calls no tools")
        check(res.conversation_id == conv, "stays in the conversation it was given")
        check("<think" not in res.assistant_text, "no reasoning plumbing leaks into the reply")
        check(elapsed < OVERHEAD_BUDGET_S, f"turn overhead {elapsed:.2f}s < {OVERHEAD_BUDGET_S}s budget")
        check(nova.llm.max_concurrent == 1,
              f"the local model is never driven concurrently within a turn (peak={nova.llm.max_concurrent})")

        check.section("The turn is remembered")
        recent = await nova.runtime.state_store.recent_chat_text(conv)
        check("kids actually helped clean up" in recent, "conversation state kept the user message")
        check(REPLY[:20] in recent, "conversation state kept Nova's reply")

        # The memory-ingest WORKER persists to SQLite off the turn; poll it.
        persisted: list[str] = []
        for _ in range(40):
            rows = await nova.memory._sqlite.recent_turns(conv, limit=20)
            persisted = [str(r.get("content") or "") for r in rows]
            if any("helped clean up" in c for c in persisted):
                break
            await asyncio.sleep(0.25)
        check(any("helped clean up" in c for c in persisted),
              "the background memory worker persisted the turn to SQLite")

        check.section("The same turn over real HTTP")
        nova.llm.reset_calls()
        r = await nova.http.post("/chat", json={"message": "morning — what's on today?"})
        check(r.status_code == 200, f"POST /chat -> {r.status_code}")
        body = r.json() if r.status_code == 200 else {}
        check((body.get("assistant") or "").strip() == REPLY, "/chat returns the assistant text")
        check(bool(body.get("conversation_id")), "/chat returns a conversation id")

        check.section("Streaming shares the same pipeline")
        chunks: list[str] = []
        done_seen = False
        async with nova.http.stream("POST", "/chat/stream", json={"msg": "tell me something good"}) as resp:
            check(resp.status_code == 200, f"POST /chat/stream -> {resp.status_code}")
            async for line in resp.aiter_lines():
                if line.startswith("event: done"):
                    done_seen = True
                elif line.startswith("data:"):
                    chunks.append(line[5:].strip())
        streamed = " ".join(chunks)
        check(done_seen, "the stream terminates with a done event")
        check(REPLY[:20] in streamed, "the streamed text carries the reply")

        check.section("An empty message is a client error, not a crash")
        r = await nova.http.post("/chat", json={"message": "   "})
        check(r.status_code == 422, f"blank message -> 422 (got {r.status_code})")

        check.section("A broken memory capture is LOUD, not silent")
        # Regression: the four capture pre-passes each sat in `except: pass`.
        # A capture that started raising would stop recording facts, lessons
        # and mood forever with no signal — which is exactly how a NameError
        # in _capture_mood survived an entire development round. The turn must
        # still succeed (capture is best-effort) AND the failure must surface.
        # Asserted at the logger call, not via a log handler: the harness pins
        # NOVA_LOG_LEVEL=ERROR and the codebase logs through structlog, so a
        # stdlib handler would see nothing even when the code is correct.
        import core.runtime as _rt_mod

        warnings: list[tuple] = []
        real_warning = _rt_mod.logger.warning
        _rt_mod.logger.warning = lambda *a, **kw: warnings.append((a, kw))

        rt = nova.runtime
        original = rt._capture_mood

        async def _boom(_text):
            raise RuntimeError("simulated capture failure")

        rt._capture_mood = _boom
        try:
            res = await nova.say("the kids built a fort today", conversation_id=uuid4())
            check(res.assistant_text.strip() != "", "the turn still succeeds when a capture breaks")
            reported = [w for w in warnings if "memory_capture_failed" in str(w)]
            check(bool(reported), f"the broken capture is reported, not swallowed ({len(warnings)} warning(s))")
            check(any("mood" in str(w) for w in reported), "the report names WHICH capture failed")
            check(any("simulated capture failure" in str(w) for w in reported),
                  "the report carries the real error text")
        finally:
            rt._capture_mood = original
            _rt_mod.logger.warning = real_warning

        check.section("Shutdown stops EVERY worker")
        # Two regressions live here, both invisible without a real boot:
        #
        # 1. Five workers let the CancelledError from their own `task.cancel()`
        #    escape `stop()` (it is a BaseException, so `except Exception`
        #    misses it). RuntimeManager.stop() then aborted on the first one
        #    and never stopped the rest — a leaked worker per restart.
        # 2. Cancelling immediately killed the memory-ingest worker INSIDE an
        #    aiosqlite transaction, which leaks aiosqlite's non-daemon
        #    connection thread. The process then finishes everything and never
        #    exits: `threading._shutdown()` joins that thread forever. It hit
        #    about 1 run in 6 — a hang, not a failure, so nothing caught it.
        for _ in range(3):  # give the ingest worker real in-flight work
            await nova.say("one more thing before bed", conversation_id=uuid4())
        rt = nova.runtime
        try:
            await rt.stop()
            stopped_cleanly = True
        except BaseException as e:  # noqa: BLE001 — the bug raised CancelledError
            stopped_cleanly = False
            print(f"       stop() raised {type(e).__name__}: {e}")
        check(stopped_cleanly, "RuntimeManager.stop() completes without raising")

        workers = {
            "memory-ingest": rt._memory_worker._task,
            "self-improve-capture": rt._self_improve._capture_task,
            "reminders": rt._reminder_worker._task,
            "research": rt._research_worker._task,
        }
        for name, task in workers.items():
            check(task is None or task.done(), f"worker '{name}' is stopped")

        import threading

        import aiosqlite

        leaked = [t for t in threading.enumerate()
                  if isinstance(t, aiosqlite.core.Connection) and t.is_alive()]
        check(not leaked,
              f"no aiosqlite connection thread survives shutdown ({len(leaked)} alive)")

    check.finish()


run(main)
