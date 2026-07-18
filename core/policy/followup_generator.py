from __future__ import annotations

import json

from core.llm_runtime import LLMRuntime
from core.logging_setup import get_logger
from core.policy._json_extract import extract_first_json_object
from core.policy.contracts import FollowUpGeneratorOutput


logger = get_logger(__name__)


class FollowUpGeneratorLLM:
    def __init__(self, llm: LLMRuntime, *, llm_semaphore) -> None:
        self._llm = llm
        self._sem = llm_semaphore

    async def regenerate(self, *, user_text: str, avoid: list[str]) -> FollowUpGeneratorOutput:
        sys = (
            "You generate ONE non-generic follow-up question for smalltalk. Output ONLY valid JSON.\n\n"
            "FollowUpGenerator JSON contract: {\"follow_up_question\":\"string\"}\n\n"
            "Rules:\n"
            "- Must be a single question ending with '?'.\n"
            "- Must NOT repeat any question in avoid.\n"
            "- Must be grounded in the user's last message.\n"
        )
        payload = {"user_text": user_text.strip(), "avoid": avoid}
        messages = [
            {"role": "system", "content": sys},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]

        async with self._sem:
            raw = await self._llm.chat(messages, max_tokens=120, temperature=0.35, stop=None)

        obj = extract_first_json_object((raw or "").strip())
        if not obj:
            return FollowUpGeneratorOutput(follow_up_question="")
        try:
            out = FollowUpGeneratorOutput.model_validate(obj)
        except Exception as e:  # noqa: BLE001
            logger.debug("followup_invalid", error=str(e), raw=(raw or "")[:300])
            return FollowUpGeneratorOutput(follow_up_question="")
        q = (out.follow_up_question or "").strip()
        if not q.endswith("?"):
            q = q.rstrip(".! ") + "?"
        return FollowUpGeneratorOutput(follow_up_question=q)
