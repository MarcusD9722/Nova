from __future__ import annotations

"""Regression suite for C2: two threads inside llama.cpp kills the process.

THE INVARIANT: never more than one thread inside the model at a time.

Real llama.cpp does not raise when this is violated — it aborts the process
(`ggml-cuda.cu:102 CUDA error`, exit 127, no traceback), which is why the audit
found this by watching Nova disappear rather than by reading a stack trace.
Measured: closing a reply stream partway through killed her 7 times out of 7.

So this suite drives the REAL `LLMRuntime` with a fake `_llama` that counts
concurrent entries instead of dying, and exercises the five paths the audit
proved could produce two. `tests/harness.py` cannot see any of this: it
substitutes the whole runtime, so every one of these was green before the fix.
"""

import asyncio
import contextlib
import os
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks  # noqa: E402

check = Checks()


class FakeLlama:
    """Counts threads inside the model. Real llama.cpp aborts instead."""

    def __init__(self, *, delay: float = 0.02, chunks: int = 40) -> None:
        self.delay = delay
        self.chunks = chunks
        self._lock = threading.Lock()
        self.inside = 0
        self.peak = 0
        self.calls = 0
        self.overlaps = 0
        #: Chunks actually decoded, per call. A cancelled stream should stop
        #: early rather than finish a reply nobody is reading.
        self.decoded: list[int] = []

    @contextlib.contextmanager
    def _enter(self):
        with self._lock:
            self.inside += 1
            self.calls += 1
            self.peak = max(self.peak, self.inside)
            if self.inside > 1:
                self.overlaps += 1
        try:
            yield
        finally:
            with self._lock:
                self.inside -= 1

    def create_chat_completion(self, messages=None, stream=False, **kw):  # noqa: ANN001, ARG002
        if not stream:
            with self._enter():
                time.sleep(self.delay * 6)
                return {"choices": [{"message": {"content": "a fake answer"}}],
                        "usage": {"prompt_tokens": 7, "completion_tokens": 3}}

        def _gen():
            n = 0
            # The model is in use for the WHOLE iteration: each next() is a
            # decode step, which is the detail the semaphore could not express.
            with self._enter():
                try:
                    for i in range(self.chunks):
                        time.sleep(self.delay)
                        n += 1
                        yield {"choices": [{"delta": {"content": f"tok{i} "},
                                            "finish_reason": None}]}
                    yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}
                finally:
                    self.decoded.append(n)

        return _gen()

    def close(self):
        pass


def _runtime(fake: FakeLlama):
    """A real LLMRuntime with the weights swapped for the counter."""
    from core.llm_runtime import LLMRuntime

    rt = LLMRuntime(model_path=None, context_tokens=4096)
    rt._llama = fake
    return rt


async def test_a_abandoned_stream_releases_the_model_before_the_next_turn():
    check.section("C2(a): a stream nobody reads any more")
    fake = FakeLlama()
    rt = _runtime(fake)

    async def read_a_bit_then_quit():
        stream = rt.chat_stream([{"role": "user", "content": "hi"}], max_tokens=200)
        async with contextlib.aclosing(stream):
            got = 0
            async for _tok in stream:
                got += 1
                if got >= 3:
                    break   # exactly what the repeat guard and the UI do
        return got

    read = await read_a_bit_then_quit()
    check(read == 3, f"the consumer stopped after {read} tokens")
    # The next turn starts IMMEDIATELY, which is the real sequence: Stop, then
    # send. Before the fix this is where the second thread walked in.
    await rt.chat([{"role": "user", "content": "what is 2+2?"}], max_tokens=50)
    check(fake.peak == 1, f"never more than one thread inside the model (peak {fake.peak})")
    check(fake.overlaps == 0, f"no overlapping entry was recorded ({fake.overlaps})")
    check(fake.decoded and fake.decoded[0] < fake.chunks,
          f"and the abandoned generation stopped early ({fake.decoded} of {fake.chunks} chunks)")


async def test_b_timeout_around_a_model_call():
    check.section("C2(b): a 12s-style timeout that gives up mid-generation")
    fake = FakeLlama(delay=0.05)
    rt = _runtime(fake)

    # Understanding and Expression both wrap their call in wait_for(…, 12).
    # `to_thread` cannot be cancelled, so the worker keeps going; the GPU permit
    # is released and the next caller used to walk straight in.
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(
            rt.chat([{"role": "user", "content": "slow"}], max_tokens=50),
            timeout=0.05)
    await rt.chat([{"role": "user", "content": "next"}], max_tokens=50)
    check(fake.peak == 1, f"still one at a time (peak {fake.peak})")
    check(fake.overlaps == 0, f"no overlap ({fake.overlaps})")


