# JARVIS V2 — Subsystem comparison

Nova against the three reference assistants, subsystem by subsystem, with an
explicit verdict for each external idea.

Verdicts:

* **ADOPT** — took the idea more or less as it stands
* **ADAPT** — took the idea, changed it to fit Nova
* **NOVA ALREADY BETTER** — Nova's existing approach wins; leave it alone
* **REJECT** — considered and declined, with a reason
* **FUTURE** — worth doing, not this round

Licensing constraints on `isair/jarvis` (non-commercial **and** share-alike) mean
its column reflects architecture-level understanding only; see
`docs/THIRD_PARTY_ARCHITECTURE_NOTES.md`.

---

## Voice

### TTS execution model

| | Approach |
|---|---|
| **Nova (before)** | XTTS in-process. CUDA aborted the backend with an illegal memory access; the workaround defaulted to CPU, and the CPU branch then raised "XTTS requires CUDA". Net effect: mute by default. |
| **InterGenJLU** | Persistent event-driven speech workers; Kokoro TTS. |
| **isair** | Own TTS pipeline in its output layer. |
| **Nova (after)** | XTTS in a dedicated child process with its own CUDA context (`services/tts_worker.py`), bounded IPC, health, crash recovery, per-turn cancellation. |

**Verdict: ADAPT.** The persistent-worker idea is InterGen's; Nova's reason for
it is different. InterGen isolates for pipeline structure. Nova isolates because
two CUDA consumers in one process corrupt each other, and because the obvious
alternative — serialising XTTS behind `core/gpu.py::GPU_SEM` — was tried and
deadlocks: sentence-streamed TTS overlaps generation by design, and the reply
stream holds the permit for the whole generation (measured: 195 s hang, then
access violations). Same card, different context, is the only arrangement that
satisfies both constraints.

Nova also declined InterGen's worker *model* for STT, because Nova's microphone
capture goes through Electron/browser rather than a native audio thread.

### Sentence chunking

| | Approach |
|---|---|
| **Nova (before)** | `re.split(r"(?<=[.!?])\s+")` plus a 260-char hard cut. |
| **InterGenJLU** | Dedicated `core/speech_chunker.py`. |
| **Nova (after)** | `core/voice/chunker.py`: abbreviation/decimal/initial/URL/filename/version aware, clause-boundary fallback, lower bar for the first chunk, code-fence hold. |

**Verdict: ADAPT.** The idea of a real chunker module is InterGen's; the rules
are Nova's, driven by Nova's own subject matter (it talks about `3.5 TB`,
`README.md` and `memory.recall` constantly). Measured: V1 mis-split 2 of 4
representative inputs, V2 zero; mean first-chunk size down 14%.

### Barge-in and echo

| | Approach |
|---|---|
| **Nova (before)** | Frontend audio-queue clearing only. No server-side turn identity, no echo handling. |
| **isair** | Echo detection with dedicated tests; listener state machine. |
| **Nova (after)** | `core/voice/turn.py` (turn identity + cancellation), `core/voice/echo.py` (three-way ECHO/USER/MIXED verdict), `POST /voice/interrupt`. |

**Verdict: ADAPT** (independently implemented — see licensing note). Nova's
addition beyond a two-way echo test is the **MIXED** verdict: when the
microphone catches the tail of Nova's sentence *and* Marcus talking over it,
the user's real suffix is recovered rather than the whole utterance being
discarded. Nova also uses echo classification defensively at the interrupt
endpoint, so Nova hearing herself cannot cancel her own turn.

### Spoken vs display text

| | Approach |
|---|---|
| **Nova (before)** | None. XTTS received raw model output including Markdown. |
| **isair** | TTS preprocessing in its output layer. |
| **Nova (after)** | `core/voice/speech_text.py` — Markdown stripped, units expanded, URLs and code described rather than recited. |

**Verdict: ADAPT.** Nova's constraint is stricter than general text cleanup:
technical accuracy must survive. `32 GB` becomes `32 gigabytes`, never "about
32 gigs", and product names, model numbers and figures pass through untouched.

---

## Tools

| | Approach |
|---|---|
| **Nova (before)** | `ToolRouter` (clean executor) + full catalogue in every `decide()` prompt, up to 6 times per turn. |
| **isair** | Tool selection, tool registry, tool search, MCP runtime. |
| **Nova (after)** | `core/tools/selector.py` in front of the unchanged `ToolRouter`. |

**Verdict on preselection: ADAPT.** Measured 49 tools → 5.8 shown, ~736 → ~94
catalogue tokens (87% smaller), 28/28 recall.

