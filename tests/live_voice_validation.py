"""LIVE hardware validation of the XTTS process-isolation fix.

This is the experiment `docs/JARVIS_V2_FINAL_REPORT.md` names as the one thing
standing between READY WITH LIMITATIONS and READY.

The hypothesis under test
-------------------------
In-process CUDA XTTS beside llama.cpp aborts Nova with

    CUDA error: an illegal memory access was encountered
      in ggml_backend_cuda_synchronize (ggml-cuda.cu:3235)

reproduced twice in ten minutes of ordinary speaking turns (core/gpu.py). The
claim is that the crash came from sharing a *process* (and therefore a CUDA
context), not from sharing the card — so running XTTS in a child process makes
GPU voice safe.

This script reproduces the exact conditions that crashed: real llama.cpp
generation on the GPU, with real XTTS CUDA synthesis running CONCURRENTLY on the
same card, for ten speaking turns. Synthesis is deliberately overlapped with
generation, because that overlap is what killed the process before.

It is NOT part of the offline suite — it needs the GPU, the model and XTTS, and
takes minutes. Deliberately named so run_tests.ps1 (test_*.py) skips it.

    venv\\Scripts\\python.exe tests\\live_voice_validation.py [turns]

Exit code 0 means the run completed with no CUDA abort.
"""

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("NOVA_TTS_DEVICE", "auto")
os.environ.setdefault("NOVA_TTS_ISOLATED", "1")

from core.llm_runtime import LLMRuntime          # noqa: E402
from core.voice.chunker import SpeechChunker     # noqa: E402
from core.voice.speech_text import to_spoken     # noqa: E402
from services.tts_client import IsolatedTtsEngine  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
VOICE = REPO / "voices" / "nova.wav"

PROMPTS = [
    "Good morning. Say hello back in one short sentence.",
    "In two sentences, what is a hard drive platter?",
    "Name three things to check on a home server. One line each.",
    "In one sentence, why is SSD latency lower than HDD latency?",
    "Give me a two sentence summary of what RAID 1 does.",
    "In one short sentence, what does 28 TB mean in plain terms?",
    "Two sentences: why do NAS drives run quieter than desktop drives?",
    "In one sentence, what is a filesystem journal?",
    "Two short sentences about why backups matter.",
    "One sentence: what is the difference between GB and GiB?",
]


def vram():
    import torch

    free, total = torch.cuda.mem_get_info()
    return (total - free) / 1e9, total / 1e9


def banner(msg):
    print(f"\n{'=' * 72}\n{msg}\n{'=' * 72}")


