# Nova Upgrade Audit — memory/, core/, backend/, plugins/

**Status:** Proposal, awaiting approval · **Date:** 2026-07-20 · ~20k lines scanned
(memory 5,067 · core 11,889 · backend 2,496 · plugins 881)

Four questions asked: what can do *more*, what can run *concurrently*, what can be
*LLM-driven instead of hardcoded*, and can we add *cloud LLM for coding* while keeping
everything else local. Findings below are grounded in actual greps/reads, not memory.

---

## The headline numbers

| Finding | Evidence |
|---|---|
| **4 `asyncio.gather` calls in ~20,000 lines** | Only in `unifier` write paths. Every read path is sequential. |
| **`_build_grounding_context` = 18 sequential awaits, every single turn** | 8 are *independent* family-fact fetches that could be one round-trip |
| **`unifier.search()` = up to 32 sequential SQLite queries per search** | `for t in terms[:8]` × 4 table queries, all awaited one at a time |
| **46 regexes in `core/runtime.py` alone** | Intent/slot extraction done by pattern matching |
| **~15 hardcoded pattern constants for understanding** | place lookup, directions, weather city, name statements, location intent |

---

## A. Hardcoded → LLM-driven (understanding & phrasing)

These are places where a **regex decides what Marcus meant**. They work on the phrasings
someone thought of and fail silently on everything else.

### A1. Intent & slot extraction — `core/runtime.py` (highest impact)
`_PLACE_LOOKUP_PATTERNS`, `_TO_DESTINATION_PATTERNS`, `_NEAREST_QUERY_RE`,
`_WEATHER_CITY_RE`, `_NAME_STATEMENT_PATTERNS`, `_USE_DEVICE_LOCATION_RE`,
`_LOCATION_ANSWER_ABORT`.

*"how do I get to Chipotle"* matches. *"what's the best way over to Chipotle"* doesn't.
*"my name is Marcus"* matches; *"everyone calls me Marcus"* doesn't (and the capital-letter
requirement means *"my name is marcus"* silently fails).

→ **Replace with a single structured extraction call** (one small utility-role LLM call
returning `{intent, slots}`), keeping the regexes as a **fast path**: if a regex hits
confidently, skip the model; otherwise ask. Best of both — no latency regression on the
common phrasing, no silent failure on the uncommon one.

### A2. Task/smalltalk routing — `core/policy/chat_decider.py`
`_looks_like_task_request`, `_needs_task_clarification`, `_task_title`,
`_smalltalk_token_budget` are all regex/heuristic. This decides whether Nova *builds
something* vs. *chats* — a high-consequence branch driven by pattern matching.
→ LLM classification with a confidence score; fall back to the heuristic when the model
is unavailable.

### A3. Specialist routing — `core/orchestrator/society.py`
`select_specialists` scores by keyword-set overlap. *"I keep putting off my training"*
routes to Fitness Coach on the word "training" but misses the Psychologist (procrastination)
entirely — no keyword overlap.
→ Semantic routing (embedding similarity against specialist descriptions, or a small LLM
routing call), with keyword scoring as the deterministic fallback.

### A4. Mood/stress reading — `core/digital_twin.py` + `core/mood.py`
`_STRESS_WORDS` substring matching and a fixed `MOOD_LABELS` set. *"I'm fine, just been a
lot lately"* contains none of the stress words.
→ LLM reads the mood/wellbeing trend text; the *storage* stays a coarse labeled fact
(honest, unchanged).

### A5. Executive phrasing — `core/executive.py`
Recommendation text is hardcoded f-strings: `f"'{label}' is overdue — want to handle it
now or reschedule it?"`. Every nudge sounds identical forever.
→ Keep the **rule-based detection + confidence gate exactly as-is** (that's what stops it
being annoying), but let the LLM *phrase* the surfaced items naturally in context.
Separation of concerns: deterministic *what*, fluid *how*.

### A6. Memory query expansion — `memory/unifier.py`
A hardcoded `synonyms = {"mom": ["mother"], "dad": ["father"], ...}` dict — four entries.
→ Embedding-based expansion (the embedding model is already loaded) or an LLM rewrite of
the query into search terms.

### A7. Graph relation extraction — `memory/graph.py`
`FAMILY_ATTR_PREDICATES` + co-mention heuristics only catch relationships that fit a fixed
shape. *"Leslie's brother Dave fixed my car"* yields no `Dave —sibling_of→ Leslie` edge.
→ LLM relation extraction on ingest (paced, off the hot path), writing edges with
**`inferred` provenance** so KG 2.0 and the hedging logic already handle them correctly.

### A8. Skill naming & parameterization — `core/skills.py`
Detection is deterministic (correct), but the learned skill's *name* and *which steps are
parameters* are mechanical.
→ LLM proposes a human name and identifies the variable slots when offering to learn.

