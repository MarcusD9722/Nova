"""U2: the provider-agnostic CloudRuntime.

Covers both request shapes, the firewall integration (personal context must
never reach the wire), and — most importantly — that EVERY failure mode falls
back to the local model instead of breaking the turn. Fully offline: httpx is
monkeypatched, so no network and no API key is ever needed.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.cloud_runtime as cr
from core.cloud_runtime import CloudRuntime, _ADAPTERS

_fail = False
SENT: list[dict] = []   # payloads that actually reached "the wire"


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


class FakeLocal:
    """Stand-in for the local LLMRuntime we fall back to."""
    def __init__(self):
        self.calls = 0

    async def chat(self, messages, **kw):
        self.calls += 1
        return "LOCAL-REPLY"


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class _FakeClient:
    """Minimal async context-manager client capturing what would be POSTed."""
    status_code = 200
    payload: dict = {}

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        SENT.append({"url": url, "headers": headers or {}, "body": json or {}})
        return _FakeResponse(self.__class__.status_code, self.__class__.payload,
                             text="boom" if self.__class__.status_code >= 400 else "")


def use_cloud(provider="openai", model="test-model", key="sk-test", enabled=True):
    os.environ["NOVA_CLOUD_ENABLED"] = "1" if enabled else "0"
    os.environ["NOVA_CLOUD_PROVIDER"] = provider
    os.environ["NOVA_CLOUD_MODEL"] = model
    os.environ["NOVA_CLOUD_API_KEY"] = key
    os.environ.pop("NOVA_CLOUD_BASE_URL", None)


async def main():
    global SENT
    cr.httpx.AsyncClient = _FakeClient  # monkeypatch: no real network, ever

    # ── OpenAI-compatible shape ──
    use_cloud("openai")
    _FakeClient.status_code = 200
    _FakeClient.payload = {"choices": [{"message": {"content": "CLOUD-CODE"}}]}
    local = FakeLocal()
    rt = CloudRuntime(fallback=local)
    check(rt.available, "cloud reports available when enabled + key + model set")

    SENT = []
    out = await rt.chat([{"role": "user", "content": "Write a quicksort in Python."}])
    check(out == "CLOUD-CODE", f"openai response parsed (got {out!r})")
    check(SENT and SENT[0]["url"].endswith("/chat/completions"), "openai endpoint used")
    check(SENT[0]["headers"].get("Authorization") == "Bearer sk-test", "bearer auth header set")
    check(SENT[0]["body"]["model"] == "test-model", "model id sent")
    check(local.calls == 0, "no local fallback on success")

    # ── Anthropic shape: system hoisted to a top-level field ──
    use_cloud("anthropic", model="claude-test")
    _FakeClient.payload = {"content": [{"type": "text", "text": "CLAUDE-CODE"}]}
    rt2 = CloudRuntime(fallback=FakeLocal())
    SENT = []
    out2 = await rt2.chat([
        {"role": "system", "content": "You are a senior engineer."},
        {"role": "user", "content": "Write a quicksort."},
    ])
    check(out2 == "CLAUDE-CODE", f"anthropic response parsed (got {out2!r})")
    check(SENT[0]["url"].endswith("/messages"), "anthropic endpoint used")
    check(SENT[0]["headers"].get("x-api-key") == "sk-test", "x-api-key header set (not bearer)")
    check(SENT[0]["body"].get("system") == "You are a senior engineer.", "system hoisted to top-level field")
    check(all(m["role"] != "system" for m in SENT[0]["body"]["messages"]), "system removed from messages array")

    # ── FIREWALL: personal context must never reach the wire ──
    use_cloud("openai")
    _FakeClient.payload = {"choices": [{"message": {"content": "ok"}}]}
    rt3 = CloudRuntime(fallback=FakeLocal(), identities=lambda: ("Marcus", ["Leslie"]))
    SENT = []
    grounding = json.dumps({"known_user": {"name": "Marcus"},
                            "known_family": {"spouse": "Leslie"},
                            "recent_mood": "tired"})
    await rt3.chat([
        {"role": "system", "content": grounding},
        {"role": "user", "content": "Marcus wants a parser; Leslie reviews it."},
    ])
    wire = json.dumps(SENT[0]["body"])
    check("known_family" not in wire, "grounding block never reaches the wire")
    check("recent_mood" not in wire, "mood signal never reaches the wire")
    check("Marcus" not in wire and "Leslie" not in wire, "personal names never reach the wire")
    check("parser" in wire, "the actual task still reaches the wire")

    # ── Fail CLOSED: if only personal content exists, refuse and go local ──
    local4 = FakeLocal()
    rt4 = CloudRuntime(fallback=local4)
    SENT = []
    out4 = await rt4.chat([{"role": "system", "content": grounding}])
    check(out4 == "LOCAL-REPLY", "nothing-left-after-scrub falls back to local")
    check(SENT == [], "no request was sent when the firewall refused")

    # ── Every failure mode falls back to local, never raises ──
    _FakeClient.status_code = 500
    local5 = FakeLocal()
    rt5 = CloudRuntime(fallback=local5)
    check(await rt5.chat([{"role": "user", "content": "hi"}]) == "LOCAL-REPLY", "HTTP 5xx falls back to local")
    check(local5.calls == 1, "local was actually invoked")
    _FakeClient.status_code = 200

    class Boom(_FakeClient):
        async def post(self, *a, **kw):
            raise ConnectionError("network down")
    cr.httpx.AsyncClient = Boom
    local6 = FakeLocal()
    check(await CloudRuntime(fallback=local6).chat([{"role": "user", "content": "hi"}]) == "LOCAL-REPLY",
          "network error falls back to local")
    cr.httpx.AsyncClient = _FakeClient

    # ── Disabled / unconfigured states are honest, not crashes ──
    use_cloud(enabled=False)
    rt7 = CloudRuntime(fallback=FakeLocal())
    check(not rt7.available, "cloud unavailable when disabled")
    check(await rt7.chat([{"role": "user", "content": "hi"}]) == "LOCAL-REPLY", "disabled -> local")

    use_cloud(key="")
    rt8 = CloudRuntime(fallback=FakeLocal())
    check(not rt8.available, "cloud unavailable without an API key")
    check(await rt8.chat([{"role": "user", "content": "hi"}]) == "LOCAL-REPLY", "missing key -> local")

    # ── status() is operator-honest and never leaks the key ──
    use_cloud("openai", key="sk-super-secret")
    st = CloudRuntime(fallback=FakeLocal()).status()
    check(json.dumps(st).find("sk-super-secret") == -1, "status() NEVER exposes the API key")
    check(st["provider"] == "openai" and st["configured"] is True, "status reports provider + configured")

    # ── Base URL override (any compatible gateway) ──
    os.environ["NOVA_CLOUD_BASE_URL"] = "https://openrouter.ai/api/v1"
    check(CloudRuntime().base_url == "https://openrouter.ai/api/v1", "base URL override honored")
    os.environ.pop("NOVA_CLOUD_BASE_URL", None)

    # ── Adapters are pluggable by name ──
    check(set(_ADAPTERS) == {"openai", "anthropic"}, "both provider adapters registered")

    for k in ("NOVA_CLOUD_ENABLED", "NOVA_CLOUD_PROVIDER", "NOVA_CLOUD_MODEL", "NOVA_CLOUD_API_KEY"):
        os.environ.pop(k, None)

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


asyncio.run(main())
