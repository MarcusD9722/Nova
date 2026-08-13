/**
 * P0 stage 1: decide FAST whether Marcus is talking over Nova.
 *
 * Barge-in is two-stage on purpose, because the two requirements conflict:
 *
 *   Stage 1 (this file)  must be FAST. Nova has to duck within a couple of
 *                        hundred milliseconds or the interruption feels
 *                        broken. It has no transcript, only levels.
 *
 *   Stage 2 (backend)    must be ACCURATE. core/voice/echo.py runs STT and
 *                        decides ECHO / USER / MIXED, and only it can salvage
 *                        the real words out of a transcript that begins as
 *                        Nova's own voice.
 *
 * So this gate is allowed to be wrong, and is designed around being wrong: it
 * DUCKS playback rather than cancelling the turn. If stage 2 returns ECHO the
 * volume is restored and Nova carries on. Nothing is destroyed on the strength
 * of a level reading alone.
 *
 * ── Why a fixed mic-to-TTS ratio cannot work ────────────────────────────────
 *
 * The first version compared mic RMS against `getTtsOutputLevel() * 1.6`. That
 * was broken twice over: getTtsOutputLevel() is a *display* signal (raw RMS
 * x3.4, clamped to 1.0) for avatar lip sync, so the threshold was ~5.4x the
 * true output RMS, and above rms 0.294 it saturated at 1.0 — making the
 * comparison unsatisfiable, since mic RMS is <= 1 by definition. Barge-in was
 * impossible exactly when Nova was loudest.
 *
 * But simply swapping in the raw RMS still leaves a constant to guess, and no
 * constant is correct: how much of Nova's output the microphone hears depends
 * on speaker volume, mic distance and the room. Those are precisely the
 * variables the live acceptance run sweeps. A value tuned at one volume is
 * wrong at another.
 *
 * So the coupling is MEASURED instead. While Nova speaks and nothing else is
 * happening, mic/tts settles at whatever this room's echo path actually is.
 * The gate fires when the mic materially exceeds what that measured coupling
 * predicts — i.e. when there is energy Nova's own voice cannot explain.
 */

import { getTtsOutputRms, isTtsPlaying } from "./recorder";

export type BargeInConfig = {
  /** Mic RMS below this is never speech, whatever else is true. */
  floor: number;
  /**
   * How far above the PREDICTED echo level the mic must sit. Multiplies a
   * measured coupling estimate, not a raw output level, so it stays meaningful
   * across speaker volumes and mic distances.
   */
  excessMargin: number;
  /** Coupling assumed before enough has been measured. Deliberately generous. */
  fallbackCoupling: number;
  /** Ignore TTS frames quieter than this when estimating coupling. */
  minTtsForEstimate: number;
  /** Mic level that counts as speech when Nova is silent between clips. */
  quietThreshold: number;
  /** Consecutive speech-like frames required before firing. */
  sustainMs: number;
  /** Ignore the first moments of a clip — speaker ramp-up is not Marcus. */
  graceMs: number;
  /** Frame period of the polling loop. */
  frameMs: number;
};

export const DEFAULT_BARGE_IN: BargeInConfig = {
  floor: 0.02,
  // Starting points, not tuned constants. The live acceptance run is what sets
  // these honestly: too low and Nova interrupts herself, too high and Marcus
  // has to shout.
  excessMargin: 2.0,
  fallbackCoupling: 0.5,
  minTtsForEstimate: 0.03,
  quietThreshold: 0.025,
  sustainMs: 180,
  graceMs: 250,
  frameMs: 20,
};

/**
 * Running estimate of how much of Nova's output this microphone hears.
 *
 * Median rather than mean: while Marcus talks over her the ratio spikes, and a
 * mean would quietly absorb those spikes into the baseline and desensitise the
 * gate — the failure mode being that the more he interrupts, the less it
 * listens. A median ignores a minority of contaminated frames.
 */
