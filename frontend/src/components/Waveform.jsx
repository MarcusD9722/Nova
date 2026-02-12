import React, { useRef, useEffect, useMemo } from "react";

/**
 * Waveform
 * Audio-reactive canvas for Nova’s voice:
 *  - source: microphone (MediaStream) or HTMLAudioElement (TTS) or silent oscillator
 *  - modes: "bars" | "dots" | "ring"
 *  - glow + transient “burst” particles
 *
 * All drawing + audio are guarded so nothing throws during render.
 */
export default function Waveform({
  mode = "bars",
  mediaStream,
  audioEl = null,
  fftSize = 1024,
  smoothing = 0.8,
  mirror = true,
  height = 90,
  theme = {
    primary: "#7C3AED",    // neon purple
    secondary: "#22D3EE",  // cyan
    glow: "#FFD700",       // gold
  },
}) {
  const canvasRef = useRef(null);
  const rafRef = useRef(0);
  const audioRef = useRef({ ctx: null, srcNode: null, analyser: null, data: null });
  const burstRef = useRef({ particles: [], lastEnergy: 0 });
  const resizeObsRef = useRef(null);

  // Safe FFT size
  const _fftSize = useMemo(() => {
    const pow2 = [256, 512, 1024, 2048];
    return pow2.includes(fftSize) ? fftSize : 1024;
  }, [fftSize]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const setupAudio = async () => {
      teardownAudio();

      const ctx = new (window.AudioContext || window.webkitAudioContext)();

      // ✅ Autoplay policies: resume once the user clicks/keys
      if (ctx.state === "suspended") {
        const resume = () => {
          ctx.resume().catch(() => {});
          window.removeEventListener("pointerdown", resume);
          window.removeEventListener("keydown", resume);
        };
        window.addEventListener("pointerdown", resume, { once: true });
        window.addEventListener("keydown", resume, { once: true });
      }

      const analyser = ctx.createAnalyser();
      analyser.fftSize = _fftSize;
      analyser.smoothingTimeConstant = Math.max(0, Math.min(0.99, smoothing));

      let srcNode = null;

      if (mediaStream) {
        srcNode = ctx.createMediaStreamSource(mediaStream);
      } else if (audioEl instanceof HTMLAudioElement) {
        srcNode = ctx.createMediaElementSource(audioEl);
        const resume = () => ctx.state === "suspended" && ctx.resume();
        audioEl.addEventListener("play", resume);
        audioEl.addEventListener("playing", resume);
      } else {
        // Silent oscillator so animation always runs (no audio source required)
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        gain.gain.value = 0;
        osc.frequency.value = 220;
        osc.start();
        osc.connect(gain).connect(analyser);
        srcNode = { disconnect: () => { try { osc.stop(); } catch {} } };
      }

      if (srcNode?.connect) {
        try { srcNode.connect(analyser); } catch {} // MediaElementSource can only connect once
      }

      audioRef.current = {
        ctx,
        srcNode,
        analyser,
        data: new Uint8Array(analyser.frequencyBinCount),
      };

      start();
    };

    const start = () => {
      stop();

      const dpr = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
      const ro = new ResizeObserver(() => {
        const parent = canvas.parentElement || document.body;
        const w = Math.floor(parent.clientWidth || 420);
        const h = height;
        canvas.width = Math.floor(w * dpr);
        canvas.height = Math.floor(h * dpr);
        canvas.style.width = `${w}px`;
        canvas.style.height = `${h}px`;
      });
      ro.observe(canvas.parentElement || canvas);
      resizeObsRef.current = ro;

      const loop = () => {
        drawFrame();
        rafRef.current = requestAnimationFrame(loop);
      };
      loop();
    };

    const stop = () => {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = 0;
      resizeObsRef.current?.disconnect();
      resizeObsRef.current = null;
    };

    const teardownAudio = () => {
      stop();
      const a = audioRef.current;
      try { a?.srcNode?.disconnect(); } catch {}
      try { a?.ctx?.close(); } catch {}
      audioRef.current = { ctx: null, srcNode: null, analyser: null, data: null };
    };

    const drawFrame = () => {
      const ctx2d = canvas.getContext("2d");
      const { analyser, data } = audioRef.current || {};
      if (!ctx2d) return;

      ctx2d.clearRect(0, 0, canvas.width, canvas.height);

      // Background gradient (subtle)
      const g = ctx2d.createLinearGradient(0, 0, canvas.width, canvas.height);
      g.addColorStop(0, "rgba(12,16,56,0.12)");
      g.addColorStop(1, "rgba(41,235,255,0.10)");
      ctx2d.fillStyle = g;
      ctx2d.fillRect(0, 0, canvas.width, canvas.height);

      // No analyser yet? show idle pulse
      if (!analyser || !data) { idlePulse(ctx2d, canvas); return; }

      analyser.getByteFrequencyData(data);
      const energy = averageEnergy(data);
      maybeBurst(energy, burstRef.current);              // ✅ defined below

      ctx2d.save();
      ctx2d.shadowColor = theme.glow || "#FFD700";
      ctx2d.shadowBlur = 18;

      if (mode === "ring")      drawRing(ctx2d, canvas, data, theme);
      else if (mode === "dots") drawDots(ctx2d, canvas, data, theme, mirror);
      else                      drawBars(ctx2d, canvas, data, theme, mirror);

      drawBursts(ctx2d, canvas, burstRef.current, theme); // ✅ defined below
      ctx2d.restore();
    };

    setupAudio();
    return () => teardownAudio();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mediaStream, audioEl, _fftSize, smoothing, mode, height, theme.primary, theme.secondary, theme.glow, mirror]);

  return (
    <div className="w-full">
      <canvas
        ref={canvasRef}
        style={{
          display: "block",
          margin: "8px auto",
          width: "100%",
          height: `${height}px`,
          filter: "drop-shadow(0 0 18px rgba(124,58,237,0.35))",
        }}
      />
    </div>
  );
}

