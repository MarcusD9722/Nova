from __future__ import annotations

"""Durable episode persistence, off the conversational critical path (V3 P4.1).

P4 built the store; this is the thing that actually feeds it during a real
conversation. It exists as a worker rather than an inline `await` for one
measured reason: a persisted result set costs roughly 16 ms per artifact row,
and a five-item result set would therefore put ~100 ms of SQLite writes between
Marcus finishing his sentence and Nova starting her reply. P2.5 spent a whole
phase getting that number down to ~130 ms total. Spending it back on bookkeeping
would be indefensible.

So the turn path only enqueues, and everything else happens here.

Two properties matter more than throughput:

* **Nothing accepted is silently lost.** `MemoryIngestWorker` learned this the
  hard way — a worker that sets its stop event first and drains second discards
  the most recent turns, invisibly, because the turn itself succeeded. This one
  drains before stopping, bounds the drain, and logs when the drain is
  incomplete rather than pretending it finished.
* **One logical event produces one episode.** Background delivery means the same
  turn can be observed twice (a retry, a duplicated publish, a restart around an
  accepted event). The episode id is derived from the artifact id, which is
  generated once at capture, so a repeat is an idempotent overwrite rather than
  a second row.
"""

import asyncio
from typing import Any

from core.events import EpisodicPersistEvent
from core.logging_setup import get_logger
from core.workers.lifecycle import log_worker_error, stop_worker
from memory.decision_seed import ensure_seeded
from memory.episodes import Episode, EpisodicStore

logger = get_logger(__name__)

#: Entity tokens worth keeping on the warm row. The warm record has to be
#: enough to judge relevance WITHOUT loading evidence, and the item titles are
#: what a later query actually matches against.
_MAX_ENTITIES = 12


def episode_id_for(artifact_id: str) -> str:
    """Stable episode identity, derived rather than generated.

    This is the whole duplicate-safety story: `uuid4()` here would make every
    redelivery a new episode.
    """
    return f"ep-{artifact_id}"


def _entities_for(artifact: Any, children: list[Any]) -> list[str]:
    """What a later query will actually match against.

    Two shapes reach here. A tool result set has a query and ordered children,
    and the child titles are the answer to "what did we look at". An MCP result
    has neither — it carries `{server, tool, args, schema_hash}` and no items —
    so its arguments are the only searchable content it has. Without them an
    MCP episode is findable only by the server and tool name, which is not how
    anyone asks about it.
    """
    prov = artifact.provenance or {}
    out: list[str] = []

    query = str(prov.get("query") or "").strip()
    if query:
        out.append(query)

    for value in (prov.get("args") or {}).values() if isinstance(prov.get("args"), dict) else ():
        text = str(value).strip()[:80]
        if text and text not in out:
            out.append(text)

    for child in children[:_MAX_ENTITIES]:
        title = str(getattr(child, "title", "") or "").strip()
        if title and title not in out:
            out.append(title)
    return out[:_MAX_ENTITIES]


