from __future__ import annotations

"""Shared worker shutdown (U10).

Every background worker had the same five lines of `stop()`, and they were
wrong in the same way: set the stop event, then IMMEDIATELY `task.cancel()`.
That cancels the worker wherever it happens to be — routinely inside an
`aiosqlite` transaction, since these workers exist to write to memory.

Two real consequences, both observed:

  * a write is aborted mid-transaction (the memory-ingest worker was caught
    being cancelled inside `ensure_conversation`'s `db.commit()`);
  * the aborted connection leaves aiosqlite's **non-daemon** connection thread
    alive, and `threading._shutdown()` then joins it forever — the process
    finishes all its work and simply never exits. It reproduced roughly 1 run
    in 6, which is exactly why it survived 46 green suites: it isn't a failure,
    it's a hang, and only a full boot-and-shutdown cycle shows it.

The fix is to ask first and cancel second: the worker loops all poll their
stop event, so given a moment they exit cleanly between units of work.
"""

import asyncio
import os

from core.logging_setup import get_logger

logger = get_logger(__name__)


def _grace_seconds() -> float:
    try:
        return max(0.0, float(os.getenv("NOVA_WORKER_STOP_GRACE_S", "3").strip() or 3.0))
    except ValueError:
        return 3.0


async def stop_worker(task: asyncio.Task | None, *, name: str = "", grace_s: float | None = None) -> None:
    """Let a worker finish the step it is on, then cancel it if it won't.

    The caller sets the worker's stop event BEFORE calling this — that is what
    the grace period is waiting for the loop to notice. Never raises for the
    ordinary outcomes, so one stubborn worker can't abort a shutdown sequence
    and leave the rest of them running.
    """
    if task is None or task.done():
        return

    grace = _grace_seconds() if grace_s is None else max(0.0, float(grace_s))
    if grace:
        try:
            # shield: a timeout here must not cancel the worker mid-write —
            # that is the exact behavior being fixed.
            await asyncio.wait_for(asyncio.shield(task), timeout=grace)
            return
        except (asyncio.TimeoutError, TimeoutError):
            logger.debug("worker_stop_grace_expired", worker=name, grace_s=grace)
        except asyncio.CancelledError:
            if task.done():
                return
            raise
        except Exception:
            return  # the worker died on its own; nothing left to stop

    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=5.0)
    except asyncio.CancelledError:
        # We just cancelled it — the expected outcome, not a failure.
        # CancelledError is a BaseException, so a bare `except Exception`
        # lets it escape and abort the caller's shutdown sequence.
        pass
    except Exception:
        pass
