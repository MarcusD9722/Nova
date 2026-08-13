# JARVIS V2 — Repository and Runtime Audit

Living document. Started 2026-08-11. Every claim here was verified against the
actual source or an actual run; nothing is carried over from the engineering
brief on trust.

---

## 1. Baseline

| Item | Value |
|---|---|
| Nova HEAD at audit start | `1e4359991a76545b0c146be26ce48a27276c73d6` |
| Branch | `main` |
| Working tree | clean (`git status --porcelain` empty) |
| Remote | `https://github.com/MarcusD9722/Nova.git` |
| Test suites | 67 files in `tests/` |
| Baseline result | **PASSED: 67/67**, zero failures |
| Runner | `run_tests.ps1` (each suite is a standalone script, not pytest) |

The brief stated the inspected head was `1e43599…`; that is still HEAD, and the
tree is clean, so there is no uncommitted user work at risk.

The previous session's claim of "67/67 suites" was re-verified by running the
suite, not taken from the commit message. It holds.

### Baseline caveats
* The runner reports only the last 3 lines per suite, so the pass/fail signal is
  the per-suite exit code, not an assertion count. There is no aggregate
  test-case count available without changing the runner.
* Total wall-clock duration was not separately instrumented on this run.

---

## 2. External repositories — resolved HEADs and licenses

Resolved live via the GitHub API at audit time, not assumed from the brief.

| Repo | HEAD (main) | Date | License | Verdict |
|---|---|---|---|---|
| `InterGenJLU/jarvis` | `39acdf6346f6c8497c3b368a6fdecef00fd6405b` | 2026-04-02 | **MIT** | Source reuse permitted with notice |
| `isair/jarvis` | `d22ed8b975792842dc09e49861f31a39cbb302a6` | 2026-08-05 | **Custom "Jarvis AI Assistant License"** | **No source reuse** — see below |
| `nazirlouis/ada_v2` | `d005af742fc5c604074b8b92bd9a223d7fca7447` | 2025-12-23 | **MIT** | Source reuse permitted with notice |

### The isair licensing decision (important)

`isair/jarvis` is not merely non-commercial. Its LICENSE is a custom
"Jarvis AI Assistant License" with **two** disqualifying terms:

1. Commercial use requires a separate license from the copyright holder.
2. **Share-alike**: "Any derivative works are also licensed under these same
   terms."

Term 2 is the harder problem. A non-commercial licence that merely restricted
use could be sandboxed; a share-alike term means any Nova module that qualified
as a derivative work would drag Nova's own licensing toward those terms. Nova's
stated constraint is that it must remain structurally capable of future
commercial use.

**Decision: no source from `isair/jarvis` enters Nova, and inspection depth was
deliberately limited to public description and architecture-level concepts.**
Recall gating, echo detection, tool preselection and hot-window context are
general engineering ideas, not protectable expression; they are implemented here
from first principles against Nova's own architecture. This is recorded in
`docs/THIRD_PARTY_ARCHITECTURE_NOTES.md`.

---

## 3. P0 — XTTS is broken in the default configuration

**Status: CONFIRMED, and worse than the brief described.**

The brief described a contradiction between the documented default and the code.
The actual runtime consequence is that **Nova cannot speak at all with default
settings**.

`core/settings.py:92` declares the default:

```python
_s("NOVA_TTS_DEVICE", "str", "cpu", "XTTS device (cpu|cuda|auto). Defaults to CPU: ...")
```

`backend/app.py:713` reads that default, then `backend/app.py:722-730`:

```python
if target_device != "cpu":
    try:
        tts = tts.to(target_device)
    except Exception as e:
        raise RuntimeError(...)
else:
    raise RuntimeError(_xtts_gpu_required_message())   # <-- the default branch
```

With `NOVA_TTS_DEVICE` unset, `target_device == "cpu"`, control reaches the
`else`, and the loader raises
*"XTTS requires CUDA GPU execution and refuses CPU fallback."*

