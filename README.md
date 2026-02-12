# Nova (Backend)

Python 3.11 async backend for an Electron frontend.

## Setup (Windows, PowerShell)

### 1) Create venv

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 2) Install dependencies

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

> Notes
> - `llama-cpp-python==0.3.4` must be a CUDA-enabled build to enforce RTX 3080 GPU offload.
> - `TTS==0.22.0` (Coqui) is required for XTTS. On Windows you may also need FFmpeg.

### 3) Configure `.env`

Create a `.env` in the repo root.

Required keys (as needed by features):

- `NOVA_LOG_LEVEL=INFO`
- `OPENWEATHER_API_KEY=...` (Weather plugin)
- `GOOGLE_MAPS_API_KEY=...` (Google Maps plugin)
- `DISCORD_BOT_TOKEN=...` and `DISCORD_CHANNEL_ID=...` (Discord plugin)

Optional:

- `NOVA_HOST=127.0.0.1`
- `NOVA_PORT=8008`
- `NOVA_MODEL_PATH=` (leave empty to auto-pick any `model/*.gguf`)
- `NOVA_CONTEXT_TOKENS=8192`
- `NOVA_MEMORY_DIR=memory_data`

### 4) Place a GGUF model

Put a `.gguf` file under `model/`.

Nova will auto-detect it. If no model exists, `/chat` will still work but will return a clear "model missing" response and tests will skip inference.

### 5) Start Nova (backend + frontend)

```powershell
.\start_nova.ps1
```

If a frontend exists, the script will attempt to run its npm start script.

### 6) Run tests

```powershell
python -m pytest
python scripts\boot_test.py
```

## GPU enforcement (llama-cpp)

When a model is present, Nova loads it with `n_gpu_layers=-1` and `main_gpu=0` and captures llama.cpp initialization logs.

- If CUDA offload is confirmed: startup succeeds and `/health` reports GPU enforcement as active.
- If CUDA offload cannot be confirmed: startup fails fast with a clear error explaining how to install a CUDA-enabled `llama-cpp-python==0.3.4` build on Windows.

This prevents silent CPU fallback for inference.
