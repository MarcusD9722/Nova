import { useEffect, useRef, useState } from "react";
import { acquireMicStreamHandle } from "../voice/recorder";

/**
 * useMicLevel
 * Lightweight mic RMS meter for "always listening" UI.
 * - enabled: when false, shuts down mic stream + audio context
 * - muted: when true, meter returns 0 (and can optionally stop processing)
 */
export default function useMicLevel({ enabled = true, muted = false } = {}) {
  const [level, setLevel] = useState(0);
  const rafRef = useRef(0);
  const ctxRef = useRef(null);
  const analyserRef = useRef(null);
  const srcRef = useRef(null);
  const micHandleRef = useRef(null);
  const dataRef = useRef(null);

  useEffect(() => {
    let cancelled = false;

    async function start() {
      if (!enabled) return;
      try {
        const h = await acquireMicStreamHandle({ debugTag: "meter" });
        const stream = h.stream;
        if (cancelled) {
          try { h.release(); } catch {}
          return;
        }
        micHandleRef.current = h;

        const AudioCtx = window.AudioContext || window.webkitAudioContext;
        const ctx = new AudioCtx();
        ctxRef.current = ctx;

        // Some runtimes require an explicit user gesture to resume.
        const tryResume = async () => {
          try {
            if (ctx.state === "suspended") await ctx.resume();
          } catch {}
        };
        try { await tryResume(); } catch {}
        const onGesture = () => {
          tryResume();
        };
        window.addEventListener("pointerdown", onGesture, { once: true });
        window.addEventListener("keydown", onGesture, { once: true });

        const src = ctx.createMediaStreamSource(stream);
        srcRef.current = src;

        const analyser = ctx.createAnalyser();
        analyser.fftSize = 1024;
        analyser.smoothingTimeConstant = 0.85;
        analyserRef.current = analyser;

        src.connect(analyser);
        const data = new Uint8Array(analyser.fftSize);
        dataRef.current = data;

        const loop = () => {
          if (!analyserRef.current || !dataRef.current) return;
          if (!enabled) return;

          if (muted) {
            setLevel(0);
          } else {
            const a = analyserRef.current;
            const d = dataRef.current;
            a.getByteTimeDomainData(d);
            // RMS in [0..1]
            let sum = 0;
            for (let i = 0; i < d.length; i++) {
              const v = (d[i] - 128) / 128;
              sum += v * v;
            }
            const rms = Math.sqrt(sum / d.length);
            // compress for nicer UI
            const compressed = Math.min(1, Math.pow(rms * 2.2, 0.6));
            setLevel(compressed);
          }

          rafRef.current = requestAnimationFrame(loop);
        };

        rafRef.current = requestAnimationFrame(loop);
      } catch {
        // mic permission denied / no device
        setLevel(0);
      }
    }

    function stop() {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = 0;
      try { srcRef.current?.disconnect(); } catch {}
      try { analyserRef.current?.disconnect?.(); } catch {}
      try { ctxRef.current?.close(); } catch {}
      ctxRef.current = null;
      analyserRef.current = null;
      srcRef.current = null;
      dataRef.current = null;
      try { micHandleRef.current?.release?.(); } catch {}
      micHandleRef.current = null;
    }

    if (enabled) start();
    else stop();

    return () => {
      cancelled = true;
      stop();
    };
  }, [enabled, muted]);

  return level;
}
