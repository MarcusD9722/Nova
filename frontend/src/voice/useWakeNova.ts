// src/voice/useWakeNova.ts
import { useCallback, useEffect, useRef } from "react";
import { acquireMicStreamHandle, recordVoiceActivityFromStreamToBlob, transcribeBlobDetailed } from "./recorder";

export type WakeState = "idle" | "listening_wake" | "listening_fallback";

function sleep(ms: number) {
  return new Promise((r) => setTimeout(r, ms));
}

function voiceDebugEnabled(): boolean {
  try {
    // @ts-ignore
    if ((window as any)?.__NOVA_VOICE_DEBUG) return true;
  } catch {}
  try {
    return window.localStorage?.getItem("novaVoiceDebug") === "1";
  } catch {}
  return false;
}

function vlog(tag: string, ...args: any[]) {
  if (!voiceDebugEnabled()) return;
  // eslint-disable-next-line no-console
  console.log(`[voice:${tag}]`, ...args);
}

function wakeLog(...args: any[]) {
  // Always-on, but only emits useful signal (we call it sparingly).
  // eslint-disable-next-line no-console
  console.debug("[wake]", ...args);
}

function isElectronRuntime(): boolean {
  try {
    // Typical Electron renderer hint
    // @ts-ignore
    if ((window as any)?.process?.versions?.electron) return true;
  } catch {}
  try {
    const ua = navigator.userAgent || "";
    return ua.toLowerCase().includes(" electron/");
  } catch {}
  return false;
}