This is not caught anywhere useful. The enclosing `for _ in range(32)` retry loop
only swallows exceptions whose message matches `Unsupported global: GLOBAL …`
(the torch-2.6 weights-only allowlist dance); anything else is re-raised at
`backend/app.py:737`. So `_load_tts_model` raises, `_ensure_tts_loaded` fails,
and every TTS request errors out.

The two halves of the file disagree with each other about the intended policy,
and the half that documents CPU-by-default is the half that is unreachable.

### Root cause, not just symptom
The comment block at `backend/app.py:694-712` and `core/gpu.py` document the real
constraint honestly and at length:

* XTTS on CUDA **in the same process as llama.cpp** aborts the backend with
  `CUDA error: an illegal memory access was encountered` in
  `ggml_backend_cuda_synchronize`. Reproduced twice in ten minutes of ordinary
  speaking turns.
* It cannot be serialised behind `core/gpu.py::GPU_SEM`, because sentence-
  streamed TTS overlaps synthesis with generation *by design*, and the reply
  stream holds that permit for the whole generation. Taking the permit inside
  `_tts_bytes` deadlocks — measured at a 195 s hang followed by access
  violations on every later turn.
* Both comments already name the correct fix: **run XTTS in its own process**.

So the fix is not to flip the conditional. Flipping it restores slow in-process
CPU synthesis, which is the thing the configuration claims to want and which
still leaves the GPU path unavailable. The fix is process isolation, which makes
GPU XTTS *safe* rather than merely *documented as dangerous*.

---

## 4. Voice pipeline — what already works and must be preserved

Verified in `backend/app.py::chat_stream`.

Real sentence-streamed TTS exists and is genuinely concurrent:

```
tokens → sent_buffer → _split_sentences → sentence_q → tts_worker task
       → _tts_bytes → STATE.tts_cache → audio_q → SSE "tts" event
```

`app.py:1541-1548` drains `audio_q` non-blockingly *inside* the token loop, so
audio events are emitted while generation continues. This is the correct
architecture and must not be regressed into generate-then-synthesise.

Other preserved good parts:
* `_TTS_CACHE_MAX` eviction on `STATE.tts_cache` (app.py:1496) — the unbounded
  WAV growth was already fixed.
* `tts_phrase_cache` for short repeated phrases (`_should_cache_tts_phrase`).
* Mood-aware speaking rate (`_MOOD_TTS_SPEED`), which is honest about XTTS
  0.22.0 having no working emotion parameter and only using real `speed`.

### Weaknesses found
| Area | Finding | Location |
|---|---|---|
| Sentence splitting | `re.split(r"(?<=[.!?])\s+")` splits on any period+space. Breaks on `Dr. Smith`, `3.5 TB`, `U.S.`, `e.g.`, initials, and URLs. | `app.py:1447-1460` |
| Long run-ons | Only cut at >260 chars, and only at a space — no clause-boundary awareness. | `app.py:1454` |
| Turn identity | There is none. `sentence_q`/`audio_q`/`worker` are closures per request; nothing carries a turn id. | `app.py:1469-1504` |
| Cancellation | None. A cancelled turn's TTS work continues to completion and lands in `STATE.tts_cache`. | `app.py:1472-1502` |
| Spoken vs display text | No distinction. XTTS is handed raw model output including Markdown. | `_tts_bytes` |
| Barge-in | No server-side concept. | — |
| Echo suppression | Not present. | — |

---

## 5. Tool system

`core/tool_router.py` is clean and correctly scoped: validation, timeout, retry,
`ToolResult`. It is an executor, not a classifier. **Preserve as-is.**

The gap is upstream, in `core/orchestrator/agent.py:54-62`:

```python
def _tool_catalog(self, agent: Agent) -> str:
    for name, desc in self._router.describe_tools().items():
        ...
        lines.append(f"- {name}: {desc}")
```

Every registered tool's name and description is embedded in **every** `decide()`
prompt, and `decide()` runs up to `agent.step_budget` (default 6) times per turn.
Cost is `O(all_tools × steps)` prompt tokens per turn.

Verified scale: **49 unique tool names** are defined in `core/tooling.py` alone,
before plugin tools. This is the "works at small scale, does not scale" condition
the brief predicted, and it is still true.