async def test_c_many_callers_at_once():
    check.section("C2(c): every caller racing at once, timeouts and all")
    fake = FakeLlama(delay=0.01, chunks=12)
    rt = _runtime(fake)

    async def streamer(n: int):
        stream = rt.chat_stream([{"role": "user", "content": f"s{n}"}], max_tokens=100)
        async with contextlib.aclosing(stream):
            i = 0
            async for _t in stream:
                i += 1
                if i > n:          # each abandons at a different point
                    break

    async def chatter(n: int):
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                rt.chat([{"role": "user", "content": f"c{n}"}], max_tokens=50),
                timeout=0.02 * (n + 1))

    await asyncio.gather(*[streamer(i) for i in range(4)],
                         *[chatter(i) for i in range(4)])
    # Not all eight: a `chat()` whose timeout expires while its work is still
    # QUEUED in the thread pool is cancelled before it starts, and never touches
    # the model at all. That is the good case, and it is why this asserts on the
    # four streams — which all definitely ran — rather than on a total.
    check(fake.calls >= 4, f"the streams all really reached the model ({fake.calls} calls)")
    check(fake.peak == 1, f"and still never two inside it (peak {fake.peak})")
    check(fake.overlaps == 0, f"no overlap across {fake.calls} calls ({fake.overlaps})")


async def test_d_vision_takes_the_same_lock():
    check.section("C2/H21: vision is not a second uncoordinated CUDA consumer")
    import tempfile

    fake = FakeLlama(delay=0.02)
    rt = _runtime(fake)
    # ONE counter for both models, because "one GPU, one lock" is the claim
    # under test — an overlap between the text and vision contexts is exactly
    # what core/gpu.py says aborts the process.
    rt._vision_llama = fake
    tmp = Path(tempfile.mkdtemp())
    (tmp / "model.gguf").write_bytes(b"fake")
    (tmp / "mmproj.gguf").write_bytes(b"fake")
    rt._model_path = tmp / "model.gguf"
    rt._mmproj_path = str(tmp / "mmproj.gguf")
    check(rt.vision_status.get("enabled") is True,
          f"vision reports ready, so the call really runs ({rt.vision_status.get('reason')})")

    async def chat():
        await rt.chat([{"role": "user", "content": "hello"}], max_tokens=50)

    async def look():
        with contextlib.suppress(Exception):
            await rt.vision_analyze(b"\x89PNG\r\n", question="what is this?")

    await asyncio.gather(chat(), look(), chat())
    check(fake.peak == 1, f"chat and vision never run together (peak {fake.peak})")
    check(fake.overlaps == 0, f"no overlap ({fake.overlaps})")
    check(rt._vision_llama is None,
          "and the vision model is freed afterwards rather than held forever")


async def test_e_warm_up_happens_once_and_before_anything_waits():
    check.section("C2: the first inference is burned at boot")
    fake = FakeLlama(delay=0.01)
    rt = _runtime(fake)
    rt._model_path = Path("fake-model.gguf")

    took = await rt.warm_up()
    check(fake.calls >= 1, f"warm_up actually ran a generation ({fake.calls} calls)")
    check(took >= 0.0, f"and reports how long it took ({took:.3f}s)")
    before = fake.calls
    again = await rt.warm_up()
    check(fake.calls == before and again == 0.0,
          f"a second warm_up is a no-op ({fake.calls - before} extra calls)")


async def test_f_lock_wait_fails_honestly():
    check.section("C2: a wedged model is reported, not waited on forever")
    from core.llm_runtime import GPUEnforcementError, _hold

    lock = threading.Lock()
    lock.acquire()
    try:
        raised = None
        try:
            with _hold(lock, 0.05, "chat"):
                pass
        except GPUEnforcementError as e:
            raised = e
        check(raised is not None, "a blocked acquire raises rather than hanging")
        check("still busy" in str(raised), f"and says why ({str(raised)[:60]})")
    finally:
        lock.release()


async def main():
    await test_a_abandoned_stream_releases_the_model_before_the_next_turn()
    await test_b_timeout_around_a_model_call()
    await test_c_many_callers_at_once()
    await test_d_vision_takes_the_same_lock()
    await test_e_warm_up_happens_once_and_before_anything_waits()
    await test_f_lock_wait_fails_honestly()
    check.finish()


if __name__ == "__main__":
    asyncio.run(main())
