# Nova

Local, GPU-only AI assistant: FastAPI backend + React/Vite/Electron desktop UI.

- Local LLM chat (llama-cpp-python, CUDA offload enforced — no CPU fallback)
- Vision (Qwen2-VL + mmproj), XTTS voice, Whisper STT, "Hey Nova" wake word
- Unified memory (SQLite + Chroma + diskcache + JSON audit)
- Tools: weather, Google Maps, Discord, web search, project scaffolding
- Live event stream (`/ws/events`) driving the UI's activity states
- Guarded Developer Mode (off by default) for self-inspection with diff approval

## Setup (Windows, PowerShell)

### 1) Python venv (3.11)

```powershell
py -3.11 -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Notes:
- `requirements.txt` includes the CUDA package indexes (cu124) and the validated
  `llama-cpp-python==0.3.23` GPU build. A CPU-only install will fail Nova's startup on purpose.
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

Then fill in what you use (all optional — features show "not configured" if missing):
`OPENWEATHER_API_KEY`, `GOOGLE_MAPS_API_KEY`, `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`.
Never commit `.env`.

### 4) Model

Place any `*.gguf` under `model\`. Nova auto-picks the most recently modified one.
For vision, keep a matching `mmproj-*.gguf` next to it (auto-detected).

## Run Nova

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

## Checks & tests

```powershell
.\venv\Scripts\python.exe scripts\boot_test.py   # import/config/plugin sanity
.\venv\Scripts\python.exe -m pytest tests\ -q    # smoke tests (no GPU needed)
cd frontend; npm run build                       # frontend build check
```

## GPU enforcement

Nova loads the model with `n_gpu_layers=-1` and parses llama.cpp logs to confirm
CUDA offload. If offload is not confirmed, startup **fails** with install guidance —
there is no silent CPU fallback. XTTS likewise requires CUDA (`NOVA_TTS_DEVICE=cuda`).

## Key endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Liveness + GPU enforcement state |
| `GET /status` | Full status: model, real GPU telemetry (nvidia-smi), subsystems, integrations |
| `WS /ws/events` | Structured live events (thinking, tools, memory, vision, web, TTS/STT) |
| `POST /chat`, `POST /chat/stream` | Chat (JSON / SSE stream with optional TTS) |
| `POST /stt`, `POST /speak` | Whisper transcription / XTTS synthesis |
| `POST /vision/analyze` | Image analysis via mmproj vision model |
| `GET /memory/recent`, `GET /memory/search` | Memory panel data |
| `GET /tasks` | Background autonomy tasks |
| `GET /dev/status`, `/dev/inspect`, `/dev/propose`, `/dev/proposals`, `/dev/apply` | Developer mode (guarded) |

## Developer Mode (guarded self-editing)

Disabled by default. Set `NOVA_DEV_MODE=1` in `.env` to enable.

- Read-only inspection of the repo (never `.env`, `.git`, `credentials`, `memory_data`, `model`, `venv`, `node_modules`)
- Changes flow through **propose → review real diff in the Improve panel → approve**;
  applies are backed up, boot-tested in a subprocess, and auto-rolled-back on failure
- Registered external projects get the same propose/approve/backup/rollback guard
  (syntax-checked but not boot-tested — surfaced honestly as `skipped_external_project`)
- Proposals and backups persist under `.nova_dev/`; see `core/dev_mode.py`

## Security

Set `NOVA_API_TOKEN` in `.env` to require `Authorization: Bearer <token>` on every
endpoint (the UI picks it up automatically; `/health` stays open). Unset = localhost-only
use, with a boot warning. See `.env.example`.

## Tests

```powershell
.\run_tests.ps1            # all suites in tests/
.\run_tests.ps1 memory     # filtered
```

## Architecture & roadmap

Nova is mid-transformation into a local-first AI operating layer. The audit, target
architecture, invariants, and the phased plan live in:

- `docs/ARCHITECTURE.md` — current state (measured), debt register, target design
- `docs/ROADMAP.md` — Phases 0–6, dependencies, open decisions
- `docs/CONTRIBUTING.md` — the one-writer rule and the verification ritual

## Packaging (not yet implemented — Phase 3.4)

`package.json` contains `dist:win:*` scripts that expect a PyInstaller build script
(`build_backend.ps1`) which **does not exist yet**. Run Nova from source with
`start_nova.ps1` for now; a standalone build ships with Phase 3 (always-on).
