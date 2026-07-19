import React, { useEffect, useState } from "react";

// API base resolution (same pattern as CameraSheet)
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

async function dataUrlToBlob(dataUrl) {
  const res = await fetch(dataUrl);
  return res.blob();
}

export default function ScreenVisionSheet() {
  const [question, setQuestion] = useState("What am I looking at? Describe what's on screen and help with anything that looks like a problem.");
  const [result, setResult] = useState("");
  const [preview, setPreview] = useState("");
  const [busy, setBusy] = useState(false);
  const [visionStatus, setVisionStatus] = useState({ enabled: true, reason: "" });

  const desktopAvailable = typeof window !== "undefined" && !!window.novaDesktop?.captureScreen;

  useEffect(() => {
    let cancelled = false;
    async function loadVisionStatus() {
      try {
        const resp = await fetch(`${API_BASE}/health`);
        if (!resp.ok) return;
        const data = await resp.json();
        const next = data?.vision;
        if (!cancelled && next && typeof next === "object") {
          setVisionStatus({ enabled: Boolean(next.enabled), reason: String(next.reason || "") });
        }
      } catch {
        // Leave the button enabled if health is unreachable; analyze() surfaces the real error.
      }
    }
    loadVisionStatus();
    return () => { cancelled = true; };
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

  async function captureAndAnalyze() {
    if (!desktopAvailable) {
      setResult("Screen capture is only available in the Nova desktop app.");
      return;
    }
    if (!visionStatus.enabled) {
      setResult(visionStatus.reason || "Vision is not configured.");
      return;
    }
    setBusy(true);
    setResult("");
    setPreview("");
    try {
      const capture = await window.novaDesktop.captureScreen();
      if (!capture?.ok || !capture?.dataUrl) {
        throw new Error(capture?.error || "Could not capture the screen.");
      }
      setPreview(capture.dataUrl);

      const blob = await dataUrlToBlob(capture.dataUrl);
      const fd = new FormData();
      fd.append("file", blob, "screen.png");

      const q = (question || "").trim() || "Describe what's on screen.";
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
      <div className="text-xs text-nova-gold/70">
        Nova only sees your screen when you press the button below — never automatically or in the background.
      </div>

      {!desktopAvailable ? (
        <div className="rounded-xl border border-amber-400/20 bg-amber-500/10 p-3 text-sm text-amber-100">
          Screen capture requires the Nova desktop app (Electron) — it isn't available in a plain browser tab.
        </div>
      ) : null}

      {preview ? (
        <div className="rounded-2xl border border-nova-gold/15 bg-black/25 overflow-hidden">
          <img src={preview} alt="Last screen capture" className="w-full max-h-[240px] object-contain" />
        </div>
      ) : null}

      <div className="space-y-2">
        <div className="text-xs text-nova-gold/70">What should Nova look for?</div>
        <textarea
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          rows={2}
          className="w-full rounded-xl bg-black/25 border border-nova-gold/15 px-3 py-2 text-sm text-nova-gold outline-none placeholder:text-nova-gold/45"
          placeholder="What should Nova look for?"
        />
        <button
          type="button"
          onClick={captureAndAnalyze}
          disabled={busy || !visionStatus.enabled || !desktopAvailable}
          className="w-full rounded-xl bg-black/25 hover:bg-black/35 border border-nova-gold/20 px-3 py-2 text-sm text-nova-gold disabled:opacity-50"
        >
          {busy ? "Capturing + Analyzing…" : "Capture screen + Analyze"}
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
