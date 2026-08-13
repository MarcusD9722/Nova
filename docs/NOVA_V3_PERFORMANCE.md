# Nova V3 — Performance

Every number here came from a run on Marcus's RTX 5080. Reproduce with:

```bash
venv/Scripts/python.exe tests/bench_stt_v3.py
venv/Scripts/python.exe tests/bench_cuda_coexist_v3.py
```

---

## Read this first: what synthetic speech can and cannot prove

Probe audio is synthesised by Nova's own XTTS worker and pushed through the
identical decode → transcribe path a browser upload takes.

| Valid | Not valid |
|---|---|
| Stage timing (decode, inference, endpointing) | **Word error rate** |
| Config comparisons (A vs B, same audio) | Real-room noise, mic distance, accent |
| Concurrency and stability | Whether Marcus is understood |
| Regression detection | Human pause length |

Synthetic speech is cleaner than a real room and would flatter accuracy. **No
accuracy claim in this document is a human-speech result.** That needs a live
microphone and is listed under "Still unmeasured".

---

## P1.1 — Where STT latency actually goes

Measured per utterance, base model, CUDA float16, 7 probes.

| Stage | Cost | Note |
|---|---|---|
| **ffmpeg decode (subprocess)** | **96 ms** (wav), 66 ms (webm) | spawn + decode, **every utterance** |
| Whisper inference (as shipped) | 82 ms mean, 109 ms P90 | |
| File read (soundfile) | <5 ms | negligible |

**The decode step costs more than the transcription.** That was not the expected
answer — the assumption going in was that faster-whisper dominated. It does not.

Note wav decodes *slower* than webm (96 vs 66 ms) despite being the simpler
format, which confirms the cost is process **spawn**, not decode work.

---

## P1.2 — STT configuration sweep

| Config | Mean | Median | P90 |
|---|---|---|---|
| Baseline (as shipped) | 82 ms | 92 ms | 109 ms |
| `condition_on_previous_text=False` | 58 ms | 64 ms | 75 ms |
| `without_timestamps=True` | **50 ms** | 54 ms | 62 ms |
| `vad_filter=False` | 42 ms | 43 ms | 50 ms |
| `beam_size=5` | 99 ms | 105 ms | 153 ms |

| Model | Mean | Median |
|---|---|---|
| `base` | **73 ms** | 75 ms |
| `small` | 106 ms | 104 ms |

### Applied
`condition_on_previous_text=False` + `without_timestamps=True` — **82 ms → ~50 ms**.
Both are free: `condition_on_previous_text` exists for long-form continuity
across chunks and Nova transcribes one utterance per request (it is also a known
hallucination source), and word timestamps were being computed then discarded
because only `.text` is ever read.

### Deliberately NOT applied
* **`vad_filter=False`** was fastest (42 ms) but it is what trims leading and
  trailing silence. Trading real-audio robustness for 8 ms is a bad deal.
* **`beam_size=5`** is slower with no measured benefit on these probes.
* **`small` model** costs +33 ms. Revisit only if live accuracy proves `base`
  insufficient — that is an accuracy question this benchmark cannot answer.

---

## P1.3 — Vocabulary biasing

`initial_prompt` conditioning, synthetic probes. **Read this as "does biasing
change the decode", not as a WER result.**

| Spoken | Unbiased | Biased |
|---|---|---|
| llama.cpp … Qwen | "**Lama.cpp** … **QN**" | "**llama.cpp** … **Qwen**" |
| XTTS … Chroma | "**XCTs** … Chroma" | "**XTTS** … Chroma" |
| RTX 5080 … 5090 | "RTX **5,080** … **5,090**" | "RTX **5080** … **5090**" |

Every one of these is a term Marcus uses constantly, and the model got them
wrong on clean synthetic audio. Applied via `initial_prompt`, controlled by
`NOVA_STT_BIAS`, extensible with `NOVA_STT_VOCABULARY`.

**Deliberately not a post-hoc search/replace table** — that would happily
"correct" a word he actually said. This is decoder conditioning, the mechanism
that exists for the job.

---

## P1.4 — Endpoint detection

The shipped `trailingSilenceMs = 700` had never been validated. Simulated
against the real `recorder.ts` VAD state machine, with a 2 s noise-floor tail
appended so the silence timer can actually elapse.

Cells are **(endpoint fire − end of speech)** — the dead air Marcus sits through
after he stops talking.

