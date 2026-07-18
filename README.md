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

## Developer Mode (self-inspection)

Disabled by default. Set `NOVA_DEV_MODE=1` in `.env` to enable.

- Read-only inspection of the repo (never `.env`, `.git`, `memory_data`, `model`, `venv`, `node_modules`)
- Changes flow through **propose → review diff → apply with `confirm=true`**; proposing never writes
- See `core/dev_mode.py` for the full safety model and open TODOs
  (proposal persistence, auto-test after apply, UI diff viewer)

## Packaging (not yet implemented)

`package.json` contains `dist:win:*` scripts that expect a PyInstaller build script
(`build_backend.ps1`) which **does not exist yet** — building a standalone .exe is a TODO.
Run Nova from source with `start_nova.ps1` for now.
