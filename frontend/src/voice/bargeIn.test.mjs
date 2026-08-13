/**
 * Offline checks for the barge-in stage-1 decision.
 *
 * The timing wrapper needs a browser, an analyser and a microphone. The
 * JUDGEMENT does not, and it is the part that decides whether Nova interrupts
 * herself — so it is verified here without any of that.
 *
 * Run:  node frontend/src/voice/bargeIn.test.mjs
 * (esbuild strips the types first; see the npm script / the command in
 *  docs/NOVA_V3_PERFORMANCE.md)
 */

import { CouplingEstimator, DEFAULT_BARGE_IN, frameIsSpeechLike, summarize, newAttempt,
         stopLatencyMs, percentile } from "./bargeIn.js";

let failed = 0;
function check(cond, label) {
  const status = cond ? "OK  " : "FAIL";
  if (!cond) failed += 1;
  console.log(`  ${status} ${label}`);
}

const cfg = DEFAULT_BARGE_IN;

console.log("\nthe regression that motivated this file");
{
  // The original gate compared raw mic RMS against a DISPLAY level (raw x3.4,
  // clamped to 1.0) times 1.6. At loud output that saturated, making the
  // threshold 1.6 — unreachable, since mic RMS <= 1. Barge-in was impossible
  // exactly when Nova was loudest. Prove the new gate survives that case.
  const loudTts = 0.40;              // saturates the old display scale
  const coupling = 0.5;

  // The precise sensitivity is a LIVE tuning question and is deliberately not
  // asserted here — mic and speaker energies add in power, not amplitude, so
  // "how much louder must Marcus be" depends on the room. What must hold
  // offline is REACHABILITY: there has to exist a mic level a human can
  // produce that fires the gate. The old comparison failed exactly this.
  const newThreshold = loudTts * coupling * cfg.excessMargin;
  check(newThreshold < 1.0,
        `new threshold at loud TTS is reachable (${newThreshold.toFixed(2)} < 1.0)`);

  const oldThreshold = Math.min(1, loudTts * 3.4) * 1.6;
  check(oldThreshold > 1.0,
        `the OLD threshold was UNREACHABLE at any mic level (${oldThreshold.toFixed(2)} > 1.0)`);

  check(frameIsSpeechLike(0.5, loudTts, cfg, coupling).speechLike,
        "clearly-louder speech over loud TTS fires");
  check(!frameIsSpeechLike(loudTts * coupling, loudTts, cfg, coupling).speechLike,
        "echo alone at loud TTS still does not fire");
}

console.log("\nNova's own echo does not trigger her");
{
  const coupling = 0.5;
  for (const tts of [0.05, 0.15, 0.30, 0.45]) {
    const echoOnly = tts * coupling;   // exactly what the room predicts
    const r = frameIsSpeechLike(echoOnly, tts, cfg, coupling);
    check(!r.speechLike, `tts=${tts.toFixed(2)} echo=${echoOnly.toFixed(3)} -> not speech`);
  }
}

console.log("\nreal speech over the echo does trigger");
{
  const coupling = 0.5;
  for (const tts of [0.05, 0.15, 0.30, 0.45]) {
    const withUser = tts * coupling * 2.6;   // clearly above the margin
    const r = frameIsSpeechLike(withUser, tts, cfg, coupling);
    check(r.speechLike, `tts=${tts.toFixed(2)} mic=${withUser.toFixed(3)} -> speech`);
  }
}

console.log("\nthe gate works across coupling regimes (mic near vs far)");
{
  // A fixed ratio cannot do this: the same absolute mic level means different
  // things at different speaker volumes and mic distances.
  for (const coupling of [0.15, 0.5, 0.9]) {
    const tts = 0.25;
    const echo = tts * coupling;
    check(!frameIsSpeechLike(echo * 1.1, tts, cfg, coupling).speechLike,
          `coupling=${coupling}: echo alone is ignored`);
    check(frameIsSpeechLike(echo * 3.0, tts, cfg, coupling).speechLike,
          `coupling=${coupling}: speech over it is caught`);
  }
}

console.log("\nnoise floor and quiet gaps");
{
  check(!frameIsSpeechLike(0.005, 0.2, cfg, 0.5).speechLike, "sub-floor mic never fires");
  check(!frameIsSpeechLike(0.010, 0.0, cfg, null).speechLike, "quiet room between clips is silent");
  check(frameIsSpeechLike(0.06, 0.0, cfg, null).speechLike, "speech between clips is caught");
}

console.log("\ncoupling estimator");
{
  const est = new CouplingEstimator();
  check(est.value() === null, "no estimate before enough evidence");
  for (let i = 0; i < 30; i += 1) est.observe(0.2 * 0.4, 0.2, cfg.minTtsForEstimate);
  const v = est.value();
  check(v !== null && Math.abs(v - 0.4) < 0.05, `learns the room (got ${v?.toFixed(3)})`);

  // A minority of contaminated frames (Marcus talking) must not move it.
  for (let i = 0; i < 8; i += 1) est.observe(0.2 * 5.0, 0.2, cfg.minTtsForEstimate);
  const v2 = est.value();
  check(v2 !== null && Math.abs(v2 - 0.4) < 0.1,
        `median resists contamination (got ${v2?.toFixed(3)})`);

  est.observe(0.5, 0.001, cfg.minTtsForEstimate);
  check(est.samples === 38, "frames below the TTS floor are not sampled");
}

console.log("\nfallback before the room is known");
{
  const r = frameIsSpeechLike(0.3, 0.2, cfg, null);
  check(typeof r.speechLike === "boolean", "a null estimate still yields a decision");
  check(r.predictedEcho > 0, "fallback coupling is applied rather than assuming zero echo");
}

console.log("\ntelemetry maths");
{
  const a = newAttempt(1);
  check(stopLatencyMs(a) === null, "no latency reported when nothing was observed");
  a.speechDetectedAt = 1000; a.playbackDuckedAt = 1180;
  check(stopLatencyMs(a) === 180, "stop latency is measured, not inferred");

  check(percentile([], 50) === null, "percentile of nothing is null, not 0");
  check(percentile([1, 2, 3, 4, 5], 50) === 3, "median");
  check(percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 90) === 9, "P90");

  const s = summarize([
    { ...newAttempt(1), outcome: "success", speechDetectedAt: 0, playbackDuckedAt: 200 },
    { ...newAttempt(2), outcome: "echo-rejected" },
    { ...newAttempt(3), outcome: "false-self-interrupt" },
    { ...newAttempt(4), outcome: "pending" },
  ]);
  check(s.total === 3, "pending attempts are excluded from the summary");
  check(s.successes === 1 && s.falseSelfInterrupts === 1 && s.echoesCorrectlyRejected === 1,
        "outcomes counted separately");
  check(s.medianStopMs === 200, "median stop latency from real observations only");
}

console.log(`\nRESULT: ${failed ? `${failed} FAILURES` : "ALL PASS"}`);
process.exit(failed ? 1 : 0);
