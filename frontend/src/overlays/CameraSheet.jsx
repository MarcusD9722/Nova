import React, { useEffect, useRef, useState } from "react";

const MAX_VISION_EDGE = 896;

// API base resolution (same idea as ChatPanel)
const API_BASE = (() => {
  try {
    if (import.meta?.env?.DEV) return "";
  } catch {}
  try {
    const fromEnv = import.meta?.env?.VITE_API_BASE ? String(import.meta.env.VITE_API_BASE) : "";
    if (fromEnv) return fromEnv.replace(/\/$/, "");
  } catch {}
  try {
    const w = window;
    const fromWindow = w && w.__NOVA_API_BASE ? String(w.__NOVA_API_BASE) : "";
    if (fromWindow) return fromWindow.replace(/\/$/, "");
  } catch {}
  return "http://localhost:8008";
})();

export default function CameraSheet({
  stream,
  status,
  gesturesOn,
  onToggleGestures,
  handStatus,
  handsDetected,
  pinchHoldMs,
  pinchStrength,
  rawCursor,
  rawPinchRatio,
  calibrationSession,
  calibrationProfile,
  currentCalibrationStep,
  onStartCalibration,
  onCaptureCalibrationStep,
  onCancelCalibration,
  onResetCalibration,
  desktopControlEnabled,
  desktopControlAvailable,
  onToggleDesktopControl,
}) {
  const videoRef = useRef(null);
  const [question, setQuestion] = useState("Identify the key objects and what is happening in the scene.");
  const [result, setResult] = useState("");
  const [busy, setBusy] = useState(false);
  const [visionStatus, setVisionStatus] = useState({ enabled: true, reason: "" });

  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;
    if (stream) v.srcObject = stream;
    return () => {
      try { v.srcObject = null; } catch {}
    };
  }, [stream]);

  useEffect(() => {
    let cancelled = false;

    async function loadVisionStatus() {
      try {
        const resp = await fetch(`${API_BASE}/health`);
        if (!resp.ok) return;
        const data = await resp.json();
        const next = data?.vision;
        if (!cancelled && next && typeof next === "object") {
          setVisionStatus({
            enabled: Boolean(next.enabled),
            reason: String(next.reason || ""),
          });
        }
      } catch {
        // Leave the button enabled if health is unreachable; analyze() will surface the real error.
      }
    }

    loadVisionStatus();
    return () => {
      cancelled = true;
    };
  }, []);

  async function readError(resp) {
    try {
      const data = await resp.json();
      if (data?.detail) return String(data.detail);
      if (data?.text) return String(data.text);
    } catch {}
    try {
      const text = await resp.text();
      if (text) return text;
    } catch {}
    return `Request failed (${resp.status})`;
  }

  async function captureFrameBlob() {
    const v = videoRef.current;
    if (!v) throw new Error("Video not ready");
    const sourceWidth = v.videoWidth || 640;
    const sourceHeight = v.videoHeight || 480;
    const scale = Math.min(1, MAX_VISION_EDGE / Math.max(sourceWidth, sourceHeight));
    const width = Math.max(1, Math.round(sourceWidth * scale));
    const height = Math.max(1, Math.round(sourceHeight * scale));

    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;

    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("Could not create frame buffer");
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = "high";

    // Un-mirror the frame to match the on-screen preview.
    ctx.translate(width, 0);
    ctx.scale(-1, 1);
    ctx.drawImage(v, 0, 0, sourceWidth, sourceHeight, 0, 0, width, height);

    return new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.82));
  }

  async function analyze() {
    if (!visionStatus.enabled) {
      setResult(visionStatus.reason || "Vision is not configured.");
      return;
    }
    if (!stream) {
      setResult("Camera is off.");
      return;
    }
    setBusy(true);
    setResult("");
    try {
      const blob = await captureFrameBlob();
      if (!blob) throw new Error("Could not capture frame");

      const fd = new FormData();
      fd.append("file", blob, "frame.jpg");

      const q = (question || "").trim() || "Describe the image in detail.";
      const resp = await fetch(`${API_BASE}/vision/analyze?question=${encodeURIComponent(q)}`, {
        method: "POST",
        body: fd,
      });
      if (!resp.ok) throw new Error(await readError(resp));
      const data = await resp.json();
      setResult(String(data?.text || ""));
    } catch (e) {
      setResult(String(e?.message || e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-3 text-nova-gold">
      <div className="text-xs text-nova-gold/70">Status: {status}</div>

      <div className="rounded-2xl border border-nova-gold/15 bg-black/25 overflow-hidden">
        <video
          ref={videoRef}
          autoPlay
          playsInline
          muted
          className="w-full h-[240px] object-cover -scale-x-100"
        />
      </div>

      <div className="space-y-2">
        <div className="rounded-xl border border-nova-gold/15 bg-black/20 p-3 space-y-3">
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="text-xs text-nova-gold/60 uppercase tracking-[0.24em]">Calibration</div>
              <div className="text-sm text-nova-gold">
                {calibrationSession?.active ? "Calibration in progress" : calibrationProfile ? "Calibration saved" : "Using default hand profile"}
              </div>
            </div>
            {!calibrationSession?.active ? (
              <button
                type="button"
                onClick={onStartCalibration}
                className="rounded-xl border border-nova-gold/20 bg-black/25 px-3 py-2 text-sm text-nova-gold hover:bg-black/35"
              >
                {calibrationProfile ? "Recalibrate" : "Start calibration"}
              </button>
            ) : null}
          </div>

          <div className="flex items-center justify-between gap-3 text-xs text-nova-gold/70">
            <span>Raw X/Y: {Math.round((rawCursor?.x || 0) * 1000) / 1000}, {Math.round((rawCursor?.y || 0) * 1000) / 1000}</span>
            <span>Raw pinch: {Math.round((rawPinchRatio || 0) * 1000) / 1000}</span>
          </div>

          {calibrationProfile?.cursorBounds ? (
            <div className="text-xs text-nova-gold/65">
              Bounds: left {calibrationProfile.cursorBounds.left.toFixed(3)} • right {calibrationProfile.cursorBounds.right.toFixed(3)} • top {calibrationProfile.cursorBounds.top.toFixed(3)} • bottom {calibrationProfile.cursorBounds.bottom.toFixed(3)}
            </div>
          ) : null}

          {calibrationProfile?.pinch ? (
            <div className="text-xs text-nova-gold/65">
              Pinch: open {calibrationProfile.pinch.open.toFixed(3)} • closed {calibrationProfile.pinch.closed.toFixed(3)} • press {calibrationProfile.pinch.pressThreshold.toFixed(3)} • release {calibrationProfile.pinch.releaseThreshold.toFixed(3)}
            </div>
          ) : null}

          {calibrationSession?.active ? (
            <div className="rounded-xl border border-cyan-400/15 bg-cyan-500/10 p-3 space-y-3">
              <div className="text-xs text-cyan-100/80 uppercase tracking-[0.2em]">
                Step {Math.min((calibrationSession.stepIndex || 0) + 1, 7)} of 7
              </div>
              <div className="text-sm text-cyan-50">
                {currentCalibrationStep?.title || "Calibration step"}
              </div>
              <div className="text-xs text-cyan-100/80">
                {currentCalibrationStep?.detail || "Hold your hand still, then capture this step."}
              </div>

              {calibrationSession?.error ? (
                <div className="rounded-lg border border-amber-400/20 bg-amber-500/10 px-3 py-2 text-xs text-amber-100">
                  {calibrationSession.error}
                </div>
              ) : null}

              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={onCaptureCalibrationStep}
                  disabled={calibrationSession.capturing}
                  className="rounded-xl border border-cyan-300/20 bg-black/25 px-3 py-2 text-sm text-cyan-50 hover:bg-black/35 disabled:opacity-50"
                >
                  {calibrationSession.capturing ? "Capturing…" : `Capture ${currentCalibrationStep?.title || "Step"}`}
                </button>
                <button
                  type="button"
                  onClick={onCancelCalibration}
                  disabled={calibrationSession.capturing}
                  className="rounded-xl border border-nova-gold/20 bg-black/25 px-3 py-2 text-sm text-nova-gold hover:bg-black/35 disabled:opacity-50"
                >
                  Cancel
                </button>
              </div>
            </div>
          ) : null}

          {!calibrationSession?.active && calibrationProfile ? (
            <div className="flex justify-end">
              <button
                type="button"
                onClick={onResetCalibration}
                className="rounded-xl border border-red-400/20 bg-red-500/10 px-3 py-2 text-sm text-red-100 hover:bg-red-500/15"
              >
                Reset calibration
              </button>
            </div>
          ) : null}
        </div>

        <div className="rounded-xl border border-nova-gold/15 bg-black/20 p-3 space-y-3">
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="text-xs text-nova-gold/60 uppercase tracking-[0.24em]">Hand Control</div>
              <div className="text-sm text-nova-gold">
                {gesturesOn ? "Active" : "Inactive"} • {handStatus || "off"}
              </div>
            </div>
            <button
              type="button"
              onClick={onToggleGestures}
              className="rounded-xl border border-nova-gold/20 bg-black/25 px-3 py-2 text-sm text-nova-gold hover:bg-black/35"
            >
              {gesturesOn ? "Disable hand tracking" : "Enable hand tracking"}
            </button>
          </div>

          <div className="flex items-center justify-between gap-3 text-xs text-nova-gold/70">
            <span>Hands detected: {Number.isFinite(handsDetected) ? handsDetected : 0}</span>
            <span>Target: {desktopControlEnabled ? "Desktop cursor" : "Nova window"}</span>
          </div>

          <div className="flex items-center justify-between gap-3 text-xs text-nova-gold/70">
            <span>Pinch hold: {Math.round((pinchHoldMs || 0) / 10) * 10}ms</span>
            <span>Pinch strength: {Math.round((pinchStrength || 0) * 100)}%</span>
          </div>

          <div className="flex items-center justify-between gap-3">
            <div className="text-xs text-nova-gold/70">
              {desktopControlAvailable
                ? "Desktop hand control is available on this machine."
                : "Desktop hand control is unavailable here; gestures stay inside Nova."}
            </div>
            <button
              type="button"
              onClick={onToggleDesktopControl}
              disabled={!desktopControlAvailable}
              className="rounded-xl border border-nova-gold/20 bg-black/25 px-3 py-2 text-sm text-nova-gold hover:bg-black/35 disabled:opacity-50"
            >
              {desktopControlEnabled ? "Desktop control on" : "Desktop control off"}
            </button>
          </div>
        </div>

        <div className="text-xs text-nova-gold/70">Vision prompt</div>
        <textarea
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          rows={2}
          className="w-full rounded-xl bg-black/25 border border-nova-gold/15 px-3 py-2 text-sm text-nova-gold outline-none placeholder:text-nova-gold/45"
          placeholder="What should Nova look for?"
        />
        <button
          type="button"
          onClick={analyze}
          disabled={busy || !visionStatus.enabled}
          className="w-full rounded-xl bg-black/25 hover:bg-black/35 border border-nova-gold/20 px-3 py-2 text-sm text-nova-gold disabled:opacity-50"
        >
          {busy ? "Analyzing…" : "Capture frame + Analyze"}
        </button>

        {!visionStatus.enabled ? (
          <div className="rounded-xl border border-amber-400/20 bg-amber-500/10 p-3 text-sm text-amber-100 whitespace-pre-wrap">
            {visionStatus.reason || "Vision is not configured."}
          </div>
        ) : null}

        {result ? (
          <div className="rounded-xl border border-nova-gold/15 bg-black/20 p-3 text-sm whitespace-pre-wrap">
            {result}
          </div>
        ) : null}

      </div>
    </div>
  );
}
