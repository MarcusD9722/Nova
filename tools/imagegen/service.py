from __future__ import annotations

"""Nova's local image/video generation microservice.

Runs in its OWN isolated venv (see tools/imagegen/README.md), separate from
Nova's main backend — mirrors the pattern already used for the Blender avatar
pipeline (tools/avatar/bpyenv). Reasons it's a separate process rather than
running in-process in backend/app.py:

  1. Hard GPU isolation. Nova's LLM is GPU-ONLY on the RTX 5080 (device 0) —
     it must never share or contend with that card. CUDA_VISIBLE_DEVICES is
     set to "1" (the second GPU) BEFORE torch is ever imported, in a process
     that has nothing else running on it, which is the only fully reliable
     way to guarantee a diffusion model can never land on device 0.
  2. Dependency isolation. torch+diffusers is a large, separate dependency
     stack from llama-cpp-python; keeping it out of Nova's main venv keeps
     the core assistant lean and avoids version conflicts.

Honest by design: at startup this only reports GPU status — it does NOT
download or load any model until the first real /generate/image call, so
running this service before the second GPU is installed costs nothing and
claims nothing. /health always reflects the ACTUAL current state.
"""

import base64
import io
import os
import time

# Must happen before `import torch` — this is what makes "cuda:0" *inside this
# process* actually mean the physical second GPU, regardless of what Nova's
# main backend process is doing on its own device 0.
os.environ.setdefault("CUDA_VISIBLE_DEVICES", os.getenv("NOVA_IMAGEGEN_CUDA_DEVICE", "1"))

from fastapi import FastAPI, HTTPException  # noqa: E402
from pydantic import BaseModel  # noqa: E402

app = FastAPI(title="Nova Image/Video Generation Service")

_MODEL_ID = os.getenv("NOVA_IMAGEGEN_MODEL", "stabilityai/sdxl-turbo")
_pipeline = None  # lazy-loaded on first real generate call


def _gpu_status() -> dict:
    """Report the truth about this process's GPU visibility. Never guesses."""
    try:
        import torch
    except ImportError:
        return {"torch_installed": False, "gpu_available": False, "device_name": None}
    try:
        available = bool(torch.cuda.is_available()) and torch.cuda.device_count() > 0
    except Exception:
        available = False
    name = None
    if available:
        try:
            name = torch.cuda.get_device_name(0)  # remapped by CUDA_VISIBLE_DEVICES
        except Exception:
            name = None
    return {"torch_installed": True, "gpu_available": available, "device_name": name}


@app.get("/health")
async def health() -> dict:
    gpu = _gpu_status()
    return {
        "ok": True,
        "gpu_available": gpu["gpu_available"],
        "device_name": gpu["device_name"],
        "torch_installed": gpu["torch_installed"],
        "model_loaded": _pipeline is not None,
        "model_id": _MODEL_ID,
    }


def _ensure_pipeline():
    global _pipeline
    if _pipeline is not None:
        return _pipeline
    gpu = _gpu_status()
    if not gpu["torch_installed"]:
        raise RuntimeError("torch is not installed in this service's venv — see tools/imagegen/README.md")
    if not gpu["gpu_available"]:
        raise RuntimeError(
            "No GPU visible to the image-gen service (looking for the second GPU as "
            f"CUDA device {os.environ.get('CUDA_VISIBLE_DEVICES')}). Install/seat the second GPU "
            "and restart this service."
        )
    import torch
    from diffusers import AutoPipelineForText2Image

    # Downloads/caches the model on first real use — not at service startup,
    # so simply having this service running costs nothing until it's asked
    # to actually generate something.
    pipe = AutoPipelineForText2Image.from_pretrained(
        _MODEL_ID, torch_dtype=torch.float16, variant="fp16"
    )
    pipe = pipe.to("cuda:0")  # remapped to the physical 2nd GPU by CUDA_VISIBLE_DEVICES
    _pipeline = pipe
    return _pipeline


class ImageGenRequest(BaseModel):
    prompt: str
    width: int = 768
    height: int = 768
    steps: int = 4  # sdxl-turbo is a distilled few-step model by design


@app.post("/generate/image")
async def generate_image(req: ImageGenRequest) -> dict:
    prompt = (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=422, detail="prompt is required")
    try:
        pipe = _ensure_pipeline()
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    width = max(256, min(int(req.width or 768), 1024))
    height = max(256, min(int(req.height or 768), 1024))
    steps = max(1, min(int(req.steps or 4), 12))

    t0 = time.time()
    result = pipe(prompt=prompt, width=width, height=height, num_inference_steps=steps, guidance_scale=0.0)
    image = result.images[0]
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    data_url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    return {"ok": True, "image_data_url": data_url, "prompt": prompt, "seconds": round(time.time() - t0, 2)}


class VideoGenRequest(BaseModel):
    prompt: str
    seconds: float = 2.0


@app.post("/generate/video")
async def generate_video(req: VideoGenRequest) -> dict:
    # Deliberately not implemented yet — per the approved plan, video is a
    # second pass once image generation is proven on real hardware. This is
    # an honest "not built" response, not a fake success or a silent no-op.
    raise HTTPException(
        status_code=501,
        detail="Video generation isn't built yet — image generation ships first. Ask for an image instead.",
    )
