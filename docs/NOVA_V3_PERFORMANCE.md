# Nova V3 — Performance

---

# P2.5 — TTFT / hidden reasoning control

## The diagnosis

P2 reported TTFT of 114 ms – 33,164 ms. That number measured
time-to-first-**visible**-token, which conflates two different things.
Instrumenting the raw llama.cpp stream separated them:

| | median |
|---|---:|
| **model compute TTFT** (first token at all) | **35 ms** |
| first VISIBLE token | 10,336 ms |
| **delay attributable to hidden reasoning** | **10,301 ms** |

**Nova's inference was never slow.** ~10 seconds per turn was the model
generating a `<think>` block that was then discarded. This is a reasoning-policy
problem; no amount of inference tuning would have touched it.

## What did not work

Five prompt rewordings, measured at production budget:

| variant | simple median | simple P90 | empty |
|---|---:|---:|---|
| **P0 current production** | **8,071 ms** | **10,663 ms** | 5/18 |
| P1 persona only | 13,503 ms | 17,366 ms | 8/18 |
| P2 spoken-voice framing | 11,297 ms | 15,408 ms | 3/18 |
| P3 answer-first framing | 12,242 ms | 16,504 ms | 6/18 |
| P4 spoken + brevity | 9,959 ms | 14,717 ms | 3/18 |

**The shipped prompt was the best of them.** An earlier candidate that looked
like a 3× win did not reproduce — it was variance at n=18. Prompt wording does
not control this model's reasoning; the chat template does.

Also rejected: a 384-token budget gave a 79 ms median and **17/18 empty
replies**. Fast because it fails fast. Exactly the trap of judging on medians.

## What worked

Prefilling the assistant turn with an **already-closed** reasoning block, so the
model continues from after it instead of opening its own:

| | simple median | simple P90 | worst | empty |
|---|---:|---:|---:|---|
| production | 8,169 ms | 10,877 ms | 20,949 ms | 5/18 |
| **prefill** | **36 ms** | **38 ms** | **39 ms** | **0/18** |

Answers stay real — *"Running two CUDA consumers in one process can cause
illegal memory access because…"*. Empty replies go to zero because nothing can
overflow inside a block that was never opened.

## The escalation ladder, and two wrong versions of it

Reasoning is **not** switched off. It moved to where decisions are made: the
agent loop's `decide()` and deep mode still pass `thinking=True`, so tool
choice, planning and critique keep full native reasoning. By the time the spoken
reply is generated, tools have run and their observations are in the prompt.

The fallback took three iterations, each corrected by measurement:

1. **FAST only** → the complex-reasoning scenario returned empty. A hard
   question opens a second block on top of the prefill and overflows.
2. **FAST → DEEP immediately** → fixed the empties but put a **19,721 ms P90**
   back on turns as simple as "Good morning", because the fast-path failure is
   stochastic and escalating costs 14–20 s.
3. **FAST → FAST → DEEP (full budget)** → shipped. A second cheap attempt costs
   ~130 ms and rescues most stochastic failures; only a turn that fails twice
   pays for reasoning.

The final attempt also had to stop shrinking the budget. It previously escalated
to `thinking=True` **with** `max_tokens//3` — reasoning enabled at a small
budget is the exact 512-token overflow combination, and it made the reasoning
scenario fail all three attempts.

## P2 before / after

Same benchmark, same ten scenarios, same machine.

| metric | before | after | change |
|---|---:|---:|---|
| **TTFT median** | 12,127 ms | **130 ms** | **93× faster** |
| TTFT mean | 11,703 ms | 3,360 ms | 3.5× |
| **total turn median** | 14,359 ms | **628 ms** | **23× faster** |
| empty replies | 0/10 | 0/10 | — |
| recall gate + memory | 84 ms | 86 ms | unchanged |
| tool selection | 42 ms | 34 ms | unchanged |

**Eight of ten scenarios now answer in under 500 ms** where the median was
previously over 12 seconds.

## Where it is honestly not fixed

**Aggregate P90 is 14,631 ms**, down only ~20% from 18,215 ms. That number is
driven entirely by the two scenarios that escalated to DEEP on this run
(`reasoning` 17.6 s, `long_tts` 14.6 s).

For the `reasoning` case that is arguably correct — a hard question earning real
thought. For `long_tts` ("explain RAID 1 in four sentences") it is not obviously
correct, and **which scenarios escalate varies between runs**, because the
fast-path failure is stochastic. So:

* P90 across *simple* turns is ~455 ms and genuinely fixed.
* P90 across *all* turns still has a 15-second tail whenever escalation fires.
* A turn that escalates pays ~130 ms of wasted fast attempts on top.

The pathological delays on simple turns are gone. The tail on escalating turns
is not, and closing it needs either a complexity classifier before the first
call, or a bounded reasoning budget that can be cut off — neither attempted
here, and both should be judged against fresh measurements rather than this
paragraph.

---

