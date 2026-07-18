from __future__ import annotations

import json

from core.llm_runtime import LLMRuntime
from core.logging_setup import get_logger
from core.policy._json_extract import extract_first_json_object
from core.policy.contracts import AutonomyPlannerOutput


logger = get_logger(__name__)


class AutonomyPlannerLLM:
    def __init__(self, llm: LLMRuntime, *, llm_semaphore) -> None:
        self._llm = llm
        self._sem = llm_semaphore

    async def plan(
        self,
        *,
        title: str,
        details: str,
        memory_context: str,
        available_tools: list[str],
    ) -> AutonomyPlannerOutput:
        sys = (
            "You are Nova's autonomy planner. Output ONLY valid JSON (no markdown).\n\n"
            "AutonomyPlanner JSON contract:\n"
            "{\n"
            "  \"action\":\"tool\"|\"ask_user\"|\"enqueue_task\"|\"idle\",\n"
            "  \"reason\":\"string\",\n"
            "  \"tool_calls\":[{\"tool\":\"string\",\"args\":{}}] | [],\n"
            "  \"new_tasks\":[{\"title\":\"string\",\"details\":\"string\",\"priority\":1-5}] | [],\n"
            "  \"message_to_user\":\"string\"|null\n"
            "}\n\n"
            "Rules:\n"
            "- Prefer idle if unclear.\n"
            "- Only choose tools from available_tools.\n"
            "- Never claim a tool result you didn't execute.\n"
        )
        payload = {
            "title": title,
            "details": details,
            "memory_context": (memory_context or "").strip(),
            "available_tools": available_tools,
        }
        messages = [
            {"role": "system", "content": sys},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]

        async with self._sem:
            raw = await self._llm.chat(messages, max_tokens=360, temperature=0.2, stop=None)

        raw = (raw or "").strip()
        obj = extract_first_json_object(raw)
        if not obj:
            return AutonomyPlannerOutput(action="idle", reason="planner_unparseable", tool_calls=[], new_tasks=[], message_to_user=None)
        try:
            out = AutonomyPlannerOutput.model_validate(obj)
        except Exception as e:  # noqa: BLE001
            logger.debug("autonomy_planner_invalid", error=str(e), raw=raw[:400])
            return AutonomyPlannerOutput(action="idle", reason="planner_invalid", tool_calls=[], new_tasks=[], message_to_user=None)

        # guard: strip tools not in registry
        out.tool_calls = [tc for tc in out.tool_calls if tc.tool in set(available_tools)]
        return out
