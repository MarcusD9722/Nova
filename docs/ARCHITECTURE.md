# Nova — Software Architecture Document

**Status:** Draft for approval · **Date:** 2026-07-18 · **Author:** Lead engineering session (Claude), audit grounded in measurements taken against the working tree on this date.

Nova is a local-first, GPU-only AI companion running on Marcus's Windows 11 machine
(RTX 5080 16GB; RTX 3080 pending install). This document is the audit of what exists,
the debt register, and the target architecture for evolving Nova from a very capable
assistant into a persistent local AI operating layer — **without ever breaking the
working system to get there.**

---

## 1. Design invariants (already proven, carried forward)

These are not aspirations; they are existing properties of the codebase that every
future phase must preserve:

1. **Honest failure.** Every capability reports real state ("not configured", "second
   GPU not detected", "boot test skipped") rather than faking success. This is Nova's
   defining quality bar.
2. **Local-first, GPU-only.** No CPU fallback, no cloud inference. Cloud integrations
   (Google OAuth, Discord, maps, weather) are explicit, narrow, and degradable.
3. **Self-modification is propose → human-approve → apply → boot-test → auto-rollback.**
   Never weakened, including for the background self-improvement loop.
4. **SQLite is the source of truth; semantic indexes are rebuildable caches.** A Chroma
   failure can never lose data.
5. **Secrets live in `.env` and `credentials/` only**, deny-listed from every read/write
   surface Nova's own tools can reach.
6. **Explicit action for sensitive senses.** Screen/camera capture requires a user
   click or an explicitly enabled session — never silent.

## 2. Current state (as measured, 2026-07-18)

### 2.1 Component inventory

| Layer | What exists |
|---|---|
| **API host** | FastAPI (`backend/app.py`), 46 endpoints: chat/SSE, speak/stt, vision, memory, reminders, goals, dev-mode, autonomy, maps proxies, WS event stream |
| **Cognition** | Single ReAct tool loop in `core/runtime.py` (≤6 steps); 8 single-purpose policy LLM classes (`core/policy/`): chat decider, summarizer, memory extractor, autonomy planner, storyteller, follow-up, contracts |
| **Model** | One `LLMRuntime` instance — Qwen3.5-9B-Q6_K, 8K context, llama-cpp CUDA, GPU-offload enforced at boot; XTTS voice; faster-whisper STT; Qwen VL mmproj vision |
| **Memory** | `memory/unifier.py` facade over SQLite (truth) + Chroma (semantic) + diskcache + JSON audit. Stores: facts (w/ singleton supersession), people, events, turns (indexed), dated digests, lessons, mood/day, wellbeing/day, habits (tool-usage log), documents + chunks, reminders, goals/tasks |
| **Workers** | 5 async workers, one consistent lifecycle shape: memory-ingest, self-improve (error capture + reflection), reminders (+briefing, birthdays, habit scan), autonomy supervisor, agent supervisor (goals) |
| **Events** | In-process pub/sub bus (`core/event_bus.py`), 100-event replay, WS fan-out to UI |
| **Tools** | ~36 registered tools: memory.*, project.*, self.*, reminder, goal, image/video.generate, maps.*, weather, discord, calendar/email (OAuth), web, shell (guarded), vision.look_at_screen (confirm-gated) |
| **Self-editing** | `core/dev_mode.py`: proposals w/ full old/new content + real diff viewer UI; backups; boot smoke test; external project roots (compile-check only, honestly flagged) |
| **Services** | `tools/imagegen/` — the one process-isolated service (own venv, CUDA_VISIBLE_DEVICES pinned), the proven pattern for GPU/dependency isolation |
| **Frontend** | React/Electron: chat, 3D avatar (photo-relief + lip sync), maps w/ live Google Map + QR handoff, memory/tasks/system/improvements panels, screen vision, focus sessions, wake word + voice sessions |

### 2.2 Vision-checklist coverage (what the "AI OS" spec already has)

Persistent memory ✅ · timeline/date recall ✅ · goals→tasks engine ✅ · long-term
projects ✅ (ProjectBuilder + per-project state) · self-evaluation/improvement ✅
(30-min reflection cycle; proposals never auto-applied) · vision ✅ (camera + screen,
single-shot) · speech ✅ (wake word, sessions, mood-paced TTS) · scheduling ✅
(reminders, recurring, missed-while-offline) · research ✅ (search+fetch) · document
indexing/synthesis ✅ · calendar/email ✅ (read + draft-only, OAuth pending user setup)
· notifications △ (in-app only) · computer control △ (cursor primitives exist for hand
tracking; no general control) · knowledge graph ✗ · world model △ (GPU telemetry only)
· multi-agent ✗ (one loop) · home automation ✗ (no devices) · media server ✗ ·
image/video gen △ (built, awaiting 3080) · packaging ✗.

**Implication:** this is an *evolution* project, not a greenfield build. Roughly half
the "long-term capabilities" list is live today; the transformation work is
architecture (structure, isolation, interfaces) more than features.

## 3. Technical debt register (ranked by risk)

| # | Debt | Evidence | Remedy (phase) |
|---|---|---|---|
| **D1** | **Months of work uncommitted.** Last commit `f54fd42` is **5 months old**; 113 modified/untracked files carry every feature round since. One bad `git restore`/disk failure loses everything. | `git log`/`git status` 2026-07-18 | Phase 0, day one: commit, then per-round commit discipline |
| **D2** | **Test suites live outside the repo** (session scratchpad); `tests/` contains one file. Verification history is real but not reproducible by anyone else (or by Nova). | `tests/` = `test_intent_routing.py` only; 14 suites in scratchpad | Phase 0: migrate suites into `tests/`, add `run_tests.ps1` |
| **D3** | **Four god-files.** `App.jsx` 2,104 · `app.py` 2,065 · `runtime.py` 1,791 · `unifier.py` 1,534 lines. Every feature lands in the same four files; merge hazard with concurrent editors; hard to reason about. | `wc -l` | Phase 0 (mechanical splits), then strangler-fig per phase |
| **D4** | **No API authentication.** 46 endpoints open to anything that reaches the port; admin token guards only `/memory/purge`. Blocks the 24/7 / multi-device ambition. | endpoint audit | Phase 0: bearer-token middleware + Electron key injection |
| **D5** | **Config sprawl.** 63 distinct `NOVA_*` env vars read ad-hoc at call sites; no schema, defaults scattered, no validation at boot. | grep count | Phase 0: typed central config (pydantic-settings), env-var names preserved |
| **D6** | **No DB migrations.** Schema evolves via `CREATE TABLE IF NOT EXISTS` + ad-hoc ALTERs; no version stamp; rollback of a bad schema change is manual. | `sqlite_backend.py` | Phase 0: schema_version table + ordered migration scripts |
| **D7** | **Single-process coupling.** Bus is in-process; a crash in any subsystem takes down all of Nova; UI event replay is capped at 100. Only imagegen is isolated. | `event_bus.py` | Phases 2–4: isolate by *criteria* (GPU/dep-conflict/crash-risk), not dogma |
| **D8** | **Concurrent-editor hazard.** Proven incident: `unifier.py` edited mid-run by another session → NameError crash-loop in a live process. No lockfile/convention. | 2026-07-17 os-crash postmortem | Phase 0: `docs/CONTRIBUTING.md` one-writer rule + git discipline makes drift visible |
| **D9** | **No packaging; stale README.** `dist:win` scripts reference a nonexistent `build_backend.ps1`; README describes dev-mode TODOs that shipped long ago. | package.json / README | Phase 3 (packaging), Phase 0 (README refresh) |
| **D10** | **Model ceiling & no routing.** One hardcoded model instance; 8K context; every cognitive job (chat, planning, extraction, coding) shares it serially. No abstraction to route roles to different models when hardware allows. | `llm_runtime.py` | Phase 2: ModelRouter interface; hardware decision (3080/14B) pending |
| **D11** | **Memory gaps vs. spec.** No cross-memory relationships (graph), no decay/recency weighting at retrieval, no consolidation (observed: many near-duplicate lessons), confidence stored but never updated. | live DB observation | Phase 1 |
| **D12** | Minor: orphaned `PrinterSheet.jsx`; 3 redundant drag/resize libs; self-improve proposes "fixes" for config-state errors (observed with OAuth-not-connected). | prior audits | Phase 0/3 cleanups |

### Addendum — 2026-08-03 (U10)

The measurements above are as of 2026-07-18 and are left intact. Two rows moved:

**D2 gained a missing category, not just files.** The suite was rigorous but
every one of its 46 suites ran against fakes — 0 booted a backend. Two bugs
reached Marcus through a fully green run. `tests/harness.py` now boots the real
`backend.app` (temp root, scripted model, no network) and four `test_it_*.py`
suites drive real turns. On their first run they surfaced four defects that unit
tests structurally could not see: a shutdown sequence that aborted halfway, a
leaked non-daemon aiosqlite thread that stopped the process from ever exiting, a
pending map request that hijacked the following turn, and a maps failure that
reported the wrong cause. Detail: `docs/NEXT_SESSION.md`.

**D3 started shrinking by strangler, not by mechanical split.** `runtime.py` is
2,209 → 1,868 lines; navigation now lives in `core/capabilities/navigation.py`
with its own patterns, state and replies. `RuntimeManager` keeps the ORDER
decision — that ordering is behavior, so the capability exposes two entry points
rather than one. Weather and identity/clock are the next two, one per commit,
each behind the integration suites.

## 4. Target architecture

### 4.1 The honest constraint that shapes everything

**One 9B model on one GPU means "multi-agent" is role orchestration over a shared
model, serialized.** Each internal agent turn is a full LLM call (~5–20 s). A
Planner→Coder→Critic chain is 3–5× the latency of a direct answer. Therefore:

- Agent roles are **prompt + tool-allowlist + model-preference bundles**, not processes.
- Deep multi-agent pipelines are **opt-in modes** (complex/coding/background tasks),
  never the default chat path. Chat stays one-pass fast.
- All interfaces are written so a role's `model` field can later point at a second
  model instance (3080 timeshare, or a Coder-14B swap) **without changing callers.**
  Scale is a config change, not a rewrite.

### 4.2 Layered model

```
┌─ Interface     Electron UI (dashboard modules) · voice · tray/notifications · REST/WS (authed)
├─ Cognition     Orchestrator (agent roles: planner, critic, coder, researcher…)
│                ModelRouter (role → model instance) · policy prompts
├─ Memory        Unifier facade → SQLite(truth) + graph edges + Chroma(index) + caches
│                Consolidation worker (dedup/decay/summarize)
├─ World         WorldState store ← pollers (system, weather, media, home…) → grounding slice
├─ Action        ToolRouter + permission tiers → tools/plugins · guarded DevMode
│                confirm-broker for Critical actions (screen-broker pattern generalized)
├─ Services      process-isolated when GPU/dep/crash risk: imagegen (exists), later
│                vision-watch, browser-automation, TTS if it earns it
└─ Kernel        event bus · config · scheduler/workers · permissions · migrations · logs
```

Nothing here requires a rewrite. Each layer is an extraction target for code that
already exists in the god-files; the strangler rule is: **new code lands in the new
module; old code moves only with its tests.**

### 4.3 Subsystem designs (deltas from today)

**Memory 2.0** — Formalize what exists into named memory types (semantic = facts/
people/graph; episodic = turns/events/digests; procedural = lessons/habits; working =
conversation state + grounding) and add the missing piece: an `edges` table
(`subject → predicate → object`, typed endpoints across facts/people/projects/events/
documents, with weight, confidence, created/last-reinforced timestamps). Retrieval
gains **recency/confidence weighting** (decay affects ranking, never deletes), and a
**consolidation pass** in the existing reflection worker merges duplicates (the lesson
list already proves the need). Graph queries become tools (`memory.related`,
`memory.timeline`) and a UI panel.

**World Model** — A `WorldState` store (typed keys, timestamps, staleness) fed by
cheap pollers: system (CPU/RAM/disk/net/processes — extends existing GPU telemetry),
weather (existing), media/home/printer states as those integrations arrive. Grounding
context gets a compact world-state slice; the bus gets `world.changed` events; the UI
gets a live dashboard card. This is the substrate scene-vision and home automation
plug into later.

**Orchestrator** — Extract the ReAct loop from `runtime.py` into `core/orchestrator/`
with an `Agent` protocol (name, system prompt, tool allowlist, model preference, step
budget). The 8 policy classes become agents under one interface. First real pipeline:
**deep mode** = Planner → Executor → Critic for coding/complex tasks, explicitly
invoked, with the Critic's verdict surfaced honestly. The existing project-builder
pipeline (plan→code→run-check→logic-tests→fix) is already 70% of the "coding
workflow" and gets absorbed rather than duplicated.

**Permissions** — Formalize the implicit tiers: `read / standard / admin / critical`.
Per-tool tier map enforced in ToolRouter; `critical` (delete outside projects,
registry, installs, network changes, any physical actuator) requires a live confirm
via the generalized confirm-broker (the `vision.look_at_screen` request/approve flow,
made generic). Plugin manifest v2 declares required permissions + config schema + UI
panel + emitted events; loader enforces.

**Computer control** — Staged, permission-gated: (1) observe (window list, active app
→ WorldState); (2) act on allowlisted app operations; (3) generic input synthesis
(the hand-tracking bridge already moves/clicks the real cursor — reuse) only behind
`critical` confirms with a visible action log. Browser automation as an isolated
Playwright service. **Registry/system-settings writes stay out of scope** until the
permission system has months of clean audit history.

**Non-goals (stated honestly):** robotics/drones/vehicle get interface stubs and
world-model schema slots only — no hardware exists. Camera-based emotion detection is
not planned (text-mood is the honest signal). No cloud sync by default — it
contradicts local-first; revisit only as encrypted opt-in backup. No "AI security
analyst" theater on a 9B; hardware/network *monitoring* yes, intrusion detection no.

## 5. What must never regress

Boot-and-verify discipline (every phase ends with: full test suite green, `npm run
build` clean, real boot, live chat check); the invariants in §1; and Nova staying
**usable every single day** — no phase may leave her broken overnight. The strangler
rule and per-phase commits are how a solo-maintainer, multi-agent-edited codebase
survives a multi-year build.
