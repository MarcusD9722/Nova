# Nova V3 P4 / P4.1 / P4.2 — Persistent episodic memory

Nova can now remember **what happened**, durably, without flooding the prompt
with history.

This document covers three phases, and the distinctions matter:

| | |
|---|---|
| **P4** | The substrate: schema, stores, retrieval, consolidation rules. Complete, tested — and **not connected to a real conversation.** |
| **P4.1** | Artifact-backed history went live. A tool result created durable memory; a later turn retrieved it. |
| **P4.2** | The remaining promotion semantics. Selections, corrections, failures and project milestones were **defined in P4 and unreachable in production** until this phase. |

P4 shipped with its gap stated openly as its first known limitation.
**P4.1 did not** — and that is the more useful thing to record, because it is the
kind of gap that looks like completion:

`worth_remembering()` has taken five signals since P4 — `is_correction`,
`is_decision`, `is_failure`, `user_selected`, plus the tool/result-set case. The
live hook P4.1 wired called it with three arguments (`user_text`, `tool`,
`result_items`) and let the other four default to `False`. Every one of them was
reachable only from unit tests calling the function directly. Nothing in the
codebase was broken, nothing failed, and the phase reported honestly on
everything it *claimed* — the claim was simply narrower than the machinery
implied, and nobody had written down which was which.

| Promotion type | Live before P4.2? |
|---|---|
| tool result sets | **yes** (P4.1) |
| MCP results | **yes** (P4.1) |
| user selections | no — flag existed, never set |
| corrections | no — flag existed, never set |
| failures | no — flag existed, never set |
| project milestones | no — no signal path at all |
| architectural decisions | partly — seeded into the `decisions` table, never as episodes |

---

## Prior architecture, and the actual gap

Nova already had strong memory. It is worth being precise about what was
missing, because the temptation with a brief like this is to rebuild things that
already work.

| Already present | Where |
|---|---|
| Facts with confidence, decay, reinforcement, contradiction, supersession | `facts` table, `MemoryUnifier` |
| Provenance vocabulary (STATED / OBSERVED / EXTERNAL / INFERRED / …) | `memory/provenance.py` |
| Knowledge graph | `edges` table, `GraphStore` |
| Semantic recall | Chroma, rebuildable |
| Conversation summaries | `conversation:<id>` facts |
| Hot artifacts, ordinals, trust, freshness | `memory/artifacts.py` (V2) |
| Recall gating | `memory/recall_gate.py` (V2) |

**The gap was durability of episodes and artifacts.** `ArtifactStore` was
explicitly hot and in-memory; only a compact summary survived a restart, via
`to_summary_fact()`. So "what was that second drive we looked at yesterday?"
was unanswerable after a restart — not because Nova lacked memory, but because
the ordered result set that gives "second" a meaning was gone.

Facts, episodes and artifacts are genuinely different things and P4 keeps them
distinct rather than collapsing them:

```
FACT      Marcus owns an RTX 5080.
EPISODE   On 2026-07-28 Marcus compared water-cooling vs overclocking it.
ARTIFACT  The benchmark output that comparison was based on.
```

Flattening an episode into a fact loses the evidence chain that makes it
checkable; flattening a fact into an episode loses the ability to state it
plainly.

## Storage model — and why not another database

**SQLite stays authoritative.** Episodes, artifacts and decisions are new tables
in the *existing* memory database (schema **v7**, migration 7). They are small,
strongly relational, and want exactly the transactional guarantees facts already
have. A second database would have added another thing to keep consistent, back
up and migrate, for no measured benefit.

**Only heavy evidence leaves SQLite.** `memory/cold_store.py` is a
content-addressed filesystem store under `<memory_dir>/cold/`, because large
blobs are read rarely, never queried by content, and would sit inside every
backup and VACUUM of a database that is otherwise a few megabytes (7.16 MB at
2,000 episodes with their result items). Content addressing also means the same
200 KB result captured three times costs one copy.

The DDL lives in `memory/episodic_schema.py` and is referenced by **both** the
create block and migration 7, so the fresh-database schema and the upgrade path
cannot drift.

## The tiers

