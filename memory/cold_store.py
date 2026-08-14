from __future__ import annotations

"""COLD tier: heavy evidence, on disk, content-addressed.

Why the filesystem and not SQLite — decided after reading the backend, not
assumed. Nova's SQLite file is the authoritative store for everything small,
relational and queried: facts, edges, episodes, artifacts, decisions. Cold
evidence is none of those things. It is large, read rarely, never queried by
content, and would sit inside every backup and every VACUUM of a database that
is otherwise a few megabytes. Content addressing also means the same 200 KB tool
result captured in three conversations costs one copy.

The invariant that matters more than the storage choice:

    **A missing or corrupt cold payload must never break memory.**

Warm records are self-sufficient — summary, entities, provenance, trust,
freshness all live in SQLite. Cold evidence is an optional enrichment. Every
read here returns None rather than raising, and the caller is expected to carry
on with the warm record it already has.
"""

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from core.logging_setup import get_logger

logger = get_logger(__name__)

#: Refuse to store anything larger than this. A tool that produces 500 MB is a
#: bug or an attack, and swallowing it silently would turn Nova's memory
#: directory into a disk-space incident.
MAX_PAYLOAD_BYTES = int(os.getenv("NOVA_COLD_MAX_BYTES", str(8 * 1024 * 1024)))


class ColdStore:
    """Content-addressed blobs under <memory_dir>/cold/."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root) / "cold"
        self._writes = 0
        self._dedupes = 0
        self._misses = 0

    def _path_for(self, digest: str) -> Path:
        # Two-level fan-out: a flat directory with 100k files is miserable on
        # Windows and slow to list anywhere.
        return self.root / digest[:2] / digest[2:4] / digest

    def put(self, payload: Any, *, content_type: str = "application/json") -> dict[str, Any] | None:
        """Store evidence. Returns a reference dict, or None if it was refused.

        The reference is what goes in the warm record: digest, size and type —
        enough to describe the evidence without loading it.
        """
        try:
            if isinstance(payload, (dict, list)):
                data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            elif isinstance(payload, bytes):
                data = payload
            else:
                data = str(payload).encode("utf-8")
        except Exception as e:  # noqa: BLE001
            logger.warning("cold_store_serialize_failed", error=str(e)[:200])
            return None

        if len(data) > MAX_PAYLOAD_BYTES:
            logger.warning("cold_store_payload_too_large", bytes=len(data),
                           limit=MAX_PAYLOAD_BYTES)
            return None

        digest = hashlib.sha256(data).hexdigest()
        path = self._path_for(digest)
        try:
            if path.exists():
                self._dedupes += 1
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                # Write to a temp file and rename: a crash mid-write must not
                # leave a truncated blob under a digest that claims to describe
                # the full content.
                tmp = path.with_suffix(".part")
                tmp.write_bytes(data)
                tmp.replace(path)
                self._writes += 1
        except OSError as e:
            logger.warning("cold_store_write_failed", error=str(e)[:200])
            return None

        return {"digest": digest, "bytes": len(data), "content_type": content_type}

    def get(self, digest: str | None) -> Any | None:
        """Hydrate evidence. None when absent, unreadable or corrupt — never raises.

        Corruption is detected, not assumed away: the digest IS the content
        hash, so a mismatch means the bytes on disk are not what the warm record
        refers to, and returning them would be worse than returning nothing.
        """
        if not digest:
            return None
        path = self._path_for(str(digest))
        try:
            if not path.exists():
                self._misses += 1
                return None
            data = path.read_bytes()
        except OSError as e:
            self._misses += 1
            logger.warning("cold_store_read_failed", digest=str(digest)[:12], error=str(e)[:200])
            return None

        if hashlib.sha256(data).hexdigest() != digest:
            self._misses += 1
            logger.warning("cold_store_corrupt", digest=str(digest)[:12])
            return None

        text = data.decode("utf-8", errors="replace")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def exists(self, digest: str | None) -> bool:
        return bool(digest) and self._path_for(str(digest)).exists()

    def delete(self, digest: str) -> bool:
        try:
            p = self._path_for(digest)
            if p.exists():
                p.unlink()
                return True
        except OSError:
            pass
        return False

    def stats(self) -> dict[str, Any]:
        return {"writes": self._writes, "dedupes": self._dedupes, "misses": self._misses,
                "root": str(self.root)}
