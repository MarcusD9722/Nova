"""Phase 3.5 / #12: internal operational state.

Reasoning/operational metrics derived from real telemetry — not feelings. Covers
the pure derivation (idle, uncertainty, low-confidence, heavy-workload), clamping,
the advisory hints, and the MetricsCollector counters that feed it.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.orchestrator.internal_state import (
    InternalStateInputs,
    derive_internal_state,
    operating_hint_text,
)
from core.orchestrator.metrics import MetricsCollector

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


def snap(**over):
    """A MetricsCollector.snapshot()-shaped dict with overridable fields."""
    s = {
        "day": "2026-07-20", "turns": 0, "empty_replies": 0,
        "reply_latency_s": {"count": 0, "avg": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0},
        "tool_calls": 0, "tool_failures": 0, "tool_failure_rate": 0.0,
        "avg_tools_per_turn": 0.0, "vision_errors": 0,
        "fact_writes": 0, "assumption_writes": 0, "lessons_learned": 0, "memory_searches": 0,
    }
    lat = over.pop("reply_latency_s", None)
    s.update(over)
    if lat:
        s["reply_latency_s"].update(lat)
    return s


def all_values(state):
    keys = ("confidence", "uncertainty", "mental_workload", "focus", "energy", "curiosity", "learning_rate")
    return [state[k]["value"] for k in keys]


def main():
    # ── Idle / cold start: neutral, honest, no hints ──
    idle = derive_internal_state(InternalStateInputs(snapshot=snap()))
    check(all(v == 0.5 for v in all_values(idle)), "idle state is neutral 0.5 (no fabricated metrics)")
    check(idle["operating_hints"] == [], "idle state emits no operating hints")
    check("insufficient" in idle["confidence"]["basis"], "idle basis is honest about lack of telemetry")

    # ── Every value clamps to [0,1] across a stress input ──
    stressed = derive_internal_state(InternalStateInputs(
        snapshot=snap(turns=10, tool_calls=10, tool_failures=10, tool_failure_rate=1.0,
                      empty_replies=10, fact_writes=10, assumption_writes=10,
                      reply_latency_s={"p50": 50.0, "p95": 90.0}),
        queued_tasks=50, running_tasks=50, active_goals=50,
    ))
    check(all(0.0 <= v <= 1.0 for v in all_values(stressed)), "all metrics clamp to [0,1]")

    # ── High assumption ratio -> high uncertainty + hedge hint ──
    unc = derive_internal_state(InternalStateInputs(
        snapshot=snap(turns=10, fact_writes=10, assumption_writes=9),
    ))
    check(unc["uncertainty"]["value"] >= 0.5, f"assumption-heavy memory raises uncertainty (got {unc['uncertainty']['value']})")
    check(any("hedge" in h for h in unc["operating_hints"]), "high uncertainty emits a hedge hint")

    # ── Failing tools + slow, empty replies -> low confidence + low energy hints ──
    weak = derive_internal_state(InternalStateInputs(
        snapshot=snap(turns=10, tool_calls=10, tool_failures=8, tool_failure_rate=0.8,
                      empty_replies=8, fact_writes=10, assumption_writes=10,
                      reply_latency_s={"p50": 25.0, "p95": 40.0}),
    ))
    check(weak["confidence"]["value"] <= 0.4, f"failing tools + empties -> low confidence (got {weak['confidence']['value']})")
    check(weak["energy"]["value"] <= 0.35, f"slow + erroring -> low energy (got {weak['energy']['value']})")
    check(any("clarifying" in h for h in weak["operating_hints"]), "low confidence -> ask-a-question hint")
    check(any("headroom" in h for h in weak["operating_hints"]), "low energy -> reliable-actions hint")

    # ── Heavy background work in flight -> high workload + concise hint ──
    busy = derive_internal_state(InternalStateInputs(
        snapshot=snap(turns=5, tool_calls=1), queued_tasks=6, running_tasks=2, active_goals=4,
    ))
    check(busy["mental_workload"]["value"] >= 0.6, f"queued/running tasks raise workload (got {busy['mental_workload']['value']})")
    check(any("concise" in h for h in busy["operating_hints"]), "heavy workload -> concise hint")
    check(operating_hint_text(busy) != "", "operating_hint_text renders the advisory line")
    check(operating_hint_text(idle) == "", "operating_hint_text is empty when idle")

    # ── Each metric carries a basis naming its real signal ──
    check(all(busy[k]["basis"] for k in ("confidence", "uncertainty", "mental_workload", "energy")),
          "every metric carries a non-empty basis string")

    # ── MetricsCollector counters feed the derivation from real bus events ──
    mc = MetricsCollector()
    ts = "2026-07-20T10:00:00+00:00"
    mc.observe("memory.write", ts, {"kind": "fact", "verification": "inferred", "entity": "mood"})
    mc.observe("memory.write", ts, {"kind": "fact", "verification": "stated", "entity": "note"})
    mc.observe("memory.write", ts, {"kind": "fact", "verification": "inferred", "entity": "lesson"})
    mc.observe("memory.write", ts, {"kind": "person"})  # non-fact write: ignored by fact counters
    mc.observe("memory.search", ts, {"query": "x"})
    s = mc.snapshot()
    check(s["fact_writes"] == 3, f"fact writes counted (got {s['fact_writes']})")
    check(s["assumption_writes"] == 2, f"assumption writes counted (got {s['assumption_writes']})")
    check(s["lessons_learned"] == 1, f"lesson writes counted (got {s['lessons_learned']})")
    check(s["memory_searches"] == 1, f"searches counted (got {s['memory_searches']})")

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


main()
