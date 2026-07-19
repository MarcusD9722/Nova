from __future__ import annotations

import json

from core.llm_runtime import LLMRuntime
from core.logging_setup import get_logger
from core.policy._json_extract import extract_first_json_object
from core.policy.contracts import MemoryExtractorOutput


logger = get_logger(__name__)


class MemoryExtractorLLM:
    def __init__(self, llm: LLMRuntime, *, llm_semaphore) -> None:
        self._llm = llm
        self._sem = llm_semaphore

    async def extract(self, *, user_text: str) -> MemoryExtractorOutput:
        sys = (
            "You extract explicit user facts for long-term memory. "
            "Output ONLY valid JSON (no markdown).\n\n"
            "MemoryExtractorLLM JSON contract:\n"
            "{\"facts\":[{\"entity\":\"user\",\"attribute\":\"name\"|\"location\"|\"spouse\"|\"child\"|\"children_type\"|\"mother\"|\"father\"|\"sibling\"|\"cousin\"|\"pet\"|\"friend\",\"value\":\"string\",\"confidence\":0-1,\"persist\":true}]}\n\n"
            "Rules:\n"
            "- Extract ONLY explicitly stated facts. Never guess.\n"
            "- Capture family relations robustly, e.g. 'I also have a mom named Tara' => mother=Tara.\n"
            "- Capture sibling/friend/cousin names when explicitly stated (lists are ok).\n"
            "- Capture pets when explicitly stated; value can be like 'Mochi|cat' or just 'Mochi'.\n"
            "- If nothing is explicit, return {\"facts\":[]}.\n"
        )
        messages = [
            {"role": "system", "content": sys},
            {"role": "user", "content": user_text.strip()},
        ]

        async with self._sem:
            raw = await self._llm.chat(messages, max_tokens=320, temperature=0.0, stop=None)

        raw = (raw or "").strip()
        obj = extract_first_json_object(raw)
        if not obj:
            return MemoryExtractorOutput(facts=[])
        try:
            return MemoryExtractorOutput.model_validate(obj)
        except Exception as e:  # noqa: BLE001
            logger.debug("memory_extractor_invalid", error=str(e), raw=raw[:400])
            return MemoryExtractorOutput(facts=[])
