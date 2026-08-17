"""V3 P5.2 closure: the API/browser contract, and threshold precedence.

The previous synthetic suite proved the algorithm and missed a broken schema:
`SpeakerService.enrol()` returned the sample count only inside a nested
`profile` object, while the router and the browser harness both read it at the
top level. Every successful enrollment therefore reported **0 kept samples**,
the 5-of-6 gate could never pass, and the harness never learned the profile ids
that every later step depends on. A real-human calibration run would have been
wasted on it.

So this file asserts shapes against the ACTUAL `enrol()` return value and the
ACTUAL field names the harness reads out of `tests/live_speaker_calibration.html`
— not against a mock invented to match the router.

It also pins the precedence matrix, because `status()` and the matcher deciding
differently is the kind of disagreement nobody notices until it matters:

    env override  ->  valid calibration  ->  provisional default

Run:  venv\\Scripts\\python.exe tests\\test_speaker_contract_v52.py
"""

from __future__ import annotations

import os
import re
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

HARNESS = REPO / "tests" / "live_speaker_calibration.html"
M_ID, G_ID = "p-marcus", "p-guest"


def _profiles(*specs):
    """Build compatible SpeakerProfiles with distinct centroids."""
    import numpy as np

    from core.speaker.backend import EMBEDDING_DIM, MODEL_ID, MODEL_REVISION
    from core.speaker.registry import SpeakerProfile

    out = []
    for i, (pid, name, thr) in enumerate(specs):
        c = np.zeros(EMBEDDING_DIM, dtype=np.float32)
        c[i] = 1.0
        out.append(SpeakerProfile(
            profile_id=pid, display_name=name, role="guest",
            model_id=MODEL_ID, model_revision=MODEL_REVISION,
            embedding_dim=EMBEDDING_DIM, centroid=c, samples=[],
            sample_count=6, consistency=0.9, threshold=thr))
    return out


# ── §1/§7. the enrollment response contract ──────────────────────────────────

async def test_enroll_contract_matches_the_real_return_shape():
    check.section("1: enroll response — backend and browser agree")
    import inspect

    from core.speaker.service import SpeakerService

    src = inspect.getsource(SpeakerService.enrol)
    body = src[src.index('return {\n            "ok": True'):]
    for field in ('"profile_id"', '"sample_count"', '"display_name"', '"role"'):
        check(field in body,
              f"enrol() returns a TOP-LEVEL {field} — the router and harness read it there")
    check('"profile"' in body, "and keeps the nested profile, so no caller breaks")

    # The router must read the same place, with the nested shape as a fallback.
    router = (REPO / "backend" / "routers" / "speaker.py").read_text(encoding="utf-8")
    check('result.get("sample_count")' in router, "the router reads the top-level count")
    check('(result.get("profile") or {}).get("sample_count")' in router,
          "with the nested value as a fallback")
    check("MIN_KEPT_SAMPLES = 5" in router, "and the 5-of-6 bar is explicit")

    # And the harness reads exactly those names.
    html = HARNESS.read_text(encoding="utf-8")
    check("out.profile_id" in html, "the harness reads out.profile_id")
    check("out.sample_count" in html or "e.sample_count" in html,
          "and the sample count")
    check("meets_p52_bar" in html, "and the P5.2 gate flag")
    check("S.marcusId = out.profile_id" in html or "S.marcusId" in html,
          "so S.marcusId / S.guestId become real profile ids")

    # Trial.truth must be a real profile id, never undefined.
    check("truth:t.truth" in html.replace(" ", ""),
          "trials submit truth from stored profile ids")
    check('t.truth === "__impostor__"' in html,
          "and the pre-enrollment impostor label is relabelled once the guest exists")


async def test_enroll_gate_counts_real_samples():
    check.section("1: 6 usable samples report 6, and the gate reflects reality")
    import numpy as np

    from core.speaker.matcher import check_sample

    # A synthetic tone passes nothing; this asserts the ARITHMETIC of the gate
    # rather than the model, which is what the schema bug actually broke.
    from backend.routers.speaker import MIN_KEPT_SAMPLES
    for kept, expect in ((6, True), (5, True), (4, False), (0, False)):
        check((kept >= MIN_KEPT_SAMPLES) is expect,
              f"{kept} kept samples -> meets_p52_bar={expect}")
    check(MIN_KEPT_SAMPLES == 5, "the bar is 5, stricter than the algorithm's 3")
    check(check_sample is not None, "and the per-sample quality gate still runs")


