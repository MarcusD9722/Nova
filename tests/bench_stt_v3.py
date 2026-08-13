"""P1: where does STT latency actually come from?

The V2 program measured generation-start -> first audio and was explicit that
microphone and STT were excluded. This closes that gap for everything that does
not need a human in the room.

Probe audio is SYNTHESISED with Nova's own XTTS worker. That is legitimate for
pipeline timing, endpoint behaviour, concurrency and regression testing — the
bytes go through the identical decode/transcribe path a browser upload takes.
It is NOT a human-speech accuracy benchmark, and this script never reports one:
synthetic speech is cleaner than a real room and would flatter the word error
rate. Accuracy needs a live microphone and is called out as such in the report.

Sections
  A. Stage breakdown — transport, ffmpeg decode, file read, inference, cleanup
  B. Format cost    — does the browser's webm force a subprocess we could skip?
  C. Config sweep   — beam size, condition_on_previous_text, timestamps, model
  D. Hotword bias   — does initial_prompt/hotwords help Marcus's vocabulary?
  E. Endpointing    — does trailingSilenceMs=700 cost real time, and does
                      lowering it cut a speaker off mid-sentence?

Run:  venv\\Scripts\\python.exe tests\\bench_stt_v3.py
"""

import asyncio
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("NOVA_TTS_DEVICE", "auto")

REPO = Path(__file__).resolve().parent.parent
VOICE = REPO / "voices" / "nova.wav"

# Utterances chosen to stress the things Marcus actually says: short commands,
# ordinary sentences, technical vocabulary, model numbers, and a sentence with a
# natural mid-sentence pause (the case a tight endpoint threshold would ruin).
PROBES = [
    ("short", "What's the weather tomorrow?"),
    ("short", "Stop."),
    ("normal", "Find me three twenty-eight terabyte drives for the media server."),
    ("technical", "Check whether llama.cpp is using CUDA and how much VRAM Qwen is taking."),
    ("technical", "Is XTTS running on the GPU, and is Chroma still indexing?"),
    ("numbers", "The RTX 5080 has sixteen gigabytes and the 5090 has thirty-two."),
    ("pause", "So the thing is, um, I think the second drive, the quieter one, is better."),
]

# Vocabulary faster-whisper will not know a priori. Used for the biasing test.
HOTWORDS = ("Nova", "Jellyfin", "StreamNChill", "llama.cpp", "Qwen", "XTTS",
            "CUDA", "Chroma", "Raspberry Pi", "Orange Pi", "RTX 5080", "faster-whisper")


def ffmpeg_bin():
    from shutil import which
    return os.getenv("NOVA_FFMPEG_PATH", "").strip() or which("ffmpeg") or "ffmpeg"


async def synth_probes(outdir: Path):
    """Render each probe to WAV with Nova's real isolated XTTS worker."""
    from services.tts_client import IsolatedTtsEngine

    engine = IsolatedTtsEngine(
        cfg={"device": os.getenv("NOVA_TTS_DEVICE", "auto"), "allow_cpu_fallback": False},
        start_timeout_s=600.0, synth_timeout_s=180.0,
    )
    if not await engine.ensure_started():
        print(f"FATAL: XTTS worker failed: {engine.last_error}")
        return None, None
    print(f"  XTTS ready on {engine.device!r} (pid {engine.pid})")

    made = []
    for i, (kind, text) in enumerate(PROBES):
        wav = await engine.synthesize(text, speaker_wav=str(VOICE), turn_id=f"probe-{i}")
        p = outdir / f"probe_{i}_{kind}.wav"
        p.write_bytes(wav)
        made.append((kind, text, p))
    await engine.stop()
    return made, engine.sample_rate


def to_webm(src: Path, dst: Path) -> bool:
    """Transcode to the container a browser MediaRecorder actually produces."""
    try:
        subprocess.run([ffmpeg_bin(), "-y", "-i", str(src), "-c:a", "libopus", str(dst)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=60)
        return dst.exists()
    except Exception:
        return False


def decode_via_ffmpeg(src: Path, dst: Path) -> float:
    """Exactly what backend/app.py does per STT request. Returns seconds."""
    t0 = time.perf_counter()
    subprocess.run([ffmpeg_bin(), "-y", "-i", str(src), "-ac", "1", "-ar", "16000",
                    "-f", "wav", str(dst)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=60)
    return time.perf_counter() - t0


def read_audio(path: Path):
    import numpy as np
    import soundfile as sf

    t0 = time.perf_counter()
    audio, sr = sf.read(str(path), dtype="float32")
    if getattr(audio, "ndim", 1) > 1:
        audio = np.mean(audio, axis=1)
    return np.asarray(audio, dtype=np.float32), int(sr), time.perf_counter() - t0


def load_model(size="base", device="cuda", compute="float16"):
    from faster_whisper import WhisperModel
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return WhisperModel(size, device=device, compute_type=compute)


def transcribe(model, audio, **kw):
    opts = dict(language="en", beam_size=1, vad_filter=True)
    opts.update(kw)
    t0 = time.perf_counter()
    segments, _info = model.transcribe(audio, **opts)
    text = " ".join(s.text.strip() for s in segments).strip()
    return text, time.perf_counter() - t0


def pct(vals, p):
    if not vals:
        return float("nan")
    s = sorted(vals)
    k = min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1))))
    return s[k]