---

## 6. Memory and context

`memory/unifier.py` is 137 KB and is the authoritative retrieval surface.
`memory/backends/sqlite_backend.py` (72 KB) is the authoritative store; Chroma is
the rebuildable semantic index.

Verified in `core/runtime.py::_chat_turn_stream`:

* `runtime.py:1158` — `await self._memory.search(...)` runs **unconditionally on
  every turn**, before any check of whether recent context already answers.
  This is the recall-gate opportunity.
* `runtime.py:1163` — `_build_grounding_context` assembles the rich profile
  block (user, family, project, patterns, mood, dates, capabilities). Confirmed
  present and valuable; keep.
* `runtime.py:1210` — `is_purely_conversational` already short-circuits the
  agent loop for social turns. This is the existing conversational fast path the
  brief asked to preserve. It is an allowlist that vetoes itself on tool-ish
  words, i.e. it fails toward *more* capability, which is the correct asymmetry.

### Confirmed present (do not rebuild)
Spot-checked in source and in the passing suites named after them: memory
reinforcement, emotional salience, decay, preference/profile memory, strict
person extraction, cross-day consolidation (`test_memory_consolidation.py`),
contradiction reconciliation (`test_memory_reconciliation.py`), provenance
(`test_provenance_p35.py`), context firewall (`test_context_firewall_u2.py`).

### Gaps
| Need | Present? |
|---|---|
| Working/active-context layer | **No** |
| Interaction artifacts (result sets with addressable items) | **No** |
| Ordinal reference resolution ("the second one") | **No** |
| Tool-result carryover across turns | **No** |
| Freshness classes on cached tool results | **No** |
| Recall gate | **No** |

---

## 7. GPU coordination

`core/gpu.py` defines a single process-wide 1-permit `GPU_SEM`, with an unusually
good comment recording how the illegal-memory-access bug was proven by
elimination. The semaphore is the right answer for consumers that can serialise.

XTTS is the one consumer that *cannot* serialise against generation without
deadlocking, which is precisely why it needs to leave the process rather than
take the permit. `tools/imagegen/service.py` already establishes the
process-isolation precedent in this codebase (separate process, `CUDA_VISIBLE_DEVICES`
pinned before `import torch`, honest `/health` that never claims a model is
loaded when it is not).

Note the difference in shape: imagegen isolates onto a *second physical GPU* in a
*separate venv*. XTTS cannot do that — Marcus has one RTX 5080 and XTTS is
already a dependency of the main venv. XTTS needs process isolation on the *same*
device: separate CUDA context, driver-level time slicing, shared VRAM.

---

## 8. Configuration and secrets

`core/settings.py` is the single declared registry (`_s(name, type, default,
doc)`) and `tools/gen_env_example.py` generates `.env.example` from it. `.env`
exists and is gitignored; no secret values were read or printed during this
audit.

---

## 9. Audit conclusions — ordered work list

1. **P0** XTTS process isolation + a coherent device contract. Nova is mute by
   default today; this is both the biggest correctness bug and the biggest
   latency win available.
2. Speech chunker V2 and a spoken-vs-display text split.
3. Turn identity and cancellation, then barge-in and echo suppression on top.
4. Working context + artifacts + deterministic ordinal resolution.
5. Recall gate (fail-open) in front of the unconditional `memory.search`.
6. Tool selector in front of `ToolLoopExecutor`, with cached tool embeddings.
7. Evals, benchmarks, integration scenario, docs.

---

## 10. Post-implementation sweep

Re-read of the changed surface after the work landed, looking specifically for
the failure mode this audit opened on: **comments that claim one behaviour while
the code does another.**

Found and fixed:

