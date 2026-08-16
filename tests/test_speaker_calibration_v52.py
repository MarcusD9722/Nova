"""V3 P5.2: the calibration MECHANICS. Not human acceptance.

Everything here runs on synthetic score distributions. That is enough to prove
the algorithm refuses what it should refuse, persists what it should persist,
and reports the truth — and it is NOT enough to say anything about how well Nova
recognises a real voice. Synthetic tests can never mark P5.2 human acceptance
PASS, and this file does not claim to.

The bar the algorithm enforces, in priority order:

    1. zero observed false accepts
    2. >= 90% genuine acceptance
    3. among candidates satisfying both, the STRICTEST

If (1) and (2) cannot both hold, calibration FAILS and says why. A threshold
that only passes because it was loosened carries a claim it cannot support.

Run:  venv\\Scripts\\python.exe tests\\test_speaker_calibration_v52.py
"""

from __future__ import annotations

import os
import random
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")
os.environ.setdefault("NOVA_REPO_ROOT", str(REPO))

from harness import Checks, run  # noqa: E402

check = Checks()

M_ID, G_ID = "p-marcus", "p-guest"


def trials_separable(seed=7):
    """Well-separated distributions — the shape a good enrollment produces."""
    rnd = random.Random(seed)
    from core.speaker.calibration import Trial
    out = []
    for _ in range(20):        # Marcus genuine, phase A
        out.append(Trial(truth=M_ID, top_profile_id=M_ID,
                         top_score=rnd.uniform(0.72, 0.90), status="known", phase="A"))
    for _ in range(12):        # real stranger scoring AGAINST Marcus
        out.append(Trial(truth=G_ID, top_profile_id=M_ID,
                         top_score=rnd.uniform(0.22, 0.44), status="unknown", phase="A"))
    for _ in range(12):        # phase B, both enrolled
        out.append(Trial(truth=M_ID, top_profile_id=M_ID,
                         top_score=rnd.uniform(0.74, 0.88),
                         second_score=rnd.uniform(0.20, 0.38),
                         second_profile_id=G_ID, status="known", phase="B"))
        out.append(Trial(truth=G_ID, top_profile_id=G_ID,
                         top_score=rnd.uniform(0.70, 0.86),
                         second_score=rnd.uniform(0.18, 0.36),
                         second_profile_id=M_ID, status="known", phase="B"))
    return out


async def test_threshold_prefers_zero_false_accepts():
    check.section("the fit refuses false accepts before it chases acceptance")
    from core.speaker.calibration import fit_profile_threshold

    genuine = [0.80, 0.78, 0.83, 0.76, 0.85, 0.79, 0.81, 0.77, 0.84, 0.82]
    impostor = [0.31, 0.28, 0.44, 0.36, 0.22]
    fit = fit_profile_threshold(M_ID, "Marcus", genuine, impostor)
    check(fit.ok, f"a separable pair calibrates ({fit.reason})")
    check(fit.false_accepts == 0, "with zero false accepts")
    check(fit.genuine_accept_rate >= 0.90, f"and >=90% acceptance ({fit.genuine_accept_rate})")
    check(fit.threshold > max(impostor),
          f"the threshold sits ABOVE every impostor score ({fit.threshold} > {max(impostor)})")
    check(fit.separation is not None and fit.separation > 0,
          f"and the separation is reported ({fit.separation})")

    # Strictest-among-valid: the most headroom for a voice the fit never saw.
    looser = fit_profile_threshold(M_ID, "Marcus", genuine, [0.10] * 5)
    check(looser.threshold >= fit.threshold - 0.001,
          f"weaker impostors do not force a looser bar ({looser.threshold})")


async def test_overlap_fails_rather_than_loosening():
    check.section("overlapping distributions FAIL — the dashboard does not win")
    from core.speaker.calibration import fit_profile_threshold

    genuine = [0.62, 0.58, 0.65, 0.55, 0.70, 0.60, 0.57, 0.63, 0.59, 0.61]
    impostor = [0.64, 0.59, 0.66, 0.61, 0.58]      # a stranger who sounds alike
    fit = fit_profile_threshold(M_ID, "Marcus", genuine, impostor)
    check(not fit.ok, "calibration fails rather than picking a number")
    check(fit.threshold is None, "no threshold is proposed")
    check("overlap" in fit.reason, f"and the reason names the overlap ({fit.reason})")
    check(fit.separation is not None and fit.separation < 0,
          f"with the measured separation reported ({fit.separation})")

    check(not fit_profile_threshold(M_ID, "M", [0.8] * 3, [0.2]).ok,
          "too few genuine trials fails")
    no_imp = fit_profile_threshold(M_ID, "M", [0.8] * 10, [])
    check(not no_imp.ok and "impostor" in no_imp.reason,
          "and no impostor evidence fails — a bound needs something to bound")


# ── the first real human run (2026-08-16) ────────────────────────────────────
#
# Marcus's measured distribution, reconstructed. The live run reported:
#
#     genuine_n 32   genuine_min 0.1092   genuine_p05 0.2405   median 0.55495
#     impostor_n 23  impostor_max 0.2001  impostor_median 0.0778
#     separation +0.0404      threshold null      "the distributions overlap"
#
# The separation was POSITIVE. A valid threshold sat near 0.21-0.25 and the
# fitter never looked below 0.30. This dataset pins that boundary exactly so the
# floor cannot come back.

