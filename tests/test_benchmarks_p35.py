"""Phase 3.5 / #14: self-benchmarking.

Trends + regression detection over the daily self-eval history. Pure computation,
so tested directly with synthetic daily snapshots. Also asserts the honest
`not_measured` reporting for quality dimensions with no harness yet.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.orchestrator.benchmarks import compute_benchmark_report

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


def daysnap(day, lat_avg, tfr, conf=0.5, turns=10, empty=0):
    return {
        "day": day, "turns": turns, "empty_replies": empty,
        "reply_latency_s": {"avg": lat_avg, "p50": lat_avg, "p95": lat_avg * 2, "count": turns, "max": lat_avg * 2},
        "tool_calls": 10, "tool_failures": int(round(tfr * 10)), "tool_failure_rate": tfr,
        "avg_tools_per_turn": 1.0, "vision_errors": 0,
        "fact_writes": 2, "assumption_writes": 0, "lessons_learned": 0, "memory_searches": 3,
        "internal_state": {"confidence": {"value": conf}, "uncertainty": {"value": 0.2}, "learning_rate": {"value": 0.3}},
    }


def main():
    # ── 14 days: baseline week vs recent week, one metric worse + one better ──
    days = [f"2026-07-{d:02d}" for d in range(1, 15)]
    snaps = []
    for i, day in enumerate(days):
        if i < 7:  # baseline: fast replies, high tool failure
            snaps.append(daysnap(day, lat_avg=2.0, tfr=0.4, conf=0.5))
        else:      # recent: slow replies (regression), low tool failure (improvement)
            snaps.append(daysnap(day, lat_avg=5.0, tfr=0.1, conf=0.5))

    # Feed unsorted to prove the report sorts by day.
    import random
    shuffled = snaps[:]
    random.shuffle(shuffled)
    rep = compute_benchmark_report(shuffled)

    check(rep["days_analyzed"] == 14, f"counts all analyzed days (got {rep['days_analyzed']})")
    check(rep["day_range"] == {"from": "2026-07-01", "to": "2026-07-14"}, f"day range sorted correctly (got {rep['day_range']})")

    m = rep["metrics"]
    check(m["reply_latency_avg"]["trend"] == "regressing", f"slower replies flagged as regression (got {m['reply_latency_avg']['trend']})")
    check(m["tool_failure_rate"]["trend"] == "improving", f"lower tool failure flagged as improvement (got {m['tool_failure_rate']['trend']})")
    check(m["confidence"]["trend"] == "stable", f"flat confidence is stable (got {m['confidence']['trend']})")

    reg_metrics = {r["metric"] for r in rep["regressions"]}
    check("reply_latency_avg" in reg_metrics, "regression appears in the regressions list")
    imp_metrics = {i["metric"] for i in rep["improvements"]}
    check("tool_failure_rate" in imp_metrics, "improvement appears in the improvements list")
    check("Regressions" in rep["summary"], f"summary mentions regressions (got {rep['summary']!r})")

    # delta_pct is signed relative to baseline.
    check(m["reply_latency_avg"]["delta_pct"] == 150.0, f"latency delta_pct computed (got {m['reply_latency_avg']['delta_pct']})")

    # ── Honest not_measured reporting ──
    nm = {x["metric"] for x in rep["not_measured"]}
    check({"reasoning_quality", "coding_quality", "planning_quality"} <= nm, "quality dims reported as not_measured, not faked")

    # ── Insufficient data: fewer than min_days ──
    thin = compute_benchmark_report([daysnap("2026-07-01", 2.0, 0.2), daysnap("2026-07-02", 2.0, 0.2)])
    check(thin["metrics"]["reply_latency_avg"]["trend"] == "insufficient_data", "too few days -> insufficient_data")

    # ── Baseline forming: enough for a recent window but no baseline yet ──
    week = compute_benchmark_report([daysnap(f"2026-07-{d:02d}", 2.0, 0.2) for d in range(1, 8)])
    check(week["metrics"]["reply_latency_avg"]["trend"] == "baseline_forming", "one window only -> baseline_forming")

    # ── Empty history ──
    empty = compute_benchmark_report([])
    check(empty["days_analyzed"] == 0 and "No self-evaluation history" in empty["summary"], "empty history handled honestly")

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


main()
