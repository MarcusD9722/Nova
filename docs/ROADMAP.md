# Nova — Development Roadmap

**Status:** Draft for approval · **Date:** 2026-07-18 · Companion to `docs/ARCHITECTURE.md`.

Ordering principle: **foundation before features, interfaces before scale, hands
after permissions, hardware-gated work last.** Every phase ships something Marcus
uses immediately, ends with the full verification ritual (tests green → build clean →
real boot → live chat check → commit), and leaves Nova bootable every day.

---

## Phase 0 — Foundation & Hygiene  *(blocks everything; mostly mechanical)*

The un-glamorous phase that makes a multi-year build survivable.

| # | Milestone | Notes |
|---|---|---|
| 0.1 | **Commit the working tree.** Initial commit of 5 months of uncommitted work (113 files), then a per-round commit rule. | Day one. Highest-risk single item in the project. |
| 0.2 | **Tests into the repo.** Migrate the 14 scratchpad suites → `tests/`, add `run_tests.ps1` one-shot runner. | Makes verification reproducible by anyone (including Nova). |
| 0.3 | **API auth.** Bearer-token middleware on all 46 endpoints; Electron/UI injects the key; localhost stays default bind. | Prerequisite for 24/7 and any remote access. |
| 0.4 | **Central config.** Typed settings module consolidating the 63 `NOVA_*` vars (names preserved), validated at boot. | Kills silent misconfiguration. |
| 0.5 | **DB migrations.** `schema_version` + ordered migration scripts; current schema = v1. | Prerequisite for Memory 2.0 schema work. |
| 0.6 | **Mechanical splits.** `app.py` → APIRouter modules (chat/voice/vision/memory/dev/maps/system); `App.jsx` → feature hooks/components. No behavior change; tests prove it. | Ends the god-file merge hazard. |
| 0.7 | Docs & cleanup: README refresh, `CONTRIBUTING.md` (one-writer rule from the concurrent-edit postmortem), delete PrinterSheet orphan + redundant drag/resize libs, exclude config-state errors (PluginConfigError) from self-correction candidates. | |

**Exit:** clean boot, all tests green from `tests/`, authenticated API, committed tree.
**Depends on:** nothing. **Everything depends on it.**

## Phase 1 — Memory 2.0 (Living Memory)

The deepest intelligence upgrade available on current hardware.

- **1.1** `edges` table + graph layer over existing entities (people/projects/facts/events/docs); auto-edge extraction on ingest; `memory.related` / `memory.timeline` tools.
- **1.2** Memory-type formalization (semantic/episodic/procedural/working mapping) — mostly naming + retrieval routing, not new storage.
- **1.3** Confidence & decay: recency/confidence-weighted ranking in `search()` (never deletes); confidence reinforcement on re-mention.
- **1.4** Consolidation pass in the reflection worker: lesson/fact dedup + weekly episodic summaries (the duplicate-lesson list is the proof of need).
- **1.5** Knowledge-graph UI panel (nodes/edges browser, timeline view).

**Exit:** "how is X connected to Y" answered from the graph; duplicate lessons collapsed; regression suite extended.
**Depends on:** 0.5 (migrations), 0.1–0.2.

## Phase 2 — Orchestrator & Model Router

- **2.1** `Agent` protocol + Orchestrator extraction of the ReAct loop (no behavior change at default settings).
- **2.2** Policy classes re-homed as agents under the one interface.
- **2.3** **Deep mode**: Planner → Executor → Critic pipeline, opt-in for coding/complex requests; Critic verdict surfaced honestly; absorbs ProjectBuilder's existing plan/build/test/fix pipeline rather than duplicating it.
- **2.4** ModelRouter: role → model-instance mapping, config-driven; today everything maps to the one 9B — the interface is the deliverable.
- **2.5** Self-eval v2: nightly report (latency percentiles, tool failure rates, empty-reply count, wake/STT errors, memory hit quality) via existing reflection worker + a UI card.

**Exit:** deep mode demonstrably catches an error single-pass misses; nightly report generating.
**Depends on:** Phase 0. Benefits from Phase 1 (graph context in planning).

## Phase 3 — Always-On & World Model

- **3.1** WorldState store + system pollers (CPU/RAM/disk/net/processes/battery-UPS-if-present; extends GPU telemetry).
- **3.2** Tray + auto-start + native Windows notifications; smart filtering (only reminders, approvals, completed builds, and world-state alerts interrupt; everything else waits in-app).
- **3.3** Backup & restore: one-command export/import of `memory_data` + `credentials` + config (local file; encrypted optional).
- **3.4** Packaging: real `build_backend.ps1` (PyInstaller) + installer; boot-with-Windows option.
- **3.5** **Jellyfin plugin** (media agent v1): library search/recommend/recently-added in chat; world-state "now playing".
- **3.6** Morning briefing v2 fed by WorldState (server health joins weather/calendar).