def _marcus_run1_genuine():
    """32 genuine scores matching the live run's five reported statistics."""
    # index:            0       1      2 <- p05      3 <- accept ceiling
    lo = [0.1092, 0.2000, 0.2405, 0.2500]
    mid = [0.30, 0.33, 0.36, 0.39, 0.42, 0.45, 0.48, 0.50, 0.52, 0.53, 0.54]
    med = [0.55495, 0.55495]                       # indexes 15,16 -> median
    hi = [0.56, 0.58, 0.60, 0.62, 0.64, 0.66, 0.68, 0.70,
          0.72, 0.74, 0.76, 0.78, 0.80, 0.82, 0.85]
    vals = lo + mid + med + hi
    assert len(vals) == 32, len(vals)
    return vals


def _marcus_run1_impostor():
    """23 impostor scores with max 0.2001 and median 0.0778."""
    low = [0.01, 0.02, 0.03, 0.035, 0.04, 0.05, 0.055, 0.06, 0.065, 0.07, 0.075]
    med = [0.0778]                                  # index 11 -> median of 23
    high = [0.08, 0.09, 0.10, 0.11, 0.12, 0.13, 0.14, 0.15, 0.16, 0.18, 0.2001]
    vals = low + med + high
    assert len(vals) == 23, len(vals)
    return vals


async def test_human_run1_threshold_below_the_old_floor():
    check.section("HUMAN RUN 1: a valid threshold below the old 0.30 floor")
    import statistics as _st

    from core.speaker.calibration import (MIN_GENUINE_ACCEPT_RATE,
                                          THRESHOLD_GRID, THRESHOLD_MIN,
                                          THRESHOLD_MAX, fit_profile_threshold)

    genuine, impostor = _marcus_run1_genuine(), _marcus_run1_impostor()

    # The fixture really is the reported distribution.
    check(len(genuine) == 32 and len(impostor) == 23,
          f"32 genuine / 23 impostor ({len(genuine)}/{len(impostor)})")
    check(abs(min(genuine) - 0.1092) < 1e-9, "genuine_min 0.1092")
    check(abs(_st.median(genuine) - 0.55495) < 1e-9, "genuine_median 0.55495")
    check(abs(max(impostor) - 0.2001) < 1e-9, "impostor_max 0.2001")
    check(abs(_st.median(impostor) - 0.0778) < 1e-9, "impostor_median 0.0778")

    fit = fit_profile_threshold(M_ID, "Marcus", genuine, impostor)
    check(abs(fit.genuine_p05 - 0.2405) < 1e-9,
          f"genuine_p05 0.2405 ({fit.genuine_p05})")
    check(abs(fit.separation - 0.0404) < 1e-4,
          f"separation +0.0404 ({fit.separation})")

    # THE FIX.
    check(fit.ok, f"the fit now succeeds ({fit.reason})")
    check(fit.threshold is not None and fit.threshold < 0.30,
          f"with a threshold BELOW the old floor ({fit.threshold})")
    check(fit.false_accepts == 0, f"zero false accepts ({fit.false_accepts})")
    check(fit.genuine_accept_rate >= MIN_GENUINE_ACCEPT_RATE,
          f">= 90% genuine accepted ({fit.genuine_accept_rate})")

    # EXACT selection, not "some threshold". 29 of 32 must clear the bar, so the
    # ceiling is the 29th largest genuine score (0.25); anything above 0.2001
    # admits no impostor. Strictest-first therefore lands on exactly 0.25.
    check(abs(fit.threshold - 0.25) < 1e-9,
          f"the STRICTEST valid candidate, 0.25 ({fit.threshold})")
    check(abs(fit.accept_ceiling - 0.25) < 1e-9,
          f"accept_ceiling is 0.25 ({fit.accept_ceiling})")
    check(sum(1 for s in genuine if s >= 0.25) == 29,
          "29 of 32 genuine clear it — 90.6%")
    check(sum(1 for s in impostor if s >= 0.25) == 0, "and no impostor does")
    # One step stricter must genuinely fail, or 0.25 was not the strictest.
    check(sum(1 for s in genuine if s >= 0.26) / 32 < MIN_GENUINE_ACCEPT_RATE,
          "0.26 would drop below 90% — so 0.25 really is the ceiling")

    # THE OLD GRID WOULD STILL FAIL. Proven by re-running the same search
    # restricted to the old range, rather than asserting it from memory.
    old_grid = [c for c in THRESHOLD_GRID if c >= 0.30]
    old_best = next((c for c in reversed(old_grid)
                     if sum(1 for s in impostor if s >= c) == 0
                     and sum(1 for s in genuine if s >= c) / len(genuine)
                         >= MIN_GENUINE_ACCEPT_RATE), None)
    check(old_best is None,
          f"the old 0.30-floor grid finds nothing ({old_best}) — this is the bug")
    check(THRESHOLD_MIN == 0.0 and THRESHOLD_MAX == 1.0,
          f"the grid now spans the metric's usable range ({THRESHOLD_MIN}"
          f"-{THRESHOLD_MAX})")
    check(min(THRESHOLD_GRID) == 0.0 and max(THRESHOLD_GRID) == 1.0
          and len(THRESHOLD_GRID) == 101,
          f"101 candidates at 0.01 resolution ({len(THRESHOLD_GRID)})")


