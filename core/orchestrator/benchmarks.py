from __future__ import annotations

"""Self-benchmarking (Goal #14, Phase 3.5).

The self-improve worker already writes one `self_eval` fact per UTC day holding a
full metrics snapshot (reply latency, tool success, empty replies, vision errors,
and — since #12 — the derived internal state). This module turns that history
into trends and regression flags over time, with zero new instrumentation.

Honesty note: it only trends dimensions Nova actually measures. Reasoning-,
coding-, and planning-QUALITY scoring would require an active benchmark harness
(known tasks with graded outputs) that does not exist yet — those are reported
explicitly under `not_measured` rather than faked with a made-up number. That
harness is Phase 7 (Autonomous Experimentation) work.
"""

from datetime import datetime, timezone

# Each measured metric: (key, label, direction, extractor).
# direction: "lower_better" or "higher_better".
_LOWER, _HIGHER = "lower_better", "higher_better"


def _lat(snap, field):
    lat = snap.get("reply_latency_s") or {}
    v = lat.get(field)
    return float(v) if isinstance(v, (int, float)) else None


def _rate(num, denom):
    n = float(num or 0)
    d = float(denom or 0)
    return (n / d) if d > 0 else None


def _istate(snap, key):
    st = snap.get("internal_state") or {}
    node = st.get(key) or {}
    v = node.get("value")
    return float(v) if isinstance(v, (int, float)) else None


_METRICS: list[tuple[str, str, str, object]] = [
    ("reply_latency_avg", "avg reply latency (s)", _LOWER, lambda s: _lat(s, "avg")),
    ("reply_latency_p95", "p95 reply latency (s)", _LOWER, lambda s: _lat(s, "p95")),
    ("tool_failure_rate", "tool failure rate", _LOWER, lambda s: float(s["tool_failure_rate"]) if s.get("tool_failure_rate") is not None else None),
    ("empty_reply_rate", "empty reply rate", _LOWER, lambda s: _rate(s.get("empty_replies"), s.get("turns"))),
    ("vision_error_rate", "vision error rate", _LOWER, lambda s: _rate(s.get("vision_errors"), s.get("turns"))),
    ("confidence", "internal confidence", _HIGHER, lambda s: _istate(s, "confidence")),
    ("uncertainty", "internal uncertainty", _LOWER, lambda s: _istate(s, "uncertainty")),
    ("learning_rate", "learning rate", _HIGHER, lambda s: _istate(s, "learning_rate")),
]

# Quality dimensions the roadmap names that are NOT yet measurable without an
# active graded-task harness. Reported honestly rather than invented.
_NOT_MEASURED = [
    {"metric": "reasoning_quality", "reason": "needs an active benchmark harness (graded tasks) — Phase 7"},
    {"metric": "coding_quality", "reason": "needs build/test pass-rate over graded coding tasks — Phase 7"},
    {"metric": "planning_quality", "reason": "needs graded planning tasks with known-good plans — Phase 7"},
]


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def compute_benchmark_report(
    snapshots: list[dict],
    *,
    recent_days: int = 7,
    baseline_days: int = 7,
    min_days: int = 4,
    regression_pct: float = 0.15,
    now_iso: str | None = None,
) -> dict:
    """Trend + regression report from a list of daily metrics snapshots (each a
    MetricsCollector.snapshot() dict, ideally carrying `internal_state`). Order
    doesn't matter — snapshots are sorted by their `day` field."""
    snaps = sorted([s for s in (snapshots or []) if isinstance(s, dict)], key=lambda s: str(s.get("day") or ""))
    now = now_iso or datetime.now(timezone.utc).isoformat()

    metrics_out: dict[str, dict] = {}
    regressions: list[dict] = []
    improvements: list[dict] = []

    for key, label, direction, extract in _METRICS:
        series = [(str(s.get("day") or ""), extract(s)) for s in snaps]
        series = [(d, v) for d, v in series if v is not None]
        entry: dict = {"label": label, "direction": direction, "samples": len(series)}

        if len(series) < min_days:
            entry["trend"] = "insufficient_data"
            metrics_out[key] = entry
            continue

        values = [v for _, v in series]
        recent = values[-recent_days:]
        baseline = values[-(recent_days + baseline_days):-recent_days] if len(values) > recent_days else []
        recent_avg = round(_mean(recent), 3)
        entry["recent_avg"] = recent_avg

        if not baseline:
            entry["trend"] = "baseline_forming"
            metrics_out[key] = entry
            continue

        baseline_avg = round(_mean(baseline), 3)
        entry["baseline_avg"] = baseline_avg
        # delta_pct: signed change relative to baseline magnitude.
        entry["delta_pct"] = round(((recent_avg - baseline_avg) / baseline_avg) * 100, 1) if baseline_avg != 0 else None

        if direction == _LOWER:
            worse = baseline_avg > 0 and recent_avg > baseline_avg * (1 + regression_pct)
            better = baseline_avg > 0 and recent_avg < baseline_avg * (1 - regression_pct)
        else:
            worse = recent_avg < baseline_avg * (1 - regression_pct)
            better = recent_avg > baseline_avg * (1 + regression_pct)

        entry["trend"] = "regressing" if worse else "improving" if better else "stable"
        metrics_out[key] = entry
        if worse:
            regressions.append({"metric": key, "label": label, "recent": recent_avg, "baseline": baseline_avg})
        elif better:
            improvements.append({"metric": key, "label": label, "recent": recent_avg, "baseline": baseline_avg})

    days = [str(s.get("day") or "") for s in snaps if s.get("day")]
    summary = _summarize(len(days), regressions, improvements)

    return {
        "generated_at": now,
        "days_analyzed": len(days),
        "day_range": {"from": days[0], "to": days[-1]} if days else {},
        "window": {"recent_days": recent_days, "baseline_days": baseline_days, "regression_pct": regression_pct},
        "metrics": metrics_out,
        "regressions": regressions,
        "improvements": improvements,
        "not_measured": _NOT_MEASURED,
        "summary": summary,
    }


def _summarize(day_count: int, regressions: list[dict], improvements: list[dict]) -> str:
    if day_count == 0:
        return "No self-evaluation history yet — benchmarks populate once Nova has run for a few days."
    parts = [f"Analyzed {day_count} day(s) of self-eval history."]
    if regressions:
        parts.append("Regressions: " + ", ".join(r["label"] for r in regressions) + ".")
    if improvements:
        parts.append("Improvements: " + ", ".join(i["label"] for i in improvements) + ".")
    if not regressions and not improvements:
        parts.append("All measured metrics stable.")
    return " ".join(parts)
