// src/voice/lipSync.ts
// Shared level -> jaw mapping used identically by every avatar renderer
// (GlbAvatarSlot, HolographicBust) so lip sync looks and behaves the same
// regardless of which one is active, with a single smoothing stage.

import { getTtsOutputLevel, isTtsPlaying } from "./recorder";

const SILENCE_THRESHOLD = 0.015;
const LEVEL_GAIN = 2.1;
const ATTACK = 0.5;
const RELEASE = 0.2;

export type JawSyncState = { value: number };

export function createJawSyncState(initial = 0): JawSyncState {
  return { value: initial };
}

/**
 * Compute this frame's target jaw-open amount (0..1).
 *
 * Priority order:
 * 1. An explicit driven value (real viseme/jaw data from the pipeline).
 * 2. The live TTS output level, while audio is actually playing.
 * 3. A calm procedural flap, but ONLY while audio is actually playing (gated
 *    on `isTtsPlaying()`, not the derived `speaking` boolean, so this never
 *    latches into a fake flutter after playback has genuinely ended).
 * 4. Fully closed otherwise -- no canned wiggle.
 */
export function jawTargetFor(opts: {
  externalJaw?: number | null;
  speaking: boolean;
  audioEnergy?: number;
  time: number;
  fallbackAmplitude?: number;
  fallbackSpeed?: number;
}): number {
  const {
    externalJaw,
    speaking,
    audioEnergy = 0,
    time,
    fallbackAmplitude = 0.3,
    fallbackSpeed = 9.5,
  } = opts;

  if (externalJaw != null) return externalJaw;

  const playing = isTtsPlaying();
  if (!playing) return 0;

  const level = getTtsOutputLevel();
  if (level > SILENCE_THRESHOLD) {
    return Math.min(1, level * LEVEL_GAIN);
  }
  if (speaking) {
    // Real audio is playing but momentarily below the noise floor (e.g. a
    // brief pause between words) -- a small procedural flap reads better
    // than a hard cut to closed.
    return 0.1 + Math.abs(Math.sin(time * fallbackSpeed)) * (0.14 + audioEnergy * fallbackAmplitude);
  }
  return 0;
}

/** Single attack/release smoothing stage shared by every avatar renderer. */
export function smoothJaw(state: JawSyncState, target: number): number {
  const smoothing = target > state.value ? ATTACK : RELEASE;
  state.value += (target - state.value) * smoothing;
  return state.value;
}