function normalizeText(s: string): string {
  return String(s || "")
    .toLowerCase()
    .replace(/[\u2019']/g, "'")
    .replace(/[^a-z0-9\s']/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function hasAnyLetter(s: string): boolean {
  try {
    // Unicode property escapes are supported in modern Chromium/Electron.
    return /\p{L}/u.test(s);
  } catch {
    return /[a-zA-Z]/.test(s);
  }
}

function isIgnorableTranscript(raw: string): boolean {
  const s = String(raw || "").trim();
  if (!s) return true;

  // Common STT pattern for non-speech: "(ambient noise)", "(clicking)", etc.
  if (/^\([^\n\r]*\)$/.test(s)) return true;

  // Ignore transcripts that contain no letters at all.
  if (!hasAnyLetter(s)) return true;

  return false;
}

function matchesWake(text: string, primaryPhrase: string): boolean {
  const t = normalizeText(text);
  const p = normalizeText(primaryPhrase);
  if (!t || !p) return false;

  const variants = new Set([p, "hey nova", "hi nova", "okay nova", "ok nova"]);
  for (const phrase of variants) {
    const words = phrase.split(" ").filter(Boolean);
    if (!words.length) continue;
    const escaped = words.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
    const re = new RegExp(`\\b${escaped.join("\\s+")}\\b`);
    if (re.test(t)) return true;
  }

  return /\bnova\b/.test(t) && /\b(hey|hi|okay|ok)\b/.test(t);
}

function formatWakeError(error: any): string {
  const raw = String(error?.message || error || "").trim();
  if (!raw) return "Wake error: STT request failed";

  const normalized = raw.replace(/\s+/g, " ").trim();

  try {
    const parsed = JSON.parse(normalized);
    const detail = parsed?.detail;
    if (typeof detail === "string" && detail.trim()) {
      return `Wake error: ${detail.trim().slice(0, 180)}`;
    }
  } catch {}

  const detailMatch = normalized.match(/"detail"\s*:\s*"([^"]+)"/i);
  if (detailMatch?.[1]) {
    return `Wake error: ${detailMatch[1].trim().slice(0, 180)}`;
  }

  return `Wake error: ${normalized.slice(0, 180)}`;
}

/**
 * Wake word listener for "hey nova".
 *
 * Primary path: Web Speech API SpeechRecognition (if available).
 * Fallback path (Electron/offline): short-chunk local STT loop via backend /transcribe.
 */
export function useWakeNova(
  onWake: () => void,
  phrase: string = "hey nova",
  opts: { onStatus?: (msg: string) => void } = {}
) {
  const recRef = useRef<any>(null);
  const runningRef = useRef<boolean>(false);
  const lastWakeAtRef = useRef<number>(0);
  const cooldownUntilRef = useRef<number>(0);
  const onWakeRef = useRef(onWake);
  onWakeRef.current = onWake;
  const onStatusRef = useRef<(msg: string) => void>(() => {});
  onStatusRef.current = typeof opts?.onStatus === "function" ? opts.onStatus : () => {};
  const micHandleRef = useRef<any>(null);
  const lastUiNoteAtRef = useRef<number>(0);

  const stopWake = useCallback(() => {
    runningRef.current = false;

    // release shared mic stream handle (fallback path)
    try {
      micHandleRef.current?.release?.();
    } catch {}
    micHandleRef.current = null;

    const r = recRef.current;
    recRef.current = null;
    if (!r) return;

    try {
      onStatusRef.current?.("Wake stopped");
    } catch {}

    // SpeechRecognition path
    try { r.onend = null; } catch {}
    try { r.onerror = null; } catch {}
    try { r.onresult = null; } catch {}
    try { r.stop?.(); } catch {}
    try { r.abort?.(); } catch {}
  }, []);

  const startWake = useCallback(() => {
    // idempotent
    if (runningRef.current) return;

    const tag = "wake";
    const electron = isElectronRuntime();

    try {
      onStatusRef.current?.("Wake listening… (say: Hey Nova)");
    } catch {}

    const SR: any =
      (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;

    // ---------- Primary: SpeechRecognition ----------
    // In Electron, treat Web Speech API as unavailable/unreliable and prefer backend STT.
    if (!electron && SR) {
      try {
        const recognition = new SR();
        recRef.current = recognition;
        runningRef.current = true;

        vlog(tag, "starting SpeechRecognition wake listener");

        recognition.lang = "en-US";
        recognition.continuous = true;
        recognition.interimResults = true;

        recognition.onresult = (event: any) => {
          try {
            const res = event.results?.[event.results.length - 1];
            const txt = String(res?.[0]?.transcript || "").toLowerCase();
            if (!txt) return;

            const p = phrase.toLowerCase();
            if (txt.includes(p)) {
              const now = Date.now();
              if (now - lastWakeAtRef.current < 1800) return;
              lastWakeAtRef.current = now;
              onWakeRef.current?.();
            }
          } catch {}
        };

        recognition.onerror = () => {
          // Keep silent; onend will restart
        };

        recognition.onend = () => {
          // Restart to behave "always listening"
          if (!runningRef.current) return;
          try { recognition.start(); } catch {}
        };

        try { recognition.start(); } catch {}
        return;
      } catch (e) {
        console.warn("SpeechRecognition start failed; falling back to STT wake loop.", e);
        // fall through to fallback
      }
    }

    // ---------- Fallback: chunked local STT loop ----------
    runningRef.current = true;
    recRef.current = { type: "fallback" };

    vlog(tag, "starting backend-STT wake loop", {
      phrase,
      electron,
      cooldownMs: 5000,
      mode: "vad_fallback",
    });
    wakeLog("wake loop start", { phrase, electron });

    (async () => {
      const betweenChunksMs = 160;
      const cooldownMs = 5000;

      // Acquire and hold mic stream for the whole wake session.
      try {
        const h = await acquireMicStreamHandle({ debugTag: tag });
        micHandleRef.current = h as any;
      } catch (e: any) {
        vlog(tag, "failed to acquire mic stream", { name: e?.name, message: e?.message });
        wakeLog("mic acquire failed", { name: e?.name, message: e?.message });
        try {
          onStatusRef.current?.("Wake error: mic permission denied/unavailable");
        } catch {}
        runningRef.current = false;
        return;
      }

      const handle = micHandleRef.current;
      if (!handle?.stream) {
        runningRef.current = false;
        return;
      }

      while (runningRef.current) {
        try {
          const now = Date.now();
          if (cooldownUntilRef.current && now < cooldownUntilRef.current) {
            // Avoid spamming console; only log occasionally.
            if ((cooldownUntilRef.current - now) > 1000) {
              wakeLog("cooldown active", { msLeft: cooldownUntilRef.current - now });
            }
            await sleep(250);
            continue;
          }

          // Record short chunk (keeps CPU reasonable).
          const blob = await recordVoiceActivityFromStreamToBlob(handle.stream, {
            maxMs: 2200,
            minSpeechMs: 180,
            trailingSilenceMs: 380,
            speechThreshold: 0.02,
            startTimeoutMs: 1800,
            timesliceMs: 200,
            debugTag: tag,
          });
          if (!runningRef.current) break;

          // Transcribe via backend
          const stt = await transcribeBlobDetailed(blob, { path: "/stt", debugTag: tag });
          const txtRaw = String(stt?.text || "").trim();

          if (!txtRaw || stt?.empty || isIgnorableTranscript(txtRaw)) {
            await sleep(betweenChunksMs);
            continue;
          }

          const txtNorm = normalizeText(txtRaw);
          if (txtNorm) {
            wakeLog("chunk transcript", {
              text: txtNorm,
              durationMs: stt?.durationMs,
              sampleRate: stt?.sampleRate,
            });
          }

          // UI hinting (throttled): surface what we heard so users can debug wake reliability.
          try {
            const nowUi = Date.now();
            const shouldNote = txtNorm && (!lastUiNoteAtRef.current || nowUi - lastUiNoteAtRef.current > 6000);
            if (shouldNote) {
              lastUiNoteAtRef.current = nowUi;
              onStatusRef.current?.(`Heard: "${txtRaw.slice(0, 80)}"`);
            }
          } catch {}

          if (matchesWake(txtNorm, phrase)) {
            const tnow = Date.now();
            if (tnow - lastWakeAtRef.current < 500) {
              // ultra-short de-dupe guard
              await sleep(120);
              continue;
            }
            lastWakeAtRef.current = tnow;
            cooldownUntilRef.current = tnow + cooldownMs;
            vlog(tag, "wake phrase matched", { phrase, cooldownMs });
            wakeLog("WAKE MATCH", { phrase, transcript: txtNorm });

            // Hard stop wake loop and release resources immediately.
            runningRef.current = false;
            try {
              micHandleRef.current?.release?.();
            } catch {}
            micHandleRef.current = null;

            try {
              onWakeRef.current?.();
            } catch {}
            break;
          } else {
            // Light pacing between chunks
            await sleep(betweenChunksMs);
          }
        } catch (e: any) {
          // Backoff on errors (permissions, backend down, etc.)
          const message = formatWakeError(e);
          vlog(tag, "wake loop error", { name: e?.name, message: e?.message || String(e), display: message });
          wakeLog("wake loop error", { name: e?.name, message: e?.message || String(e), display: message });
          try {
            const nowUi = Date.now();
            if (!lastUiNoteAtRef.current || nowUi - lastUiNoteAtRef.current > 6000) {
              lastUiNoteAtRef.current = nowUi;
              onStatusRef.current?.(message);
            }
          } catch {}
          await sleep(900);
        }
      }

      vlog(tag, "wake loop exiting");
      wakeLog("wake loop exit");
    })();
  }, [phrase]);

  // Cleanup on unmount
  useEffect(() => () => stopWake(), [stopWake]);

  return { startWake, stopWake };
}
