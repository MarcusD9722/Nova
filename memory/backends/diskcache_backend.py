from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from diskcache import Cache


class DiskCacheBackend:
    def __init__(self, cache_dir: Path):
        self._cache_dir = cache_dir
        self._cache: Cache | None = None
        self._lock = asyncio.Lock()

    def _get_cache(self) -> Cache:
        if self._cache is None:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            self._cache = Cache(directory=str(self._cache_dir))
        return self._cache

    async def set(self, key: str, value: Any, ttl_s: int = 300) -> None:
        async with self._lock:
            await asyncio.to_thread(self._get_cache().set, key, value, expire=ttl_s)

    async def get(self, key: str) -> Any | None:
        async with self._lock:
            return await asyncio.to_thread(self._get_cache().get, key, None)

    

    async def delete(self, key: str) -> bool:
        """Delete a cached key. Returns True if removed."""
        async with self._lock:
            return bool(await asyncio.to_thread(self._get_cache().pop, key, None) is not None)

    async def close(self) -> None:
        if self._cache is not None:
            await asyncio.to_thread(self._cache.close)
            self._cache = None
