from __future__ import annotations

"""Memory panel, lessons, background tasks, and reminders endpoints.

Moved verbatim from backend/app.py in Phase 0.6 — behavior unchanged.
"""

import os

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from backend.state import STATE

router = APIRouter()


class MemoryPurgeRequest(BaseModel):
    entity: str = Field(..., description="Fact entity (e.g. 'user').")
    attribute: str | None = Field(None, description="Optional attribute filter (e.g. 'child').")
    value_in: list[str] | None = Field(None, description="Delete values whose LOWER(value) is in this list.")
    value_ilike: str | None = Field(None, description="Delete values whose LOWER(value) matches LIKE '%value_ilike%'.")
    dry_run: bool = Field(True, description="If true, return matches without deleting.")
    limit: int = Field(5000, ge=1, le=20000, description="Max facts to match/delete in one call.")


class MemoryPurgeResponse(BaseModel):
    entity: str
    attribute: str | None
    value_in: list[str] | None
    value_ilike: str | None
    dry_run: bool
    matched: int
    deleted: int
    ids: list[str]


def _require_admin_token(token: str | None) -> None:
    expected = os.getenv("NOVA_ADMIN_TOKEN", "").strip()
    if not expected:
        return
    if (token or "").strip() != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.get("/tasks")
async def tasks_list(status_filter: str | None = Query(None, alias="status"), limit: int = Query(50, ge=1, le=200)) -> dict:
    """List background autonomy tasks for the Tasks panel."""
    if STATE.memory is None:
        raise HTTPException(status_code=503, detail="Not ready")
    items = await STATE.memory.list_tasks(status=status_filter, limit=limit)
    return {"tasks": items}


@router.get("/memory/recent")
async def memory_recent(limit: int = Query(50, ge=1, le=200)) -> dict:
    """Recent long-term memory records for the Memory panel."""
    if STATE.memory is None:
        raise HTTPException(status_code=503, detail="Not ready")
    items = await STATE.memory.recent_memory(limit=limit)
    return {"items": items}


@router.get("/memory/search")
async def memory_search(q: str = Query(min_length=1)) -> dict:
    if STATE.memory is None:
        raise HTTPException(status_code=503, detail="Not ready")
    hits = await STATE.memory.search(q=q, conversation_id=None, limit=12)
    return {"q": q, "results": [h.model_dump() for h in hits]}


@router.post("/memory/purge", response_model=MemoryPurgeResponse)
async def memory_purge(req: MemoryPurgeRequest, admin_token: str | None = Query(None, alias="admin_token")) -> MemoryPurgeResponse:
    """Maintenance endpoint to purge bad/legacy facts from memory stores.

    If env NOVA_ADMIN_TOKEN is set, caller must provide ?admin_token=... .
    """
    _require_admin_token(admin_token)
    if STATE.memory is None:
        raise HTTPException(status_code=503, detail="Memory not initialized")
    result = await STATE.memory.purge_facts(
        entity=req.entity,
        attribute=req.attribute,
        value_in=req.value_in,
        value_ilike=req.value_ilike,
        dry_run=req.dry_run,
        limit=req.limit,
    )
    return MemoryPurgeResponse(**result)


@router.get("/memory/lessons")
async def memory_lessons(limit: int = Query(50)) -> dict:
    if STATE.memory is None:
        raise HTTPException(status_code=503, detail="Not ready")
    return {"lessons": await STATE.memory.lesson_records(limit=int(limit))}


# ── Reminders / scheduling (real "remind me at 5pm", proactive check-ins) ────

class ReminderCreateRequest(BaseModel):
    title: str
    when: str  # "5pm", "in 20 minutes", "tomorrow at 9am", "every morning at 8", ...
    details: str | None = None


@router.post("/reminders")
async def reminders_create(req: ReminderCreateRequest) -> dict:
    from core.dates import parse_reminder_time

    if STATE.memory is None:
        raise HTTPException(status_code=503, detail="Not ready")
    title = (req.title or "").strip()
    if not title:
        raise HTTPException(status_code=422, detail="title is required")
    parsed = parse_reminder_time(req.when or "")
    if parsed is None:
        raise HTTPException(status_code=422, detail=f"Could not understand the time '{req.when}'.")
    due_at, recurrence = parsed
    rid = await STATE.memory.create_reminder(
        title=title, details=(req.details or title), due_at_iso=due_at.isoformat(), recurrence=recurrence,
    )
    return {"reminder_id": str(rid), "title": title, "due_at": due_at.isoformat(), "recurrence": recurrence}


@router.get("/reminders")
async def reminders_list(status: str | None = Query(None), limit: int = Query(50)) -> dict:
    if STATE.memory is None:
        raise HTTPException(status_code=503, detail="Not ready")
    return {"reminders": await STATE.memory.list_reminders(status=status, limit=int(limit))}


@router.delete("/reminders/{reminder_id}")
async def reminders_cancel(reminder_id: str) -> dict:
    if STATE.memory is None:
        raise HTTPException(status_code=503, detail="Not ready")
    await STATE.memory.cancel_reminder(reminder_id=reminder_id)
    return {"reminder_id": reminder_id, "status": "cancelled"}