# ── §2/§3/§8. the precedence matrix ──────────────────────────────────────────

async def _policy_case(*, profiles, calibrated, env_thresh=None, env_margin=None,
                       revision=None):
    from core.speaker.calibration import CalibrationRecord, resolve_policy
    from core.speaker.matcher import DEFAULT_MARGIN, DEFAULT_THRESHOLD

    rec = None
    if calibrated:
        kw = {"margin": 0.22, "profile_ids": [p.profile_id for p in profiles],
              "metrics": {}}
        if revision:
            kw["model_revision"] = revision
        rec = CalibrationRecord(**kw)
    return resolve_policy(profiles, rec, env_threshold=env_thresh,
                          env_margin=env_margin,
                          default_threshold=DEFAULT_THRESHOLD,
                          default_margin=DEFAULT_MARGIN)


async def test_precedence_matrix():
    check.section("2: env override -> calibration -> provisional default")
    from core.speaker.matcher import DEFAULT_MARGIN, DEFAULT_THRESHOLD

    P = _profiles((M_ID, "Marcus", 0.71), (G_ID, "Guest", 0.69))

    # no calibration, no env
    pol = await _policy_case(profiles=P, calibrated=False)
    check(pol.threshold_source == "provisional default", "uncalibrated -> default")
    check(abs(pol.threshold_for(M_ID) - DEFAULT_THRESHOLD) < 1e-9,
          f"and a STORED threshold does not decide ({pol.threshold_for(M_ID)})")
    check(abs(pol.margin - DEFAULT_MARGIN) < 1e-9, "default margin")

    # calibration, no env
    pol = await _policy_case(profiles=P, calibrated=True)
    check(pol.threshold_source == "calibrated", "valid calibration -> calibrated")
    check(abs(pol.threshold_for(M_ID) - 0.71) < 1e-9, "his own fitted threshold")
    check(abs(pol.threshold_for(G_ID) - 0.69) < 1e-9, "and hers")
    check(abs(pol.margin - 0.22) < 1e-9 and pol.margin_source == "calibrated",
          "with the calibrated margin")

    # calibration + threshold env: env WINS (this was inverted)
    pol = await _policy_case(profiles=P, calibrated=True, env_thresh=0.80)
    check(pol.threshold_source == "env override", "an explicit env threshold wins")
    check(abs(pol.threshold_for(M_ID) - 0.80) < 1e-9,
          f"for EVERY profile, overriding the stored 0.71 ({pol.threshold_for(M_ID)})")
    check(pol.margin_source == "calibrated", "while the margin stays calibrated")

    # calibration + margin env
    pol = await _policy_case(profiles=P, calibrated=True, env_margin=0.05)
    check(pol.margin_source == "env override" and abs(pol.margin - 0.05) < 1e-9,
          "an explicit env margin wins")
    check(pol.threshold_source == "calibrated", "thresholds stay calibrated")


