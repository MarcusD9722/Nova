from __future__ import annotations

"""One GPU, one consumer at a time — within this process.

Nova used to run three independent CUDA consumers inside a single process:

  * llama.cpp (the 9B, via llama-cpp-python)
  * XTTS      (torch, voice synthesis)      <- NO LONGER IN THIS PROCESS
  * bge-small (torch, memory embeddings)

XTTS moved out in the JARVIS V2 round. It now runs in a child process with its
own CUDA context (services/tts_worker.py), because it was the one consumer that
could not take this semaphore: sentence-streamed TTS overlaps synthesis with
generation BY DESIGN, and the reply stream holds the permit for the whole
generation, so waiting for it here deadlocked (measured: a 195 s hang, then
access violations on every later turn). Same card, separate context, driver
time-slicing.

The evidence below is why that was necessary, and is kept verbatim.

Nothing coordinated them. llama.cpp's calls were serialized against each other
by RuntimeManager's semaphore, but the torch models allocated and ran on the
same device whenever they liked. The result, captured live on Marcus's machine:

    embedding_model_loaded  device=cuda   model=BAAI/bge-small-en-v1.5
    GPT2InferenceModel ...                          <- XTTS synthesizing
    CUDA error: an illegal memory access was encountered
      current device: 0, in function ggml_backend_cuda_synchronize
      ggml-cuda.cu:3235  cudaStreamSynchronize(cuda_ctx->stream())
    ggml-cuda.cu:102: CUDA error                    <- process aborts

The backend dies outright — no traceback in the app, no Windows error report,
just gone mid-conversation. It reproduced twice in ten minutes of ordinary
speaking turns, and was the "unexplained CUDA crash" carried in
docs/AUDIT_2026-08-03.md since the U10 boot check.

Proven by elimination on the live machine:
  * embeddings moved to CPU, XTTS still on CUDA  ->  still crashed
  * XTTS moved to CPU                            ->  8 speaking turns, no crash

So this semaphore is the same fix already applied to the cloud->local fallback
in d1f407e ("re-serialize on the GPU before touching the local model"),
extended to every consumer instead of just llama.cpp.

It is a plain 1-permit semaphore, not a lock, so it composes with the existing
ModelHandle plumbing unchanged. Anything that touches the GPU IN THIS PROCESS
takes it.

Out-of-process consumers (the XTTS worker; tools/imagegen) neither take nor need
this permit — they are isolated by having their own CUDA context, which is a
stronger guarantee than serialisation and is why they were moved out.
"""

import asyncio

#: THE GPU semaphore. One permit, process-wide, for every in-process CUDA
#: consumer. Import this rather than constructing a new one — a second semaphore
#: guarding the same device provides no protection at all.
GPU_SEM = asyncio.Semaphore(1)
