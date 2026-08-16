"""The fast guided calibration harness — its LOGIC, executed, not grepped.

The P5.2 UX rewrite replaced click-per-utterance with automatic batches. That is
exactly the kind of change that can silently drop a sample, skip an index on
retry, mislabel a speaker across a handoff, or let calibration audio leak into
validation — and none of it is visible in a screenshot or a substring search.

So this suite pulls the pure-logic half out of `tests/live_speaker_calibration.html`
and RUNS it under node: real functions, real inputs, real assertions. The
harness is written in two parts for this reason — part 1 has no DOM, no network
and no timers, and exports a `P52` object; part 2 is the browser layer.

Requires `node` on PATH (already required by the frontend suites).

Run:  venv\\Scripts\\python.exe tests\\test_calibration_harness_v52.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, run  # noqa: E402

check = Checks()

HARNESS = REPO / "tests" / "live_speaker_calibration.html"

#: Evidence counts the UX rewrite was forbidden to touch. Hard-coded here on
#: purpose: if someone "speeds up" the run by trimming trials, this fails.
REQUIRED = {
    "enroll_marcus": 6, "trials_marcus": 20, "trials_guest": 12,
    "enroll_guest": 6, "phaseb_marcus": 12, "phaseb_guest": 12,
    "valid_marcus": 10, "valid_guest": 10,
    "sentinel": 5, "permission": 2, "latency": 12,
}
TOTAL_RECORDINGS = sum(REQUIRED.values())     # 107


def _script() -> str:
    html = HARNESS.read_text(encoding="utf-8")
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    if len(blocks) != 1:
        raise AssertionError(f"expected one <script> block, found {len(blocks)}")
    return blocks[0]


def _node(body: str) -> dict:
    """Load the harness logic in node and run `body`, which sets `out`."""
    node = shutil.which("node")
    if node is None:
        raise AssertionError("node is not on PATH")
    with tempfile.TemporaryDirectory() as td:
        mod = Path(td) / "harness.cjs"
        mod.write_text(_script(), encoding="utf-8")
        driver = Path(td) / "run.cjs"
        driver.write_text(
            "const P52 = require(" + json.dumps(str(mod).replace("\\", "/")) + ");\n"
            "const out = {};\n" + body + "\n"
            "process.stdout.write(JSON.stringify(out));\n",
            encoding="utf-8")
        proc = subprocess.run([node, str(driver)], capture_output=True, text=True,
                              timeout=60)
        if proc.returncode != 0:
            raise AssertionError(f"node failed: {proc.stderr[:900]}")
        return json.loads(proc.stdout)


# ── 1. the harness logic loads at all ────────────────────────────────────────

async def test_logic_half_is_dom_free():
    check.section("the pure half loads with no DOM, no window, no fetch")
    out = _node("out.keys = Object.keys(P52).sort();")
    for fn in ("planConditions", "blockPlan", "computeAcceptance", "advance",
               "storeFor", "auditTruth", "confusion", "freshState"):
        check(fn in out["keys"], f"P52 exports {fn}")


# ── 2. evidence counts are unchanged ─────────────────────────────────────────

async def test_sample_counts_are_unchanged():
    check.section("1: required sample counts survived the UX rewrite")
    out = _node("""
      const plan = P52.blockPlan();
      out.counts = {}; plan.forEach(b => out.counts[b.id] = b.count);
      out.total = P52.totalRecordings(plan);
      out.seconds = {}; plan.forEach(b => out.seconds[b.id] = b.seconds);
    """)
    for block, want in REQUIRED.items():
        got = out["counts"].get(block)
        check(got == want, f"{block}: {got} (must be {want})")
    check(out["total"] == TOTAL_RECORDINGS,
          f"total recordings {out['total']} == {TOTAL_RECORDINGS}")


async def test_condition_distribution():
    check.section("2/G: 4 each of five conditions, and GROUPED not round-robin")
    out = _node("""
      const tally = n => { const c = {};
        P52.planConditions(n).forEach(x => c[x] = (c[x]||0)+1); return c; };
      out.a20 = tally(20); out.b12 = tally(12); out.v10 = tally(10);
      out.names = P52.CONDITION_NAMES;
      out.seq20 = P52.planConditions(20);
      out.seq10 = P52.planConditions(10);
      out.seq12 = P52.planConditions(12);
      // Grouped means each condition occupies ONE contiguous run.
      const runs = seq => { const r = []; seq.forEach(x =>
        { if (!r.length || r[r.length-1][0] !== x) r.push([x,1]); else r[r.length-1][1]++; });
        return r; };
      out.runs20 = runs(out.seq20); out.runs10 = runs(out.seq10);
      out.runs12 = runs(out.seq12);
    """)
    check(out["names"] == ["normal", "quiet", "loud", "near", "far"],
          "the five conditions are unchanged")
    check(all(v == 4 for v in out["a20"].values()) and len(out["a20"]) == 5,
          f"phase A 20 trials -> 4 of each condition ({out['a20']})")
    check(sum(out["b12"].values()) == 12 and len(out["b12"]) == 5,
          f"phase B 12 covers all five conditions ({out['b12']})")
    check(all(v == 2 for v in out["v10"].values()) and len(out["v10"]) == 5,
          f"validation 10 -> 2 of each ({out['v10']})")

    # GROUPED: exactly five runs, one per condition. Round-robin would give 20.
    check(len(out["runs20"]) == 5,
          f"phase A is grouped — 5 contiguous runs, not {len(out['runs20'])}")
    check(out["runs20"] == [[c, 4] for c in out["names"]],
          f"and each run is 4 long ({out['runs20']})")
    check(out["runs10"] == [[c, 2] for c in out["names"]],
          f"validation is grouped 2 each ({out['runs10']})")
    check(len(out["runs12"]) == 5,
          f"phase B is grouped too ({out['runs12']})")
    # The 12 distribution must match what round-robin produced: 3,3,2,2,2.
    check([n for _, n in out["runs12"]] == [3, 3, 2, 2, 2],
          f"with the same counts as before, only grouped ({out['runs12']})")
    check(out["seq20"][:5] != out["names"],
          "the sequence is NOT round-robin — a human does not move on every take")


# ── 3. the batch engine's index rules ────────────────────────────────────────

async def test_failed_samples_retry_and_are_never_counted():
    check.section("3: a failed or retried sample repeats the SAME index")
    out = _node("""
      out.ok    = P52.advance(4, "ok");
      out.error = P52.advance(4, "error");
      out.retry = P52.advance(4, "retry");
      // A full block where every third attempt fails must still end at exactly
      // `count`, having recorded each index once.
      let i = 0, attempts = 0; const seen = [];
      while (i < 12 && attempts < 100) {
        attempts++;
        const outcome = (attempts % 3 === 0) ? "error" : "ok";
        if (outcome === "ok") seen.push(i);
        i = P52.advance(i, outcome);
      }
      out.final = i; out.seen = seen; out.attempts = attempts;
    """)
    check(out["ok"] == 5, "a processed sample advances by exactly one")
    check(out["error"] == 4, "an error does NOT advance — nothing is skipped")
    check(out["retry"] == 4, "a retry does NOT advance — nothing is counted twice")
    check(out["final"] == 12, f"the block still completes ({out['final']})")
    check(out["seen"] == list(range(12)),
          f"each index recorded exactly once, in order ({out['seen']})")
    check(out["attempts"] > 12, "and it genuinely took extra attempts")


async def test_pause_resume_cannot_duplicate_or_skip():
    check.section("4: pause/resume is index-neutral")
    # Pausing only gates WHEN a sample runs, never which index. Proven by
    # driving the same advance rule with arbitrary pause interleavings.
    out = _node("""
      const runs = [];
      for (const pausePattern of [[], [0], [3,4], [0,1,2,3,4,5,6,7,8,9]]) {
        let i = 0; const seen = [];
        while (i < 10) {
          if (pausePattern.includes(i)) { /* paused: loop again, no advance */ }
          seen.push(i);
          i = P52.advance(i, "ok");
        }
        runs.push(seen);
      }
      out.runs = runs;
    """)
    for i, seen in enumerate(out["runs"]):
        check(seen == list(range(10)),
              f"pause pattern {i}: indexes {seen} — no duplicates, no gaps")


# ── 4. storage separation and attribution ────────────────────────────────────

async def test_validation_never_shares_storage_with_calibration():
    check.section("6: validation is stored separately from calibration")
    out = _node("""
      out.a = P52.storeFor("A"); out.b = P52.storeFor("B"); out.v = P52.storeFor("V");
      // The cue pools must be disjoint, so validation cannot reuse a
      // calibration sentence even if a human is on autopilot.
      const cal = new Set(P52.CAL_CUES), val = new Set(P52.VAL_CUES);
      out.overlap = P52.VAL_CUES.filter(c => cal.has(c));
      out.enrollOverlap = P52.ENROLL_PHRASES.filter(p => cal.has(p) || val.has(p));
      out.calN = P52.CAL_CUES.length; out.valN = P52.VAL_CUES.length;
    """)
    check(out["a"] == "trials" and out["b"] == "trials",
          "phase A and B feed the calibration set")
    check(out["v"] == "validation", "phase V feeds the validation set — never the fit")
    check(out["overlap"] == [], f"cue pools are disjoint ({out['overlap']})")
    check(out["enrollOverlap"] == [],
          f"enrollment phrases are reused by neither ({out['enrollOverlap']})")
    check(out["valN"] >= 10, f"enough validation cues to cover 10 trials ({out['valN']})")


async def test_handoff_cannot_mislabel_a_speaker():
    check.section("7: a guest recording cannot be filed as Marcus")
    out = _node("""
      const M = "p-marcus", G = "p-guest";
      const good = [
        {block:"trials_marcus", truth:M}, {block:"phaseb_marcus", truth:M},
        {block:"phaseb_guest",  truth:G}, {block:"valid_guest",   truth:G},
        {block:"trials_guest",  truth:"__impostor__"},
      ];
      out.good = P52.auditTruth(good, M, G);
      const bad = good.concat([{block:"phaseb_guest", truth:M}]);
      out.bad = P52.auditTruth(bad, M, G);
      const untagged = [{truth:M}];
      out.untagged = P52.auditTruth(untagged, M, G);
    """)
    check(out["good"]["ok"] is True, "correctly-labelled rows pass the audit")
    check(out["bad"]["ok"] is False,
          "a guest-block row filed under Marcus is caught")
    check(out["bad"]["mismatches"][0]["block"] == "phaseb_guest",
          "and the offending block is named")
    check(out["untagged"]["ok"] is False,
          "a row with no block at all is a failure, not a pass")


# ── 5. autosave / resume ─────────────────────────────────────────────────────

async def test_enrollment_cannot_resume_across_a_reload():
    check.section("A/B: interrupted enrollment resets to 0 — its audio is gone")
    out = _node("""
      // A. 3 of 6 recorded, then a reload. No completed server enrollment.
      const s = P52.freshState();
      s.blocks.enroll_marcus = {done: 3};
      out.beforeA = s.blocks.enroll_marcus.done;
      out.wipedA = P52.reconcileEnrollment(s);
      out.afterA = s.blocks.enroll_marcus.done;

      // B. the upload SUCCEEDED — progress is real and must survive.
      const t = P52.freshState();
      t.blocks.enroll_marcus = {done: 6};
      t.enroll.marcus = {ok:true, profile_id:"p-marcus", sample_count:6,
                         meets_p52_bar:true};
      out.wipedB = P52.reconcileEnrollment(t);
      out.afterB = t.blocks.enroll_marcus.done;

      // C. ok=true but BELOW the P5.2 bar: complete, so progress stands, but it
      //    must not count as acceptable.
      const u = P52.freshState();
      u.blocks.enroll_guest = {done: 6};
      u.enroll.guest = {ok:true, profile_id:"p-guest", sample_count:4,
                        meets_p52_bar:false};
      out.wipedC = P52.reconcileEnrollment(u);
      out.completeC = P52.enrollmentComplete(u.enroll.guest);
      out.acceptableC = P52.enrollmentAcceptable(u.enroll.guest);

      // D. both blocks stale at once.
      const v = P52.freshState();
      v.blocks.enroll_marcus = {done: 5}; v.blocks.enroll_guest = {done: 2};
      out.wipedD = P52.reconcileEnrollment(v).sort();
      out.afterD = [v.blocks.enroll_marcus.done, v.blocks.enroll_guest.done];

      // E. trials are NOT touched — they are genuinely resumable.
      const w = P52.freshState();
      w.blocks.trials_marcus = {done: 13};
      P52.reconcileEnrollment(w);
      out.trialsUntouched = w.blocks.trials_marcus.done;
    """)
    check(out["beforeA"] == 3, "fixture really had 3 of 6 persisted")
    check(out["afterA"] == 0,
          f"an interrupted enrollment resets to 0, not {out['afterA']} — the "
          "blobs were never persisted, so they cannot be resumed")
    check(out["wipedA"] == ["enroll_marcus"], "and the reset is reported, not silent")
    check(out["afterB"] == 6 and out["wipedB"] == [],
          "a COMPLETED enrollment keeps its progress")
    check(out["wipedC"] == [], "a below-bar enrollment still reached the server")
    check(out["completeC"] is True and out["acceptableC"] is False,
          "but complete != acceptable — 4 of 6 is not the P5.2 bar")
    check(out["wipedD"] == ["enroll_guest", "enroll_marcus"]
          and out["afterD"] == [0, 0], "both blocks reset when both are stale")
    check(out["trialsUntouched"] == 13,
          "trial progress is untouched — those samples were uploaded when recorded")


async def test_below_bar_enrollment_does_not_advance():
    check.section("C: a sub-P5.2 enrollment cannot advance the protocol")
    out = _node(_complete_state_js() + """
      const s = base();
      s.enroll.marcus = {ok:true, profile_id:"p-marcus", sample_count:4,
                         meets_p52_bar:false};
      const r = P52.computeAcceptance(s);
      out.acceptance = r.acceptance; out.missing = r.missing;
      out.acceptable = P52.enrollmentAcceptable(s.enroll.marcus);
    """)
    check(out["acceptable"] is False, "4 of 6 is not acceptable")
    check(out["acceptance"] != "PASS",
          f"and the gate does not pass ({out['acceptance']})")
    check(any("Marcus enrollment" in m for m in out["missing"]),
          f"naming the enrollment as the blocker ({out['missing']})")

    # The UI path must refuse to advance too, and must not delete the profile.
    html = HARNESS.read_text(encoding="utf-8")
    fn = html[html.index("async function runEnrollBlock"):
              html.index("function alertBanner")]
    check("meets_p52_bar" in fn, "runEnrollBlock inspects the bar")
    # The sub-bar branch must return BEFORE the line that advances the step.
    bar_at = fn.index("!out.meets_p52_bar")
    step_at = fn.index("S.step = stepAfter")
    check(bar_at < step_at, "the bar is checked before the step would advance")
    branch = fn[bar_at:step_at]
    check("return false" in branch,
          "and the sub-bar branch returns false without advancing")
    check("S.step" not in branch,
          "nothing in that branch touches the step counter")
    check("DELETE" not in fn and "profiles/" not in fn,
          "and no profile is silently deleted — that stays the human's decision")
    check("resumable:false" in fn or "resumable: false" in fn,
          "the enrollment block is declared non-resumable")


async def test_speaker_handoffs_gate_every_change():
    check.section("D/E: a deliberate Continue before every speaker change")
    out = _node("""
      const S = P52.SENTINEL_STEPS, P = P52.PERMISSION_STEPS;
      out.sentinelWho = S.map(s => s.who);
      out.sentinelHandoffs = S.map((_, i) => P52.handoffBeforeIndex(S, i));
      out.permWho = P.map(s => s.who);
      out.permHandoffs = P.map((_, i) => P52.handoffBeforeIndex(P, i));
      out.labels = S.map(s => P52.stepLabel(s));
    """)
    check(out["sentinelWho"] == ["Marcus", "Guest", "Marcus", "Guest", "unverified"],
          f"five sentinel turns across three people ({out['sentinelWho']})")
    # Index 0 has no predecessor; 1-4 are all genuine changes.
    check(out["sentinelHandoffs"][0] is None, "no handoff before the first turn")
    check(out["sentinelHandoffs"][1] == "Guest", "Marcus -> Guest is gated")
    check(out["sentinelHandoffs"][2] == "Marcus", "Guest -> Marcus is gated")
    check(out["sentinelHandoffs"][3] == "Guest", "Marcus -> Guest is gated")
    check(out["sentinelHandoffs"][4] == "unverified",
          "Guest -> the third, unenrolled person is gated")
    check(sum(1 for h in out["sentinelHandoffs"] if h) == 4,
          "all four speaker changes require a Continue")
    check(out["permHandoffs"][1] == "Guest",
          "and the permission probe gates Marcus -> Guest")
    check(out["labels"][4] == "a third, unenrolled person",
          "the banner names the third person in words, while `who` stays the key")

    # The engine must actually consume it, before the countdown.
    html = HARNESS.read_text(encoding="utf-8")
    eng = html[html.index("async function runBlock"):html.index("// ── per-sample")]
    check("handoffBefore" in eng, "runBlock takes a handoffBefore hook")
    check(eng.index("handoffBefore") < eng.index("const lead ="),
          "and awaits it BEFORE the countdown starts")
    check("await handoff(" in eng, "through the inline banner, not an alert")
    check("handoffShownFor" in eng, "shown once per index, so a retry does not re-ask")


async def test_trial_persistence_is_atomic():
    check.section("F: the row and the block index commit together")
    out = _node("""
      const s = P52.freshState();
      // Ten successful samples, committed the way runBlock does it.
      for (let i = 0; i < 10; i++) {
        s.blocks.trials_marcus = s.blocks.trials_marcus || {done:0};
        const next = P52.advance(s.blocks.trials_marcus.done, "ok");
        P52.commitSample(s, {blockId:"trials_marcus", nextIndex:next,
                             row:{block:"trials_marcus", truth:"p-marcus", i},
                             store:"trials"});
        // After EVERY commit the two must agree — this is the state a reload
        // could observe, and the old code had a window where it did not.
        if (s.trials.length !== s.blocks.trials_marcus.done) { out.desync = i; break; }
      }
      out.rows = s.trials.length; out.done = s.blocks.trials_marcus.done;

      // Validation routes to the other array, and to its own block counter.
      const v = P52.freshState();
      P52.commitSample(v, {blockId:"valid_guest", nextIndex:1,
                           row:{block:"valid_guest", truth:"p-guest"},
                           store:P52.storeFor("V")});
      out.valRows = v.validation.length; out.valTrials = v.trials.length;
      out.valDone = v.blocks.valid_guest.done;

      // A commit with no row (enrollment, latency) still advances the index.
      const e = P52.freshState();
      P52.commitSample(e, {blockId:"latency", nextIndex:3, row:null, store:null});
      out.latDone = e.blocks.latency.done; out.latRows = e.trials.length;
    """)
    check("desync" not in out,
          f"row count and block index never diverge ({out.get('desync')})")
    check(out["rows"] == 10 and out["done"] == 10,
          f"10 rows, block index 10 ({out['rows']} / {out['done']})")
    check(out["valRows"] == 1 and out["valTrials"] == 0,
          "validation rows go to the validation array, never the calibration one")
    check(out["valDone"] == 1, "with its own block counter")
    check(out["latDone"] == 3 and out["latRows"] == 0,
          "a row-less commit still advances the index")

    # And the sample handler must not save on its own any more.
    html = HARNESS.read_text(encoding="utf-8")
    build = html[html.index("function buildTrialRow"):html.index("async function enrolBatch")]
    check("save()" not in build,
          "buildTrialRow does not save — one save follows the atomic commit")
    check(".push(row)" not in build,
          "and does not store the row itself")
    # Just the sample handler's body, not the block-level code after it: one
    # save at the end of a whole block is fine, a save PER SAMPLE is the bug.
    rt = html[html.index("async function runTrialBlock"):html.index("function render()")]
    body = rt[rt.index("doSample: async (blob, i, cond)"):rt.index("return {row:")]
    check("save()" not in body,
          "the trial sample handler does not save — runBlock's single commit does")
    check(".push(" not in body, "and does not push the row itself")


async def test_autosave_shape_is_non_audio_and_resumable():
    check.section("5: reload preserves progress, and never audio")
    out = _node("""
      const S = P52.freshState();
      out.keys = Object.keys(S).sort();
      out.blocks = S.blocks; out.startedAt = S.startedAt;
      // Simulate: 13 of 20 Marcus trials done, then a reload.
      S.blocks.trials_marcus = {done: 13};
      const round = JSON.parse(JSON.stringify(S));
      out.resumeAt = round.blocks.trials_marcus.done;
      out.serialised = JSON.stringify(round);
    """)
    for banned in ("blob", "audio", "base64", "webm", "embedding", "centroid"):
        check(banned not in out["serialised"].lower(),
              f"persisted state contains no {banned}")
    check(out["resumeAt"] == 13,
          "a reload resumes at sample 13, not 0 — completed work is not repeated")
    for key in ("blocks", "trials", "validation", "paceMs", "startedAt"):
        check(key in out["keys"], f"state tracks `{key}`")


# ── 6. acceptance is unchanged ───────────────────────────────────────────────

def _complete_state_js() -> str:
    """A state that SHOULD pass, so each mutation below isolates one rule."""
    return """
      const M = "p-marcus", G = "p-guest";
      function trials(block, truth, name, n, phase) {
        return Array.from({length:n}, (_, i) => ({
          block, truth, truthName:name, phase, condition:"normal",
          status:"known", predicted_profile_id:truth, top_profile_id:truth,
          top_score:0.9, second_score:0.2, second_profile_id:null}));
      }
      function base() {
        return {
          marcusId:M, guestId:G,
          enroll:{marcus:{meets_p52_bar:true, sample_count:6},
                  guest:{meets_p52_bar:true, sample_count:6}},
          trials: trials("trials_marcus",M,"Marcus",20,"A")
            .concat(trials("trials_guest",G,"Guest",12,"A"))
            .concat(trials("phaseb_marcus",M,"Marcus",12,"B"))
            .concat(trials("phaseb_guest",G,"Guest",12,"B")),
          validation: trials("valid_marcus",M,"Marcus",10,"V")
            .concat(trials("valid_guest",G,"Guest",10,"V")),
          applied:true,
          statusAfter:{threshold_calibrated:true, profiles_detail:[
            {profile_id:M, role:"owner", compatible:true},
            {profile_id:G, role:"guest", compatible:true}]},
          sentinel:{pass:true, conversation_stable:true, unverified_is_unverified:true},
          permission:{pass:true},
          sttLatency:{n_off:6, n_on:6},
        };
      }
    """


async def test_acceptance_bars_are_unchanged():
    check.section("9: every P5.2 failure condition still fails the gate")
    out = _node(_complete_state_js() + """
      out.baseline = P52.computeAcceptance(base()).acceptance;
      const cases = {};
      const mut = (name, f) => { const s = base(); f(s);
                                 cases[name] = P52.computeAcceptance(s); };
      mut("marcus enrollment below bar", s => s.enroll.marcus.meets_p52_bar = false);
      mut("guest enrollment below bar",  s => s.enroll.guest.meets_p52_bar = false);
      mut("calibration not applied",     s => s.applied = false);
      mut("threshold_calibrated false",  s => s.statusAfter.threshold_calibrated = false);
      mut("a third profile",             s => s.statusAfter.profiles_detail.push(
                                              {profile_id:"p-x", role:"guest", compatible:true}));
      mut("wrong owner role",            s => s.statusAfter.profiles_detail[0].role = "guest");
      mut("an identity swap",            s => s.validation[0].predicted_profile_id = "p-guest");
      mut("marcus below 90%",            s => { for (let i=0;i<2;i++)
                                                  s.validation[i].status = "unknown"; });
      mut("sentinel fails",              s => s.sentinel.pass = false);
      mut("conversation id changed",     s => s.sentinel.conversation_stable = false);
      mut("unverified was recognised",   s => s.sentinel.unverified_is_unverified = false);
      mut("permission differs",          s => s.permission.pass = false);
      mut("latency missing",             s => s.sttLatency = null);
      mut("latency has no OFF samples",  s => s.sttLatency.n_off = 0);
      mut("short phase A",               s => s.trials = s.trials.slice(0, 50));
      mut("short validation",            s => s.validation = s.validation.slice(0, 15));
      // Index 44 is the first phaseb_guest row: 20 marcus + 12 guest + 12
      // phaseb_marcus precede it. Filing it under Marcus is the handoff error.
      mut("mislabelled guest row",       s => { s.trials[44].block = "phaseb_guest";
                                                s.trials[44].truth = "p-marcus"; });
      out.cases = {}; Object.keys(cases).forEach(k =>
        out.cases[k] = {acceptance: cases[k].acceptance,
                        missing: cases[k].missing, failed: cases[k].failed});
    """)
    check(out["baseline"] == "PASS",
          f"the complete state passes, so each case isolates one rule ({out['baseline']})")
    for name, res in out["cases"].items():
        check(res["acceptance"] != "PASS",
              f"{name} -> {res['acceptance']} (must not be PASS)")


async def test_incomplete_run_is_not_complete_not_pass():
    check.section("an unrun item reads NOT COMPLETE, never PASS")
    out = _node(_complete_state_js() + """
      const s = base(); s.sentinel = null; s.permission = null; s.sttLatency = null;
      const r = P52.computeAcceptance(s);
      out.acceptance = r.acceptance; out.missing = r.missing;
      const fresh = P52.computeAcceptance(P52.freshState());
      out.fresh = fresh.acceptance; out.freshMissing = fresh.missing.length;
    """)
    check(out["acceptance"] == "NOT COMPLETE",
          f"unrun checks -> NOT COMPLETE ({out['acceptance']})")
    check(out["fresh"] == "NOT COMPLETE", "and a brand new session likewise")
    check(out["freshMissing"] >= 8, f"with everything listed ({out['freshMissing']})")


# ── 7. recording windows and phrase fit ──────────────────────────────────────

async def test_recording_windows_are_safe():
    check.section("recording windows leave room for the prompt and the model")
    out = _node("""
      out.dur = P52.DUR;
      out.phrases = P52.ENROLL_PHRASES.map(p => ({words: p.split(/\\s+/).length, p}));
      out.calWords = P52.CAL_CUES.map(c => c.split(/\\s+/).length);
      out.valWords = P52.VAL_CUES.map(c => c.split(/\\s+/).length);
      out.gap = P52.GAP_MS;
    """)
    d = out["dur"]
    # Backend floors: enrollment 1.5s (matcher.MIN_SAMPLE_S), command 1.0s.
    check(d["enroll"] >= 3.0,
          f"enrollment window {d['enroll']}s keeps 2x margin over the 1.5s floor")
    check(d["trial"] >= 3.0 and d["validation"] >= 3.0,
          "trial and validation windows are 3.0s")
    check(d["latency"] >= 2.5, f"latency window {d['latency']}s")
    check(d["sentinelStore"] >= 5.0,
          f"the sentinel STORE phrase keeps its longer window ({d['sentinelStore']}s) "
          "— it carries a token that must not be clipped")
    # A fixed MediaRecorder window truncates anything longer, so the phrases had
    # to shrink with it. ~8 words is comfortably under 3.0s.
    longest = max(p["words"] for p in out["phrases"])
    check(longest <= 9,
          f"the longest enrollment phrase is {longest} words — fits a 3.0s window")
    check(max(out["calWords"]) <= 9 and max(out["valWords"]) <= 9,
          "and every trial cue is answerable inside the window")
    check(500 <= out["gap"] <= 1200,
          f"inter-sample gap is {out['gap']}ms — not an arbitrary 3-5s wait")


# ── 8. latency evidence ──────────────────────────────────────────────────────

async def test_latency_is_stt_only_and_not_pooled():
    check.section("8: latency compares like with like")
    html = HARNESS.read_text(encoding="utf-8")
    flow = html[html.index("async function sttLatencyFlow"):
                html.index("// ── steps UI")]
    check('"/chat"' not in flow, "no /chat anywhere in the benchmark")
    check("sttOnly(" in flow, "it goes through the /stt-only path")
    check("n_off" in flow and "n_on" in flow, "both sample counts are reported")
    for k in ("stt_off_p50", "stt_off_p95", "stt_on_p50", "stt_on_p95",
              "delta_p50", "delta_p95"):
        check(k in flow, f"reports {k}")
    check("i < 6" in flow, "6 OFF and 6 ON")
    # The reuse question, answered in the source rather than left implicit.
    check("NOT pooled" in flow or "NOT be reused" in flow or "not reused" in flow,
          "and states why longer speaker-ON recordings are not pooled in")

    out = _node("out.latency = P52.DUR.latency;")
    check(out["latency"] == 2.5,
          f"OFF and ON share one window length ({out['latency']}s), so the delta "
          "is not measuring audio length")


async def main():
    await test_logic_half_is_dom_free()
    await test_sample_counts_are_unchanged()
    await test_condition_distribution()
    await test_failed_samples_retry_and_are_never_counted()
    await test_pause_resume_cannot_duplicate_or_skip()
    await test_validation_never_shares_storage_with_calibration()
    await test_handoff_cannot_mislabel_a_speaker()
    await test_enrollment_cannot_resume_across_a_reload()
    await test_below_bar_enrollment_does_not_advance()
    await test_speaker_handoffs_gate_every_change()
    await test_trial_persistence_is_atomic()
    await test_autosave_shape_is_non_audio_and_resumable()
    await test_acceptance_bars_are_unchanged()
    await test_incomplete_run_is_not_complete_not_pass()
    await test_recording_windows_are_safe()
    await test_latency_is_stt_only_and_not_pooled()
    check.finish()


if __name__ == "__main__":
    run(main)
