from __future__ import annotations

"""Foreground priority for the single GPU.

Nova has ONE llama.cpp context behind a 1-permit semaphore, and background
memory work (fact extraction after every turn, summarization every 8) competes
for it with the reply Marcus is waiting on. Measured on the live machine:

    warm turn, 3 LLM calls   ->   3.2s
    warm turn, 3 LLM calls   ->   6.1s
    warm turn, 6 LLM calls   ->  41.2s   <- background extraction + summarizer

The semaphore is fair, not prioritized, so a turn that arrives while the
extractor holds it simply waits. That is the 30-60s "she thinks for ages even
on a simple reply".

This gate does not preempt — an in-flight llama.cpp call cannot be interrupted
— it stops background work from STARTING while a turn is in flight. Since a
turn is short and extraction happens right after it, that removes almost all
of the overlap.

Starvation is explicitly bounded: background callers wait at most
`max_wait_s`, then proceed anyway. Nova must still learn from a long
conversation, just not at the cost of every reply in it.
"""

import asyncio
import os

from core.logging_setup import get_logger

logger = get_logger(__name__)


def _max_wait_s() -> float:
    try:
        return max(0.0, float(os.getenv("NOVA_BACKGROUND_YIELD_MAX_S", "45").strip() or 45.0))
    except ValueError:
        return 45.0


class TurnGate:
    """Tracks whether a user-facing turn is in flight."""

    def __init__(self) -> None:
        self._idle = asyncio.Event()
        self._idle.set()
        self._active = 0
        self._waits = 0
        self._timeouts = 0

    # ── foreground ──────────────────────────────────────────────────────
    def turn_started(self) -> None:
        self._active += 1
        self._idle.clear()

    def turn_finished(self) -> None:
        # Never let a miscounted finish latch the gate closed forever: that
        # would silently stop ALL background memory work.
        self._active = max(0, self._active - 1)
        if self._active == 0:
            self._idle.set()

    def turn(self) -> "_TurnScope":
        """`async with gate.turn():` around a user-facing turn."""
        return _TurnScope(self)

    # ── background ──────────────────────────────────────────────────────
    async def wait_for_idle(self, *, max_wait_s: float | None = None, what: str = "") -> bool:
        """Hold background GPU work until no turn is in flight.

        Returns True if the gate opened, False if it timed out and the caller
        should proceed regardless. Never raises — a failure here must not stop
        memory ingest.
        """
        if self._idle.is_set():
            return True
        limit = _max_wait_s() if max_wait_s is None else max_wait_s
        if limit <= 0:
            return False
        self._waits += 1
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=limit)
            return True
        except (TimeoutError, asyncio.TimeoutError):
            self._timeouts += 1
            logger.debug("background_yield_timeout", what=what or "background", waited_s=limit)
            return False
        except Exception:  # noqa: BLE001
            return False

    @property
    def busy(self) -> bool:
        return not self._idle.is_set()

    def stats(self) -> dict[str, int]:
        return {"active_turns": self._active, "background_waits": self._waits,
                "background_timeouts": self._timeouts}


class _TurnScope:
    def __init__(self, gate: TurnGate) -> None:
        self._gate = gate

    async def __aenter__(self) -> "TurnGate":
        self._gate.turn_started()
        return self._gate

    async def __aexit__(self, *_exc) -> bool:
        self._gate.turn_finished()
        return False


#: Process-wide gate. A module-level singleton because the workers and the
#: runtime are constructed in different places but share one GPU.
GATE = TurnGate()
