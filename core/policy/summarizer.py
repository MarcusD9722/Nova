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
            # SAY WHAT THE ITEMS LOOK LIKE. "null|[]" named the container and
            # not its contents, so the model filled the lists with strings, the
            # contract wanted dicts, and every summary was silently dropped
            # (audit 2026-11-02, H9). The contract now accepts both; the prompt
            # asks for the simpler one.
            "Summarizer JSON contract: {\"summary\":\"string\","
            "\"key_facts\":null|[\"short note\", ...],"
            "\"open_loops\":null|[\"short note\", ...]}\n\n"
            "Rules:\n"
            "- Summary should be short (2-5 sentences).\n"
            "- key_facts and open_loops are plain strings, one short note each, "
            "or null when there are none.\n"
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
            # WARNING, not debug. This failing means the conversation summary is
            # thrown away, and at the production log level (INFO) nobody ever
            # saw it: the real database held 3 summaries and 0 digests for 110
            # conversations and nothing had ever said so (audit, H9/M12).
            logger.warning("summarizer_invalid", error=str(e), raw=raw[:400])
            return SummarizerOutput(summary="", key_facts=None, open_loops=None)
