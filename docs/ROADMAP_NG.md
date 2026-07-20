# Nova Next-Generation Roadmap (Goals #1–#20 integrated)

**Status:** Approved 2026-07-20 · **Date:** 2026-07-20 · Extends `docs/ROADMAP.md`
(Phases 0–2 are ✅ merged) and `docs/ARCHITECTURE.md`. Nothing here rewrites a
working system; every item is an extension behind a feature flag.

## Build order (set by Marcus, 2026-07-20)

**3.5 → 4 → 5 → 6 → 7 → 8 → 3 → 9 → 10.** Marcus chose to start with the cheap
cognitive instrumentation (3.5) and defer the Awareness Substrate & Always-On
work (Phase 3) to *after* Phase 8. This is safe: Phase 3.5 has no dependency on
Phase 3, and Phase 3 still precedes Phase 9 (Physical World), which needs it.
Two acknowledged consequences of the deferral:
- **Executive Intelligence (#1, Phase 5) has no proactive push channel** until
  Phase 3 lands — it computes recommendations that surface in-app/in-chat rather
  than as background tray notifications. Acceptable during active development.
- **Autonomous Experimentation (#15, Phase 7) can't measure hardware resource
  usage** until Phase 3's pollers exist; it measures speed/accuracy/reliability
  meanwhile.
- *Optional amendment on the table:* pull `backup/restore` out of Phase 3 into
  3.5 so the memory we accumulate through 3.5→8 is protected sooner.

## The core finding

The 20 goals are not 20 greenfield builds. Mapped against what Nova already runs:

| Already a working foundation | Goals it feeds |
|---|---|
| Knowledge graph (P1.1) | #8 KG 2.0, #7 Vision Memory, #11 World Model |
| Memory confidence + decay + reinforcement (P1.3) | #19 Provenance |
| Self-eval metrics collector (P2.5) | #14 Benchmarking, #12 Internal State |
| Orchestrator agent roles + deep mode (P2.1/2.3) | #5 Agent Society |
| ModelRouter (P2.4) | satisfies "future model swaps without refactor" already |
| Goals + AgentSupervisor | #3 Planning Engine |
| Mood / wellbeing / habits / interest-drift | #4 Digital Twin |
| Reflection / self-improve loop | #6 Internal Thought, #9 Research, #15 Experimentation |
| Guarded self-editing + external project roots | #10 Codebase Understanding, #18 SW-Eng Platform |
| web.search / web.fetch | #9 Research, #11 World Model |
| Planned Phase 3 (WorldState/system pollers) | #13 Hardware Intelligence |
| Planned Phase 3.5 (Jellyfin) | #17 Media Intelligence |
| Planned Phase 4 (Hands/permissions) | #2 Skill Learning |
| Planned Phase 6 (Home Assistant) | #16 Smart Home |

**Consequence:** the next build phase does not change — it's still Phase 3
(Awareness Substrate). These 20 goals mostly *enrich* the phases already queued
and add three genuinely new capstone phases (Executive, Agent Society, SW-Eng).

## Non-negotiable constraints that shape sequencing

1. **One 9B on one GPU, serialized.** Agent society, internal thought, research,
   and experimentation all add LLM calls. They run as *paced background work*,
   never blocking chat. "Persistent agents" = durable prompt+memory+confidence
   bundles whose collaboration is turn-based — real concurrency waits on the
   RTX 3080 / a second model (which ModelRouter already makes a config change).
2. **Substrate before synthesis.** Executive Intelligence (#1) can't reason about
   your day until WorldState, the digital twin, and planning exist to reason over.
3. **Observation before automation.** Skill learning (#2) requires watching your
   actions — it depends on computer-control/observation, which depends on the
   permission system shipping first.
4. **Hardware/OAuth honesty.** Vision memory needs cameras; smart home needs
   devices; media/exec need the Google OAuth you run once; image/video need the
   3080. Each stays inert-but-honest until its gate is met — the project's rule.
5. **Provenance before more autonomy.** #19 (memory provenance) lands early,
   because research/agents/experiments will *write* memories, and we never want
   an autonomous inference stored as if it were a confirmed fact.

## Phase sequence

Each phase ships behind a feature flag, keeps Nova bootable daily, and ends with
the ritual (tests green → build clean → live check → PR). Complexity: S/M/L/XL.

### Phase 3 — Awareness Substrate & Always-On *(DEFERRED to after Phase 8 per Marcus, 2026-07-20)*
Everything proactive needs a live world-state + a resident process.
- WorldState store + system pollers → **#13 Hardware Intelligence** (CPU/GPU/RAM/
  VRAM/disk/SMART/temps/net; anomaly flags; history) — **M**
- Tray + auto-start + native Windows notifications; smart interrupt filtering — **M**
- Backup / restore of memory+config; PyInstaller packaging — **M**
- Jellyfin plugin → **#17 Media Intelligence v1** (library, now-playing, recommend) — **M**
- **Depends on:** Phase 0 auth (done). **Gate:** none (Jellyfin needs your server URL).

### Phase 3.5 — Cognitive Instrumentation *(✅ SHIPPED 2026-07-20)*
Cheap, hardware-free, and it instruments everything downstream. Delivered:
memory provenance (schema v3 + assumption-vs-fact hedging), internal operational
state (7 telemetry-derived metrics advising replies), and self-benchmarking
(daily-history trends + regression flags at `/autonomy/benchmarks`). All behind
feature flags (`NOVA_MEMORY_PROVENANCE`, `NOVA_INTERNAL_STATE`, `NOVA_SELF_BENCHMARK`).
A UI surface for internal-state/benchmarks is a thin follow-up (data is already
API-accessible).
- **#19 Memory Provenance** (source, evidence, verification status, last-confirmed,
  re-verification; extends existing confidence/decay) — **M**
- **#12 Internal Operational State** (confidence/curiosity/workload/focus as *reasoning
  metrics*, not feelings; extends self-eval) — **S/M**
- **#14 Self-Benchmarking** (scheduled evals, trend reports, regression alerts;
  extends the P2.5 metrics collector) — **M**
