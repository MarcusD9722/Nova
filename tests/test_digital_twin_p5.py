"""Phase 5 / #4: personal digital twin.

Working-pattern predictions derived from real signals — predicts, never
impersonates. Pure derivation, tested directly.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.digital_twin import DigitalTwinInputs, derive_profile

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


def main():
    # ── Cold start: honest about insufficient data ──
    cold = derive_profile(DigitalTwinInputs(turn_hours=[9, 10, 14]))
    check(cold["enough_data"] is False, "few data points -> not enough_data")
    check(cold["predictions"] == {}, "no predictions on cold start")
    check(cold["confidence"] < 0.1, "cold-start confidence is low")

    # ── Detects an evening worker, confidence rises with data ──
    evening = [20, 21, 22, 20, 21, 23, 21, 22, 20, 21] * 4  # 40 evening samples
    prof = derive_profile(DigitalTwinInputs(turn_hours=evening))
    check(prof["enough_data"] is True, "enough samples -> profile produced")
    check(prof["peak_period"]["value"] == "evening", f"peak period detected (got {prof['peak_period']['value']})")
    check(prof["predictions"]["best_time_to_work"]["value"] == "evening", "best-time prediction matches peak")
    check(21 in prof["preferred_work_hours"]["value"], "top work hours include the busiest hour")
    check(prof["confidence"] > 0.5, "confidence rises with more samples")

    # ── Procrastination signal from late reminders + stalled goals ──
    proc = derive_profile(DigitalTwinInputs(
        turn_hours=[9] * 20, reminders_ontime=1, reminders_late=9,
        goals_active=4, goals_stalled=3,
    ))
    check(proc["procrastination_likelihood"]["value"] >= 0.5, f"late reminders + stalled goals -> high procrastination (got {proc['procrastination_likelihood']['value']})")
    check(proc["predictions"]["likely_to_procrastinate"]["value"] is True, "predicts likely to procrastinate")

    # ── Punctual, on-track profile -> low procrastination ──
    ontrack = derive_profile(DigitalTwinInputs(
        turn_hours=[9] * 20, reminders_ontime=10, reminders_late=0,
        goals_active=3, goals_stalled=0,
    ))
    check(ontrack["procrastination_likelihood"]["value"] == 0.0, "punctual + on-track -> zero procrastination signal")
    check(ontrack["predictions"]["likely_to_procrastinate"]["value"] is False, "predicts NOT likely to procrastinate")

    # ── Stress read from the (honest, coarse) mood/wellbeing trend ──
    stressed = derive_profile(DigitalTwinInputs(
        turn_hours=[1] * 12, mood_trend="Marcus seemed tired and stressed", wellbeing_trend="up late several nights",
    ))
    check(stressed["stress_level"]["value"] == "elevated", f"multiple stress cues -> elevated (got {stressed['stress_level']['value']})")
    calm = derive_profile(DigitalTwinInputs(turn_hours=[10] * 12, mood_trend="Marcus seemed upbeat"))
    check(calm["stress_level"]["value"] == "low", "no stress cues -> low")

    # ── Learning speed from lesson volume ──
    check(derive_profile(DigitalTwinInputs(turn_hours=[9] * 12, lessons_recent=6))["learning_speed"]["value"] == "rapid",
          "many recent lessons -> rapid learning")
    check(derive_profile(DigitalTwinInputs(turn_hours=[9] * 12, lessons_recent=0))["learning_speed"]["value"] == "quiet",
          "no recent lessons -> quiet")

    # ── Every field carries a basis (transparency, never a bare number) ──
    for key in ("preferred_work_hours", "procrastination_likelihood", "stress_level", "learning_speed"):
        check(bool(prof.get(key, proc.get(key, {})).get("basis") or proc[key]["basis"]), f"{key} carries a basis")

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


main()
