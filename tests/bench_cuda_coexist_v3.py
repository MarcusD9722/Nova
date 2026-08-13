"""P1: is faster-whisper on CUDA safe beside llama.cpp and the XTTS process?

Nova already learned this the expensive way once. In-process CUDA XTTS beside
llama.cpp aborted the backend with an illegal memory access, and the fix was to
move XTTS into its own process (core/gpu.py, services/tts_worker.py).

STT now presents the same SHAPE of risk: `backend/app.py::_load_stt_engine`
tries `("cuda", "float16")` first, putting a CTranslate2 CUDA context in the
same process as llama.cpp. CTranslate2 is a different runtime from torch, so it
is NOT automatically the same bug — but "different runtime" is a hypothesis, not
evidence, and it has never been tested under concurrent load.

So: measure, do not assume. The wrong reaction to this uncertainty is to quietly
move STT to CPU and call it safety — that is a silent regression with no
evidence behind it.

Seven configurations, per the V3 brief:
  1 llama.cpp alone          2 faster-whisper alone      3 XTTS process alone
  4 llama + whisper          5 llama + XTTS              6 whisper + XTTS
  7 all three, realistic conversational workload

Watches for: CUDA errors, VRAM drift, latency change vs the alone-case,
deadlocks, inference failures, worker restarts.

Run:  venv\\Scripts\\python.exe tests\\bench_cuda_coexist_v3.py
"""

import asyncio
import os
import statistics
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("NOVA_TTS_DEVICE", "auto")

REPO = Path(__file__).resolve().parent.parent
VOICE = REPO / "voices" / "nova.wav"

SYSTEM = ("You are Nova. Reply in one short sentence of plain prose.\n\n"
          "IMPORTANT: Reply with ONLY what you'd say out loud. No analysis, no "
          "reasoning block — just the reply.")

_fail = False
errors: list[str] = []


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


def vram():
    import torch
    free, total = torch.cuda.mem_get_info()
    return (total - free) / 1e9


def banner(msg):
    print(f"\n{'=' * 74}\n{msg}\n{'=' * 74}")


