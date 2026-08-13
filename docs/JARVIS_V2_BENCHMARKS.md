# JARVIS V2 — Benchmarks

Every number here was produced by running `tests/bench_jarvis_v2.py` on
Marcus's machine. Nothing is estimated, extrapolated, or carried over from the
engineering brief.

**Read the "Not measured" section first.** The headline metric the brief asks
for — *user stops speaking → Nova's first audible word* — was **not measured**,
and this document does not pretend otherwise.

Reproduce with:

```bash
venv/Scripts/python.exe tests/bench_jarvis_v2.py
```

---

## 0. LIVE GPU VALIDATION — the central hypothesis, tested

**Run on 2026-08-12, RTX 5080, `tests/live_voice_validation.py 10`.**

The claim under test: the illegal-memory-access crash came from XTTS sharing a
*process* (and therefore a CUDA context) with llama.cpp, not from sharing the
card — so a child process makes GPU voice safe.

The script reproduces the exact conditions that crashed before: real llama.cpp
generation on the GPU, with real XTTS **CUDA** synthesis running concurrently on
the same card, for ten speaking turns. Synthesis is deliberately overlapped with
generation, because that overlap is what killed the process.

```
model loaded in 5.0s      gpu_offload_confirmed
XTTS ready in 46.8s       device='cuda'  pid=29944  sr=24000

turn    TTFT  1st audio   total  chunks  audio s    RTF    VRAM
   0   0.41s      1.27s   1.27s       1     1.4s   0.92   8.79G
   1  10.44s     13.06s  15.10s       3    18.6s   0.81   8.79G
   2   0.08s      1.58s   5.14s       5    20.0s   0.26   8.79G
   3   6.67s      9.02s  10.81s       3    15.9s   0.68   8.79G
   4   0.10s      2.93s   4.61s       3    16.5s   0.28   8.79G
   5   0.11s      2.66s   2.66s       1     8.4s   0.32   8.79G
   6   0.09s      1.75s   7.44s       5    27.2s   0.27   8.79G
   7   8.50s     10.35s  11.42s       2    12.1s   0.94   8.79G
   8   5.96s      7.55s   9.25s       3    14.3s   0.65   8.79G
   9  27.21s     29.13s  30.55s       2    14.3s   2.14   8.79G
```

| Result | Value |
|---|---|
| Turns completed | **10/10** |
| Clips synthesised on CUDA, concurrent with generation | **28** |
| Synthesis errors | **0** |
| Worker restarts / degradations | **0** |
| **CUDA aborts** | **0** |
| VRAM: model | 7.36 GB |
| VRAM: drift with XTTS resident and 28 clips synthesised | **+0.05 GB** |
| Voice state at end | `ready`, `device=cuda`, 0 pending |

**The hypothesis holds.** Ten speaking turns with concurrent CUDA synthesis and
generation, no abort — the same bar the CPU workaround was originally accepted
on, and the condition that previously reproduced the crash twice in ten minutes.

### Latency breakdown

`first audio` is measured from generation start, so it contains TTFT. Separating
them is what shows where the time actually goes:

| | Mean | Best |
|---|---|---|
| TTFT (model thinking before any visible token) | 5.96 s | 0.08 s |
| **XTTS first-chunk synthesis** (first audio − TTFT) | **≈1.97 s** | 0.86 s |
| Turn total | 9.83 s | 1.27 s |
| Speech produced per turn | 14.9 s | — |
| RTF (turn wall-clock ÷ audio produced) | **0.73** | 0.26 |

Two honest observations:

* **RTF 0.73 means Nova generates speech faster than it can be spoken.** Once
  the first chunk lands, the voice never has to wait for the pipeline.
* **TTFT, not TTS, dominates.** The high-variance turns (10.4 s, 27.2 s) are the
  reasoning model thinking before emitting a visible token, and are unrelated to
  this round's work. The voice contributes a fairly stable ~2 s to first audio.
  If conversational latency is attacked again, TTFT is where the time is.

### Two flaws in the harness, found and fixed while running it

Recorded because both produced misleading results first:

1. `max_tokens=400` with `thinking=True` produced **empty replies on every
   non-trivial prompt** — the hidden reasoning block consumed the budget and the
   visible reply stripped to nothing, exactly as `core/runtime.py` documents.
   Only 2 of 10 turns produced speech. Fixed by using 1536 (production's
   `NOVA_MAX_TOKENS`) and production's system-prompt wording.
