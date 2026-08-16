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

    # Uncalibrated: the default, and it says so.
    r = match(v, [prof(M_ID, "Marcus", v)])
    check(abs(r.threshold - DEFAULT_THRESHOLD) < 1e-9,
          f"an uncalibrated profile reports the default ({r.threshold})")
    check(r.threshold_source == "default", "labelled as the default")

    # Calibrated: the per-profile value, NOT the global fallback. This is the
    # bug P5.2 fixes — the diagnostic used to describe a decision never made.
    r = match(v, [prof(M_ID, "Marcus", v, thr=0.71)])
    check(abs(r.threshold - 0.71) < 1e-9,
          f"a calibrated profile reports ITS threshold ({r.threshold})")
    check(r.threshold_source == "profile", "labelled as calibrated")
    check(r.status == "known", "and the decision still lands")

    r = match(w, [prof(M_ID, "Marcus", v, thr=0.71)])
    check(r.status == "unknown" and "0.710" in r.reason,
          f"a rejection quotes the same effective number ({r.reason})")

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


async def main():
    await test_threshold_prefers_zero_false_accepts()
    await test_overlap_fails_rather_than_loosening()
    await test_margin_refuses_wrong_person()
    await test_calibrate_end_to_end_and_requires_two_humans()
    await test_persistence_and_model_mismatch()
    await test_effective_threshold_is_reported()
    await test_status_tells_the_truth()
    await test_calibration_goes_stale_when_a_speaker_is_added()
    check.finish()


if __name__ == "__main__":
    run(main)