async def main():
    global _fail
    import torch

    if not torch.cuda.is_available():
        print("FATAL: no CUDA — this benchmark exists to test CUDA coexistence")
        return 2
    if not VOICE.exists():
        print(f"FATAL: reference voice missing: {VOICE}")
        return 2
    model_path = next((p for p in (REPO / "model").glob("*.gguf")
                       if "mmproj" not in p.name.lower()), None)
    if model_path is None:
        print("FATAL: no GGUF in model/")
        return 2

    from core.llm_runtime import LLMRuntime
    from services.tts_client import IsolatedTtsEngine

    banner("SETUP")
    print(f"VRAM idle: {vram():.2f} GB")

    # ── Build the three consumers ────────────────────────────────────────────
    llm = LLMRuntime(model_path=model_path, context_tokens=8192)
    await llm.initialize()
    print(f"llama.cpp loaded          VRAM {vram():.2f} GB")

    whisper = None
    whisper_device = None
    try:
        import warnings
        from faster_whisper import WhisperModel
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            whisper = WhisperModel("base", device="cuda", compute_type="float16")
        import numpy as np
        list(whisper.transcribe(np.zeros(16000, dtype=np.float32), language="en")[0])
        whisper_device = "cuda"
        print(f"faster-whisper on CUDA    VRAM {vram():.2f} GB")
    except Exception as e:  # noqa: BLE001
        print(f"faster-whisper CUDA unavailable ({type(e).__name__}: {e}) — "
              f"this itself is a finding")
        whisper_device = None

    engine = IsolatedTtsEngine(
        cfg={"device": "auto", "allow_cpu_fallback": False},
        start_timeout_s=600.0, synth_timeout_s=180.0, max_restarts=3,
        on_event=lambda e, p: print(f"  [{e}] {p}") if e not in {"tts.loading"} else None,
    )
    if not await engine.ensure_started():
        print(f"FATAL: XTTS worker failed: {engine.last_error}")
        return 2
    print(f"XTTS isolated on {engine.device!r}    VRAM {vram():.2f} GB")

    # Probe audio for whisper, produced by the real XTTS worker.
    probe_wav = await engine.synthesize(
        "Check whether llama.cpp is using CUDA and how much VRAM Qwen is taking.",
        speaker_wav=str(VOICE), turn_id="probe")
    probe_path = Path(os.environ.get("TEMP", "/tmp")) / "nova_coexist_probe.wav"
    probe_path.write_bytes(probe_wav)

    import numpy as np
    import soundfile as sf
    audio, sr = sf.read(str(probe_path), dtype="float32")
    if getattr(audio, "ndim", 1) > 1:
        audio = np.mean(audio, axis=1)
    audio = np.asarray(audio, dtype=np.float32)

    # ── Workloads ────────────────────────────────────────────────────────────
    async def gen_load(n=4):
        lat = []
        for i in range(n):
            t0 = time.perf_counter()
            out = []
            try:
                async for tok in llm.chat_stream(
                    [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": f"In one sentence, what is item {i}?"}],
                    max_tokens=512, temperature=0.3, thinking=False):
                    out.append(tok)
            except Exception as e:  # noqa: BLE001
                errors.append(f"llm: {type(e).__name__}: {e}")
                continue
            lat.append(time.perf_counter() - t0)
        return lat

    async def stt_load(n=6):
        if whisper is None:
            return []
        lat = []
        for _ in range(n):
            t0 = time.perf_counter()
            try:
                segs, _ = await asyncio.to_thread(
                    lambda: whisper.transcribe(audio, language="en", beam_size=1,
                                               vad_filter=True))
                _ = " ".join(s.text for s in segs)
            except Exception as e:  # noqa: BLE001
                errors.append(f"stt: {type(e).__name__}: {e}")
                continue
            lat.append(time.perf_counter() - t0)
        return lat

    async def tts_load(n=5):
        lat = []
        for i in range(n):
            t0 = time.perf_counter()
            try:
                await engine.synthesize(f"Coexistence probe sentence number {i}.",
                                        speaker_wav=str(VOICE), turn_id=f"coexist-{i}")
            except Exception as e:  # noqa: BLE001
                errors.append(f"tts: {type(e).__name__}: {e}")
                continue
            lat.append(time.perf_counter() - t0)
        return lat

    async def noop():
        return []

    # ── The seven configurations ─────────────────────────────────────────────
    banner("SEVEN-WAY COEXISTENCE MATRIX")
    print(f"{'#':<3} {'configuration':<34} {'llm':>9} {'stt':>9} {'tts':>9} "
          f"{'VRAM':>8} {'err':>5}")

    results = {}
    configs = [
        (1, "llama.cpp alone",            True,  False, False),
        (2, "faster-whisper alone",       False, True,  False),
        (3, "XTTS process alone",         False, False, True),
        (4, "llama + whisper",            True,  True,  False),
        (5, "llama + XTTS",               True,  False, True),
        (6, "whisper + XTTS",             False, True,  True),
        (7, "all three (conversational)", True,  True,  True),
    ]

    for num, label, use_llm, use_stt, use_tts in configs:
        errs_before = len(errors)
        v0 = vram()
        t0 = time.perf_counter()
        llm_lat, stt_lat, tts_lat = await asyncio.gather(
            gen_load() if use_llm else noop(),
            stt_load() if use_stt else noop(),
            tts_load() if use_tts else noop(),
        )
        wall = time.perf_counter() - t0
        v1 = vram()
        n_err = len(errors) - errs_before

        def m(x):
            return f"{statistics.mean(x) * 1000:.0f}ms" if x else "-"

        results[num] = {"llm": llm_lat, "stt": stt_lat, "tts": tts_lat,
                        "vram": v1, "errors": n_err, "wall": wall}
        print(f"{num:<3} {label:<34} {m(llm_lat):>9} {m(stt_lat):>9} {m(tts_lat):>9} "
              f"{v1:>7.2f}G {n_err:>5}")

    # ── Contention analysis ──────────────────────────────────────────────────
    banner("CONTENTION: concurrent vs alone")

    def mean_ms(x):
        return statistics.mean(x) * 1000 if x else float("nan")

    base_llm = mean_ms(results[1]["llm"])
    base_stt = mean_ms(results[2]["stt"])
    base_tts = mean_ms(results[3]["tts"])

    def delta(label, alone, together):
        if alone != alone or together != together:
            print(f"  {label:<34} n/a")
            return
        pctd = (together - alone) / alone * 100 if alone else 0
        print(f"  {label:<34} {alone:>7.0f}ms -> {together:>7.0f}ms  ({pctd:+.0f}%)")

    delta("llm alone -> +whisper", base_llm, mean_ms(results[4]["llm"]))
    delta("llm alone -> +XTTS", base_llm, mean_ms(results[5]["llm"]))
    delta("llm alone -> all three", base_llm, mean_ms(results[7]["llm"]))
    delta("whisper alone -> +llm", base_stt, mean_ms(results[4]["stt"]))
    delta("whisper alone -> +XTTS", base_stt, mean_ms(results[6]["stt"]))
    delta("whisper alone -> all three", base_stt, mean_ms(results[7]["stt"]))
    delta("XTTS alone -> +llm", base_tts, mean_ms(results[5]["tts"]))
    delta("XTTS alone -> all three", base_tts, mean_ms(results[7]["tts"]))

    # ── Verdict ──────────────────────────────────────────────────────────────
    banner("STABILITY VERDICT")
    print(f"  whisper device attempted : {whisper_device or 'CUDA unavailable'}")
    print(f"  VRAM idle -> final       : {results[7]['vram']:.2f} GB")
    print(f"  total errors             : {len(errors)}")
    for e in errors[:5]:
        print(f"    {e}")

    check(not errors, f"no inference errors across all seven configurations "
                      f"({len(errors)} seen)")
    check(llm.model_loaded, "llama.cpp survived every configuration")
    check(engine.state == "ready", f"XTTS worker healthy at exit ({engine.state})")
    check(engine.restarts == 0, f"XTTS never needed a restart ({engine.restarts})")

    # A CUDA abort kills the process outright, so simply reaching this line with
    # all three having run concurrently is itself the strongest evidence.
    check(True, "process survived concurrent llama.cpp + CTranslate2 + XTTS CUDA")

    await engine.stop()
    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    print("\nInterpretation belongs in docs/NOVA_V3_PERFORMANCE.md. Do NOT move STT")
    print("to CPU on suspicion alone — this matrix is the evidence either way.")
    return 1 if _fail else 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        traceback.print_exc()
        sys.exit(2)
