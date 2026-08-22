from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, ConfigDict, field_validator


class _StrictModel(BaseModel):
    # "Strict" here means: we validate types and known fields.
    # In practice, local models often add harmless extra keys; ignore them
    # instead of hard-failing into fallback behavior.
    model_config = ConfigDict(extra="ignore")


# "mom"/"dad" are accepted from the model because that is how people talk, but
# NOTHING downstream reads them: grounding, the singleton-supersession set in
# memory/unifier.py, and every get_latest_fact call all use "mother"/"father".
# Stored raw, such a fact was written successfully and then never found again —
# a memory that silently disappears. Normalized at the contract boundary so
# there is exactly one spelling past this point.
#
# Module-level, NOT a class attribute: pydantic v2 turns a leading-underscore
# class attribute into a ModelPrivateAttr, so `cls._ALIASES.get(...)` raises
# and every fact gets dropped.
_ATTRIBUTE_ALIASES = {"mom": "mother", "dad": "father"}


class MemoryFact(_StrictModel):
    entity: str
    # The allowed set was family/identity only, so the extractor was
    # STRUCTURALLY unable to remember a favourite food, a hobby, or which days
    # Marcus works — those facts were parsed and then dropped on the floor.
    #
    # Everything added below is a durable property of a person, not a passing
    # state. "I'm tired today" must not become a stored fact; "I work Mon-Thu"
    # should. Keep that test in mind before extending this list again.
    attribute: Literal[
        # ── identity & family (original set) ──
        "name",
        "location",
        "spouse",
        "child",
        "children_type",
        "mother",
        "father",
        "sibling",
        "cousin",
        "pet",
        "friend",
        "mom",
        "dad",
        # ── preferences: the "things I like" gap ──
        "likes",
        "dislikes",
        "favorite_food",
        "favorite_drink",
        "favorite_place",
        "favorite_music",
        "favorite_show",
        "favorite_color",
        "hobby",
        "interest",
        "allergy",
        "dietary_restriction",
        # ── work & routine: the "days I work / schedule" gap ──
        "job",
        "employer",
        "work_days",
        "work_hours",
        "routine",
        "goal",
        # ── milestones: the "important dates / trips" gap ──
        "birthday",
        "anniversary",
        "important_date",
        "trip",
        # ── age, WITHOUT the staleness ──
        # A bare `age = 3` is true for a few months and silently false after
        # that, so age is never stored as a timeless scalar. What is stored is
        # the observation and the day it was made (`age_observation` +
        # `age_observed_on`), from which today's age is computed. `birth_date`
        # is recorded only when it can be derived unambiguously, and
        # `birth_date_source` says whether it was stated or derived — Marcus
        # gives an age and a day, almost never a year.
        "age_observation",
        "age_observed_on",
        "birth_date",
        "birth_date_source",
        # ── people detail: the "physical traits" gap ──
        "appearance",
        "vehicle",
        "hometown",
    ]
    value: str
    confidence: float = Field(ge=0.0, le=1.0)
    persist: bool = True

    @field_validator("attribute", mode="before")
    @classmethod
    def _canonical_attribute(cls, v: Any) -> Any:
        if isinstance(v, str):
            return _ATTRIBUTE_ALIASES.get(v.strip().lower(), v)
        return v


class TaskToEnqueue(_StrictModel):
    title: str
    details: str


class ToolPlanItem(_StrictModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class ConversationPolicyOutput(_StrictModel):
    mode: Literal["smalltalk", "task", "clarify", "refuse"] = "smalltalk"
    assistant_reply: str = ""
    follow_up_question: str | None = None
    should_enqueue_task: bool = False
    task_to_enqueue: TaskToEnqueue | None = None
    tool_plan: list[ToolPlanItem] = Field(default_factory=list)
    memory_facts: list[MemoryFact] = Field(default_factory=list)


class MemoryExtractorOutput(_StrictModel):
    facts: list[MemoryFact] = Field(default_factory=list)


class AutonomyPlannerOutput(_StrictModel):
    action: Literal["tool", "ask_user", "enqueue_task", "idle"] = "idle"
    reason: str = ""
    tool_calls: list[ToolPlanItem] = Field(default_factory=list)
    new_tasks: list[dict[str, Any]] = Field(default_factory=list)  # {title, details, priority}
    message_to_user: str | None = None


class FollowUpGeneratorOutput(_StrictModel):
    follow_up_question: str = ""


class SummarizerOutput(_StrictModel):
    summary: str = ""
    key_facts: list[dict[str, Any]] | None = None
    open_loops: list[dict[str, Any]] | None = None
