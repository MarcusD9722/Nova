"""Out-of-process helpers for Nova subsystems that must not share a CUDA
context with llama.cpp.

Nothing in here may import `backend.app` — these modules are loaded inside
child processes, where importing the FastAPI app would boot a second copy of
the whole backend.
"""