class EpisodicIngestWorker:
    """Drains accepted episodes into the warm tier."""

    def __init__(
        self,
        *,
        store: EpisodicStore,
        queue: "asyncio.Queue[EpisodicPersistEvent]",
        memory: Any = None,
        seed_decisions: bool = True,
    ) -> None:
        self._store = store
        self._q = queue
        # Only used to guarantee the schema exists before the first write —
        # the episodic tables are created by SQLiteMemoryBackend.initialize().
        self._memory = memory
        self._seed = bool(seed_decisions)
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.stats: dict[str, Any] = {
            "queued": 0, "persisted": 0, "dropped": 0, "failed": 0,
            "artifacts": 0, "last_error": None,
        }

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run())
        logger.info("episodic_ingest_worker_started")

    async def stop(self) -> None:
        # Drain BEFORE signalling stop, for the same reason MemoryIngestWorker
        # does: the loop exits at the top once _stop is set, so anything still
        # queued would be dropped after Nova had already accepted it.
        await self._drain_queue_for_shutdown()
        self._stop.set()
        await stop_worker(self._task, name="episodic-ingest")

    async def _drain_queue_for_shutdown(self, *, budget_s: float = 10.0) -> None:
        if self._q.empty() or self._task is None or self._task.done():
            return
        try:
            await asyncio.wait_for(self._q.join(), timeout=budget_s)
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning("episodic_drain_incomplete", pending=self._q.qsize())
        except Exception as e:  # noqa: BLE001
            log_worker_error(logger, "episodic_drain_failed", e)

    # -- accepting work -------------------------------------------------------

    def submit(self, event: EpisodicPersistEvent) -> bool:
        """Accept an episode for durable storage. Never blocks, never raises.

        A full queue drops the episode and says so. Historical memory is
        optional; the reply Marcus is waiting on is not.
        """
        try:
            self._q.put_nowait(event)
        except asyncio.QueueFull:
            self.stats["dropped"] += 1
            logger.warning("episodic_queue_full", depth=self._q.qsize())
            return False
        except Exception as e:  # noqa: BLE001
            self.stats["dropped"] += 1
            logger.warning("episodic_enqueue_failed", error=str(e)[:200])
            return False
        self.stats["queued"] += 1
        return True

    # -- the loop -------------------------------------------------------------

    async def _run(self) -> None:
        # The episodic tables are created by the memory backend's schema, so the
        # first write has to happen after initialization. Idempotent.
        if self._memory is not None:
            try:
                await self._memory.initialize()
            except Exception as e:  # noqa: BLE001
                log_worker_error(logger, "episodic_memory_init_failed", e)
        if self._seed:
            try:
                await ensure_seeded(self._store)
            except Exception as e:  # noqa: BLE001
                # A seeding failure must not stop episode persistence: decisions
                # are a nice-to-have, the user's own history is not.
                log_worker_error(logger, "decision_seed_failed", e)

        while not self._stop.is_set():
            try:
                ev = await asyncio.wait_for(self._q.get(), timeout=0.5)
            except (TimeoutError, asyncio.TimeoutError):
                continue
            except asyncio.CancelledError:
                return

            try:
                await self._persist(ev)
                self.stats["persisted"] += 1
            except Exception as e:  # noqa: BLE001
                self.stats["failed"] += 1
                self.stats["last_error"] = str(e)[:200]
                log_worker_error(logger, "episodic_persist_failed", e)
            finally:
                self._q.task_done()

    async def _persist(self, ev: EpisodicPersistEvent) -> None:
        art = ev.artifact
        children = list(ev.children or [])
        ep_id = episode_id_for(art.artifact_id)

        # Provenance is carried through structurally, not flattened into prose.
        # `artifact_id` is what later makes cross-session ordinal resolution
        # possible: it is the handle back to the ordered children.
        provenance = dict(art.provenance or {})
        provenance["artifact_id"] = art.artifact_id
        provenance["turn_id"] = ev.turn_id
        if ev.user_text:
            provenance["asked"] = ev.user_text[:200]

        episode = Episode(
            id=ep_id,
            kind=ev.kind,
            summary=art.summary,
            entities=_entities_for(art, children),
            conversation_id=str(ev.conversation_id),
            project=ev.project,
            source_tool=art.source_tool or None,
            # Verbatim. Nothing in this worker may raise a trust class — a web
            # result is no more trustworthy for having reached a background
            # queue.
            trust=art.trust,
            freshness=art.freshness,
            provenance=provenance,
            outcome=ev.reason or None,
            importance=float(ev.importance),
            created_at=ev.timestamp.isoformat(),
        )
        # Episode and evidence in ONE transaction. The ordered children are the
        # point of persisting at all — without them "the second one" has nothing
        # to count — and an episode that survived a crash its result set did not
        # would promise exactly that and fail to deliver it.
        n = await self._store.record_happening(episode, art, children)
        self.stats["artifacts"] += max(0, n - 1)

    def status(self) -> dict[str, Any]:
        return {**self.stats, "queue_depth": self._q.qsize(),
                "running": bool(self._task and not self._task.done())}