| Tier | What | Where | Bounded by |
|---|---|---|---|
| **HOT** | current conversation, active result sets, unresolved references | `ArtifactStore` (in-memory, unchanged) | per-conversation cap |
| **WARM** | what happened: summary, entities, project, trust, freshness, importance, provenance | `episodes`, `artifacts`, `decisions` in SQLite | retention rules |
| **COLD** | full tool output, large payloads, transcripts | content-addressed files under `<memory_dir>/cold/` | 8 MB per item |

The invariant that makes tiering worth anything: **warm records are
self-sufficient**. Ranking, relevance and prompt rendering all work from the
warm row alone. Cold evidence is an optional enrichment, hydrated only after
something has been shortlisted.

## Retrieval pipeline

```
1. hot context          already in front of her?
2. episodic gate        does this turn reference the PAST at all?   ← stops most turns
3. warm query           SQL match on summary/entities, relevance-first
4. rank                 entity overlap · importance · recency · prior access
5. cold hydration       shortlist only, opt-in
6. budget               hard character cap; trust + freshness carried
```

**The gate is deliberately stricter than the fact-recall gate.** Fact recall
fails *open* because forgetting something Nova knows is the worst outcome.
Episodic search fails *closed*: it is a database query for things that happened,
and running it on every turn costs latency while buying almost nothing. It
requires positive evidence that the past is being asked about.

## Performance

Measured against a **2,001-episode corpus (1.22 MB)** with
`tests/bench_episodic_v4.py`.

### Added pre-inference latency

| Scenario | Added latency | Prompt chars added |
|---|---:|---:|
| **greeting** | **0.01 ms** | **0** |
| fact lookup | 0.02 ms | 0 |
| on-screen reference | 0.03 ms | 0 |
| historical recall | 17.9 ms | 145 |
| project history | 12.4 ms | 0 |
| needle in haystack | 24.9 ms | 435 |

**"Good morning" adds 10 microseconds and zero prompt characters.** The P2.5
fast path (~130 ms) is untouched. The gate is pure string work — no database, no
model — and turns that do not reference the past never reach the query.

### Component costs

| | median |
|---|---:|
| gate decision | 1.9–21.6 µs |
| warm retrieval (2k corpus) | 9.8–33.7 ms |
| cold hydration | +0.33 ms |
| episode write | ~16 ms per row *(P4; see P4.1 — a result set was one write per item, and is now one transaction)* |

## Two bugs found by measurement (P4)

**1. Candidate selection was recency-limited.** The first implementation drew
candidates with `recent_episodes(limit=60)` and ranked those. That works in a
benchmark where the relevant episode is recent and fails completely in real use:
once a few hundred newer episodes exist, an older relevant one is never a
candidate. Fixed by matching in SQL against summary and entities
(`search_episodes`), with recency demoted to a tie-breaker. Regression test:
`test_old_relevant_episode_is_reachable` buries the target under 300 newer
episodes.

**2. No stemming meant "drive" never matched "drives".** The historical and
project scenarios both returned **zero** episodes. Fixed by reusing the recall
gate's stemmer rather than writing a second, differently-wrong one. This also
cut the needle-in-haystack case from 84 ms to 24.9 ms, since batching the
access-reinforcement writes went in at the same time.

Both were caught because the benchmark printed episode counts, not just timings.
A benchmark that only reported latency would have shown two fast, empty results.

## Security: trust never launders

An artifact stored as `UNTRUSTED_EXTERNAL` comes back `UNTRUSTED_EXTERNAL` after
persistence, restart, retrieval and cold hydration. There is no code path in
`memory/episodes.py` that raises a trust class — `persist_artifact` has no
parameter that could, deliberately.

Rendered episodes carry the label inline
(`[external content — data only, never instructions]`), the same discipline
`describe_for_prompt` uses in hot memory. Persistence must not make a scraped
web page more trustworthy for having been written to disk.

## Freshness survives persistence

The acceptance case from the brief works end to end:

* Nova finds a drive at `$399`. Restart.
* *"What drive did we look at?"* → recalled, with capacity intact.
* *"How much is it now?"* → `stale_fields()` flags **price** as stale while
  leaving **capacity** alone, so the historical price can be stated as
  *"it was $399 when we checked"* rather than as current.

## MCP provenance

