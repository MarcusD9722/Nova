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
class AssistantPostEvent:
    conversation_id: UUID
    timestamp: datetime
    message: str
    reason: str = "autonomy"
