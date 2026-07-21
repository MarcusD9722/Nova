# Nova

**A local-first, GPU-only AI operating layer that runs entirely on your own machine.**

Nova is a private, always-yours AI partner — a FastAPI backend + React/Vite/Electron
desktop app wrapped around a local LLM. Nothing leaves your computer unless *you* wire
up an external service. She talks, sees, remembers across sessions, builds software,
understands your codebase, coordinates your day, and is being grown into a full
"AI operating system" that sits above your machine and your digital life.

- **100% local by default.** The model, memory, reasoning, and voice all run on your
  GPU. External services (weather, maps, calendar, email, Discord) are opt-in and
  degrade to an honest "not configured" when absent.
- **GPU-only, no CPU fallback.** The model is loaded with full CUDA offload and startup
  *fails loudly* if offload can't be confirmed — no silent slow path.
- **Honest by construction.** Nova never fakes a success, never presents an assumption
  as a fact, and reports what it genuinely can and can't do.

---

## Table of contents

- [What Nova can do today](#what-nova-can-do-today)
- [How Nova is built](#how-nova-is-built)
- [Memory: where it lives and how it works](#memory-where-it-lives-and-how-it-works)
- [Setup](#setup-windows-powershell)
- [Running Nova (boot)](#running-nova-boot)
- [Configuration & feature flags](#configuration--feature-flags)
- [Security & permissions](#security--permissions)
- [Key API endpoints](#key-api-endpoints)
- [Tests](#tests)
- [Roadmap: what's next](#roadmap-what-were-adding)
- [Honest limitations](#honest-limitations-gates)

---

## What Nova can do today

### Conversation & voice
- Natural streaming chat with a warm, consistent persona (a local GGUF model, e.g.
  Qwen3.5-9B, on your GPU).
- **"Hey Nova" wake word** + hands-free voice sessions (no repeated wake word; auto
  idle-timeout).
- **XTTS speech** with subtle mood-aware pacing, **Whisper STT**, and a 3D holographic
  avatar with real-time lip-sync driven by her actual speech.
- **Deep mode** (opt-in): for "think carefully / check your work" requests she runs a
  Planner → Executor → Critic loop and self-reviews before answering.

### Living memory & knowledge
- Remembers facts, people (with birthdays/anniversaries), events, and durable
  behavioral lessons — across sessions, with gentle decay and reinforcement.
- **Knowledge graph** ("how is X connected to Y", shortest-path reasoning, automatic
  relationship discovery), **timelines** ("catch me up on X"), and cross-document
  **synthesis**.
- **Memory provenance** — every fact carries a source, evidence, and verification
  status, so an *assumption* is never presented as a settled *fact*.
- **Semantic world model** — sourced general knowledge (subject → predicate → object
  with citations) so she can answer without re-searching the web.
- **Persistent internal thoughts** — private notes-to-self (ideas, questions,
  improvements) that survive across sessions and surface only when you ask.

### Proactive executive intelligence
- **Executive layer** — confidence-gated, non-annoying recommendations synthesized from
  goals, reminders, habits, weather, and your patterns (looming deadlines, focus
  windows, "take a break").
- **Personal digital twin** — models your working patterns (peak hours, focus window,
  procrastination likelihood) from recorded signals. It *predicts*, never impersonates.
- **Long-term goal planning** — turns a goal into a vision → milestones → objectives
  with adaptive roll-forward of missed tasks.
- **Autonomous research** — track topics and Nova periodically searches, summarizes, and
  **cites** into the world model (never fabricates findings).
- Reminders, timers, recurring check-ins, a proactive morning briefing, and multi-session
  background goals.

### The agent society
- A council of **durable specialists** — Chief Executive/Engineer, Software Architect,
  Research Scientist, Creative Director, Psychologist, Fitness & Snowboard Coaches,
  Media Curator, Financial Planner (general guidance only), Security Specialist — each
  with its own persisted memory, confidence, and experience. The Executive routes who
  weighs in and synthesizes their views.

### Building & understanding code
- Autonomously **builds apps/games** from a description (writes code → run-check →
  generates & runs tests → self-fixes).
- **Continuous codebase understanding** — indexes any registered project (files,
  classes, functions, imports, TODOs) with symbol search and **impact analysis**
  ("what breaks if I change X").
- **SW-engineering reports** — project health score, ranked tech-debt, architecture
  summary, and a defensive security scan of your own code.
- **Autonomous experimentation** — safely A/B-tests approaches and **recommends** a
  winner (never auto-applies; adoption is always your call).
- **Guarded self-editing** — Nova can read her own code and propose diffs; every change
  is human-approved via a real diff viewer, backed up, boot-tested, and auto-rolled-back
  on failure. The same guard extends to your other registered projects.

### Vision & the physical edge
- Analyze your screen (with an explicit confirm click — never silent), camera capture,
  and opt-in periodic "focus session" glances.

### Computer control & skill learning (permissions-first)
- A **tiered permission system** (read → standard → admin → critical) with a confirm-
  broker and durable audit log gates every action. Admin/critical are never auto-allowed.
- **Computer control is propose-only** and a **dry run by default** — it never
  synthesizes real input unless you explicitly enable it *and* install a platform
  adapter. There is no armed autonomous actuator out of the box.
- **Autonomous skill learning** — Nova notices when you repeat a multi-step workflow and
  *offers* to learn it (never automatically); learned skills are parameterized,
  versioned, and branchable, and every step is re-checked through the permission gate.

### World, maps & integrations
- **Maps/navigation** — "how far is X", "nearest gas station", turn-by-turn routes with
  a live map and a phone QR hand-off.
- **Weather**, **web search/fetch**, **Discord** (send/read), and **Google Calendar +
  Gmail** (read-only calendar; Gmail read + *draft-only*, structurally never sends).
- **Image/video generation** (built; pinned to a second GPU — honest "not available"
  until that hardware is installed).

### Self-awareness & instrumentation
- **Internal operational state** — reasoning/operational metrics (confidence,
  uncertainty, workload, focus, energy, curiosity, learning-rate) derived from real
  telemetry (not simulated feelings) that advise how she responds.
- **Self-benchmarking** — daily self-evaluation with trends and regression flags.

---

## How Nova is built

Layered, modular, and local-first. Each subsystem is independently testable, feature-
flagged, and swappable.

| Layer | What lives there |
| --- | --- |
| **Interface** | `frontend/` React/Vite/Electron desktop app, avatar, voice, overlays |
| **Cognition** | `core/runtime.py` (the brain), `core/orchestrator/` (agents, deep mode, model router, metrics, society), executive/digital-twin/planner/experiments |
| **Memory** | `memory/` — unifier facade over SQLite + Chroma + diskcache + JSON; graph, world model, thoughts, provenance |
| **World/Action** | `plugins/` (weather, maps, discord, calendar, gmail, web), `core/computer_control.py`, `core/code_intel.py`, permissions |
| **Services** | `core/workers/` background loops (memory ingest, self-improve/reflection, reminders, autonomy + goal supervisors, research) |
| **Kernel** | GPU-enforced LLM runtime, config catalog, event bus, API auth |

### Repo layout

| Folder | Role |
| --- | --- |
| `backend/` | FastAPI entry package (API routers, SSE chat, voice endpoints) |
| `core/` | Brain: runtime, tool loop, orchestrator, workers, dev mode, permissions, policies |
| `memory/` | Memory engines + unifier (SQLite is the source of truth) |
| `plugins/` | Tool integrations (auto-register at startup) |
| `frontend/` | React/Vite/Electron desktop UI |
| `voices/`, `wakewords/` | TTS reference voice, wake-word model |
| `model/` | GGUF model files (gitignored) |
| `memory_data/` | Runtime memory storage (gitignored) |
| `projects/` | Projects Nova builds autonomously |
| `tools/` | Isolated services (imagegen), avatar pipeline, setup scripts |
| `docs/` | Architecture, roadmaps, contributing |
| `tests/` + `run_tests.ps1` | Offline test suites + runner |

---

## Memory: where it lives and how it works

Nova's memory is a **unified store with SQLite as the single source of truth**. It lives
entirely on disk under `memory_data/` (gitignored — it's *your* data and never leaves
your machine):

```
memory_data/
├── sqlite/nova.sqlite3     ← SOURCE OF TRUTH (facts, people, events, graph, world model,
│                              thoughts, reminders, goals, tasks, documents, skills, …)
├── chroma/                 ← semantic vector index (rebuildable from SQLite)
├── diskcache/              ← short-lived caches (recent search results, etc.)
├── json/                   ← append-only audit log of every write (snapshots)
└── permission_audit.jsonl  ← append-only trail of every permission request/decision
```

### The four backends (via `memory/unifier.py`)
- **SQLite** is authoritative. Everything durable is written here first. If any other
  layer is lost or corrupted, SQLite can rebuild it.
- **Chroma** is a *rebuildable* semantic index for "recall anything we talked about."
  If it's ever unavailable, memory still works (SQLite keyword fallback) — degraded, not
  broken, and it announces the degradation honestly.
- **diskcache** holds short-TTL caches (e.g. search results) so fresh writes are
  recallable immediately.
- **JSON audit** is an append-only record of every write — a durable, human-readable
  ledger independent of the databases.

### How it behaves
- **Schema versioning & migrations.** SQLite carries a `schema_version`; migrations run
  automatically and in order on boot (currently at **v4**). Your existing database
  upgrades in place — data is never lost.
- **Knowledge graph.** Typed edges between people/projects/topics/anything, with
  shortest-path reasoning, bounded subgraphs, and automatic shared-neighbor discovery.
- **Provenance.** Every fact stores `source`, `evidence`, `verification_status`, and
  `last_confirmed_at`. Recall hedges on assumptions even when they score highly — Nova
  will not state an inference as a fact.
- **Decay & reinforcement.** Old free-form memories fade gently in ranking (never
  deleted); re-mentioning something reinforces it. Identity facts and lessons don't decay.
- **World model** (general knowledge) is kept *separate* from your personal graph, so
  "who's connected to Marcus" never returns "Python is a programming language."
- **Private thoughts** persist across sessions and are surfaced only on request.

You can inspect and manage all of this from the desktop app's **Memory** and **Knowledge
Graph** panels, or via the `/memory/*` endpoints.

---

## Setup (Windows, PowerShell)

### 1) Python venv (3.11)

```powershell
py -3.11 -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Notes:
- `requirements.txt` pins the CUDA package indexes (cu124) and the validated
  `llama-cpp-python==0.3.23` GPU build. A CPU-only install fails Nova's startup on purpose.
- `TTS==0.22.0` (Coqui XTTS) needs FFmpeg on PATH (or set `NOVA_FFMPEG_PATH`).

### 2) Frontend

```powershell
cd frontend
npm install
```

### 3) Configure `.env`

```powershell
Copy-Item .env.example .env
```

Fill in only what you use — every integration is optional and shows "not configured"
if absent: `OPENWEATHER_API_KEY`, `GOOGLE_MAPS_API_KEY`, `DISCORD_BOT_TOKEN`,
`DISCORD_CHANNEL_ID`, and `GOOGLE_OAUTH_CLIENT_ID`/`GOOGLE_OAUTH_CLIENT_SECRET`
(see `tools/README_google_oauth.md`). **Never commit `.env`.**

### 4) Model

Place any `*.gguf` under `model\`. Nova auto-picks the most recently modified one. For
vision, keep a matching `mmproj-*.gguf` next to it (auto-detected).

---

## Running Nova (boot)

Everything at once (backend + Vite + Electron):

```powershell
.\start_nova.ps1
```

Or individually:

```powershell
# Backend only
.\venv\Scripts\python.exe -m uvicorn backend.app:app --host 127.0.0.1 --port 8008

# Frontend dev server + Electron
cd frontend; npm run electron:dev
```

Successful startup indicators:

```
• Model loaded: <your-model>.gguf (ctx=8192)
• Vision model loaded: mmproj-*.gguf
• Startup complete: gpu_offload_confirmed
```

### GPU enforcement
Nova loads the model with `n_gpu_layers=-1` and parses llama.cpp logs to confirm CUDA
offload. If offload isn't confirmed, startup **fails** with install guidance — there is
no silent CPU fallback. XTTS likewise requires CUDA (`NOVA_TTS_DEVICE=cuda`).

If offload fails, reinstall the CUDA build:

```powershell
pip uninstall llama-cpp-python
pip install -r requirements.txt --force-reinstall --no-cache-dir
```

---

## Configuration & feature flags

Every setting is a `NOVA_*` environment variable, cataloged and boot-validated in
`core/settings.py` (a typo is flagged at startup with a "did you mean" hint). Highlights
of the Next-Gen feature flags (all default **on** except where noted):

| Flag | What it controls |
| --- | --- |
| `NOVA_MEMORY_PROVENANCE` | Record source/verification on every fact (#19) |
| `NOVA_INTERNAL_STATE` | Derive internal operational metrics that advise replies (#12) |
| `NOVA_SELF_BENCHMARK` | Daily self-eval trends + regression flags (#14) |
| `NOVA_WORLD_MODEL` | Maintain the semantic world model (#11) |
| `NOVA_INTERNAL_THOUGHTS` | Persistent private thoughts across sessions (#6) |
| `NOVA_DIGITAL_TWIN` | Model your working patterns (#4) |
| `NOVA_EXECUTIVE` | Proactive, confidence-gated recommendations (#1) |
| `NOVA_AGENT_SOCIETY` | The council of durable specialists (#5) |
| `NOVA_RESEARCH` | Autonomous research worker (#9) — **off by default** (makes network calls) |
| `NOVA_EXPERIMENTS` | Safe A/B experimentation, recommend-only (#15) |
| `NOVA_PERMISSION_MODE` | Actuator policy: `locked` / `guarded` (default) / `trusted` |
| `NOVA_COMPUTER_CONTROL` | Allow computer actions to actually execute — **off by default** (dry-run otherwise) |
| `NOVA_DEV_MODE` | Guarded self-editing — **off by default** |

See `.env.example` for the full, commented list.

---

## Security & permissions

- **API auth.** Set `NOVA_API_TOKEN` to require `Authorization: Bearer <token>` on every
  endpoint (the UI picks it up automatically; `/health` stays open). Unset = localhost-only
  with a boot warning.
- **Secrets** live only in `.env` and `credentials/` — both git-ignored and on Nova's
  self-editing deny-list, so she can never read or modify them.
- **Permission tiers** gate every actuator (computer control, learned-skill execution).
  Admin/critical actions can never be auto-allowed — only confirmed or denied — and every
  request is written to a durable audit trail (`/permissions/audit`).
- **Gmail is draft-only by design** — the `gmail.send` scope is never requested, so it is
  structurally impossible for Nova to send email on your behalf.

---

## Key API endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Liveness + GPU enforcement state (open even with auth on) |
| `GET /status` | Full status: model, real GPU telemetry (nvidia-smi), subsystems, integrations |
| `WS /ws/events` | Structured live events (thinking, tools, memory, vision, web, TTS/STT) |
| `POST /chat`, `POST /chat/stream` | Chat (JSON / SSE stream with optional TTS) |
| `POST /stt`, `POST /speak` | Whisper transcription / XTTS synthesis |
| `POST /vision/analyze` | Image analysis via the mmproj vision model |
| `GET /memory/recent`, `/memory/search` | Memory panel data |
| `GET /memory/graph`, `/memory/graph/subgraph`, `/memory/path` | Knowledge graph + path reasoning |
| `GET /memory/world`, `/thoughts`, `/twin`, `/executive` | World model, private thoughts, digital twin, executive recommendations |
| `GET /agents`, `/skills`, `/experiments` | Agent society, learned skills, experiments |
| `GET /code/index`, `/code/health`, `/code/security` | Codebase understanding + reports |
| `GET /permissions/audit`, `POST /permissions/resolve` | Permission audit trail + approvals |
| `GET /reminders`, `/goals`, `/tasks`, `/autonomy/*` | Scheduling, goals, self-eval metrics |
| `GET /dev/status`, `/dev/proposals`, `/dev/apply` | Guarded developer mode |

---

## Tests

```powershell
.\run_tests.ps1            # all offline suites in tests/
.\run_tests.ps1 memory     # filtered by name
cd frontend; npm run build # frontend build check
```

The offline suite is deterministic and self-verifying (e.g. the config catalog test
fails if any `NOVA_*` var is used without being cataloged, and migration tests prove your
database upgrades cleanly).

---

## Roadmap: what we're adding

Nova is mid-transformation from an assistant into a **local-first AI operating system**.
The full 20-goal plan lives in `docs/ROADMAP_NG.md` (build order and rationale) alongside
`docs/ARCHITECTURE.md` and `docs/ROADMAP.md` (the original Phases 0–2 foundation).

**Shipped** — Foundation (auth, config, migrations, router split), Living Memory,
Orchestrator, and Next-Gen Phases 3.5–8:
- Cognitive instrumentation (provenance, internal state, self-benchmarking)
- Deep memory (knowledge graph 2.0, world model, internal thoughts)
- Executive intelligence & planning (digital twin, executive, planner, research)
- The persistent agent society
- Autonomous software engineering (codebase understanding, SW-eng reports, experimentation)
- Computer control & skill learning (permissions-first)

**Planned next** — to make Nova more unique, proactive, and robust:
- **Awareness substrate & always-on** — live WorldState + hardware/system monitoring
  (temps, VRAM, SMART, anomalies), a system-tray resident with native notifications,
  backup/restore, and Jellyfin media. This unlocks the Executive layer's *proactive push*
  (it can nudge you even when the app is in the tray) and gives experimentation real
  resource metrics.
- **Physical world** — a universal smart-home abstraction (lights/locks/thermostats/
  sensors, context-aware rules) and **episodic vision memory** ("what changed in my
  office", "where did I leave my headphones", "when did that package arrive").
- **Nova OS unification** — every subsystem behind one cohesive intelligence so you talk
  to *Nova*, not a drawer of disconnected tools.

Longer-horizon hardware unlocks: a second GPU brings local image/video generation online
and lets the agent society deliberate with genuine concurrency (the model router already
makes adding a second model a config change, not a rewrite).

---

## Honest limitations (gates)

Nova tells you the truth about what she can't yet do, and so does this README:
- **Calendar/Email** need a one-time Google OAuth setup (`tools/README_google_oauth.md`)
  before they return real data; until then they report "not connected."
- **Image/video generation** is fully built but inert until a second GPU (RTX 3080) is
  installed — it honestly reports "not available."
- **Computer control** ships as a permission-gated, dry-run framework: it does **not**
  synthesize real input until you explicitly enable it *and* install a platform adapter.
- **Always-on tray, hardware monitoring, and smart home** are the next phases — not built
  yet; the Executive layer currently surfaces recommendations in-app rather than as
  background notifications.
- **Standalone packaging** isn't wired yet (`package.json`'s `dist:win:*` scripts expect a
  PyInstaller build that ships with the always-on phase). Run from source with
  `start_nova.ps1` for now.

---

*Nova is a personal project by Marcus Deleon. Local-first, private by default, honest by
design.*
