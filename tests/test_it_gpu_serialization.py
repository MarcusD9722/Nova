"""INTEGRATION (U10): the cloud->local fallback must never break GPU serialization.

The bug this pins (fixed in d1f407e, never covered): a cloud role runs under
the CLOUD handle's semaphore, which has several permits because remote calls
don't contend for the GPU. The moment such a call falls back to the local
model, it is driving llama.cpp again — and if it doesn't re-acquire the LOCAL
semaphore first, several fallbacks plus the background workers hit one
llama.cpp context at once and take the CUDA backend down with them.

Reproducing it needs three things together — a cloud role, a failing provider,
and concurrent local work — which is why 46 green unit suites never saw it.

The cloud provider here is a real HTTP server on localhost (harness.CloudStub),
so no API key and no outbound traffic are involved. `ScriptedLLM` records the
high-water mark of concurrent local calls; anything above 1 is the crash.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Checks, CloudStub, boot, run

check = Checks()

LOCAL_REPLY = "```python\nprint('local fallback wrote this')\n```"
CLOUD_REPLY = "```python\nprint('cloud wrote this')\n```"

# Enough overlap that unserialized calls WOULD collide, short enough to stay fast.
CALL_DELAY_S = 0.05
FANOUT = 6


def cloud_env(stub: CloudStub) -> dict[str, str]:
    return {
        "NOVA_CLOUD_ENABLED": "1",
        "NOVA_CLOUD_PROVIDER": "openai",
        "NOVA_CLOUD_API_KEY": "harness-not-a-real-key",
        "NOVA_CLOUD_MODEL": "stub-model",
        "NOVA_CLOUD_BASE_URL": stub.base_url,
        "NOVA_CLOUD_CONCURRENCY": "4",
    }


async def main() -> None:
    # ── 1. Provider is down: every coder call falls back onto the GPU ──────
    broken = CloudStub(status=500).start()
    try:
        async with boot(env=cloud_env(broken), default_reply=LOCAL_REPLY) as nova:
            nova.llm.call_delay = CALL_DELAY_S
            builder = nova.runtime._project_builder
            cloud = nova.runtime.cloud

            check.section("Roles are actually routed to the cloud")
            coder = nova.runtime.models.for_role("coder")
            check(coder.runtime is cloud, "the 'coder' role resolves to the cloud runtime")
            check(coder.semaphore._value > 1,
                  f"the cloud handle holds several permits ({coder.semaphore._value})")

            check.section("Concurrent fallbacks under background chat load")
            nova.llm.reset_calls()
            # _llm_file is the real production path a project build takes for
            # every generated file: cloud handle -> CloudRuntime -> fallback.
            work = [builder._llm_file(f"Write the complete contents of file_{i}.py") for i in range(FANOUT)]
            # ...while ordinary local turns run at the same time, which is what
            # made the live crash reproducible.
            work += [nova.say("how's the build going", conversation_id=uuid4()) for _ in range(3)]
            results = await asyncio.gather(*work, return_exceptions=True)

            errors = [r for r in results if isinstance(r, BaseException)]
            check(not errors, f"no call raised ({[type(e).__name__ for e in errors[:3]]})")
            check(cloud.status()["fallbacks_to_local"] >= FANOUT,
                  f"every cloud call fell back to local ({cloud.status()['fallbacks_to_local']})")
            check(nova.llm.max_concurrent == 1,
                  f"the local model was NEVER driven concurrently (peak={nova.llm.max_concurrent})")
            check(any("local fallback wrote this" in str(r) for r in results),
                  "the fallback really produced the LOCAL model's output")

            check.section("The concurrency detector is not vacuous")
            # If ScriptedLLM couldn't observe overlap, the check above would
            # pass no matter what. Drive it without any semaphore and prove it.
            nova.llm.reset_calls()
            await asyncio.gather(*(nova.llm.chat([{"role": "user", "content": f"raw {i}"}])
                                   for i in range(FANOUT)))
            check(nova.llm.max_concurrent > 1,
                  f"unguarded calls DO register as concurrent (peak={nova.llm.max_concurrent})")
    finally:
        broken.stop()

    # ── 2. Provider is healthy: cloud work overlaps and skips the GPU ─────
    healthy = CloudStub(status=200, reply=CLOUD_REPLY, latency_s=CALL_DELAY_S).start()
    try:
        async with boot(env=cloud_env(healthy), default_reply=LOCAL_REPLY) as nova:
            builder = nova.runtime._project_builder
            check.section("A healthy provider is not serialized on the GPU")
            nova.llm.reset_calls()
            outs = await asyncio.gather(
                *(builder._llm_file(f"Write the complete contents of file_{i}.py") for i in range(FANOUT))
            )
            check(all("cloud wrote this" in o for o in outs), "the cloud answered every call")
            check(nova.llm.max_concurrent == 0,
                  f"a working cloud never touches the local model (local calls={len(nova.llm.prompts)})")
            check(healthy.max_concurrent > 1,
                  f"cloud calls genuinely overlap ({healthy.max_concurrent} in flight)")
            check(nova.runtime.cloud.status()["fallbacks_to_local"] == 0, "no fallback was needed")
    finally:
        healthy.stop()

    check.finish()


run(main)
