Nova — Local AI Assistant Backend

• Local-first AI assistant backend built in Python 3.11
• Designed to power the Nova desktop app
• FastAPI async API for Electron frontend
• Local GGUF model inference via llama-cpp with GPU offload
• Persistent multi-layer memory
• Plugin system and voice pipeline support
• README reflects current Nova boot and setup behavior

---

Requirements

• Windows (primary supported platform)
• Python 3.11
• NVIDIA GPU with CUDA
• PowerShell
• FFmpeg recommended for voice features
• Node.js only if running the frontend

---

Expected Repo Layout (do not restructure)

• backend/
• core/
• memory/
• plugins/
• voice/
• model/
• memory_data/
• projects/
• start_nova.ps1
• requirements.txt
• .env

Folder roles:

• backend/ → FastAPI entry package
• core/ → brain and orchestration
• memory/ → memory engines and unifier
• plugins/ → all tools and integrations
• voice/ → TTS / STT pipeline
• model/ → GGUF model files
• memory_data/ → runtime memory storage (gitignored)
• projects/ → boot tests and utility runners

---

Setup (Windows / PowerShell)

• Create virtual environment
py -3.11 -m venv .venv
..venv\Scripts\Activate.ps1

• Install dependencies
python -m pip install --upgrade pip
pip install -r requirements.txt

Critical packages:

• llama-cpp-python==0.3.4 (CUDA-enabled build required)
• TTS==0.22.0 (Coqui XTTS)

---

.env configuration (repo root)

Core settings:

• NOVA_LOG_LEVEL=INFO
• NOVA_HOST=127.0.0.1
• NOVA_PORT=8000
• NOVA_CONTEXT_TOKENS=8192
• NOVA_MEMORY_DIR=memory_data

Optional plugin keys (only if those plugins are used):

• OPENWEATHER_API_KEY=
• GOOGLE_MAPS_API_KEY=
• DISCORD_BOT_TOKEN=
• DISCORD_CHANNEL_ID=

---

Model setup

• Place at least one GGUF file inside model/
• Example: model/llama-3.1-8b-instruct-q6_k.gguf
• If no model exists, server still boots
• /chat returns a model-missing response

---

Boot Nova

Recommended:

• Run start script
.\start_nova.ps1

Script behavior:

• Activates virtual environment
• Starts backend server
• Loads model
• Initializes memory layers
• Registers plugins
• Starts frontend if present

Manual backend boot:

• python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000

---

Successful startup indicators

• Model loaded: llama-3.1-8b-instruct-q6_k.gguf (ctx=8192)
• Startup complete: gpu_offload_confirmed

---

GPU enforcement (llama-cpp)

• Model loads with full GPU layer offload
• n_gpu_layers = -1
• main_gpu = 0
• If CUDA offload is not confirmed, startup errors

---

Plugins

• All tools must live in plugins/
• Plugins auto-register at startup
• Missing API keys disable only that plugin, not Nova

---

Voice system

• Voice modules live in voice/
• Uses Coqui TTS
• FFmpeg required for audio processing

---

Memory system

• Runtime memory stored in memory_data/
• Do not commit this folder

---

Tests

• python -m pytest
• python projects\boot_test.py

---

Gitignore these

• .env
• memory_data/
• **pycache**/
• *.gguf

---

Troubleshooting GPU

• Reinstall llama-cpp with CUDA build
pip uninstall llama-cpp-python
pip install llama-cpp-python==0.3.4 --force-reinstall --no-cache-dir
