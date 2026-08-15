from __future__ import annotations

"""Durable enrolled speaker profiles.

Stored in Nova's existing SQLite database, for the reason D5 already gave for
episodes: it is the authoritative structured store, and a second database would
be another thing to keep consistent, back up and migrate for no measured gain.

WHAT IS NOT STORED
------------------
Raw enrollment audio. By default the samples are used to compute embeddings and
then discarded — a voice recording is among the most personal things Nova could
hold, and P5 does not need it after enrollment. `NOVA_SPEAKER_KEEP_AUDIO=1`
exists for someone deliberately building a calibration set, and is off.

Deleting a profile removes its embeddings. That is the whole point of a delete.

MODEL COMPATIBILITY
-------------------
Every profile records the model id, revision and embedding dimension that
produced it. Embeddings from different models do not share a vector space, so
comparing them is not "slightly less accurate" — it is meaningless, and it would
be meaningless *confidently*, which is worse. A profile whose model no longer
matches is reported `needs_reenrollment` and is never matched against.
"""

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import numpy as np

import aiosqlite
from contextlib import asynccontextmanager

from core.logging_setup import get_logger
from core.speaker.backend import EMBEDDING_DIM, MODEL_ID, MODEL_REVISION

logger = get_logger(__name__)

SPEAKER_DDL: list[str] = [
    """CREATE TABLE IF NOT EXISTS speaker_profiles (
        profile_id TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'guest',
        model_id TEXT NOT NULL,
        model_revision TEXT NOT NULL,
        embedding_dim INTEGER NOT NULL,
        centroid TEXT NOT NULL,
        samples TEXT NOT NULL DEFAULT '[]',
        sample_count INTEGER NOT NULL DEFAULT 0,
        consistency REAL,
        threshold REAL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL);""",
    "CREATE INDEX IF NOT EXISTS idx_speaker_name ON speaker_profiles(display_name);",
]


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def keep_audio() -> bool:
    return os.getenv("NOVA_SPEAKER_KEEP_AUDIO", "0").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class SpeakerProfile:
    profile_id: str
    display_name: str
    role: str = "guest"
    model_id: str = MODEL_ID
    model_revision: str = MODEL_REVISION
    embedding_dim: int = EMBEDDING_DIM
    centroid: np.ndarray | None = None
    #: The individual enrollment embeddings. Kept so a future recalibration can
    #: recompute the centroid without asking Marcus to record six samples again.
    samples: list[np.ndarray] = field(default_factory=list)
    sample_count: int = 0
    consistency: float | None = None
    threshold: float | None = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    @property
    def compatible(self) -> bool:
        """Can this profile be compared against embeddings from THIS build?"""
        return (self.model_id == MODEL_ID
                and self.model_revision == MODEL_REVISION
                and self.embedding_dim == EMBEDDING_DIM
                and self.centroid is not None
                and self.centroid.size == EMBEDDING_DIM)

    @property
    def status(self) -> str:
        return "ready" if self.compatible else "needs_reenrollment"

    def describe(self) -> dict[str, Any]:
        """Metadata only — never the embedding itself."""
        return {
            "profile_id": self.profile_id,
            "display_name": self.display_name,
            "role": self.role,
            "status": self.status,
            "model_id": self.model_id,
            "model_revision": self.model_revision[:12],
            "embedding_dim": self.embedding_dim,
            "sample_count": self.sample_count,
            "consistency": round(self.consistency, 4) if self.consistency is not None else None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, row) -> "SpeakerProfile":
        def _vec(raw):
            try:
                return np.asarray(json.loads(raw), dtype=np.float32)
            except Exception:  # noqa: BLE001
                return None

        centroid = _vec(row["centroid"])
        try:
            samples = [np.asarray(s, dtype=np.float32) for s in json.loads(row["samples"] or "[]")]
        except Exception:  # noqa: BLE001
            samples = []
        return cls(
            profile_id=row["profile_id"], display_name=row["display_name"],
            role=row["role"], model_id=row["model_id"],
            model_revision=row["model_revision"],
            embedding_dim=int(row["embedding_dim"] or 0),
            centroid=centroid, samples=samples,
            sample_count=int(row["sample_count"] or 0),
            consistency=row["consistency"], threshold=row["threshold"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )


class SpeakerRegistry:
    """Profiles, durably. Same database as facts and episodes."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)

    @asynccontextmanager
    async def _conn(self):
        # `async with self._conn() as db`, never `async with await ...` — an
        # aiosqlite Connection is both awaitable and a context manager, and
        # doing both starts its worker thread twice (learned in P4).
        async with aiosqlite.connect(str(self._db_path)) as db:
            db.row_factory = aiosqlite.Row
            yield db

    async def initialize(self) -> None:
        async with self._conn() as db:
            for sql in SPEAKER_DDL:
                await db.execute(sql)
            await db.commit()

    async def save(self, profile: SpeakerProfile) -> str:
        if profile.centroid is None:
            raise ValueError("profile has no centroid")
        profile.updated_at = _now()
        async with self._conn() as db:
            await db.execute(
                """INSERT OR REPLACE INTO speaker_profiles
                   (profile_id, display_name, role, model_id, model_revision,
                    embedding_dim, centroid, samples, sample_count, consistency,
                    threshold, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (profile.profile_id, profile.display_name, profile.role,
                 profile.model_id, profile.model_revision, profile.embedding_dim,
                 json.dumps([float(v) for v in profile.centroid]),
                 json.dumps([[float(v) for v in s] for s in profile.samples]),
                 profile.sample_count, profile.consistency, profile.threshold,
                 profile.created_at, profile.updated_at),
            )
            await db.commit()
        logger.info("speaker_profile_saved", profile=profile.profile_id,
                    name=profile.display_name, samples=profile.sample_count)
        return profile.profile_id

    async def all(self) -> list[SpeakerProfile]:
        async with self._conn() as db:
            async with db.execute("SELECT * FROM speaker_profiles ORDER BY created_at ASC") as cur:
                rows = await cur.fetchall()
        return [SpeakerProfile.from_row(r) for r in rows]

    async def matchable(self) -> list[SpeakerProfile]:
        """Only profiles this build can honestly compare against."""
        return [p for p in await self.all() if p.compatible]

    async def get(self, profile_id: str) -> SpeakerProfile | None:
        async with self._conn() as db:
            async with db.execute(
                "SELECT * FROM speaker_profiles WHERE profile_id = ?", (profile_id,)) as cur:
                row = await cur.fetchone()
        return SpeakerProfile.from_row(row) if row else None

    async def by_name(self, display_name: str) -> list[SpeakerProfile]:
        return [p for p in await self.all()
                if p.display_name.strip().lower() == (display_name or "").strip().lower()]

    async def delete(self, profile_id: str) -> bool:
        """Remove the profile AND its embeddings. Nothing is retained."""
        async with self._conn() as db:
            cur = await db.execute(
                "DELETE FROM speaker_profiles WHERE profile_id = ?", (profile_id,))
            await db.commit()
            gone = (cur.rowcount or 0) > 0
        if gone:
            logger.info("speaker_profile_deleted", profile=profile_id)
        return gone

    async def stats(self) -> dict[str, Any]:
        profiles = await self.all()
        return {
            "profiles": len(profiles),
            "ready": sum(1 for p in profiles if p.compatible),
            "needs_reenrollment": sum(1 for p in profiles if not p.compatible),
        }


def new_profile_id() -> str:
    return f"spk-{uuid.uuid4().hex[:12]}"