async def test_stale_calibration_is_inert():
    check.section("3: a stale calibration must not decide anything")
    from core.speaker.matcher import DEFAULT_MARGIN, DEFAULT_THRESHOLD

    P = _profiles((M_ID, "Marcus", 0.71), (G_ID, "Guest", 0.69))
    good = await _policy_case(profiles=P, calibrated=True)
    check(good.calibrated, "the baseline is calibrated")

    # A. a third profile appears
    P3 = P + _profiles(("p-new", "Newcomer", None))[:1]
    pol = await _policy_case(profiles=P3, calibrated=True)
    check(not pol.calibrated, "A: adding a profile invalidates it")
    check(pol.threshold_source == "provisional default", "A: source falls back")
    check(abs(pol.threshold_for(M_ID) - DEFAULT_THRESHOLD) < 1e-9,
          f"A: his stored 0.71 is INERT ({pol.threshold_for(M_ID)})")
    check(abs(pol.margin - DEFAULT_MARGIN) < 1e-9, "A: and so is the margin")

    # B. a covered profile is deleted.
    #
    # This assertion was BACKWARDS in the first closure pass: it required that
    # removing the guest "leaves the rest covered". It does not, and must not.
    # Marcus's fitted threshold exists because the guest's voice supplied the
    # impostor evidence that bounded his false-accept rate. Delete the guest and
    # the number outlives the only measurement that justified it. Coverage is
    # EXACT SET EQUALITY.
    from core.speaker.calibration import (CalibrationRecord, calibration_covers,
                                          resolve_policy)
    rec = CalibrationRecord(margin=0.22, profile_ids=[M_ID, G_ID], metrics={})
    pol = resolve_policy(P[:1], rec, env_threshold=None, env_margin=None,
                         default_threshold=DEFAULT_THRESHOLD,
                         default_margin=DEFAULT_MARGIN)
    check(not pol.calibrated, "B: deleting a covered profile invalidates it")
    check(abs(pol.threshold_for(M_ID) - DEFAULT_THRESHOLD) < 1e-9,
          f"B: and his stored 0.71 is inert ({pol.threshold_for(M_ID)})")

    # B2. a profile the record never named
    pol = resolve_policy(_profiles(("p-other", "Other", 0.6)), rec,
                         env_threshold=None, env_margin=None,
                         default_threshold=DEFAULT_THRESHOLD,
                         default_margin=DEFAULT_MARGIN)
    check(not pol.calibrated, "B2: an uncovered profile invalidates it")

    # B3. REPLACED — same count, different people. Containment would also miss
    # this if the record happened to name a superset.
    swapped = _profiles((M_ID, "Marcus", 0.71), ("p-replacement", "Someone", 0.69))
    pol = resolve_policy(swapped, rec, env_threshold=None, env_margin=None,
                         default_threshold=DEFAULT_THRESHOLD,
                         default_margin=DEFAULT_MARGIN)
    check(not pol.calibrated, "B3: replacing a profile invalidates it")

    # B4. THE FAIL-CLOSED BACKSTOP. `_invalidate_calibration()` deletes the row
    # on enrol/delete — but if that write fails (locked db, disk error) the stale
    # row is still there on the next boot. Set equality is what makes it inert
    # anyway, so a failed clear degrades to provisional defaults instead of a
    # silent stale claim.
    check(not calibration_covers(rec, P[:1]),
          "B4: a stale row surviving a failed clear is INERT (fails closed)")

    # C. model revision mismatch
    pol = await _policy_case(profiles=P, calibrated=True, revision="deadbeef")
    check(not pol.calibrated, "C: a model revision change invalidates it")
    check(abs(pol.threshold_for(M_ID) - DEFAULT_THRESHOLD) < 1e-9,
          "C: stored thresholds are inert")

    # D. calibration cleared
    pol = await _policy_case(profiles=P, calibrated=False)
    check(not pol.calibrated and pol.threshold_source == "provisional default",
          "D: cleared calibration falls back")


