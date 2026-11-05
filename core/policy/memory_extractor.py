from __future__ import annotations

import json

from core.llm_runtime import LLMRuntime
from core.logging_setup import get_logger
from core.policy._json_extract import extract_first_json_object
from core.policy.contracts import (MemoryExtractorOutput, MemoryFact,
                                   split_people_value)


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
            '{"facts":[{"entity":"user","attribute":"<one of the list below>",'
            '"value":"string","confidence":0-1,"persist":true}]}\n\n'
            "Allowed attributes:\n"
            "  identity/family: name, location, spouse, child, children_type, mother, father,\n"
            "                   sibling, cousin, pet, friend\n"
            "  preferences:     likes, dislikes, favorite_food, favorite_drink, favorite_place,\n"
            "                   favorite_music, favorite_show, favorite_color, hobby, interest,\n"
            "                   allergy, dietary_restriction\n"
            "  work/routine:    job, employer, work_days, work_hours, routine, goal\n"
            "  milestones:      birthday, anniversary, important_date, trip\n"
            "  person detail:   appearance, vehicle, hometown\n\n"
            "Rules:\n"
            "- Extract ONLY explicitly stated facts. Never guess.\n"
            "- Do NOT extract ages. 'Mateo is three years old' is handled\n"
            "  elsewhere, deterministically, because an age is only true for a\n"
            "  few months and the date it was said has to be recorded with it.\n"
            "  There is no `age` attribute and adding one will be dropped. A\n"
            "  stated birthday IS wanted: 'his birthday is September 16' =>\n"
            "  attribute=birthday, value=09-16.\n"
            "- Capture family relations robustly, e.g. 'I also have a mom named Tara' => mother=Tara.\n"
            "- Capture sibling/friend/cousin names when explicitly stated (lists are ok).\n"
            "- Capture pets when explicitly stated; value can be like 'Mochi|cat' or just 'Mochi'.\n"
            "- DURABLE properties only. 'I work Monday to Thursday' is durable (work_days);\n"
            "  'I'm tired today' is a passing mood and must NOT be stored. If it would stop\n"
            "  being true tomorrow, leave it out.\n"
            "- `entity` is WHO the fact is about: 'user' for Marcus, otherwise the person's\n"
            "  name. 'Leslie has red hair' => entity=Leslie, attribute=appearance.\n"
            "- An attribute outside the list above is dropped, so pick the closest one rather\n"
            "  than inventing a new name.\n"
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

        # Validate facts INDIVIDUALLY. Validating the batch meant one
        # unsupported attribute ("favorite_color") discarded every good fact
        # beside it — "my wife is Leslie and my son is Mateo and my favorite
        # colour is blue" remembered NOTHING, silently, at debug level. A
        # single junk entry is a normal small-model slip; losing the whole
        # message over it is not acceptable.
        raw_facts = obj.get("facts")
        if not isinstance(raw_facts, list):
            logger.debug("memory_extractor_no_facts_list", raw=raw[:400])
            return MemoryExtractorOutput(facts=[])

        # Ages are NOT this extractor's job — see the rule in the prompt above
        # and the note in `contracts.MemoryFact`. `core.personal_dates` captures
        # them deterministically during ingestion, stamping the observation date
        # itself. If a model emits `age` or one of the normalized age fields
        # anyway, validation drops that one fact here and keeps the rest.
        kept: list[MemoryFact] = []
        dropped = 0
        split = 0
        for item in raw_facts:
            try:
                fact = MemoryFact.model_validate(item)
            except Exception as e:  # noqa: BLE001
                dropped += 1
                # DEBUG on purpose (audit 2026-11-02, M12, which raised 56 real
                # failures to warning): an expected small-model slip, and the INFO
                # `memory_extractor_partial` line below reports the count.
                logger.debug("memory_extractor_fact_dropped", error=str(e)[:200], item=str(item)[:200])
                continue
            # ONE PERSON PER FACT. The prompt invites lists, and the model
            # obliges with child="Mateo|Liam" — which was stored verbatim, so
            # the graph held one child named "Mateo|Liam", nothing could answer
            # a question about Liam, and the context firewall had no "Liam" to
            # redact (audit, M11). `pet` is excluded there: its "|" separates a
            # name from a species.
            names = split_people_value(fact.attribute, fact.value)
            if len(names) > 1:
                split += 1
                for name in names:
                    kept.append(fact.model_copy(update={"value": name}))
            else:
                kept.append(fact)
        if dropped:
            logger.info("memory_extractor_partial", kept=len(kept), dropped=dropped)
        if split:
            logger.info("memory_extractor_split_people", values=split,
                        facts=len(kept))
        return MemoryExtractorOutput(facts=kept)
