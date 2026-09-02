"""Event capture for Stage 15, attributed by payload rather than by position.

WHY THIS FILE EXISTS. Several Stage 15 suites captured events like this:

    n0 = len(BUS.recent(900))
    ...
    new = BUS.recent(900)[n0:]

`BUS.recent(n)` reads a deque whose maxlen is 100. Asking for 900 returns at
most 100, so `n0` stops being a position in a growing list the moment a turn
publishes more than a hundred events -- which a project build does easily. The
slice then silently selects the wrong window, and an assertion like "no
permission was requested" can pass because it was looking at the wrong events
entirely.

That is exactly the "never attribute events by list position" rule, broken by
the person who wrote the rule down. The fix is a real subscription: a queue fed
by the bus from the moment the recorder is entered.

Subscriber queues hold 256 and drop the OLDEST when full, so anything waiting
through a long operation must drain as it goes -- `pump()` does that.
"""

from __future__ import annotations

import asyncio
from typing import Any

from core.event_bus import BUS


class Recorder:
    """Every event published while this is open, in order, never by index."""

    def __init__(self) -> None:
        self._q: asyncio.Queue | None = None
        self.events: list[Any] = []

    def __enter__(self) -> "Recorder":
        self._q = BUS.subscribe()
        return self

    def __exit__(self, *exc) -> None:
        self.drain()
        if self._q is not None:
            BUS.unsubscribe(self._q)
        self._q = None

    def drain(self) -> list[Any]:
        if self._q is None:
            return self.events
        while True:
            try:
                self.events.append(self._q.get_nowait())
            except asyncio.QueueEmpty:
                return self.events

    async def pump(self, seconds: float, *, every: float = 0.05) -> None:
        """Wait, draining as we go, so a burst cannot overflow the queue."""
        steps = max(1, int(seconds / every))
        for _ in range(steps):
            self.drain()
            await asyncio.sleep(every)
        self.drain()

    # ── attribution helpers: all by PAYLOAD ────────────────────────────────

    def of(self, kind: str) -> list[Any]:
        self.drain()
        return [e for e in self.events if e.type == kind]

    def for_project(self, kind: str, slug: str) -> list[Any]:
        return [e for e in self.of(kind)
                if str(e.data.get("project") or "") == slug]

    def projects_in(self, kind: str) -> list[str]:
        return [str(e.data.get("project") or "") for e in self.of(kind)]

    def targets_of(self, kind: str) -> list[str]:
        """The `details.project` of each event of this kind (permissions)."""
        return [str((e.data.get("details") or {}).get("project") or "")
                for e in self.of(kind)]

    def kinds(self) -> dict[str, int]:
        self.drain()
        out: dict[str, int] = {}
        for e in self.events:
            out[e.type] = out.get(e.type, 0) + 1
        return out
