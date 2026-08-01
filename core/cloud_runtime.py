from __future__ import annotations

"""Provider-agnostic cloud LLM runtime (U2).

A drop-in stand-in for `LLMRuntime` that talks to a remote model instead of the
local GPU. Because it implements the same surface (`chat`, `chat_stream`,
`generate`), `ModelRouter` can hand it to a role — `coder`, `planner` — with no
caller changes, exactly as designed in Phase 2.4.

Non-negotiables baked in here:

* **Opt-in.** Off unless `NOVA_CLOUD_ENABLED=1` *and* an API key is present.
* **Firewalled.** Every outbound message list passes `core.context_firewall`.
  Personal context is dropped, identities redacted, and if anything personal
  survives the scrub the call is REFUSED and falls back to local (fail closed).
* **Never a hard failure.** Missing key, HTTP error, timeout, refusal → honest
  log + `cloud.*` bus event, then transparently fall back to the local model.
* **Swappable by .env.** Provider, base URL and model are config, not code, so
  any OpenAI-compatible endpoint (OpenAI, OpenRouter, Together, Groq, vLLM,
  LM Studio, …) or Anthropic works without touching this file.
* **Own concurrency.** Remote calls don't contend for the GPU, so this runtime
  carries its own semaphore — which is what finally lets a cloud role run
  *while* the local model is busy.
"""

import json
import os
from typing import Any, AsyncIterator, Callable

import httpx

from core.context_firewall import ScrubResult, scrub_messages, verify_safe
from core.event_bus import BUS
from core.logging_setup import get_logger

logger = get_logger(__name__)

_DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
}


