"""Phase 5 / #1: executive intelligence.

Confidence-gated, ranked, deduped proactive recommendations. Pure engine, tested
directly. Verifies it stays quiet on weak signals and never floods.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.executive import ExecutiveContext, day_part, recommend

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


def kinds(recs):
    return [r["kind"] for r in recs]


def main():
    check(day_part(2) == "late night" and day_part(9) == "morning" and day_part(20) == "evening", "day_part buckets hours")

    # ── Nothing pressing -> silence (never invents a nudge) ──
    quiet = recommend(ExecutiveContext(now_hour=14, active_goals=["Nova"]))
    check(quiet == [], "no pressing signals -> no recommendations")

    # ── Overdue item is top priority, high confidence ──
    od = recommend(ExecutiveContext(now_hour=10, overdue=["File taxes"]))
    check(od and od[0]["kind"] == "deadline" and od[0]["confidence"] >= 0.9, "overdue item surfaces as a high-confidence deadline")

    # ── Upcoming deadline + procrastination -> firmer nudge ──
    up = recommend(ExecutiveContext(now_hour=10, upcoming=[{"label": "Report", "hours_until": 6}], procrastination=0.7))
    check(up and up[0]["kind"] == "deadline", "upcoming deadline surfaces")
    check(up[0]["confidence"] > 0.75, "procrastination raises the deadline nudge confidence")
    check("start it now" in up[0]["message"].lower(), "procrastination changes the phrasing")

    # ── A deadline >24h away is NOT surfaced (too far to nag) ──
    far = recommend(ExecutiveContext(now_hour=10, upcoming=[{"label": "Trip", "hours_until": 100}]))
    check(far == [], "a deadline far out is not surfaced")

    # ── Focus window only fires in the peak period with active work ──
    focus_now = recommend(ExecutiveContext(now_hour=20, peak_period="evening", active_goals=["Nova"]))
    check(any(k == "focus" for k in kinds(focus_now)), "focus recommendation fires in the peak period")
    focus_off = recommend(ExecutiveContext(now_hour=9, peak_period="evening", active_goals=["Nova"]))
    check("focus" not in kinds(focus_off), "no focus recommendation outside the peak period")

    # ── Weather only when a weather signal is actually present ──
    wet = recommend(ExecutiveContext(now_hour=8, weather={"condition": "light rain"}))
    check(any(k == "weather" for k in kinds(wet)), "weather nudge fires on precipitation")
    dry = recommend(ExecutiveContext(now_hour=8, weather={"condition": "clear"}))
    check("weather" not in kinds(dry), "no weather nudge in clear conditions")

    # ── Break suggestion on late-night activity ──
    late = recommend(ExecutiveContext(now_hour=2, active_goals=["Nova"]))
    check(any(k == "break" for k in kinds(late)), "late-night activity suggests a break")

    # ── Ranking + cap: many signals collapse to the top few by priority×confidence ──
    flood = recommend(ExecutiveContext(
        now_hour=2, overdue=["A", "B"], upcoming=[{"label": "C", "hours_until": 3}],
        upcoming_dates=[{"name": "Sam", "label": "birthday", "days_until": 1}],
        peak_period="late night", active_goals=["Nova"], stalled_goals=["Old"], stress_level="elevated",
        max_recommendations=3,
    ))
    check(len(flood) == 3, f"output capped at max_recommendations (got {len(flood)})")
    check(flood[0]["kind"] == "deadline", "highest priority (overdue) ranks first")
    check(len({r["key"] for r in flood}) == len(flood), "no duplicate recommendation keys")

    # ── Confidence gate: raising the threshold silences weaker items ──
    gated = recommend(ExecutiveContext(now_hour=20, peak_period="evening", active_goals=["Nova"], min_confidence=0.8))
    check(gated == [], "a high confidence gate suppresses the 0.6 focus nudge")

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


main()