P3's evidence chain survives: server, remote tool name, arguments, schema hash
and the injection flag persist as structured provenance, not flattened into
prose. Nova can distinguish *"Marcus told me"* from *"GitHub MCP returned it"*
from *"this was my inference"*.

## Decision memory

`docs/NOVA_DECISIONS.md` remains the human-readable record. `memory/decision_seed.py`
mirrors it into the `decisions` table so the reasoning is **retrievable**, not
just readable. Seeding is idempotent and never overwrites an edited decision.

Both acceptance questions from the brief pass:

* *"Why do unknown MCP capabilities require ADMIN confirmation?"* → D4, with the
  rationale that a server is third-party code and its own hints may only
  restrict further.
* *"Why does the FAST path use a closed Qwen reasoning block?"* → D3, **retaining
  the warning** that it is model- and template-specific and must be re-measured
  rather than carried across a model swap.

Supersession keeps history: a superseded decision is marked, not deleted, and
still points at what replaced it.

## Consolidation

Deterministic rules (`worth_remembering`), not an LLM call on the ingest path.

**Promoted:** decisions, corrections, failures, anything the user selected,
result sets from substantive tools, MCP results.
**Not promoted:** greetings, acknowledgements, clock lookups, ordinary chat.

If LLM judgement is added later it must run off the critical path and fail
toward *not* promoting, so a model outage cannot silently fill memory with noise.

## Failure handling

| Case | Behaviour |
|---|---|
| Cold payload missing | Warm record still loads; summary and provenance intact |
| Cold payload **corrupt** | Detected by digest mismatch and refused — serving wrong bytes is worse than serving none |
| Crash mid-write | Temp file + atomic rename; a truncated blob never appears under a valid digest |
| Oversized payload | Refused at 8 MB with a log line |
| One bad artifact | Isolated; other artifacts unaffected |
| Pre-P4 database | Migrates to v7 and gains the tables |

## Lifecycle

`prune_episodes` is deliberately narrow: only episodes that are **old AND
unimportant AND never revisited AND not a decision/project/preference**. Nova
must never quietly forget a decision or a stated preference because a cleanup
job ran. Tested.

## Migration and compatibility

* Schema **v6 → v7**, additive only. No existing table is altered; no data is
  rewritten. An existing database gains four empty tables. **P4.1 adds no
  further migration** — it writes to tables P4 already created.
* All pre-existing suites pass unchanged (75 at P4, 76 at P4.1).
* `ArtifactStore` gains an optional `on_artifact` callback and nothing else.
  With no callback it behaves exactly as before, which is what every existing
  artifact test asserts.
* **Rollback:** set `NOVA_EPISODIC_MEMORY=0` to stop both storing and querying
  while leaving hot artifacts and facts untouched. Reverting the code leaves the
  tables inert; nothing else reads them.

---

# Production integration (P4.1)

## Where it plugs in

Exactly two places in `core/runtime.py`, plus one hook.

### Writing — a promotion hook on the hot store

```
tool / MCP  →  ArtifactStore.add*  →  on_artifact hook  →  worth_remembering?
                                                              ↓ yes
                                        queue (bounded, drops)  →  EpisodicIngestWorker
                                                                        ↓
                                                          one transaction: episode + items
```

The hook lives on `ArtifactStore` itself rather than in the tool loop, and that
is the load-bearing choice. **Anything that produces an artifact is considered
for durable memory by virtue of producing one.** MCP needs no special case:
`McpManager` already stored an artifact carrying its full P3 provenance, so it
was promoted the day the hook existed. A subsystem writing its own episodes
would have been a second persistence path, and two paths disagree within a
release.

Children are announced with their parent, once, when the set is complete —
"the second one" is meaningless without the set it belongs to.

### Reading — one call in context assembly

`_episodic_context()` runs after hot reference resolution and the fact-recall
decision, before the tool loop. It returns a prompt block and a precedence flag,
and the common case is that it returns nothing at all.

Its output is **its own labelled block**, deliberately separate from facts
("Things you remember"), hot artifacts ("On screen right now") and live tool
output. History that reads like current state gets quoted as current state.

## Persistence is not on the turn path

P4 measured ~16 ms per artifact row. A three-item result set is four rows, so
awaiting it inline would have put ~60–150 ms between Marcus finishing a sentence
and Nova starting to answer — handing back most of what P2.5 reclaimed.