def cloud_enabled() -> bool:
    return os.getenv("NOVA_CLOUD_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}


def cloud_provider() -> str:
    p = (os.getenv("NOVA_CLOUD_PROVIDER", "openai") or "openai").strip().lower()
    return p if p in {"openai", "anthropic"} else "openai"


class CloudUnavailable(RuntimeError):
    """Raised internally when a remote call can't or shouldn't proceed."""


# ── Provider adapters ────────────────────────────────────────────────────────
# Each adapter only translates the request/response shape. Adding a provider is
# a new small class here plus a name in _DEFAULT_BASE_URLS — never a change to
# CloudRuntime or to any caller.

class _OpenAICompatible:
    """OpenAI /chat/completions shape — also OpenRouter, Together, Groq,
    vLLM, LM Studio, and most self-hosted gateways."""

    name = "openai"

    def headers(self, api_key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    def endpoint(self, base_url: str) -> str:
        return f"{base_url.rstrip('/')}/chat/completions"

    def payload(self, *, model: str, messages: list[dict[str, Any]], max_tokens: int,
                temperature: float, stop: list[str] | None, stream: bool) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model, "messages": messages,
            "max_tokens": int(max_tokens), "temperature": float(temperature), "stream": bool(stream),
        }
        if stop:
            body["stop"] = stop
        return body

    def parse(self, data: dict[str, Any]) -> str:
        try:
            return str(data["choices"][0]["message"]["content"] or "")
        except Exception:
            return ""

    def parse_delta(self, obj: dict[str, Any]) -> str:
        try:
            return str(obj["choices"][0]["delta"].get("content") or "")
        except Exception:
            return ""


class _Anthropic:
    """Anthropic /v1/messages shape (system prompt is a top-level field)."""

    name = "anthropic"

    def headers(self, api_key: str) -> dict[str, str]:
        return {
            "x-api-key": api_key,
            "anthropic-version": os.getenv("NOVA_CLOUD_ANTHROPIC_VERSION", "2023-06-01").strip() or "2023-06-01",
            "Content-Type": "application/json",
        }

    def endpoint(self, base_url: str) -> str:
        return f"{base_url.rstrip('/')}/messages"

    def payload(self, *, model: str, messages: list[dict[str, Any]], max_tokens: int,
                temperature: float, stop: list[str] | None, stream: bool) -> dict[str, Any]:
        system_bits = [str(m.get("content") or "") for m in messages if m.get("role") == "system"]
        convo = [m for m in messages if m.get("role") in {"user", "assistant"}]
        if not convo:  # the API requires at least one turn
            convo = [{"role": "user", "content": " ".join(system_bits) or "Continue."}]
            system_bits = []
        body: dict[str, Any] = {
            "model": model, "messages": convo,
            "max_tokens": int(max_tokens), "temperature": float(temperature), "stream": bool(stream),
        }
        if system_bits:
            body["system"] = "\n\n".join(b for b in system_bits if b)
        if stop:
            body["stop_sequences"] = stop
        return body

    def parse(self, data: dict[str, Any]) -> str:
        try:
            return "".join(part.get("text", "") for part in data.get("content", []) if isinstance(part, dict))
        except Exception:
            return ""

    def parse_delta(self, obj: dict[str, Any]) -> str:
        if obj.get("type") == "content_block_delta":
            return str((obj.get("delta") or {}).get("text") or "")
        return ""


_ADAPTERS = {"openai": _OpenAICompatible(), "anthropic": _Anthropic()}


class CloudRuntime:
    """Remote model with the local `LLMRuntime` surface, a firewall, and a
    local fallback. Constructed with the local runtime it falls back to."""

    def __init__(self, *, fallback: Any | None = None,
                 identities: Callable[[], tuple[str | None, list[str]]] | None = None) -> None:
        self._fallback = fallback
        self._identities = identities
        self._last_error: str = ""
        self._calls = 0
        self._fallbacks = 0

    # ── config (re-read each call so .env edits apply on restart only) ──
    @property
    def provider(self) -> str:
        return cloud_provider()

    @property
    def model(self) -> str:
        return (os.getenv("NOVA_CLOUD_MODEL", "") or "").strip()

    @property
    def base_url(self) -> str:
        return (os.getenv("NOVA_CLOUD_BASE_URL", "") or "").strip() or _DEFAULT_BASE_URLS[self.provider]

    @property
    def _api_key(self) -> str:
        return (os.getenv("NOVA_CLOUD_API_KEY", "") or "").strip()

    @property
    def available(self) -> bool:
        return bool(cloud_enabled() and self._api_key and self.model)

    def status(self) -> dict[str, Any]:
        """Operator-visible state. Never includes the key."""
        return {
            "enabled": cloud_enabled(),
            "configured": self.available,
            "provider": self.provider,
            "model": self.model or None,
            "base_url": self.base_url,
            "calls": self._calls,
            "fallbacks_to_local": self._fallbacks,
            "last_error": self._last_error or None,
        }

    # ── firewall ──
    def _prepare(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], ScrubResult]:
        user_name, known = (None, [])
        if self._identities is not None:
            try:
                user_name, known = self._identities()
            except Exception:
                user_name, known = (None, [])
        result = scrub_messages(messages, user_name=user_name, known_names=known)
        surviving = verify_safe(result.messages)
        if surviving:
            # Fail closed: something personal got past the scrub.
            raise CloudUnavailable(f"context firewall refused the call (markers: {surviving[:3]})")
        if not result.messages:
            raise CloudUnavailable("nothing left to send after the firewall scrub")
        return result.messages, result

    async def _post(self, payload: dict[str, Any], *, stream: bool) -> httpx.Response | dict[str, Any]:
        adapter = _ADAPTERS[self.provider]
        timeout = float(os.getenv("NOVA_CLOUD_TIMEOUT_S", "120") or 120)
        url = adapter.endpoint(self.base_url)
        headers = adapter.headers(self._api_key)
        if stream:
            return httpx.AsyncClient(timeout=timeout).stream("POST", url, headers=headers, json=payload)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code >= 400:
                raise CloudUnavailable(f"{self.provider} HTTP {resp.status_code}: {resp.text[:200]}")
            return resp.json()

    async def _fallback_chat(self, reason: str, messages, **kw) -> str:
        self._fallbacks += 1
        self._last_error = reason
        logger.warning("cloud_fallback_to_local", reason=reason[:200])
        BUS.publish("cloud.fallback", {"reason": reason[:160], "provider": self.provider})
        if self._fallback is None:
            raise RuntimeError(f"cloud unavailable and no local fallback: {reason}")
        return await self._fallback.chat(messages, **kw)

    # ── LLMRuntime-compatible surface ──
    async def chat(self, messages: list[dict[str, Any]], max_tokens: int = 512,
                   temperature: float = 0.2, stop: list[str] | None = None,
                   thinking: bool = False) -> str:
        kw = {"max_tokens": max_tokens, "temperature": temperature, "stop": stop, "thinking": thinking}
        if not self.available:
            why = "cloud disabled" if not cloud_enabled() else (
                "NOVA_CLOUD_API_KEY not set" if not self._api_key else "NOVA_CLOUD_MODEL not set")
            return await self._fallback_chat(why, messages, **kw)
        try:
            safe, scrub = self._prepare(messages)
        except CloudUnavailable as e:
            return await self._fallback_chat(str(e), messages, **kw)

        adapter = _ADAPTERS[self.provider]
        payload = adapter.payload(model=self.model, messages=safe, max_tokens=max_tokens,
                                  temperature=temperature, stop=stop, stream=False)
        try:
            data = await self._post(payload, stream=False)
        except Exception as e:  # noqa: BLE001
            return await self._fallback_chat(f"{type(e).__name__}: {e}"[:200], messages, **kw)

        self._calls += 1
        text = adapter.parse(data if isinstance(data, dict) else {})
        logger.info("cloud_call", provider=self.provider, model=self.model,
                    sent_messages=len(safe), firewall=scrub.summary())
        BUS.publish("cloud.call", {"provider": self.provider, "model": self.model,
                                   "firewall": scrub.summary(), "chars": len(text)})
        return text

    async def chat_stream(self, messages: list[dict[str, Any]], max_tokens: int = 512,
                          temperature: float = 0.2, stop: list[str] | None = None,
                          thinking: bool = False) -> AsyncIterator[str]:
        kw = {"max_tokens": max_tokens, "temperature": temperature, "stop": stop, "thinking": thinking}
        if not self.available:
            text = await self._fallback_chat("cloud not configured", messages, **kw)
            yield text
            return
        try:
            safe, scrub = self._prepare(messages)
        except CloudUnavailable as e:
            yield await self._fallback_chat(str(e), messages, **kw)
            return

        adapter = _ADAPTERS[self.provider]
        payload = adapter.payload(model=self.model, messages=safe, max_tokens=max_tokens,
                                  temperature=temperature, stop=stop, stream=True)
        timeout = float(os.getenv("NOVA_CLOUD_TIMEOUT_S", "120") or 120)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", adapter.endpoint(self.base_url),
                                         headers=adapter.headers(self._api_key), json=payload) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", "replace")[:200]
                        raise CloudUnavailable(f"{self.provider} HTTP {resp.status_code}: {body}")
                    self._calls += 1
                    BUS.publish("cloud.call", {"provider": self.provider, "model": self.model,
                                               "firewall": scrub.summary(), "stream": True})
                    async for line in resp.aiter_lines():
                        line = (line or "").strip()
                        if not line.startswith("data:"):
                            continue
                        chunk = line[5:].strip()
                        if chunk == "[DONE]":
                            break
                        try:
                            obj = json.loads(chunk)
                        except Exception:
                            continue
                        delta = adapter.parse_delta(obj)
                        if delta:
                            yield delta
        except Exception as e:  # noqa: BLE001
            yield await self._fallback_chat(f"{type(e).__name__}: {e}"[:200], messages, **kw)

    async def generate(self, prompt: str, max_tokens: int = 512, temperature: float = 0.2,
                       stop: list[str] | None = None, **_: Any) -> str:
        return await self.chat([{"role": "user", "content": prompt}],
                               max_tokens=max_tokens, temperature=temperature, stop=stop)

    # Local-runtime compatibility shims (callers probe these).
    async def initialize(self) -> None:
        return None

    @property
    def model_loaded(self) -> bool:
        return self.available
