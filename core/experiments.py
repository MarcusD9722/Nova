from __future__ import annotations

"""Autonomous experimentation (Goal #15, Phase 7).

A safe framework for A/B-testing potential improvements — prompt variants,
retrieval algorithms, memory ranking, planning heuristics, scheduling — by
collecting trial metrics (accuracy, reliability, latency, resource use) and
comparing variants into a ranked RECOMMENDATION.

Safety is structural, not just policy: `compare_variants` only ever *recommends*
a winner and returns `requires_approval: True`. Nothing here applies a change —
adopting a variant is a human decision, routed through the same approval gate as
any other change. It also refuses to declare a winner on thin data or a slim
margin ("inconclusive"), so it never nudges Nova toward a risky swap on noise.

Pure and deterministic → fully testable. The metric collection that feeds it is
the live/orchestration part.
"""

from typing import Any

# Metric direction. Anything not listed is treated as higher-is-better.
LOWER_BETTER = frozenset({"latency", "latency_s", "resource", "tokens", "cost", "errors"})

_DEFAULT_WEIGHTS = {"accuracy": 0.4, "reliability": 0.3, "latency": 0.2, "latency_s": 0.2, "resource": 0.1}


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def compare_variants(
    trials: list[dict[str, Any]],
    *,
    weights: dict[str, float] | None = None,
    min_samples: int = 3,
    min_margin: float = 0.05,
) -> dict[str, Any]:
    """Rank experiment variants from trial metrics and recommend one — or decline.

    trials: [{"variant": str, "metrics": {"accuracy": .., "latency_s": .., ...}}, ...]
    Returns a ranking, a verdict ('adopt' | 'inconclusive'), the margin, a
    confidence, and requires_approval=True (never auto-applies).
    """
    weights = weights or _DEFAULT_WEIGHTS
    by_variant: dict[str, list[dict[str, float]]] = {}
    for t in trials:
        v = str(t.get("variant") or "").strip()
        m = t.get("metrics") or {}
        if v and isinstance(m, dict):
            by_variant.setdefault(v, []).append({k: float(x) for k, x in m.items() if isinstance(x, (int, float))})

    if len(by_variant) < 2:
        return {"verdict": "inconclusive", "reason": "need at least two variants to compare",
                "ranking": [], "requires_approval": True}

    # Per-variant mean of each metric.
    metric_keys = sorted({k for samples in by_variant.values() for s in samples for k in s})
    means: dict[str, dict[str, float]] = {}
    counts: dict[str, int] = {}
    for v, samples in by_variant.items():
        counts[v] = len(samples)
        means[v] = {k: _mean([s[k] for s in samples if k in s]) for k in metric_keys}

    # Min-max normalize each metric across variants; invert lower-better; weight.
    def normed(metric: str) -> dict[str, float]:
        vals = {v: means[v].get(metric, 0.0) for v in by_variant}
        lo, hi = min(vals.values()), max(vals.values())
        out = {}
        for v, x in vals.items():
            n = 0.5 if hi == lo else (x - lo) / (hi - lo)
            if metric in LOWER_BETTER:
                n = 1.0 - n
            out[v] = n
        return out

    used_weight = sum(weights.get(k, 0.1) for k in metric_keys) or 1.0
    scores: dict[str, float] = {v: 0.0 for v in by_variant}
    per_metric = {k: normed(k) for k in metric_keys}
    for k in metric_keys:
        w = weights.get(k, 0.1)
        for v in by_variant:
            scores[v] += w * per_metric[k][v]
    scores = {v: round(scores[v] / used_weight, 4) for v in scores}

    ranking = sorted(by_variant, key=lambda v: scores[v], reverse=True)
    winner, runner = ranking[0], ranking[1]

    # Margin = the winner's mean RELATIVE improvement over the runner on the raw
    # metrics (direction-aware), NOT the normalized-score gap. Min-max scores are
    # only for cross-metric ranking; using them for the margin would turn a 0.01
    # accuracy difference into a "100% better" illusion and defeat the guard.
    def _rel_improvement() -> float:
        imps: list[float] = []
        for k in metric_keys:
            w, r = means[winner].get(k, 0.0), means[runner].get(k, 0.0)
            base = max(abs(r), 1e-9)
            imps.append((r - w) / base if k in LOWER_BETTER else (w - r) / base)
        return sum(imps) / len(imps) if imps else 0.0

    margin = round(_rel_improvement(), 4)
    enough = counts[winner] >= min_samples and counts[runner] >= min_samples
    decisive = margin >= min_margin
    verdict = "adopt" if (enough and decisive) else "inconclusive"
    confidence = round(min(1.0, min(counts.values()) / 10.0) * min(1.0, margin / 0.2), 3) if (enough and decisive) else 0.0

    return {
        "verdict": verdict,
        "winner": winner if verdict == "adopt" else None,
        "margin": margin,
        "confidence": confidence,
        "requires_approval": True,  # structural: never auto-applies
        "note": (f"Recommend adopting '{winner}' (pending your approval)." if verdict == "adopt"
                 else f"Inconclusive — {'need more samples' if not enough else 'margin too small'}; keep the current approach."),
        "ranking": [
            {"variant": v, "score": scores[v], "samples": counts[v], "means": {k: round(means[v].get(k, 0.0), 4) for k in metric_keys}}
            for v in ranking
        ],
    }
