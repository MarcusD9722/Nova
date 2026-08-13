# Nova V3 — Baseline

Phase 0 of the V3 program. Established by inspection and by running things, not
by reading the V2 report. Opened 2026-08-12.

---

## 1. Repository state

| | |
|---|---|
| Nova HEAD | `de60c2d11a3e9c49f3012228a68a6be704c5e2b2` |
| Branch | `main` |
| Working tree | clean |
| Last change | PR #22 (JARVIS V2) merged |
| Test suites | **73/73 pass**, zero failures |

No uncommitted user work is at risk.

### Known flake
`test_workers.py` failed once during a suite run under heavy load, then passed
standalone and on re-run. Not root-caused. Treat a single failure there as
suspect before treating it as a regression.

---

## 2. Reference repositories — none have moved

Resolved live at V3 Phase 0:

| Repo | HEAD | Date | Change since V2 audit |
|---|---|---|---|
| `InterGenJLU/jarvis` | `39acdf6346f6c8497c3b368a6fdecef00fd6405b` | 2026-04-02 | **none** |
| `isair/jarvis` | `d22ed8b975792842dc09e49861f31a39cbb302a6` | 2026-08-05 | **none** |
| `nazirlouis/ada_v2` | `d005af742fc5c604074b8b92bd9a223d7fca7447` | 2025-12-23 | **none** |

**Consequence: the V2 competitive analysis is still current.** The V3 competitor
audit is an extension of `docs/JARVIS_V2_COMPARISON.md`, not a redo.

