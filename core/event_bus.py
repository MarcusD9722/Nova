from __future__ import annotations

"""Nova's unified event bus.

Any backend component can publish structured events; the frontend subscribes
via the /ws/events WebSocket (see backend/app.py). Events power the UI's
visual activity states (thinking, tool use, memory access, vision, speech).

Design constraints:
- publish() must never raise or block the caller.
- Event payloads must not contain secrets or full prompts; keep data small
  and truncate free text (see _clip).
"""

import asyncio
import itertools
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(text: Any, limit: int = 240) -> str:
    s = str(text or "")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _publisher_identity():
    """The speaker in scope at publish time, or None off the turn path.

    `current_identity_or_none()`, NOT `current_identity()`: the latter has a
    legacy default of typed Marcus, so every background publish — a finished
    build, a recurring failure, a scheduled job — was snapshotted as though he
    had said it (V3 P5.1e.1). None here means "no human was involved", which is
    a different and true thing.
    """
    try:
        from core.turn_identity import current_identity_or_none
        return current_identity_or_none()
    except Exception:  # noqa: BLE001 - the bus never raises
        return None


@dataclass
class NovaEvent:
    seq: int
    type: str
    ts: str
    data: dict[str, Any] = field(default_factory=dict)
    #: Who was speaking when this was published (V3 P5.1e).
    #:
    #: Stamped at PUBLISH, because that is the last moment the speaker is still
    #: in scope. Subscribers drain on their own tasks — the episodic promoter
    #: literally says "this drains in its own task" — so a consumer calling
    #: `current_identity()` reads the typed default and files a guest's
    #: correction as Marcus's. Same lesson as MemoryIngestEvent (D12), one layer
    #: down: identity crosses a queue by snapshot, never by inheritance.
    #:
    #: Deliberately NOT in `to_dict()`: the bus feeds an SSE debug stream, and
    #: this is internal routing rather than something to broadcast.
    identity: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "type": self.type, "ts": self.ts, "data": self.data}


class EventBus:
    """In-process pub/sub with a bounded replay buffer."""

    def __init__(self, history: int = 100, queue_size: int = 256) -> None:
        self._seq = itertools.count(1)
        self._history: deque[NovaEvent] = deque(maxlen=history)
        self._subscribers: set[asyncio.Queue[NovaEvent]] = set()
        self._queue_size = queue_size
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Bind the server's event loop so worker threads can publish safely."""
        try:
            self._loop = loop or asyncio.get_running_loop()
        except Exception:
            pass

    def publish(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """Fire-and-forget publish. Never raises. Safe from worker threads."""
        try:
            event = NovaEvent(seq=next(self._seq), type=str(event_type), ts=_now_iso(),
                              data=dict(data or {}), identity=_publisher_identity())

            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None

            # asyncio queues are not thread-safe: from a foreign thread, hand
            # the fan-out to the bound loop instead of touching queues directly.
            if running is None and self._loop is not None and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._fanout, event)
            else:
                self._fanout(event)
        except Exception:
            pass

    def _fanout(self, event: NovaEvent) -> None:
        try:
            self._history.append(event)
            for q in list(self._subscribers):
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    # Slow consumer: drop its oldest event to make room.
                    try:
                        q.get_nowait()
                        q.put_nowait(event)
                    except Exception:
                        pass
        except Exception:
            pass

    def subscribe(self) -> asyncio.Queue[NovaEvent]:
        q: asyncio.Queue[NovaEvent] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[NovaEvent]) -> None:
        self._subscribers.discard(q)

    def recent(self, limit: int = 50) -> list[NovaEvent]:
        items = list(self._history)
        return items[-limit:]


# Global bus shared across the backend process.
BUS = EventBus()


def clip(text: Any, limit: int = 240) -> str:
    """Public helper so publishers keep payloads small."""
    return _clip(text, limit)