# ── E. Endpoint simulation ───────────────────────────────────────────────────

def rms_frames(audio, sr, frame_ms=20):
    """Frame-wise RMS, matching what the browser analyser sees."""
    import numpy as np

    n = max(1, int(sr * frame_ms / 1000))
    frames = [audio[i:i + n] for i in range(0, len(audio) - n + 1, n)]
    return [float(np.sqrt(np.mean(f * f))) for f in frames], frame_ms


def simulate_endpoint(levels, frame_ms, *, threshold, trailing_ms, min_speech_ms):
    """Port of the VAD state machine in frontend/src/voice/recorder.ts.

    Returns (fire_frame_index, fired) — when the recorder would have decided the
    speaker stopped. Reimplemented rather than guessed so the numbers describe
    the code that actually ships.
    """
    speech_started = None
    heard_at = None
    for i, lv in enumerate(levels):
        now = i * frame_ms
        if lv >= threshold:
            heard_at = now
            if speech_started is None:
                speech_started = now
        if speech_started is not None and heard_at is not None:
            spoken_for = heard_at - speech_started
            silent_for = now - heard_at
            if spoken_for >= min_speech_ms and silent_for >= trailing_ms:
                return i, True
    return len(levels), False


async def main():
    print("Nova V3 P1 — STT and voice-pipeline latency")
    print("=" * 78)

    if not VOICE.exists():
        print(f"FATAL: reference voice missing: {VOICE}")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="nova-stt-bench-"))
    print(f"\nSynthesising {len(PROBES)} probe utterances with the real XTTS worker...")
    probes, tts_sr = await synth_probes(tmp)
    if not probes:
        return 2
    print(f"  {len(probes)} probes written ({tts_sr} Hz)\n")

    # ── B. Format cost ───────────────────────────────────────────────────────
    print("=" * 78)
    print("B. CONTAINER / DECODE COST  (backend spawns ffmpeg per STT request)")
    print("=" * 78)
    webm_ok = to_webm(probes[0][2], tmp / "fmt.webm")
    print(f"  {'probe':<28} {'wav decode':>12} {'webm decode':>12}")
    wav_dec, webm_dec = [], []
    for kind, text, p in probes:
        d_wav = decode_via_ffmpeg(p, tmp / "d_wav.wav")
        wav_dec.append(d_wav)
        d_webm = float("nan")
        if webm_ok and to_webm(p, tmp / "d.webm"):
            d_webm = decode_via_ffmpeg(tmp / "d.webm", tmp / "d_webm.wav")
            webm_dec.append(d_webm)
        print(f"  {(kind + ': ' + text[:20]):<28} {d_wav * 1000:>10.1f}ms "
              f"{d_webm * 1000:>10.1f}ms")
    print(f"\n  mean ffmpeg decode: wav {statistics.mean(wav_dec) * 1000:.1f}ms"
          + (f"   webm {statistics.mean(webm_dec) * 1000:.1f}ms" if webm_dec else ""))
    print("  NOTE: this is a subprocess SPAWN plus decode, paid on every single")
    print("  utterance, on the critical path between speech ending and STT starting.")

    # ── A/C. Stage breakdown and config sweep ────────────────────────────────
    print("\n" + "=" * 78)
    print("A/C. INFERENCE STAGE + CONFIG SWEEP")
    print("=" * 78)

    import torch
    cuda_ok = torch.cuda.is_available()
    print(f"  CUDA available: {cuda_ok}")

    configs = [
        ("baseline (as shipped)", dict()),
        ("condition_on_previous_text=False", dict(condition_on_previous_text=False)),
        ("without_timestamps=True", dict(without_timestamps=True)),
        ("no vad_filter", dict(vad_filter=False)),
        ("beam_size=5", dict(beam_size=5)),
    ]

    model = load_model("base", "cuda" if cuda_ok else "cpu",
                       "float16" if cuda_ok else "int8")
    print(f"  model=base device={'cuda' if cuda_ok else 'cpu'}\n")
    # Warm so the first config is not charged for lazy init.
    a0, sr0, _ = read_audio(probes[0][2])
    transcribe(model, a0)

    print(f"{'config':<36} {'mean':>9} {'median':>9} {'P90':>9}")
    baseline_texts = {}
    for label, kw in configs:
        times = []
        for kind, text, p in probes:
            audio, sr, _rd = read_audio(p)
            got, dt = transcribe(model, audio, **kw)
            times.append(dt)
            if label.startswith("baseline"):
                baseline_texts[text] = got
        print(f"{label:<36} {statistics.mean(times) * 1000:>7.0f}ms "
              f"{statistics.median(times) * 1000:>7.0f}ms {pct(times, 90) * 1000:>7.0f}ms")

    # Model size comparison, if the bigger model is worth its time.
    print()
    for size in ("base", "small"):
        try:
            m = model if size == "base" else load_model(
                size, "cuda" if cuda_ok else "cpu", "float16" if cuda_ok else "int8")
            times = []
            for kind, text, p in probes:
                audio, sr, _ = read_audio(p)
                _got, dt = transcribe(m, audio)
                times.append(dt)
            print(f"  model={size:<6} mean {statistics.mean(times) * 1000:>6.0f}ms   "
                  f"median {statistics.median(times) * 1000:>6.0f}ms")
            if size != "base":
                del m
        except Exception as e:  # noqa: BLE001
            print(f"  model={size:<6} unavailable: {type(e).__name__}: {e}")

    # ── D. Hotword biasing ───────────────────────────────────────────────────
    print("\n" + "=" * 78)
    print("D. VOCABULARY BIASING (initial_prompt / hotwords)")
    print("=" * 78)
    print("  Synthetic speech, so treat this as 'does biasing change the output'")
    print("  and NOT as a human word-error-rate result.\n")

    bias_prompt = "Vocabulary: " + ", ".join(HOTWORDS) + "."
    tech = [(k, t, p) for k, t, p in probes if k in ("technical", "numbers")]
    for kind, text, p in tech:
        audio, sr, _ = read_audio(p)
        plain, _ = transcribe(model, audio)
        biased, _ = transcribe(model, audio, initial_prompt=bias_prompt)
        hit_plain = sum(1 for w in HOTWORDS if w.lower() in plain.lower())
        hit_bias = sum(1 for w in HOTWORDS if w.lower() in biased.lower())
        print(f"  said   : {text}")
        print(f"  plain  : {plain}   [{hit_plain} known terms]")
        print(f"  biased : {biased}   [{hit_bias} known terms]")
        print()

    # ── E. Endpointing ───────────────────────────────────────────────────────
    print("=" * 78)
    print("E. ENDPOINT DETECTION  (recorder.ts VAD, reimplemented faithfully)")
    print("=" * 78)
    print("  Shipped defaults: trailingSilenceMs=700  speechThreshold=0.025\n")
    print(f"{'trailing':<10} {'thresh':<8} " + " ".join(f"{k[:9]:>10}" for k, _, _ in probes))

    import numpy as np

    thresholds = (0.020, 0.025, 0.035)
    trailings = (300, 450, 600, 700, 900)
    cutoff_risk = {}
    added_latency = {}

    # A real room does not go digitally silent when the speaker stops, and an
    # XTTS clip ends the instant the words do. Without a trailing tail the
    # silence timer can never elapse, so every setting would report the same
    # end-of-buffer fallback and the measurement would be meaningless. Append a
    # low noise floor so the endpoint decision is the thing being measured.
    tails = {}
    for kind, text, p in probes:
        audio, sr, _ = read_audio(p)
        speech_end_ms = int(len(audio) / sr * 1000)
        rng = np.random.default_rng(0)
        tail = (rng.standard_normal(int(sr * 2.0)).astype(np.float32) * 0.002)
        tails[p] = (np.concatenate([audio, tail]), sr, speech_end_ms)

    for trailing in trailings:
        for thresh in thresholds:
            cells = []
            for kind, text, p in probes:
                audio, sr, speech_end_ms = tails[p]
                levels, fms = rms_frames(audio, sr)
                idx, fired = simulate_endpoint(levels, fms, threshold=thresh,
                                               trailing_ms=trailing, min_speech_ms=250)
                fire_ms = idx * fms
                early = fire_ms < speech_end_ms - 150
                if early:
                    cutoff_risk.setdefault((trailing, thresh), []).append(kind)
                else:
                    added_latency.setdefault((trailing, thresh), []).append(
                        fire_ms - speech_end_ms)
                cells.append("CUT" if early else f"{fire_ms - speech_end_ms:+d}")
            print(f"{trailing:<10} {thresh:<8} " + " ".join(f"{c:>10}" for c in cells))

    print("\n  Cells show (endpoint fire - END OF SPEECH) in ms: the dead air Marcus")
    print("  waits through after he stops talking. 'CUT' means the recorder would")
    print("  have stopped while he was still speaking.")
    print("\n  Mean added dead-air per setting (lower is better, CUT-free only):")
    for (tr, th) in sorted(added_latency):
        if (tr, th) in cutoff_risk:
            continue
        vals = added_latency[(tr, th)]
        print(f"    trailing={tr:<4} thresh={th:<6} {statistics.mean(vals):>7.0f}ms "
              f"across {len(vals)}/{len(probes)} probes")
    print("\n  Cut-off risk by setting:")
    for (tr, th), kinds in sorted(cutoff_risk.items()):
        print(f"    trailing={tr} thresh={th}: cuts {sorted(set(kinds))}")
    if not cutoff_risk:
        print("    none observed on these probes")

    print("\n" + "=" * 78)
    print("Synthetic speech: pipeline timing is valid, ACCURACY IS NOT.")
    print("Human word-error rate requires a live microphone (see NOVA_V3_PERFORMANCE.md).")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