**Exit:** Nova survives a reboot unattended, reachable from tray, notifies natively, backs herself up nightly.
**Depends on:** Phase 0 (auth mandatory before always-on). Parallel-safe with Phase 1/2 after 0.

## Phase 4 — Hands (Computer Control & Browser)

Security gate: **4.1 ships before any actuator.**

- **4.1** Permission tiers (`read/standard/admin/critical`) enforced in ToolRouter; per-tool map; generalized confirm-broker for `critical`; persistent action audit log + UI.
- **4.2** Observe: running apps/windows/active document → WorldState (feeds cross-app context awareness).
- **4.3** Act v1: allowlisted app/file operations (open/focus/close, project-scoped file management) at `admin` tier.
- **4.4** Act v2: generic input synthesis (reuses the hand-tracking cursor bridge) — `critical`, per-session arming, visible action log.
- **4.5** Browser automation as an isolated Playwright service (research + form-fill with approval).
- Registry/system-settings writes: **deferred indefinitely** (see ARCHITECTURE §4.3).

**Exit:** "open the Jellyfin dashboard and check transcode load" works end-to-end with an auditable trail.
**Depends on:** Phase 2 (orchestrator plans multi-step control), Phase 3 (WorldState), Phase 0 (auth).

## Phase 5 — Creation Suite  *(partially hardware-gated)*

- **5.1** Coding pipeline v2 = deep mode specialized (architect/coder/tester/reviewer roles) + git integration for projects (auto-init, commit-per-change, diff review in UI).
- **5.2** Model upgrade decision executed (see Open Decisions): Coder-14B swap **or** dual-model routing once the 3080 is in.
- **5.3** Image generation goes live on the 3080 (service already built); inline gallery + project asset linking.
- **5.4** Video generation v1 (short clips, honest expectations) on the 3080.
- **5.5** 3D pipeline: Blender-headless service (avatar pipeline precedent) for parametric models/exports; game-dev workflow polish on ProjectBuilder.

**Exit:** "build and iterate a small game with tests, commit history, and generated art" as one guided flow.
**Depends on:** Phase 2 (orchestrator); 5.3/5.4 on **RTX 3080 installed**.

## Phase 6 — Physical World  *(device-gated)*

- **6.1** Home Assistant integration (when devices exist) as the home-automation agent; devices → WorldState; `critical`-tier actuation.
- **6.2** Camera scene-watch service: periodic VLM scene descriptions + change detection → world events ("package at door") — honest about VLM limits; explicit opt-in per camera.
- **6.3** 3D printing: resurrect PrinterSheet against OctoPrint/Moonraker when a printer exists.
- **6.4** Robotics/drone/vehicle: interface stubs + world-model schema only. No hardware, no build — revisit yearly.

**Depends on:** Phase 3 (WorldState), Phase 4 (permissions), and **hardware Marcus acquires**.

---

## Dependency graph

```
Phase 0 ─┬─► Phase 1 ─┬─► Phase 2 ─┬─► Phase 4 ─► Phase 6
         │            │            └─► Phase 5 (5.3+ gated on RTX 3080)
         └─► Phase 3 ─┴─────────────► (4, 6 world-state inputs)
```

**Recommended order:** 0 → 1 → 2 → 3 → 4 → 5 → 6, with 3 allowed to interleave after 0
(its milestones are independent), and 5.3/5.4 sliding to whenever the 3080 lands.

## Open decisions (Marcus)

1. **RTX 3080 allocation** — image/video gen only (current plan), or timeshare a second
   LLM (utility model or coder model) when not generating? Affects ModelRouter config, nothing structural.
2. **Model upgrade** — swap chat model to Qwen2.5-Coder-14B (better code, one model) vs.
   keep 9B + route coding to 14B on the 3080 (needs decision 1).
3. **Cloud sync** — out by default per local-first values; want encrypted off-site backup anyway?
4. **Home hardware** — Phase 6 stays parked until you actually buy devices/cameras; no pre-build.

## Risk register

| Risk | Mitigation |
|---|---|
| Rewrite stall (the classic death) | Strangler rule; Nova bootable daily; every phase ships user-visible value |
| Concurrent editors corrupting mid-flight work | One-writer rule (CONTRIBUTING.md); committed tree makes drift visible in `git diff` |
| GPU OOM as capabilities stack | ModelRouter owns VRAM budget; services own their device via CUDA_VISIBLE_DEVICES (imagegen precedent) |
| Scope creep from the capability list | Phases only pull from this roadmap; new ideas get triaged into it, not bolted on |
| 9B latency making deep mode feel broken | Deep mode opt-in + progress events streamed to UI; latency budgets in self-eval v2 |