/* ---------------- helpers (drawing + particles) ---------------- */

function averageEnergy(arr) { let s=0; for (let i=0;i<arr.length;i++) s+=arr[i]; return s/(arr.length||1); }
function bandAverage(data, start, end) {
  const s = Math.max(0, Math.min(data.length - 1, start));
  const e = Math.max(s + 1, Math.min(data.length, end));
  let sum = 0;
  for (let i = s; i < e; i++) sum += data[i];
  return sum / (e - s);
}

function roundRect(ctx, x, y, w, h, r) {
  const rr = Math.min(r, w * 0.5, h * 0.5);
  ctx.beginPath();
  ctx.moveTo(x + rr, y);
  ctx.arcTo(x + w, y, x + w, y + h, rr);
  ctx.arcTo(x + w, y + h, x, y + h, rr);
  ctx.arcTo(x, y + h, x, y, rr);
  ctx.arcTo(x, y, x + w, y, rr);
  ctx.closePath();
}

function drawBars(ctx, canvas, data, theme, mirror) {
  const w = canvas.width, h = canvas.height;
  const barCount = 64;
  const gap = 2;
  const innerW = w - gap * (mirror ? barCount * 2 - 1 : barCount - 1);
  const barW = innerW / (mirror ? barCount * 2 : barCount);
  const baseY = h * 0.5;

  for (let i = 0; i < barCount; i++) {
    const t = i / barCount;
    const start = Math.floor(Math.pow(t, 1.3) * data.length);
    const end = Math.floor(Math.pow((i + 1) / barCount, 1.3) * data.length);
    const v = bandAverage(data, start, end) / 255;
    const barH = (h * 0.42) * Math.pow(v, 0.8) + 2;

    const x = i * (barW + gap);
    const grd = ctx.createLinearGradient(0, baseY - barH, 0, baseY + barH);
    grd.addColorStop(0, theme.secondary || "#22D3EE");
    grd.addColorStop(1, theme.primary || "#7C3AED");
    ctx.fillStyle = grd;
    roundRect(ctx, x, baseY - barH, barW, barH * 2, Math.min(barW, 12));
    ctx.fill();

    if (mirror) {
      const xR = w - x - barW;
      roundRect(ctx, xR, baseY - barH, barW, barH * 2, Math.min(barW, 12));
      ctx.fill();
    }
  }
}

