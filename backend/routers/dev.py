from __future__ import annotations

"""Developer-mode endpoints (guarded self-inspection; see core/dev_mode.py).

Moved verbatim from backend/app.py in Phase 0.6 — behavior unchanged.
"""

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from backend.state import STATE

if TYPE_CHECKING:
    from core.dev_mode import DevMode

router = APIRouter()


# ── Continuous codebase understanding (Phase 7 / #10 + #18) ──────────────────
# These reuse the runtime-registered code.* tools (which own the project→root
# resolver + index cache), so the REST surface and the agent tools stay in sync.

async def _code_tool(name: str, project: str) -> dict:
    if STATE.runtime is None:
        raise HTTPException(status_code=503, detail="Not ready")
    from core.tool_router import ToolCall

    res = await STATE.runtime.router.execute(ToolCall(name=name, args={"project": project}), timeout_s=45.0, retries=0)
    return res.result if (res.ok and isinstance(res.result, dict)) else {"ok": False, "error": "execution_failed"}


@router.get("/code/index")
async def code_index(project: str = Query("")) -> dict:
    """Structural understanding + health for a project ('' or 'self' = Nova)."""
    return await _code_tool("code.index", project)


@router.get("/code/health")
async def code_health(project: str = Query("")) -> dict:
    """Health score + ranked technical-debt report."""
    return await _code_tool("code.health", project)


@router.get("/code/security")
async def code_security(project: str = Query("")) -> dict:
    """Defensive security scan (risky-pattern heuristics for human review)."""
    return await _code_tool("code.security", project)


# ── Permission broker (Phase 8): audit trail + confirm/deny pending actions ──

class PermissionResolveRequest(BaseModel):
    request_id: str
    approved: bool


@router.get("/permissions/audit")
async def permissions_audit(limit: int = Query(100)) -> dict:
    """The append-only audit trail of every permission request and outcome."""
    if STATE.runtime is None:
        raise HTTPException(status_code=503, detail="Not ready")
    b = STATE.runtime.permission_broker
    return {"mode": b.mode, "audit": b.audit_log(limit=int(limit)), "pending": b.pending()}


#: What to tell someone whose click arrived too late, per ending. Only endings
#: a person can actually meet a stale button on are named; anything else falls
#: back to the general sentence.
_RESOLVE_NOTES = {
    "interrupted_by_restart":
        "Nova restarted before that was answered, so the request is gone — "
        "nothing was executed. Ask again if you still want it.",
    "timeout":
        "That request timed out waiting for an answer — nothing was executed.",
    "approved": "You already approved that one; it has already run.",
    "rejected": "You already declined that one — nothing was executed.",
    "cancelled":
        "That request was withdrawn before you answered — nothing was executed.",
    "abandoned":
        "Whatever was waiting on that request stopped waiting — nothing was executed.",
}


@router.post("/permissions/resolve")
async def permissions_resolve(req: PermissionResolveRequest) -> dict:
    """Approve or deny a pending action that needed Marcus's confirmation."""
    if STATE.runtime is None:
        raise HTTPException(status_code=503, detail="Not ready")
    broker = STATE.runtime.permission_broker
    ok = broker.resolve(req.request_id, bool(req.approved))
    # `approved` echoes the CLICK; `applied` says whether it did anything. The old
    # shape returned approved=true for a request that had already timed out, so
    # the UI could report a deletion that never ran.
    out = {"resolved": ok, "applied": ok, "request_id": req.request_id,
           "approved": bool(req.approved)}
    if not ok:
        # WHICH kind of "no longer waiting". The broker knows — including,
        # since it reads its own durable trail at startup, that Nova restarted
        # while the request was still open. Listing every possibility to a
        # person who is owed one specific answer is a way of not answering.
        settled = broker.settled_as(req.request_id)
        out["settled_as"] = settled
        out["note"] = _RESOLVE_NOTES.get(settled) or (
            "That request was no longer waiting (it timed out, was "
            "withdrawn, or was already answered) — nothing was executed.")
    return out


class DevInspectRequest(BaseModel):
    path: str
    project: str = ""  # registered external project name; "" = Nova's own code