- **Depends on:** P1.3, P2.5. **Gate:** none.

### Phase 4 — Deep Memory & World Understanding *(✅ SHIPPED 2026-07-20)*
- **#8 Knowledge Graph 2.0** — `path_between` (shortest-path reasoning), `subgraph`
  (bounded neighborhood), `discover_associations` (shared-neighbor auto-discovery,
  paced in the self-improve cycle), universal typed `link()` for any node kind;
  `memory.path`/`memory.link` tools + `/memory/path`, `/memory/graph/subgraph`.
- **#11 Semantic World Model** — new `world_model` table (migration v4), sourced
  subject-predicate-object triples with confidence + freshness, kept separate from
  the personal graph; refuses unsourced facts; `world.recall`/`world.learn` tools +
  `/memory/world`; `remember_web_finding` folds web results in.
- **#6 Persistent Internal Thought** — new `thoughts` table (v4), private notes
  (idea/question/unresolved/improvement/discovery/…) surfaced only on request;
  `thoughts.note`/`thoughts.recall` + `/thoughts`; a benchmark regression auto-files
  an improvement thought.
- Flags: `NOVA_WORLD_MODEL`, `NOVA_INTERNAL_THOUGHTS`. Migration v4 verified on a
  copy of the real DB (v2→v4, 128 facts intact).

### Phase 5 — Executive Intelligence & Planning *(✅ SHIPPED 2026-07-20)*
- **#4 Personal Digital Twin** (`NOVA_DIGITAL_TWIN`) — `core/digital_twin.py`: work
  hours, peak focus period, procrastination likelihood, stress, learning speed,
  interests, all derived from recorded signals with a `basis` each; predicts, never
  impersonates; honest cold-start. `twin.profile` tool + `/twin`.
- **#1 Executive Intelligence** (`NOVA_EXECUTIVE`) — `core/executive.py`: confidence-
  gated, ranked, deduped, throttled recommendations (overdue/looming deadlines, focus
  window, stalled goals, weather, breaks) synthesized from goals/reminders/habits/twin.
  Stays silent on weak signals; surfaces ≤2 into grounding; `executive.brief` + `/executive`.
- **#3 Long-Term Goal Planning** — `core/goal_planner.py`: vision→milestones→dated items,
  **adaptive roll-forward** (missed recurring items advance instead of going overdue;
  open milestones past target → at_risk), progress scoring. Plan stored as a JSON fact
  per goal (no migration). `plan.save`/`plan.status`/`plan.advance` + `/plans/{goal_id}`.
