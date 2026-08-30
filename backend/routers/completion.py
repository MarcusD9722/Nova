"""Completion state, the acceptance contract, and answering Nova's questions.

Everything here reads from `CompletionService`, which derives the state from
acceptance criteria and evidence. Nothing here can assign a state, and there is
deliberately no endpoint that could.

THE TRUST BOUNDARY, STATED PRECISELY

`channel` is NOT taken from the request body. The route knows how the request
arrived and says so itself, so a caller cannot claim to be the UI by typing
"ui" into some JSON. That closes the half of the boundary the server can close.

`actor` IS taken from the caller, and this deployment has no authentication to
check it against. So the record says who the caller CLAIMED to be, and the
channel says how they reached Nova. Neither is proof that a physical human was
present, and nothing here should be read as claiming otherwise.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from backend.state import STATE

router = APIRouter()


def _service():
    if STATE.runtime is None:
        raise HTTPException(status_code=503, detail="Not ready")
    return STATE.runtime.completion


def _channel_for(request: Request) -> str:
    """How this request actually arrived, decided HERE.

    A client-supplied channel is a claim; this is an observation. The UI is
    served from the same origin and identifies itself with a header the browser
    sets, so a same-origin XHR is `ui` and anything else is `api`.
    """
    origin = (request.headers.get("origin") or "").strip()
    referer = (request.headers.get("referer") or "").strip()
    fetch_site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if fetch_site == "same-origin" or origin or referer:
        return "ui"
    return "api"


@router.get("/completion/{slug}")
async def completion_state(slug: str) -> dict:
    """The authoritative completion state. Derived on every call."""
    svc = _service()
    verdict = await svc.evaluate(slug=slug)
    return {
        "project": slug,
        "state": verdict.state,
        "revision": verdict.revision,
        "contract": verdict.seal_mode,
        "reasons": list(verdict.reasons),
        "outstanding": [s.criterion.text for s in verdict.outstanding],
        "failing": [s.criterion.text for s in verdict.failing],
        "criteria": [
            {"criterion_id": s.criterion.criterion_id,
             "text": s.criterion.text,
             "origin_quote": s.criterion.origin_quote,
             "required": s.criterion.required,
             "verify_kind": s.criterion.verify_kind,
             "verdict": s.verdict,
             "note": s.stale_reason}
            for s in verdict.criteria
        ],
        "legacy_status": verdict.legacy_status,
        "legacy_note": verdict.legacy_note,
    }


@router.get("/completion/{slug}/contract")
async def completion_contract(slug: str) -> dict:
    """The acceptance contract, for a person to read and agree with.

    This exists because of a boundary nothing here can police: a criterion can
    quote the request exactly and still mean less than it. Showing the list is
    what makes that a person's decision rather than a silent assumption.
    """
    summary = await _service().contract_summary(slug=slug)
    if not summary:
        raise HTTPException(status_code=404,
                            detail=f"no requirement recorded for {slug!r}")
    return summary


@router.post("/completion/{slug}/contract/confirm")
async def ask_contract_confirmation(slug: str) -> dict:
    """Open a question asking whether the contract is the right list."""
    try:
        decision_id = await _service().ask_contract_confirmation(slug=slug)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"decision_id": decision_id, "project": slug}


class ResolveRequest(BaseModel):
    accepted: bool
    actor: str
    # No `channel` field, on purpose. The route decides that.


@router.get("/completion/{slug}/decisions")
async def open_decisions(slug: str) -> dict:
    """Questions Nova is waiting on for this project."""
    svc = _service()
    rows = await svc._memory.list_human_decisions(project_name=slug,
                                                  open_only=True)
    return {"project": slug, "pending": rows}


@router.post("/completion/decisions/{decision_id}/resolve")
async def resolve_decision(decision_id: str, req: ResolveRequest,
                           request: Request) -> dict:
    """Answer one of Nova's questions — a criterion or a whole contract.

    Refusals are specific. "Already answered", "no such decision" and "that is
    a contract question, not a criterion one" are different mistakes, and
    collapsing them into one generic 400 would hide which happened from the
    person who needs to know.
    """
    svc = _service()
    channel = _channel_for(request)
    actor = (req.actor or "").strip()
    if not actor:
        raise HTTPException(status_code=400,
                            detail="a decision must name who made it")

    pending = await svc._memory.get_human_decision(decision_id=decision_id)
    if pending is None:
        raise HTTPException(status_code=404,
                            detail=f"no decision {decision_id!r} was ever asked")

    from core.completion_service import CONTRACT_TARGET
    try:
        if pending["criterion_id"] == CONTRACT_TARGET:
            revision = await svc.resolve_contract_confirmation(
                decision_id=decision_id, accepted=req.accepted, actor=actor,
                channel=channel)
            return {"resolved": True, "kind": "contract",
                    "project": pending["project_name"], "revision": revision,
                    "accepted": req.accepted, "actor": actor,
                    "channel": channel}
        await svc.resolve_human_decision(
            decision_id=decision_id, accepted=req.accepted, actor=actor,
            channel=channel)
    except ValueError as e:
        # Already-answered is a conflict, not a bad request: the caller did
        # nothing wrong, the question simply is not open any more.
        code = 409 if "already answered" in str(e) else 400
        raise HTTPException(status_code=code, detail=str(e))
    return {"resolved": True, "kind": "criterion",
            "project": pending["project_name"],
            "criterion_id": pending["criterion_id"],
            "accepted": req.accepted, "actor": actor, "channel": channel}
