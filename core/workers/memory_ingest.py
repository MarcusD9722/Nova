from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone

from core.events import MemoryIngestEvent, SummarizeHintEvent
from core.logging_setup import get_logger
from core.policy.memory_extractor import MemoryExtractorLLM
from core.policy.summarizer import SummarizerLLM
from core.conversation_state import ConversationStateStore
from core.turn_gate import GATE
from core.workers.lifecycle import log_worker_error, stop_worker
from memory.unifier import MemoryUnifier


logger = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MemoryIngestWorker:
    def __init__(
        self,
        *,
        memory: MemoryUnifier,
        extractor: MemoryExtractorLLM,
        summarizer: SummarizerLLM,
        state: ConversationStateStore,
        queue: "asyncio.Queue[MemoryIngestEvent]",
        summarize_queue: "asyncio.Queue[SummarizeHintEvent]",
        summary_every_n: int = 8,
    ) -> None:
        self._memory = memory
        self._extractor = extractor
        self._summarizer = summarizer
        self._state = state
        self._q = queue
        self._sq = summarize_queue
        self._summary_every_n = int(summary_every_n)
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._turn_counter: dict[str, int] = {}

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run())
        logger.info("memory_ingest_worker_started")

    async def stop(self) -> None:
        # Drain what is already queued BEFORE signalling stop. These are the
        # most recent things Marcus said; the loop exits at the top once
        # _stop is set, so anything still queued would be discarded and never
        # reach long-term memory — invisibly, since the turn itself succeeded.
        await self._drain_queue_for_shutdown()
        self._stop.set()
        # Graceful first: this worker writes turns to SQLite, and cancelling it
        # mid-transaction both loses the write and leaks aiosqlite's non-daemon
        # connection thread (see core/workers/lifecycle.py).
        await stop_worker(self._task, name="memory-ingest")

    async def _drain_queue_for_shutdown(self, *, budget_s: float = 10.0) -> None:
        """Wait (briefly) for the in-flight backlog to be ingested."""
        if self._q.empty() or self._task is None or self._task.done():
            return
        try:
            await asyncio.wait_for(self._q.join(), timeout=budget_s)
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning("memory_ingest_drain_incomplete", pending=self._q.qsize())
        except Exception as e:  # noqa: BLE001
            log_worker_error(logger, "memory_ingest_drain_failed", e)

    async def _run(self) -> None:
        await self._memory.initialize()
        while not self._stop.is_set():
            try:
                ev = await asyncio.wait_for(self._q.get(), timeout=0.5)
            except TimeoutError:
                await self._drain_summarize_hints(max_items=1)
                continue
            except asyncio.CancelledError:
                return

            try:
                await self._handle_ingest(ev)
            except Exception as e:  # noqa: BLE001
                log_worker_error(logger, "memory_ingest_failed", e)
            finally:
                self._q.task_done()

            # opportunistically summarize
            cid = str(ev.conversation_id)
            self._turn_counter[cid] = self._turn_counter.get(cid, 0) + 1
            if self._turn_counter[cid] % self._summary_every_n == 0:
                try:
                    await self._sq.put(SummarizeHintEvent(conversation_id=ev.conversation_id, timestamp=_now(), reason="periodic"))
                except Exception:
                    pass

            await self._drain_summarize_hints(max_items=1)

    async def _handle_ingest(self, ev: MemoryIngestEvent) -> None:
        user_text = (ev.user_message or "").strip()
        assistant_text = (ev.assistant_message or "").strip()

        if user_text:
            await self._memory.ingest_turn(ev.conversation_id, "user", user_text)
        if assistant_text:
            await self._memory.ingest_turn(ev.conversation_id, "assistant", assistant_text)

        # LLM-powered extraction (explicit-only).
        #
        # Yield to any live turn first. This call and the reply Marcus is
        # waiting on contend for the SAME 1-permit GPU semaphore, and the
        # semaphore is fair rather than prioritized — so a turn arriving while
        # this runs simply waits behind it. Measured: 3.2s and 6.1s for turns
        # with no background work, 41.2s for one that collided with extraction
        # plus the summarizer.
        if user_text:
            await GATE.wait_for_idle(what="memory_extract")
            out = await self._extractor.extract(user_text=user_text)
            for f in out.facts:
                if not f.persist:
                    continue
                if f.confidence < 0.55:
                    continue
                await self._memory.add_fact(entity=f.entity, attribute=f.attribute, value=f.value, confidence=float(f.confidence))

        # also persist policy-provided facts (already validated) if any
        for mf in (ev.policy_memory_facts or []):
            try:
                entity = str(mf.get("entity") or "").strip()
                attribute = str(mf.get("attribute") or "").strip()
                value = str(mf.get("value") or "").strip()
                conf = float(mf.get("confidence") or 0.7)
                persist = bool(mf.get("persist", True))
                if entity and attribute and value and persist:
                    await self._memory.add_fact(entity=entity, attribute=attribute, value=value, confidence=conf)
            except Exception:
                continue

    async def _drain_summarize_hints(self, *, max_items: int = 2) -> None:
        for _ in range(max_items):
            if self._stop.is_set():
                return
            try:
                hint = self._sq.get_nowait()
            except Exception:
                return
            try:
                transcript = await self._state.recent_chat_text(hint.conversation_id)
                if transcript:
                    # Same yield as extraction: summarization is the single
                    # most expensive background call (a whole transcript) and
                    # fires every 8 turns, which is exactly the periodic
                    # "why was THAT one so slow?" spike.
                    await GATE.wait_for_idle(what="summarize")
                    s = await self._summarizer.summarize(transcript=transcript)
                    if s.summary.strip():
                        # Rolling "right now" summary (singleton, overwrites).
                        await self._memory.add_fact(
                            entity=f"conversation:{hint.conversation_id}",
                            attribute="summary",
                            value=s.summary.strip(),
                            confidence=0.75,
                        )
                        # Dated digest that ACCUMULATES across days, so history
                        # isn't destroyed on the next summarization — this is what
                        # makes "what did we talk about last Tuesday" answerable.
                        day = _now().strftime("%Y-%m-%d")
                        await self._memory.add_fact(
                            entity=f"conversation:{hint.conversation_id}:digest",
                            attribute=day,
                            value=f"[{day}] {s.summary.strip()}",
                            confidence=0.75,
                        )
            except Exception as e:  # noqa: BLE001
                logger.debug("summarize_failed", error=str(e))
            finally:
                with contextlib.suppress(Exception):
                    self._sq.task_done()
