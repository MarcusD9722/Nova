from __future__ import annotations

import json

from core.llm_runtime import LLMRuntime
from core.logging_setup import get_logger
from core.policy._json_extract import extract_first_json_object
from core.policy.contracts import SummarizerOutput


logger = get_logger(__name__)


class SummarizerLLM:
    def __init__(self, llm: LLMRuntime, *, llm_semaphore) -> None:
        self._llm = llm
        self._sem = llm_semaphore

    async def summarize(self, *, transcript: str) -> SummarizerOutput:
        sys = (
            "You summarize a conversation for long-term memory. Output ONLY valid JSON.\n\n"
            "Summarizer JSON contract: {\"summary\":\"string\",\"key_facts\":null|[],\"open_loops\":null|[]}\n\n"
            "Rules:\n"
            "- Summary should be short (2-5 sentences).\n"
            "- No moralizing or boilerplate.\n"
        )
        messages = [
            {"role": "system", "content": sys},
            {"role": "user", "content": transcript.strip()},
        ]

        async with self._sem:
            raw = await self._llm.chat(messages, max_tokens=260, temperature=0.2, stop=None)

        raw = (raw or "").strip()
        obj = extract_first_json_object(raw)
        if not obj:
            return SummarizerOutput(summary="", key_facts=None, open_loops=None)
        try:
            return SummarizerOutput.model_validate(obj)
        except Exception as e:  # noqa: BLE001
            logger.debug("summarizer_invalid", error=str(e), raw=raw[:400])
            return SummarizerOutput(summary="", key_facts=None, open_loops=None)
