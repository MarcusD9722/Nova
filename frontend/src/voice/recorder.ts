// src/voice/recorder.ts
// Audio recording + STT/TTS helpers for Nova frontend.

type VoiceDebugOptions = {
  debugTag?: string;
};

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

function vlog(debugTag: string | undefined, ...args: any[]) {
  if (!voiceDebugEnabled()) return;
  const prefix = debugTag ? `[voice:${debugTag}]` : "[voice]";
  // eslint-disable-next-line no-console
  console.log(prefix, ...args);
}

export async function playAudioUrl(url: string, opts: { debugTag?: string } = {}): Promise<void> {
  const debugTag = opts.debugTag || "tts";
  const audio = new Audio(url);
  try {
    const p = audio.play();
    if (p && typeof (p as any).then === "function") await p;
    vlog(debugTag, "playing audio url", { url });
  } catch (e: any) {
    vlog(debugTag, "audio url play failed", { url, name: e?.name, message: e?.message || String(e) });
    throw e;
  }
}

function apiBase(): string {
  // Dev: keep relative (Vite proxy)
  try {
    // @ts-ignore
    if (import.meta?.env?.DEV) return "";
  } catch {}
  try {
    // @ts-ignore
    const env = import.meta?.env;
    const fromEnv = env?.VITE_API_BASE ? String(env.VITE_API_BASE) : "";
    if (fromEnv) return fromEnv.replace(/\/$/, "");
  } catch {}
  // Electron file:// fallback
  try {
    // @ts-ignore
    const w = window as any;
    const fromWindow = w?.__NOVA_API_BASE ? String(w.__NOVA_API_BASE) : "";
    if (fromWindow) return fromWindow.replace(/\/$/, "");
  } catch {}
  return "http://localhost:8008";
}

function apiUrl(path: string) {
  const base = apiBase();
  return (base ? base : "") + path;
}

// ===== Shared microphone stream (wake loop + mic meter + capture) =====
let _sharedMicStream: MediaStream | null = null;
let _sharedMicRefs = 0;

export type MicStreamHandle = {
  stream: MediaStream;
  release: () => void;
};

export async function acquireMicStreamHandle(opts: VoiceDebugOptions = {}): Promise<MicStreamHandle> {
  const tag = opts.debugTag;
  _sharedMicRefs += 1;
  const myRef = _sharedMicRefs;

  if (_sharedMicStream) {
    vlog(tag, "reusing mic stream", { refs: _sharedMicRefs });
    return {
      stream: _sharedMicStream,
      release: () => releaseMicStreamHandle({ debugTag: tag, _fromRef: myRef }),
    };
  }

  vlog(tag, "requesting mic stream (getUserMedia)");
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
    _sharedMicStream = stream;
    vlog(tag, "mic stream acquired", { refs: _sharedMicRefs, tracks: stream.getAudioTracks().length });
    return {
      stream,
      release: () => releaseMicStreamHandle({ debugTag: tag, _fromRef: myRef }),
    };
  } catch (e: any) {
    // undo ref bump
    _sharedMicRefs = Math.max(0, _sharedMicRefs - 1);
    vlog(tag, "mic permission/stream error", e?.name || e);
    throw e;
  }
}

function releaseMicStreamHandle({ debugTag, _fromRef }: { debugTag?: string; _fromRef?: number }) {
  _sharedMicRefs = Math.max(0, _sharedMicRefs - 1);
  vlog(debugTag, "release mic handle", { refs: _sharedMicRefs, from: _fromRef });

  if (_sharedMicRefs > 0) return;
  if (!_sharedMicStream) return;

  try {
    _sharedMicStream.getTracks().forEach((t) => t.stop());
  } catch {}
  _sharedMicStream = null;
  vlog(debugTag, "mic stream released");
}

function bestSupportedMime(): string {
  const candidates = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/ogg;codecs=opus",
    "audio/ogg",
  ];
  for (const c of candidates) {
    // @ts-ignore
    if (window.MediaRecorder && MediaRecorder.isTypeSupported?.(c)) return c;
  }
  return "";
}