- **#9 Autonomous Research** (`NOVA_RESEARCH`, OFF by default) —
  `core/workers/research_worker.py`: paced, killable topic worker that
  search→summarize→**cites** into the world model; never fabricates. Topic store +
  `research.track`/`research.list`/`research.findings` + `/research`.
- Backend/API only. **Google OAuth (calendar/email) still unlocks the Executive's full
  value** — it degrades honestly without it (uses reminders/goals/habits/twin).

### Phase 6 — Persistent Agent Society
- **#5 Multi-Agent Society** (persistent specialists — Engineer/Architect/Researcher/
  Coach/etc. — each with durable memory, confidence, experience, reasoning style;
  Executive routes which participate; structured turn-based collaboration) — **XL**
- **Depends on:** Phase 2 orchestrator + Phase 4 (world model/memory to specialize on).
- **Honest note:** serialized on one GPU today; genuinely concurrent debate arrives
  with a second model.

### Phase 7 — Autonomous Software Engineering
- **#10 Continuous Codebase Understanding** (index every registered project: files/
  classes/functions/deps/TODOs/tests/debt; semantic navigation; impact analysis
  before edits) — **L**
- **#18 Autonomous SW-Engineering Platform** (auto architecture/API docs, DB diagrams,
  tests, security + dependency + health reports; self-maintaining where safe) — **XL**
- **#15 Autonomous Experimentation** (safe A/B of prompt/retrieval/ranking/scheduling
  variants; measure speed/accuracy/reliability/resources; adopt only with approval) — **L**
- **Depends on:** self-editing (built), #14 benchmarking, Phase 6 agents (architect/
  coder/critic). **Risk:** highest so far → approval-gated, sandboxed.

### Phase 8 — Computer Control & Skill Learning *(Hands)*
- Permission tiers (read/standard/admin/critical) + confirm-broker + audit log —
  **ships before any actuator** — **M**
- Staged computer control: observe running apps/windows → allowlisted actions →
  gated input synthesis (reuses the hand-tracking cursor bridge) — **L**
- **#2 Autonomous Skill Learning** (detect repeated multi-step workflows → "want me to
  learn this?" → parameterize / edit / version / branch → later, conditional
  auto-triggers) — **XL**
- **Depends on:** observation (computer control). **Deliberately late:** highest blast
  radius; needs months of clean permission-audit history first.

### Phase 9 — Physical World *(hardware/device-gated)*
- **#16 Smart Home Intelligence** (abstraction layer buildable now; context-aware rules;
  Home Assistant integration when devices exist) — **L, gated**
- **#7 Vision Memory** (episodic: objects/rooms/packages/whiteboards/changes with
  timestamps+confidence; "what changed in my office / where are my headphones") — **L,
  needs cameras**

### Phase 10 — Nova OS Unification *(capstone, emergent)*
- **#20 Nova Operating System** — not a discrete build but the *convergence*: the
  Executive layer (#1) + orchestrator routing every subsystem so you interact with
  one cohesive intelligence. Delivered by finishing the above, plus a unifying
  command surface and a single "what is Nova doing / thinking / tracking" view.

## Cross-cutting engineering (applies to every phase)
Feature flag per subsystem (extends `core/settings.py` catalog + `/status`);
async workers with low idle CPU; provenance/confidence on every written memory;
robust configurable logging; unit + integration tests per subsystem (self-verifying
where possible, as the config/catalog tests already are); public-module docs;
model-swap-safe interfaces (ModelRouter); privacy-by-default (all local unless you
configure an external service); migration paths (the schema-version system).

## Immediate next step
Phases 3.5, 4, and 5 are ✅ shipped. **Next up is Phase 6 (Persistent Agent
Society)** — durable specialist agents (#5) with their own memory/confidence/
experience, routed by the Executive layer — pending Marcus's go-ahead. Honest note
for Phase 6: still serialized on one GPU, so agents are durable prompt+memory
bundles collaborating turn-by-turn; genuine concurrency waits on the RTX 3080.
Phase 3 (Awareness Substrate) remains deferred to after Phase 8.

**Open decisions for Marcus:** (1) confirm this sequencing or reprioritize a specific
goal earlier; (2) run the Google OAuth setup before Phase 5 (Executive needs it);
(3) 3080 timing (unblocks generation + a concurrent second model for the agent
society); (4) which smart-home ecosystem, if/when you buy devices.
