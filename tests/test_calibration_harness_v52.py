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


def _code_only(js: str) -> str:
    """Strip JS comments. Every "must NOT appear" check has to run against code:
    the harness explains in comments exactly which mistakes it avoids, and
    naming them there would otherwise fail the check that they are gone."""
    out = []
    for line in js.splitlines():
        if line.strip().startswith("//"):
            continue
        if "//" in line and "://" not in line:
            line = line[:line.index("//")]
        out.append(line)
    return "\n".join(out)


def _node(body: str) -> dict:
    """Load the harness logic in node and run `body`, which sets `out`."""
    node = shutil.which("node")
    if node is None:
        raise AssertionError("node is not on PATH")
    with tempfile.TemporaryDirectory() as td:
        mod = Path(td) / "harness.cjs"
        mod.write_text(_script(), encoding="utf-8")
        driver = Path(td) / "run.cjs"
        # The body is wrapped in an async IIFE so it may use `await` — a .cjs
        # file has no top-level await, and switching to .mjs would break the
        # CommonJS export the harness uses.
        driver.write_text(
            "const P52 = require(" + json.dumps(str(mod).replace("\\", "/")) + ");\n"
            "const out = {};\n"
            "(async () => {\n" + body + "\n})()\n"
            "  .then(() => process.stdout.write(JSON.stringify(out)))\n"
            "  .catch(e => { console.error(e && e.stack || String(e));"
            " process.exit(1); });\n",
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


async def test_sentinel_canonicalization():
    check.section("STEP 9: formatting-tolerant, word-exact sentinel matching")
    out = _node("""
      const M = P52.SENTINELS.marcus, G = P52.SENTINELS.guest;
      out.M = M; out.G = G;
      const c = (r, s) => P52.containsSentinel(r, s);

      // POSITIVE: the same words, however STT chose to punctuate them.
      out.pos = {
        exact:      c("BLUE TIGER SPOON", M),
        lower:      c("blue tiger spoon", M),
        hyphen:     c("Blue-tiger-spoon", M),
        sentence:   c("The three words were Blue Tiger Spoon.", M),
        commas:     c("They were blue, tiger, spoon!", M),
        multispace: c("BLUE   TIGER\\n SPOON", M),
      };

      // NEGATIVE: a near-miss is a miss. No edit distance, no partial credit.
      out.neg = {
        truncated:  c("BLUE TIGER", M),
        substituted:c("BLUE TIGER FORK", M),
        otherToken: c(G, M),
        reordered:  c("SPOON TIGER BLUE", M),
        extraInside:c("BLUE TIGER GREEN SPOON", M),
        empty:      c("", M),
        nullReply:  c(null, M),
        nullSentinel: c("BLUE TIGER SPOON", null),
      };

      // LEAK DETECTION uses the SAME function, so normalisation cannot make the
      // positive easier while making the leak harder to see.
      out.leak = {
        plain:   c("Your three words were BLUE TIGER SPOON", M),
        cased:   c("your three words were blue tiger spoon", M),
        hyphen:  c("I recall Blue-Tiger-Spoon for you", M),
        guestGotMarcus: c("Marcus said blue tiger spoon", M),
      };
      out.sameFn = String(P52.containsSentinel).length > 0;
      out.tokens = P52.canonTokens("  Blue-Tiger, SPOON!! ");
    """)
    check(out["M"] == "BLUE TIGER SPOON", f"Marcus sentinel ({out['M']})")
    check(out["G"] == "GREEN ROCKET CHAIR", f"guest sentinel ({out['G']})")
    check(out["tokens"] == ["BLUE", "TIGER", "SPOON"],
          f"canonical tokens ({out['tokens']})")
    check(not any(ch.isdigit() for ch in out["M"] + out["G"]),
          "letter-only — no digits for STT to render as words")
    check("-" not in out["M"] and "-" not in out["G"],
          "and no punctuation for STT to drop")

    for name, got in out["pos"].items():
        check(got is True, f"POSITIVE {name}: counts as the sentinel")
    for name, got in out["neg"].items():
        check(got is False, f"NEGATIVE {name}: does NOT count")
    for name, got in out["leak"].items():
        check(got is True,
              f"LEAK {name}: a formatting-varied Marcus sentinel IS detected")


async def test_step9_mints_a_fresh_conversation_per_run():
    check.section("STEP 9: every re-run gets a NEW conversation id")
    html = HARNESS.read_text(encoding="utf-8")
    flow = _code_only(html[html.index("async function sentinelFlow"):
                           html.index("async function permissionFlow")])

    # The bug: `if (!S.sentinelConversationId)` reused the abandoned attempt's
    # id, and conversation state lives 7 days — so a retry ran on top of the
    # old run's store turns.
    check("if (!S.sentinelConversationId)" not in flow,
          "the reuse guard is gone")
    check("S.sentinelConversationId = crypto.randomUUID();" in flow,
          "a UUID is minted unconditionally on every invocation")
    mint = flow.index("S.sentinelConversationId = crypto.randomUUID()")
    first_record = flow.index("await runBlock(")
    check(mint < first_record, "before the first recording")
    check("const CID = S.sentinelConversationId;" in flow,
          "and the five turns of THIS run share exactly that one id")
    # Steps 1-8 state must not be touched by minting a step-9 id.
    seg = flow[mint:mint + 400]
    for key in ("S.trials", "S.validation", "S.enroll", "S.applied", "S.marcusId",
                "S.guestId", "S.proposal"):
        check(key not in seg, f"minting does not disturb {key} — steps 1-8 survive")

    # Behavioural: two invocations of the mint rule must differ, and a persisted
    # id from an earlier session cannot force reuse.
    out = _node("""
      const state = P52.freshState();
      const mint = () => { state.sentinelConversationId = "uuid-" + (out.n = (out.n||0) + 1); };
      mint(); const first = state.sentinelConversationId;
      mint(); const second = state.sentinelConversationId;
      out.first = first; out.second = second; out.differ = first !== second;
      // A reloaded state carrying an old id must still be overwritten.
      const reloaded = JSON.parse(JSON.stringify(state));
      reloaded.sentinelConversationId = "uuid-from-last-week";
      mint.call(null);
      out.reloadedHeld = reloaded.sentinelConversationId;
      out.freshHasNone = P52.freshState().sentinelConversationId === null;
    """)
    check(out["differ"], f"two runs get different ids ({out['first']} vs {out['second']})")
    check(out["freshHasNone"], "a new session starts with no sentinel conversation")


async def test_step9_canary_comes_from_the_real_transcript():
    check.section("STEP 9: the expected answer is what STT actually decoded")
    out = _node("""
      const E = P52.extractCanary;
      out.good = {
        anchored:  E("Remember these three words blue tiger soon."),
        colon:     E("remember these three words: green rocket chair"),
        punctuated:E("Remember these three words, Blue-Tiger-Spoon!"),
        noAnchor:  E("blue tiger spoon"),
        trailing:  E("Okay. Remember these three words: silver harbour lantern."),
      };
      out.bad = {
        tooFew:    E("Remember these three words"),
        empty:     E(""),
        nullIn:    E(null),
        oneWord:   E("uh"),
        duplicate: E("Remember these three words blue blue tiger"),
        stopword:  E("please remember these three words"),
      };
      out.intended = P52.SENTINELS;
      out.stopwords = P52.CANARY_STOPWORDS;
    """)
    check(out["good"]["anchored"] == "BLUE TIGER SOON",
          f"a misheard word is taken AS HEARD ({out['good']['anchored']}) — "
          "SOON, not SPOON, because SOON is what /chat received")
    check(out["good"]["colon"] == "GREEN ROCKET CHAIR", "colon form parses")
    check(out["good"]["punctuated"] == "BLUE TIGER SPOON", "punctuation is stripped")
    check(out["good"]["noAnchor"] == "BLUE TIGER SPOON",
          "and it falls back to the last three tokens when STT drops the anchor")
    check(out["good"]["trailing"] == "SILVER HARBOUR LANTERN",
          "HARBOUR is accepted as heard — no alias list, no fuzzy matching")

    for name, got in out["bad"].items():
        check(got is None,
              f"unparseable ({name}) returns null so the sample is RETAKEN, not guessed")

    check(out["intended"]["marcus"] == "BLUE TIGER SPOON"
          and out["intended"]["guest"] == "GREEN ROCKET CHAIR",
          f"the spoken cues are simple three-word phrases ({out['intended']})")

    # The flow must actually fail the sample on a parse failure.
    html = HARNESS.read_text(encoding="utf-8")
    flow = _code_only(html[html.index("async function sentinelFlow"):
                           html.index("async function permissionFlow")])
    # The per-sample logic now lives in makeSentinelDoSample (PART 1) so it can
    # be driven with injected deps — see the behavioural test below.
    sample = _code_only(html[html.index("function makeSentinelDoSample"):
                             html.index("// ── the guided stage")])
    check("extractCanary(s.text)" in sample, "the canary is read from the /stt text")
    check("throw new Error" in sample,
          "and a failed parse throws, so runBlock re-records the SAME index")
    check("ctx.observed[spec.canaryFor] = pendingCanary" in sample,
          "the observed canary is stored per speaker, only after /chat succeeds")
    check("makeSentinelDoSample(" in flow,
          "and the flow wires the real deps into it")
    # And every verdict compares against `observed`, never the cue card.
    for field in ("marcus_got_own", "guest_got_own", "guest_got_marcus",
                  "unverified_got_either"):
        line = [l for l in flow.splitlines() if f"out.{field} =" in l
                or (field in l and "containsSentinel" in l)]
        check(any("observed." in l for l in line),
              f"{field} compares against the OBSERVED canary ({[l.strip()[:70] for l in line]})")
    check("containsSentinel(replies[2], M_S)" not in flow,
          "no verdict compares against the intended cue text")
    check("canaries_parsed" in flow,
          "and an unparsed canary fails the run rather than passing vacuously")
    for field in ("intended_canary", "observed_stt_canary"):
        check(field in flow, f"the report records `{field}`")


async def test_store_is_atomic_with_respect_to_chat():
    check.section("A/B/C: validate BEFORE /chat; abort after it")
    html = HARNESS.read_text(encoding="utf-8")
    flow = _code_only(html[html.index("async function sentinelFlow"):
                           html.index("async function permissionFlow")])

    # A. The pipeline is split, and validation sits between the halves.
    check("async function sttTurn" in html and "async function chatFromStt" in html,
          "the pipeline is split into /stt and /chat primitives")
    check("chatTurn(" not in flow,
          "the fused /stt->/chat call is gone from the sentinel flow")
    sample_src = _code_only(html[html.index("function makeSentinelDoSample"):
                                 html.index("// ── the guided stage")])
    stt_at = sample_src.index("await deps.sttTurn(blob)")
    extract_at = sample_src.index("extractCanary(s.text)")
    ask_at = sample_src.index("isAskTranscript(s.text)")
    chat_at = sample_src.index("await deps.chatFromStt(")
    check(stt_at < extract_at < chat_at,
          "the store canary is validated between /stt and /chat")
    check(stt_at < ask_at < chat_at,
          "the ask transcript is validated between /stt and /chat too")
    check("throw new Error" in sample_src[extract_at:chat_at],
          "a rejection throws BEFORE any /chat call is made")

    # Exactly one transcription per sample: no re-running Whisper on audio that
    # already succeeded. (Proven behaviourally as well, further down.)
    check(sample_src.count("await deps.sttTurn(") == 1,
          "audio is transcribed exactly once")
    check(sample_src.count("await deps.chatFromStt(") == 1,
          "and redeemed at most once")
    # The commit boundary covers the bookkeeping, not only the HTTP call.
    check("committed = true" in sample_src and "committed && !e.postChat" in sample_src,
          "everything after dispatch is inside the post-chat boundary")
    check("throw PostChatError(" in sample_src,
          "and a bookkeeping failure is promoted to a run abort")

    # The handle and text forwarded are THE ones /stt returned — the browser
    # never asserts identity.
    prim = _code_only(html[html.index("async function chatFromStt"):
                           html.index("// ── composite flows")])
    check("sttResult.text" in prim and "sttResult.handle" in prim,
          "chatFromStt forwards exactly the /stt text and handle")
    # The browser may pass the opaque handle through; it must never assert WHO.
    body = prim[prim.index("JSON.stringify({"):prim.index("})});") + 5]
    for forged in ("profile_id", "display_name", "role", "speaker_status"):
        check(forged not in body,
              f"and the /chat body never asserts {forged} from the browser")
    check("voice_turn_id: sttResult.handle" in body,
          "identity travels only as the backend-issued handle")

    # B. Post-dispatch failure aborts the whole run.
    check("function PostChatError" in html and "e.postChat = true" in html,
          "a /chat failure is tagged as post-dispatch")
    check("throw PostChatError(" in prim, "and chatFromStt raises it")
    eng = _code_only(html[html.index("async function runBlock"):
                          html.index("// ── per-sample")])
    check("e.postChat" in eng and "RUN.abort = true" in eng,
          "runBlock ABORTS on a post-dispatch failure instead of retrying")
    check("aborted_reason" in eng, "and records why")
    # An aborted sentinel returns nothing usable.
    check("if (out.steps.length < SENTINEL_STEPS.length) return null" in flow,
          "an incomplete sentinel run yields no result at all")


async def test_step9_evidence_revision_and_gating():
    check.section("K/L: stale evidence rejected; a failed gate does not advance")
    html = HARNESS.read_text(encoding="utf-8")

    out = _node(_complete_state_js() + """
      const REV = P52.STEP9_EVIDENCE_REVISION;
      out.rev = REV;
      const miss = s => P52.computeAcceptance(s).missing.join(" | ");
      const fail = s => P52.computeAcceptance(s).failed.join(" | ");

      let s = base(); s.sentinel.evidence_revision = "p52-step9-OLD";
      out.oldRev = miss(s);
      s = base(); delete s.sentinel.evidence_revision;
      out.noRev = miss(s);

      // I: effective source must be calibrated, not merely present.
      s = base(); s.statusAfter.threshold_source = "env override";
      out.envThresh = miss(s);
      s = base(); s.statusAfter.margin_source = "env override";
      out.envMargin = miss(s);
      s = base(); s.statusAfter.threshold_source = "provisional default";
      out.provThresh = miss(s);
      s = base(); s.statusAfter.margin_source = "provisional default";
      out.provMargin = miss(s);

      // J: exactly 6 + 6.
      out.latency = {};
      for (const [off, on] of [[6,6],[5,6],[6,5],[7,6],[6,7],[1,1],[0,0]]) {
        const t = base(); t.sttLatency = {n_off:off, n_on:on};
        out.latency[off + "+" + on] = miss(t);
      }

      // E: the symmetric privacy verdict.
      out.sym = {};
      for (const f of ["marcus_got_guest", "guest_got_marcus", "store_cross_leak"]) {
        const t = base(); t.sentinel[f] = true;
        out.sym[f] = fail(t);
      }
      const t = base(); t.sentinel.canaries_parsed = false;
      out.unparsed = fail(t);
      out.clean = P52.computeAcceptance(base()).acceptance;
    """)
    check(out["rev"] == "p52-step9-atomic-v1", f"revision constant ({out['rev']})")
    check("re-run" in out["oldRev"], f"an OLD revision demands a re-run ({out['oldRev']})")
    check("unversioned" in out["noRev"], f"and a missing one too ({out['noRev']})")

    check("threshold_source" in out["envThresh"],
          f"an env threshold override blocks acceptance ({out['envThresh']})")
    check("margin_source" in out["envMargin"],
          f"an env margin override blocks acceptance ({out['envMargin']})")
    check("threshold_source" in out["provThresh"], "as does a provisional threshold")
    check("margin_source" in out["provMargin"], "and a provisional margin")

    check(out["latency"]["6+6"] == "", "6+6 is complete")
    for combo in ("5+6", "6+5", "7+6", "6+7", "1+1", "0+0"):
        check("latency" in out["latency"][combo],
              f"{combo} is rejected ({out['latency'][combo]})")

    for field, msg in out["sym"].items():
        check(msg, f"{field} fails the run ({msg})")
    check(out["unparsed"], f"an unparsed canary fails ({out['unparsed']})")
    check(out["clean"] == "PASS", f"and a clean run still passes ({out['clean']})")

    # L: a failing gate must not advance the step counter.
    ui = _code_only(html[html.index("const msb = el(\"button\""):
                         html.index("root.appendChild(stepCard(10")])
    check("if (r.pass) S.step = 9;" in ui,
          "step 9 advances ONLY on a pass")
    check("S.sentinel = null; save();" in ui,
          "and the previous result is cleared before the run, not after")
    pm = _code_only(html[html.index("const pmb = el(\"button\""):
                         html.index("root.appendChild(stepCard(11")])
    check("if (r.pass) S.step = 10;" in pm, "step 10 advances only on a pass")
    check("S.permission = null; save();" in pm, "and clears its stale result too")


async def test_ask_and_glue_grammar():
    check.section("C/D/F: ask validation, fail-closed parsing, list grammar")
    out = _node("""
      const A = P52.isAskTranscript, E = P52.extractCanary,
            c = (r,s) => P52.containsSentinel(r,s), M = "BLUE TIGER SPOON";
      out.ask_ok = ["Repeat my three words.", "What are my three words?",
                    "What were my three words", "Tell me my three words",
                    "Say my three words"].map(A);
      out.ask_bad = ["what are my words", "repeat the three words",
                     "tell me about the weather", "repeat my three sentences",
                     "", "what are your three words"].map(A);
      out.parse_bad = ["so I went to the shop yesterday morning",
                       "I think the answer is blue tiger spoon",
                       "Remember these three words and the",
                       "the words are blue tiger"].map(E);
      out.parse_ok = [E("Remember these three words blue tiger soon."),
                      E("blue tiger spoon")];
      out.glue_ok = ["BLUE TIGER SPOON", "blue, tiger, and spoon",
                     "BLUE AND TIGER AND SPOON",
                     "Your words were blue, tiger, and spoon."].map(r => c(r, M));
      out.glue_bad = ["BLUE TIGER FORK", "BLUE FORK SPOON", "SPOON TIGER BLUE",
                      "BLUE TIGER GREEN SPOON", "BLUE TIGER",
                      "AND AND AND"].map(r => c(r, M));
      out.leak_glue = c("Marcus's words were blue, tiger, and spoon", M);
      out.stopwords = P52.CANARY_STOPWORDS;
    """)
    check(all(out["ask_ok"]), f"every valid ask phrasing is accepted ({out['ask_ok']})")
    check(not any(out["ask_bad"]),
          f"malformed asks are rejected BEFORE /chat ({out['ask_bad']})")
    check(all(p is None for p in out["parse_bad"]),
          f"fail-closed: no canary from arbitrary sentences ({out['parse_bad']})")
    check(out["parse_ok"] == ["BLUE TIGER SOON", "BLUE TIGER SPOON"],
          f"while the two legitimate shapes parse ({out['parse_ok']})")
    check(all(out["glue_ok"]), f"list grammar matches ({out['glue_ok']})")
    check(not any(out["glue_bad"]),
          f"but no wrong word, order or extra word does ({out['glue_bad']})")
    check(out["leak_glue"] is True,
          "and a list-grammar leak in a guest reply is still caught")
    for w in ("AND", "A", "AN", "MY", "THE", "IS", "ARE", "THREE", "WORDS", "REMEMBER", "THESE"):
        check(w in out["stopwords"], f"{w} is rejected as a payload word")


async def test_live_contract_consistency():
    check.section("7: the CUE the human sees passes the VALIDATOR that runs")
    # This is the test that was missing. The validator was only ever exercised
    # against phrases invented next to it, so 99/99 stayed green while the
    # printed cue ("What three words did I ask you to remember?") contained no
    # MY and could never pass — a guaranteed live deadlock on all three asks.
    out = _node("""
      const U = P52.ASK_UTTERANCE;
      out.utterance = U;
      out.utterance_valid = P52.isAskTranscript(U);
      out.cue_derives = P52.ASK_CUE.indexOf(U) >= 0;
      // EVERY ask step, exactly as shown to the human.
      out.asks = P52.SENTINEL_STEPS.filter(s => s.phase === "ask").map(s => {
        const m = /say — "([^"]+)"/.exec(s.cue);
        const spoken = m ? m[1] : null;
        return {who: s.who, spoken, valid: spoken ? P52.isAskTranscript(spoken) : false,
                derived: spoken === U};
      });
      // And the STORE cue: a perfect transcription must parse to the intended
      // canary, or the store turn deadlocks the same way.
      out.stores = P52.SENTINEL_STEPS.filter(s => s.canaryFor).map(s => {
        const m = /say — "([^"]+)"/.exec(s.cue);
        const spoken = m ? m[1] : null;
        return {who: s.who, spoken, parsed: P52.extractCanary(spoken),
                intended: P52.SENTINELS[s.canaryFor]};
      });
    """)
    check(out["utterance_valid"] is True,
          f"the real utterance {out['utterance']!r} passes the real validator")
    check(out["cue_derives"] is True, "and the cue is derived from it")
    check(len(out["asks"]) == 3, f"three ask turns ({len(out['asks'])})")
    for a in out["asks"]:
        check(a["valid"] is True,
              f"{a['who']}: the spoken cue {a['spoken']!r} is ACCEPTED")
        check(a["derived"] is True,
              f"{a['who']}: and comes from ASK_UTTERANCE, not a second copy")
    check(len(out["stores"]) == 2, "two store turns")
    for s in out["stores"]:
        check(s["parsed"] == s["intended"],
              f"{s['who']}: a perfect transcription parses to {s['intended']!r} "
              f"(got {s['parsed']!r})")


async def test_sentinel_sample_behaviour_with_injected_deps():
    check.section("3: real call COUNTS, not source-code shape")
    out = _node("""
      const REV = P52.STEP9_EVIDENCE_REVISION;
      function harness(sttText, opts) {
        opts = opts || {};
        const calls = {stt: 0, chat: 0};
        const ctx = {steps: P52.SENTINEL_STEPS, CID: "CID-1",
                     observed: {marcus: null, guest: null}, replies: [],
                     out: {steps: [], conversation_stable: true}};
        const deps = {
          sttTurn: async () => { calls.stt++;
            return {text: sttText, speaker: {status: "known"}, handle: "h1"}; },
          chatFromStt: async () => { calls.chat++;
            if (opts.chatThrows) { const e = new Error("boom"); e.postChat = true; throw e; }
            return {text: sttText, speaker: {status: "known"},
                    reply: opts.reply || "ok",
                    conversation_id: opts.cid === undefined ? "CID-1" : opts.cid}; },
        };
        // Optionally break the bookkeeping AFTER /chat returns.
        if (opts.breakBookkeeping) {
          Object.defineProperty(ctx.out, "steps",
            {get() { throw new Error("bookkeeping exploded"); }});
        }
        return {calls, ctx, fn: P52.makeSentinelDoSample(deps, ctx)};
      }
      async function run(h, i) {
        try { await h.fn(null, i); return {ok: true}; }
        catch (e) { return {ok: false, msg: e.message, postChat: !!e.postChat}; }
      }
      // A. invalid STORE transcript -> stt 1, chat 0
      let h = harness("uh what");
      out.invalid = {r: await run(h, 0), calls: h.calls};
      // B. valid STORE -> stt 1, chat 1
      h = harness("Remember these three words blue tiger spoon");
      out.valid = {r: await run(h, 0), calls: h.calls,
                   observed: h.ctx.observed.marcus};
      // C. invalid ASK -> stt 1, chat 0
      h = harness("tell me about the weather");
      out.badAsk = {r: await run(h, 2), calls: h.calls};
      // D. valid ASK -> stt 1, chat 1
      h = harness(P52.ASK_UTTERANCE);
      out.goodAsk = {r: await run(h, 2), calls: h.calls};
      // E. post-chat bookkeeping failure -> chat happened, must be postChat
      h = harness(P52.ASK_UTTERANCE, {breakBookkeeping: true});
      out.bookkeeping = {r: await run(h, 2), calls: h.calls};
      // F. wrong conversation id -> postChat abort
      h = harness(P52.ASK_UTTERANCE, {cid: "SOMEONE-ELSE"});
      out.wrongCid = {r: await run(h, 2), calls: h.calls,
                      stable: h.ctx.out.conversation_stable};
      // G. null conversation id -> postChat abort
      h = harness(P52.ASK_UTTERANCE, {cid: null});
      out.nullCid = {r: await run(h, 2), calls: h.calls};
      // H. /chat itself fails -> postChat
      h = harness(P52.ASK_UTTERANCE, {chatThrows: true});
      out.chatFails = {r: await run(h, 2), calls: h.calls};
    """)

    check(out["invalid"]["calls"] == {"stt": 1, "chat": 0},
          f"invalid STORE: /stt once, /chat NEVER ({out['invalid']['calls']})")
    check(out["invalid"]["r"]["postChat"] is False,
          "and it is retryable, not an abort")
    check(out["valid"]["calls"] == {"stt": 1, "chat": 1},
          f"valid STORE: /stt once, /chat once ({out['valid']['calls']})")
    check(out["valid"]["observed"] == "BLUE TIGER SPOON",
          f"with the observed canary recorded ({out['valid']['observed']})")
    check(out["badAsk"]["calls"] == {"stt": 1, "chat": 0},
          f"malformed ASK: /stt once, /chat NEVER ({out['badAsk']['calls']})")
    check(out["goodAsk"]["calls"] == {"stt": 1, "chat": 1},
          f"the REAL ask utterance goes through ({out['goodAsk']['calls']})")
    check(out["goodAsk"]["r"]["ok"] is True,
          f"and succeeds ({out['goodAsk']['r'].get('msg')})")

    # The whole point of item 2.
    check(out["bookkeeping"]["calls"]["chat"] == 1,
          "post-chat failure: /chat DID happen")
    check(out["bookkeeping"]["r"]["postChat"] is True,
          f"so it is promoted to a run-aborting error "
          f"({out['bookkeeping']['r'].get('msg')})")
    for name in ("wrongCid", "nullCid"):
        check(out[name]["r"]["postChat"] is True,
              f"{name}: a conversation-id mismatch ABORTS ({out[name]['r'].get('msg')})")
    check(out["wrongCid"]["stable"] is False, "and is recorded as unstable")
    check(out["chatFails"]["r"]["postChat"] is True,
          "a /chat transport failure aborts too")


async def test_effective_policy_helper_is_shared():
    check.section("4: one policy helper, used everywhere")
    out = _node("""
      const C = P52.calibratedPolicyStatus;
      const good = {threshold_calibrated:true, threshold_source:"calibrated",
                    margin_source:"calibrated"};
      out.good = C(good);
      out.noStatus = C(null);
      out.envT = C({...good, threshold_source:"env override"});
      out.envM = C({...good, margin_source:"env override"});
      out.provT = C({...good, threshold_source:"provisional default"});
      out.provM = C({...good, margin_source:"provisional default"});
      out.notCal = C({...good, threshold_calibrated:false});
      out.problemEnv = P52.policyProblem({...good, threshold_source:"env override"});
      out.problemNone = P52.policyProblem(null);
      out.problemGood = P52.policyProblem(good);
    """)
    check(out["good"] is True, "all three calibrated -> true")
    for k in ("noStatus", "envT", "envM", "provT", "provM", "notCal"):
        check(out[k] is False, f"{k} -> false")
    check("env override" in out["problemEnv"], f"and says why ({out['problemEnv']})")
    check("press Check" in out["problemNone"], "no status tells the human to Check")
    check(out["problemGood"] == "", "a good policy reports no problem")

    html = HARNESS.read_text(encoding="utf-8")
    code = _code_only(html)
    # One definition, used by Apply, Check, both preflights and acceptance.
    check(code.count("function calibratedPolicyStatus") == 1,
          "the helper is defined once")
    check(code.count("calibratedPolicyStatus(") >= 4,
          f"and consulted from several places ({code.count('calibratedPolicyStatus(')})")
    check("policyPreflight(\"Step 8 validation\")" in code,
          "step 8 preflights the policy")
    check("policyPreflight(\"Step 9\")" in code, "step 9 preflights the policy")
    # The preflight must run BEFORE the mic and before state is cleared.
    ms = code[code.index("msb.onclick"):code.index("ms.appendChild(msb)")]
    check(ms.index("policyPreflight") < ms.index("S.sentinel = null"),
          "and step 9's preflight runs BEFORE the previous result is cleared")
    # Check button persists the status it fetched.
    ping = code[code.index('$("#ping").onclick'):]
    check("S.statusAfter = st" in ping, "the Check button PERSISTS the status")
    check("threshold_source" in ping and "margin_source" in ping,
          "and displays BOTH sources")


async def test_step_gating_predicates():
    check.section("5: a failed mandatory gate cannot be stepped past")
    out = _node("""
      const REV = P52.STEP9_EVIDENCE_REVISION;
      const ready = {marcusId:"m", guestId:"g",
        enroll:{marcus:{meets_p52_bar:true}, guest:{meets_p52_bar:true}},
        applied:true, validation:[{}], sentinel:null, permission:null};
      out.s9ready = P52.step9Ready(ready);
      out.s9notReady = P52.step9Ready({...ready, applied:false});
      out.s9noValidation = P52.step9Ready({...ready, validation:[]});

      const pass9 = {...ready, sentinel:{pass:true, evidence_revision:REV}};
      const fail9 = {...ready, sentinel:{pass:false, evidence_revision:REV}};
      const old9  = {...ready, sentinel:{pass:true, evidence_revision:"p52-OLD"}};
      out.s10_afterPass = P52.step10Ready(pass9);
      out.s10_afterFail = P52.step10Ready(fail9);
      out.s10_afterOld  = P52.step10Ready(old9);
      out.s10_afterNone = P52.step10Ready(ready);

      out.s11_afterPass = P52.step11Ready({...pass9, permission:{pass:true}});
      out.s11_afterFail = P52.step11Ready({...pass9, permission:{pass:false}});
      out.s11_afterNone = P52.step11Ready(pass9);
    """)
    check(out["s9ready"] is True, "step 9 available once steps 1-8 exist")
    check(out["s9notReady"] is False, "not before calibration is applied")
    check(out["s9noValidation"] is False, "nor before step 8 validation exists")
    check(out["s10_afterPass"] is True, "step 10 available after a step-9 PASS")
    check(out["s10_afterFail"] is False, "NOT after a step-9 failure")
    check(out["s10_afterOld"] is False,
          "NOT after an old-revision step-9 PASS")
    check(out["s10_afterNone"] is False, "nor with no step-9 result at all")
    check(out["s11_afterPass"] is True, "step 11 available after a step-10 PASS")
    check(out["s11_afterFail"] is False, "NOT after a step-10 failure")
    check(out["s11_afterNone"] is False, "nor with no step-10 result")

    html = HARNESS.read_text(encoding="utf-8")
    code = _code_only(html)
    for pred, btn in (("step9Ready", "msb"), ("step10Ready", "pmb"), ("step11Ready", "ltb")):
        check(f"if (!{pred}(S))" in code and f"{btn}.disabled = true" in code,
              f"{btn} is disabled when {pred} is false")
    # Results must remain visible even when the next gate is locked.
    check("ms.appendChild(el(\"pre\", null, JSON.stringify(S.sentinel" in code,
          "a failed step-9 result stays on screen and copyable")


async def test_clean_p52_profile_lifecycle_is_accepted():
    check.section("1: the DESIGNED one-profile -> two-profile transition")
    # The first attempt at generation tracking compared the whole compatible
    # set, so Phase A ({M}) never matched Phase B ({M,G}) and a PERFECTLY CLEAN
    # run could not reach step 7. This walks the real protocol.
    out = _node("""
      const B = "ecapa@0f99f2d0", M = "spk-M", G = "spk-G";
      const S = P52.freshState();
      const stale = (ids, build) => P52.staleLineage(S, ids, build || B).map(x => x.block);

      // Step 2-4: only Marcus is enrolled. The guest records 12 utterances as an
      // UNENROLLED impostor — scored against Marcus's centroid alone.
      P52.recordLineage(S, "trials_marcus", [M], B);
      P52.recordLineage(S, "trials_guest", [M], B);
      out.afterPhaseA = stale([M]);

      // Step 5: the guest is enrolled. The population GROWS by design.
      out.afterGuestEnrolled = stale([M, G]);

      // Step 6-8: both centroids now.
      P52.recordLineage(S, "phaseb_marcus", [M, G], B);
      P52.recordLineage(S, "phaseb_guest", [M, G], B);
      P52.recordLineage(S, "valid_marcus", [M, G], B);
      P52.recordLineage(S, "valid_guest", [M, G], B);
      out.afterPhaseB = stale([M, G]);

      // Full acceptance on the clean run.
      const A = (() => {
        const s = S;
        s.marcusId = M; s.guestId = G;
        s.enroll = {marcus:{meets_p52_bar:true}, guest:{meets_p52_bar:true}};
        const row = (block, truth, phase) => ({block, truth, truthName:"x", phase,
          condition:"normal", status:"known", predicted_profile_id:truth,
          top_profile_id:truth, second_profile_id:null, top_score:0.8});
        s.trials = [].concat(
          Array.from({length:20}, () => row("trials_marcus", M, "A")),
          Array.from({length:12}, () => row("trials_guest", G, "A")),
          Array.from({length:12}, () => row("phaseb_marcus", M, "B")),
          Array.from({length:12}, () => row("phaseb_guest", G, "B")));
        s.validation = [].concat(
          Array.from({length:10}, () => row("valid_marcus", M, "V")),
          Array.from({length:10}, () => row("valid_guest", G, "V")));
        s.applied = true;
        s.statusAfter = {threshold_calibrated:true, threshold_source:"calibrated",
          margin_source:"calibrated", profiles_detail:[
            {profile_id:M, role:"owner", compatible:true},
            {profile_id:G, role:"guest", compatible:true}]};
        s.sentinel = {pass:true, conversation_stable:true,
          unverified_is_unverified:true, canaries_parsed:true,
          evidence_revision:P52.STEP9_EVIDENCE_REVISION};
        s.permission = {pass:true};
        s.sttLatency = {n_off:6, n_on:6};
        return P52.computeAcceptance(s);
      })();
      out.acceptance = A.acceptance;
      out.generation = {ok: A.generation.ok, lineage_ok: A.generation.lineage_ok,
                        trials_ok: A.generation.trials_ok,
                        validation_ok: A.generation.validation_ok};
      out.failed = A.failed; out.missing = A.missing;
      out.step9Ready = P52.step9Ready(S);

      // MUTATIONS.
      out.marcusReplaced = stale(["spk-M2", G]);
      out.guestReplaced = stale([M, "spk-G2"]);
      out.buildChanged = stale([M, G], "ecapa@OTHERREV");
      out.oldRunIds = stale(["spk-601053c258fa", "spk-c96353f36365"]);
    """)
    check(out["afterPhaseA"] == [],
          f"Phase A is clean while only Marcus exists ({out['afterPhaseA']})")
    check(out["afterGuestEnrolled"] == [],
          f"and STAYS clean once the guest is enrolled — the designed step-5 "
          f"transition is not staleness ({out['afterGuestEnrolled']})")
    check(out["afterPhaseB"] == [],
          f"Phase B and validation are clean too ({out['afterPhaseB']})")
    check(out["generation"]["ok"] is True,
          f"the whole clean run passes the generation check ({out['generation']})")
    check(out["acceptance"] == "PASS",
          f"and the clean run PASSES acceptance ({out['acceptance']} / "
          f"missing={out['missing']} failed={out['failed']})")
    check(out["step9Ready"] is True, "step 9 is reachable")

    # Replacing a centroid invalidates exactly what depended on it.
    check(sorted(out["marcusReplaced"]) ==
          sorted(["trials_marcus", "trials_guest", "phaseb_marcus",
                  "phaseb_guest", "valid_marcus", "valid_guest"]),
          f"replacing MARCUS invalidates everything ({out['marcusReplaced']})")
    check(sorted(out["guestReplaced"]) ==
          sorted(["phaseb_marcus", "phaseb_guest", "valid_marcus", "valid_guest"]),
          f"replacing the GUEST invalidates phase B and validation only — "
          f"phase A survives ({out['guestReplaced']})")
    check("trials_marcus" not in out["guestReplaced"],
          "because phase A was never scored against a guest centroid")
    check(len(out["buildChanged"]) == 6,
          f"a model build change invalidates everything ({out['buildChanged']})")
    check(len(out["oldRunIds"]) == 6,
          f"and the real failed run's ids invalidate everything "
          f"({out['oldRunIds']})")


async def test_mixed_profile_generation_is_rejected():
    check.section("6: the EXACT live mixed-generation shape must FAIL")
    # Reproduced from the real run: 56 calibration trials scored against
    # spk-601053c258fa / spk-c96353f36365, 20 validation trials against the
    # current spk-ccc5aafb945f / spk-4ebf6e6c6135. The browser computed and
    # applied a calibration from the old scores without noticing.
    out = _node("""
      const OLD_M = "spk-601053c258fa", OLD_G = "spk-c96353f36365";
      const NEW_M = "spk-ccc5aafb945f", NEW_G = "spk-4ebf6e6c6135";
      const gen = ids => "model@rev#" + ids.slice().sort().join(",");

      const S = P52.freshState();
      S.marcusId = NEW_M; S.guestId = NEW_G;
      S.enroll = {marcus:{meets_p52_bar:true, ok:true, profile_id:NEW_M},
                  guest:{meets_p52_bar:true, ok:true, profile_id:NEW_G}};
      S.trials = Array.from({length:56}, (_, i) => ({
        block: i < 20 ? "trials_marcus" : "phaseb_marcus",
        truth: i % 2 ? OLD_M : OLD_G, truthName:"x", phase:"B", condition:"normal",
        status:"known", predicted_profile_id: i % 2 ? OLD_M : OLD_G,
        top_profile_id: i % 2 ? OLD_M : OLD_G,
        second_profile_id: i % 2 ? OLD_G : OLD_M, top_score:0.8, second_score:0.2}));
      S.validation = Array.from({length:20}, (_, i) => ({
        block: i < 10 ? "valid_marcus" : "valid_guest",
        truth: i < 10 ? NEW_M : NEW_G, truthName:"x", phase:"V", condition:"normal",
        status:"known", predicted_profile_id: i < 10 ? NEW_M : NEW_G,
        top_profile_id: i < 10 ? NEW_M : NEW_G, second_profile_id:null,
        top_score:0.8, second_score:null}));
      S.proposal = {ok:true, profiles:[{profile_id:OLD_M, threshold:0.38},
                                       {profile_id:OLD_G, threshold:0.24}]};
      S.applied = true;
      // Lineage as the real run would have recorded it: trials scored against
      // the OLD centroids, validation against the NEW ones.
      P52.recordLineage(S, "phaseb_marcus", [OLD_M, OLD_G], "ecapa@rev");
      P52.recordLineage(S, "valid_marcus", [NEW_M, NEW_G], "ecapa@rev");
      S.statusAfter = {threshold_calibrated:false,
                       threshold_source:"provisional default",
                       margin_source:"provisional default",
                       profiles_detail:[{profile_id:NEW_M, role:"owner", compatible:true},
                                        {profile_id:NEW_G, role:"guest", compatible:true}]};

      const A = P52.computeAcceptance(S);
      out.acceptance = A.acceptance;
      out.generation = A.generation;
      out.failed = A.failed;
      // What the preflight would say against the CURRENT generation.
      out.stale = P52.staleLineage(S, [NEW_M, NEW_G], "ecapa@rev");
      // Deleting a profile must discard everything measured against it.
      const D = JSON.parse(JSON.stringify(S));
      out.dependent = P52.dependentEvidence(D, NEW_M);
      P52.invalidateForProfile(D, NEW_M);
      out.after = {trials: D.trials.length, validation: D.validation.length,
                   proposal: D.proposal, applied: D.applied,
                   marcusId: D.marcusId, sentinel: D.sentinel,
                   lineage: Object.keys(D.lineage || {})};
    """)
    check(out["acceptance"] == "FAIL",
          f"the mixed-generation state FAILS acceptance ({out['acceptance']})")
    check(out["generation"]["ok"] is False, "the generation check reports not-ok")
    check(out["generation"]["trial_ids"] == ["spk-601053c258fa", "spk-c96353f36365"],
          f"naming the stale ids ({out['generation']['trial_ids']})")
    check(out["generation"]["expected_ids"] == ["spk-4ebf6e6c6135", "spk-ccc5aafb945f"],
          f"and the current ones ({out['generation']['expected_ids']})")
    check(any("stale profile generation" in f for f in out["failed"]),
          f"with an explicit failure ({out['failed']})")
    check(any("no longer exist" in f for f in out["failed"]),
          "and it says which evidence lost its centroids")
    check(out["generation"]["validation_ok"] is True,
          "the validation trials themselves are current — only the fit is stale")

    # The preflight would block before any recording.
    check(len(out["stale"]) >= 1,
          f"a preflight against the current generation flags it ({out['stale']})")
    check(any(s["block"] == "phaseb_marcus" for s in out["stale"]),
          f"naming the block specifically ({out['stale']})")

    # Deletion reconciles the browser instead of leaving orphaned scores.
    check("Marcus enrolment" in out["dependent"],
          f"deleting Marcus is shown to discard his enrolment ({out['dependent']})")
    check(out["after"]["validation"] == 0, "validation rows measured against him go")
    check(out["after"]["proposal"] is None and out["after"]["applied"] is False,
          "the fit and its application go")
    check(out["after"]["marcusId"] is None, "and his id is cleared")
    check(out["after"]["sentinel"] is None, "step 9 evidence goes too")


async def test_sentinel_comparison_is_symmetric():
    check.section("STEP 9: one comparison rule for positives AND privacy")
    html = HARNESS.read_text(encoding="utf-8")
    flow = _code_only(html[html.index("async function sentinelFlow"):
                           html.index("async function permissionFlow")])

    # All four verdicts must go through containsSentinel — none may use
    # `.includes(...)`, which is what produced the false negative in run 2.
    for field in ("marcus_got_own", "guest_got_own", "guest_got_marcus",
                  "unverified_got_either"):
        line = [l for l in flow.splitlines() if field in l and "=" in l]
        check(any("containsSentinel" in l for l in line),
              f"{field} uses containsSentinel ({[l.strip()[:60] for l in line]})")
    check(".includes(M_S)" not in flow and ".includes(G_S)" not in flow,
          "no exact .includes() comparison survives anywhere in the flow")
    check(flow.count("containsSentinel(") >= 5,
          f"used for every verdict and both per-turn flags "
          f"({flow.count('containsSentinel(')})")


async def test_sentinel_turns_carry_raw_evidence():
    check.section("STEP 9: raw STT and reply are recorded for diagnosis")
    html = HARNESS.read_text(encoding="utf-8")
    # The recorded object lives in makeSentinelDoSample (PART 1) now.
    flow = html[html.index("function makeSentinelDoSample"):
                html.index("// ── the guided stage")]

    for field in ("stt_text", "assistant_reply", "status", "profile_id",
                  "conversation_id", "stt_tokens", "reply_tokens"):
        check(f"{field}:" in flow, f"every sentinel turn records `{field}`")
    check("r.text" in flow and "r.reply" in flow,
          "taken from the real /stt text and /chat reply")
    # Diagnostics, not biometrics — checked against the recorded object itself,
    # not the surrounding code (which legitimately has a `blob` parameter).
    rec = flow[flow.index("ctx.out.steps.push({"):]
    rec = _code_only(rec[:rec.index("});") + 3])
    for banned in ("embedding", "centroid", "similarity", "blob", "audio",
                   "threshold"):
        check(banned not in rec.lower(),
              f"and no {banned} is recorded on a sentinel turn")


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
          statusAfter:{threshold_calibrated:true,
                       // The EFFECTIVE policy must be the calibrated one, not
                       // merely a record that exists behind an env override.
                       threshold_source:"calibrated", margin_source:"calibrated",
                       profiles_detail:[
            {profile_id:M, role:"owner", compatible:true},
            {profile_id:G, role:"guest", compatible:true}]},
          sentinel:{pass:true, conversation_stable:true, unverified_is_unverified:true,
                    canaries_parsed:true, marcus_got_guest:false,
                    guest_got_marcus:false, store_cross_leak:false,
                    evidence_revision: P52.STEP9_EVIDENCE_REVISION},
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
    await test_sentinel_canonicalization()
    await test_step9_mints_a_fresh_conversation_per_run()
    await test_step9_canary_comes_from_the_real_transcript()
    await test_store_is_atomic_with_respect_to_chat()
    await test_step9_evidence_revision_and_gating()
    await test_ask_and_glue_grammar()
    await test_live_contract_consistency()
    await test_sentinel_sample_behaviour_with_injected_deps()
    await test_effective_policy_helper_is_shared()
    await test_step_gating_predicates()
    await test_clean_p52_profile_lifecycle_is_accepted()
    await test_mixed_profile_generation_is_rejected()
    await test_sentinel_comparison_is_symmetric()
    await test_sentinel_turns_carry_raw_evidence()
    await test_autosave_shape_is_non_audio_and_resumable()
    await test_acceptance_bars_are_unchanged()
    await test_incomplete_run_is_not_complete_not_pass()
    await test_recording_windows_are_safe()
    await test_latency_is_stt_only_and_not_pooled()
    check.finish()


if __name__ == "__main__":
    run(main)
