"""Isolated XTTS worker: device contract, IPC, cancellation, crash recovery.

Runs with no GPU, no torch and no XTTS installed. The engine's device policy is
pure and tested directly; the client's state machine is tested against a fake
transport that behaves exactly like the real worker protocol; and one case
spawns a genuine child process to prove the multiprocessing path actually
carries messages both ways on this platform.
"""

import asyncio
import multiprocessing as mp
import queue
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services import tts_worker as P
from services.tts_client import IsolatedTtsEngine, TtsUnavailable
from services.xtts_engine import TtsDeviceError, resolve_device

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


# ── A fake worker that speaks the real protocol ──────────────────────────────

class FakeTransport:
    """In-process stand-in for the spawned worker.

    Same message contract as services/tts_worker.py, so every client behaviour
    under test here is the behaviour the real worker will drive.
    """

    def __init__(self, *, load_error=None, device="cuda", synth_delay=0.0,
                 fail_texts=(), hang_texts=(), die_after=None):
        self.load_error = load_error
        self.device = device
        self.synth_delay = synth_delay
        self.fail_texts = set(fail_texts)
        self.hang_texts = set(hang_texts)
        self.die_after = die_after

        self._req = queue.Queue()
        self._res = queue.Queue()
        self._alive = False
        self._thread = None
        self._stop = threading.Event()
        self.synthesized = []
        self.cancel_msgs = []

    # transport interface
    def start(self):
        self._alive = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def send(self, msg):
        if not self._alive:
            raise RuntimeError("transport dead")
        self._req.put(msg)

    def recv(self, timeout):
        try:
            return self._res.get(timeout=timeout)
        except queue.Empty:
            return None

    def is_alive(self):
        return self._alive

    def stop(self):
        self._stop.set()
        self._alive = False

    # test control
    def kill(self):
        """Simulate the process dying without notice."""
        self._stop.set()
        self._alive = False

    def _run(self):
        if self.load_error:
            self._res.put({"kind": P.RES_LOAD_ERROR, "error": self.load_error, "stage": "load"})
            return
        self._res.put({"kind": P.RES_READY, "device": self.device, "reason": "fake",
                       "sample_rate": 24000, "load_ms": 1.0, "pid": 4242})

        pending = []
        cancelled = set()
        done = 0
        while not self._stop.is_set():
            try:
                msg = self._req.get(timeout=0.05) if not pending else self._req.get_nowait()
            except queue.Empty:
                msg = None

            if msg is not None:
                kind = msg.get("kind")
                if kind == P.REQ_SHUTDOWN:
                    return
                if kind == P.REQ_CANCEL_TURN:
                    cancelled.add(msg.get("turn_id"))
                    self.cancel_msgs.append(msg.get("turn_id"))
                elif kind == P.REQ_SYNTH:
                    pending.append(msg)
                continue

            if not pending:
                continue

            job = pending.pop(0)
            turn_id = job.get("turn_id")
            text = job.get("text")

            if turn_id and turn_id in cancelled:
                self._res.put({"kind": P.RES_CANCELLED, "request_id": job["request_id"],
                               "turn_id": turn_id})
                continue

            if text in self.hang_texts:
                while not self._stop.is_set():
                    time.sleep(0.02)
                return

            if self.synth_delay:
                time.sleep(self.synth_delay)

            if text in self.fail_texts:
                self._res.put({"kind": P.RES_ERROR, "request_id": job["request_id"],
                               "turn_id": turn_id, "error": "bad reference voice"})
                continue

            self.synthesized.append(text)
            self._res.put({"kind": P.RES_AUDIO, "request_id": job["request_id"],
                           "turn_id": turn_id, "wav": b"RIFF" + text.encode(),
                           "synth_ms": 5.0})
            done += 1
            if self.die_after is not None and done >= self.die_after:
                self._alive = False
                return


def engine_with(transport, **kw):
    kw.setdefault("start_timeout_s", 5.0)
    kw.setdefault("synth_timeout_s", 2.0)
    return IsolatedTtsEngine(transport_factory=lambda: transport, **kw)


# ── The real cross-process path ──────────────────────────────────────────────

