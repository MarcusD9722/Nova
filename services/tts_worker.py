from __future__ import annotations

"""The isolated XTTS child process.

Why this exists, in one paragraph: XTTS on CUDA *inside the backend process*
aborts Nova outright with `CUDA error: an illegal memory access was
encountered` in `ggml_backend_cuda_synchronize`, because it is a second
uncoordinated CUDA consumer sitting beside llama.cpp (the evidence is in
core/gpu.py). It cannot be serialised behind `GPU_SEM` either — sentence-
streamed TTS overlaps synthesis with generation by design, and the reply stream
holds that permit for the whole generation, so taking it here deadlocks
(measured: a 195 s hang, then access violations on every later turn). A separate
*process* gets a separate CUDA context, which the driver time-slices safely, so
the voice can be both fast and non-fatal. Same card, different context.

Protocol notes:
  * Messages are plain dicts. No custom classes cross the process boundary, so
    a version skew between parent and child can never fail to unpickle.
  * The child is sequential: one synthesis at a time. It drains every pending
    control message before starting each job, which is what makes barge-in
    cheap — cancelling a turn drops the queued sentences for it that have not
    started yet.
  * A synthesis already in flight cannot be interrupted (XTTS offers no such
    hook). The parent discards its result instead; see services/tts_client.py.
"""

import os
import queue
import time
from collections import deque
from typing import Any

# ── Message kinds ────────────────────────────────────────────────────────────
# parent -> child
REQ_SYNTH = "synth"
REQ_CANCEL_TURN = "cancel_turn"
REQ_PING = "ping"
REQ_SHUTDOWN = "shutdown"

# child -> parent
RES_READY = "ready"
RES_LOAD_ERROR = "load_error"
RES_PROGRESS = "progress"
RES_AUDIO = "audio"
RES_ERROR = "error"
RES_CANCELLED = "cancelled"
RES_PONG = "pong"

#: How many recently-cancelled turn ids the child remembers. Bounded on purpose:
#: an unbounded set here would be a slow leak in a long-lived process.
_CANCEL_MEMORY = 256


class _CancelledTurns:
    """Bounded FIFO set of cancelled turn ids."""

    def __init__(self, maxlen: int = _CANCEL_MEMORY) -> None:
        self._order: deque[str] = deque(maxlen=maxlen)
        self._set: set[str] = set()

    def add(self, turn_id: str) -> None:
        if turn_id in self._set:
            return
        if len(self._order) == self._order.maxlen and self._order:
            self._set.discard(self._order[0])
        self._order.append(turn_id)
        self._set.add(turn_id)

    def __contains__(self, turn_id: object) -> bool:
        return turn_id in self._set


def worker_main(req_q: Any, res_q: Any, cfg: dict[str, Any] | None = None) -> None:
    """Child process entry point.

    Must stay a module-level function: Windows uses spawn, so multiprocessing
    pickles this target by qualified name and re-imports the module in the
    child. Nothing at import time in this module (or in services.xtts_engine)
    may touch CUDA, or the "separate context" property is lost before we start.
    """
    cfg = dict(cfg or {})

    # Keep the child from inheriting a stray device pin. It shares the card with
    # llama.cpp deliberately; what it must NOT share is the CUDA context, and
    # that comes free from being a different process.
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

    from services.xtts_engine import TtsDeviceError, load_engine, resolve_device, synthesize

    try:
        device, reason = resolve_device(
            cfg.get("device"), allow_cpu_fallback=cfg.get("allow_cpu_fallback")
        )
    except TtsDeviceError as e:
        res_q.put({"kind": RES_LOAD_ERROR, "error": str(e), "stage": "device"})
        return
    except Exception as e:  # noqa: BLE001
        res_q.put({"kind": RES_LOAD_ERROR, "error": f"{type(e).__name__}: {e}", "stage": "device"})
        return

    started = time.perf_counter()
    try:
        tts, sample_rate = load_engine(
            device, progress=lambda m: res_q.put({"kind": RES_PROGRESS, "message": m})
        )
    except Exception as e:  # noqa: BLE001
        res_q.put({"kind": RES_LOAD_ERROR, "error": f"{type(e).__name__}: {e}", "stage": "load"})
        return

    res_q.put({
        "kind": RES_READY,
        "device": device,
        "reason": reason,
        "sample_rate": sample_rate,
        "load_ms": round((time.perf_counter() - started) * 1000.0, 1),
        "pid": os.getpid(),
    })

    pending: deque[dict[str, Any]] = deque()
    cancelled = _CancelledTurns()

    while True:
        # Read everything currently available before doing any work. This is
        # what lets a barge-in cancel land ahead of sentences that were queued
        # a moment ago but have not started synthesising.
        msg: dict[str, Any] | None
        try:
            msg = req_q.get() if not pending else req_q.get_nowait()
        except queue.Empty:
            msg = None
        except (EOFError, OSError):
            return  # parent went away

        if msg is not None:
            kind = msg.get("kind")
            if kind == REQ_SHUTDOWN:
                return
            if kind == REQ_PING:
                res_q.put({"kind": RES_PONG, "device": device, "pid": os.getpid()})
            elif kind == REQ_CANCEL_TURN:
                cancelled.add(str(msg.get("turn_id") or ""))
            elif kind == REQ_SYNTH:
                pending.append(msg)
            continue

        if not pending:
            continue

        job = pending.popleft()
        request_id = str(job.get("request_id") or "")
        turn_id = str(job.get("turn_id") or "")

        if turn_id and turn_id in cancelled:
            res_q.put({"kind": RES_CANCELLED, "request_id": request_id, "turn_id": turn_id})
            continue

        t0 = time.perf_counter()
        try:
            wav = synthesize(
                tts, sample_rate,
                text=str(job.get("text") or ""),
                speaker_wav=str(job.get("speaker_wav") or ""),
                language=str(job.get("language") or "en"),
                speed=float(job.get("speed") or 1.0),
            )
        except Exception as e:  # noqa: BLE001
            # A single bad request (empty text, unreadable reference voice) must
            # not take the process down — the next sentence may be fine.
            res_q.put({
                "kind": RES_ERROR,
                "request_id": request_id,
                "turn_id": turn_id,
                "error": f"{type(e).__name__}: {e}",
            })
            continue

        res_q.put({
            "kind": RES_AUDIO,
            "request_id": request_id,
            "turn_id": turn_id,
            "wav": wav,
            "synth_ms": round((time.perf_counter() - t0) * 1000.0, 1),
        })
