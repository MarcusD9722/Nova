from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class ConversationTurn(BaseModel):
    id: UUID
    conversation_id: UUID
    role: Literal["user", "assistant", "tool"]
    content: str
    created_at: datetime


class FactRecord(BaseModel):
    id: UUID
    entity: str
    attribute: str
    value: str
    confidence: float = Field(ge=0.0, le=1.0, default=0.7)
    created_at: datetime


class PersonRecord(BaseModel):
    id: UUID
    name: str
    attributes: dict[str, str] = Field(default_factory=dict)
    created_at: datetime


class EventRecord(BaseModel):
    id: UUID
    date: str
    note: str
    created_at: datetime


class MemoryHit(BaseModel):
    id: str
    kind: str
    text: str
    score: float
    provenance: dict[str, str]
