from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class MemoryIngestEvent:
    conversation_id: UUID
    user_message: str
    assistant_message: str
    timestamp: datetime
    policy_memory_facts: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class SummarizeHintEvent:
    conversation_id: UUID
    timestamp: datetime
    reason: str = "periodic"


@dataclass(frozen=True)
class AutonomyHintEvent:
    conversation_id: UUID
    timestamp: datetime
    hint: str


@dataclass(frozen=True)
class EpisodicPersistEvent:
    """One thing that happened, on its way to durable memory (V3 P4.1).

    Carries the LIVE artifact objects rather than their ids. The hot store is
    bounded and evicts, so an id could be dangling by the time the worker drains
    the queue — and re-capturing from the tool result would be a second capture
    path, which is exactly what P4.1 must not create.

    `artifact_id` is the parent's id, generated once at capture. It is the
    stable identity that makes persistence idempotent: the same logical event
    observed twice writes the same episode row twice, not two episodes.
    """

    # A string, not a UUID: this comes off the artifact, which stores the
    # conversation id as text. Annotating it UUID would be a quiet lie.
    conversation_id: UUID | str
    turn_id: str
    timestamp: datetime
    artifact: Any                       # memory.artifacts.Artifact (the parent)
    children: list[Any] = field(default_factory=list)
    user_text: str = ""
    reason: str = ""
    kind: str = "tool_result"
    project: str | None = None
    importance: float = 0.5


@dataclass(frozen=True)
class AssistantPostEvent:
    conversation_id: UUID
    timestamp: datetime
    message: str
    reason: str = "autonomy"
