from __future__ import annotations

"""Parent-side client for the isolated XTTS worker.

Responsibilities, in priority order:

1. **Never take the backend down.** A dead worker degrades the voice; text chat
   keeps working. Every failure path here ends in a reported state, not an
   exception escaping into the request handler.
2. **Never play a cancelled turn's audio.** Results are matched on
   ``(epoch, request_id)`` and filtered against cancelled turns on arrival, so
   a clip that finishes synthesising after the user barged in is dropped rather
   than emitted against the new turn.
3. **Stay bounded.** Request backlog, cancelled-turn memory and restart attempts
   all have caps. An assistant that runs for weeks cannot afford an unbounded
   anything.

The transport is injectable so the whole state machine — handshake, matching,
cancellation, crash detection, restart, timeout — is testable without CUDA, a
GPU, or even torch installed.
"""

import asyncio
import os
import threading
import time
import uuid
from collections import deque
from typing import Any, Callable, Protocol

from services import tts_worker as P

DEFAULT_START_TIMEOUT_S = float(os.getenv("NOVA_TTS_START_TIMEOUT", "300").strip() or "300")
DEFAULT_SYNTH_TIMEOUT_S = float(os.getenv("NOVA_TTS_SYNTH_TIMEOUT", "60").strip() or "60")
DEFAULT_MAX_BACKLOG = int(os.getenv("NOVA_TTS_MAX_BACKLOG", "32").strip() or "32")
DEFAULT_MAX_RESTARTS = int(os.getenv("NOVA_TTS_MAX_RESTARTS", "3").strip() or "3")

_CANCEL_MEMORY = 256


class TtsUnavailable(RuntimeError):
    """The voice is not available. Carries the real reason, never a guess."""


class Transport(Protocol):
    """Everything the client needs from the thing running the worker."""

    def start(self) -> None: ...
    def send(self, msg: dict[str, Any]) -> None: ...
    def recv(self, timeout: float) -> dict[str, Any] | None: ...
    def is_alive(self) -> bool: ...
    def stop(self) -> None: ...


class MultiprocessTransport:
    """The real transport: a spawned child process and two bounded queues.

    `multiprocessing.Queue` is deliberate rather than sockets or shared memory.
    A sentence of 24 kHz mono 16-bit speech is on the order of 100-300 KB, which
    pickles through a pipe in well under a millisecond — far below XTTS
    synthesis time. Shared memory would buy nothing measurable and would add a
    lifetime-management problem across a process that is allowed to crash.
    """

    def __init__(self, cfg: dict[str, Any] | None = None, max_backlog: int = DEFAULT_MAX_BACKLOG) -> None:
        self._cfg = dict(cfg or {})
        self._max_backlog = max_backlog
        self._proc: Any = None
        self._req_q: Any = None
        self._res_q: Any = None

    def start(self) -> None:
        import multiprocessing as mp

        ctx = mp.get_context("spawn")
        self._req_q = ctx.Queue(maxsize=self._max_backlog)
        self._res_q = ctx.Queue()
        self._proc = ctx.Process(
            target=P.worker_main,
            args=(self._req_q, self._res_q, self._cfg),
            name="nova-xtts",
            daemon=True,
        )
        self._proc.start()

    def send(self, msg: dict[str, Any]) -> None:
        if self._req_q is None:
            raise TtsUnavailable("tts transport not started")
        self._req_q.put(msg, timeout=5.0)

    def recv(self, timeout: float) -> dict[str, Any] | None:
        import queue as _q

        if self._res_q is None:
            return None
        try:
            return self._res_q.get(timeout=timeout)
        except _q.Empty:
            return None
        except (EOFError, OSError):
            return None

    def is_alive(self) -> bool:
        return bool(self._proc is not None and self._proc.is_alive())

    def stop(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.is_alive():
                try:
                    self.send({"kind": P.REQ_SHUTDOWN})
                except Exception:  # noqa: BLE001
                    pass
                proc.join(timeout=3.0)
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=3.0)
            if proc.is_alive():
                proc.kill()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._proc = None
            for q in (self._req_q, self._res_q):
                try:
                    if q is not None:
                        q.close()
                except Exception:  # noqa: BLE001
                    pass
            self._req_q = None
            self._res_q = None


class _Pending:
    __slots__ = ("future", "turn_id", "epoch", "queued_at")

    def __init__(self, future: asyncio.Future, turn_id: str, epoch: int) -> None:
        self.future = future
        self.turn_id = turn_id
        self.epoch = epoch
        self.queued_at = time.perf_counter()