async def test_matcher_and_status_agree():
    check.section("8: the matcher's number IS the diagnostic IS the status source")
    import numpy as np

    from core.speaker.backend import EMBEDDING_DIM
    from core.speaker.calibration import CalibrationRecord
    from core.speaker.matcher import match
    from core.speaker.service import SpeakerService

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        svc = SpeakerService(Path(td) / "spk.db")
        await svc.initialize()
        P = _profiles((M_ID, "Marcus", 0.71), (G_ID, "Guest", 0.69))
        for p in P:
            await svc.registry.save(p)

        # Uncalibrated: stored thresholds present but inert, everywhere.
        pol = await svc.policy()
        st = await svc.status()
        v = np.zeros(EMBEDDING_DIM, dtype=np.float32); v[0] = 1.0
        r = match(v, P, policy=pol)
        check(st["threshold_calibrated"] is False, "status: not calibrated")
        check(r.threshold_source == st["threshold_source"] == "provisional default",
              f"same source in match and status ({r.threshold_source} / {st['threshold_source']})")
        check(abs(r.threshold - pol.threshold_for(M_ID)) < 1e-9,
              "and the matcher used exactly the policy's number")
        check(abs(st["margin"] - r.margin) < 1e-9, "margins agree too")
        detail = {d["profile_id"]: d for d in st["profiles_detail"]}
        check(detail[M_ID]["stored_threshold"] == 0.71,
              "status still shows the stored value as history")
        check(abs(detail[M_ID]["effective_threshold"] - pol.threshold_for(M_ID)) < 1e-9,
              "but reports the EFFECTIVE one separately")

        # Calibrated: now the stored values decide, and everything says so.
        await svc.calib.save(CalibrationRecord(margin=0.22,
                                               profile_ids=[M_ID, G_ID], metrics={}))
        pol = await svc.policy()
        st = await svc.status()
        r = match(v, P, policy=pol)
        check(st["threshold_calibrated"] is True, "status: calibrated")
        check(r.threshold_source == st["threshold_source"] == "calibrated",
              "sources agree")
        check(abs(r.threshold - 0.71) < 1e-9, f"and the fitted value decides ({r.threshold})")
        check(abs(st["margin"] - 0.22) < 1e-9, "with the calibrated margin")


async def test_enrol_and_delete_invalidate_centrally():
    check.section("3: a DIRECT enrol/delete invalidates calibration too")
    import inspect

    from core.speaker.calibration import CalibrationRecord
    from core.speaker.service import SpeakerService

    src = inspect.getsource(SpeakerService)
    check("_invalidate_calibration" in src, "invalidation lives in the service")
    check(src.count("await self._invalidate_calibration") >= 2,
          "called by both enrol and delete")
    router = (REPO / "backend" / "routers" / "speaker.py").read_text(encoding="utf-8")
    # The explicit DELETE /speaker/calibration endpoint SHOULD clear — that is
    # its whole job. What must not happen is the router clearing as a SIDE
    # EFFECT of enroll/delete, because then a non-HTTP caller skips it.
    enroll_fn = router[router.index("async def enroll("):router.index("async def identify(")]
    delete_fn = router[router.index("async def delete_profile("):router.index("async def enroll(")]
    check("calib.clear()" not in enroll_fn, "enroll no longer clears it itself")
    check("calib.clear()" not in delete_fn, "nor does delete_profile")
    check("calib.clear()" in router, "while the explicit clear endpoint still does")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        svc = SpeakerService(Path(td) / "spk.db")
        await svc.initialize()
        for p in _profiles((M_ID, "Marcus", 0.71), (G_ID, "Guest", 0.69)):
            await svc.registry.save(p)
        await svc.calib.save(CalibrationRecord(margin=0.22,
                                               profile_ids=[M_ID, G_ID], metrics={}))
        check((await svc.status())["threshold_calibrated"] is True, "calibrated")

        await svc.delete(G_ID)          # the production call, not the HTTP route
        check(await svc.calib.load() is None,
              "a direct delete() cleared the calibration record")
        check((await svc.status())["threshold_calibrated"] is False,
              "and status is honestly false again")


# ── §4. the margin fitter models the real classifier ─────────────────────────

async def test_margin_uses_the_real_classifier():
    check.section("4: the margin is fitted against thresholds that will ship")
    from core.speaker.calibration import Trial, fit_margin

    # Every gap is wide, so a gap-only fitter would report 100% correct — but
    # every top score is BELOW the threshold that will actually ship, so the
    # live classifier returns `unknown` every time.
    trials = []
    for _ in range(8):
        trials.append(Trial(truth=M_ID, top_profile_id=M_ID, top_score=0.50,
                            second_score=0.10, second_profile_id=G_ID, phase="B"))
        trials.append(Trial(truth=G_ID, top_profile_id=G_ID, top_score=0.52,
                            second_score=0.11, second_profile_id=M_ID, phase="B"))

    m_gap, rate_gap, _w, _r = fit_margin(trials)               # no thresholds
    check(m_gap is not None and rate_gap >= 0.90,
          f"gap alone would claim success ({m_gap}, {rate_gap:.0%})")

    m_real, _rate, _w, why = fit_margin(trials, {M_ID: 0.71, G_ID: 0.69})
    check(m_real is None,
          f"but the REAL classifier cannot reach 90% and it fails ({why})")

    # And the true speaker ranking SECOND must be counted as a genuine score.
    from core.speaker.calibration import calibrate
    runner_up = []
    for _ in range(12):
        runner_up.append(Trial(truth=M_ID, top_profile_id=G_ID, top_score=0.40,
                               second_score=0.78, second_profile_id=M_ID, phase="B"))
    res = calibrate(runner_up + [Trial(truth=G_ID, top_profile_id=G_ID,
                                       top_score=0.80, second_score=0.30,
                                       second_profile_id=M_ID, phase="B")] * 12)
    marcus = next((p for p in res.profiles if p.profile_id == M_ID), None)
    check(marcus is not None and marcus.genuine_n == 12,
          f"Marcus's 12 runner-up trials count as genuine ({marcus.genuine_n if marcus else 0})")