# P2 — Formal benchmark baseline (BEFORE P2.5)

> Preserved as the pre-P2.5 baseline. The current numbers are in the
> before/after table above.

`tests/bench_nova_v3.py`, ten scenarios, real model + real memory + real
selector + real isolated XTTS. Memory runs in a temp directory so a benchmark
never pollutes Marcus's own.

```bash
venv/Scripts/python.exe tests/bench_nova_v3.py
```

## The headline finding, and it is uncomfortable

| Metric | mean | median | P90 |
|---|---:|---:|---:|
| recall gate + memory | 84 ms | 80 ms | 123 ms |
| tool selection | 42 ms | 32 ms | 48 ms |
| artifact resolution | **0 ms** | 0 ms | 0 ms |
| **TTFT** | **11,703 ms** | **12,127 ms** | **18,215 ms** |
| first speakable chunk | 11,878 ms | 12,414 ms | 18,258 ms |
| XTTS first chunk | 2,661 ms | — | — |
| total turn | 12,529 ms | 14,359 ms | 18,695 ms |

**Everything V2 and V3 P1 optimised is noise.** Memory retrieval, the recall
gate, tool selection and artifact resolution together account for ~126 ms of a
turn whose median is 14.4 seconds. The ~130 ms P1 bought is real, and it is
about 1% of the problem.

The dominant term is **hidden reasoning before the first visible token**, and its
variance is extreme:

| Scenario | TTFT |
|---|---:|
| reasoning | **114 ms** |
| project recall | 128 ms |
| greeting | 332 ms |
| followup | 8,874 ms |
| long_tts | 9,615 ms |
| freshness | 14,640 ms |
| ordinal | 14,945 ms |
| memory | 17,005 ms |
| multitool | 18,215 ms |
| tool | **33,164 ms** |

**114 ms to 33 seconds on the same model with the same settings.** Note the
ordering is not intuitive — the scenario literally named "complex reasoning"
was the *fastest*, and "what's the weather tomorrow?" was the slowest. Prompt
content, not question difficulty, is driving think length.

33 seconds to first word is not a conversational assistant. This is now the
single most important open problem in Nova, and it is far larger than anything
addressed in V2 or V3 so far.

## Per-scenario detail

```
scenario    gate     mem    sel tools    ttft   chunk    tts1    total  chars
greeting    skip       0      0     2      332     366       —      498     70
memory      R        123      4    10    17005   17248       —    17375     60
tool        R         62      0     4    33164   33404       —    33547    102
followup    R         69    214    10     8874    8919       —     9384     79
multitool   R         91      0     4    18215   18258       —    18695    128
ordinal     skip       0     32    10    14945   15176       —    15453    132
reasoning   R        122     32     9      114     302       —      968    369
project     R        196     46    10      128     282       —      650    134
freshness   R         62     45     9    14640   14891       —    15141    119
long_tts    R        112     48     8     9615    9938    2661    13577    485
```

| | |
|---|---|
| Tools exposed per turn | **7.6 of 20** registered |
| Recall-gate skips | 2/10 (greeting, ordinal) |
| LLM calls per turn | 1.0 |
| Empty replies | **0/10** |
| Wasted generations | 1 |
| Errors | 0 |
| VRAM at end | 8.86 GB |
| RSS at end | 7,985 MB |

The subsystems built in V2 behave exactly as designed — the gate skips the
greeting and the ordinal reference, artifact resolution is genuinely free
(sub-millisecond), and the selector shows 7.6 tools instead of 20. They are
just not where the time goes.

## A harness bug, recorded because the wrong numbers looked plausible

The first run reported **3/10 empty replies and 8 wasted generations** — which
reads exactly like the empty-generation regression returning. It was not. The
benchmark defaulted scenarios to `max_tokens=512`; production uses 1536, and
`core/runtime.py` documents that a small budget with thinking enabled lets the
hidden block overflow and strip to nothing.

Raising it to 1536 gave 0/10 empty and 1 wasted generation. Same mistake made
earlier in `live_voice_validation.py`; the fix is now commented in both so the
next harness does not repeat it a third time.

## Not measured — needs a microphone and a live browser

Omitted rather than estimated:

* VAD / speech-onset latency
* endpoint (trailing-silence) latency in a real room
* frontend audio finalisation and upload transport
* backend → frontend TTS delivery, playback start
* **end of user speech → first audible word** (the headline metric)
* barge-in stop latency — **P0: IMPLEMENTED, LIVE ACCEPTANCE PENDING**

## What this changes about V3 priorities

The remaining V3 phases (MCP, artifacts, speaker ID, CAD, gestures, devices,
productization) all add capability. **None of them touch the 12-second median.**
On the evidence above, bounding or suppressing hidden reasoning for
conversational turns is worth more to the experience than any of them, and it
should be raised in priority rather than left until after P9.

That is a recommendation, not a unilateral reordering — the phase order was set
deliberately and this is one benchmark run.

---


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
