# Nova — architectural decision record

Durable decisions with the evidence behind them, so a future agent (human or
otherwise) does not undo a good architecture because the reason was not written
down.

**These decisions are now also stored as data.** `memory/decision_seed.py`
mirrors this file into the `decisions` table (P4), so Nova can *retrieve* the
reasoning rather than only display it — "why do unknown MCP capabilities require
confirmation?" is answerable from memory, with the rationale attached.

The two views are kept deliberately, not redundantly: prose here because it is
what a person will actually read and review in a diff, structure there because
retrieval and supersession need fields. Seeding is idempotent and never
overwrites an edited decision. **When you add a decision here, add it to the
seed too** — the test suite checks the acceptance questions, not the file.

Format: what was decided, why, what the alternatives were, what evidence
supports it, and what would justify revisiting it.

---

## D1 — XTTS runs in an isolated child process

**Decided:** 2026-08-12 (JARVIS V2)

**Decision.** XTTS synthesis runs in a dedicated child process with its own CUDA
context, not in the backend process.

**Rationale.** In-process CUDA XTTS beside llama.cpp aborts the backend with
`CUDA error: an illegal memory access was encountered` in
`ggml_backend_cuda_synchronize`. It cannot be serialised behind `GPU_SEM`
either: sentence-streamed TTS overlaps generation by design and the reply stream
holds the permit for the whole generation, so taking it deadlocks (measured:
195 s hang, then access violations on every later turn).

**Alternatives rejected.** CPU-only XTTS (4× slower, and the config claiming it
was unreachable); serialising on the GPU semaphore (deadlocks); a second GPU
(hardware Marcus does not have).

**Evidence.** `tests/live_voice_validation.py` — 10/10 turns, 28 concurrent CUDA
clips, 0 aborts, +0.05 GB VRAM drift, RTF 0.73. `core/gpu.py` records the
original failure.

**Revisit if.** A future llama.cpp/torch combination demonstrably shares a CUDA
context safely, proven by the same ten-turn concurrent test.

---

## D2 — STT stays on CUDA

**Decided:** 2026-08-12 (V3 P1)

**Decision.** faster-whisper keeps `("cuda", "float16")` as its first attempt,
in the backend process, despite CTranslate2 being a second CUDA runtime there.

**Rationale.** The risk is real in shape but was not borne out. Moving STT to
CPU on suspicion would be a silent regression with no measurement behind it.

**Evidence.** `tests/bench_cuda_coexist_v3.py` — seven configurations
(each consumer alone, in pairs, and all three under conversational load): zero
inference errors, zero aborts, zero XTTS restarts, VRAM flat at 9.04 GB.
Contention is real but asymmetric (llama.cpp +6%, whisper +185%, XTTS +209%).

**Revisit if.** A CUDA abort is ever observed with STT on GPU. Re-run the matrix
before changing the device.

---

## D3 — The FAST reasoning contract is a closed-block prefill

**Decided:** 2026-08-13 (V3 P2.5)

**Decision.** Ordinary conversational replies (`thinking=False`) prefill the
assistant turn with an already-closed reasoning block
(`<think>\n\n</think>\n\n`) so the model continues from after it. Decision-making
paths (`decide()`, deep mode) keep native reasoning.

**Rationale.** Model compute TTFT is 35 ms; ~10.3 s per turn was hidden
reasoning that was generated and then discarded. Prompt wording does not control
it — five rewordings were measured and the shipped prompt was the best of them.
The chat template is what opens the block, so the control has to live there.

**Evidence.** `tests/bench_ttft_v3*.py`, `tests/bench_nova_v3.py`:
first-visible-token 8,169 ms → 36 ms simple median, empty replies 5/18 → 0/18;
end-to-end TTFT median 12,127 ms → 130 ms.

### ⚠ CONSTRAINT: this is model- and template-specific

