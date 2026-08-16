from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone

from core.event_bus import BUS, clip
from core.events import MemoryIngestEvent, SummarizeHintEvent
from core.mood import emotional_salience
from core.policy._json_extract import extract_first_json_object
from core.logging_setup import get_logger
from core.policy.memory_extractor import MemoryExtractorLLM
from core.policy.summarizer import SummarizerLLM
from core.conversation_state import ConversationStateStore
from core.turn_gate import GATE
from core.turn_identity import (TurnIdentity, active_turn, remap_entity_for,
                                turn_speaker_label)
from core.workers.lifecycle import log_worker_error, stop_worker
from memory.unifier import MemoryUnifier


logger = get_logger(__name__)


def _conv_entity(conversation_id) -> str:
    """Conversation-local durable entity, scoped to the current speaker.

    Mirrors core.runtime._conv_entity; the owner's key is unchanged so no
    existing summary or digest is orphaned.
    """
    from core.turn_identity import (OWNER_ENTITY, UNVERIFIED_SCOPE,
                                    conversation_scope)

    scope = conversation_scope()
    base = f"conversation:{conversation_id}"
    if scope == OWNER_ENTITY:
        return base
    if scope == UNVERIFIED_SCOPE:
        return f"{UNVERIFIED_SCOPE}:{base}"
    return f"{scope}:{base}"


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
                    await self._sq.put(SummarizeHintEvent(
                        conversation_id=ev.conversation_id, timestamp=_now(),
                        reason="periodic", identity=ev.identity))
                except Exception:
                    pass

            await self._drain_summarize_hints(max_items=1)

    async def _handle_ingest(self, ev: MemoryIngestEvent) -> None:
        # Re-enter the speaker's identity for the whole of this event. It comes
        # off the event, never off this task's ContextVar: this worker runs long
        # after the turn ended and would otherwise see the typed default and
        # quietly write a guest's evening into Marcus's memory.
        ident = ev.identity or TurnIdentity.typed()
        with active_turn(ident):
            await self._ingest_scoped(ev, ident)

    async def _ingest_scoped(self, ev: MemoryIngestEvent, ident: TurnIdentity) -> None:
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
            # How charged was the moment this was learned in? Emotion is the
            # clearest natural signal for what should outlast what, and it was
            # already being computed per turn and discarded. A fact picked up
            # while Marcus was excited or upset now decays far more slowly than
            # one mentioned in passing. 0.0 for ordinary messages, which is
            # most of them — and that's the point.
            moment = emotional_salience(user_text)
            out = await self._extractor.extract(user_text=user_text)
            for f in out.facts:
                if not f.persist:
                    continue
                if f.confidence < 0.55:
                    continue
                # The extractor speaks in first person and always says
                # entity="user". Decide whose "user" that was before writing.
                entity = remap_entity_for(f.entity, ident)
                if entity is None:
                    logger.debug("memory_extract_suppressed_unverified",
                                 attribute=str(f.attribute)[:40])
                    continue
                # Retire anything this contradicts BEFORE writing it, so the
                # two never coexist. Skipped for singleton attributes, which
                # already supersede by key and need no model call.
                if not self._memory._is_singleton_fact(entity, f.attribute):
                    await self._reconcile(entity=entity, attribute=f.attribute,
                                          value=f.value, ident=ident)
                await self._memory.add_fact(
                    entity=entity, attribute=f.attribute, value=f.value,
                    confidence=float(f.confidence),
                    # None lets the unifier's default stand for unremarkable
                    # moments, so identity facts keep their own high floor
                    # instead of being dragged down by a flat 0.0.
                    salience=(moment if moment > 0 else None),
                )

        # also persist policy-provided facts (already validated) if any
        for mf in (ev.policy_memory_facts or []):
            try:
                entity = str(mf.get("entity") or "").strip()
                attribute = str(mf.get("attribute") or "").strip()
                value = str(mf.get("value") or "").strip()
                conf = float(mf.get("confidence") or 0.7)
                persist = bool(mf.get("persist", True))
                # Same routing as the extractor. These come from the policy
                # layer rather than a model, but they describe the same speaker.
                target = remap_entity_for(entity, ident) if entity else None
                if target and attribute and value and persist:
                    await self._memory.add_fact(entity=target, attribute=attribute, value=value, confidence=conf)
            except Exception:
                continue

    async def _reconcile(self, *, entity: str, attribute: str, value: str,
                         ident: TurnIdentity | None = None) -> None:
        """Retire anything the new fact contradicts, before writing it.

        Singleton attributes already supersede by key, so this covers the case
        that leaked: free-form facts where nothing links the two. "I love
        running" and, months later, "I hate running" both sat in memory as
        equally current, and recall surfaced whichever happened to score higher.

        Deliberately conservative. Losing a real memory is much worse than
        keeping a stale one, so a fact is retired only on an unambiguous
        CONTRADICTS verdict — REFINES, DUPLICATE and UNRELATED all leave it be.
        Entirely best-effort: any failure means the new fact is simply added,
        which is exactly the old behavior.
        """
        try:
            candidates = await self._memory.find_conflict_candidates(entity=entity, value=value)
        except Exception:
            return
        if not candidates:
            return

        listed = "\n".join(f"{i + 1}. {c.value[:200]}" for i, c in enumerate(candidates))
        # Whose beliefs are being reconciled? The prompt used to assert Marcus
        # unconditionally, which told the judge that a guest's statement was his
        # and invited it to retire his real facts as "out of date".
        who = turn_speaker_label(ident or TurnIdentity.typed())
        prompt = (
            f"{who} just told Nova something new. Decide whether it CONTRADICTS anything she "
            f"already believes about {who}, so the outdated belief can be retired.\n\n"
            f"NEW: {value[:300]}\n\nEXISTING:\n{listed}\n\n"
            "For each existing item choose one:\n"
            "  CONTRADICTS - cannot both be true now; the old one is out of date\n"
            "  REFINES     - the new one adds detail; both stay\n"
            "  DUPLICATE   - same thing said again\n"
            "  UNRELATED   - different subject\n\n"
            "Changing tastes and circumstances CONTRADICT (loved running -> hates running; lived in "
            "Austin -> lives in Dallas). Two things that can both be true at once do NOT — liking "
            "two foods, having two hobbies, knowing two people.\n\n"
            'Reply ONLY with JSON: {"verdicts": [{"n": 1, "verdict": "CONTRADICTS"}, ...]}'
        )
        try:
            raw = await self._llm_judge(prompt)
        except Exception as e:  # noqa: BLE001
            # Best-effort by design, but say so: a silent return here is how
            # this whole feature looked "working" while doing nothing.
            logger.debug("reconcile_judge_failed", error=str(e)[:160])
            return
        obj = extract_first_json_object(raw or "") or {}
        verdicts = obj.get("verdicts") if isinstance(obj, dict) else None
        if not isinstance(verdicts, list):
            return

        retire: list[str] = []
        for v in verdicts:
            if not isinstance(v, dict):
                continue
            if str(v.get("verdict") or "").strip().upper() != "CONTRADICTS":
                continue
            try:
                idx = int(v.get("n")) - 1
            except Exception:
                continue
            if 0 <= idx < len(candidates):
                retire.append(str(candidates[idx].id))
        if retire:
            n = await self._memory.supersede_facts(old_ids=retire, reason=f"contradicted by: {value[:120]}")
            if n:
                logger.info("memory_contradiction_resolved", retired=n, new_value=value[:80])
                BUS.publish("memory.superseded", {"retired": n, "because": clip(value, 120)})

    async def _llm_judge(self, prompt: str) -> str:
        """Small judgement call on the model behind the extractor.

        Borrows the extractor's model AND its semaphore — that semaphore is
        the shared GPU permit (core/gpu.py), so skipping it would drive
        llama.cpp concurrently with a live turn.
        """
        async with self._extractor._sem:
            return await self._extractor._llm.chat(
                [{"role": "user", "content": prompt}], max_tokens=220, temperature=0.0, thinking=False
            )

    async def _summarize_one(self, hint: SummarizeHintEvent) -> None:
        """Summarise one conversation for whoever the hint belongs to."""
        transcript = await self._state.recent_chat_text(hint.conversation_id)
        if not transcript:
            return
        # Same yield as extraction: summarization is the single most expensive
        # background call (a whole transcript) and fires every 8 turns, which is
        # exactly the periodic "why was THAT one so slow?" spike.
        await GATE.wait_for_idle(what="summarize")
        s = await self._summarizer.summarize(transcript=transcript)
        if not s.summary.strip():
            return
        base = _conv_entity(hint.conversation_id)
        # Rolling "right now" summary (singleton, overwrites).
        await self._memory.add_fact(
            entity=base, attribute="summary",
            value=s.summary.strip(), confidence=0.75,
        )
        # Dated digest that ACCUMULATES across days, so history isn't destroyed
        # on the next summarization — this is what makes "what did we talk about
        # last Tuesday" answerable.
        day = _now().strftime("%Y-%m-%d")
        await self._memory.add_fact(
            entity=f"{base}:digest", attribute=day,
            value=f"[{day}] {s.summary.strip()}", confidence=0.75,
        )

    async def _drain_summarize_hints(self, *, max_items: int = 2) -> None:
        for _ in range(max_items):
            if self._stop.is_set():
                return
            try:
                hint = self._sq.get_nowait()
            except Exception:
                return
            try:
                # Re-enter the speaker for the WHOLE hint (V3 P5.1 closure).
                # This worker drains off the turn path, so without the snapshot
                # `recent_chat_text` would return the owner's transcript and the
                # digest would land under his entity — summarising a shared
                # conversation as one person's history, which is precisely the
                # cross-speaker channel this closure removes.
                with active_turn(hint.identity or TurnIdentity.typed()):
                    await self._summarize_one(hint)
            except Exception as e:  # noqa: BLE001
                logger.debug("summarize_failed", error=str(e))
            finally:
                with contextlib.suppress(Exception):
                    self._sq.task_done()
