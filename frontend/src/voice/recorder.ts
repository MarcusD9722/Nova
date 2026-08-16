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

// Keep a reference to the most recent Audio element while it plays.
// In some Chromium/Electron cases, losing all references can cause
// playback to stop unexpectedly.
let _activeAudio: HTMLAudioElement | null = null;

export function stopActiveAudio(): void {
  const audio = _activeAudio;
  if (!audio) return;
  try { audio.pause(); } catch {}
  try { audio.currentTime = 0; } catch {}
  _activeAudio = null;
}

// ── TTS output analyser (drives avatar lip sync) ────────────────────────────
// Nova's playback is routed through a WebAudio AnalyserNode so the avatar's
// mouth can follow the real speech waveform. Blob object-URLs are same-origin,
// so routing never taints/mutes the audio.
let _outCtx: AudioContext | null = null;
let _outAnalyser: AnalyserNode | null = null;
let _outData: Uint8Array | null = null;

function ensureOutputAnalyser(): { ctx: AudioContext; analyser: AnalyserNode } | null {
  try {
    if (!_outCtx) {
      const Ctx = window.AudioContext || (window as any).webkitAudioContext;
      _outCtx = new Ctx();
    }
    if (!_outAnalyser) {
      _outAnalyser = _outCtx.createAnalyser();
      _outAnalyser.fftSize = 512;
      _outAnalyser.smoothingTimeConstant = 0.5;
      _outAnalyser.connect(_outCtx.destination);
      _outData = new Uint8Array(_outAnalyser.fftSize);
    }
    if (_outCtx.state === "suspended") _outCtx.resume().catch(() => {});
    return { ctx: _outCtx, analyser: _outAnalyser };
  } catch {
    return null;
  }
}

function routeThroughAnalyser(audio: HTMLAudioElement): void {
  const nodes = ensureOutputAnalyser();
  if (!nodes) return;
  try {
    const source = nodes.ctx.createMediaElementSource(audio);
    source.connect(nodes.analyser);
  } catch {
    // Element still plays through the default output if routing fails.
  }
}

/**
 * RAW RMS (0..1) of Nova's speech output — no display scaling, no clamp.
 *
 * getTtsOutputLevel() below multiplies by 3.4 and clamps to 1.0 because the
 * avatar's mouth needs a lively 0..1 display signal. That makes it useless as
 * an acoustic reference, and using it as one is an outright bug rather than a
 * mis-tuning:
 *
 *   moderate speech (rms ~0.15) -> 0.51, so a naive `mic > level * ratio`
 *   test demands a mic RMS no human voice produces;
 *   loud speech (rms >= 0.294)  -> SATURATES at 1.0, so the comparison can
 *   never be satisfied at all, because mic RMS is <= 1 by definition.
 *
 * Barge-in would therefore have been impossible exactly when Nova is loudest.
 * Echo/coupling logic must use this function; lip sync keeps the scaled one.
 */
export function getTtsOutputRms(): number {
  try {
    if (!_outAnalyser || !_outData || !_activeAudio || _activeAudio.paused) return 0;
    _outAnalyser.getByteTimeDomainData(_outData);
    let sum = 0;
    for (let i = 0; i < _outData.length; i += 1) {
      const v = (_outData[i] - 128) / 128;
      sum += v * v;
    }
    return Math.sqrt(sum / _outData.length);
  } catch {
    return 0;
  }
}

/**
 * Duck Nova's playback without destroying the turn.
 *
 * Barge-in's fast stage acts on levels alone and is allowed to be wrong, so it
 * must be reversible: attenuate here, and restore if the backend comes back
 * ECHO. stopActiveAudio() is the irreversible version and is only correct once
 * a real interruption has been confirmed.
 */
let _preDuckVolume: number | null = null;

export function duckPlayback(gain = 0.05): boolean {
  const audio = _activeAudio;
  if (!audio) return false;
  try {
    if (_preDuckVolume === null) _preDuckVolume = audio.volume;
    audio.volume = Math.max(0, Math.min(1, gain));
    return true;
  } catch {
    return false;
  }
}

export function restorePlayback(): void {
  const audio = _activeAudio;
  if (_preDuckVolume === null) return;
  try {
    if (audio) audio.volume = _preDuckVolume;
  } catch {
    // Nothing to restore to if the clip already ended.
  }
  _preDuckVolume = null;
}

export function isDucked(): boolean {
  return _preDuckVolume !== null;
}

