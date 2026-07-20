from __future__ import annotations

"""Executive intelligence (Goal #1, Phase 5).

The layer that stops Nova being purely reactive: it synthesizes goals, reminders,
habits, the digital-twin profile, weather (when available), and the time of day
into a small set of proactive, CONFIDENCE-GATED recommendations — leave earlier,
a deadline is looming, this is your focus window, take a break.

Two design rules keep it from ever becoming annoying:
1. Confidence gate — a candidate is only surfaced when its confidence clears a
   threshold. Weak signals stay silent.
2. Scarcity — at most a few recommendations, ranked by priority × confidence, and
   the caller throttles re-surfacing the same item (see MemoryUnifier).

`recommend()` is a pure function of gathered context, so it is fully testable
without a database, model, or network. The gatherer lives in MemoryUnifier.
"""

from dataclasses import dataclass, field


def day_part(hour: int) -> str:
    if 0 <= hour <= 5:
        return "late night"
    if 6 <= hour <= 11:
        return "morning"
    if 12 <= hour <= 17:
        return "afternoon"
    return "evening"


@dataclass(frozen=True)
class ExecutiveContext:
    now_hour: int = 12
    overdue: list[str] = field(default_factory=list)                 # overdue reminder/goal labels
    upcoming: list[dict] = field(default_factory=list)               # {label, hours_until}
    upcoming_dates: list[dict] = field(default_factory=list)         # {name, label, days_until}
    active_goals: list[str] = field(default_factory=list)
    stalled_goals: list[str] = field(default_factory=list)
    peak_period: str | None = None                                   # from the digital twin
    procrastination: float = 0.0
    stress_level: str = "low"
    weather: dict | None = None                                      # {condition, ...} when available
    min_confidence: float = 0.5
    max_recommendations: int = 3


@dataclass
class Recommendation:
    key: str          # stable id for dedup/throttle
    kind: str         # deadline | focus | break | relationship | weather | goal
    message: str
    confidence: float
    priority: int
    rationale: str

    def as_dict(self) -> dict:
        return {
            "key": self.key, "kind": self.kind, "message": self.message,
            "confidence": round(self.confidence, 2), "priority": self.priority, "rationale": self.rationale,
        }


def recommend(ctx: ExecutiveContext) -> list[dict]:
    cands: list[Recommendation] = []
    current = day_part(ctx.now_hour)

    # 1) Overdue items — highest priority, high confidence (it's a fact).
    for label in ctx.overdue[:3]:
        cands.append(Recommendation(
            f"overdue:{label.lower()}", "deadline",
            f"'{label}' is overdue — want to handle it now or reschedule it?",
            0.9, 9, "a tracked item is past its due time",
        ))

    # 2) Upcoming deadlines within the next day.
    for u in ctx.upcoming[:3]:
        hrs = u.get("hours_until")
        if hrs is None or hrs > 24:
            continue
        label = str(u.get("label") or "")
        # A looming deadline plus a procrastination tendency = a firmer, more
        # confident nudge (this is exactly when a reminder earns its keep).
        conf = 0.75 + (0.1 if ctx.procrastination >= 0.5 else 0.0)
        cands.append(Recommendation(
            f"upcoming:{label.lower()}", "deadline",
            f"'{label}' is due in about {int(hrs)}h." + (" Given how the week's gone, maybe start it now?" if ctx.procrastination >= 0.5 else ""),
            min(0.9, conf), 7, "a deadline is within 24 hours",
        ))

    # 3) Birthdays / anniversaries coming up.
    for d in ctx.upcoming_dates[:2]:
        when = "today" if d.get("days_until") == 0 else f"in {d.get('days_until')} day(s)"
        cands.append(Recommendation(
            f"date:{str(d.get('name','')).lower()}:{d.get('label')}", "relationship",
            f"{d.get('name')}'s {d.get('label')} is {when} — worth planning something?",
            0.8, 6, "an important personal date is near",
        ))

    # 4) Weather-driven (only when weather signal is actually present).
    if ctx.weather:
        cond = str(ctx.weather.get("condition") or ctx.weather.get("main") or "").lower()
        if any(w in cond for w in ("rain", "snow", "storm", "sleet")):
            cands.append(Recommendation(
                "weather:precip", "weather",
                f"{cond.title()} is expected — leave a little earlier and grab a jacket if you're heading out.",
                0.7, 6, "adverse weather in the current conditions",
            ))

    # 5) Focus window — it's the twin's peak period and there's real work queued.
    if ctx.peak_period and current == ctx.peak_period and ctx.active_goals:
        top = ctx.active_goals[0]
        cands.append(Recommendation(
            "focus:peak", "focus",
            f"This is usually your sharpest time of day — a good window to push on '{top}'.",
            0.6, 5, "current time matches your peak focus period and a goal is active",
        ))

    # 6) Stalled goal nudge.
    for title in ctx.stalled_goals[:1]:
        cands.append(Recommendation(
            f"stalled:{title.lower()}", "goal",
            f"'{title}' hasn't moved in a while — want to take a small next step on it?",
            0.55, 5, "an active goal has had no recent progress",
        ))

    # 7) Rest / break — elevated stress or working in the small hours.
    if ctx.stress_level == "elevated" or current == "late night":
        cands.append(Recommendation(
            "break:rest", "break",
            "You've been going hard" + (" and it's late" if current == "late night" else "") + " — a real break might pay off.",
            0.6, 4, "elevated stress or late-night activity",
        ))

    # Gate by confidence, rank by priority × confidence, dedup by key, cap.
    seen: set[str] = set()
    ranked = sorted(cands, key=lambda r: r.priority * r.confidence, reverse=True)
    out: list[dict] = []
    for r in ranked:
        if r.confidence < ctx.min_confidence or r.key in seen:
            continue
        seen.add(r.key)
        out.append(r.as_dict())
        if len(out) >= ctx.max_recommendations:
            break
    return out