`<think>` / `</think>` is **Qwen3-family syntax**, validated only against Nova's
current `Qwen3.5-9B-Q6_K` and its chat template. It is not a general technique.

Applying it blindly to another model would at best do nothing and at worst
inject literal `<think>` text into the visible answer, or — given what V2 found
about naming the tag in prompts — actively trigger the pathology it exists to
prevent.

**Therefore, before any model swap or productization work:**

* A reasoning contract must be **selected per model / per chat template**, not
  applied globally. The current behaviour is correct *for the current model* and
  must be gated on that, not assumed.
* `NOVA_LLM_FAST_PREFILL=0` already falls back to the older `/no_think` switch,
  so the escape hatch exists — but a fallback is not model selection.
* The right eventual shape is a small per-template registry: template identity →
  how to request "no hidden reasoning" (prefill, a template flag, a sampling
  parameter, or nothing at all), with a documented default of "do nothing" for
  unknown templates.
* Any new model must be re-measured with `tests/bench_ttft_v3c.py` before its
  contract is trusted. Do not port the number 36 ms across models.

**Alternatives rejected.** Prompt rewording (measured, the shipped prompt was
best); a short token budget (79 ms median but **17/18 empty** — fast because it
fails fast); `/no_think` alone (barely helped, raised empties to 6/18).

**Revisit if.** The model or chat template changes — mandatory, not optional.

---

## D4 — MCP is a governed capability source, not a model-side bypass

**Decided:** 2026-08-13 (V3 P3)

**Decision.** MCP servers feed Nova's existing capability registry, ToolSelector,
permission broker, context firewall and artifact system. MCP tools are ordinary
`ToolRouter` tools with namespaced identities, not a parallel execution path.

**Rationale.** The value of MCP is the ecosystem, not its execution model.
Nova's selection, permission and trust machinery is stronger than routing tool
calls straight from the model, and adopting MCP must not cost that.

**Evidence.** See `docs/NOVA_V3_MCP.md`.

**Revisit if.** The MCP specification adds capabilities that genuinely cannot be
expressed as a governed Nova tool.

---

## D5 — Episodic memory lives in Nova's existing SQLite database

**Decided:** 2026-08-13 (V3 P4)

**Decision.** Episodes, artifacts and decisions are tables in the existing
memory database (schema v7). Only heavy evidence goes to a content-addressed
filesystem store under `<memory_dir>/cold/`.

**Rationale.** SQLite is already Nova's authoritative structured store, and the
warm records are small, relational, and want the same transactional guarantees
as facts. A second database would add another thing to keep consistent, back up
and migrate, for no measured benefit. Cold evidence is genuinely different: it is
large, read rarely, never queried by content, and would sit inside every backup
and VACUUM of a database that is otherwise a couple of megabytes.

**Alternatives rejected.** A dedicated vector/document database for episodes
(splits the source of truth, no measured benefit); storing evidence blobs in
SQLite (bloats every backup for data read rarely).

**Evidence.** `tests/bench_episodic_v4.py`: 2,001 episodes = 1.22 MB database; a
greeting adds 0.01 ms and 0 prompt characters. `tests/test_episodic_memory_v4.py`
covers persistence, trust, freshness, provenance, corruption and lifecycle.

**Constraint.** A missing or corrupt cold payload must never break memory. Warm
records are self-sufficient; cold hydration returns None rather than raising, and
a digest mismatch is refused rather than served.

**Revisit if.** Retrieval quality at 50k+ episodes proves lexical ranking
insufficient, or write throughput becomes a bottleneck.

---

## D6 — Episodic search fails CLOSED, unlike fact recall

**Decided:** 2026-08-13 (V3 P4)

**Decision.** `needs_episodic_memory()` requires positive evidence that a turn
references the past. Fact recall (`should_recall`) does the opposite and fails
open.