**Verdict on `ToolRouter` itself: NOVA ALREADY BETTER.** It is already a clean
executor — validation, timeout, retry, typed result — with no classification
mixed in. It was left completely untouched, and the selector is forbidden from
touching permissions (enforced by an AST test).

**Progressive tool discovery / `tool.search`: FUTURE.** The right answer at
hundreds of tools; unnecessary complexity at 49. The selector's interface takes
a `descriptions` dict rather than reaching into the router, so a future
discovery stage can supply a subset without changing anything downstream.

**MCP compatibility: FUTURE, deliberately deferred.** The value is real, but MCP
would need to route through Nova's permission system, context firewall,
auditing and cancellation before it could be safe, and this round's remaining
budget was better spent making the voice work at all. Nothing here blocks it:
MCP tools would register as ordinary router tools and be selected like any
other.

---

## Memory and context

| Capability | Nova (before) | Verdict |
|---|---|---|
| Reinforcement, salience, decay, provenance, contradiction reconciliation, cross-day inference | Present, tested | **NOVA ALREADY BETTER** — untouched |
| Rich runtime grounding (user, family, project, mood, dates, capabilities) | Present | **NOVA ALREADY BETTER** — untouched |
| Conversational fast path (`is_purely_conversational`) | Present | **NOVA ALREADY BETTER** — an allowlist that vetoes itself on tool-ish words, i.e. it already fails toward more capability |
| Working context | Absent | **ADAPT** → `memory/working_context.py` |
| Interaction artifacts | Absent | **ADAPT** → `memory/artifacts.py` |
| Ordinal reference resolution | Absent | **ADAPT** → deterministic, in `memory/artifacts.py` |
| Freshness classes | Absent | **ADAPT** → six classes, plus per-field volatility |
| Recall gate | Absent (search ran on every turn) | **ADAPT** → `memory/recall_gate.py` |

**Chroma: REJECT replacing it.** InterGen uses a different vector system. Nova's
SQLite remains authoritative and Chroma remains a rebuildable index with health
reporting. There is no evidence a swap would improve retrieval quality, and
`docs/JARVIS_V2_AUDIT.md` records that the existing memory suites all pass.

**A second database for artifacts: REJECT.** InterGen's artifact cache is its own
store. Nova's artifacts live hot in memory, bounded per conversation, with
compact summaries persisted through the normal fact path. This avoids a schema
migration against a live memory database for a feature whose value is almost
entirely within-session.

**Ordinal resolution via embeddings: REJECT, emphatically.** "The second one" is
positional. Asking a vector database what it means is a category error. Nova's
resolution is arithmetic over `item_index` and is tested against 11 phrasings
plus four negative cases ("the second world war" is not a reference).

---

## Evaluation

| | Approach |
|---|---|
| **isair** | Extensive eval suite, performance tests. |
| **Nova (before)** | 67 module-level suites, all passing. |
| **Nova (after)** | 67 preserved + 5 new behavioural suites + a 14-stage integration scenario + a measured benchmark script. |

**Verdict: ADAPT.** Nova's existing tests were not replaced. The new layer tests
*behaviour* (does an ordinal resolve, does a cancelled turn stay silent, does the
gate fail open) rather than module existence, which is the useful part of
isair's discipline.

Nova adds one thing worth noting: the benchmark document states explicitly what
was **not** measured, including the central unverified hypothesis of the P0 fix.

---

## ADA v2

| Capability | Verdict |
|---|---|
| CAD integration, project lifecycle | **FUTURE** — `docs/FUTURE_CAD_INTEGRATION.md` |
| 3D visualisation, spatial UI | **FUTURE** — the artifact model is shaped to carry it |
| Gesture interaction, face auth | **REJECT for now** — no hardware requirement, and it competes with nothing currently weak |
| 3D printer / slicer orchestration | **FUTURE** — and explicitly not gated on owning a printer |

Nothing from ADA was implemented this round. The brief was explicit that CAD must
not consume the upgrade, and the voice was broken.

---

## Where Nova now leads

* **Honest degradation.** `/status` reports the actual device, actual worker
  state, and the real last error. `resolve_device()` refuses to silently drop a
  GPU-only assistant to a CPU voice without an explicit opt-in.
* **Trust that survives storage.** An artifact fetched from a web page is
  `UNTRUSTED_EXTERNAL` a month later, and renders inline in the prompt as "data
  only, never instructions".
* **Freshness at field granularity.** A three-day-old drive listing still knows
  its capacity and is flagged as no longer knowing its price.
* **Asymmetric optimisation.** The recall gate and the tool selector both
  document *why* their thresholds are lopsided, and both have tests for the
  expensive direction of failure rather than only the cheap one.