2. `thinking=False` did not help — this model ignores `/no_think`, also as
   documented. The first run had the same 2/10 failure.

A run reporting "no crash" while 8 of 10 turns silently generated nothing would
have been worthless. The turn table is printed per-turn precisely so that
failure mode is visible rather than hidden in an average.

### What this validation does NOT cover

* It drives `LLMRuntime` and `IsolatedTtsEngine` **directly**, not through the
  FastAPI `/chat/stream` endpoint. The CUDA isolation and the engine are
  validated; the backend's SSE wiring around them is covered only by the offline
  suite.
* No microphone and no STT, so the brief's full metric — *user stops speaking →
  first audible word* — still excludes capture and transcription time.
* Ten turns is the bar the original bug was judged on, not proof of stability
  over hours.

---

## 0b. WHERE TTFT ACTUALLY GOES — and the thing it turned out to be

The live validation showed TTFT (mean 5.96 s) dwarfing synthesis (~1.97 s), so
§41 asks for a KV/prefix-cache audit. `tests/bench_ttft.py` measured the three
candidate explanations on the real llama.cpp runtime rather than reasoning about
them. **Two of the three are dead ends, and the real cause was something else
entirely.**

### Prompt evaluation — small

| System prompt | TTFT |
|---|---|
| ~0.1 KB | 0.091 s |
| ~7 KB | 0.305 s |
| **Cost of 7 KB of extra grounding** | **+0.214 s** |

Nova's real grounding block is smaller than 7 KB. Prompt size is not the problem.

### Prefix cache — irrelevant here

| | TTFT |
|---|---|
| Same long prefix, repeated call | 0.310 s |
| Prefix **changed** between calls | 0.312 s |
| **Cost of invalidating the prefix** | **+0.002 s** |

Invalidating the cached prefix costs essentially nothing measurable. **Reordering
Nova's prompt to put stable content first would buy nothing.** This is a useful
negative result: §41's suggested work is not worth doing on this runtime, and the
brief's instruction not to reorder prompts on theory alone is exactly why it was
measured first.

### The real cause — wasted generations

`core/llm_runtime.py::chat_stream` retries up to three times whenever a
generation produces no *visible* output, which happens when the reasoning model
spends its whole budget on an unclosed `<think>` block. Each retry is a **full
extra generation**. Nothing reported it above debug log level, so it presented
as latency.

Measured over one benchmark run (24 generations plus probes):

| | Value |
|---|---|
| Wasted generations | **32** |
| Turns that produced nothing at all after 3 attempts | **9** |
| Observed TTFT, thinking=True | 12.59 s |
| **True first-attempt TTFT** (uncontaminated samples) | **3.87 s**, best 0.12 s |

A plain "Good morning" measured 18–20 s to first token — not because greeting
Nova is hard, but because two full 1536-token generations were thrown away
first.

### The fix, and its measured effect

The retry loop repeated **identically**, so a prompt that reliably triggered
think-overflow burned three identical generations and still returned nothing.
Attempts now escalate: attempt 1 honours the caller, attempt 2 forces
`/no_think` (the hidden reasoning is what failed), attempt 3 also cuts the budget
and asks outright for a direct answer.

| | Before | After |
|---|---|---|
| Turns producing nothing at all | 9 | **4** |
| Worst-case turn (failing reasoning prompt) | 38.88 s | **10.26 s** |
| Observed TTFT, thinking=True | 12.59 s | **9.64 s** |
| Wasted generations | 32 | 32 |

Wasted generations are unchanged, and that is expected: escalation only improves
attempts 2 and 3, not the first. What improved is how often a turn ends in
nothing, and how fast a doomed turn gives up.

**Caveat: one run per configuration, temperature 0.4, n=8 prompts.** The model is
stochastic and per-prompt variance is large. The `empty_exhausted` count (9 → 4,
measured across the whole run including probes) is the more reliable signal; the
per-section figures should not be read as precise.

`empty_retries` and `empty_exhausted` are now in `LLMRuntime.usage_stats`, so
`/status` reports them and this stops being invisible.

### Root cause of the empty generations — forensics