async def test_strictest_valid_threshold_is_chosen():
    check.section("several candidates pass — the STRICTEST wins")
    from core.speaker.calibration import fit_profile_threshold

    # Every candidate from 0.22 to 0.25 gives 0 false accepts and >=90%
    # acceptance. Widening the floor must not make the fitter reach for a looser
    # bar just because looser bars are now reachable.
    genuine = [0.24, 0.25, 0.26, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90]
    impostor = [0.05, 0.10, 0.15, 0.21]
    fit = fit_profile_threshold(M_ID, "Marcus", genuine, impostor)
    check(fit.ok, f"it fits ({fit.reason})")

    # Enumerate what actually passes, so the expected answer is derived from the
    # rule rather than asserted from memory.
    passing = [c for c in (0.22, 0.23, 0.24, 0.25, 0.26)
               if sum(1 for s in impostor if s >= c) == 0
               and sum(1 for s in genuine if s >= c) / len(genuine) >= 0.90]
    check(passing == [0.22, 0.23, 0.24, 0.25],
          f"four candidates satisfy both bars ({passing})")
    check(abs(fit.threshold - max(passing)) < 1e-9,
          f"the strictest of them, {max(passing)}, is chosen ({fit.threshold})")
    check(abs(fit.genuine_accept_rate - 0.90) < 1e-9,
          f"at exactly the 90% floor ({fit.genuine_accept_rate})")
    check(fit.false_accepts == 0, "and zero false accepts")
    check(0.26 not in passing, "0.26 fails, so 0.25 really is the ceiling")


async def test_true_overlap_still_fails_closed():
    check.section("widening the range did NOT weaken the privacy boundary")
    from core.speaker.calibration import fit_profile_threshold

    # An impostor who genuinely sounds like the target: to accept 90% of the
    # genuine speech you must go below scores the impostor already reached.
    genuine = [0.30, 0.32, 0.35, 0.38, 0.40, 0.42, 0.45, 0.48, 0.50, 0.52]
    impostor = [0.33, 0.36, 0.41, 0.47, 0.55]
    fit = fit_profile_threshold(M_ID, "Marcus", genuine, impostor)

    check(not fit.ok, "the fit fails")
    check(fit.threshold is None, "no threshold is proposed")
    check(fit.accept_ceiling is not None and fit.accept_ceiling <= fit.impostor_max,
          f"because the accept ceiling {fit.accept_ceiling} is at or below the "
          f"impostor max {fit.impostor_max}")
    check("genuinely overlap" in fit.reason,
          f"and the reason says so accurately ({fit.reason})")
    check("search range" not in fit.reason,
          "without blaming the search range for a data problem")

    # A negative-separation case must not be rescued by the new 0.00 floor.
    g2 = [0.05, 0.06, 0.07, 0.08, 0.09, 0.10, 0.11, 0.12, 0.13, 0.14]
    i2 = [0.20, 0.25, 0.30]
    f2 = fit_profile_threshold(M_ID, "Marcus", g2, i2)
    check(not f2.ok, "scores below every impostor still fail")
    check("genuinely overlap" in f2.reason, f"honestly ({f2.reason})")


async def test_failure_diagnostic_distinguishes_causes():
    check.section("a failure names its ACTUAL cause, not 'overlap' by default")
    from core.speaker.calibration import fit_profile_threshold

    # The old message asserted overlap for every failure. Marcus's real data had
    # POSITIVE separation and was told the distributions overlapped, which sends
    # a person to re-record forty perfectly good utterances.
    fit = fit_profile_threshold(M_ID, "Marcus", _marcus_run1_genuine(),
                                _marcus_run1_impostor())
    check(fit.ok and "overlap" not in fit.reason,
          f"the human case no longer claims overlap at all ({fit.reason})")

    too_few = fit_profile_threshold(M_ID, "M", [0.8] * 3, [0.2])
    check("genuine trials" in too_few.reason and "overlap" not in too_few.reason,
          f"too-few-trials says so ({too_few.reason})")
    no_imp = fit_profile_threshold(M_ID, "M", [0.8] * 10, [])
    check("impostor" in no_imp.reason and "overlap" not in no_imp.reason,
          f"no-impostor-evidence says so ({no_imp.reason})")


async def test_margin_refuses_wrong_person():
    check.section("margin: ambiguous beats a confident mistake")
    from core.speaker.calibration import Trial, fit_margin

    m, rate, wrong, why = fit_margin(trials_separable())
    check(m is not None, f"a clean two-profile set calibrates ({why})")
    check(wrong == 0 and rate >= 0.90, f"0 wrong-person, >=90% correct ({rate})")

    # One consistently mis-ranked speaker must NOT be papered over.
    bad = [t for t in trials_separable() if t.phase == "B"]
    for t in bad[:8]:
        if t.truth == G_ID:
            t.top_profile_id = M_ID          # the matcher names the wrong human
            t.top_score, t.second_score = 0.81, 0.30
    m2, _r, _w, why2 = fit_margin(bad)
    check(m2 is None, f"a set containing wrong-person calls does not calibrate ({why2})")
    check("wrong-person" in why2, "and says so plainly")

    m3, _r, _w, why3 = fit_margin([])
    check(m3 is None and "at least 8" in why3, f"too few trials fails ({why3})")


