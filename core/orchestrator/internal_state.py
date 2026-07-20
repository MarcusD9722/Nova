from __future__ import annotations

"""Internal operational state (Goal #12, Phase 3.5).

Seven internal reasoning/operational metrics that influence how Nova decides —
NOT simulated human emotions. Each is derived deterministically from telemetry
that already exists (the self-eval MetricsCollector snapshot plus live task/goal
counts), and each carries a `basis` string naming the signal it came from, so
the number is never a mystery and never a pretend feeling.

    confidence       how reliable recent action has been (tool success, few assumptions)
    uncertainty      how much recent belief rests on assumptions / empty output
    mental_workload  how much work is in flight (queued/running tasks, goals, tools/turn)
    focus            how concentrated vs. scattered current work is (load + latency steadiness)
    energy           operational headroom (fast + reliable = capacity to take on more)
    curiosity        rate of novel intake (new facts + recall activity)
    learning_rate    rate of durable learning (lessons distilled)

These influence decisions via `operating_hints` — short, honest guidance the
reply/grounding layer can act on (e.g. hedge under high uncertainty, stay concise
under heavy workload). They are advisory, never a behavior override.
"""

from dataclasses import dataclass


def clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


@dataclass(frozen=True)
class InternalStateInputs:
    snapshot: dict          # MetricsCollector.snapshot()
    queued_tasks: int = 0   # background autonomy tasks waiting
    running_tasks: int = 0  # background autonomy tasks executing
    active_goals: int = 0   # open goals

    # Latency target (seconds) at/above which reply latency reads as full
    # "pressure" for the energy metric. A slow local model at ~30s p95 = no
    # headroom; fast replies = high headroom.
    latency_target_s: float = 30.0


def derive_internal_state(inp: InternalStateInputs) -> dict:
    s = inp.snapshot or {}
    turns = int(s.get("turns") or 0)
    tool_calls = int(s.get("tool_calls") or 0)
    empty = int(s.get("empty_replies") or 0)
    tfr = float(s.get("tool_failure_rate") or 0.0)
    avg_tools = float(s.get("avg_tools_per_turn") or 0.0)
    fact_writes = int(s.get("fact_writes") or 0)
    assumption_writes = int(s.get("assumption_writes") or 0)
    lessons = int(s.get("lessons_learned") or 0)
    searches = int(s.get("memory_searches") or 0)
    lat = s.get("reply_latency_s") or {}
    p50 = float(lat.get("p50") or 0.0)
    p95 = float(lat.get("p95") or 0.0)

    inflight = int(inp.queued_tasks) + int(inp.running_tasks)
    has_signal = turns > 0 or tool_calls > 0 or fact_writes > 0 or inflight > 0

    def var(value: float, basis: str) -> dict:
        return {"value": round(clamp01(value), 3), "basis": basis}

    # Idle / cold start: refuse to claim strong metrics from no data. Neutral
    # 0.5 with an honest basis beats "1.0 confidence" derived from zero activity.
    if not has_signal:
        neutral = "insufficient telemetry (idle)"
        state = {k: var(0.5, neutral) for k in
                 ("confidence", "uncertainty", "mental_workload", "focus", "energy",
                  "curiosity", "learning_rate")}
        state["operating_hints"] = []
        state["sampled_turns"] = turns
        return state

    empty_rate = (empty / turns) if turns else 0.0
    assumption_ratio = (assumption_writes / fact_writes) if fact_writes else 0.0
    latency_spread = ((p95 - p50) / p95) if p95 > 0 else 0.0
    latency_pressure = clamp01(p95 / inp.latency_target_s) if inp.latency_target_s > 0 else 0.0

    confidence = clamp01(0.5 * (1 - tfr) + 0.3 * (1 - assumption_ratio) + 0.2 * (1 - empty_rate))
    uncertainty = clamp01(0.6 * assumption_ratio + 0.4 * empty_rate)
    workload = clamp01(0.5 * min(1.0, inflight / 8.0)
                       + 0.3 * min(1.0, inp.active_goals / 5.0)
                       + 0.2 * min(1.0, avg_tools / 4.0))
    focus = clamp01(0.6 * (1 - workload) + 0.4 * (1 - latency_spread))
    energy = clamp01(0.6 * (1 - latency_pressure) + 0.4 * (1 - tfr))
    novelty = min(1.0, fact_writes / max(1, turns))
    exploring = min(1.0, searches / max(1, turns * 2))
    curiosity = clamp01(0.6 * novelty + 0.4 * exploring)
    learning_rate = clamp01(min(1.0, lessons / max(1.0, turns / 5.0)))

    state = {
        "confidence": var(confidence, f"tool failure rate {tfr:.2f}, {assumption_ratio:.2f} of new facts were assumptions, {empty_rate:.2f} empty replies"),
        "uncertainty": var(uncertainty, f"{assumption_ratio:.2f} assumption ratio, {empty_rate:.2f} empty-reply rate"),
        "mental_workload": var(workload, f"{inflight} task(s) in flight, {inp.active_goals} active goal(s), {avg_tools:.1f} tools/turn"),
        "focus": var(focus, f"workload {workload:.2f}, latency steadiness {(1 - latency_spread):.2f}"),
        "energy": var(energy, f"p95 latency {p95:.1f}s vs {inp.latency_target_s:.0f}s target, tool failure rate {tfr:.2f}"),
        "curiosity": var(curiosity, f"{fact_writes} new fact(s) over {turns} turn(s), {searches} recall(s)"),
        "learning_rate": var(learning_rate, f"{lessons} lesson(s) distilled over {turns} turn(s)"),
    }

    hints: list[str] = []
    if uncertainty >= 0.5:
        hints.append("Operating with elevated uncertainty — verify or hedge before asserting.")
    if workload >= 0.6:
        hints.append("Heavy workload in flight — keep replies concise and defer non-urgent background work.")
    if energy <= 0.35:
        hints.append("Low operational headroom (slow or erroring) — prefer simple, reliable actions.")
    if confidence <= 0.4:
        hints.append("Low confidence signal — prefer asking a clarifying question over guessing.")
    state["operating_hints"] = hints
    state["sampled_turns"] = turns
    return state


def operating_hint_text(state: dict) -> str:
    """One-line advisory for the grounding/reply layer, or '' when nothing is
    worth flagging. Kept short and factual — a nudge, not a mood."""
    hints = (state or {}).get("operating_hints") or []
    return " ".join(hints)
