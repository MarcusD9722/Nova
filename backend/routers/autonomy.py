from __future__ import annotations

"""Autonomy / self-improvement + goals endpoints.

Moved verbatim from backend/app.py in Phase 0.6 — behavior unchanged.
"""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from backend.state import STATE

router = APIRouter()


# ── Autonomy / self-improvement (bounded, killable) ──────────────────────────

@router.get("/autonomy/status")
async def autonomy_status() -> dict:
    if STATE.runtime is None:
        return {"available": False}
    try:
        return {"available": True, **STATE.runtime.self_improve.status()}
    except Exception as e:  # noqa: BLE001
        return {"available": False, "error": str(e)}


@router.post("/autonomy/stop")
async def autonomy_stop() -> dict:
    """Kill switch: pause the proactive self-improvement loop and cancel any
    queued background work. Error capture keeps running (harmless)."""
    if STATE.runtime is None:
        raise HTTPException(status_code=503, detail="Not ready")
    STATE.runtime.self_improve.set_enabled(False)
    try:
        if STATE.memory is not None:
            await STATE.memory.cancel_pending_background_work()
    except Exception:
        pass
    return {"enabled": False}


@router.post("/autonomy/start")
async def autonomy_start() -> dict:
    if STATE.runtime is None:
        raise HTTPException(status_code=503, detail="Not ready")
    STATE.runtime.self_improve.set_enabled(True)
    return {"enabled": True}


@router.get("/autonomy/errors")
async def autonomy_errors(limit: int = Query(50)) -> dict:
    if STATE.runtime is None:
        raise HTTPException(status_code=503, detail="Not ready")
    recent = await STATE.runtime.error_log.recent(limit=int(limit))
    recurring = await STATE.runtime.error_log.recurring(min_count=2)
    return {"recent": recent, "recurring": recurring}


@router.get("/autonomy/metrics")
async def autonomy_metrics() -> dict:
    """Live self-evaluation metrics (Phase 2.5): reply latency, tool failure
    rate, empty replies, vision errors — today so far. Snapshotted daily into
    a self_eval fact by the self-improve worker."""
    if STATE.runtime is None:
        raise HTTPException(status_code=503, detail="Not ready")
    return STATE.runtime.self_improve.metrics()


@router.get("/autonomy/state")
async def autonomy_internal_state() -> dict:
    """Nova's internal operational state (#12): confidence, uncertainty,
    mental_workload, focus, energy, curiosity, learning_rate — each with the
    real signal it was derived from. Internal reasoning metrics, not feelings."""
    if STATE.runtime is None:
        raise HTTPException(status_code=503, detail="Not ready")
    return await STATE.runtime.self_improve.internal_state()


@router.get("/autonomy/benchmarks")
async def autonomy_benchmarks(days: int = Query(30)) -> dict:
    """Self-benchmark report (#14): trends and regressions across the daily
    self-eval history (latency, tool success, empty replies, internal state).
    Quality dimensions with no measurement harness yet are reported honestly
    under `not_measured`, never faked."""
    if STATE.runtime is None:
        raise HTTPException(status_code=503, detail="Not ready")
    return await STATE.runtime.self_improve.benchmark_report(days=int(days))


# ── Goals (multi-session objectives, advanced by AgentSupervisor) ────────────

class GoalCreateRequest(BaseModel):
    objective: str
    title: str | None = None
    success_criteria: str | None = None
    project: str = "general"


@router.post("/goals")
async def goals_create(req: GoalCreateRequest) -> dict:
    if STATE.memory is None:
        raise HTTPException(status_code=503, detail="Not ready")
    objective = (req.objective or "").strip()
    if not objective:
        raise HTTPException(status_code=422, detail="objective is required")
    gid = await STATE.memory.create_goal(
        project_name=(req.project or "general"),
        title=(req.title or objective[:60]),
        objective=objective,
        success_criteria=(req.success_criteria or ""),
    )
    await STATE.memory.enqueue_goal_task(
        goal_id=gid, project_name=(req.project or "general"), tool_name="__decide__", args={}
    )
    return {"goal_id": str(gid), "title": req.title or objective[:60], "project": req.project or "general"}


@router.get("/goals")
async def goals_list(project: str | None = Query(None), limit: int = Query(50)) -> dict:
    if STATE.memory is None:
        raise HTTPException(status_code=503, detail="Not ready")
    return {"goals": await STATE.memory.list_goals(project_name=project, limit=int(limit))}


@router.get("/goals/{goal_id}/tasks")
async def goals_tasks(goal_id: str, limit: int = Query(50)) -> dict:
    if STATE.memory is None:
        raise HTTPException(status_code=503, detail="Not ready")
    return {"goal_id": goal_id, "tasks": await STATE.memory.list_goal_tasks(goal_id=goal_id, limit=int(limit))}


@router.post("/goals/{goal_id}/cancel")
async def goals_cancel(goal_id: str) -> dict:
    if STATE.memory is None:
        raise HTTPException(status_code=503, detail="Not ready")
    await STATE.memory.update_goal_status(goal_id=UUID(goal_id), status="cancelled")
    return {"goal_id": goal_id, "status": "cancelled"}


@router.post("/goals/{goal_id}/resume")
async def goals_resume(goal_id: str, project: str = Query("general")) -> dict:
    if STATE.memory is None:
        raise HTTPException(status_code=503, detail="Not ready")
    await STATE.memory.update_goal_status(goal_id=UUID(goal_id), status="active")
    await STATE.memory.enqueue_goal_task(goal_id=UUID(goal_id), project_name=project, tool_name="__decide__", args={})
    return {"goal_id": goal_id, "status": "active"}