/** Display level (0..1) for avatar lip sync. Scaled and clamped — NOT an
 *  acoustic reference; use getTtsOutputRms() for that. */
export function getTtsOutputLevel(): number {
  try {
    if (!_outAnalyser || !_outData || !_activeAudio || _activeAudio.paused) return 0;
    _outAnalyser.getByteTimeDomainData(_outData);
    let sum = 0;
    for (let i = 0; i < _outData.length; i += 1) {
      const v = (_outData[i] - 128) / 128;
      sum += v * v;
    }
    const rms = Math.sqrt(sum / _outData.length);
    return Math.min(1, rms * 3.4);
  } catch {
    return 0;
  }
}

/** True while a TTS/voice clip is actually playing (not just "queued"). */
export function isTtsPlaying(): boolean {
  return !!(_activeAudio && !_activeAudio.paused);
}

async function playBlobAsAudio(blob: Blob, opts: { debugTag?: string; onEnded?: () => void } = {}) {
  const url = URL.createObjectURL(blob);
  stopActiveAudio();
  const audio = new Audio(url);
  routeThroughAnalyser(audio);
  _activeAudio = audio;

  audio.onended = () => {
    try { opts.onEnded?.(); } catch {}
    try { URL.revokeObjectURL(url); } catch {}
    if (_activeAudio === audio) _activeAudio = null;
  };
  audio.onerror = () => {
    try { URL.revokeObjectURL(url); } catch {}
    if (_activeAudio === audio) _activeAudio = null;
  };

  const p = audio.play();
  if (p && typeof (p as any).then === "function") await p;
  vlog(opts.debugTag, "playing audio blob", { bytes: blob.size, type: blob.type });
}

export async function playAudioUrl(
  url: string,
  opts: {
    debugTag?: string;
    onEnded?: () => void;
    onError?: (e: any) => void;
    /**
     * Optional in-flight (or already resolved) fetch+decode kicked off when
     * this clip was enqueued, so it's ready by the time playback advances to
     * it — removes the audible dead-air between sentences.
     */
    preloadedBlob?: Blob | Promise<Blob | null> | null;
  } = {}
): Promise<void> {
  const debugTag = opts.debugTag || "tts";

  if (opts.preloadedBlob) {
    try {
      const blob = await opts.preloadedBlob;
      if (blob) {
        await playBlobAsAudio(blob, { debugTag, onEnded: opts.onEnded });
        return;
      }
    } catch (e: any) {
      vlog(debugTag, "preloaded blob playback failed; falling back to fetch", { url, message: e?.message || String(e) });
    }
  }

  // Preferred path: fetch bytes and play as a same-origin blob. This lets the
  // output route through the lip-sync analyser without CORS-tainting (a
  // tainted media source plays SILENCE through WebAudio), and blob playback
  // is immune to MIME/proxy quirks. Backend is localhost, so the fetch is fast.
  if (!url.startsWith("blob:")) {
    try {
      const res = await fetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const blob = await res.blob();
      await playBlobAsAudio(blob, { debugTag, onEnded: opts.onEnded });
      return;
    } catch (e: any) {
      vlog(debugTag, "fetch+blob playback failed; trying direct url", { url, message: e?.message || String(e) });
    }
  } else {
    try {
      const res = await fetch(url);
      const blob = await res.blob();
      await playBlobAsAudio(blob, { debugTag, onEnded: opts.onEnded });
      return;
    } catch {}
  }

  // Fallback: direct element playback (no lip-sync analysis).
  stopActiveAudio();
  const audio = new Audio(url);
  _activeAudio = audio;
  if (typeof opts.onEnded === "function") {
    audio.onended = () => {
      try { opts.onEnded?.(); } catch {}
      if (_activeAudio === audio) _activeAudio = null;
    };
  } else {
    audio.onended = () => {
      if (_activeAudio === audio) _activeAudio = null;
    };
  }
  audio.onerror = () => {
    if (_activeAudio === audio) _activeAudio = null;
  };
  try {
    const p = audio.play();
    if (p && typeof (p as any).then === "function") await p;
    vlog(debugTag, "playing audio url (direct)", { url });
  } catch (e: any) {
    vlog(debugTag, "audio url play failed", { url, name: e?.name, message: e?.message || String(e) });
    try { opts.onError?.(e); } catch {}
    throw e;
  }
}

/**
 * Kick off a fetch+decode of a TTS clip's audio immediately, without playing
 * it. Call this as soon as a clip is enqueued so it's already resolved by
 * the time playback advances to it. Resolves to null (never rejects) on
 * failure or for blob: URLs that don't need prefetching.
 */