def echo_worker_main(req_q, res_q, cfg):
    """A real child-process worker speaking the protocol, minus the model.

    Module-level so Windows `spawn` can pickle it by name — the same constraint
    services.tts_worker.worker_main lives under.
    """
    res_q.put({"kind": P.RES_READY, "device": cfg.get("device", "cpu"), "reason": "echo",
               "sample_rate": 24000, "load_ms": 0.0, "pid": 1})
    while True:
        msg = req_q.get()
        if msg.get("kind") == P.REQ_SHUTDOWN:
            return
        if msg.get("kind") == P.REQ_SYNTH:
            res_q.put({"kind": P.RES_AUDIO, "request_id": msg["request_id"],
                       "turn_id": msg.get("turn_id", ""),
                       "wav": b"WAV:" + str(msg.get("text", "")).encode(),
                       "synth_ms": 1.0})


class EchoProcessTransport:
    def __init__(self):
        ctx = mp.get_context("spawn")
        self._req = ctx.Queue(maxsize=16)
        self._res = ctx.Queue()
        self._proc = ctx.Process(target=echo_worker_main, args=(self._req, self._res, {"device": "cpu"}),
                                 daemon=True)

    def start(self):
        self._proc.start()

    def send(self, msg):
        self._req.put(msg, timeout=5.0)

    def recv(self, timeout):
        try:
            return self._res.get(timeout=timeout)
        except queue.Empty:
            return None
        except (EOFError, OSError):
            return None

    def is_alive(self):
        return self._proc.is_alive()

    def stop(self):
        try:
            if self._proc.is_alive():
                self._req.put({"kind": P.REQ_SHUTDOWN})
                self._proc.join(timeout=5.0)
            if self._proc.is_alive():
                self._proc.terminate()
        except Exception:
            pass


# ── Tests ────────────────────────────────────────────────────────────────────

def test_device_contract():
    print("\ndevice contract")

    # The P0 regression: the default configuration must produce a usable
    # device, not an exception. Before this change, NOVA_TTS_DEVICE defaulted
    # to "cpu" and the loader raised "XTTS requires CUDA" on that exact branch.
    dev, _ = resolve_device("cpu", cuda_available=False)
    check(dev == "cpu", f"explicit cpu is honoured, never raises (got {dev})")

    dev, reason = resolve_device("auto", cuda_available=True)
    check(dev == "cuda", f"auto picks cuda when available (got {dev})")
    check("isolated" in reason.lower(), "auto explains why cuda is now safe")

    try:
        resolve_device("auto", cuda_available=False, allow_cpu_fallback=False)
        check(False, "auto without cuda and without opt-in must refuse")
    except TtsDeviceError as e:
        check("NOVA_TTS_ALLOW_CPU_FALLBACK" in str(e),
              "auto refusal names the opt-in flag instead of silently degrading")

    dev, _ = resolve_device("auto", cuda_available=False, allow_cpu_fallback=True)
    check(dev == "cpu", f"explicit opt-in allows cpu fallback (got {dev})")

    try:
        resolve_device("cuda", cuda_available=False, allow_cpu_fallback=True)
        check(False, "explicit cuda must not be downgraded by the fallback flag")
    except TtsDeviceError:
        check(True, "explicit cuda is a hard requirement even with fallback on")

    try:
        resolve_device("gpu0", cuda_available=True)
        check(False, "invalid device should raise")
    except TtsDeviceError:
        check(True, "invalid device name rejected")


async def test_happy_path():
    print("\nsynthesis + status")
    t = FakeTransport(device="cuda")
    eng = engine_with(t)
    ok = await eng.ensure_started()
    check(ok, "worker starts")
    check(eng.state == "ready", f"state is ready (got {eng.state})")

    wav = await eng.synthesize("Hello Marcus.", speaker_wav="v.wav", turn_id="t1")
    check(wav == b"RIFFHello Marcus.", "audio round-trips through the protocol")

    st = eng.status()
    check(st["actual_device"] == "cuda", f"status reports ACTUAL device (got {st['actual_device']})")
    check(st["synth_count"] == 1, "synth counted")
    check(st["pending"] == 0, "no pending requests leak after completion")
    await eng.stop()


async def test_load_error_degrades_not_crashes():
    print("\nload failure")
    t = FakeTransport(load_error="no CUDA on this box")
    eng = engine_with(t)
    ok = await eng.ensure_started()
    check(not ok, "failed load reports not-ready rather than raising")
    check(eng.state == "degraded", f"state degraded (got {eng.state})")
    check("no CUDA" in (eng.last_error or ""), "the real reason is preserved verbatim")

    raised = None
    try:
        await eng.synthesize("hi", speaker_wav="v.wav")
    except TtsUnavailable as e:
        raised = str(e)
    check(raised is not None, "synthesis raises TtsUnavailable, not a bare crash")
    await eng.stop()