### Licensing — re-verified, unchanged
`isair/jarvis` remains under the custom "Jarvis AI Assistant License":
non-commercial **and** share-alike ("Any derivative works are also licensed under
these same terms"). The V2 decision stands: **no source reuse, architecture-level
inspection only.** See `docs/THIRD_PARTY_ARCHITECTURE_NOTES.md`.

### One new observation about ADA
ADA's CAD agent is **Gemini-powered** — a cloud LLM generating CAD. Nova is
local-first. For V3 P6 this is an architectural fork in the road, not a detail:
matching ADA's CAD quality with a local 9B is a materially harder problem than
calling Gemini, and pretending otherwise would set up a false comparison. Decide
deliberately in P6 whether Nova accepts an optional cloud CAD path (governed by
the existing cloud runtime and permissions) or commits to local-only and accepts
lower geometry complexity.

---

## 3. Hardware and models

| | |
|---|---|
| GPU | NVIDIA GeForce RTX 5080, 17.1 GB |
| VRAM idle | 1.38 GB |
| VRAM with model resident | 8.74 GB (+7.36 for the model) |
| VRAM with model + isolated XTTS | 8.79 GB (+0.05 for XTTS) |
| LLM | `Qwen3.5-9B-Q6_K.gguf` (7.36 GB), 8192 ctx, GPU offload confirmed |
| Vision | `mmproj-Qwen3.5-9B-BF16.gguf` present |
| TTS | XTTS v2, **isolated child process on CUDA**, 24 kHz |
| STT | faster-whisper, `base`, CUDA float16 → CPU int8 fallback |
| Embeddings | bge-small-en-v1.5, CPU |
| Platform | Windows 11, Python venv |

---

## 4. Measured latency at baseline

From `tests/live_voice_validation.py` and `tests/bench_ttft.py` (see
`docs/JARVIS_V2_BENCHMARKS.md` for full tables).

| Metric | Value |
|---|---|
| XTTS worker cold load | 41–71 s |
| **XTTS first-chunk synthesis** | **≈1.97 s** mean, 0.86 s best |
| RTF (turn wall-clock ÷ audio produced) | **0.73** |
| TTFT, first-attempt clean | 3.87 s mean, **0.12 s** best |
| Prompt eval, +7 KB of prefix | +0.214 s |
| Prefix-cache invalidation | +0.002 s |
| Recall gate | 3.1 µs, no model call |
| Tool selection | 12.42 ms/turn warm |
| Tool catalogue | 49 tools → 5.8 shown (~736 → ~94 tokens) |

**Not measured at baseline, and required by V3 P1:** VAD/endpoint latency,
upload/transport, STT queue, Whisper inference, transcript cleanup, frontend
delivery, playback start. The V2 program measured generation-start → first audio
and was explicit that mic + STT were excluded. **That gap is exactly what V3 P1
must close.**

---

## 5. Architecture as it actually stands

### Voice
```
mic (browser) → recorder.ts VAD → /stt (faster-whisper)
  → /chat/stream → recall gate → tool selector → agent loop
  → llama.cpp stream → SpeechChunker → to_spoken()
  → IsolatedTtsEngine (child process, own CUDA context)
  → SSE tts events → frontend sequential playback
```

* `services/tts_worker.py` — XTTS in its own process. Verified on hardware:
  10/10 turns, 28 concurrent CUDA clips, 0 aborts, +0.05 GB drift.
* `core/voice/turn.py` — turn identity; cancelled turns cannot speak.
* `core/voice/echo.py` — ECHO / USER / MIXED with suffix salvage.
* `core/voice/chunker.py` — abbreviation/decimal/URL aware; 0/4 mis-splits.
* `POST /voice/interrupt` — runs echo suppression before cancelling.

### Memory
SQLite authoritative; Chroma rebuildable index; graph, provenance, decay,
reinforcement, salience, contradiction/supersession, cross-day inference — all
present and untouched by V2. Added in V2 and now live in the turn path:
`memory/working_context.py`, `memory/artifacts.py`, `memory/recall_gate.py`.

### Tools
`ToolRouter` unchanged (execution only). `core/tools/selector.py` preselects
3–8 candidates. Permissions untouched; an AST test forbids the selector from
referencing them.

---

## 6. Baseline limitations — the honest list

Ordered by how much they matter for V3.

1. **Barge-in is not live.** `watchForBargeIn()` exists in
   `frontend/src/App.jsx` but is **OFF by default** and has never run against a
   real microphone. This is V3 P0.
2. **The VAD ignores the TTS reference signal.** `recorder.ts` exports
   `getTtsOutputLevel()` (line 79) and `isTtsPlaying()` (line 96), but the VAD
   loop (line ~438) compares only `level >= speechThreshold` and never consults
   either. **The primitive for acoustic echo rejection exists and is unused** —
   this is the single most useful thing found in Phase 0, and it is the
   foundation P0 should build on.
3. **Endpointing is untuned.** `trailingSilenceMs = 700`, `minSpeechMs = 250`,
   `speechThreshold = 0.025`, `startTimeoutMs = 3500`. These are plausible
   defaults that have never been benchmarked. The brief is right that waiting
   too long to decide the user stopped is a common cause of "feels slow".
4. **STT may put a third CUDA context in the backend process.** faster-whisper
   attempts `("cuda", "float16")` first. That is a ctranslate2 context, not
   torch, so it is not obviously the same failure mode that forced XTTS out —
   but it is the same *shape* of risk, and it has never been tested under
   concurrent generation. Worth measuring in P1 before it becomes a mystery
   crash.
5. **Empty-generation rate is 0/30 but not zero.** The retry loop remains as
   insurance; `empty_retries` / `empty_exhausted` are in `/status`.
6. **No MCP** (V3 P3). **No speaker ID** (P5). **No CAD** (P6). **No gestures**
   (P7). **No device registry** (P8). **No memory viewer / installer** (P9).
7. **Artifacts persist as compact summaries only** — no full warm tier (P4).
8. **No soak beyond 20 turns.**

---

## 7. V3 readiness carried forward

`docs/JARVIS_V2_FINAL_REPORT.md` closed at **READY WITH LIMITATIONS**, hinging
on exactly one thing: barge-in written but untested. That is unchanged at V3
Phase 0, and it is why P0 is P0.

---

## 8. A constraint on this program that must be stated up front

**Several V3 priorities have acceptance criteria that cannot be met without a
human at the hardware.** The brief is explicit that mock passes do not count, so
these are listed as blocked rather than quietly approximated:

| Priority | What needs a human |
|---|---|
| **P0 barge-in** | 20 live interruption attempts with real mic + speakers, at several volumes and mic distances. Median/P90 stop latency, false-self-interruption rate. **Cannot be simulated.** |
| P1 STT | Partially measurable: probe audio can be *synthesised with XTTS* and fed through the real STT path to measure inference, transport and endpointing. Real-room acoustics and true VAD onset still need a mic. |
| P5 speaker ID | Enrollment and guest-rejection accuracy need at least two real voices. |
| P7 gestures | Needs a camera and hands. |
| P8 devices | Needs the actual devices. |

Everything else — P2 benchmarks, P3 MCP, P4 artifact persistence, P6 CAD, P9
memory viewer — is fully buildable and testable without a human present.

---

## 9. Change log

* 2026-08-12 — Phase 0 opened. HEAD `de60c2d`, 73/73. All three reference repos
  unchanged since the V2 audit; isair licence re-verified and still excluded.
  Found the unused `getTtsOutputLevel()` primitive that P0 should build on.