export async function recordFromStreamToBlob(
  stream: MediaStream,
  {
    maxMs = 8000,
    timesliceMs = 250,
    debugTag,
  }: { maxMs?: number; timesliceMs?: number; debugTag?: string } = {}
): Promise<Blob> {
  if (!window.MediaRecorder) {
    throw new Error("MediaRecorder not supported in this runtime.");
  }

  const mimeType = bestSupportedMime();
  const rec = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
  const chunks: BlobPart[] = [];

  return await new Promise<Blob>((resolve, reject) => {
    let done = false;
    const startedAt = performance.now();

    const finish = () => {
      if (done) return;
      done = true;
      const blob = new Blob(chunks, { type: rec.mimeType || "audio/webm" });
      const durMs = Math.round(performance.now() - startedAt);
      vlog(debugTag, "recorded blob", { size: blob.size, type: blob.type, durMs });
      resolve(blob);
    };

    rec.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) chunks.push(e.data);
    };
    rec.onerror = (e) => {
      if (done) return;
      done = true;
      vlog(debugTag, "MediaRecorder error", e);
      reject(new Error("MediaRecorder error"));
    };
    rec.onstop = finish;

    try {
      rec.start(timesliceMs);
    } catch (e) {
      reject(e as any);
      return;
    }

    const timer = window.setTimeout(() => {
      try {
        rec.stop();
      } catch {}
    }, maxMs);

    window.addEventListener(
      "keydown",
      (e) => {
        if (e.key === "Escape") {
          window.clearTimeout(timer);
          try {
            rec.stop();
          } catch {}
        }
      },
      { once: true }
    );
  });
}

export async function recordOnceToBlob(
  {
    maxMs,
    seconds,
    debugTag,
  }: { maxMs?: number; seconds?: number; debugTag?: string } = {}
): Promise<Blob> {
  const effectiveMaxMs =
    typeof maxMs === "number" && Number.isFinite(maxMs)
      ? maxMs
      : typeof seconds === "number" && Number.isFinite(seconds)
      ? Math.max(200, Math.round(seconds * 1000))
      : 8000;

  const handle = await acquireMicStreamHandle({ debugTag });
  try {
    return await recordFromStreamToBlob(handle.stream, {
      maxMs: effectiveMaxMs,
      timesliceMs: 250,
      debugTag,
    });
  } finally {
    handle.release();
  }
}

export async function transcribeBlob(
  blob: Blob,
  urlOrOpts?: string | { url?: string; path?: string; debugTag?: string }
): Promise<string> {
  const fd = new FormData();
  const ext = blob.type.includes("wav") ? "wav" : blob.type.includes("ogg") ? "ogg" : "webm";
  fd.append("file", blob, `recording.${ext}`);

  let url = apiUrl("/stt");
  let debugTag: string | undefined;
  if (typeof urlOrOpts === "string") {
    url = urlOrOpts;
  } else if (urlOrOpts) {
    debugTag = urlOrOpts.debugTag;
    if (urlOrOpts.url) url = String(urlOrOpts.url);
    else if (urlOrOpts.path) url = apiUrl(String(urlOrOpts.path));
  }

  const t0 = performance.now();
  vlog(debugTag, "STT request", { url, size: blob.size, type: blob.type });
  const res = await fetch(url, { method: "POST", body: fd });
  const dtMs = Math.round(performance.now() - t0);
  if (!res.ok) throw new Error(await res.text());
  const data = await res.json();
  const text = String(data?.text || data?.transcript || "");
  vlog(debugTag, "STT response", { dtMs, text });
  return text;
}

export async function speak(
  text: string,
  opts: { voice_id?: string; voice_name?: string } = {}
): Promise<void> {
  const payload: any = { text };
  if (opts.voice_id) payload.voice_id = opts.voice_id;
  if (opts.voice_name) payload.voice_name = opts.voice_name;

  const res = await fetch(apiUrl("/speak"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(await res.text());
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);

  const audio = new Audio(url);
  try {
    const p = audio.play();
    if (p && typeof (p as any).then === "function") {
      await p;
    }
    vlog("tts", "audio started", { bytes: blob.size });
  } catch (e: any) {
    vlog("tts", "audio.play failed", { name: e?.name, message: e?.message || String(e) });
    throw e;
  } finally {
    audio.onended = () => URL.revokeObjectURL(url);
  }
}

// Backward-compatible alias
export const speakText = speak;