async def test_calibrate_end_to_end_and_requires_two_humans():
    check.section("calibrate() needs two real humans")
    from core.speaker.calibration import calibrate

    res = calibrate(trials_separable(), names={M_ID: "Marcus", G_ID: "Guest"})
    check(res.ok, f"a good dataset calibrates ({res.reason})")
    check(len(res.profiles) == 2, "with a fit per profile")
    check(all(p.ok and p.threshold for p in res.profiles), "both usable")
    check(res.margin is not None, "and a margin")

    only_one = [t for t in trials_separable() if t.truth == M_ID]
    solo = calibrate(only_one)
    check(not solo.ok and "two real humans" in solo.reason,
          f"one speaker is not a calibration ({solo.reason})")


async def test_persistence_and_model_mismatch():
    check.section("persistence, and a model change invalidates it")
    from core.speaker.calibration import CalibrationRecord, CalibrationStore

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        store = CalibrationStore(Path(td) / "spk.db")
        check(await store.load() is None, "nothing stored to begin with")

        rec = CalibrationRecord(margin=0.18, profile_ids=[M_ID, G_ID],
                                metrics={"trials": 56})
        await store.save(rec)
        got = await store.load()
        check(got is not None and abs(got.margin - 0.18) < 1e-9, "the margin round-trips")
        check(got.profile_ids == [M_ID, G_ID], "and the covered profiles")
        check(got.valid_for_build, "and it is valid for this build")

        stale = CalibrationRecord(margin=0.18, profile_ids=[M_ID],
                                  metrics={}, model_revision="deadbeef")
        check(not stale.valid_for_build,
              "a different model revision invalidates it automatically")
        stale2 = CalibrationRecord(margin=0.18, profile_ids=[M_ID], metrics={},
                                   protocol_version=999)
        check(not stale2.valid_for_build, "as does a protocol change")

        await store.clear()
        check(await store.load() is None, "and it can be cleared")

        # No biometric material anywhere in the record.
        import json
        blob = json.dumps({"margin": got.margin, "ids": got.profile_ids,
                           "metrics": got.metrics})
        for banned in ("centroid", "embedding", "samples", "audio"):
            check(banned not in blob, f"no {banned} in the calibration record")


async def test_effective_threshold_is_reported():
    check.section("9: /stt reports the threshold that actually decided")
    import numpy as np

    from core.speaker.matcher import DEFAULT_THRESHOLD, match
    from core.speaker.registry import SpeakerProfile
    from core.speaker.backend import EMBEDDING_DIM, MODEL_ID, MODEL_REVISION

    v = np.zeros(EMBEDDING_DIM, dtype=np.float32); v[0] = 1.0
    w = np.zeros(EMBEDDING_DIM, dtype=np.float32); w[1] = 1.0

    def prof(pid, name, cen, thr=None):
        return SpeakerProfile(profile_id=pid, display_name=name, role="guest",
                              model_id=MODEL_ID, model_revision=MODEL_REVISION,
                              embedding_dim=EMBEDDING_DIM, centroid=cen,
                              sample_count=6, threshold=thr)

    from core.speaker.calibration import CalibrationRecord, resolve_policy
    from core.speaker.matcher import DEFAULT_MARGIN

    def policy_for(profiles, *, calibrated):
        rec = (CalibrationRecord(margin=0.10,
                                 profile_ids=[p.profile_id for p in profiles],
                                 metrics={}) if calibrated else None)
        return resolve_policy(profiles, rec, env_threshold=None, env_margin=None,
                              default_threshold=DEFAULT_THRESHOLD,
                              default_margin=DEFAULT_MARGIN)

    # Uncalibrated: the default, and it says so.
    r = match(v, [prof(M_ID, "Marcus", v)])
    check(abs(r.threshold - DEFAULT_THRESHOLD) < 1e-9,
          f"an uncalibrated profile reports the default ({r.threshold})")
    check(r.threshold_source == "default", "labelled as the default")

    # Calibrated: the per-profile value, NOT the global fallback — but it takes
    # a RESOLVED POLICY to activate it. `match()` never reads profile.threshold
    # itself (V3 P5.2 final closure §2), so the stored number reaches the
    # decision through exactly one door.
    P = [prof(M_ID, "Marcus", v, thr=0.71)]
    pol = policy_for(P, calibrated=True)
    r = match(v, P, policy=pol)
    check(abs(r.threshold - 0.71) < 1e-9,
          f"a calibrated profile reports ITS threshold ({r.threshold})")
    check(r.threshold_source == "calibrated", "labelled as calibrated")
    check(r.status == "known", "and the decision still lands")

    r = match(w, P, policy=pol)
    check(r.status == "unknown" and "0.710" in r.reason,
          f"a rejection quotes the same effective number ({r.reason})")

    # THE ESCAPE HATCH THAT IS NOW CLOSED. A stored threshold with no valid
    # policy behind it must not decide anything — this is what let a stale
    # calibration keep ruling, and what let any direct caller reactivate it.
    hot = [prof(M_ID, "Marcus", v, thr=0.91)]
    r = match(v, hot)
    check(abs(r.threshold - DEFAULT_THRESHOLD) < 1e-9,
          f"a stored 0.91 with NO policy is not used ({r.threshold})")
    check(r.threshold_source != "profile",
          f"and is never sourced from the profile ({r.threshold_source})")
    r = match(v, hot, policy=policy_for(hot, calibrated=False))
    check(abs(r.threshold - DEFAULT_THRESHOLD) < 1e-9,
          "nor when the policy says uncalibrated")

    # Runner-up diagnostics, for calibration only.
    r = match(v, [prof(M_ID, "Marcus", v, thr=0.10), prof(G_ID, "Guest", w, thr=0.10)])
    check(r.second_best_profile_id == G_ID, "the runner-up profile id is exposed")
    from core.speaker.backend import MODEL_ID as _MID
    d = str(r.for_response(model_id=_MID))
    for banned in ("centroid", "embedding", "samples"):
        check(banned not in d, f"and describe() still exposes no {banned}")