export class CouplingEstimator {
  private ratios: number[] = [];
  private cached: number | null = null;
  constructor(private readonly capacity = 150) {}

  observe(mic: number, tts: number, minTts: number): void {
    if (tts < minTts) return;
    this.ratios.push(mic / tts);
    if (this.ratios.length > this.capacity) this.ratios.shift();
    this.cached = null;
  }

  /** null until there is enough evidence to be worth trusting. */
  value(): number | null {
    if (this.ratios.length < 20) return null;
    if (this.cached !== null) return this.cached;
    const s = [...this.ratios].sort((a, b) => a - b);
    this.cached = s[Math.floor(s.length / 2)];
    return this.cached;
  }

  get samples(): number {
    return this.ratios.length;
  }

  reset(): void {
    this.ratios = [];
    this.cached = null;
  }
}

export type BargeInEvent = {
  at: number;
  micLevel: number;
  ttsRms: number;
  coupling: number;
  predictedEcho: number;
  sustainedMs: number;
  reason: "over-echo" | "tts-quiet";
};

/**
 * The whole stage-1 decision for one frame, as a pure function.
 *
 * Split out precisely so the judgement that decides whether Nova interrupts
 * herself can be verified without a browser, an analyser or a microphone.
 */
export function frameIsSpeechLike(
  mic: number,
  ttsRms: number,
  cfg: BargeInConfig,
  coupling: number | null,
): { speechLike: boolean; reason: BargeInEvent["reason"]; predictedEcho: number } {
  if (mic < cfg.floor) {
    return { speechLike: false, reason: "tts-quiet", predictedEcho: 0 };
  }
  if (ttsRms > 0.01) {
    // Nova is audible. Predict how loud her echo should be in this room, and
    // require the mic to exceed it by a margin.
    const c = coupling ?? cfg.fallbackCoupling;
    const predicted = ttsRms * c;
    return {
      speechLike: mic > predicted * cfg.excessMargin,
      reason: "over-echo",
      predictedEcho: predicted,
    };
  }
  // Between clips: ordinary speech detection applies.
  return {
    speechLike: mic >= cfg.quietThreshold,
    reason: "tts-quiet",
    predictedEcho: 0,
  };
}

/**
 * Watches a live analyser and calls `onCandidate` the first time it believes
 * Marcus has started talking over Nova.
 *
 * Returns stop(). Deliberately does NOT touch playback, cancel a turn or call
 * the backend — the caller owns those decisions, so this stays testable and
 * cannot half-perform an interruption.
 */
export function watchForSpeechOverPlayback(
  analyser: AnalyserNode,
  onCandidate: (ev: BargeInEvent) => void,
  cfg: Partial<BargeInConfig> = {},
  estimator: CouplingEstimator = new CouplingEstimator(),
): () => void {
  const c = { ...DEFAULT_BARGE_IN, ...cfg };
  const data = new Uint8Array(analyser.fftSize);
  const startedAt = performance.now();
  let sustainedFrom: number | null = null;
  let fired = false;

  const tick = () => {
    if (fired) return;
    const now = performance.now();

    // Only meaningful while Nova is actually speaking; otherwise the normal VAD
    // owns the microphone and this would double-trigger.
    if (!isTtsPlaying()) {
      sustainedFrom = null;
      return;
    }
    if (now - startedAt < c.graceMs) return;

    analyser.getByteTimeDomainData(data);
    let sum = 0;
    for (let i = 0; i < data.length; i += 1) {
      const v = (data[i] - 128) / 128;
      sum += v * v;
    }
    const mic = Math.sqrt(sum / data.length);
    const ttsRms = getTtsOutputRms();

    const coupling = estimator.value();
    const { speechLike, reason, predictedEcho } =
      frameIsSpeechLike(mic, ttsRms, c, coupling);

    // Only learn the room from frames that look like Nova alone. Feeding
    // candidate-interruption frames back in would teach the estimator that
    // Marcus's voice is normal echo.
    if (!speechLike) estimator.observe(mic, ttsRms, c.minTtsForEstimate);

    if (!speechLike) {
      sustainedFrom = null;
      return;
    }
    if (sustainedFrom === null) sustainedFrom = now;
    const sustained = now - sustainedFrom;
    if (sustained >= c.sustainMs) {
      fired = true;
      onCandidate({
        at: now, micLevel: mic, ttsRms,
        coupling: coupling ?? c.fallbackCoupling,
        predictedEcho, sustainedMs: sustained, reason,
      });
    }
  };

  const timer = window.setInterval(tick, c.frameMs);
  return () => window.clearInterval(timer);
}

