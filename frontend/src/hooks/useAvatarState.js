import { useEffect, useMemo, useState } from "react";

export const AVATAR_STATES = Object.freeze(["idle", "listening", "thinking", "speaking"]);

export const AVATAR_STATE_CONFIG = Object.freeze({
  idle: { glow: 0.72, eyeGlow: 1.1, motion: 0.55, particlePull: 0 },
  listening: { glow: 1, eyeGlow: 1.85, motion: 1.05, particlePull: 0.08 },
  thinking: { glow: 0.92, eyeGlow: 1.28, motion: 0.86, particlePull: 0.42 },
  speaking: { glow: 1.18, eyeGlow: 1.52, motion: 0.92, particlePull: 0.16 },
});

const clamp01 = (value) => Math.min(1, Math.max(0, Number(value) || 0));

export function useReducedMotionPreference() {
  const [reducedMotion, setReducedMotion] = useState(false);

  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return undefined;
    const mediaQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
    const updatePreference = () => setReducedMotion(mediaQuery.matches);
    updatePreference();
    mediaQuery.addEventListener?.("change", updatePreference);
    return () => mediaQuery.removeEventListener?.("change", updatePreference);
  }, []);

  return reducedMotion;
}

export default function useAvatarState({
  state = "idle",
  micLevel = 0,
  ttsPlaying = false,
  lipSync = null,
} = {}) {
  const normalizedState = AVATAR_STATES.includes(state) ? state : "idle";
  const effectiveState = ttsPlaying ? "speaking" : normalizedState;
  const reducedMotion = useReducedMotionPreference();

  return useMemo(() => {
    const externalJaw = lipSync?.jawOpen;
    const audioEnergy = Math.max(clamp01(micLevel), ttsPlaying ? 0.38 : 0);

    return {
      state: effectiveState,
      config: AVATAR_STATE_CONFIG[effectiveState],
      audioEnergy,
      reducedMotion,
      facial: {
        jawOpen: externalJaw == null ? null : clamp01(externalJaw),
        viseme: lipSync?.viseme || "sil",
        expression: lipSync?.expression || (effectiveState === "thinking" ? "focus" : "neutral"),
        blink: lipSync?.blink == null ? null : clamp01(lipSync.blink),
      },
    };
  }, [effectiveState, lipSync, micLevel, reducedMotion, ttsPlaying]);
}