async def test_status_tells_the_truth():
    check.section("12: status() reports reality, not a hardcoded False")
    import inspect

    from core.speaker.service import SpeakerService

    src = inspect.getsource(SpeakerService.status)
    check('"threshold_calibrated": False' not in src,
          "threshold_calibrated is no longer hardcoded")
    for field in ("margin_source", "calibrated_at", "profiles_detail",
                  "threshold_calibrated"):
        check(field in src, f"status reports {field}")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        svc = SpeakerService(Path(td) / "spk.db")
        st = await svc.status()
        check(st["threshold_calibrated"] is False,
              "with no profiles and no calibration it is honestly false")
        check(st["margin_source"] == "provisional default",
              f"and the margin source is named ({st['margin_source']})")
        check(st["calibrated_at"] is None, "with no calibration timestamp")


async def test_calibration_goes_stale_when_a_speaker_is_added():
    check.section("10: adding a speaker makes calibration honestly stale")
    import numpy as np

    from core.speaker.backend import EMBEDDING_DIM, MODEL_ID, MODEL_REVISION
    from core.speaker.calibration import CalibrationRecord
    from core.speaker.registry import SpeakerProfile
    from core.speaker.service import SpeakerService

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        svc = SpeakerService(Path(td) / "spk.db")
        await svc.initialize()

        def prof(pid, name, thr):
            c = np.zeros(EMBEDDING_DIM, dtype=np.float32)
            c[abs(hash(pid)) % EMBEDDING_DIM] = 1.0
            return SpeakerProfile(profile_id=pid, display_name=name, role="guest",
                                  model_id=MODEL_ID, model_revision=MODEL_REVISION,
                                  embedding_dim=EMBEDDING_DIM, centroid=c,
                                  sample_count=6, threshold=thr)

        await svc.registry.save(prof(M_ID, "Marcus", 0.70))
        await svc.registry.save(prof(G_ID, "Guest", 0.68))
        await svc.calib.save(CalibrationRecord(margin=0.18,
                                               profile_ids=[M_ID, G_ID], metrics={}))
        st = await svc.status()
        check(st["threshold_calibrated"] is True, "two calibrated profiles -> true")
        check(abs(st["margin"] - 0.18) < 1e-9, f"and the calibrated margin is used ({st['margin']})")
        check(st["margin_source"] == "calibrated", "labelled as calibrated")

        # A third, uncalibrated speaker: the old fit no longer describes the
        # population, so "calibrated" becomes false rather than partly true.
        await svc.registry.save(prof("p-new", "Newcomer", None))
        st = await svc.status()
        check(st["threshold_calibrated"] is False,
              "a newly enrolled speaker makes it stale")


# ── the matcher -> Trial -> fitter chain, end to end (§3, §9) ────────────────
#
# Deliberately NOT built from hand-written Trials. A synthetic Trial that hands
# an `unknown` result a classification profile_id is the exact mistake this
# section exists to catch: it would pass while the live pipeline threw the score
# away, because the field the harness reads (`top_scored_profile_id`) is not the
# field a classification writes (`profile_id`).
#
# So every Trial below is derived from a REAL `match()` result through the same
# mapping the browser uses.

def _basis(a: float, b: float):
    """A unit vector with cosine `a` to e0 (Marcus) and `b` to e1 (Guest)."""
    import numpy as np

    from core.speaker.backend import EMBEDDING_DIM

    v = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    v[0], v[1] = a, b
    rest = 1.0 - a * a - b * b
    v[2] = float(np.sqrt(max(rest, 0.0)))
    return (v / np.linalg.norm(v)).astype(np.float32)


def _trial_from_match(match, truth, *, phase="A", condition="normal"):
    """EXACTLY the mapping tests/live_speaker_calibration.html performs."""
    from core.speaker.calibration import Trial

    return Trial(truth=truth,
                 top_profile_id=match.top_scored_profile_id,
                 top_score=match.similarity,
                 second_score=match.second_best_similarity,
                 second_profile_id=match.second_best_profile_id,
                 status=match.status, condition=condition, phase=phase)


