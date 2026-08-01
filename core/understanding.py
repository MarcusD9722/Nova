from __future__ import annotations

"""LLM-driven understanding with deterministic fallback (U3).

Nova decided a lot of things with regexes: is this a task or chat, which
specialist should answer, what terms to search memory for. Pattern matching only
works on phrasings someone thought of — "how do I get to Chipotle" matches,
"what's the best way over to Chipotle" doesn't — and it fails *silently*.

This module routes those judgements through the model instead, with one
non-negotiable property: **it is never worse than the heuristic it replaces.**
Every call takes a `fallback` and returns it when the model is unavailable,
errors, times out, or answers with something that isn't in the allowed set. The
deterministic path stays in the codebase as the safety net, not as the decider.

Gated by NOVA_LLM_UNDERSTANDING (default on). Turning it off restores the pure
heuristic behavior exactly.
"""

import json
import os
import re
from typing import Any, Sequence

from core.logging_setup import get_logger
from core.policy._json_extract import extract_first_json_object

logger = get_logger(__name__)


def understanding_enabled() -> bool:
    return os.getenv("NOVA_LLM_UNDERSTANDING", "1").strip().lower() not in {"0", "false", "no", "off"}


class Understanding:
    """Small, reusable LLM judgement calls. Every method is fallback-safe."""

    def __init__(self, llm: Any | None = None, *, semaphore: Any | None = None,
                 timeout_s: float = 12.0) -> None:
        self._llm = llm
        self._sem = semaphore
        self._timeout = timeout_s

    @property
    def available(self) -> bool:
        return bool(self._llm is not None and understanding_enabled())

    async def _ask(self, prompt: str, *, max_tokens: int = 160) -> str:
        import asyncio

        async def _call() -> str:
            if self._sem is not None:
                async with self._sem:
                    return await self._llm.chat(
                        [{"role": "user", "content": prompt}],
                        max_tokens=max_tokens, temperature=0.0, thinking=False,
                    )
            return await self._llm.chat(
                [{"role": "user", "content": prompt}],
                max_tokens=max_tokens, temperature=0.0, thinking=False,
            )

        try:
            return (await asyncio.wait_for(_call(), timeout=self._timeout) or "").strip()
        except Exception as e:  # noqa: BLE001
            logger.debug("understanding_call_failed", error=str(e)[:160])
            return ""

    async def classify(self, text: str, *, labels: Sequence[str], fallback: str,
                       instruction: str = "") -> str:
        """Pick one of `labels` for `text`. Returns `fallback` on any problem —
        including a model answer that isn't one of the allowed labels."""
        if not self.available or not (text or "").strip():
            return fallback
        allowed = [str(x) for x in labels]
        prompt = (
            f"{instruction}\n\n" if instruction else ""
        ) + (
            f"Classify the message into EXACTLY ONE of these labels: {', '.join(allowed)}.\n"
            f"Message: {text.strip()[:800]}\n\n"
            'Reply with only JSON: {"label": "<one label>"}'
        )
        raw = await self._ask(prompt, max_tokens=60)
        if not raw:
            return fallback
        obj = extract_first_json_object(raw) or {}
        label = str(obj.get("label") or "").strip()
        if label in allowed:
            return label
        # Tolerate a bare label with no JSON wrapper.
        bare = raw.strip().strip('"').strip()
        if bare in allowed:
            return bare
        logger.debug("understanding_label_rejected", got=raw[:80])
        return fallback

    async def rank(self, text: str, *, options: dict[str, str], fallback: list[str],
                   limit: int = 3, instruction: str = "") -> list[str]:
        """Choose the most relevant option ids for `text`. `options` maps id ->
        description. Unknown ids are discarded; an empty result falls back."""
        if not self.available or not options or not (text or "").strip():
            return fallback
        catalog = "\n".join(f"- {oid}: {desc}" for oid, desc in options.items())
        prompt = (
            f"{instruction}\n\n" if instruction else ""
        ) + (
            f"Given the request, choose up to {limit} of the most relevant entries.\n"
            f"Entries:\n{catalog}\n\nRequest: {text.strip()[:800]}\n\n"
            'Reply with only JSON: {"ids": ["id1", "id2"]}'
        )
        raw = await self._ask(prompt, max_tokens=120)
        obj = extract_first_json_object(raw) or {}
        ids = obj.get("ids")
        if not isinstance(ids, list):
            return fallback
        picked = [str(i).strip() for i in ids if str(i).strip() in options]
        return picked[:limit] or fallback

    async def expand_query(self, query: str, *, fallback: list[str], limit: int = 8) -> list[str]:
        """Rewrite a natural question into memory search terms (synonyms,
        alternate phrasings). Falls back to the caller's term list."""
        if not self.available or not (query or "").strip():
            return fallback
        prompt = (
            "Extract the key search terms for looking this up in a personal memory "
            "database. Include obvious synonyms (mom/mother, dad/father). Single words "
            "or short phrases, lowercase, no stopwords.\n"
            f"Question: {query.strip()[:400]}\n\n"
            'Reply with only JSON: {"terms": ["term1", "term2"]}'
        )
        raw = await self._ask(prompt, max_tokens=120)
        obj = extract_first_json_object(raw) or {}
        terms = obj.get("terms")
        if not isinstance(terms, list):
            return fallback
        cleaned: list[str] = []
        for t in terms:
            t = re.sub(r"[^a-z0-9' ]", "", str(t).strip().lower()).strip()
            if len(t) >= 2 and t not in cleaned:
                cleaned.append(t)
        # Union with the deterministic terms: the model adds recall, it never
        # silently removes a term the heuristic found.
        merged = list(dict.fromkeys([*fallback, *cleaned]))
        return merged[:limit] or fallback

    async def extract(self, text: str, *, fields: dict[str, str], fallback: dict[str, Any] | None = None,
                      instruction: str = "") -> dict[str, Any]:
        """Pull structured slots out of a message. `fields` maps name ->
        description. Missing/failed extraction returns `fallback` (or {})."""
        fallback = fallback if fallback is not None else {}
        if not self.available or not (text or "").strip():
            return fallback
        spec = "\n".join(f'- "{k}": {v}' for k, v in fields.items())
        prompt = (
            f"{instruction}\n\n" if instruction else ""
        ) + (
            f"Extract these fields from the message. Use null when absent.\nFields:\n{spec}\n"
            f"Message: {text.strip()[:800]}\n\nReply with only a JSON object of those fields."
        )
        raw = await self._ask(prompt, max_tokens=200)
        obj = extract_first_json_object(raw)
        if not isinstance(obj, dict):
            return fallback
        return {k: obj.get(k) for k in fields if obj.get(k) not in (None, "", [])} or fallback