Measured on the real path:

| | |
|---|---:|
| enqueue, on the turn | **0.035 ms** (P90 0.046 ms) |
| completion, in the background | 41.7 ms per episode |

The turn pays **three orders of magnitude less than the write costs.** That
ratio is the whole argument for a worker. (The enqueue figure is tens of
microseconds and correspondingly noisy — an earlier run measured 0.12 ms. The
conclusion does not depend on which.)

Accepted work is not lost on shutdown. The worker drains *before* setting its
stop event — the ordering `MemoryIngestWorker` learned the hard way, since a
worker that stops first and drains second discards the most recent turns
invisibly, the turn itself having succeeded. Tested by interrupting three
in-flight writes with a real shutdown and finding all three after restart.

**Back-pressure drops rather than blocks.** A full queue costs an episode, never
a reply. This is visible in the corpus builder: 300 result sets captured
back-to-back with no awaits lost a third of them before the worker ran once. A
real conversation cannot produce tool results at that rate, but the counter
(`dropped`) exists so it is never a silent loss.

## Duplicate safety

Background delivery means one turn can be observed twice — a retry, a duplicated
publish, a restart around an accepted event. The episode id is **derived** from
the artifact id (`ep-<artifact_id>`), which is generated once at capture, so a
redelivery is an idempotent overwrite. Tested: three deliveries of one event
produce one episode and three items, not nine.

## Ordinal precedence, made explicit

Three things can answer "the second one", and the order is:

1. **Wording about the present** → the set on screen. `needs_episodic_memory`
   never opens for it, so no historical set can steal a current ordinal.
2. **Wording about the past** → the historical set, *even when something is on
   screen*. "The second drive we looked at yesterday" also matches the current
   set positionally, so both layers resolve it; the historical answer wins and
   the hot selection is dropped rather than shown beside it. A prompt saying
   both "he means the LG monitor" and "he means the WD Gold" is worse than
   either.
3. **Neither clearly meant** → ask. If two old result sets score within 1.25× of
   each other, ambiguity is surfaced and nothing is chosen.

Resolution is arithmetic over stored order throughout. The model is never asked
which item is second.

## Cold hydration policy

Warm answers most questions: *what did we look at*, *what did we decide*, *did
we already try this*. Cold is read only when the request is for the evidence
itself — exact wording, the full output, the actual numbers, an error message.

Measured: an ordinary recall performs **zero** cold reads; "what were the exact
numbers from that benchmark" performs one.

## Performance on the real context path

`tests/bench_episodic_v41.py` drives `RuntimeManager` and toggles one variable —
`NOVA_EPISODIC_MEMORY=0` (the P4 state, substrate present and unwired) versus
`1`. Same process, same corpus, same scripted model. **2,002 episodes / 8,006
artifacts / 7.16 MB, all created through capture → hook → worker.**

| Scenario | Before | After | P90 | DB queries | cold reads | prompt chars |
|---|---:|---:|---:|---:|---:|---:|
| **"Good morning."** | 0.001 ms | **0.067 ms** | 0.108 ms | **0** | **0** | **0** |
| on-screen ordinal | 0.001 ms | 0.103 ms | 0.141 ms | 0 | 0 | 0 |
| known fact | 0.001 ms | 0.119 ms | 0.151 ms | 0 | 0 | 0 |
| historical recall | 0.001 ms | 30.9 ms | 38.1 ms | 1 | 0 | 859 |
| decision recall | 0.001 ms | 2.9 ms | 3.1 ms | 1 | 0 | 1042 |
| exact evidence | 0.001 ms | 26.2 ms | 27.9 ms | 1 | **1** | 717 |

**The fast path costs 67 microseconds, zero database queries and zero prompt
characters.** Not "low latency" — no query at all, asserted by counting calls
rather than by measuring time. A test that only measured milliseconds would pass
against a fast query too.

The scripted model means these are *pre-inference* numbers: the work Nova does
between reading a message and calling the model, which is the only part P4.1 can
affect.

### P2.5's end-to-end benchmark, before and after

`tests/bench_nova_v3.py` on the real GGUF, run either side of this work:

| | before | after |
|---|---:|---:|
| TTFT median | 125 ms | 132 ms |
| TTFT P90 | 13,242 ms | 13,120 ms |
| empty replies | 0/10 | 0/10 |
| errors | 0 | 0 |

Unchanged within noise, and expected to be: that benchmark assembles its own
prompt rather than going through `RuntimeManager`, so it cannot see P4.1's
context assembly at all. It is run here to catch a *global* regression — an
import, a worker, a startup cost — not to measure the feature. The
per-scenario table above is the measurement.

The P90 remains dominated by the reasoning and long-reply cases, which is the
documented technical debt P2.5 left behind and P4.1 does not touch.

## Four bugs integration found

None of these were visible to P4's isolated tests.

**1. Cold evidence was unreachable.** `retrieve()` decides whether to hydrate by
checking `episode.provenance["cold_ref"]` — but nothing ever set it. The digest
lived on the artifact row, which retrieval has not loaded yet, and loading it to
find out whether there was anything worth loading would defeat the tiering. So
evidence was written correctly to disk and could never be read back. Measured on
the real path: cold hydration fired **zero** times. The warm row now carries the
digest.

**2. A result set cost four connections and four commits.** `persist_result_set`
looped over `persist_artifact`, each opening its own connection. Measured on the
real promotion path: **147.5 ms** to store one thing that happened. It was also
the wrong transaction boundary — a crash midway left a result set with *some* of
its items, and a half-persisted ordered set is worse than none, because "the
second one" then quietly means something else. Episode and evidence now go in
one transaction: **41.7 ms**, ~3.5× faster and atomic.