async def test_rejected_trial_keeps_its_score_attribution():
    check.section("3: an UNKNOWN at 0.43 is still Marcus impostor evidence")
    import numpy as np

    from core.speaker.matcher import match
    from core.speaker.registry import SpeakerProfile
    from core.speaker.backend import EMBEDDING_DIM, MODEL_ID, MODEL_REVISION

    def prof(pid, name, cen):
        return SpeakerProfile(profile_id=pid, display_name=name, role="guest",
                              model_id=MODEL_ID, model_revision=MODEL_REVISION,
                              embedding_dim=EMBEDDING_DIM, centroid=cen,
                              sample_count=6)

    marcus_only = [prof(M_ID, "Marcus", _basis(1.0, 0.0))]

    # A real stranger, scoring 0.43 against the only enrolled profile.
    r = match(_basis(0.43, 0.0), marcus_only, thresh=0.55, min_margin=0.10)
    check(r.status == "unknown", f"the honest answer is unknown ({r.status})")
    check(r.profile_id is None, "so nobody is asserted")
    check(r.display_name is None, "and no name is asserted")
    check(r.top_scored_profile_id == M_ID,
          f"but Marcus's profile is recorded as the top scorer ({r.top_scored_profile_id})")
    check(r.top_scored_display_name == "Marcus", "with his display name")
    check(abs(r.similarity - 0.43) < 1e-4, f"at exactly 0.43 ({r.similarity:.4f})")

    # The mapping the harness performs must carry that through to a Trial.
    t = _trial_from_match(r, truth=G_ID)
    check(t.top_profile_id == M_ID,
          "the Trial derived from it attributes the score to Marcus")
    check(abs(t.top_score - 0.43) < 1e-4, "and carries the score itself")

    # Had the harness read `profile_id` (the pre-closure bug), this trial would
    # be attributed to nobody and the score would vanish from the fit.
    check(r.profile_id != r.top_scored_profile_id,
          "reading profile_id here would have lost it — the fields differ")

    # Now prove the FITTER keeps it. Marcus's impostor evidence here comes from
    # nothing but rejected trials, so if a rejection's score were dropped his
    # impostor distribution would be empty and the fit would refuse outright.
    from core.speaker.calibration import calibrate

    two = [prof(M_ID, "Marcus", _basis(1.0, 0.0)),
           prof(G_ID, "Guest", _basis(0.0, 1.0))]
    trials = []
    for i in range(6):                       # Marcus, genuinely recognised
        trials.append(_trial_from_match(
            match(_basis(0.86 - i * 0.01, 0.04), two, thresh=0.55, min_margin=0.10),
            M_ID, phase="B"))
    guest_rows = []
    for i in range(6):                       # the guest speaks; Marcus sits at 0.43
        gm = match(_basis(0.43, 0.52 + i * 0.06), two, thresh=0.55, min_margin=0.10)
        guest_rows.append(gm)
        trials.append(_trial_from_match(gm, G_ID, phase="B"))

    rejected = [g for g in guest_rows if g.status != "known"]
    check(rejected, f"some guest trials were REJECTED, not known "
                    f"({[g.status for g in guest_rows]})")

    res = calibrate(trials, names={M_ID: "Marcus", G_ID: "Guest"})
    mf = {f.profile_id: f for f in res.profiles}[M_ID]
    check(mf.impostor_n == len(guest_rows),
          f"every guest trial gave Marcus impostor evidence, rejected ones "
          f"included ({mf.impostor_n} of {len(guest_rows)})")
    check(mf.impostor_max is not None and abs(mf.impostor_max - 0.43) < 1e-3,
          f"and the bound IS that 0.43 ({mf.impostor_max})")
    check(mf.threshold is None or mf.threshold > 0.43,
          f"so his fitted threshold sits above it ({mf.threshold})")