---

## ⚠️ What must STAY hardcoded (a real architectural line)

"Everything LLM-driven, no hardcode" is right for *understanding and phrasing* — and
**wrong** for these. Making them model-driven would make Nova less trustworthy, not more:

| Module | Why it must stay deterministic |
|---|---|
| `core/permissions.py` | Security gate. An LLM that can be talked into `allow` is not a gate. |
| `core/dev_mode.py` deny-lists | Protects `.env`, `credentials/`, `.git`. Must be absolute. |
| `core/experiments.py` | Comparison math — a model that "feels" a winner defeats the purpose. |
| `core/code_intel.py` | `ast` parsing is exact and instant; an LLM would be slower and wrong sometimes. |
| `sqlite_backend` migrations | Data integrity. Never probabilistic. |
| `core/llm_runtime.py` GPU enforcement | Boot-time safety invariant. |
| `memory/provenance.py` vocabulary | The definitions that make honesty checkable. |
| `core/dates.py` **arithmetic** | Date *math* stays exact; only unusual *phrasing* gets an LLM fallback. |

---

## B. Async / concurrency

Nova is almost entirely sequential outside the four write-gathers. Three concrete wins:

**B1. Grounding context (every turn).** 18 sequential awaits; the 8 family-fact lookups
(`mother`, `father`, `spouse`, `child`, `sibling`, `cousin`, `friend`, `pet`) are fully
independent, as are the mood / birthdays / drift / wellbeing / catch-up / executive blocks.
→ `asyncio.gather` the independent groups. This is the single highest-value latency fix
because it's on *every* turn.

**B2. Memory search (every recall).** Up to 8 terms × 4 tables = **32 sequential queries**.
→ Gather per-term across tables. (SQLite is single-writer but reads parallelize fine.)

**B3. Independent profile gathers.** `_gather_executive` and `digital_twin_profile` each
run 5–6 independent fetches sequentially. → gather.

**B4. Agent society** is sequential *by necessity* today (one GPU, one semaphore) — this
is the honest constraint. **But** it becomes genuinely parallel the moment a second handle
exists (see C), because each handle carries its own semaphore.

---

## C. Cloud LLM for coding — the architecture already has the seam

`ModelRouter` (built in Phase 2.4) defines `ROLES = (chat, decider, planner, critic,
coder, utility)` and maps each to a `ModelHandle(name, runtime, semaphore)`. A cloud
provider is **a second handle**, not a rewrite.

**What's needed:**

1. **`core/cloud_runtime.py`** — a `CloudRuntime` implementing the exact `LLMRuntime`
   surface already used: `chat(messages, max_tokens, temperature, stop, thinking) -> str`,
   `chat_stream(...)` async generator, `generate(...)`. Drop-in by construction.
2. **Config** (`.env`): `NOVA_CLOUD_ENABLED` (default **0**), `NOVA_CLOUD_API_KEY`
   (secret), `NOVA_CLOUD_MODEL`, `NOVA_CLOUD_BASE_URL`. Then routing is the *existing*
   knob: `NOVA_MODEL_ROLES=coder=cloud`.
3. **🔒 A context firewall — the critical piece.** Nova's whole premise is local privacy.
   The cloud handle must **never** receive `_build_grounding_context` output (family names,
   location, mood, health signals). Cloud roles get a **task-scoped context only**: the
   code, the file, the spec. Enforced in code + tested, not left to convention.
4. **Honest transparency.** Every cloud call logs *what role, how many tokens, which
   model* to the existing audit trail; `/status` shows plainly when a role is remote.
   A `cloud.*` bus event so the UI can show "this reply used the cloud."
5. **Graceful local fallback.** No key / rate-limited / offline → fall back to the local
   model with an honest note, never a hard failure.
6. **Concurrency unlock.** The cloud handle gets its **own semaphore with real
   concurrency** (no GPU serialization) — so a cloud `coder` runs *while* the local model
   handles chat, and the agent society can finally deliberate in parallel.

**Net effect:** better code + true parallelism, with personal memory still never leaving
the machine. Everything except the explicitly-routed roles stays 100% local.

---

## D. New capability ideas (beyond the four asks)

- **Semantic response cache** — embed the query, reuse a recent answer when cosine
  similarity is very high. Big latency win on repeated questions.
- **Speculative tool execution** — when the decider is confident about a read-only tool
  (weather/search), fire it *while* the reply is still being composed.
- **Vision-driven coding** — with a VL model: screenshot → working UI code. Directly
  pairs with the model question you raised.
- **Memory "dreaming"** — an idle-time LLM pass that consolidates the day's turns into
  durable facts and graph edges (extends the existing reflection worker).
- **Conversational memory editing** — "no, Leslie's birthday is the 14th" → Nova finds the
  fact, shows it, corrects it with provenance, instead of writing a contradictory second fact.