**3. Decision memory dropped the decision that mattered most.** Rendering
exceeded the retrieval budget and the whole record was skipped — and D3 is the
longest record Nova has *and* the one carrying a live warning ("Qwen-specific,
re-measure on a model swap"). The decision most in need of being remembered was
the one that never surfaced. Fields are now clipped individually, in priority
order: what was decided, then the constraint on it, then why. Losing the
rationale costs context; losing the constraint turns a warning into a
recommendation.

**4. Historical recall could say a search happened but not what it found.**
`describe_episode` rendered only the summary, so *"what did we look at?"*
produced "1 result from web.search for 'raid setup'" and nothing else. The item
titles were already on the warm row — carried so relevance could be judged
without evidence — and were simply not being shown. Now rendered, bounded to
five, still warm-only.

Two smaller gaps in the gates, both from the brief's own acceptance cases:

* `_HISTORICAL` only matched past-tense verbs, so *"what drive did we **look**
  at?"* — where the past tense sits in the auxiliary — fell through to
  default-closed and never reached history.
* **Decisions needed their own gate.** None of the decision acceptance cases
  reference the past, so the historical gate refused them, correctly. A "why is
  it built this way" question is not an event.

## Instrumentation

`RuntimeManager.episodic_status()` returns counters, not content: gate skips,
searches, warm hits, cold hydrations, decision searches and hits, historical
ordinals, ambiguities, failures, plus the worker's queued / persisted / dropped /
failed / queue depth / last error. No memory content is logged by default.

## Failure isolation

Every path degrades rather than failing the turn, tested end to end:

| Injected failure | Result |
|---|---|
| episodic query raises | reply still returns; failure counted |
| write raises | worker survives, counts it, chat continues |
| queue full | episode dropped and counted; reply unaffected |
| missing / corrupt cold payload | warm record still serves |
| `NOVA_EPISODIC_MEMORY=0` | nothing stored, nothing queried, hot artifacts unchanged |

Fact memory and hot artifact behaviour are unaffected in every case.

---

# Event promotion (P4.2)

## The rule: route evidence, do not invent it

The tempting fix was to pass the four missing booleans. That would have required
Nova to *decide* whether a turn was a correction — a second detector, disagreeing
with the first one within a release.

Every signal already existed, computed by something with better evidence:

| Signal | Who already knew | Evidence used |
|---|---|---|
| **selection** | `ArtifactStore.resolve()` on the turn path | the resolved artifact, plus wording that expresses a choice |
| **correction** | `MemoryUnifier` / `MemoryIngestWorker` | `memory.corrected` (with was/now), `memory.superseded` |
| **failure** | `ErrorLog` + `ToolRouter` | normalised signature; transients already swallowed by retry |
| **project** | `ProjectBuilder` | `project.started` / `completed` / `error` |

`core/episodic_promoter.py` routes those into the **existing** P4.1 queue and
worker. It decides; it never writes. There is still exactly one thing in the
process that writes episodes.

## Selection: the highest-value case

Nova shows three drives. Marcus says *"let's go with the second one."*

Both halves of the evidence are already in hand: the hot resolver has determined
that means the WD Gold, deterministically, and `is_selection()` distinguishes a
choice from a question about the same item. **No model is asked to rediscover
either.** *"What about the second one?"* resolves the same artifact and is not a
selection.

The result set is **not copied**. The selection is its own small episode holding
the chosen artifact — with its ordinal position, parent link, trust and freshness
intact — and the original result-set episode is *reinforced* through P4's access
mechanism. So these stay different questions with different answers:

* *"What drives did we look at?"* → the three-drive set
* *"What drive did I end up choosing?"* → the WD Gold

Identity is the selected artifact id, so *"let's go with the second one"*,
*"yeah, I like that one"* and *"I'll take the WD Gold"* are **one** decision.

## Failure: one occurrence, or a pattern?

Measured and decided rather than assumed. Two thresholds, for two different kinds
of evidence:

* **`project.error` promotes immediately.** A failed build is user-visible,
  project-scoped, and the thing Marcus asks about next time.
* **Generic errors promote on the 3rd occurrence of the same signature.**
  `ToolRouter` already retries once and only publishes `tool.error` after every
  attempt failed, so a single event already means "failed for real". That is
  still not a durable life event — a flaky endpoint would fill memory with
  identical rows. Three of the *same normalised signature* is a pattern.

Identity is the signature, so the fourth and fiftieth occurrence update one
episode rather than adding forty-seven. `ErrorLog.signature` is reused rather
than reimplemented: if episodic memory and the self-improvement loop disagreed
about what "the same error" means, they would tell Marcus different stories about
one fault.

Never promoted: `tool.not_configured` (a state, not a defect), `permission.*`
(a refusal working correctly), `autonomy.*` / `dev.*` (Nova talking to herself).

## Project: milestones, not ticks

`project.started`, `project.completed` and `project.error` are events.
`project.progress` fires many times per build — writing files, repairing, running
checks — and is never promoted. A real build in the test suite produces exactly
**two** episodes.

## Corrections are events, facts are state

```
FACT      Marcus's GPU is an RTX 5080.                     (fact memory owns this)
EPISODE   On the 14th Marcus corrected his GPU: 3080 -> 5080.
```

Fact memory keeps doing exactly what it did. The episode adds the thing a fact
cannot say about itself — that the belief *changed*, and when. Re-stating an
unchanged value supersedes a row without anything having been wrong, and is
rejected.

## Explicit decisions — where they went, and why

Deliberately **not** a second architecture-decision system.

* Artifact-backed choices ("let's buy the WD Gold") are **selections**. That is
  the concrete form the decision takes, with the evidence attached.
* Project-level decisions arrive as **project events**.
* The `decisions` table stays reserved for architectural records with rationale,
  alternatives, constraints and supersession — fields a conversational choice
  does not have and would only be given empty.
* Free-prose decisions with no artifact and no state transition ("keep this
  feature local-only") are **not promoted.** There is no deterministic evidence
  that a decision was made, and inventing one means an LLM classifier on the
  ingest path. That belongs off the critical path and must fail toward *not*
  promoting — see known limitations.

## Importance policy

One scale, in one place (`memory/episodic_recall.py::IMPORTANCE`), anchored on
what P4/P4.1 already used:

| | |
|---|---:|
| tool / MCP result | 0.5 |
| result set with items | 0.6 |
| project event | 0.7 |
| failure | 0.7 |
| selection | 0.8 |
| correction | 0.85 |
| decision | 0.9 |

The **ordering** is the claim, not the exact numbers: what Marcus decided
outranks what he chose, which outranks what happened to a project, which
outranks something Nova fetched. Pruning deletes at ≤ 0.3, so none of these are
ever pruning candidates — the values drive ranking, not survival. `PROTECTED_KINDS`
additionally shields selections and corrections from age-based cleanup, for the
same reason decisions were already shielded.

## Noise

The measurement that matters for a memory system is what it *refuses*.

A synthetic session of **34 interactions** — 20 greetings, 5 routine questions,
3 searches, a selection, a correction, and a failure repeated three times —
produced **7 durable episodes**: 4 result sets, 1 selection, 1 correction,
1 failure.

Worth noting precisely: the 25 trivial turns were not "rejected", they never
reached the promoter at all. A greeting produces no artifact and no error event,
so nothing is ever asked about it. The rejection counter saw 2 — the first two
occurrences of the failure signature, below threshold.

## Latency

Promotion decisions, measured (`tests/bench_episodic_v42.py`):

| Event type | median | P90 |
|---|---:|---:|
| artifact | 3.8 µs | 4.5 µs |
| selection | 5.8 µs | 6.0 µs |
| correction | 8.0 µs | 8.6 µs |
| project | 7.9 µs | 8.3 µs |
| failure | 10.7 µs | 11.2 µs |
| `project.progress` (rejected) | 1.3 µs | 1.4 µs |

The failure path is slowest because it normalises the message into an
`ErrorLog` signature — which is the work that stops a flaky endpoint writing
fifty near-identical episodes.

Three of the five paths are not on the turn at all: they arrive on the event bus,
which publishers fire and forget. A publish costs **6.1 µs with a subscribed
promoter versus 4.0 µs without** — one more bounded queue. Nothing in
`ProjectBuilder` or `MemoryUnifier` waits for a promotion.

**The fast path is unchanged:** "Good morning" still does zero episodic database
queries, zero episode writes, and adds zero prompt characters — re-measured
through P4.1's own benchmark after this work, at 72.9 µs and 0 queries against
the 2,002-episode corpus.

**On variance:** the retrieval numbers in the P4.1 table above move by 30–50%
between runs on this machine (historical recall has measured 21–48 ms across
runs of identical code). Treat the *orders of magnitude* as the result —
microseconds to decide, tens of milliseconds to retrieve, zero of either on the
fast path — not the third significant figure.

## Known limitations

*P4's first two limitations were closed by P4.1; P4.1's unstated one — four of
the five promotion signals never reached production — was closed by P4.2. What
remains:*

0. **Free-prose decisions are not promoted.** "Keep this feature local-only"
   leaves no artifact and no state transition, so there is no deterministic
   evidence a decision was made. Adding an LLM classifier is the obvious
   extension and the obvious hazard: it belongs off the critical path and must
   fail toward NOT promoting, or a model outage quietly fills memory with noise.

1. **Retrieval ranking is lexical.** Entity overlap plus stemming, no
   embeddings. Adequate at 2k episodes (47 ms worst case); unmeasured at 50k.
   Historical recall is the slowest path by an order of magnitude and is the
   first place to look if it degrades.
2. **Gating is regex-based, so phrasing decides reachability.** Two of the
   brief's own acceptance phrasings failed before being added to the pattern,
   which is direct evidence that others will. The failure is silent: a closed
   gate looks exactly like a retrieval-quality problem. D6 says to check the
   gate first for that reason.
3. **Capabilities can pre-empt context assembly entirely.** Nova's Navigation
   capability claims "find me …" before the agent loop runs, so *"find me what
   we looked at yesterday"* never reaches episodic retrieval. Pre-existing
   routing behaviour, not introduced here, but it bounds what memory can answer.
4. **Episode writes cost ~40 ms.** Fine in the background, still too slow for
   a hot loop. One connection per episode.
5. **The `cold_evidence` table is defined but unused.** P4's schema created it;
   the cold store is content-addressed on the filesystem and nothing indexes
   into the table. Its row count is therefore always 0 and should not be read
   as "no cold evidence". Left rather than populated: an index no query reads
   would be architecture for its own sake.
6. **No `edges` integration.** Relationships (episode→project,
   decision→evidence) are columns and JSON rather than graph edges. Reusing
   `GraphStore` is the right long-term shape.
7. **No LLM consolidation.** Deterministic rules only.
8. **Chroma is not involved.** Episodes are not semantically indexed.
9. **No live-hardware validation.** Every measurement here comes from the real
   runtime with a scripted model. The end-to-end voice path — and P0's
   microphone barge-in acceptance run — remain outstanding and are not affected
   by this work either way.