async def test_no_score_disappears_across_the_scoring_matrix():
    check.section("9: every required trial shape keeps its evidence")
    from core.speaker.calibration import calibrate
    from core.speaker.matcher import match
    from core.speaker.registry import SpeakerProfile
    from core.speaker.backend import EMBEDDING_DIM, MODEL_ID, MODEL_REVISION

    def prof(pid, name, cen):
        return SpeakerProfile(profile_id=pid, display_name=name, role="guest",
                              model_id=MODEL_ID, model_revision=MODEL_REVISION,
                              embedding_dim=EMBEDDING_DIM, centroid=cen,
                              sample_count=6)

    P = [prof(M_ID, "Marcus", _basis(1.0, 0.0)),
         prof(G_ID, "Guest", _basis(0.0, 1.0))]

    trials, shapes = [], {}

    def add(label, emb, truth, phase="A"):
        r = match(emb, P, thresh=0.55, min_margin=0.10)
        trials.append(_trial_from_match(r, truth, phase=phase))
        shapes.setdefault(label, r)
        return r

    # 1. known, correct top — Marcus speaking as himself.
    for i in range(6):
        add("known_correct", _basis(0.88 - i * 0.01, 0.05), M_ID, phase="B")
    check(shapes["known_correct"].status == "known"
          and shapes["known_correct"].profile_id == M_ID,
          f"known/correct produced ({shapes['known_correct'].status})")

    # 2. unknown — top below threshold. A stranger, Marcus nearest.
    for i in range(3):
        add("unknown_low", _basis(0.43 - i * 0.01, 0.10), "p-stranger")
    check(shapes["unknown_low"].status == "unknown",
          f"unknown/below-threshold produced ({shapes['unknown_low'].status})")

    # 3. ambiguous — both clear the bar, neither by the margin.
    for i in range(2):
        add("ambiguous", _basis(0.66, 0.62 + i * 0.005), M_ID, phase="B")
    check(shapes["ambiguous"].status == "ambiguous",
          f"ambiguous produced ({shapes['ambiguous'].status})")

    # 4. the TRUE human as RUNNER-UP — guest speaking, but Marcus ranks first.
    for i in range(2):
        add("truth_runner_up", _basis(0.70, 0.64 - i * 0.01), G_ID, phase="B")
    r4 = shapes["truth_runner_up"]
    check(r4.top_scored_profile_id == M_ID and r4.second_best_profile_id == G_ID,
          "true speaker ranked SECOND (the hard case)")

    # 5 & 6. the guest speaking: Marcus earns impostor evidence as runner-up.
    for i in range(6):
        add("guest_genuine", _basis(0.28 + i * 0.01, 0.90 - i * 0.01), G_ID, phase="B")
    r5 = shapes["guest_genuine"]
    check(r5.top_scored_profile_id == G_ID and r5.second_best_profile_id == M_ID,
          "guest tops, Marcus is runner-up — impostor evidence for Marcus")

    res = calibrate(trials, names={M_ID: "Marcus", G_ID: "Guest"})
    fits = {f.profile_id: f for f in res.profiles}

    check(M_ID in fits and G_ID in fits, "both humans were fitted")
    mf, gf = fits[M_ID], fits[G_ID]

    # NO SCORE MAY DISAPPEAR because the classification was unknown/ambiguous.
    # Marcus's genuine evidence spans trials where he ranked top AND second;
    # his impostor evidence spans rejected trials AND runner-up trials.
    marcus_truth = [t for t in trials if t.truth == M_ID]
    marcus_scored_when_others_spoke = [
        t for t in trials if t.truth != M_ID
        and (t.top_profile_id == M_ID
             or (t.second_profile_id == M_ID and t.second_score is not None))]
    check(mf.genuine_n == len(marcus_truth),
          f"every trial Marcus spoke counts as genuine ({mf.genuine_n} of "
          f"{len(marcus_truth)})")
    check(mf.impostor_n == len(marcus_scored_when_others_spoke),
          f"every score his profile earned while others spoke counts as impostor "
          f"({mf.impostor_n} of {len(marcus_scored_when_others_spoke)})")
    check(mf.impostor_n > 0, "which is not zero — the bound has something to bound")

    # And specifically: the 0.43 rejection is in there.
    check(mf.impostor_max is not None and mf.impostor_max >= 0.43 - 1e-4,
          f"the rejected 0.43 reached his impostor distribution "
          f"(max {mf.impostor_max})")

    # The guest's evidence comes almost entirely from runner-up scores, which
    # is exactly the case top-only collection used to drop to zero.
    check(gf.impostor_n > 0,
          f"the guest has impostor evidence too ({gf.impostor_n})")

    # An ambiguous trial is still evidence, not a discarded row.
    amb = [t for t in trials if t.status == "ambiguous"]
    check(amb, "the matrix really did produce ambiguous trials")
    for t in amb:
        check(t.top_profile_id is not None and t.second_profile_id is not None,
              "an ambiguous trial keeps BOTH ranks")