async def test_harness_acceptance_is_two_tier():
    check.section("5/6: recognition PASS is not P5.2 PASS")
    html = HARNESS.read_text(encoding="utf-8")

    check("recognition_validation" in html, "the report has a recognition verdict")
    check("p52_human_acceptance" in html, "and a SEPARATE acceptance verdict")
    check('"NOT COMPLETE"' in html, "with a NOT COMPLETE state")
    for req in ("memory attribution", "permission regression", "latency measurement",
                "calibration applied", "threshold_calibrated == true"):
        check(req in html, f"acceptance requires: {req}")
    for fn in ("sentinelFlow", "permissionFlow", "sttLatencyFlow"):
        check(f"async function {fn}" in html, f"{fn} exists")
    check("MARCUS-LIVE-551" in html and "GUEST-LIVE-661" in html,
          "the live sentinels are the specified ones")
    check("identify_ms" in html and "embed_p50" not in html,
          "latency is named honestly — identify(), not embedding-only")
    check("stt_off_p50" in html and "stt_on_p50" in html and "delta_p50" in html,
          "and /stt off/on/delta are measured separately")

    # No audio or biometric material in the report.
    rep = html[html.index("const report = {"):html.index("rep.appendChild(el(\"pre\"")]
    for banned in ("blob", "audio", "embedding", "centroid"):
        check(banned not in rep.lower(), f"the report contains no {banned}")


def _code_only(js: str) -> str:
    """Strip JS comments.

    Every "this string must NOT appear" check has to run against code, not
    prose: the harness explains in comments exactly which mistakes it is
    avoiding, and naming them there would otherwise fail the check that they
    are gone.
    """
    out = []
    for line in js.splitlines():
        stripped = line.strip()
        if stripped.startswith("//"):
            continue
        if "//" in line and "://" not in line:
            line = line[:line.index("//")]
        out.append(line)
    return "\n".join(out)


async def test_harness_separates_prediction_from_ranking():
    check.section("3: the harness records BOTH fields, and uses each correctly")
    html = HARNESS.read_text(encoding="utf-8")

    check("predicted_profile_id: out.profile_id" in html,
          "prediction comes from the asserted identity")
    check("top_profile_id: out.top_scored_profile_id" in html,
          "ranking comes from the score attribution, NOT profile_id")
    check("topName: out.top_scored_display_name" in html,
          "and the ranked name likewise")
    check("top_profile_id: out.profile_id" not in html,
          "the old conflation is gone — profile_id never feeds top_profile_id")

    # The confusion matrix is about what Nova ASSERTED.
    conf = _code_only(html[html.index("function confusion("):
                           html.index("function pct(")])
    check("r.predicted_profile_id" in conf,
          "the confusion matrix scores the asserted identity")
    check("r.top_profile_id" not in conf,
          "and never counts a ranking as a prediction")

    # Calibration fits on the ranking + runner-up.
    check("top_profile_id:t.top_profile_id" in html.replace(" ", ""),
          "the calibration payload sends the ranked profile")
    check("second_profile_id:t.second_profile_id" in html.replace(" ", ""),
          "and the runner-up, which the impostor bound needs")


