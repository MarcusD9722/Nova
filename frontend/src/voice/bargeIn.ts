/**
 * P0 stage 1: decide FAST whether Marcus is talking over Nova.
 *
 * Barge-in is two-stage on purpose, because the two requirements conflict:
 *
 *   Stage 1 (this file)  must be FAST. Nova has to stop talking within a
 *                        couple of hundred milliseconds or the interruption
 *                        feels broken. It has no transcript, only levels.
 *
 *   Stage 2 (backend)    must be ACCURATE. core/voice/echo.py runs STT and
 *                        decides ECHO / USER / MIXED, and only it can salvage
 *                        the real words out of a transcript that begins as
 *                        Nova's own voice.
 *
 * So this gate is allowed to be wrong, and is designed around being wrong:
 * it ATTENUATES playback rather than cancelling the turn. If stage 2 comes
 * back ECHO, the volume is restored and Nova carries on. Nothing is destroyed
 * on the strength of a level reading alone.
 *
 * The hard problem is that the microphone hears Nova. Two signals separate her
 * voice from his:
 *
 *   1. Nova's own output level, from getTtsOutputLevel(). When the mic rises
 *      in step with her speech it is almost certainly echo; when it rises while
 *      she is quiet, it is not.
 *   2. Sustained duration. A door, a keyboard, a cough is a spike. Speech holds
 *      energy across many frames.
 *
 * Neither alone is enough. A loud speaker near the mic defeats (1); a long
 * stretch of Nova's own speech defeats (2). Requiring both is what keeps Nova
 * from interrupting herself.
 */

import { getTtsOutputLevel, isTtsPlaying } from "./recorder";

export type BargeInConfig = {
  /** Mic RMS below this is never speech, whatever else is true. */
  floor: number;
  /**
   * How far above Nova's own output the mic must sit before her voice stops
   * explaining the reading. 1.0 = "as loud as her"; higher = more conservative.
   */
  overTtsRatio: number;
  /** Mic level that counts as speech when Nova is silent. */
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
  // Measured starting point, not a tuned constant. The live acceptance run
  // (tests/live_barge_in_harness) is what sets this honestly: too low and Nova
  // interrupts herself, too high and Marcus has to shout.
  overTtsRatio: 1.6,
  quietThreshold: 0.025,
  sustainMs: 180,
  graceMs: 250,
  frameMs: 20,
};

/**
 * The whole stage-1 decision for a single frame, as a pure function.
 *
 * Split out so it can be tested without a browser, an analyser or a
 * microphone — the timing wrapper around it genuinely needs all three, but the
 * judgement it encodes is what decides whether Nova interrupts herself, and
 * that must be verifiable offline.
 */
export function frameIsSpeechLike(
  mic: number,
  tts: number,
  cfg: BargeInConfig,
): { speechLike: boolean; reason: BargeInEvent["reason"] } {
  if (mic < cfg.floor) return { speechLike: false, reason: "tts-quiet" };
  if (tts > 0.01) {
    // Nova is audible: the mic must clearly exceed what her own voice explains.
    return { speechLike: mic > tts * cfg.overTtsRatio, reason: "over-tts" };
  }
  // Nova is between clips: ordinary speech detection applies.
  return { speechLike: mic >= cfg.quietThreshold, reason: "tts-quiet" };
}

export type BargeInEvent = {
  at: number;
  micLevel: number;
  ttsLevel: number;
  sustainedMs: number;
  reason: "over-tts" | "tts-quiet";
};

/**
 * Watches a live analyser and calls `onCandidate` the first time it believes
 * Marcus has started talking over Nova.
 *
 * Returns a stop() function. Deliberately does NOT touch playback, cancel a
 * turn, or call the backend — the caller owns those decisions, so this stays
 * testable and cannot half-perform an interruption.
 */
export function watchForSpeechOverPlayback(
  analyser: AnalyserNode,
  onCandidate: (ev: BargeInEvent) => void,
  cfg: Partial<BargeInConfig> = {},
): () => void {
  const c = { ...DEFAULT_BARGE_IN, ...cfg };
  const data = new Uint8Array(analyser.fftSize);
  const startedAt = performance.now();
  let sustainedFrom: number | null = null;
  let fired = false;
  let timer: number | null = null;

  const tick = () => {
    if (fired) return;
    const now = performance.now();

    // Only meaningful while Nova is actually speaking; otherwise the normal
    // VAD owns the microphone and this would double-trigger.
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
    const tts = getTtsOutputLevel();

    const { speechLike, reason } = frameIsSpeechLike(mic, tts, c);

    if (!speechLike) {
      sustainedFrom = null;
      return;
    }
    if (sustainedFrom === null) sustainedFrom = now;
    const sustained = now - sustainedFrom;
    if (sustained >= c.sustainMs) {
      fired = true;
      onCandidate({ at: now, micLevel: mic, ttsLevel: tts, sustainedMs: sustained, reason });
    }
  };

  timer = window.setInterval(tick, c.frameMs);
  return () => {
    if (timer !== null) window.clearInterval(timer);
    timer = null;
  };
}

/**
 * Barge-in telemetry for one attempt.
 *
 * Every field is a real observation or null — nothing is inferred, so the live
 * acceptance run cannot accidentally report a latency it never measured.
 */
export type BargeInAttempt = {
  attempt: number;
  novaSentence: string | null;
  playbackStartedAt: number | null;
  speechDetectedAt: number | null;
  playbackStoppedAt: number | null;
  transcriptAt: number | null;
  transcript: string | null;
  classification: string | null;
  salvagedText: string | null;
  turnCancelled: boolean | null;
  staleAudioLeaked: boolean;
  newFirstAudioAt: number | null;
  outcome: "success" | "missed" | "false-self-interrupt" | "echo-rejected" | "pending";
};

export function newAttempt(n: number): BargeInAttempt {
  return {
    attempt: n,
    novaSentence: null,
    playbackStartedAt: null,
    speechDetectedAt: null,
    playbackStoppedAt: null,
    transcriptAt: null,
    transcript: null,
    classification: null,
    salvagedText: null,
    turnCancelled: null,
    staleAudioLeaked: false,
    newFirstAudioAt: null,
    outcome: "pending",
  };
}

export function stopLatencyMs(a: BargeInAttempt): number | null {
  if (a.speechDetectedAt === null || a.playbackStoppedAt === null) return null;
  return a.playbackStoppedAt - a.speechDetectedAt;
}

export function replyLatencyMs(a: BargeInAttempt): number | null {
  if (a.transcriptAt === null || a.newFirstAudioAt === null) return null;
  return a.newFirstAudioAt - a.transcriptAt;
}

function percentile(vals: number[], p: number): number | null {
  if (!vals.length) return null;
  const s = [...vals].sort((x, y) => x - y);
  const k = Math.min(s.length - 1, Math.round((p / 100) * (s.length - 1)));
  return s[k];
}

/** Summary for the live acceptance run, so Marcus never computes a timing. */
export function summarize(attempts: BargeInAttempt[]) {
  const done = attempts.filter((a) => a.outcome !== "pending");
  const stops = done.map(stopLatencyMs).filter((x): x is number => x !== null);
  const replies = done.map(replyLatencyMs).filter((x): x is number => x !== null);
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
  };
}
