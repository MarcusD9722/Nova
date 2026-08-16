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
    check.finish()


if __name__ == "__main__":
    run(main)