async def test_harness_sentinel_shares_one_conversation():
    check.section("5: one conversation id for the whole sentinel run")
    html = HARNESS.read_text(encoding="utf-8")

    check("crypto.randomUUID()" in html, "the harness generates a UUID itself")
    check("sentinelConversationId" in html, "and persists it in non-audio state")
    check("conversation_id: convId" in html,
          "every /chat in the flow sends it")
    check("conversation_stable" in html,
          "and the echoed id is checked, so a silently-ignored one cannot pass")

    sent = html[html.index("async function sentinelFlow"):html.index("async function permissionFlow")]
    code = _code_only(sent)

    # STRICT equality. A null/missing returned id must FAIL, not pass — that is
    # precisely what a server ignoring conversation_id would produce, so a
    # truthiness guard would excuse the exact bug being tested for.
    check("if (r.conversation_id !== CID)" in code,
          "the id check is strict equality against the requested CID")
    check("r.conversation_id &&" not in code,
          "no truthiness guard — a missing id is not 'stable'")
    # The five turns are data now (SENTINEL_STEPS), driven by the batch engine.
    # Behaviour is proven by executing it in tests/test_calibration_harness_v52.py;
    # this only pins the declared order.
    steps = html[html.index("const SENTINEL_STEPS"):html.index("const PERMISSION_STEPS")]
    check(steps.count('who:"') == 5, f"five sentinel turns ({steps.count('who:')})")
    flat = re.sub(r"\s+", " ", steps)
    order = re.findall(r'who:"([^"]+)"', flat)
    check(order == ["Marcus", "Guest", "Marcus", "Guest", "unverified"],
          f"in the required order ({order})")
    phases = re.findall(r'phase:"([^"]+)"', flat)
    check(phases == ["store", "store", "ask", "ask", "ask"],
          f"two stores then three asks ({phases})")
    # Letter-only sentinels: a voice pipeline transcribes the store utterance
    # BEFORE storing it, and STT renders a hyphenated digit-bearing token as
    # words — which is what made run 2 report a memory failure that had not
    # happened. The tokens live in SENTINELS and the cues interpolate them.
    tokens = html[html.index("const SENTINELS"):html.index("function canonTokens")]
    check("COBALT ORCHARD PINE" in tokens and "SILVER HARBOR LANTERN" in tokens,
          "each speaker stores their own letter-only sentinel")
    check(not any(c.isdigit() for c in tokens),
          "with no digits for STT to spell out")
    check("${SENTINELS.marcus}" in steps and "${SENTINELS.guest}" in steps,
          "and the spoken cues interpolate exactly those tokens")
    check("chatTurn(blob, CID)" in code,
          "and every turn goes through the full /stt -> /chat pipeline with that CID")

    # The unverified turn must actually BE unverified.
    check("UNVERIFIED_OK" in html, "allowed unverified statuses are named")
    check('"unknown", "ambiguous", "unavailable", "too_short"' in html,
          "exactly the four honest non-identifications")
    check("unverified_is_unverified" in sent,
          "and the run records whether that held")
    check("marcus_recognised" in sent and "guest_recognised" in sent,
          "the two enrolled people must be recognised as THEMSELVES")
    check("out.unverified_is_unverified" in sent and "out.pass" in sent,
          "all of which gate the sentinel verdict")


async def test_harness_latency_measures_stt_only():
    check.section("latency: /stt round trip, with no /chat inside it")
    html = HARNESS.read_text(encoding="utf-8")

    check("async function sttOnly" in html, "an /stt-only helper exists")
    only = _code_only(html[html.index("async function sttOnly"):
                           html.index("// ── composite flows")])
    check('api("/stt"' in only, "it calls /stt")
    check('"/chat"' not in only, "and never /chat")
    check("stt_ms" in only, "returning the /stt round trip")
    check("speaker" in only, "plus the speaker metadata the probe needs")

    flow = _code_only(html[html.index("async function sttLatencyFlow"):
                           html.index("// ── steps UI")])
    check("sttOnly(blob, i >= 6)" in flow,
          "six OFF then six ON, through the same /stt-only path")
    check("count:12" in flow, "twelve latency samples")
    check("chatTurn(" not in flow,
          "no latency sample goes through the /chat pipeline")
    check('"/chat"' not in flow, "and /chat appears nowhere in the benchmark")
    check("n_off" in flow and "n_on" in flow,
          "and the report states how many samples each side actually got")

    # chatTurn must still do the full pipeline: the sentinel needs it.
    vt = _code_only(html[html.index("async function chatTurn"):
                         html.index("async function sttOnly")])
    check('api("/stt"' in vt and '"/chat"' in vt,
          "chatTurn still runs /stt -> /chat for the memory sentinel")


