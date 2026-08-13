"""LIVE stress and fault injection on real hardware (brief §58, §59, §60).

Three things the offline suite cannot prove, because they need the actual GPU,
the actual model and an actual child process to kill:

  1. RAPID TURNS      — 20 short turns back to back. Watches for queue growth,
                        leaked threads, VRAM drift and out-of-order audio.
  2. GPU CONTENTION   — llama.cpp generating while the isolated XTTS process
                        synthesises AND the embedding model runs. Three CUDA
                        consumers, two processes.
  3. WORKER KILL      — SIGTERM the XTTS process mid-reply. The backend must
                        survive, report degraded honestly, and recover.

Not part of the offline suite (needs GPU + model), so it is deliberately not
named test_*.py.

    venv\\Scripts\\python.exe tests\\live_stress_validation.py
"""

import asyncio
import os
import signal
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("NOVA_TTS_DEVICE", "auto")

from core.llm_runtime import LLMRuntime            # noqa: E402
from core.voice.chunker import SpeechChunker       # noqa: E402
from core.voice.speech_text import to_spoken       # noqa: E402
from core.voice.turn import TurnRegistry           # noqa: E402
from services.tts_client import IsolatedTtsEngine, TtsUnavailable  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
VOICE = REPO / "voices" / "nova.wav"

_fail = False


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


# Mirrors core/runtime.py, including the deliberate absence of the literal think
# tag. The earlier version of this file named it, which is why the contention
# section saw 3 of 4 generations return nothing — the harness was reproducing the
# bug it was supposed to be measuring around.
SYSTEM = ("You are Nova. Reply in one short sentence of plain prose.\n\n"
          "IMPORTANT: Reply with ONLY what you'd say out loud. No analysis, no "
          "reasoning block — just the reply.")


