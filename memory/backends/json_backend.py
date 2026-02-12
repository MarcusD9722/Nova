from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiofiles
import orjson


class JsonAuditBackend:
    def __init__(self, json_dir: Path):
        self._json_dir = json_dir
        self._audit_path = json_dir / "audit.jsonl"
        self._snapshot_path = json_dir / "snapshots.jsonl"
        self._lock = asyncio.Lock()

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    async def initialize(self) -> None:
        self._json_dir.mkdir(parents=True, exist_ok=True)
        for p in [self._audit_path, self._snapshot_path]:
            if not p.exists():
                async with aiofiles.open(p, "wb") as f:
                    await f.write(b"")

    async def append_audit(self, record: dict[str, Any]) -> None:
        await self.initialize()
        record = dict(record)
        record.setdefault("ts", self._now_iso())
        line = orjson.dumps(record) + b"\n"
        async with self._lock:
            async with aiofiles.open(self._audit_path, "ab") as f:
                await f.write(line)

    async def append_snapshot(self, snapshot: dict[str, Any]) -> None:
        await self.initialize()
        snapshot = dict(snapshot)
        snapshot.setdefault("ts", self._now_iso())
        line = orjson.dumps(snapshot) + b"\n"
        async with self._lock:
            async with aiofiles.open(self._snapshot_path, "ab") as f:
                await f.write(line)