// ── Telemetry ───────────────────────────────────────────────────────────────

/**
 * One barge-in attempt. Every field is a real observation or null — nothing is
 * inferred, so the acceptance run cannot report a latency it never measured.
 */
export type BargeInAttempt = {
  attempt: number;
  novaSentence: string | null;
  playbackStartedAt: number | null;
  speechDetectedAt: number | null;
  playbackDuckedAt: number | null;
  transcriptAt: number | null;
  transcript: string | null;
  classification: string | null;
  salvagedText: string | null;
  turnCancelled: boolean | null;
  staleAudioLeaked: boolean;
  newFirstAudioAt: number | null;
  coupling: number | null;
  micLevel: number | null;
  ttsRms: number | null;
  outcome: "success" | "missed" | "false-self-interrupt" | "echo-rejected" | "pending";
};

export function newAttempt(n: number): BargeInAttempt {
  return {
    attempt: n, novaSentence: null, playbackStartedAt: null, speechDetectedAt: null,
    playbackDuckedAt: null, transcriptAt: null, transcript: null, classification: null,
    salvagedText: null, turnCancelled: null, staleAudioLeaked: false,
    newFirstAudioAt: null, coupling: null, micLevel: null, ttsRms: null,
    outcome: "pending",
  };
}

export function stopLatencyMs(a: BargeInAttempt): number | null {
  if (a.speechDetectedAt === null || a.playbackDuckedAt === null) return null;
  return a.playbackDuckedAt - a.speechDetectedAt;
}

export function replyLatencyMs(a: BargeInAttempt): number | null {
  if (a.transcriptAt === null || a.newFirstAudioAt === null) return null;
  return a.newFirstAudioAt - a.transcriptAt;
}

export function percentile(vals: number[], p: number): number | null {
  if (!vals.length) return null;
  const s = [...vals].sort((x, y) => x - y);
  return s[Math.min(s.length - 1, Math.round((p / 100) * (s.length - 1)))];
}

/** Summary for the live acceptance run, so Marcus never computes a timing. */
export function summarize(attempts: BargeInAttempt[]) {
  const done = attempts.filter((a) => a.outcome !== "pending");
  const stops = done.map(stopLatencyMs).filter((x): x is number => x !== null);
  const replies = done.map(replyLatencyMs).filter((x): x is number => x !== null);
  const couplings = done.map((a) => a.coupling).filter((x): x is number => x !== null);
  return {
    total: done.length,
    successes: done.filter((a) => a.outcome === "success").length,
    missed: done.filter((a) => a.outcome === "missed").length,
    falseSelfInterrupts: done.filter((a) => a.outcome === "false-self-interrupt").length,
    echoesCorrectlyRejected: done.filter((a) => a.outcome === "echo-rejected").length,
    mixedSalvaged: done.filter((a) => a.classification === "mixed" && !!a.salvagedText).length,
    staleAudioLeaks: done.filter((a) => a.staleAudioLeaked).length,
    medianStopMs: percentile(stops, 50),
    p90StopMs: percentile(stops, 90),
    medianReplyMs: percentile(replies, 50),
    medianCoupling: percentile(couplings, 50),
  };
}