async def main():
    if not VOICE.exists():
        print(f"FATAL: reference voice missing: {VOICE}")
        return 2
    model_path = next((p for p in (REPO / "model").glob("*.gguf")
                       if "mmproj" not in p.name.lower()), None)
    if model_path is None:
        print("FATAL: no GGUF in model/")
        return 2

    banner("SETUP")
    threads_at_start = threading.active_count()
    vram_start = vram()
    print(f"threads at start: {threads_at_start}   VRAM: {vram_start:.2f} GB")

    llm = LLMRuntime(model_path=model_path, context_tokens=8192)
    await llm.initialize()
    engine = IsolatedTtsEngine(
        cfg={"device": os.getenv("NOVA_TTS_DEVICE", "auto"), "allow_cpu_fallback": False},
        start_timeout_s=600.0, synth_timeout_s=120.0, max_restarts=3,
        on_event=lambda e, p: print(f"  [{e}] {p}") if e not in {"tts.loading"} else None,
    )
    if not await engine.ensure_started():
        print(f"FATAL: XTTS worker failed: {engine.last_error}")
        return 2
    print(f"model + XTTS ready on {engine.device!r} (pid {engine.pid})   "
          f"VRAM {vram():.2f} GB")
    turns = TurnRegistry()

    # ── 1. RAPID TURNS ───────────────────────────────────────────────────────
    banner("1. RAPID TURNS (20 back-to-back)")
    vram_before = vram()
    threads_before = threading.active_count()
    order_violations = 0
    synth_errors = 0
    clips = 0
    t0 = time.perf_counter()

    for i in range(20):
        turn = turns.start("stress")
        chunker = SpeechChunker()
        produced: list[str] = []
        messages = [{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": f"Say the number {i} in words, nothing else."}]

        pending = []
        async for token in llm.chat_stream(messages, max_tokens=256, temperature=0.3,
                                           thinking=False):
            for chunk in chunker.feed(token):
                spoken = to_spoken(chunk)
                if spoken:
                    pending.append((len(pending), spoken))
        for chunk in chunker.flush():
            spoken = to_spoken(chunk)
            if spoken:
                pending.append((len(pending), spoken))

        # Synthesise sequentially per turn and confirm clips come back in order.
        for idx, text in pending:
            try:
                await engine.synthesize(text, speaker_wav=str(VOICE), turn_id=turn.turn_id)
                produced.append(text)
                clips += 1
            except asyncio.CancelledError:
                pass
            except TtsUnavailable:
                synth_errors += 1
        if produced != [t for _, t in pending][:len(produced)]:
            order_violations += 1
        turns.finish(turn.turn_id)

    elapsed = time.perf_counter() - t0
    vram_after = vram()
    print(f"  20 turns in {elapsed:.1f}s   {clips} clips   {synth_errors} synth errors")
    check(order_violations == 0, f"audio stayed in order ({order_violations} violations)")
    check(engine.status()["pending"] == 0,
          f"no synthesis requests leaked (pending={engine.status()['pending']})")
    check(engine.restarts == 0, f"no worker restarts were needed ({engine.restarts})")
    check(engine.state == "ready", f"voice still ready ({engine.state})")
    drift = vram_after - vram_before
    check(abs(drift) < 1.0, f"VRAM drift over 20 turns is small ({drift:+.2f} GB)")
    thread_drift = threading.active_count() - threads_before
    check(thread_drift <= 2, f"no thread leak ({thread_drift:+d} threads)")
    check(turns.stats()["active"] == 0, f"no turn leak ({turns.stats()})")

    # ── 2. GPU CONTENTION ────────────────────────────────────────────────────
    banner("2. GPU CONTENTION (llama.cpp + XTTS process + embeddings, concurrent)")
    from memory.embeddings import embed_texts, embedding_available

    embeddings_ok = embedding_available()
    print(f"  embedding model available: {embeddings_ok}")

    contention_errors = []

    empty_generations = []

    async def gen_load():
        for i in range(4):
            messages = [{"role": "system", "content": SYSTEM},
                        {"role": "user", "content": f"In one sentence, what is item {i}?"}]
            out = []
            try:
                async for tok in llm.chat_stream(messages, max_tokens=512, temperature=0.3,
                                                 thinking=False):
                    out.append(tok)
            except Exception as e:  # noqa: BLE001
                contention_errors.append(f"generation {i} raised: {type(e).__name__}: {e}")
                continue
            if not "".join(out).strip():
                # NOT a contention failure. The model returns nothing visible at
                # a measurable baseline rate whether or not anything else is on
                # the GPU (docs/JARVIS_V2_BENCHMARKS.md, "wasted generations").
                # Counting it as contention damage would blame the wrong thing.
                empty_generations.append(i)

    async def tts_load():
        turn = turns.start("contend")
        for i in range(6):
            try:
                await engine.synthesize(f"This is contention test sentence number {i}.",
                                        speaker_wav=str(VOICE), turn_id=turn.turn_id)
            except Exception as e:  # noqa: BLE001
                contention_errors.append(f"tts {i}: {type(e).__name__}: {e}")
        turns.finish(turn.turn_id)

    async def embed_load():
        if not embeddings_ok:
            return
        for i in range(8):
            try:
                await asyncio.to_thread(embed_texts, [f"embedding contention probe {i}"] * 4)
            except Exception as e:  # noqa: BLE001
                contention_errors.append(f"embed {i}: {type(e).__name__}: {e}")

    vram_peak_before = vram()
    t0 = time.perf_counter()
    await asyncio.gather(gen_load(), tts_load(), embed_load())
    print(f"  concurrent load finished in {time.perf_counter() - t0:.1f}s   "
          f"VRAM {vram():.2f} GB (was {vram_peak_before:.2f})")
    check(not contention_errors,
          f"no crashes or exceptions under contention ({contention_errors[:2]})")
    check(engine.state == "ready", f"voice survived contention ({engine.state})")
    check(llm.model_loaded, "llama.cpp survived contention")
    check(engine.restarts == 0, f"contention did not kill the voice worker ({engine.restarts})")
    if empty_generations:
        print(f"  note: {len(empty_generations)}/4 generations returned nothing visible "
              f"(pre-existing, see benchmarks doc — not a contention failure)")

    # ── 3. WORKER KILL ───────────────────────────────────────────────────────
    banner("3. WORKER KILL (SIGTERM the XTTS process mid-flight)")
    victim_pid = engine.pid
    print(f"  killing XTTS worker pid {victim_pid} ...")
    try:
        os.kill(int(victim_pid), signal.SIGTERM)
    except Exception as e:  # noqa: BLE001
        print(f"  (could not signal pid {victim_pid}: {e})")

    # The client's reader thread should notice the corpse.
    for _ in range(150):
        await asyncio.sleep(0.1)
        if engine.state == "degraded":
            break
    check(engine.state == "degraded",
          f"a killed worker is reported degraded, not silently hung ({engine.state})")
    check(bool(engine.last_error), f"the reason is recorded ({engine.last_error!r})")

    # The whole point: text still works while the voice is down.
    # A few attempts, because the model has an independent baseline rate of
    # returning nothing visible; one empty reply here would say nothing about
    # whether the dead worker broke text chat.
    text_ok = False
    for _ in range(3):
        out = []
        async for tok in llm.chat_stream(
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": "Say 'text still works' and nothing else."}],
            max_tokens=512, temperature=0.3, thinking=False):
            out.append(tok)
        if "".join(out).strip():
            text_ok = True
            break
    check(text_ok, "text generation survives a dead voice worker")

    # Synthesis against a dead worker transparently recovers rather than
    # failing — which is the right behaviour for "Nova, say something" — but the
    # recovery MUST be charged against the restart cap, or a crash-looping
    # worker could be respawned forever through this path.
    restarts_before = engine.restarts
    recovered = None
    try:
        recovered = await engine.synthesize("auto recovery probe",
                                            speaker_wav=str(VOICE), turn_id="x")
    except TtsUnavailable:
        recovered = None
    check(engine.restarts > restarts_before,
          f"transparent recovery is counted against the cap "
          f"({restarts_before} -> {engine.restarts})")
    check(recovered is None or len(recovered) > 1000,
          "synthesis either recovers and produces audio, or fails cleanly — never hangs")

    # And an explicit restart still works.
    print("  restarting worker ...")
    ok = await engine.restart(reason="stress test kill")
    check(ok, "worker restarts after being killed")
    check(engine.state == "ready", f"state recovers to ready ({engine.state})")
    check(engine.pid != victim_pid, f"a NEW process is serving (old {victim_pid}, new {engine.pid})")
    wav = await engine.synthesize("Recovered and speaking again.",
                                  speaker_wav=str(VOICE), turn_id="recovered")
    check(len(wav) > 1000, f"the restarted worker actually synthesises ({len(wav)} bytes)")

    # ── FINAL ────────────────────────────────────────────────────────────────
    banner("FINAL HEALTH")
    st = engine.status()
    print(f"  state={st['state']} device={st['actual_device']} pending={st['pending']} "
          f"clips={st['synth_count']} errors={st['error_count']} restarts={st['restarts']}")
    check(st["pending"] == 0, "no pending requests at exit")
    check(st["state"] == "ready", "voice healthy at exit")

    await engine.stop()
    await asyncio.sleep(0.5)
    vram_end = vram()
    thread_end = threading.active_count()
    print(f"  VRAM end {vram_end:.2f} GB (start {vram_start:.2f})   "
          f"threads end {thread_end} (start {threads_at_start})")
    check(thread_end - threads_at_start <= 4,
          f"no runaway thread growth ({thread_end - threads_at_start:+d})")

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