| trailing | Cuts speaker off | Mean dead air |
|---|---|---|
| 300 ms | **7 of 7 probes** | — |
| 450 ms | **3–5 of 7** (incl. the mid-sentence pause) | — |
| **600 ms** | **0 of 7** | **−4 ms** |
| 700 ms (was shipped) | 0 of 7 | +96 ms |
| 900 ms | 0 of 7 | +296 ms |

`speechThreshold` (0.020 / 0.025 / 0.035) made almost no difference; 0.025 kept.

### Applied
`trailingSilenceMs: 700 → 600`. **Saves ~100 ms of dead air on every turn**, with
zero cut-offs across all seven probes including one with a deliberate
mid-sentence "um" pause.

### The caveat, stated plainly
450 ms already cuts, so there is only ~150 ms of headroom, and **XTTS pauses are
cleaner and shorter than a real "um… hang on" pause.** If Marcus gets clipped
mid-sentence, 700 is the first thing to put back. The comment in `recorder.ts`
says exactly that.

### A harness bug worth recording
The first run reported 600/700/900 ms as *identical*, which is impossible. Cause:
XTTS clips end the instant the words do, so with no trailing audio the silence
timer could never elapse and every setting hit the same end-of-buffer fallback.
The numbers above are from the corrected run. A benchmark that cannot distinguish
its own variables is worse than no benchmark.

---

## P1.5 — CUDA coexistence: is faster-whisper safe beside llama.cpp?

Nova learned once, expensively, that a second CUDA consumer in the backend
process aborts it (core/gpu.py). `backend/app.py` puts a **CTranslate2** CUDA
context in that same process. Different runtime from torch — but that is a
hypothesis, not evidence.

Seven configurations, real workloads:

| # | Configuration | llm | stt | tts | VRAM | errors |
|---|---|---|---|---|---|---|
| 1 | llama.cpp alone | 15550 ms | – | – | 9.04 G | 0 |
| 2 | faster-whisper alone | – | 119 ms | – | 9.04 G | 0 |
| 3 | XTTS process alone | – | – | 840 ms | 9.04 G | 0 |
| 4 | llama + whisper | 16380 ms | 272 ms | – | 9.04 G | 0 |
| 5 | llama + XTTS | 14856 ms | – | 2501 ms | 9.04 G | 0 |
| 6 | whisper + XTTS | – | 138 ms | 873 ms | 9.04 G | 0 |
| 7 | **all three** | 16467 ms | 339 ms | 2598 ms | 9.04 G | **0** |

**Verdict: coexistence is stable. STT stays on CUDA.**

Zero inference errors, zero CUDA aborts, zero XTTS restarts, VRAM flat at 9.04 GB
throughout. A CUDA abort kills the process outright, so simply completing
configuration 7 is itself the evidence.

**STT was NOT moved to CPU.** Doing so on suspicion would have been a silent
regression with no measurement behind it — the exact mistake this matrix existed
to prevent.

### Contention is real, and asymmetric

| | Alone | With everything | Change |
|---|---|---|---|
| llama.cpp | 15550 ms | 16467 ms | **+6%** |
| faster-whisper | 119 ms | 339 ms | **+185%** |
| XTTS | 840 ms | 2598 ms | **+209%** |

The 9B dominates the card; the small consumers absorb the time-slicing. Nothing
breaks, but **XTTS paying +198% while llama.cpp generates is the single largest
contention cost in the voice path**, and it lands directly on first-audio
latency. That is a scheduling question for a later phase, not a stability one.

---

## Summary: what P1 bought

| Change | Saving |
|---|---|
| `condition_on_previous_text=False` + `without_timestamps=True` | ~32 ms/utterance |
| `trailingSilenceMs` 700 → 600 | ~100 ms/turn |
| **Total off the critical path** | **~130 ms** |
| Vocabulary biasing | accuracy, not latency |

---

## Still unmeasured

1. **Human word error rate.** Needs a live microphone. Synthetic speech cannot
   answer it and this document does not pretend to.
2. **The 96 ms ffmpeg spawn.** Now the largest single item in the STT path.
   Killing it means decoding webm/opus in-process — PyAV or torchaudio — which
   is a new dependency and was not added unilaterally. **Concrete proposal:**
   try `soundfile` directly first (free, works when the client sends WAV), fall
   back to ffmpeg otherwise; then evaluate PyAV against the 96 ms.
3. **Real VAD onset latency** — how long the browser analyser takes to notice
   speech has *started*. Matters for barge-in (P0) and needs a mic.
4. **Transport, queueing and playback-start** — need a live browser session.
5. **Whether 600 ms clips real human pauses.**