async def test_sub_030_threshold_survives_the_whole_runtime():
    check.section("a 0.25 threshold is honoured end to end, not clamped anywhere")
    import numpy as np

    from core.speaker.calibration import (CalibrationRecord, CalibrationStore,
                                          resolve_policy)
    from core.speaker.matcher import DEFAULT_MARGIN, DEFAULT_THRESHOLD, match
    from core.speaker.registry import SpeakerProfile
    from core.speaker.service import SpeakerService
    from core.speaker.backend import EMBEDDING_DIM, MODEL_ID, MODEL_REVISION

    LOW = 0.25                      # below the old floor, from the human run
    v = np.zeros(EMBEDDING_DIM, dtype=np.float32); v[0] = 1.0
    w = np.zeros(EMBEDDING_DIM, dtype=np.float32); w[1] = 1.0

    def prof(pid, name, cen, thr=None):
        return SpeakerProfile(profile_id=pid, display_name=name, role="owner",
                              model_id=MODEL_ID, model_revision=MODEL_REVISION,
                              embedding_dim=EMBEDDING_DIM, centroid=cen,
                              sample_count=6, threshold=thr)

    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "spk.sqlite3"
        svc = SpeakerService(db)
        await svc.initialize()
        await svc.registry.save(prof(M_ID, "Marcus", v, thr=LOW))
        await svc.registry.save(prof(G_ID, "Leslie", w, thr=0.38))

        # 1. persists and reloads unrounded/unclamped
        store = CalibrationStore(db)
        await store.save(CalibrationRecord(margin=0.29,
                                           profile_ids=[M_ID, G_ID], metrics={}))
        back = await store.load()
        check(back is not None and abs(back.margin - 0.29) < 1e-9,
              "the calibration record round-trips")
        got = {p.profile_id: p.threshold for p in await svc.registry.all()}
        check(abs(got[M_ID] - LOW) < 1e-9,
              f"0.25 persists in SQLite exactly ({got[M_ID]})")

        # 2. policy resolution accepts it
        pol = await svc.policy()
        check(pol.calibrated, "the policy reports calibrated")
        check(abs(pol.threshold_for(M_ID) - LOW) < 1e-9,
              f"and resolves 0.25 for Marcus ({pol.threshold_for(M_ID)})")
        check(abs(pol.threshold_for(G_ID) - 0.38) < 1e-9, "0.38 for Leslie")

        # 3. the matcher actually decides on it
        r = match(v, await svc.registry.matchable(), policy=pol)
        check(abs(r.threshold - LOW) < 1e-9,
              f"the matcher uses 0.25 ({r.threshold})")
        check(r.threshold_source == "calibrated", "sourced from calibration")

        # A score between the old floor and the new threshold must now be
        # ACCEPTED — that is the whole point, and it would have been rejected
        # before. 0.28 clears 0.25 but would have failed a 0.30 bar.
        #
        # The runner-up is pushed negative so the gap clears the calibrated
        # margin (0.29, the value the real run fitted); otherwise the honest
        # answer here is `ambiguous` and the threshold is not what is being
        # tested.
        mix = np.zeros(EMBEDDING_DIM, dtype=np.float32)
        mix[0] = 0.28
        mix[1] = -0.20
        mix[2] = float(np.sqrt(1.0 - 0.28 ** 2 - 0.20 ** 2))
        r2 = match(mix, await svc.registry.matchable(), policy=pol)
        check(abs(r2.similarity - 0.28) < 1e-4,
              f"a 0.28 utterance scores 0.28 ({r2.similarity})")
        check(r2.second_best_similarity is not None
              and (r2.similarity - r2.second_best_similarity) >= pol.margin,
              f"with a gap clearing the calibrated margin {pol.margin}")
        check(r2.status == "known" and r2.profile_id == M_ID,
              f"and is now recognised ({r2.status}) — under a 0.30 floor it "
              "could not have been")

        # 4. /speaker/status reports it
        st = await svc.status()
        check(st["threshold_calibrated"] is True, "status says calibrated")
        detail = {p["profile_id"]: p for p in st["profiles_detail"]}
        check(abs(detail[M_ID]["effective_threshold"] - LOW) < 1e-9,
              f"effective_threshold 0.25 ({detail[M_ID]['effective_threshold']})")
        check(abs(detail[M_ID]["stored_threshold"] - LOW) < 1e-9,
              "stored_threshold 0.25")

        # 5. env override still wins over a calibrated sub-0.30 value
        os.environ["NOVA_SPEAKER_THRESHOLD"] = "0.80"
        try:
            pol2 = await svc.policy()
            check(pol2.threshold_source == "env override",
                  f"env still takes precedence ({pol2.threshold_source})")
            check(abs(pol2.threshold_for(M_ID) - 0.80) < 1e-9,
                  f"and overrides 0.25 ({pol2.threshold_for(M_ID)})")
        finally:
            os.environ.pop("NOVA_SPEAKER_THRESHOLD", None)

        # 6. no calibration -> unchanged provisional fallback
        await store.clear()
        for p in await svc.registry.all():
            p.threshold = None
            await svc.registry.save(p)
        pol3 = await svc.policy()
        check(pol3.threshold_source == "provisional default"
              and abs(pol3.threshold_for(M_ID) - DEFAULT_THRESHOLD) < 1e-9,
              f"provisional fallback is untouched ({pol3.threshold_for(M_ID)})")
        check(abs(pol3.margin - DEFAULT_MARGIN) < 1e-9, "and so is the margin")


async def test_safety_bars_are_untouched():
    check.section("the constants this fix was forbidden to move")
    from core.speaker import calibration as C

    check(C.MAX_FALSE_ACCEPTS == 0, f"MAX_FALSE_ACCEPTS is 0 ({C.MAX_FALSE_ACCEPTS})")
    check(C.MIN_GENUINE_ACCEPT_RATE == 0.90,
          f"MIN_GENUINE_ACCEPT_RATE is 0.90 ({C.MIN_GENUINE_ACCEPT_RATE})")
    check(C.MARGIN_GRID[0] == 0.02 and C.MARGIN_GRID[-1] == 0.30,
          f"the margin grid is unchanged ({C.MARGIN_GRID[0]}-{C.MARGIN_GRID[-1]})")
    check(C.PROTOCOL_VERSION == 1, "and the protocol version is unchanged")
    # A negative threshold can never be proposed: it would admit vectors with no
    # directional agreement at all, which no amount of impostor evidence from a
    # 23-sample run can rule out at runtime.
    check(min(C.THRESHOLD_GRID) >= 0.0,
          f"no non-positive candidate exists ({min(C.THRESHOLD_GRID)})")


async def main():
    await test_threshold_prefers_zero_false_accepts()
    await test_overlap_fails_rather_than_loosening()
    await test_margin_refuses_wrong_person()
    await test_calibrate_end_to_end_and_requires_two_humans()
    await test_persistence_and_model_mismatch()
    await test_effective_threshold_is_reported()
    await test_status_tells_the_truth()
    await test_calibration_goes_stale_when_a_speaker_is_added()
    await test_rejected_trial_keeps_its_score_attribution()
    await test_no_score_disappears_across_the_scoring_matrix()
    await test_human_run1_threshold_below_the_old_floor()
    await test_strictest_valid_threshold_is_chosen()
    await test_true_overlap_still_fails_closed()
    await test_failure_diagnostic_distinguishes_causes()
    await test_sub_030_threshold_survives_the_whole_runtime()
    await test_safety_bars_are_untouched()
    check.finish()


if __name__ == "__main__":
    run(main)
