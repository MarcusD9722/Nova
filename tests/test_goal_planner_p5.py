"""Phase 5 / #3: long-term goal planning — build, adaptive roll-forward, progress."""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.goal_planner import build_plan, progress, roll_forward

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


def main():
    today = date(2026, 7, 20)
    yesterday = (today - timedelta(days=1)).isoformat()
    last_week = (today - timedelta(days=10)).isoformat()
    past_target = (today - timedelta(days=5)).isoformat()
    future = (today + timedelta(days=5)).isoformat()

    plan = build_plan(
        "Ship Nova v1",
        horizon_days=90,
        milestones=[
            {"title": "Beta release", "target_date": past_target, "risk": "high"},
            {"title": "Launch", "target_date": future, "risk": "low"},
            {"title": "no date milestone"},
        ],
        items=[
            {"title": "Daily standup note", "cadence": "daily", "due": yesterday},
            {"title": "Weekly review", "cadence": "weekly", "due": last_week},
            {"title": "One-off: file LLC", "cadence": "once", "due": yesterday},
            {"title": "Future task", "cadence": "daily", "due": future},
            {"title": "junk"},  # no cadence -> 'once', no due
        ],
    )

    # ── build_plan sanitizes ──
    check(len(plan["milestones"]) == 3, "milestones built (blank titles dropped)")
    check(plan["milestones"][2]["risk"] == "medium", "missing risk defaults to medium")
    check(plan["milestones"][0]["status"] == "open", "milestones start open")
    check(len(plan["items"]) == 5 and plan["items"][4]["cadence"] == "once", "invalid cadence falls back to once")

    # ── roll_forward: recurring items advance, one-off flagged, milestone at-risk ──
    result = roll_forward(plan, today=today)
    p = result["plan"]
    items = {it["title"]: it for it in p["items"]}

    check(date.fromisoformat(items["Daily standup note"]["due"]) >= today, "overdue daily item rolled to >= today")
    check(items["Daily standup note"]["due"] == today.isoformat(), "daily rolled to exactly today (next occurrence)")
    check(date.fromisoformat(items["Weekly review"]["due"]) >= today, "overdue weekly item rolled forward")
    check(items["One-off: file LLC"].get("overdue") is True, "one-off past item flagged overdue, not moved")
    check(items["One-off: file LLC"]["due"] == yesterday, "one-off due date unchanged")
    check(items["Future task"]["due"] == future, "future item untouched")
    check(len(result["rolled"]) == 2, f"exactly the two recurring items rolled (got {len(result['rolled'])})")

    ms = {m["title"]: m for m in p["milestones"]}
    check(ms["Beta release"]["status"] == "at_risk", "open milestone past target -> at_risk")
    check(ms["Launch"]["status"] == "open", "future milestone stays open")
    check(any(a["title"] == "Beta release" for a in result["at_risk"]), "at_risk milestone reported")

    # ── progress ──
    p["items"][0]["status"] = "done"
    p["milestones"][1]["status"] = "done"
    prog = progress(p)
    check(prog["items_total"] == 5 and prog["milestones_total"] == 3, "progress counts totals")
    check(prog["items_done"] == 1 and prog["milestones_done"] == 1, "progress counts done")
    check(prog["milestones_at_risk"] == 1, "progress counts at-risk")
    check(prog["items_overdue"] == 1, "progress counts overdue one-offs")
    check(prog["percent_complete"] == round(200.0 / 8, 1), f"percent complete computed (got {prog['percent_complete']})")

    # ── idempotence: rolling an already-current plan changes nothing ──
    again = roll_forward(p, today=today)
    check(again["rolled"] == [], "re-rolling a current plan rolls nothing")

    # ── empty plan is honest ──
    check(progress(build_plan("x"))["percent_complete"] == 0.0, "empty plan is 0% (no div-by-zero)")

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


main()
