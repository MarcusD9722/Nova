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


def _goal_uuid(goal_id: str) -> UUID:
    """The path parameter as a UUID, or an honest 422.

    `UUID(goal_id)` sat unguarded in the cancel and resume routes, so any id
    that was not a UUID — a stale link, a typo, anything at all — raised
    ValueError inside the handler and the client got `500 Internal Server
    Error`. Malformed input is the client's mistake to see, not a server fault
    to hide.
    """
    try:
        return UUID(str(goal_id))
    except (AttributeError, TypeError, ValueError):
        raise HTTPException(status_code=422, detail=f"Not a goal id: {goal_id}") from None


@router.get("/goals/{goal_id}/tasks")
async def goals_tasks(goal_id: str, limit: int = Query(50)) -> dict:
    if STATE.memory is None:
        raise HTTPException(status_code=503, detail="Not ready")
    gid = _goal_uuid(goal_id)
    return {"goal_id": goal_id, "tasks": await STATE.memory.list_goal_tasks(goal_id=str(gid), limit=int(limit))}


@router.get("/goals/{goal_id}/progress")
async def goals_progress(goal_id: str, limit: int = Query(50)) -> dict:
    """What happened on this goal, in order.

    `AgentSupervisor` records progress from seven places -- retries, errors,
    blocked work, a tool completing, and the note that a completion arrived
    after its run had ended -- and until now nothing could read any of it back.
    The only reader that existed, `fetch_unacked_progress`, acknowledges what it
    returns, so polling it would consume the history it was showing.
    """
    if STATE.memory is None:
        raise HTTPException(status_code=503, detail="Not ready")
    gid = _goal_uuid(goal_id)
    return {"goal_id": goal_id,
            "events": await STATE.memory.list_progress_events(
                goal_id=str(gid), limit=int(limit))}


@router.post("/goals/{goal_id}/cancel")
async def goals_cancel(goal_id: str) -> dict:
    """Cancel a goal and stop the work it had queued.

    The transition belongs to the store, not to this route: cancelling used to
    set `goals.status` here and leave every queued task runnable, so the
    supervisor kept executing a goal the user had cancelled. Route-level
    check-then-act cannot fix that — a claimer racing between two statements
    would still win — so `cancel_goal` owns existence, the status change and
    the queued rows together, and the claim itself refuses a non-active goal.
    """
    if STATE.memory is None:
        raise HTTPException(status_code=503, detail="Not ready")
    # A goal that does not exist was answered `{"status": "cancelled"}` — a
    # report of work that never happened, which is the one thing Nova's status
    # replies must never be.
    out = await STATE.memory.cancel_goal(goal_id=_goal_uuid(goal_id))
    if out is None:
        raise HTTPException(status_code=404, detail=f"No such goal: {goal_id}")
    # `already_running` is reported, not hidden: a tool call already in flight
    # cannot be un-executed, and saying otherwise would be the same dishonesty
    # this route was fixed for.
    return {"goal_id": goal_id, "status": "cancelled",
            "project": out["project_name"],
            "cancelled_tasks": out["cancelled_tasks"],
            "already_running": out["already_running"]}


@router.post("/goals/{goal_id}/resume")
async def goals_resume(goal_id: str, project: str | None = Query(None)) -> dict:
    """Resume a goal — idempotently, and in the goal's OWN project.

    `project` used to default to "general" and was used to enqueue the
    continuation, so a goal created under "alpha" resumed into "general"
    merely because the caller omitted the parameter. The stored goal row is
    authoritative; the parameter now only gets to DISAGREE, and a disagreement
    is an error rather than a silent change of ownership. Changing a goal's
    project, if ever wanted, is a different operation with its own semantics.
    """
    if STATE.memory is None:
        raise HTTPException(status_code=503, detail="Not ready")
    gid = _goal_uuid(goal_id)
    goal = await STATE.memory.get_goal(goal_id=gid)
    if goal is None:
        raise HTTPException(status_code=404, detail=f"No such goal: {goal_id}")
    if project is not None and project != goal["project_name"]:
        raise HTTPException(
            status_code=409,
            detail=(f"Goal {goal_id} belongs to project "
                    f"'{goal['project_name']}', not '{project}'. Resuming does "
                    f"not move a goal between projects."))
    # Worse than a false "active": resuming an unknown goal ENQUEUED a
    # `__decide__` task against it, so the supervisor picked up work for a goal
    # that does not exist. And an unconditional enqueue meant three resumes
    # produced three continuations. Both are the store's job now.
    out = await STATE.memory.resume_goal(goal_id=gid)
    if out is None:
        raise HTTPException(status_code=404, detail=f"No such goal: {goal_id}")
    return {"goal_id": goal_id, "status": "active",
            "project": out["project_name"],
            "continuation_enqueued": out["continuation_enqueued"],
            "existing_continuation": out["existing_continuation"]}
