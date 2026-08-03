"""U6: cloud spend tracking + daily cap, and parallel file generation.

The cap is a financial safety rail: every project build hits a paid API, so
running away must be impossible. It reuses the existing local-fallback path, so
hitting the cap slows Nova down but never fails a turn.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.cloud_runtime as cr
from core.cloud_runtime import CloudRuntime

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


class FakeLocal:
    def __init__(self): self.calls = 0
    async def chat(self, messages, **kw):
        self.calls += 1
        return "LOCAL-REPLY"


class _Client:
    payload = {"choices": [{"message": {"content": "CLOUD"}}],
               "usage": {"prompt_tokens": 400, "completion_tokens": 600}}
    def __init__(self, *a, **kw): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, url, headers=None, json=None):
        class R:
            status_code = 200
            text = ""
            def json(self_inner): return _Client.payload
        return R()


async def main():
    cr.httpx.AsyncClient = _Client
    os.environ.update({"NOVA_CLOUD_ENABLED": "1", "NOVA_CLOUD_PROVIDER": "openai",
                       "NOVA_CLOUD_MODEL": "m", "NOVA_CLOUD_API_KEY": "k"})
    os.environ.pop("NOVA_CLOUD_DAILY_TOKEN_CAP", None)
    os.environ.pop("NOVA_CLOUD_COST_PER_MTOK_IN", None)
    os.environ.pop("NOVA_CLOUD_COST_PER_MTOK_OUT", None)

    # ── tokens accumulate from the provider response ──
    rt = CloudRuntime(fallback=FakeLocal())
    await rt.chat([{"role": "user", "content": "write a parser for me please"}])
    st = rt.status()["today"]
    check(st["prompt_tokens"] == 400 and st["completion_tokens"] == 600, f"tokens recorded (got {st})")
    await rt.chat([{"role": "user", "content": "write another parser please"}])
    check(rt.status()["today"]["total_tokens"] == 2000, "tokens accumulate across calls")

    # ── cost estimate uses configured rates ──
    os.environ["NOVA_CLOUD_COST_PER_MTOK_IN"] = "2.50"
    os.environ["NOVA_CLOUD_COST_PER_MTOK_OUT"] = "10.00"
    cost = rt.estimated_cost_usd()          # 800/1e6*2.5 + 1200/1e6*10
    check(abs(cost - 0.014) < 1e-6, f"cost estimated from rates (got ${cost})")
    check("estimated_cost_usd" in rt.status()["today"], "cost surfaced on status")

    # ── the API key NEVER appears in status, even with spend data ──
    # (use a distinctive key — asserting on a single letter matches ordinary
    # field names like "prompt_tokens" and proves nothing)
    os.environ["NOVA_CLOUD_API_KEY"] = "sk-DISTINCTIVE-SECRET-9134"
    leaky = CloudRuntime(fallback=FakeLocal())
    leaky._prompt_tokens, leaky._completion_tokens = 800, 1200
    check("DISTINCTIVE-SECRET" not in json.dumps(leaky.status()),
          "status never leaks the API key, even alongside spend data")
    os.environ["NOVA_CLOUD_API_KEY"] = "k"

    # ── CAP: once exceeded, cloud is unavailable and we fall back to local ──
    os.environ["NOVA_CLOUD_DAILY_TOKEN_CAP"] = "1500"     # already at 2000
    local = FakeLocal()
    capped = CloudRuntime(fallback=local)
    capped._prompt_tokens, capped._completion_tokens = 900, 900
    check(not capped.available, "over the cap -> cloud reports unavailable")
    out = await capped.chat([{"role": "user", "content": "one more build please"}])
    check(out == "LOCAL-REPLY" and local.calls == 1, "over the cap -> falls back to local, turn still succeeds")
    check("cap" in (capped.status()["last_error"] or "").lower(), "the reason names the cap honestly")

    # ── under the cap, cloud is used normally ──
    os.environ["NOVA_CLOUD_DAILY_TOKEN_CAP"] = "100000"
    fine = CloudRuntime(fallback=FakeLocal())
    check(fine.available, "under the cap -> cloud available")
    check(await fine.chat([{"role": "user", "content": "build me a thing"}]) == "CLOUD", "under the cap -> cloud used")

    # ── no cap configured = unlimited (previous behavior) ──
    os.environ["NOVA_CLOUD_DAILY_TOKEN_CAP"] = "0"
    unl = CloudRuntime(fallback=FakeLocal())
    unl._prompt_tokens = 10_000_000
    check(unl.available, "cap of 0 means no cap")

    # ── day rollover resets the counters ──
    roll = CloudRuntime(fallback=FakeLocal())
    roll._prompt_tokens, roll._completion_tokens, roll._capped = 500, 500, True
    roll._day = "1999-01-01"
    roll._roll_day()
    check(roll.status()["today"]["total_tokens"] == 0 and not roll._capped, "counters reset on a new day")

    for k in ("NOVA_CLOUD_ENABLED", "NOVA_CLOUD_MODEL", "NOVA_CLOUD_API_KEY",
              "NOVA_CLOUD_DAILY_TOKEN_CAP", "NOVA_CLOUD_COST_PER_MTOK_IN", "NOVA_CLOUD_COST_PER_MTOK_OUT"):
        os.environ.pop(k, None)

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


asyncio.run(main())
