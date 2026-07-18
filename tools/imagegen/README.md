# Nova Image/Video Generation Service

A small, standalone FastAPI service that gives Nova local image generation,
pinned to a **second GPU** so it never competes with the RTX 5080 running her
language model. Runs in its own isolated Python environment — same pattern as
`tools/avatar`'s Blender pipeline — because it needs `torch`/`diffusers`,
which are deliberately kept out of Nova's main backend venv.

## Requirements

- A second CUDA GPU installed in this machine (in addition to the RTX 5080).
  Marcus's setup: an RTX 3080 not yet installed at the time this was built.
- Until that second GPU is present, this service still runs and reports
  itself honestly as unavailable (`GET /health` → `gpu_available: false`) —
  Nova will tell you plainly rather than pretending it can generate.

## Setup (one-time, once the second GPU is installed)

```powershell
cd tools\imagegen
python -m venv venv
venv\Scripts\pip install -r requirements.txt
```

`torch` here should be a CUDA build matching your second GPU's driver — if
`pip install torch` doesn't pick up CUDA automatically, follow
https://pytorch.org/get-started/locally/ for the right index URL.

## Running

```powershell
tools\imagegen\venv\Scripts\python.exe -m uvicorn tools.imagegen.service:app --host 127.0.0.1 --port 8801
```

(Or set `NOVA_IMAGEGEN_PORT` in Nova's `.env` to match if you use a different
port — see `plugins are wired via core/tooling.py::_image_generate`.)

This is a **separate process you start manually** — Nova's main backend does
not auto-launch it, the same way the avatar Blender pipeline isn't auto-run.
Nova's `image.generate` tool checks `/health` before every request and gives
an honest "service isn't running" or "second GPU not detected" message if
either is true — no fake success, no silent fallback onto the main GPU.

## What's implemented

- **Images**: yes — `POST /generate/image` using `stabilityai/sdxl-turbo` by
  default (fast, few-step, good fit for a single mid-range GPU). Override the
  model via `NOVA_IMAGEGEN_MODEL`.
- **Video**: not yet. `POST /generate/video` returns an honest 501 — this was
  intentionally scoped as a follow-up once image generation is proven on real
  hardware (local single-GPU video generation is heavier and slower; better
  to get images right first).

## Safety

`CUDA_VISIBLE_DEVICES` is set (default `"1"`, override with
`NOVA_IMAGEGEN_CUDA_DEVICE`) **before `torch` is imported**, which is the only
fully reliable way to guarantee this process can never see or touch GPU 0 —
the one running Nova's LLM — even if code inside this service asks for
`"cuda:0"` (which, inside this process, is remapped to the physical second
card).