class DevProposeRequest(BaseModel):
    path: str
    new_content: str
    reason: str = ""
    project: str = ""  # registered external project name; "" = Nova's own code


class DevApplyRequest(BaseModel):
    proposal_id: str
    confirm: bool = False


class DevRollbackRequest(BaseModel):
    proposal_id: str


def _dev_mode() -> "DevMode":
    from core.dev_mode import DevMode, dev_mode_enabled

    cfg = STATE.config
    if cfg is None:
        raise HTTPException(status_code=503, detail="Not ready")
    if not dev_mode_enabled():
        raise HTTPException(status_code=403, detail="Developer mode is disabled (set NOVA_DEV_MODE=1 in .env).")
    # Prefer the shared instance the tool router built, so proposals Nova files
    # via self.propose_change and proposals managed through these endpoints are
    # the same store. Fall back to a lazily-created instance if unavailable.
    shared = getattr(getattr(STATE.runtime, "router", None), "dev_mode", None) if STATE.runtime is not None else None
    if shared is not None:
        STATE.dev_mode = shared
        return shared
    if getattr(STATE, "dev_mode", None) is None:
        STATE.dev_mode = DevMode(repo_root=cfg.repo_root, projects_dir=cfg.projects_dir)
    return STATE.dev_mode


@router.get("/dev/status")
async def dev_status() -> dict:
    from core.dev_mode import dev_mode_enabled

    enabled = dev_mode_enabled()
    pending = 0
    if enabled and getattr(STATE, "dev_mode", None) is not None:
        pending = sum(1 for p in STATE.dev_mode.list_proposals() if p.get("status") == "pending")
    return {"enabled": enabled, "pending_proposals": pending}


@router.post("/dev/inspect")
async def dev_inspect(req: DevInspectRequest) -> dict:
    from core.dev_mode import DevModeError

    dev = _dev_mode()
    try:
        return dev.read_file(req.path, project=req.project)
    except DevModeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/dev/propose")
async def dev_propose(req: DevProposeRequest) -> dict:
    from core.dev_mode import DevModeError

    dev = _dev_mode()
    try:
        proposal = dev.propose_change(req.path, req.new_content, reason=req.reason, project=req.project)
        return {"proposal_id": proposal.id, "path": proposal.path, "diff": proposal.diff, "status": proposal.status}
    except DevModeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/dev/proposals")
async def dev_proposals() -> dict:
    from core.dev_mode import DevModeError

    dev = _dev_mode()
    try:
        return {"proposals": dev.list_proposals()}
    except DevModeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/dev/proposals/{proposal_id}")
async def dev_proposal_detail(proposal_id: str) -> dict:
    """Full detail (old_content + new_content) for the diff viewer — the list
    endpoint stays lightweight."""
    from core.dev_mode import DevModeError

    dev = _dev_mode()
    try:
        return dev.get_proposal(proposal_id)
    except DevModeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/dev/projects")
async def dev_projects() -> dict:
    """Registered external project roots (guarded editing beyond Nova's repo)."""
    from core.dev_mode import DevModeError

    dev = _dev_mode()
    try:
        return {"projects": dev.list_external_roots()}
    except DevModeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/dev/apply")
async def dev_apply(req: DevApplyRequest) -> dict:
    from core.dev_mode import DevModeError

    dev = _dev_mode()
    try:
        return dev.apply_proposal(req.proposal_id, confirm=req.confirm)
    except DevModeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/dev/reject")
async def dev_reject(req: DevRollbackRequest) -> dict:
    from core.dev_mode import DevModeError

    dev = _dev_mode()
    try:
        dev.reject_proposal(req.proposal_id)
        return {"proposal_id": req.proposal_id, "status": "rejected"}
    except DevModeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/dev/rollback")
async def dev_rollback(req: DevRollbackRequest) -> dict:
    from core.dev_mode import DevModeError

    dev = _dev_mode()
    try:
        return dev.rollback_proposal(req.proposal_id)
    except DevModeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/dev/backups")
async def dev_backups() -> dict:
    from core.dev_mode import DevModeError

    dev = _dev_mode()
    try:
        return {"backups": dev.list_backups()}
    except DevModeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
