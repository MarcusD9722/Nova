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


@dataclass
class NovaEvent:
    seq: int
    type: str
    ts: str
    data: dict[str, Any] = field(default_factory=dict)

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
            event = NovaEvent(seq=next(self._seq), type=str(event_type), ts=_now_iso(), data=dict(data or {}))

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