function drawDots(ctx, canvas, data, theme, mirror) {
  const w = canvas.width, h = canvas.height;
  const dotCount = 72;
  const baseY = h * 0.5;

  for (let i = 0; i < dotCount; i++) {
    const t = i / dotCount;
    const start = Math.floor(Math.pow(t, 1.1) * data.length);
    const end = Math.floor(Math.pow((i + 1) / dotCount, 1.1) * data.length);
    const v = bandAverage(data, start, end) / 255;
    const amp = (h * 0.35) * Math.pow(v, 0.9);
    const radius = 3 + 8 * Math.pow(v, 0.7);

    const x = i * (w / (mirror ? dotCount * 2 : dotCount));
    ctx.beginPath();
    ctx.arc(x, baseY - amp * 0.6, radius, 0, Math.PI * 2);
    ctx.fillStyle = `rgba(34,211,238,${0.6 + 0.4 * v})`;
    ctx.fill();

    if (mirror) {
      ctx.beginPath();
      ctx.arc(w - x, baseY + amp * 0.6, radius, 0, Math.PI * 2);
      ctx.fillStyle = theme.secondary || "#22D3EE";
      ctx.globalAlpha = 0.8;
      ctx.fill();
      ctx.globalAlpha = 1;
    }
  }
}

function drawRing(ctx, canvas, data, theme) {
  const w = canvas.width, h = canvas.height;
  const cx = w / 2, cy = h / 2;
  const radius = Math.min(w, h) * 0.35;
  const points = 96;
  const stroke = 2;

  ctx.lineWidth = stroke;
  ctx.strokeStyle = theme.primary || "#7C3AED";

  ctx.beginPath();
  for (let i = 0; i <= points; i++) {
    const t = i / points;
    const a = t * Math.PI * 2;
    const idx = Math.floor(t * (data.length - 1));
    const v = (data[idx] || 0) / 255;
    const r = radius * (1 + 0.22 * Math.pow(v, 0.8));
    const x = cx + r * Math.cos(a);
    const y = cy + r * Math.sin(a);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  }
  ctx.stroke();

  // soft glow ring
  ctx.beginPath();
  ctx.arc(cx, cy, radius * 1.02, 0, Math.PI * 2);
  ctx.strokeStyle = hexWithAlpha(theme.secondary || "#22D3EE", 0.35);
  ctx.lineWidth = 1;
  ctx.stroke();
}

function idlePulse(ctx, canvas) {
  const w = canvas.width, h = canvas.height;
  const cx = w / 2, cy = h / 2;
  const t = performance.now() / 1000;
  const r = (Math.min(w, h) * 0.32) * (1 + 0.03 * Math.sin(t * 2.4));

  ctx.beginPath();
  ctx.arc(cx, cy, r, 0, Math.PI * 2);
  ctx.strokeStyle = "rgba(124,58,237,0.35)";
  ctx.lineWidth = 2;
  ctx.stroke();
}

/* ------------- simple burst particle system ------------- */
function maybeBurst(energy, state) {
  const now = performance.now() / 1000;
  const prev = state.lastEnergy || 0;
  const delta = energy - prev;
  state.lastEnergy = energy;

  // Trigger when energy spikes
  if (delta > 14) {
    const count = 16 + Math.floor(Math.random() * 12);
    const particles = [];
    for (let i = 0; i < count; i++) {
      const ang = (i / count) * Math.PI * 2 + Math.random() * 0.3;
      const speed = 70 + Math.random() * 80;
      particles.push({
        x: 0, y: 0,
        vx: Math.cos(ang) * speed,
        vy: Math.sin(ang) * speed,
        life: 0,
        ttl: 0.5 + Math.random() * 0.6,
      });
    }
    state.particles.push(...particles);
  }
}

function drawBursts(ctx, canvas, state, theme) {
  const dt = 1 / 60;
  const cx = canvas.width / 2, cy = canvas.height / 2;

  const next = [];
  for (const p of state.particles) {
    p.life += dt;
    if (p.life > p.ttl) continue;
    p.x += p.vx * dt;
    p.y += p.vy * dt;
    const a = 1 - p.life / p.ttl;

    ctx.beginPath();
    ctx.arc(cx + p.x, cy + p.y, 2 + 2 * a, 0, Math.PI * 2);
    ctx.fillStyle = hexWithAlpha(theme.glow || "#FFD700", 0.5 * a);
    ctx.fill();

    next.push(p);
  }
  state.particles = next;
}

function hexWithAlpha(hex, alpha = 1) {
  const c = hex.replace("#", "");
  const bigint = parseInt(c.length === 3 ? c.split("").map(ch => ch + ch).join("") : c, 16);
  const r = (bigint >> 16) & 255;
  const g = (bigint >> 8) & 255;
  const b = bigint & 255;
  return `rgba(${r},${g},${b},${alpha})`;
}
