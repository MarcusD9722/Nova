from __future__ import annotations

import asyncio
from pathlib import Path

import aiofiles

from core.safety import ensure_within_any_root


class CodeOps:
    def __init__(self, repo_root: Path, extra_allowed_roots: list[Path] | None = None):
        repo = repo_root.resolve()
        extras = [p.resolve() for p in (extra_allowed_roots or [])]
        # Always allow repo root; optionally allow additional roots (e.g. projects dir).
        self._allowed_roots = [repo, *extras]
        self._lock = asyncio.Lock()

    async def read_text(self, path: Path, encoding: str = "utf-8") -> str:
        safe = ensure_within_any_root(self._allowed_roots, path)
        async with aiofiles.open(safe, "r", encoding=encoding) as f:
            return await f.read()

    async def write_text(self, path: Path, content: str, encoding: str = "utf-8") -> None:
        safe = ensure_within_any_root(self._allowed_roots, path)
        safe.parent.mkdir(parents=True, exist_ok=True)
        async with self._lock:
            async with aiofiles.open(safe, "w", encoding=encoding) as f:
                await f.write(content)

    async def apply_patch_atomic(self, path: Path, new_content: str, encoding: str = "utf-8") -> None:
        safe = ensure_within_any_root(self._allowed_roots, path)
        tmp = safe.with_suffix(safe.suffix + ".tmp")
        safe.parent.mkdir(parents=True, exist_ok=True)
        async with self._lock:
            async with aiofiles.open(tmp, "w", encoding=encoding) as f:
                await f.write(new_content)
            await asyncio.to_thread(tmp.replace, safe)
