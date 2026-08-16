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

**Decided:** 2026-08-14 (V3 P4.1) — **refined by [D9](#d9) the same day (V3 P4.2).**
The single-path principle held. The assumption that every promotable event is
artifact-backed did not, and D9 records why.

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

---

## D9 — One promotion POLICY, not one promotion SOURCE

**Decided:** 2026-08-14 (V3 P4.2). Refines [D7](#d7); does not supersede it.

**Decision.** Durable memory is decided in exactly one place
(`core/episodic_promoter.py`) and written by exactly one thing (the P4.1 queue
and worker). But the promoter accepts events from several sources, and an event
is no longer required to be artifact-backed. `EpisodicPersistEvent` now carries
either a live artifact or a self-describing event with its own stable identity.

**What D7 got right, and what it assumed.** D7's real content is "no subsystem
writes its own episodes", and that has held: MCP still needs no special case,
and there is still one writer. What it *assumed* — because every case in front
of it at the time was artifact-backed — is that hanging promotion off
`ArtifactStore` was sufficient. It is not. A correction is not an artifact. A
project milestone is not an artifact. A recurring failure is not an artifact.

**Rationale.** The alternative was to manufacture artifacts for events that have
none, purely to keep the wording of D7 true. That is fabricating evidence to
satisfy a data shape: a synthetic "artifact" for a correction would carry a
trust class, a freshness class and a provenance dict describing a thing that
never existed. Widening the event is the smaller and more honest change.

**Alternatives rejected.** Fake artifacts for non-artifact events (invents
evidence); a second promoter per source (two policies drift, and each would
reimplement trust and provenance handling — exactly how a security invariant
erodes); passing `worth_remembering()` more booleans computed by a new detector
(a second correction/failure classifier disagreeing with the one fact memory and
ErrorLog already run).

**Constraint.** Every source must supply a DETERMINISTIC stable identity —
artifact id, error signature, entity+attribute, project slug plus the publish
timestamp. Random ids on a redelivery-sensitive path are prohibited: background
delivery means every event can arrive twice, and "Marcus chose the WD Gold"
must not accumulate copies. The promoter still never writes and never calls a
model.

**Lifecycle constraint (added 2026-08-14, V3 P4.2.1).** This is a two-queue
pipeline, and shutdown order is therefore a correctness property, not tidiness:

```
producers → BUS → promoter queue → persistence queue → SQLite
```

Every stage stops only after everything feeding it has stopped, and drains what
it already accepted before it stops. Concretely: all six producers, then the
promoter, then the persistence worker. Shipping it the other way round lost 11
of 12 queued events, and `MemoryIngestWorker` publishes `memory.superseded`
*during its own drain* — so the likeliest correction in a shutdown was the one
most at risk. Do not reorder these calls without re-running
`tests/test_episodic_durability_v421.py`.

**Evidence.** `tests/test_episodic_events_v42.py` — 34 interactions produce 7
episodes; three phrasings of one choice produce one; seven occurrences of one
failure produce one; redelivery of every event type is idempotent.
`tests/bench_episodic_v42.py` — every promotion decision is 1.3–10.7 µs, and a
bus publish costs 6.1 µs with a subscriber versus 4.0 µs without.

**Revisit if.** A source appears that cannot supply a stable identity. The fix is
to find one in the source, not to relax the requirement.

---

## D10 — A changed choice supersedes within its result set, and the old one is kept

**Decided:** 2026-08-14 (V3 P4.2.1)

**Decision.** Selection episodes are identified by the chosen artifact, so
saying the same choice again is one decision. Choosing a *different* item from
the **same result set** marks the earlier selection `superseded_by` the newer
one. Selections from different result sets never affect each other. The
superseded episode is marked, never deleted.

**Rationale.** Identity-by-artifact is what makes "the second one" / "yeah, that
one" / "I'll take the WD Gold" a single decision, and it is right. It also meant
that changing your mind produced two episodes with `superseded_by IS NULL` —
measured: 2 — so "what did I choose?" had two equally current answers with
nothing to rank between them.

Scope is `parent_id` because that is the choice context. Anything wider —
supersession by artifact type, or globally by kind — would have a monitor
comparison silently retire your choice of drive, which is not a thing the user
did.

Keeping the old episode is what makes "what did I originally pick, before I
changed my mind?" answerable at all. Normal retrieval already filters
`superseded_by IS NULL`, so a replaced choice stops competing without
disappearing, and a narrow gate (`wants_superseded`) lets an explicitly
historical question see it — rendered with a SUPERSEDED marker so it cannot read
as current.

**Alternatives rejected.** Deleting the previous selection (destroys the history
that makes the question answerable, and a choice is exactly the thing worth
keeping); global supersession by kind (unrelated decisions retire each other);
leaving both active and ranking by recency (a guess presented as an answer —
the same failure D8 rejected for ordinals).

**Evidence.** `tests/test_episodic_durability_v421.py` — changed choice leaves
one active and one marked; switching back restores the first without creating a
third; a drive and a monitor stay independently current; repeating one choice
still yields one active episode.

**Constraint.** No new table. `superseded_by` already existed on `episodes` with
exactly these semantics for decisions; reusing it is the point. Supersession runs
in the persistence worker, only for events carrying a scope — ordinary turns
never reach it.

**Revisit if.** Users report Nova naming a choice they had already changed. Check
the scope (`parent_id`) before touching retrieval ranking.

---

## D11 — Speaker read scope is a positive allow-list at the data layer

**Decided:** 2026-08-15 (V3 P5.1d)

**Decision.** A speaker's read scope is decided by `may_read_entity()` in
`core/turn_identity.py` and applied inside `MemoryUnifier.search()` — the single
point every semantic read passes through. It is an **allow-list**: shared
entities (`world`, `system`, `capability`) are readable
by anyone; the owner reads everything; a known guest reads their own namespace
plus shared; anything not positively recognised is refused. The disk cache key
includes the speaker **and** cached results are re-filtered on the way out.

**Rationale.** P5.1 enforced privacy in grounding only. Measured on `78cba4d`:
the `memory.recall` **tool** returned Marcus's private fact to an unknown speaker
on request. A boundary enforced only in grounding is one tool call wide, and the
model can make that call.

An allow-list rather than a deny-list because a personal entity added in a later
phase must be private by default rather than public by oversight. `note` is
deliberately excluded from shared: it is free-form and routinely holds personal
material, and "not stored under `user`" is not the same as "public".

**Alternatives rejected.** Instructing the model not to reveal other speakers'
data (a prompt is not a boundary — the audit measured the model reading around
it with a tool call); filtering at each caller (a caller that forgot would be a
silent leak, and there are three independent read paths); a deny-list of private
entities (fails open for every entity anyone adds later).

**Evidence.** `tests/test_speaker_privacy_v51d.py` — the owner sees his own
facts; a guest sees theirs plus shared and never his; an unknown speaker sees
shared only; a cache warmed by the owner is not replayed to the next speaker;
`memory.recall` refuses an unverified speaker.

**Constraint.** The filter is a no-op for the owner, byte for byte, so pre-P5
behaviour is unchanged. It must stay inside `search()` rather than moving to
callers. The cache key alone is insufficient — cached results are re-filtered,
because a key still trusts whatever was stored under it.

**Revisit if.** A guest needs to read something shared that is currently private.
Add it to `SHARED_ENTITY_ROOTS` explicitly; do not weaken the default.

**Amended 2026-08-15 (P5.1d.1).** Three corrections, all reproduced first:

1. Matching is now **delimiter-exact** (`under_root`). The original
   `startswith` meant `worldsecret` and `system_private` were classified as
   shared. An allow-list that matches on substring is not an allow-list.
2. The filter runs **before** reinforcement and before the cache write. It ran
   after, so a denied hit still bumped `access_count` 0 → 1 and stamped
   `last_accessed_at` — a side channel, and a corruption of the reinforcement
   signal itself.
3. The unverified-speaker refusal in `memory.recall` was removed. It predated
   this rule and contradicted it: shared knowledge is readable by anyone, and
   Nova could not tell a visitor where the Eiffel Tower is. Generic recall
   delegates to this filter; only date-range *history* keeps its own gate.

---

## D12 — Identity crosses an async boundary by snapshot, never by inheritance

**Decided:** 2026-08-15 (V3 P5.1d)

**Decision.** `MemoryIngestEvent` carries a `TurnIdentity` snapshot taken where
the turn ran. The ingest worker re-enters it with `active_turn()` and routes
every extracted fact through `remap_entity_for()`. An event with **no** identity
is treated as legacy owner semantics; an identity that resolves to **nobody**
discards the fact rather than redirecting it.

**Rationale.** P5.1 scoped the live turn with a `ContextVar`. A `ContextVar` does
not cross a queue. The background extractor writes the *durable* facts, seconds
to minutes after the speaker has gone, on a worker task that never entered
`active_turn` — so it read the typed default and filed every guest's first-person
statement under `user`. This is the write that mattered most, and the one the
synchronous fix missed entirely.

`None` must mean "write nowhere". Turning it back into a default is the exact
failure the whole phase exists to prevent.

**Alternatives rejected.** `contextvars.copy_context()` into the worker (it is
long-lived and processes a queue — there is no single context to copy, and it
would bind whichever turn happened to start it); reading `current_identity()` in
the worker (this *is* the bug — it yields whoever is speaking when the backlog
drains, or the default when nobody is); passing a `profile_id` string (the worker
would have to re-derive attempted/status/role, reimplementing the attribution
matrix in a second place).

**Evidence.** `tests/test_speaker_ingest_v51d.py` — a guest's spouse is filed
under `speaker:<id>` and never under `user`; an unverified speaker's fact is
written NOWHERE (asserted against the `facts` table, not merely against Marcus's
namespace); the worker ignores an ambient owner identity active while it drains;
an identity-less legacy event still writes to `user`.

**Constraint.** The snapshot is taken at enqueue in `RuntimeManager._finish`. The
worker must never fall back to `current_identity()`. Conversation summaries stay
unscoped on purpose — they are conversation-level, not person-level — and run
outside the `active_turn` block.

**Revisit if.** Another queue-crossing event grows a personal write. Give it a
snapshot field too rather than reaching for the ContextVar.

---

## D13 — One canonical namespace per person, and turn attribution lives in SQLite

**Decided:** 2026-08-15 (V3 P5.1d.1)

**Decision.** Every person's memory is one hierarchy rooted at their personal
entity — `user` for the owner, `speaker:<id>` for a known speaker — with
structured children below it (`speaker:<id>:lesson`, `:mood`, `:wellbeing`,
`:session`, `:person:<x>`). Read policy is a single containment check,
`entity_belongs_to_speaker`, using the same delimiter-exact `under_root` helper
as the shared allow-list. `personal_tail()` normalises a speaker entity to its
owner-equivalent, and salience, decay and singleton rules apply the owner's
existing logic to that normalised form.

Conversation attribution is persisted on the `turns` row itself —
`speaker_entity`, `speaker_label`, `input_source`, `speaker_status` — added by
in-place `ALTER TABLE`, never embeddings or similarity or audio.

**Rationale.** P5.1d put a guest's child namespaces *beside* their root
(`lesson:speaker:p-alice`) while the read policy allowed only the exact root, so
Alice's own lessons, mood and wellbeing were unreadable by Alice. Enumerating
child namespaces by hand is what produced that gap, and would produce it again
for the next one added.

The same fragmentation had already broken person-quality memory: salience,
decay and singleton each had their own idea of what a speaker entity was.
Prefix-matching `speaker:` in the decay rule made every guest fact permanent —
not parity, a different wrong answer. Normalising once makes parity a property
of the namespace rather than three rules that must agree.

Attribution had to move into SQLite because the durable row is what date-range
recall reads. With it only in Chroma metadata, `recall_conversation` could not
distinguish speakers at all, so it refused guests wholesale rather than scoping
them — the reason Alice could not recall her own history.

**Alternatives rejected.** Keeping the beside-the-root shape and listing each
child in the policy (rejected: it is the design that produced the bug, and the
list is unbounded); a separate table for speaker turns (rejected: a second
parallel memory subsystem, and D5's reasoning applies — no new store to fit an
architecture); storing the profile id only and joining (rejected: the label and
input source are what a read needs, and a join buys nothing at this size);
rebuilding the DB rather than migrating (rejected: Marcus's history is the
product).

**Evidence.** `tests/test_speaker_scope_v51d1.py` — Alice reads her root, her
lesson and her nested person fact and none of Bob's or Marcus's;
`speaker:p-alice2` is not inside `speaker:p-alice`; owner and known speaker get
identical salience on all ten core-identity attributes while a guest's hobby and
their acquaintance's name do not become max-salience; the durable row carries
the attribution and a pre-migration row still reads back as owner history.

**Constraint.** `under_root` is the only way to test namespace containment —
never `startswith`. Legacy `<base>:speaker:<id>` entities are still recognised on
read so nothing already written is stranded. Column defaults (`user` / `typed`)
are the correct backfill because every row predating them was Marcus: the
frontend has never sent a speaker identity.

**Revisit if.** A child namespace needs different read semantics from its
parent. That is a policy change in one function, not a new namespace shape.

---

## D14 - The tool surface is scoped by data ownership, not by permission

**Decided:** 2026-08-15 (V3 P5.1d.2)

**Decision.** Every direct memory tool routes through the same identity policy
as the rest of memory. Writes resolve through `resolve_write_target()`, which
**refuses** an entity naming another speaker's namespace rather than remapping
it. Stores with no per-person ownership - `people`, `events`, the knowledge
graph, the digital twin, reminders, thoughts - fail closed for non-owners with a
`scoped_unavailable` result. `memory.remember_person` is scoped instead of
refused, because `speaker:<id>:person:<key>` already exists in the canonical
hierarchy and needs no new store.

`PermissionBroker` is untouched. Every speaker may call every tool and receives
the identical decision; only the reachable data changes.

**Rationale.** P5.1d and P5.1d.1 scoped the paths Nova takes on her own -
grounding, semantic search, quick-fact capture, the async extractor. The tools
the *model* calls were still global, and emitting a tool call is the ordinary
way the model touches memory, so the boundary had a door in it.

Measured on `d1ec5a9`: a guest overwrote another speaker's stored fact via
`memory.correct(entity="speaker:p-bob")`, added people and events to Marcus's
stores, mutated his relationship graph, and read Nova's private notes about him.

The `memory.correct` case shows why refusal beats remapping. Nesting Bob's root
under Alice would produce `speaker:p-alice:speaker:p-bob` - an entity that reads
like a claim about Bob and belongs to nobody. And the model cannot be asked to
pick a safe entity: it does not know who is in the room, so delegating that
would make a privacy boundary probabilistic.

**Alternatives rejected.** Routing identity into `PermissionBroker` (rejected:
speaker identity is not authentication, and D-series precedent is explicit -
this would make a voice match into an authorisation level); building parallel
guest stores for people/events/graph in this patch (rejected: a half-built
second memory system is harder to remove than a gap, and fail-closed is safer
before frontend activation); partially scoping `memory.timeline` (rejected: it
aggregates events, digests and reminders - scoping one source leaves a history
that looks complete and is not); trusting the tool descriptions to keep the
model away from other speakers' data (rejected: descriptions are guidance,
execution is the boundary).

**Evidence.** `tests/test_speaker_tools_v51d2.py` - Alice cannot correct Bob,
Marcus, or beneath either root, and Bob cannot correct Alice; an unverified
speaker persists nothing, asserted against the facts table rather than the
tool's return text; guests neither read nor mutate Marcus's people, events,
timeline or graph; `thoughts.recall` / `twin.profile` / `executive.brief` /
`reminder.create` are owner-only; shared world knowledge still reaches every
speaker; permissions identical across five identities and three capabilities.

**Constraint.** Fail-closed refusals must stay *data* refusals with a sentence
Nova can say aloud - never permission errors. The legacy namespace rule matches
only the four exact shapes P5.1d could write; an `endswith` rule read
`speaker:p-bob:lesson:speaker:p-alice` as Alice's and must never return.

**Revisit if.** `people`, `events` or the graph gain per-person ownership. Then
the `_owner_only` refusals become scoped reads, one call site at a time.

---

## D15 - Persistent-state tools are classified, and the classification is tested

**Decided:** 2026-08-15 (V3 P5.1d.3)

**Decision.** Every registered built-in tool carries an explicit classification -
speaker-scoped, owner-private, shared/system, capability-governed, or ephemeral -
recorded in `tests/test_speaker_persistent_state_v51d3.py::_CLASSIFICATION` and
asserted against the live router. A tool added without one fails the suite.

Owner-private stores fail closed for non-owners at the data layer via
`_owner_only`. Capability tools stay governed by `PermissionBroker` and developer
mode, never by voice.

**Rationale.** P5.1d.2 fixed the tools named `memory.*`. The failure mode left
over was structural: a tool bypasses speaker privacy because the durable state it
touches has a different name. Measured on `641f499` - a guest overwrote the
owner's saved plan; created a goal row *and* an enqueued `__decide__` task, i.e.
unattended background work started by someone Nova cannot name; updated and
**deleted** his learned skill; indexed a folder into his document store; read and
extended his research registry.

Two more were found only because the completeness check compared the inventory
against the live registry: `memory.synthesize` and `skill.run` are registered by
`RuntimeManager`, not `core/tooling.py`, and both read owner-private stores.

And one was a layer below the tool surface entirely: `AgentSociety` injects
`agent_recall` notes into every specialist prompt, so a guest could receive
Marcus's accumulated context inside a deliberation answer without ever calling
`agent.recall`.

Each of the three preceding passes missed something by omission rather than
error, which is why the inventory is now a test rather than a document.

**Alternatives rejected.** Auditing by tool name (rejected: `plan`, `goal`,
`skill` and `thoughts` hold personal state and none is called "memory");
restricting capability tools by speaker (rejected: that is exactly
"voice = authentication", which D14 and the phase brief both forbid); building
guest-scoped plan/goal/skill/document stores in this pass (rejected: four
half-built parallel stores, when fail-closed is safe and reversible);
classifying `agent.recall` from its name (rejected: it was classified from
evidence - `agent_remember` has no production caller and the only note in the
tree is "Marcus prefers primary sources over blog posts").

**Evidence.** `tests/test_speaker_persistent_state_v51d3.py` - the inventory
covers the live registry exactly; a 22-tool sweep confirms every owner-private
tool refuses both a guest and an unknown speaker; the owner's plan, skill store
and document index are byte-for-byte intact after the attempts; guest
`goal.create` adds zero goal rows and zero tasks, asserted against both tables;
`society.consult` still deliberates for a guest but carries none of Marcus's
notes, while the owner's still does; `research.findings` returns only
`{summary, source, confidence}`.

**Constraint.** The classification table must be updated when a tool is added -
the suite fails otherwise, and that failure is the feature. Fail-closed refusals
stay *data* refusals with a sentence Nova can say aloud, never permission errors.
`experiment.*` and `agents.roster` are deliberately shared and must not be
restricted merely because a guest can call them.

**Revisit if.** Plans, goals, skills or documents gain per-person ownership. Each
`_owner_only` call site then becomes a scoped read, one at a time.

---

## D16 - Plugin data scope is required metadata, enforced outside the plugin

**Decided:** 2026-08-15 (V3 P5.1d.4)

**Decision.** Every plugin `ToolSpec` carries a required `data_scope` of
`"shared"` or `"owner_private"`. `@tool(...)` takes it keyword-only with **no
default** - omitting it is a `TypeError` at import time, an invalid value a
`ValueError` at registration. Owner-private plugins are wrapped where specs
become router functions, so a non-owner is refused **before** the plugin body
runs: zero OAuth token retrieval, zero HTTP, zero drafts, zero sends.

The live-router completeness test no longer subtracts plugin names:
`live router == built-in classifications UNION plugin data_scopes`.

**Rationale.** P5.1d.3 declared the router classification-complete after
subtracting every plugin name - so the claim was true only of the set it had
already narrowed to, and the excluded set was exactly the tools reaching
Marcus's connected accounts. Measured on `71fc0eb` with transports mocked and
calls counted: a guest and an unknown speaker each received his unread mail
subjects and snippets, his calendar events with locations, and his Discord
history, and could create a draft in his mailbox and send a message as his bot.

The guard is the outermost layer on purpose. Refusing inside the plugin, or
after the token fetch, would still have touched his credentials - "refused" has
to mean no external work happened, and that is asserted with counters rather
than by reading an error string.

Required-with-no-default matters more than the six current classifications: the
failure being closed is a *future* integration silently inheriting public access
to somebody's private account.

**Alternatives rejected.** Ad-hoc `current_identity()` checks inside each plugin
(rejected: nine files, each free to forget, and the check would sit after the
token fetch); defaulting `data_scope` to `"shared"` (rejected: that IS the bug -
a new plugin must be triaged, not assumed public); defaulting to
`"owner_private"` (safer but silently breaks public tools and teaches authors to
ignore the field); routing plugin calls through `PermissionBroker` here
(rejected: that is generic actuator hardening for P8, and conflating it with
data scope would make a voice match into an authorisation level).

**Evidence.** `tests/test_speaker_plugins_v51d4.py` - owner behaviour unchanged
with identical request patterns (1 token + 2 HTTP for `email.recent`, draft
created); guest and unknown refused on all six with every external counter at
zero; the eight public tools still execute for both; omitting `data_scope`
raises; an invalid value raises; permissions identical across four identities
and five capabilities.

**Constraint.** This is a DATA-scope boundary only. `ToolRouter` still does not
generically broker plugin calls through `PermissionBroker`; a test asserts that
absence so the limitation stays honest. Generic external-actuator permission
coverage is later P8 work and must not be claimed as delivered by P5.

**Revisit if.** A plugin gains genuine per-speaker connected accounts. Then its
scope becomes a third value rather than a refusal, and the wrapper resolves the
speaker's own credentials.
