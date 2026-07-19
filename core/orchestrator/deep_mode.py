from __future__ import annotations

"""Deep mode: Planner → Executor → Critic (Phase 2.3 of docs/ROADMAP.md).

The honest tradeoff (ARCHITECTURE §4.1): on one 9B on one GPU, every agent
stage is a full, serialized LLM call. A plan + execute + critique(+revise)
chain is 3–5× the latency of a direct answer, so deep mode is strictly
OPT-IN — triggered by an explicit request — and the normal chat path stays
one-pass fast. The Executor stage reuses the existing ToolLoopExecutor
(Phase 2.1); this module adds the Planner and Critic bookends.
"""

import re

from core.logging_setup import get_logger
from core.orchestrator.model_router import ModelRouter

logger = get_logger(__name__)

# Explicit opt-in phrases. Deep mode never triggers on ordinary chat.
_DEEP_RE = re.compile(
    r"\b(deep mode|think (?:hard|deeply|carefully|step by step)|"
    r"take your time|be thorough|work through this carefully|"
    r"plan (?:this|it) out|check your work)\b",
    re.IGNORECASE,
)


def is_deep_request(text: str, *, flag: bool = False) -> bool:
    """True if the user explicitly asked for the careful treatment."""
    if flag:
        return True
    return bool(_DEEP_RE.search(text or ""))


class DeepPipeline:
    def __init__(self, models: ModelRouter) -> None:
        self._models = models

    async def plan(self, *, user_text: str, grounding: str, tool_catalog: str) -> str:
        """A short numbered plan the Executor will follow. Plain text; kept
        brief because it's injected into the tool-loop prompt."""
        prompt = (
            "You are the PLANNER for Nova. Break the user's request into a short, concrete plan "
            "(2–4 numbered steps) the assistant will follow. Note which steps need a tool.\n\n"
            f"Tools available:\n{tool_catalog}\n\n"
            f"Context: {grounding}\n"
            f"User request: {user_text}\n\n"
            "Reply with ONLY the numbered steps — no preamble, no commentary. If the request is "
            "trivial (a greeting, a simple fact), reply with the single line: DIRECT."
        )
        handle = self._models.for_role("planner")
        async with handle.semaphore:
            raw = await handle.runtime.chat(
                [{"role": "user", "content": prompt}], max_tokens=400, temperature=0.2, thinking=True
            )
        plan = (raw or "").strip()
        if not plan or plan.upper().startswith("DIRECT"):
            return ""
        return plan

    async def critique(self, *, user_text: str, plan: str, draft: str,
                       tool_results: list[dict]) -> tuple[str, str]:
        """Review the draft against the request, plan, and real tool results.
        Returns (verdict, notes): verdict is "approve" or "revise"; notes are
        the specific problems to fix when revising."""
        obs = "\n".join(
            f"- {r['tool']}: {'OK ' + str(r['result'])[:300] if r['ok'] else 'FAILED ' + str(r['error'])[:150]}"
            for r in (tool_results or [])
        ) or "(no tools were called)"
        prompt = (
            "You are the CRITIC for Nova. Check the draft reply before it's sent. Judge only:\n"
            "1) Does it actually answer the user's request?\n"
            "2) Is it consistent with the real tool results (no invented facts, no claiming a failed "
            "tool succeeded)?\n"
            "3) Is it honest about anything uncertain or that failed?\n\n"
            f"User request: {user_text}\n"
            f"Plan:\n{plan or '(none)'}\n"
            f"Tool results:\n{obs}\n\n"
            f"Draft reply:\n{draft}\n\n"
            'Reply with ONLY JSON: {"verdict": "approve"} if the draft is good, or '
            '{"verdict": "revise", "notes": "<specific, brief problems to fix>"} if it needs work. '
            "Do not rewrite the reply yourself — only judge it."
        )
        handle = self._models.for_role("critic")
        async with handle.semaphore:
            raw = await handle.runtime.chat(
                [{"role": "user", "content": prompt}], max_tokens=400, temperature=0.1, thinking=True
            )
        from core.policy._json_extract import extract_first_json_object

        obj = extract_first_json_object(raw or "") or {}
        verdict = str(obj.get("verdict") or "").strip().lower()
        if verdict == "revise":
            return "revise", str(obj.get("notes") or "").strip()[:600]
        # Anything not an explicit, parseable "revise" is treated as approve —
        # deep mode must never block a reply on a flaky critic parse.
        return "approve", ""