| Location | Contradiction | Resolution |
|---|---|---|
| `core/gpu.py` docstring | Listed XTTS as one of three CUDA consumers "inside a single process". No longer true. | Corrected, with the original crash evidence kept verbatim — it is why the isolation exists. |
| `core/settings.py` `NOVA_EMBED_DEVICE` | Justified the CPU default partly by contention "alongside llama.cpp and XTTS". XTTS has left the process. | Reworded; the bge-small reasoning still stands on its own. |
| `backend/app.py` `_silent_call` | Dead after the loader moved to `services/xtts_engine.py`. | Removed. |
| `backend/app.py` `_tts_bytes` comment | Explained why CPU was the default "until then". | Rewritten to explain why the permit is still not taken and what replaced it. |
| `memory/artifacts.py` module docstring | Referenced a `persist_summary()` that was never written. | Corrected to `Artifact.to_summary_fact()`. |

Also checked and clean:

* Unused-import scan across all 14 added/changed modules — nothing beyond
  pre-existing items in `backend/app.py` and `core/runtime.py` that this round
  did not touch.
* No new `except Exception: pass`. Every swallow either logs or converts to a
  reported state (`TtsUnavailable`, `GateDecision`, `Selection(stage="all")`).
* Bounded: TTS backlog, cancelled-turn memory, turn history, artifacts per
  conversation, working contexts, and the tool vector cache all have caps.
* No second permission system: an AST test asserts the selector never references
  permission or execution symbols.

One naming collision worth recording: the trust label from the brief,
`NOVA_INFERENCE`, tripped `test_settings_p04.py`, which scans source for `NOVA_*`
tokens to find undocumented environment variables. The label was renamed to
`ASSISTANT_INFERENCE` rather than polluting the settings catalogue with a
non-setting.

---

## 11. Change log

* 2026-08-11 — Audit opened. Baseline **67/67**. P0 confirmed (Nova mute by
  default). External HEADs and licences resolved; isair source reuse ruled out
  on share-alike grounds.
* 2026-08-12 — Implementation complete. Final suite **73/73** (67 pre-existing
  preserved + 6 new). Post-implementation sweep above.
* 2026-08-12 — **P0 hypothesis verified on hardware.** `tests/live_voice_validation.py`
  ran ten speaking turns on the RTX 5080 with real llama.cpp generation and real
  XTTS CUDA synthesis overlapping it: 10/10 turns, 28 clips, **zero CUDA aborts**,
  zero restarts, +0.05 GB VRAM drift, RTF 0.73. The crash that reproduced twice
  in ten minutes in-process did not occur once out-of-process.
* 2026-08-12 — Recall gate and artifact capture wired into `core/runtime.py`;
  frontend now cancels turns server-side on an explicit interrupt phrase. Suite
  re-run: **73/73**.
* 2026-08-12 — **TTFT investigated on the live runtime (§41).** Prompt evaluation
  costs +0.214 s for 7 KB; invalidating the prefix cache costs +0.002 s — so
  reordering prompts for KV reuse would buy nothing, and that work is explicitly
  *not* recommended. The real cost is wasted generations: `chat_stream` retried
  identically up to three times when the model produced no visible output,
  burning full generations invisibly (32 per benchmark run, 9 turns returning
  nothing). Retries now escalate; turns producing nothing 9 → 4, worst case
  38.9 s → 10.3 s. `empty_retries`/`empty_exhausted` added to `usage_stats` and
  `/status` so it stops being invisible.
* 2026-08-12 — **Empty-generation root cause found and fixed.** Raw-output
  forensics (`tests/bench_empty_generations.py`) showed 12/12 failures dying
  inside an unclosed `<think>`, none empty at the model, `finish_reason=stop` —
  never a budget problem. The model was quoting Nova's own system prompt back to
  itself and dying exactly where the next token is `<think>`, the literal tag
  that instruction contained. Removing the tag while keeping the prohibition
  took the empty rate from **30% to 0 of 30 samples**, with faster and longer
  replies than either alternative. One line of prose; the largest single latency
  and quality win in the program.
* 2026-08-12 — **Live stress and fault injection passed (§58, §59, §60).**
  20 rapid turns clean; three concurrent CUDA consumers across two processes with
  no crash; SIGTERM'd XTTS worker detected, reported, and recovered. Found and
  fixed a real bug: transparent recovery in `ensure_started()` bypassed the
  restart cap, so a crash-looping worker could respawn forever. Suite: **73/73**.