export function prefetchAudioBlob(url: string): Promise<Blob | null> {
  if (!url || url.startsWith("blob:")) return Promise.resolve(null);
  return fetch(url)
    .then((res) => {
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.blob();
    })
    .catch((e: any) => {
      vlog("tts", "prefetch failed", { url, message: e?.message || String(e) });
      return null;
    });
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

export async function unlockAudioContext(): Promise<void> {
  // Some Chromium/Electron configurations keep AudioContext suspended
  // until it is first resumed during a user gesture. Call this from a
  // click/tap handler (e.g., mic unmute) to ensure meters + playback work.
  try {
    const AudioCtx: any = (window as any).AudioContext || (window as any).webkitAudioContext;
    if (!AudioCtx) return;
    const ctx = new AudioCtx();
    try {
      if (ctx.state === "suspended") {
        await ctx.resume();
      }
    } finally {
      try { await ctx.close(); } catch {}
    }
  } catch {
    // best-effort only
  }
}

// ===== Shared microphone stream (wake loop + mic meter + capture) =====
let _sharedMicStream: MediaStream | null = null;
let _sharedMicRefs = 0;

export type MicStreamHandle = {
  stream: MediaStream;
  /**
   * Live analyser over the shared microphone.
   *
   * Barge-in's acoustic gate has to read mic level WHILE no recording is in
   * progress, which the per-recording analysers inside recordVoiceActivity*
   * cannot provide — they only exist for the duration of a capture. Shared and
   * lazily created so repeated handles do not stack AudioContexts.
   */
  analyser: AnalyserNode | null;
  release: () => void;
};

// Shared INPUT analyser. Distinct from the output analyser above, which taps
// Nova's playback for lip sync.
let _inCtx: AudioContext | null = null;
let _inAnalyser: AnalyserNode | null = null;
let _inSourceFor: MediaStream | null = null;

function ensureInputAnalyser(stream: MediaStream): AnalyserNode | null {
  try {
    if (_inAnalyser && _inSourceFor === stream) return _inAnalyser;
    if (!_inCtx) {
      const Ctx = window.AudioContext || (window as any).webkitAudioContext;
      _inCtx = new Ctx();
    }
    if (_inCtx.state === "suspended") _inCtx.resume().catch(() => {});
    const analyser = _inCtx.createAnalyser();
    analyser.fftSize = 512;
    analyser.smoothingTimeConstant = 0.3;
    // Deliberately NOT connected to destination: monitoring the microphone
    // through the speakers would create the exact feedback loop barge-in
    // exists to reason about.
    _inCtx.createMediaStreamSource(stream).connect(analyser);
    _inAnalyser = analyser;
    _inSourceFor = stream;
    return analyser;
  } catch {
    return null;
  }
}

export async function acquireMicStreamHandle(opts: VoiceDebugOptions = {}): Promise<MicStreamHandle> {
  const tag = opts.debugTag;
  _sharedMicRefs += 1;
  const myRef = _sharedMicRefs;

  if (_sharedMicStream) {
    vlog(tag, "reusing mic stream", { refs: _sharedMicRefs });
    return {
      stream: _sharedMicStream,
      analyser: ensureInputAnalyser(_sharedMicStream),
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
      analyser: ensureInputAnalyser(stream),
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
  _inAnalyser = null;
  _inSourceFor = null;
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


export type VADRecordOptions = {
  maxMs?: number;
  minSpeechMs?: number;
  trailingSilenceMs?: number;
  speechThreshold?: number;
  startTimeoutMs?: number;
  timesliceMs?: number;
  debugTag?: string;
};

function rmsFromAnalyser(analyser: AnalyserNode, data: Uint8Array): number {
  analyser.getByteTimeDomainData(data);
  let sum = 0;
  for (let i = 0; i < data.length; i += 1) {
    const centered = (data[i] - 128) / 128;
    sum += centered * centered;
  }
  return Math.sqrt(sum / data.length);
}

export async function recordVoiceActivityFromStreamToBlob(
  stream: MediaStream,
  {
    maxMs = 8000,
    minSpeechMs = 250,
    // 700 -> 600. Measured in tests/bench_stt_v3.py against seven probe
    // utterances (short commands, technical vocabulary, model numbers, and a
    // sentence with a deliberate mid-sentence "um" pause):
    //
    //   300ms  cuts the speaker off on 7/7 probes
    //   450ms  cuts 3-5 of 7, including the mid-sentence pause
    //   600ms  0 cuts, -4ms dead air after speech ends
    //   700ms  0 cuts, +96ms dead air   <- was shipped
    //   900ms  0 cuts, +296ms dead air
    //
    // 600 is the lowest CUT-free setting measured and saves ~100ms of dead air
    // on every single turn. The margin matters though: 450 already cuts, so
    // there is only ~150ms of headroom, and synthetic speech pauses are
    // cleaner than a real "um... hang on" pause. If Marcus gets clipped
    // mid-sentence, put this back to 700 first.
    trailingSilenceMs = 600,
    speechThreshold = 0.025,
    startTimeoutMs = 3500,
    timesliceMs = 250,
    debugTag,
  }: VADRecordOptions = {}
): Promise<Blob> {
  if (!window.MediaRecorder) {
    throw new Error("MediaRecorder not supported in this runtime.");
  }

  const AudioCtx: any = (window as any).AudioContext || (window as any).webkitAudioContext;
  if (!AudioCtx) {
    return recordFromStreamToBlob(stream, { maxMs, timesliceMs, debugTag });
  }

  const audioCtx = new AudioCtx();
  const source = audioCtx.createMediaStreamSource(stream);
  const analyser = audioCtx.createAnalyser();
  analyser.fftSize = 2048;
  analyser.smoothingTimeConstant = 0.82;
  source.connect(analyser);
  const data = new Uint8Array(analyser.fftSize);

  const mimeType = bestSupportedMime();
  const rec = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
  const chunks: BlobPart[] = [];

  return await new Promise<Blob>((resolve, reject) => {
    let done = false;
    let heardSpeechAt = 0;
    let speechStartedAt = 0;
    const startedAt = performance.now();

    const cleanup = async () => {
      try { source.disconnect(); } catch {}
      try { analyser.disconnect(); } catch {}
      try { await audioCtx.close(); } catch {}
    };

    const finish = async () => {
      if (done) return;
      done = true;
      const blob = new Blob(chunks, { type: rec.mimeType || "audio/webm" });
      const durMs = Math.round(performance.now() - startedAt);
      vlog(debugTag, "vad recorded blob", {
        size: blob.size,
        type: blob.type,
        durMs,
        speechStartedAt,
        heardSpeechAt,
      });
      await cleanup();
      resolve(blob);
    };

    const fail = async (error: any) => {
      if (done) return;
      done = true;
      await cleanup();
      reject(error);
    };

    rec.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) chunks.push(e.data);
    };
    rec.onerror = (e) => {
      fail(new Error(`MediaRecorder error: ${String((e as any)?.error?.name || "unknown")}`));
    };
    rec.onstop = () => { void finish(); };

    try {
      rec.start(timesliceMs);
    } catch (e) {
      void fail(e);
      return;
    }

    const tick = () => {
      if (done) return;
      const elapsed = performance.now() - startedAt;
      const level = rmsFromAnalyser(analyser, data);
      const isSpeech = level >= speechThreshold;
      const now = performance.now();

      if (isSpeech) {
        heardSpeechAt = now;
        if (!speechStartedAt) {
          speechStartedAt = now;
          vlog(debugTag, "vad speech start", { elapsed: Math.round(elapsed), level });
        }
      }

      if (!speechStartedAt && elapsed >= startTimeoutMs) {
        try { rec.stop(); } catch {}
        return;
      }

      if (speechStartedAt) {
        const spokenFor = heardSpeechAt - speechStartedAt;
        const silentFor = heardSpeechAt ? now - heardSpeechAt : 0;
        if (spokenFor >= minSpeechMs && silentFor >= trailingSilenceMs) {
          try { rec.stop(); } catch {}
          return;
        }
      }

      if (elapsed >= maxMs) {
        try { rec.stop(); } catch {}
        return;
      }

      window.setTimeout(tick, 40);
    };

    tick();
  });
}

export async function recordVoiceActivityToBlob(opts: VADRecordOptions = {}): Promise<Blob> {
  const handle = await acquireMicStreamHandle({ debugTag: opts.debugTag });
  try {
    return await recordVoiceActivityFromStreamToBlob(handle.stream, opts);
  } finally {
    handle.release();
  }
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

/**
 * Additive speaker metadata `/stt` returns when classification was requested.
 *
 * DIAGNOSTIC ONLY on the client. `display_name` and `status` may be shown in
 * debug UI, but no branch here may conclude "this is Marcus, therefore …" —
 * privacy behaviour comes from the backend redeeming `voice_turn_id`. The
 * client never sends any field from this object except the handle.
 */
export type SttSpeakerInfo = {
  status?: string;
  reason?: string | null;
  attempted?: boolean;
  profile_id?: string | null;
  display_name?: string | null;
  similarity?: number | null;
  model_id?: string | null;
  voice_turn_id?: string | null;
};

export type SttResult = {
  text: string;
  durationMs?: number;
  sampleRate?: number;
  empty?: boolean;
  speaker?: SttSpeakerInfo | null;
};

export type TranscribeOptions = {
  url?: string;
  path?: string;
  debugTag?: string;
  /**
   * Ask the backend to classify who is speaking (V3 P5.1e).
   *
   * Sent as a multipart FORM FIELD, matching the existing backend contract —
   * not a query parameter. Off by default, so wake chunks and barge-in captures
   * stay speaker-free without every caller having to remember to disable it.
   */
  speaker?: boolean;
  /** Injectable for tests; defaults to global fetch. */
  fetchImpl?: typeof fetch;
};

export async function transcribeBlobDetailed(
  blob: Blob,
  urlOrOpts?: string | TranscribeOptions
): Promise<SttResult> {
  const fd = new FormData();
  const ext = blob.type.includes("wav") ? "wav" : blob.type.includes("ogg") ? "ogg" : "webm";
  fd.append("file", blob, `recording.${ext}`);

  let url = apiUrl("/stt");
  let debugTag: string | undefined;
  let wantSpeaker = false;
  let doFetch: typeof fetch = fetch;
  if (typeof urlOrOpts === "string") {
    url = urlOrOpts;
  } else if (urlOrOpts) {
    debugTag = urlOrOpts.debugTag;
    if (urlOrOpts.url) url = String(urlOrOpts.url);
    else if (urlOrOpts.path) url = apiUrl(String(urlOrOpts.path));
    wantSpeaker = Boolean(urlOrOpts.speaker);
    if (urlOrOpts.fetchImpl) doFetch = urlOrOpts.fetchImpl;
  }
  // Only appended when asked. An absent field means "don't classify", which is
  // what keeps wake detection at zero embeddings.
  if (wantSpeaker) fd.append("speaker", "true");

  const t0 = performance.now();
  vlog(debugTag, "STT request", { url, size: blob.size, type: blob.type, speaker: wantSpeaker });
  const res = await doFetch(url, { method: "POST", body: fd });
  const dtMs = Math.round(performance.now() - t0);
  if (!res.ok) throw new Error(await res.text());
  const data = await res.json();
  const text = String(data?.text || data?.transcript || "");
  const result: SttResult = {
    text,
    durationMs: Number.isFinite(Number(data?.duration_ms)) ? Number(data.duration_ms) : undefined,
    sampleRate: Number.isFinite(Number(data?.sample_rate)) ? Number(data.sample_rate) : undefined,
    empty: Boolean(data?.empty),
    speaker: (data && typeof data.speaker === "object" && data.speaker) ? data.speaker : null,
  };
  vlog(debugTag, "STT response", { dtMs, ...result });
  return result;
}

/**
 * Handle from an STT result, or null.
 *
 * Null is a normal outcome — speaker ID disabled, an empty transcript, a
 * service failure — and callers must still mark the turn as voice. See
 * turnOrigin.ts.
 */
export function voiceTurnIdOf(stt: SttResult | null | undefined): string | null {
  const id = stt?.speaker?.voice_turn_id;
  return typeof id === "string" && id.trim() ? id : null;
}

export async function transcribeBlob(
  blob: Blob,
  urlOrOpts?: string | TranscribeOptions
): Promise<string> {
  const result = await transcribeBlobDetailed(blob, urlOrOpts);
  return result.text;
}

export async function speak(
  text: string,
  opts: { voice?: string; voice_id?: string; voice_name?: string } = {}
): Promise<void> {
  const payload: any = { text };

  // Backend expects `voice` (a filename under the voices directory).
  if (opts.voice) payload.voice = opts.voice;
  // Back-compat: map prior option names onto `voice`.
  else if (opts.voice_name) payload.voice = opts.voice_name;
  else if (opts.voice_id) payload.voice = opts.voice_id;

  const res = await fetch(apiUrl("/speak"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(await res.text());
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);

  stopActiveAudio();
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