async def main():
    turns = int(sys.argv[1]) if len(sys.argv) > 1 else 10

    banner("LIVE XTTS PROCESS-ISOLATION VALIDATION")
    used, total = vram()
    print(f"VRAM at start:        {used:.2f} / {total:.1f} GB")
    if not VOICE.exists():
        print(f"FATAL: reference voice missing: {VOICE}")
        return 2

    # ── Load llama.cpp on the GPU ────────────────────────────────────────────
    model_path = next((p for p in (REPO / "model").glob("*.gguf")
                       if "mmproj" not in p.name.lower()), None)
    if model_path is None:
        print("FATAL: no GGUF model found in model/")
        return 2

    print(f"\nLoading {model_path.name} ...")
    t0 = time.perf_counter()
    llm = LLMRuntime(model_path=model_path, context_tokens=8192)
    await llm.initialize()
    print(f"  model loaded in {time.perf_counter() - t0:.1f}s  "
          f"gpu_offload={llm.gpu_status.__dict__}")
    used_llm, _ = vram()
    print(f"VRAM after model:     {used_llm:.2f} GB  (+{used_llm - used:.2f})")

    # ── Start the ISOLATED XTTS worker (its own process, its own context) ────
    print("\nStarting isolated XTTS worker (CUDA, separate process) ...")
    engine = IsolatedTtsEngine(
        cfg={"device": os.getenv("NOVA_TTS_DEVICE", "auto"), "allow_cpu_fallback": False},
        start_timeout_s=600.0, synth_timeout_s=120.0,
        on_event=lambda e, p: print(f"  [{e}] {p}") if e != "tts.loading" else None,
    )
    t0 = time.perf_counter()
    if not await engine.ensure_started():
        print(f"FATAL: XTTS worker failed to start: {engine.last_error}")
        return 2
    print(f"  XTTS ready in {time.perf_counter() - t0:.1f}s  "
          f"device={engine.device!r} pid={engine.pid} sr={engine.sample_rate}")
    if engine.device != "cuda":
        print(f"  WARNING: expected cuda, got {engine.device!r} — this run does NOT "
              f"test the isolation hypothesis")
    used_both, _ = vram()
    print(f"VRAM after XTTS:      {used_both:.2f} GB  (+{used_both - used_llm:.2f} for XTTS)")

    # ── Ten speaking turns, synthesis OVERLAPPING generation ─────────────────
    banner(f"{turns} SPEAKING TURNS (synthesis overlaps generation)")
    print(f"{'turn':>4} {'TTFT':>7} {'1st audio':>10} {'total':>7} {'chunks':>7} "
          f"{'audio s':>8} {'RTF':>6} {'VRAM':>7}")

    stats = []
    for i in range(turns):
        prompt = PROMPTS[i % len(PROMPTS)]
        turn_id = f"live-turn-{i}"
        chunker = SpeechChunker()
        pending: list[asyncio.Task] = []

        turn_start = time.perf_counter()
        ttft = None
        first_audio_at = None
        audio_bytes = 0
        chunk_count = 0

        async def synth(text: str):
            """Synthesise while generation continues — the crash condition."""
            nonlocal first_audio_at, audio_bytes
            wav = await engine.synthesize(text, speaker_wav=str(VOICE), turn_id=turn_id)
            if first_audio_at is None:
                first_audio_at = time.perf_counter()
            audio_bytes += len(wav)
            return wav

        # System prompt mirrors core/runtime.py's proven wording. The final
        # instruction is load-bearing with this model: without it the reply
        # arrives as an unclosed reasoning block and strips to nothing.
        messages = [
            {"role": "system", "content": (
                "You are Nova — Marcus's AI assistant. Talk like a real person, in plain "
                "prose. No lists, no markdown, no headings.\n\n"
                "IMPORTANT: Reply with ONLY what you'd actually say to Marcus out loud. Do "
                "NOT write any analysis, planning, notes, or a reasoning block — "
                "just say your reply directly.")},
            {"role": "user", "content": prompt},
        ]

        # thinking=True deliberately, matching core/runtime.py: this model
        # ignores '/no_think' and reasons anyway, and forcing it produces a long
        # unclosed <think> that overflows the budget and strips to nothing.
        # 1536 matches NOVA_MAX_TOKENS in production. A smaller budget is not a
        # smaller answer with this model — the hidden think block eats it and the
        # visible reply strips to nothing (measured: 400 tokens produced empty
        # replies on every non-trivial prompt).
        async for token in llm.chat_stream(messages, max_tokens=1536, temperature=0.4,
                                           thinking=True):
            if ttft is None:
                ttft = time.perf_counter() - turn_start
            for chunk in chunker.feed(token):
                spoken = to_spoken(chunk)
                if spoken:
                    chunk_count += 1
                    pending.append(asyncio.create_task(synth(spoken)))

        for chunk in chunker.flush():
            spoken = to_spoken(chunk)
            if spoken:
                chunk_count += 1
                pending.append(asyncio.create_task(synth(spoken)))

        results = await asyncio.gather(*pending, return_exceptions=True)
        errors = [r for r in results if isinstance(r, BaseException)]
        total_s = time.perf_counter() - turn_start

        # 16-bit mono at the worker's sample rate.
        audio_s = audio_bytes / 2 / (engine.sample_rate or 24000)
        rtf = (total_s / audio_s) if audio_s else float("nan")
        first_audio = (first_audio_at - turn_start) if first_audio_at else float("nan")
        used_now, _ = vram()

        print(f"{i:>4} {ttft or 0:>6.2f}s {first_audio:>9.2f}s {total_s:>6.2f}s "
              f"{chunk_count:>7} {audio_s:>7.1f}s {rtf:>6.2f} {used_now:>6.2f}G"
              + (f"  ERRORS: {errors[:1]}" if errors else ""))

        stats.append({"ttft": ttft, "first_audio": first_audio, "total": total_s,
                      "chunks": chunk_count, "audio_s": audio_s, "rtf": rtf,
                      "errors": len(errors)})

        if engine.state != "ready":
            print(f"\nFATAL: voice degraded mid-run: {engine.last_error}")
            return 1

    # ── Verdict ──────────────────────────────────────────────────────────────
    banner("RESULT")
    ok = [s for s in stats if s["ttft"] is not None]
    if ok:
        n = len(ok)
        print(f"turns completed:        {n}/{turns}")
        print(f"mean TTFT:              {sum(s['ttft'] for s in ok) / n:.2f}s")
        print(f"mean first audio:       {sum(s['first_audio'] for s in ok) / n:.2f}s")
        print(f"  (generation start -> first synthesised clip; excludes mic + STT)")
        print(f"best first audio:       {min(s['first_audio'] for s in ok):.2f}s")
        print(f"mean turn total:        {sum(s['total'] for s in ok) / n:.2f}s")
        print(f"mean speech produced:   {sum(s['audio_s'] for s in ok) / n:.1f}s")
        print(f"mean RTF (turn/audio):  {sum(s['rtf'] for s in ok) / n:.2f}")
        print(f"synthesis errors:       {sum(s['errors'] for s in ok)}")

    st = engine.status()
    used_end, _ = vram()
    print(f"\nvoice state:            {st['state']}  device={st['actual_device']}")
    print(f"clips synthesised:      {st['synth_count']}   errors={st['error_count']}   "
          f"restarts={st['restarts']}")
    print(f"pending requests:       {st['pending']} (must be 0)")
    print(f"VRAM at end:            {used_end:.2f} GB  "
          f"(drift from post-load: {used_end - used_both:+.2f} GB)")

    await engine.stop()

    crashed = engine.restarts > 0 or engine.state == "degraded"
    print("\n" + ("HYPOTHESIS NOT SUPPORTED: the worker died or degraded."
                  if crashed else
                  f"NO CUDA ABORT across {turns} speaking turns with concurrent "
                  f"llama.cpp generation and isolated XTTS CUDA synthesis."))
    return 1 if crashed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