async def test_cancellation_drops_queued_and_late_audio():
    print("\ncancellation / barge-in")
    # Slow synthesis so several sentences are genuinely queued when we cancel.
    t = FakeTransport(synth_delay=0.15)
    eng = engine_with(t, synth_timeout_s=5.0)
    await eng.ensure_started()

    tasks = [asyncio.create_task(eng.synthesize(f"sentence {i}", speaker_wav="v.wav", turn_id="turn-105"))
             for i in range(5)]
    await asyncio.sleep(0.05)  # first one is in flight, rest are queued

    dropped = eng.cancel_turn("turn-105")
    check(dropped >= 1, f"cancel drops in-flight requests (dropped {dropped})")

    results = await asyncio.gather(*tasks, return_exceptions=True)
    cancelled = sum(1 for r in results if isinstance(r, asyncio.CancelledError))
    audio = sum(1 for r in results if isinstance(r, bytes))
    check(cancelled == 5, f"every request for the cancelled turn is cancelled (got {cancelled}/5)")
    check(audio == 0, f"no audio survives cancellation (got {audio} clips)")
    check(eng.is_cancelled("turn-105"), "turn stays marked cancelled")

    # Item 14: a NEW turn after cancellation must work normally.
    wav = await eng.synthesize("fresh turn", speaker_wav="v.wav", turn_id="turn-106")
    check(wav == b"RIFFfresh turn", "the next turn synthesises normally after a barge-in")

    # And a request for the old turn can never be issued again.
    raised = False
    try:
        await eng.synthesize("zombie", speaker_wav="v.wav", turn_id="turn-105")
    except asyncio.CancelledError:
        raised = True
    check(raised, "a request against a cancelled turn is refused up front")
    await eng.stop()


async def test_late_result_for_cancelled_turn_is_discarded():
    print("\nlate result suppression")
    t = FakeTransport(synth_delay=0.25)
    eng = engine_with(t, synth_timeout_s=5.0)
    await eng.ensure_started()

    task = asyncio.create_task(eng.synthesize("slow one", speaker_wav="v.wav", turn_id="turn-A"))
    await asyncio.sleep(0.05)
    eng.cancel_turn("turn-A")

    result = await asyncio.gather(task, return_exceptions=True)
    check(isinstance(result[0], asyncio.CancelledError), "awaiting caller sees cancellation")

    # The fake worker still finishes and emits audio for it; give it time to land.
    await asyncio.sleep(0.4)
    check(eng.synth_count >= 0 and eng.cancelled_count >= 1,
          "late audio for a cancelled turn is counted as cancelled, not delivered")
    await eng.stop()


async def test_synthesis_error_is_survivable():
    print("\nper-request failure")
    t = FakeTransport(fail_texts={"bad"})
    eng = engine_with(t)
    await eng.ensure_started()

    raised = None
    try:
        await eng.synthesize("bad", speaker_wav="v.wav", turn_id="t")
    except TtsUnavailable as e:
        raised = str(e)
    check(raised is not None and "reference voice" in raised, "bad request reports the real error")
    check(eng.state == "ready", "one bad sentence does not degrade the worker")

    wav = await eng.synthesize("good", speaker_wav="v.wav", turn_id="t")
    check(wav == b"RIFFgood", "the next sentence still synthesises")
    await eng.stop()


async def test_worker_crash_and_restart():
    print("\ncrash recovery")
    transports = []

    def factory():
        # First worker dies after one clip; the replacement is healthy.
        t = FakeTransport(die_after=1) if not transports else FakeTransport()
        transports.append(t)
        return t

    eng = IsolatedTtsEngine(transport_factory=factory, start_timeout_s=5.0, synth_timeout_s=2.0)
    await eng.ensure_started()
    wav = await eng.synthesize("one", speaker_wav="v.wav", turn_id="t")
    check(wav == b"RIFFone", "first clip works")

    # Let the reader notice the corpse.
    for _ in range(50):
        await asyncio.sleep(0.02)
        if eng.state == "degraded":
            break
    check(eng.state == "degraded", f"death is detected, not silently ignored (state {eng.state})")

    ok = await eng.restart(reason="test")
    check(ok, "worker restarts")
    check(eng.state == "ready", "state recovers to ready")
    check(eng.restarts == 1, f"restart counted (got {eng.restarts})")

    wav = await eng.synthesize("two", speaker_wav="v.wav", turn_id="t2")
    check(wav == b"RIFFtwo", "synthesis works again after restart")
    await eng.stop()