class IsolatedTtsEngine:
    """Async facade over the isolated XTTS worker."""

    def __init__(
        self,
        *,
        transport_factory: Callable[[], Transport] | None = None,
        cfg: dict[str, Any] | None = None,
        start_timeout_s: float = DEFAULT_START_TIMEOUT_S,
        synth_timeout_s: float = DEFAULT_SYNTH_TIMEOUT_S,
        max_backlog: int = DEFAULT_MAX_BACKLOG,
        max_restarts: int = DEFAULT_MAX_RESTARTS,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._cfg = dict(cfg or {})
        self._transport_factory = transport_factory or (lambda: MultiprocessTransport(self._cfg, max_backlog))
        self._start_timeout_s = start_timeout_s
        self._synth_timeout_s = synth_timeout_s
        self._max_backlog = max_backlog
        self._max_restarts = max_restarts
        self._on_event = on_event or (lambda _e, _p: None)

        self._transport: Transport | None = None
        self._reader: threading.Thread | None = None
        self._reader_stop = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._start_lock = asyncio.Lock()

        self._epoch = 0
        self._pending: dict[tuple[int, str], _Pending] = {}
        self._ready_future: asyncio.Future | None = None

        self._cancelled_order: deque[str] = deque(maxlen=_CANCEL_MEMORY)
        self._cancelled: set[str] = set()

        # Observable state — /status reads exactly these, never a guess.
        self.state = "stopped"          # stopped | starting | ready | degraded
        self.device: str | None = None
        self.device_reason: str | None = None
        self.sample_rate: int | None = None
        self.last_error: str | None = None
        self.last_synth_ms: float | None = None
        self.load_ms: float | None = None
        self.pid: int | None = None
        self.synth_count = 0
        self.error_count = 0
        self.cancelled_count = 0
        self.restarts = 0

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def ensure_started(self) -> bool:
        """Start the worker if needed. Never raises; returns readiness."""
        if self.state == "ready" and self._transport is not None and self._transport.is_alive():
            return True
        async with self._start_lock:
            if self.state == "ready" and self._transport is not None and self._transport.is_alive():
                return True
            if self.state == "degraded" and self.restarts >= self._max_restarts:
                return False
            return await self._start_locked()

    async def _start_locked(self) -> bool:
        # Anything past the first start is a restart, including the transparent
        # recovery `ensure_started()` performs when a caller hits a dead worker.
        # Counting it here rather than only in `restart()` is what keeps the cap
        # honest: previously a worker that died on every use could be respawned
        # forever through `synthesize()` -> `ensure_started()`, because
        # `self.restarts` never moved and the cap was never reached.
        if self._epoch > 0:
            self.restarts += 1
        self._teardown_transport()
        self._loop = asyncio.get_running_loop()
        self._epoch += 1
        self.state = "starting"
        self.device = None
        self.sample_rate = None

        ready: asyncio.Future = self._loop.create_future()
        self._ready_future = ready

        try:
            transport = self._transport_factory()
            transport.start()
        except Exception as e:  # noqa: BLE001
            self.state = "degraded"
            self.last_error = f"worker_spawn_failed: {type(e).__name__}: {e}"
            self._on_event("tts.worker_failed", {"error": self.last_error})
            return False

        self._transport = transport
        self._reader_stop = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, args=(transport, self._epoch), daemon=True,
                                        name="nova-xtts-reader")
        self._reader.start()

        try:
            await asyncio.wait_for(ready, timeout=self._start_timeout_s)
        except asyncio.TimeoutError:
            self.state = "degraded"
            self.last_error = f"tts worker did not become ready within {self._start_timeout_s:.0f}s"
            self._on_event("tts.worker_failed", {"error": self.last_error})
            self._teardown_transport()
            return False
        except Exception as e:  # noqa: BLE001
            self.state = "degraded"
            self.last_error = str(e)
            self._on_event("tts.worker_failed", {"error": self.last_error})
            self._teardown_transport()
            return False

        self.state = "ready"
        self.last_error = None
        self._on_event("tts.loaded", {"device": self.device, "reason": self.device_reason})
        return True

    async def stop(self) -> None:
        self._teardown_transport()
        self.state = "stopped"

    def _teardown_transport(self) -> None:
        self._reader_stop.set()
        transport, self._transport = self._transport, None
        if transport is not None:
            try:
                transport.stop()
            except Exception:  # noqa: BLE001
                pass
        reader, self._reader = self._reader, None
        if reader is not None and reader.is_alive() and reader is not threading.current_thread():
            reader.join(timeout=2.0)
        self._fail_pending("tts worker stopped")

    # ── reader thread ────────────────────────────────────────────────────────

    def _read_loop(self, transport: Transport, epoch: int) -> None:
        """Runs off the event loop. Only ever hands work back via
        `call_soon_threadsafe`, so all state mutation stays single-threaded."""
        stop = self._reader_stop
        while not stop.is_set():
            try:
                msg = transport.recv(timeout=0.2)
            except Exception:  # noqa: BLE001
                msg = None
            if msg is None:
                if stop.is_set():
                    return
                if not transport.is_alive():
                    self._post(self._on_worker_died, epoch)
                    return
                continue
            self._post(self._handle_message, epoch, msg)

    def _post(self, fn: Callable[..., None], *args: Any) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:
            pass

    # ── message handling (event loop thread) ─────────────────────────────────

    def _handle_message(self, epoch: int, msg: dict[str, Any]) -> None:
        if epoch != self._epoch:
            # A message from a worker generation we have already replaced.
            # Dropping it is the whole point of the epoch counter.
            return

        kind = msg.get("kind")

        if kind == P.RES_READY:
            self.device = str(msg.get("device") or "")
            self.device_reason = str(msg.get("reason") or "")
            self.sample_rate = int(msg.get("sample_rate") or 0) or None
            self.load_ms = msg.get("load_ms")
            self.pid = msg.get("pid")
            fut = self._ready_future
            if fut is not None and not fut.done():
                fut.set_result(True)
            return

        if kind == P.RES_LOAD_ERROR:
            self.last_error = str(msg.get("error") or "tts load failed")
            fut = self._ready_future
            if fut is not None and not fut.done():
                fut.set_exception(TtsUnavailable(self.last_error))
            return

        if kind == P.RES_PROGRESS:
            self._on_event("tts.loading", {"message": str(msg.get("message") or "")})
            return

        if kind == P.RES_PONG:
            return

        request_id = str(msg.get("request_id") or "")
        entry = self._pending.pop((epoch, request_id), None)

        if kind == P.RES_AUDIO:
            self.synth_count += 1
            self.last_synth_ms = msg.get("synth_ms")
            if entry is None:
                return
            turn_id = entry.turn_id
            if turn_id and turn_id in self._cancelled:
                # Late arrival for a turn the user already interrupted. This is
                # the exact leak that lets Turn 105's voice speak over Turn 106.
                self.cancelled_count += 1
                if not entry.future.done():
                    entry.future.cancel()
                return
            if not entry.future.done():
                entry.future.set_result(bytes(msg.get("wav") or b""))
            return

        if kind == P.RES_CANCELLED:
            self.cancelled_count += 1
            if entry is not None and not entry.future.done():
                entry.future.cancel()
            return

        if kind == P.RES_ERROR:
            self.error_count += 1
            err = str(msg.get("error") or "tts synthesis failed")
            self.last_error = err
            if entry is not None and not entry.future.done():
                entry.future.set_exception(TtsUnavailable(err))
            return

    def _on_worker_died(self, epoch: int) -> None:
        if epoch != self._epoch:
            return
        self.state = "degraded"
        self.last_error = "tts worker process exited unexpectedly"
        self._on_event("tts.worker_died", {"epoch": epoch, "restarts": self.restarts})
        fut = self._ready_future
        if fut is not None and not fut.done():
            fut.set_exception(TtsUnavailable(self.last_error))
        self._fail_pending(self.last_error)

    def _fail_pending(self, reason: str) -> None:
        pending, self._pending = self._pending, {}
        for entry in pending.values():
            if not entry.future.done():
                entry.future.set_exception(TtsUnavailable(reason))

    # ── public API ───────────────────────────────────────────────────────────

    async def synthesize(
        self,
        text: str,
        *,
        speaker_wav: str,
        turn_id: str = "",
        language: str = "en",
        speed: float = 1.0,
        timeout_s: float | None = None,
    ) -> bytes:
        """Synthesise one utterance. Raises TtsUnavailable on failure and
        asyncio.CancelledError when the turn was cancelled."""
        text = (text or "").strip()
        if not text:
            raise TtsUnavailable("tts_text_empty")

        if turn_id and turn_id in self._cancelled:
            raise asyncio.CancelledError()

        if not await self.ensure_started():
            raise TtsUnavailable(self.last_error or "tts worker unavailable")

        if len(self._pending) >= self._max_backlog:
            # Backpressure rather than unbounded growth. Hitting this means the
            # model is generating text far faster than the voice can speak it,
            # which is a real condition worth surfacing, not swallowing.
            raise TtsUnavailable(
                f"tts backlog full ({len(self._pending)} pending); dropping this sentence"
            )

        loop = asyncio.get_running_loop()
        self._loop = loop
        request_id = uuid.uuid4().hex
        key = (self._epoch, request_id)
        future: asyncio.Future = loop.create_future()
        self._pending[key] = _Pending(future, turn_id, self._epoch)

        transport = self._transport
        if transport is None:
            self._pending.pop(key, None)
            raise TtsUnavailable("tts worker unavailable")

        try:
            transport.send({
                "kind": P.REQ_SYNTH,
                "request_id": request_id,
                "turn_id": turn_id,
                "text": text,
                "speaker_wav": speaker_wav,
                "language": language,
                "speed": float(speed),
            })
        except Exception as e:  # noqa: BLE001
            self._pending.pop(key, None)
            raise TtsUnavailable(f"tts send failed: {type(e).__name__}: {e}") from e

        try:
            return await asyncio.wait_for(future, timeout=timeout_s or self._synth_timeout_s)
        except asyncio.TimeoutError as e:
            self._pending.pop(key, None)
            # A hung synthesis means the worker is wedged (or the card is). It
            # cannot be interrupted in place, so replace it. Hold the message in
            # a local: a *successful* restart clears self.last_error, and the
            # caller still needs to be told why their sentence never spoke.
            reason = f"tts synthesis timed out after {timeout_s or self._synth_timeout_s:.0f}s"
            self.last_error = reason
            self._on_event("tts.timeout", {"chars": len(text)})
            await self.restart(reason="synthesis timeout")
            raise TtsUnavailable(reason) from e
        finally:
            self._pending.pop(key, None)

    def cancel_turn(self, turn_id: str) -> int:
        """Cancel everything outstanding for a turn. Returns how many in-flight
        requests were dropped. Safe to call when the worker is dead."""
        turn_id = str(turn_id or "")
        if not turn_id:
            return 0

        if turn_id not in self._cancelled:
            if len(self._cancelled_order) == self._cancelled_order.maxlen and self._cancelled_order:
                self._cancelled.discard(self._cancelled_order[0])
            self._cancelled_order.append(turn_id)
            self._cancelled.add(turn_id)

        transport = self._transport
        if transport is not None:
            try:
                transport.send({"kind": P.REQ_CANCEL_TURN, "turn_id": turn_id})
            except Exception:  # noqa: BLE001
                pass

        dropped = 0
        for key, entry in list(self._pending.items()):
            if entry.turn_id == turn_id:
                self._pending.pop(key, None)
                if not entry.future.done():
                    entry.future.cancel()
                dropped += 1
        self.cancelled_count += dropped
        return dropped

    def is_cancelled(self, turn_id: str) -> bool:
        return bool(turn_id) and turn_id in self._cancelled

    async def restart(self, reason: str = "manual") -> bool:
        if self.restarts >= self._max_restarts:
            self.state = "degraded"
            self.last_error = (
                f"tts worker exceeded {self._max_restarts} restarts; not restarting again "
                f"(last reason: {reason})"
            )
            self._on_event("tts.worker_gave_up", {"reason": reason})
            return False
        # `_start_locked` does the counting, so every path — explicit restart and
        # transparent recovery alike — is charged against the same cap.
        self._on_event("tts.worker_restarting", {"reason": reason, "attempt": self.restarts + 1})
        async with self._start_lock:
            return await self._start_locked()

    def status(self) -> dict[str, Any]:
        """Exactly what is true right now. No inferred capability."""
        alive = bool(self._transport is not None and self._transport.is_alive())
        return {
            "state": self.state,
            "process_alive": alive,
            "pid": self.pid,
            "configured_device": (self._cfg.get("device") or os.getenv("NOVA_TTS_DEVICE", "auto")),
            "actual_device": self.device,
            "device_reason": self.device_reason,
            "sample_rate": self.sample_rate,
            "load_ms": self.load_ms,
            "last_synth_ms": self.last_synth_ms,
            "pending": len(self._pending),
            "max_backlog": self._max_backlog,
            "synth_count": self.synth_count,
            "error_count": self.error_count,
            "cancelled_count": self.cancelled_count,
            "restarts": self.restarts,
            "last_error": self.last_error,
        }
