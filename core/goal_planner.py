from __future__ import annotations

"""Long-term goal planning (Goal #3, Phase 5).

Turns a goal (a vision) into a structured plan: milestones with target dates and
risk, plus dated action items at a cadence (once / daily / weekly / monthly). The
signature behaviour is **adaptive roll-forward** — a missed recurring item's due
date advances to its next occurrence instead of silently becoming "overdue", and
a milestone whose target date has passed while still open is flagged `at_risk`.

The chat model composes the plan (vision → milestones → items) and persists it via
the plan.* tools; this module is the deterministic engine — building, rolling
forward, and scoring progress — so all of it is testable without a model.
(Named goal_planner to avoid the existing intent-router core/planner.py.)
"""

from datetime import date, datetime, timedelta
from typing import Any
from uuid import uuid4

CADENCES = ("once", "daily", "weekly", "monthly")


def _today() -> date:
    return datetime.now().astimezone().date()


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _parse_date(s: Any) -> date | None:
    if isinstance(s, date):
        return s
    try:
        return date.fromisoformat(str(s)[:10])
    except Exception:
        return None


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    for day in (d.day, 31, 30, 29, 28):
        try:
            return date(y, m, min(day, d.day))
        except ValueError:
            continue
    return date(y, m, 28)


def _next_occurrence(due: date, cadence: str, today: date) -> date:
    """Advance a past due date to its next on-or-after-today occurrence."""
    if cadence == "daily":
        step = lambda x: x + timedelta(days=1)  # noqa: E731
    elif cadence == "weekly":
        step = lambda x: x + timedelta(days=7)  # noqa: E731
    elif cadence == "monthly":
        step = lambda x: _add_months(x, 1)  # noqa: E731
    else:
        return due  # 'once' never rolls forward
    guard = 0
    while due < today and guard < 1000:
        due = step(due)
        guard += 1
    return due


def build_plan(vision: str, *, horizon_days: int = 90, milestones: list[dict] | None = None,
               items: list[dict] | None = None) -> dict:
    """Assemble a plan from the pieces the model supplies. Missing/invalid bits
    are dropped rather than guessed."""
    now = _now_iso()
    ms_out: list[dict] = []
    for m in (milestones or []):
        title = str(m.get("title") or "").strip()
        if not title:
            continue
        risk = str(m.get("risk", "")).lower()
        ms_out.append({
            "id": uuid4().hex[:8],
            "title": title[:200],
            "target_date": (str(m.get("target_date"))[:10] if _parse_date(m.get("target_date")) else None),
            "risk": risk if risk in ("low", "medium", "high") else "medium",
            "status": "open",
        })
    it_out: list[dict] = []
    for it in (items or []):
        title = str(it.get("title") or "").strip()
        if not title:
            continue
        cadence = str(it.get("cadence", "once")).lower()
        cadence = cadence if cadence in CADENCES else "once"
        it_out.append({
            "id": uuid4().hex[:8],
            "title": title[:200],
            "cadence": cadence,
            "due": (str(it.get("due"))[:10] if _parse_date(it.get("due")) else None),
            "status": "pending",
            "milestone_id": it.get("milestone_id"),
        })
    return {
        "vision": (vision or "").strip()[:500],
        "horizon_days": int(horizon_days),
        "created_at": now,
        "updated_at": now,
        "milestones": ms_out,
        "items": it_out,
    }


def roll_forward(plan: dict, *, today: date | None = None) -> dict:
    """Adaptive roll-forward. Returns {plan, rolled, at_risk, overdue}.

    - A pending recurring item whose due date is in the past advances to its next
      occurrence (so it never lingers as "overdue").
    - A pending one-off item in the past is flagged overdue (kept, not moved).
    - An open milestone past its target date is marked at_risk.
    """
    today = today or _today()
    rolled: list[dict] = []
    at_risk: list[dict] = []
    overdue: list[dict] = []

    for it in plan.get("items", []):
        if it.get("status") != "pending":
            continue
        due = _parse_date(it.get("due"))
        if due is None or due >= today:
            continue
        if it.get("cadence", "once") == "once":
            it["overdue"] = True
            overdue.append({"title": it["title"], "due": it["due"]})
        else:
            new_due = _next_occurrence(due, it["cadence"], today)
            if new_due != due:
                rolled.append({"title": it["title"], "from": it["due"], "to": new_due.isoformat()})
                it["due"] = new_due.isoformat()
                it.pop("overdue", None)

    for m in plan.get("milestones", []):
        if m.get("status") != "open":
            continue
        target = _parse_date(m.get("target_date"))
        if target is not None and target < today:
            m["status"] = "at_risk"
            at_risk.append({"title": m["title"], "target_date": m["target_date"]})

    plan["updated_at"] = _now_iso()
    return {"plan": plan, "rolled": rolled, "at_risk": at_risk, "overdue": overdue}


def progress(plan: dict) -> dict:
    ms = plan.get("milestones", [])
    items = plan.get("items", [])
    ms_done = sum(1 for m in ms if m.get("status") == "done")
    it_done = sum(1 for it in items if it.get("status") == "done")
    total = len(ms) + len(items)
    done = ms_done + it_done
    return {
        "milestones_total": len(ms),
        "milestones_done": ms_done,
        "milestones_at_risk": sum(1 for m in ms if m.get("status") == "at_risk"),
        "items_total": len(items),
        "items_done": it_done,
        "items_overdue": sum(1 for it in items if it.get("overdue")),
        "percent_complete": round(100.0 * done / total, 1) if total else 0.0,
    }