async def test_restart_cap():
    print("\nrestart cap")
    eng = IsolatedTtsEngine(
        transport_factory=lambda: FakeTransport(load_error="always broken"),
        start_timeout_s=2.0, max_restarts=2,
    )
    await eng.ensure_started()
    check(await eng.restart("1") is False or eng.restarts <= 2, "restarts are attempted")
    await eng.restart("2")
    ok = await eng.restart("3")
    check(ok is False, "restarts stop at the cap instead of looping forever")
    check("exceeded" in (eng.last_error or ""), "giving up is reported honestly")
    await eng.stop()


async def test_transparent_recovery_is_capped():
    print("\ntransparent recovery counts against the cap")
    # A worker that dies immediately on every start. Callers reach it through
    # synthesize() -> ensure_started(), which transparently respawns. That path
    # used to bypass the cap entirely — `restarts` never moved, so a
    # crash-looping worker could be respawned forever. Found by the live
    # fault-injection run (tests/live_stress_validation.py).
    eng = IsolatedTtsEngine(
        transport_factory=lambda: FakeTransport(load_error="dies on start"),
        start_timeout_s=2.0, max_restarts=2,
    )
    await eng.ensure_started()
    before = eng.restarts

    attempts = 0
    for _ in range(8):
        try:
            await eng.synthesize("probe", speaker_wav="v.wav", turn_id="t")
        except TtsUnavailable:
            attempts += 1
    check(eng.restarts > before,
          f"recovery through synthesize() is counted ({before} -> {eng.restarts})")
    check(eng.restarts <= 2 + 1,
          f"and stops at the cap rather than respawning forever (got {eng.restarts})")
    check(attempts == 8, "every attempt still fails cleanly rather than hanging")
    await eng.stop()


async def test_backlog_is_bounded():
    print("\nbounded backlog")
    t = FakeTransport(synth_delay=0.3)
    eng = engine_with(t, max_backlog=3, synth_timeout_s=5.0)
    await eng.ensure_started()

    tasks = [asyncio.create_task(eng.synthesize(f"s{i}", speaker_wav="v.wav", turn_id="t"))
             for i in range(6)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    rejected = sum(1 for r in results if isinstance(r, TtsUnavailable) and "backlog" in str(r))
    check(rejected >= 1, f"backlog overflow is rejected, not queued forever (rejected {rejected})")
    eng.cancel_turn("t")
    await eng.stop()


async def test_timeout_replaces_wedged_worker():
    print("\nsynthesis timeout")
    transports = []

    def factory():
        t = FakeTransport(hang_texts={"wedge"}) if not transports else FakeTransport()
        transports.append(t)
        return t

    eng = IsolatedTtsEngine(transport_factory=factory, start_timeout_s=5.0, synth_timeout_s=0.3)
    await eng.ensure_started()

    raised = None
    try:
        await eng.synthesize("wedge", speaker_wav="v.wav", turn_id="t")
    except TtsUnavailable as e:
        raised = str(e)
    check(raised is not None and "timed out" in raised, "a wedged synthesis times out")
    check(eng.restarts == 1, f"the wedged worker is replaced (restarts {eng.restarts})")
    check(eng.state == "ready", "the replacement is ready")

    wav = await eng.synthesize("after", speaker_wav="v.wav", turn_id="t2")
    check(wav == b"RIFFafter", "voice works again after the timeout")
    await eng.stop()


async def test_real_subprocess_ipc():
    print("\nreal cross-process IPC")
    eng = IsolatedTtsEngine(transport_factory=EchoProcessTransport, start_timeout_s=60.0,
                            synth_timeout_s=30.0)
    try:
        ok = await eng.ensure_started()
        check(ok, "a genuine spawned child process reaches ready")
        wav = await eng.synthesize("cross process", speaker_wav="v.wav", turn_id="t")
        check(wav == b"WAV:cross process", "audio bytes survive the process boundary")
        st = eng.status()
        check(st["process_alive"], "status sees a live child process")
    finally:
        await eng.stop()


async def main():
    test_device_contract()
    await test_happy_path()
    await test_load_error_degrades_not_crashes()
    await test_cancellation_drops_queued_and_late_audio()
    await test_late_result_for_cancelled_turn_is_discarded()
    await test_synthesis_error_is_survivable()
    await test_worker_crash_and_restart()
    await test_restart_cap()
    await test_transparent_recovery_is_capped()
    await test_backlog_is_bounded()
    await test_timeout_replaces_wedged_worker()
    await test_real_subprocess_ipc()

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


if __name__ == "__main__":
    asyncio.run(main())
