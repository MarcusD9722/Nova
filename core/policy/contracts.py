from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, ConfigDict


class _StrictModel(BaseModel):
    # "Strict" here means: we validate types and known fields.
    # In practice, local models often add harmless extra keys; ignore them
    # instead of hard-failing into fallback behavior.
    model_config = ConfigDict(extra="ignore")


class MemoryFact(_StrictModel):
    entity: str
    attribute: Literal[
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
    ]
    value: str
    confidence: float = Field(ge=0.0, le=1.0)
    persist: bool = True


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