- **Streaming tool progress** — surface `tool.started` → partial results in the UI mid-turn.

---

## Proposed phasing

| Phase | Contents | Risk | Why this order |
|---|---|---|---|
| **U1 — Async parallelization** ✅ **SHIPPED** | B1, B2, B3 | Low | Pure speedup, no behavior change, immediately felt every turn. Do first. |
| **U2 — Cloud LLM handle** ✅ **SHIPPED** | C1–C6 | Medium | Unlocks better coding *and* real concurrency. Firewall + fallback are the work. |
| **U3 — LLM-driven understanding** | A1, A2, A3, A6 | Medium | The fluency win. Fast-path/fallback keeps it safe. |
| **U4 — LLM-driven expression & extraction** | A4, A5, A7, A8 | Low-Med | Natural phrasing + richer graph. |
| **U5 — New capabilities** | D (pick from menu) | Varies | After the foundation is faster and smarter. |

Each phase keeps the existing ritual: feature-flagged, tests green, PR, pause for review.

### U1 results (shipped 2026-07-20)

Parallelized: grounding context (12 sequential awaits → 1 round), `unifier.search()`
(up to 33 → 1), `digital_twin_profile()` (6 → 1), `_gather_executive()` (4 → 1).
Write-involving/order-dependent blocks (wellbeing nudge, session-gap marking, executive
throttle) were deliberately left sequential.

Measured with injected 50ms-per-query latency (`tests/test_async_u1.py`), which proves the
fan-out genuinely overlaps rather than just not-crashing:

| Path | Concurrent | Sequential floor |
|---|---|---|
| `search()` | **0.097s** | ≥0.50s |
| `digital_twin_profile()` | **0.062s** | ~0.30s |
| `executive_recommendations()` | **0.053s** | ~0.20s |

Honest caveat: on the *current* real database (small, local SSD, Chroma off) absolute
timings are already low — search 42ms, twin 7ms, executive 16ms — so the wall-clock win
today is modest. The structural win scales with (a) memory growth, (b) Chroma enabled
(embedding queries are the slow part and now overlap the SQLite fan-out), and (c) the
cloud handle in U2, where a remote call's latency overlaps local work instead of blocking.

### U2 results (shipped 2026-07-20)

**`core/context_firewall.py` — built and tested FIRST, before any cloud code existed.**
Drops grounding blocks (JSON keys *and* their rendered natural-language form) and raw
memory records; redacts identities inside otherwise task-shaped messages; then re-verifies
and **fails closed** — surviving personal markers mean the remote call is refused.

**`core/cloud_runtime.py` — provider-agnostic.** Implements the exact `LLMRuntime` surface
(`chat`/`chat_stream`/`generate`), so `ModelRouter` hands it to a role with zero caller
changes. Two adapters: `openai` (OpenAI, OpenRouter, Together, Groq, vLLM, LM Studio, any
compatible gateway via `NOVA_CLOUD_BASE_URL`) and `anthropic` (Messages API, system prompt
hoisted). Adding a provider = one small class, never a caller change.

**Routing** (verified): cloud off → all six roles local. Cloud on → `coder`+`planner`
remote, `chat`/`decider`/`critic`/`utility` local. Explicit `NOVA_MODEL_ROLES` always wins.
The cloud handle carries its **own semaphore** (`NOVA_CLOUD_CONCURRENCY`, default 4) — remote
calls don't contend for the GPU, which is what unlocks true parallelism.

**Every failure falls back to local, never breaks a turn:** disabled, missing key, missing
model, HTTP 4xx/5xx, network error, firewall refusal, nothing-left-after-scrub. Each emits
an honest `cloud.fallback` bus event + log. `/status` exposes `cloud` state and which roles
are remote; `status()` never includes the key (asserted in tests).

Tests: `test_context_firewall_u2.py` (24 checks) + `test_cloud_runtime_u2.py` (28 checks,
fully offline via monkeypatched httpx — no key, no network). Suite **39/39**.

---

## Decisions (Marcus, 2026-07-20)

1. **Start with U1** (async parallelization) — fast, safe, felt every turn.
2. **Cloud roles: `coder` + `planner`.** `chat`, `decider`, `critic`, `utility` stay local.
   `chat` stays local permanently — it's the personal one.
3. **Provider-agnostic by design.** Marcus wants to "plug in any cloud API into the .env
   and choose whichever cloud model I want, whenever I want." So U2 builds a pluggable
   adapter: an OpenAI-compatible default (covers OpenAI, OpenRouter, Together, Groq,
   vLLM, LM Studio, …) plus an Anthropic adapter, selected by `NOVA_CLOUD_PROVIDER` with
   a configurable `NOVA_CLOUD_BASE_URL`/`NOVA_CLOUD_MODEL` — swapping models/providers is
   an `.env` edit and a restart, never a code change.
