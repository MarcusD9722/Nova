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


# ── Knowledge graph (Phase 1.1) ──────────────────────────────────────────────

@router.get("/memory/graph")
async def memory_graph(key: str = Query(min_length=1), limit: int = Query(20, ge=1, le=100)) -> dict:
    """Graph neighbors for one key — powers memory.related and the future
    graph UI panel."""
    if STATE.memory is None:
        raise HTTPException(status_code=503, detail="Not ready")
    return await STATE.memory.related(key, limit=int(limit))


@router.get("/memory/graph/stats")
async def memory_graph_stats() -> dict:
    if STATE.memory is None:
        raise HTTPException(status_code=503, detail="Not ready")
    return await STATE.memory.graph.stats()


@router.get("/memory/graph/subgraph")
async def memory_subgraph(key: str = Query(min_length=1), depth: int = Query(2, ge=1, le=3),
                          limit: int = Query(60, ge=1, le=200)) -> dict:
    """Bounded neighborhood around a node (KG 2.0) — nodes + edges for the graph
    UI or reasoning."""
    if STATE.memory is None:
        raise HTTPException(status_code=503, detail="Not ready")
    return await STATE.memory.graph_subgraph(key, depth=int(depth), limit=int(limit))


@router.get("/memory/path")
async def memory_path(from_: str = Query(alias="from", min_length=1),
                      to: str = Query(min_length=1),
                      max_depth: int = Query(4, ge=1, le=6)) -> dict:
    """Shortest connection path between two nodes (KG 2.0) — 'how are X and Y
    related'. Returns the hop chain or an empty path."""
    if STATE.memory is None:
        raise HTTPException(status_code=503, detail="Not ready")
    path = await STATE.memory.graph_path(from_, to, max_depth=int(max_depth))
    return {"from": from_, "to": to, "connected": bool(path), "path": path}


@router.get("/memory/timeline")
async def memory_timeline(about: str | None = Query(None), days: int = Query(14, ge=1, le=120)) -> dict:
    if STATE.memory is None:
        raise HTTPException(status_code=503, detail="Not ready")
    return {"entries": await STATE.memory.timeline(about=about, days=days)}


@router.get("/memory/world")
async def memory_world(subject: str | None = Query(None), q: str | None = Query(None)) -> dict:
    """Semantic world model (#11): recall what Nova knows about a subject, or
    keyword-search across world facts. Each triple carries source + confidence."""
    if STATE.memory is None:
        raise HTTPException(status_code=503, detail="Not ready")
    if subject:
        return await STATE.memory.world_recall(subject)
    if q:
        return {"query": q, "results": await STATE.memory.world_search(q)}
    return {"stats": await STATE.memory.world.stats()}


@router.get("/thoughts")
async def memory_thoughts(kind: str | None = Query(None), topic: str | None = Query(None),
                          limit: int = Query(30, ge=1, le=100)) -> dict:
    """Nova's persistent internal thoughts (#6) — private notes surfaced on
    request. Ideas, unresolved questions, potential improvements, discoveries."""
    if STATE.memory is None:
        raise HTTPException(status_code=503, detail="Not ready")
    thoughts = await STATE.memory.recall_thoughts(topic=topic, kind=kind, limit=int(limit))
    return {"thoughts": thoughts, "stats": await STATE.memory.thoughts.stats()}


@router.get("/twin")
async def digital_twin() -> dict:
    """Marcus's personal digital-twin profile (#4): working-pattern predictions
    derived from recorded signals. Predicts, never impersonates."""
    if STATE.memory is None:
        raise HTTPException(status_code=503, detail="Not ready")
    return await STATE.memory.digital_twin_profile()


@router.get("/executive")
async def executive_brief() -> dict:
    """Executive recommendations (#1): confidence-gated proactive suggestions
    synthesized from goals, reminders, habits, and the digital-twin profile."""
    if STATE.memory is None:
        raise HTTPException(status_code=503, detail="Not ready")
    return {"recommendations": await STATE.memory.executive_recommendations(throttle=False)}


@router.get("/plans/{goal_id}")
async def goal_plan(goal_id: str) -> dict:
    """A goal's long-term plan (#3): milestones, dated items, and progress."""
    if STATE.memory is None:
        raise HTTPException(status_code=503, detail="Not ready")
    from core.goal_planner import progress
    plan = await STATE.memory.load_plan(goal_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="No plan for this goal")
    return {"goal_id": goal_id, "plan": plan, "progress": progress(plan)}


@router.get("/research")
async def research(topic: str | None = Query(None)) -> dict:
    """Autonomous research (#9): tracked topics, or the sourced findings for one
    topic. Every finding carries its citation; nothing is fabricated."""
    if STATE.memory is None:
        raise HTTPException(status_code=503, detail="Not ready")
    if topic:
        return {"topic": topic, "findings": await STATE.memory.research_findings(topic)}
    return {"topics": await STATE.memory.list_research_topics()}


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
