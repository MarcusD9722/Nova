from __future__ import annotations

"""LLM-driven expression: deterministic *what*, fluid *how* (U4).

Several systems decide something correctly and then say it with a hardcoded
f-string, so Nova sounds identical forever — the executive nudge, the mood read,
a learned skill's name. This module lets the model do the *phrasing* while the
deterministic layer keeps deciding *whether* and *what*.

That split is deliberate and load-bearing. Executive intelligence, for example,
must keep its rule-based detection and confidence gate exactly as-is — that's
what stops it being annoying. Only the wording becomes fluid. If the model is
unavailable or misbehaves, the original template is used verbatim, so this can
never degrade an existing behavior.

Gated by NOVA_LLM_EXPRESSION (default on).
"""

import os
import re
from typing import Any, Sequence

from core.logging_setup import get_logger

logger = get_logger(__name__)


def expression_enabled() -> bool:
    return os.getenv("NOVA_LLM_EXPRESSION", "1").strip().lower() not in {"0", "false", "no", "off"}


class Expression:
    """Rephrasing/naming helpers. Every method falls back to its input."""

    def __init__(self, llm: Any | None = None, *, semaphore: Any | None = None,
                 timeout_s: float = 12.0) -> None:
        self._llm = llm
        self._sem = semaphore
        self._timeout = timeout_s

    @property
    def available(self) -> bool:
        return bool(self._llm is not None and expression_enabled())

    async def _ask(self, prompt: str, *, max_tokens: int = 200, temperature: float = 0.4) -> str:
        import asyncio

        async def _call() -> str:
            if self._sem is not None:
                async with self._sem:
                    return await self._llm.chat([{"role": "user", "content": prompt}],
                                                max_tokens=max_tokens, temperature=temperature, thinking=False)
            return await self._llm.chat([{"role": "user", "content": prompt}],
                                        max_tokens=max_tokens, temperature=temperature, thinking=False)

        try:
            return (await asyncio.wait_for(_call(), timeout=self._timeout) or "").strip()
        except Exception as e:  # noqa: BLE001
            logger.debug("expression_call_failed", error=str(e)[:160])
            return ""

    @staticmethod
    def _sane(text: str, *, max_chars: int) -> str:
        """Reject model output that's empty, over-long, or has leaked framing."""
        t = (text or "").strip().strip('"').strip()
        if not t or len(t) > max_chars:
            return ""
        low = t.lower()
        if low.startswith(("here's", "here is", "sure,", "certainly", "as an ai")):
            return ""
        if "\n\n" in t:  # a paragraph dump, not a one-liner
            return ""
        return t

    async def rephrase(self, template: str, *, context: str = "", max_chars: int = 220) -> str:
        """Say the same thing, naturally. Returns `template` unchanged on any
        problem — the meaning is fixed by the caller, only the wording varies."""
        if not self.available or not (template or "").strip():
            return template
        prompt = (
            "Rewrite this assistant note so it sounds natural and conversational. "
            "Keep the EXACT same meaning, facts, and any names/numbers. One short sentence. "
            "Do not add new information, do not add pleasantries, do not explain yourself.\n"
            + (f"Situation: {context.strip()[:300]}\n" if context.strip() else "")
            + f"Note: {template.strip()[:400]}\n\nRewritten:"
        )
        out = self._sane(await self._ask(prompt, max_tokens=120), max_chars=max_chars)
        return out or template

    async def name_for(self, steps: Sequence[str], *, fallback: str) -> str:
        """A short human name for a detected workflow (U4/#2)."""
        if not self.available or not steps:
            return fallback
        listed = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(list(steps)[:8]))
        prompt = ("Give this repeated workflow a short, human name (2-4 words, Title Case). "
                  "Reply with only the name.\n" + listed + "\n\nName:")
        out = self._sane(await self._ask(prompt, max_tokens=24, temperature=0.2), max_chars=48)
        if not out or len(out.split()) > 6:
            return fallback
        return re.sub(r"[^A-Za-z0-9 &/-]", "", out).strip() or fallback

    async def read_signal(self, evidence: str, *, labels: Sequence[str], fallback: str) -> str:
        """Read a coarse label (e.g. stress level) from free-text evidence
        instead of substring-matching a fixed word list."""
        if not self.available or not (evidence or "").strip():
            return fallback
        allowed = [str(x) for x in labels]
        prompt = (
            f"Based only on this evidence, choose ONE label from: {', '.join(allowed)}.\n"
            f"Evidence: {evidence.strip()[:500]}\n\nReply with only the label."
        )
        out = (await self._ask(prompt, max_tokens=16, temperature=0.0)).strip().strip('".').lower()
        for label in allowed:
            if out == label.lower():
                return label
        return fallback