`tests/bench_empty_generations.py` captures the **raw** completion (before
`<think>` stripping) so the retry loop cannot hide what happened. Over 18
generations with the shipped prompt, 5 produced nothing visible. Every one of
them looked like this:

| | |
|---|---|
| Failures with an **unclosed `<think>`** | **5 of 5** |
| Failures that were empty **at the model** | **0 of 5** |
| `finish_reason` | `stop` — *not* `length` |
| Raw output length | 294–1696 chars |

Two things follow immediately, and both contradict the assumption in the code
comments:

1. **The model is not running out of budget.** `finish_reason` is `stop` and the
   output is a few hundred characters against a 1536-token budget. The existing
   comment in `core/runtime.py` ("burns its whole generation on an unclosed
   `<think>`") describes the symptom but not the mechanism.
2. **The model is generating plenty — it is all inside the think block**, and
   generation terminates before the block closes.

The raw tails say what it was doing when it stopped:

```
'...Do NOT write any analysis, planning, notes, or a reasoning/'
'...notes, or a reasoning/'
'...Do NOT write any analysis, planning, notes, or reasoning/'
```

The model is **quoting Nova's own system prompt back to itself** inside its
reasoning, and generation dies at exactly the point where the next token is
`<think>` — the literal tag that appears in that instruction:

```
"Do NOT write any analysis, planning, notes, or a reasoning/<think> block"
                                                            ^^^^^^^
```

So the instruction telling the model not to emit a think block contains the
think-block tag, and reciting it is what breaks the generation. That is a
strong, evidence-backed hypothesis, and the fix it suggests is trivial: say the
same thing without naming the tag.

### Confirmed: the prompt was causing it

Config matrix, 5 samples × 6 prompts = 30 generations per configuration:

| Configuration | Empty rate | Unclosed `<think>` | Mean time | Mean reply |
|---|---|---|---|---|
| **Shipped prompt (names `<think>`)** | **30%** (9/30) | 9 | 7.51 s | 82 chars |
| Tag **and** prohibition removed | 7% (2/30) | 2 | 12.12 s | 117 chars |
| **Tag removed, prohibition kept** | **0%** (0/30) | 0 | **8.92 s** | **144 chars** |

**Naming the think tag in the instruction was causing 30% of turns to produce
nothing.** Removing it dropped that to zero across 30 samples — the largest
single latency and quality win found in this whole program, and it is one line
of prose.

The middle row is the instructive one. The first attempt at the fix removed the
tag *and* the words "or a reasoning block", which stopped the failures but also
stopped discouraging the reasoning — so the model then thought on every turn:
slower (12.12 s vs 8.92 s) and shorter replies. Keeping the prohibition while
dropping the tag is better on **every** axis. Forbid the block; do not name it.

Two secondary results worth keeping:

* The sampling knobs that looked promising in an earlier, smaller matrix
  (`/no_think`, dropping stop sequences) were noise. An 18-sample run put
  `/no_think + no stop` at 11% and it would have been plausible to ship;
  it did not survive a larger sample, and combining it with the prompt fix made
  things *worse* (7% → 20%). The cause was in the text, not the parameters.
* This is why the script prints its own n and refuses to draw conclusions from
  one run.

### The fix

`core/runtime.py`'s reply prompt now reads:

```
Do NOT write any analysis, planning, notes, or a reasoning block — just say your reply directly.
```

instead of

```
Do NOT write any analysis, planning, notes, or a reasoning/<think> block — ...
```

The comment at the call site records the measurements, so nobody helpfully adds
the tag back.

### What is still open

0 of 30 is a strong result, not a proof of zero. The retry loop stays as
insurance, and `empty_retries` / `empty_exhausted` in `/status` will show if the
rate creeps back up on a different prompt, model or quantisation.

---

## 0c. LIVE STRESS AND FAULT INJECTION (§58, §59, §60)

`tests/live_stress_validation.py`, on the RTX 5080 with the model and isolated
XTTS both resident. **All pass.**

**§58 — 20 rapid turns:** 54.9 s, 14 clips, 0 synthesis errors, audio stayed in
order, 0 leaked requests, 0 restarts, **+0.05 GB VRAM drift**, +1 thread, no turn
leak.

**§59 — GPU contention** (llama.cpp generating + isolated XTTS synthesising +
bge-small embedding, concurrently — three CUDA consumers across two processes):
no crashes, no exceptions, voice and model both survived, 0 restarts. 3 of 4
generations returned nothing visible, which is the pre-existing empty-generation
rate above, not contention damage — the test reports it separately rather than
blaming the wrong subsystem.

**§60 — worker kill** (SIGTERM the XTTS process): death detected and reported
`degraded` with the real reason; text generation unaffected; synthesis
transparently recovered on the next call; explicit restart produced a new process
(pid 20712 → 12492) that synthesised 153 KB of audio. VRAM returned to baseline,
+2 threads.

### A bug this found

Transparent recovery in `ensure_started()` **bypassed the restart cap** — it
called `_start_locked()` directly, so `restarts` never incremented and a
crash-looping worker could be respawned forever through `synthesize()`.
Fixed by counting the restart in `_start_locked()` itself, so every path is
charged against the same cap. Regression test:
`test_transparent_recovery_is_capped` in `tests/test_tts_isolation_jv2.py`.

---

## Not measured, and why

| Metric | Status | Reason |
|---|---|---|
| User stops speaking → first audible word (incl. mic + STT) | **Partially measured** | Generation start → first audio is measured above. Capture and STT are not. |
| End-to-end through `/chat/stream` | **Not measured** | Validation drove the runtime directly. |
| Tokens/sec | **Not measured** | Not instrumented this round. |
| STT latency | **Not measured** | Unchanged by this work. |
| Stability beyond ten turns | **Not measured** | — |

The remaining numbers below are CPU-side and deterministic. They describe work
removed from the critical path.

---

## 1. Speech chunker — time to first speakable chunk

The voice cannot start until the chunker yields something, so the size of the
first chunk is a direct proxy for time-to-first-audio. Measured by streaming
each reply one character at a time and recording when the first chunk appears.

| Reply | V1 chars | V2 chars | Change |
|---|---:|---:|---|
| 1 — drive comparison | 32 | 32 | 0% |
| 2 — long run-on with clauses | 126 | **57** | **−55%** |
| 3 — starts with "Dr. Chen's review…" | 3 | 48 | +1500% ⚠ |
| 4 — short greeting | 13 | 13 | 0% |
| **Mean** | **44** | **38** | **−14%** |

⚠ **Reply 3 is not a regression.** V1's 3-character "first chunk" was the string
`Dr.` — it split on the abbreviation and would have had XTTS pronounce
"Doctor." as a complete utterance, with a full stop and a pause, before saying
anything else. Starting 45 characters later and saying a real clause is the
correct behaviour. Counting that as a latency win for V1 would be measuring the
bug as a feature.

The genuine win is reply 2: a long run-on sentence with no terminator. V1 waited
126 characters (its hard-cut threshold); V2 cut at a clause boundary after 57.

### Chunk correctness

Cases where V1 produced the wrong number of utterances:

| Input | V1 | V2 | Correct |
|---|---:|---:|---:|
| `The Exos holds 3.5 TB per platter. That is a lot.` | 2 | 2 | 2 |
| `Dr. Chen reviewed it. He liked it.` | **3** | 2 | 2 |
| `Check e.g. the WD Gold. It is quieter.` | **3** | 2 | 2 |
| `Open README.md and read it. Then tell me.` | 2 | 2 | 2 |

**Mis-splits: V1 = 2/4, V2 = 0/4.**

The full regression set (abbreviations, decimals, initials, filenames, URLs,
dotted identifiers, emails, version numbers, ellipses) is in
`tests/test_voice_chunker_jv2.py`.

---

## 2. Tool selection

The agent loop embeds the tool catalogue in **every** `decide()` prompt, and
`decide()` runs up to `step_budget` (default 6) times per turn. Token counts are
approximate (chars ÷ 4) and are used only as a ratio.

Registry: **49 tools**. Full catalogue: **~736 tokens** per `decide()` prompt.

### Steady state — bge-small resident

This is the normal configuration in a live session, because memory keeps the
embedding model loaded anyway.

| Metric | Value |
|---|---|
| Tools shown (mean) | **5.8 of 49** |
| Catalogue (mean) | **~94 tokens (87% smaller)** |
| Selector cost per turn | **12.42 ms** |
| Per turn at `step_budget=6` | **~4,416 → ~564 tokens** |
| Stage mix over 28 queries | 17 deterministic, 11 semantic, 0 widened, 0 fail-open |

### Cold process — embeddings not yet loaded

What the first turn after boot actually sees. The selector refuses to block on
the model load (see §4) and ranks lexically instead, which widens the list.

| Metric | Value |
|---|---|
| Tools shown (mean) | **11.7 of 49** |
| Catalogue (mean) | **~184 tokens (75% smaller)** |
| Selector cost per turn | **0.13 ms** |
| Per turn at `step_budget=6` | ~4,416 → ~1,107 tokens |
| Stage mix | 17 deterministic, 7 widened, 4 fail-open-to-all |

Degrading from 87% to 75% reduction when the embedding model is missing is the
intended trade: the lexical path buys recall with precision, never the reverse.

### Accuracy

Measured over the 28-query dataset in `tests/test_tool_selector_jv2.py`:

| Configuration | Required tool retained |
|---|---|
| Semantic (bge-small) | **28/28** |
| Lexical only | **28/28** |
| Embedding backend raising | 28/28 (fails open to all 49) |

Recall is the tracked metric. A wrong exclusion is a silent capability loss; an
extra candidate costs a few dozen tokens.

---

## 3. Recall gate

`MemoryUnifier.search()` previously ran unconditionally on every turn
(`core/runtime.py:1158`).

| Metric | Value |
|---|---|
| Cost per decision | **3.1 microseconds** (20,000 decisions in 0.062 s) |
| Model calls | **0** — proven structurally by an AST test over the module's imports and the absence of any `await` |
| Sample skip rate | 3 of 5 representative queries |

Each skip avoids one full semantic search. The absolute saving depends on
memory size and is **not** measured here — only the gate's own cost is.

---

## 4. A latency regression this work introduced, and fixed

Worth recording because it was caught by Nova's own existing test rather than by
inspection.

`tests/test_it_chat_pipeline.py` asserts turn overhead stays under a 3 s budget.
After the selector was wired in, it failed:

```
FAIL turn overhead 7.26s < 3.0s budget
```

Cause: `ToolEmbeddingCache` called `embedding_available()`, which **loads**
bge-small on first call. Selection sits in front of every turn, so the first
turn after boot paid for the model load.

Fix: `memory/embeddings.py` gained `embedding_loaded()` (checks, never loads)
and `warm_in_background()`. The selector now uses embeddings only if they are
already resident, and otherwise kicks off a background warm and ranks lexically
for that turn.

| | Turn overhead |
|---|---|
| After wiring the selector (broken) | 7.26 s |
| After the fix | **0.46 s** |

This is a restoration to roughly the pre-existing baseline, **not** an
improvement over it.

---

## 5. Spoken-text conversion

| Metric | Value |
|---|---|
| Cost | **18.8 microseconds** per reply (5,000 conversions in 0.094 s) |
| Example | `## Options\n1. **Seagate Exos** — 28 TB, $429. See [specs](https://…)` → `Options. Seagate Exos — 28 terabytes, $429. See specs.` |

Negligible against synthesis time, and it removes markdown, raw URLs and code
fences from what XTTS is asked to pronounce.

---

## Live validation status

| | Status |
|---|---|
| 1. CUDA isolation hypothesis | **DONE — passed.** See §0. 10/10 turns, 28 concurrent CUDA clips, 0 aborts. |
| 2. VRAM headroom | **DONE.** 7.36 GB model + 0.05 GB drift with XTTS resident, on a 17.1 GB card. |
| 3. First audible word | **Partial.** Generation start → first audio measured (≈2 s of it is TTS). Mic + STT still unmeasured. |
| 4. Worker crash under load | **Covered offline** (`tests/test_tts_isolation_jv2.py`, integration stage 13); not yet done against real XTTS mid-reply. |
| 5. End-to-end through `/chat/stream` | **Not done.** |
| 6. Stability beyond ten turns | **Not done.** |

Reproduce §0 with:

```bash
venv/Scripts/python.exe tests/live_voice_validation.py 10
```

Exit code 0 means the run completed with no CUDA abort. Read the per-turn table,
not just the exit code — a run where the model returns empty replies will exit 0
while proving nothing.