**Rationale.** The two gates guard different failure modes. Forgetting a fact
Nova knows is the worst thing she can do, so that gate errs toward searching.
Episodic search is a database query for things that *happened*; running it on
every turn costs latency and buys almost nothing, and the cost lands on exactly
the conversational turns P2.5 worked to make fast.

**Evidence.** `tests/bench_episodic_v4.py`: a greeting costs 0.01 ms and adds
zero prompt characters against a 2,001-episode corpus.

**Revisit if.** Users report Nova failing to recall something she demonstrably
stored — check the gate before touching ranking.

---

## D7 — Durable memory is promoted by ONE hook on the hot artifact store

**Decided:** 2026-08-14 (V3 P4.1)

**Decision.** Anything that produces an artifact is considered for durable
memory by virtue of producing one. `ArtifactStore` announces each complete unit
(a standalone artifact, or a result set with its ordered children) to a single
promotion hook; the runtime decides eligibility with `worth_remembering()` and
enqueues. No subsystem writes episodes itself.

**Rationale.** The alternative is each producer persisting its own history, and
the producers do not agree for long. MCP is the proof: `McpManager` already
stored an artifact carrying server, remote tool, arguments, schema hash and the
injection flag, so it became durable the day the hook existed — no MCP-specific
persistence, no second code path to keep in sync, and P3's rule that "MCP uses
Nova's normal machinery" held without anyone maintaining it. A hook on the hot
store is also the only place that sees every producer, including ones not
written yet.

**Alternatives rejected.** Persisting inside the tool loop (misses MCP and
capabilities, which do not go through it); each subsystem writing its own
episodes (two paths disagree within a release, and trust/provenance handling
gets reimplemented per subsystem — the exact way a security invariant erodes).

**Constraint.** The hook is called on the turn path, so it must stay
synchronous, cheap and non-raising: decide and enqueue, never await, never
touch the database. Persistence itself is a background worker
(`EpisodicIngestWorker`) that drains before it stops and drops rather than
blocks when saturated — an episode may be lost to back-pressure, a reply may
not.

**Evidence.** `tests/bench_episodic_v41.py`: enqueue 0.035 ms on the turn versus
41.7 ms to complete the write in the background — three orders of magnitude.
`tests/test_episodic_integration_v41.py` covers MCP promotion, duplicate
delivery, shutdown drain and failure isolation.

**Revisit if.** A producer appears that legitimately cannot express itself as an
artifact. Adding a second persistence path is not the fix; extending the
artifact vocabulary is.

---

## D8 — Explicit past wording outranks the result set on screen

**Decided:** 2026-08-14 (V3 P4.1)

**Decision.** Ordinal references resolve in a fixed order: wording about the
present resolves against the HOT set; wording about the past resolves against
the historical set **even when something is on screen**, and the hot selection
is then dropped; when neither is clearly meant, Nova asks instead of choosing.

**Rationale.** "The second drive we looked at yesterday" matches the set
currently on screen too — positionally, by coincidence — so both layers resolve
it. Presenting both leaves the model a prompt that says "he means the LG
monitor" and "he means the WD Gold", which is worse than either answer alone.
The user's tense is the only evidence available about which set they mean, and
it is unambiguous evidence.

**Alternatives rejected.** Hot always wins (ignores the word "yesterday", which
is the user telling Nova exactly what they mean); most-recent historical set
wins on ties (a guess wearing a confident face); asking an LLM to pick (makes a
deterministic operation probabilistic).

**Constraint.** Ambiguity must survive as ambiguity. Two historical result sets
within 1.25x of each other resolve to a question, and must never be settled by
whatever happens to be on screen.

**Evidence.** `tests/test_episodic_integration_v41.py` —
`test_current_ordinal_precedence`, `test_historical_wording_outranks_hot`,
`test_ambiguity_is_not_resolved_arbitrarily`.

**Revisit if.** Users report Nova answering about the wrong result set. Check
which of the three rules fired before changing ranking.
