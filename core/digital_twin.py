from __future__ import annotations

"""Personal digital twin (Goal #4, Phase 5).

A continuously-improving model of Marcus's WORKING PATTERNS, derived entirely
from signals Nova already records — turn timestamps, mood/wellbeing trends,
interest focus, lesson rate, reminder punctuality, goal progress. It PREDICTS
(best time to work, likelihood of procrastination, focus window); it never
impersonates Marcus or claims to know his mind. Confidence scales with how much
data exists, and everything carries a `basis` naming the signal it came from.

The derivation is a pure function of gathered inputs, so it is fully testable
without a database or a model. `gather_and_derive(memory)` collects the inputs.
"""

from dataclasses import dataclass, field


def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


# Coarse day parts for describing when Marcus is active.
def _bucket(hour: int) -> str:
    if 0 <= hour <= 5:
        return "late night"
    if 6 <= hour <= 11:
        return "morning"
    if 12 <= hour <= 17:
        return "afternoon"
    return "evening"


_STRESS_WORDS = ("stressed", "tired", "sad", "anxious", "overwhelmed", "late_night", "frustrated", "down")


@dataclass(frozen=True)
class DigitalTwinInputs:
    turn_hours: list[int] = field(default_factory=list)   # local hour (0-23) of recent turns
    mood_trend: str = ""
    wellbeing_trend: str = ""
    interests: list[str] = field(default_factory=list)
    lessons_recent: int = 0
    reminders_ontime: int = 0
    reminders_late: int = 0
    goals_active: int = 0
    goals_stalled: int = 0   # active goals with no recent task progress
    min_samples: int = 8     # below this, patterns are "still forming"


def derive_profile(inp: DigitalTwinInputs) -> dict:
    hours = [h for h in inp.turn_hours if isinstance(h, int) and 0 <= h <= 23]
    n = len(hours)
    confidence = round(_clamp01(n / 60.0), 2)  # tightens as activity accumulates

    def field_(value, basis: str) -> dict:
        return {"value": value, "basis": basis}

    if n < inp.min_samples:
        return {
            "enough_data": False,
            "confidence": confidence,
            "note": f"Still learning Marcus's patterns — only {n} recent data point(s) so far.",
            "predictions": {},
        }

    # Hour histogram -> preferred work hours + peak day-part.
    hist: dict[int, int] = {}
    for h in hours:
        hist[h] = hist.get(h, 0) + 1
    top_hours = sorted(hist, key=lambda h: hist[h], reverse=True)[:3]
    top_hours.sort()

    bucket_counts: dict[str, int] = {}
    for h in hours:
        b = _bucket(h)
        bucket_counts[b] = bucket_counts.get(b, 0) + 1
    peak_bucket = max(bucket_counts, key=lambda b: bucket_counts[b])

    # Procrastination: late-vs-ontime reminders blended with stalled goals.
    total_rem = inp.reminders_ontime + inp.reminders_late
    late_ratio = (inp.reminders_late / total_rem) if total_rem else 0.0
    stalled_ratio = (inp.goals_stalled / inp.goals_active) if inp.goals_active else 0.0
    procrastination = _clamp01(0.6 * late_ratio + 0.4 * stalled_ratio)

    # Stress from the (already coarse, honest) mood/wellbeing trend text.
    blob = f"{inp.mood_trend} {inp.wellbeing_trend}".lower()
    stress_hits = sum(1 for w in _STRESS_WORDS if w in blob)
    stress_level = "elevated" if stress_hits >= 2 else "some" if stress_hits == 1 else "low"

    # Learning speed from recent lesson distillation volume.
    learning = "rapid" if inp.lessons_recent >= 5 else "steady" if inp.lessons_recent >= 1 else "quiet"

    hour_label = ", ".join(f"{h:02d}:00" for h in top_hours)
    profile = {
        "enough_data": True,
        "confidence": confidence,
        "preferred_work_hours": field_(top_hours, f"most active at {hour_label} across {n} recent sessions"),
        "peak_period": field_(peak_bucket, f"{bucket_counts[peak_bucket]}/{n} of recent activity is in the {peak_bucket}"),
        "procrastination_likelihood": field_(
            round(procrastination, 2),
            f"{inp.reminders_late}/{total_rem or 0} reminders late, {inp.goals_stalled}/{inp.goals_active or 0} goals stalled",
        ),
        "stress_level": field_(stress_level, f"mood/wellbeing signal: {inp.mood_trend or 'none'}; {inp.wellbeing_trend or 'none'}"),
        "learning_speed": field_(learning, f"{inp.lessons_recent} lesson(s) distilled recently"),
        "interests": field_(inp.interests[:6], "from recent interest-focus tracking"),
    }

    profile["predictions"] = {
        "best_time_to_work": field_(peak_bucket, f"peak activity is the {peak_bucket} (hours {hour_label})"),
        "likely_to_procrastinate": field_(procrastination >= 0.5, f"procrastination signal {round(procrastination, 2)}"),
        "focus_window": field_(peak_bucket, "concentrate demanding work in the peak period"),
    }
    return profile