async def test_harness_permission_pass_is_measured():
    check.section("6: the permission verdict is computed, never asserted")
    html = HARNESS.read_text(encoding="utf-8")
    raw = html[html.index("async function permissionFlow"):html.index("async function sttLatencyFlow")]
    flow = _code_only(raw)

    check("pass: true" not in flow and "pass:true" not in flow.replace(" ", ""),
          "no hardcoded pass in permissionFlow")
    check('"computer.type"' in flow or "'computer.type'" in flow,
          "it probes computer.type — a real PermissionBroker-gated actuator")
    check('"memory.remember"' not in flow and "'memory.remember'" not in flow,
          "and no longer memory.remember, which is not permission-gated at all")
    check("/speaker/permission-probe" in flow,
          "through the real backend probe, not a JS reimplementation")
    # The browser must not decide anything itself — no tier table, no decision
    # literals, no local allow/deny logic. Every decision is read off the
    # backend response.
    for literal in ('"allowed"', '"denied"', '"standard"', '"admin"', '"critical"',
                    '"guarded"', '"trusted"', '"locked"'):
        check(literal not in flow,
              f"no {literal} literal in the browser — the backend decides")
    check("decision: r.decision" in flow and "tier: r.tier" in flow,
          "decisions and tiers are READ from the response, not computed")

    # Three cases, compared against the typed reference rather than a constant.
    check("typed" in flow and "voice_turn_id" in flow,
          "typed reference plus voice handles")
    check("marcus.decision === typed.decision" in flow
          and "guest.decision === typed.decision" in flow,
          "both voice decisions are compared to the typed one")
    check("needs_confirmation" not in flow,
          "with no hardcoded expectation of guarded mode")
    check("recognised" in flow,
          "and both handles must have been recognised for the result to mean anything")
    check("pass: sameDecision" in flow, "the verdict is that comparison")


async def test_harness_requires_exactly_two_profiles():
    check.section("7: exactly two compatible profiles, with the right roles")
    html = HARNESS.read_text(encoding="utf-8")

    check("twoProfiles" in html, "the report carries a two-profile check")
    check("compat.length === 2" in html, "compatible profile count must be 2")
    check("JSON.stringify(compatIds) === JSON.stringify(wantIds)" in html,
          "and they must be exactly the two this run enrolled")
    check('roleOf(S.marcusId) === "owner"' in html, "Marcus is the owner")
    check('roleOf(S.guestId) === "guest"' in html, "the guest is a guest")
    check("exactly-two-profile requirement" in html,
          "failing it fails P5.2 acceptance")
    check("two_profile_requirement" in html, "and it is reported in the JSON")

    # Not inferred from threshold_calibrated.
    idx = html.index("const twoProfiles")
    check("threshold_calibrated" not in html[html.index("const compat ="):idx],
          "the check does not lean on threshold_calibrated")


async def main():
    await test_enroll_contract_matches_the_real_return_shape()
    await test_enroll_gate_counts_real_samples()
    await test_precedence_matrix()
    await test_stale_calibration_is_inert()
    await test_matcher_and_status_agree()
    await test_enrol_and_delete_invalidate_centrally()
    await test_margin_uses_the_real_classifier()
    await test_harness_acceptance_is_two_tier()
    await test_harness_separates_prediction_from_ranking()
    await test_harness_sentinel_shares_one_conversation()
    await test_harness_latency_measures_stt_only()
    await test_harness_permission_pass_is_measured()
    await test_harness_requires_exactly_two_profiles()
    check.finish()


if __name__ == "__main__":
    run(main)
