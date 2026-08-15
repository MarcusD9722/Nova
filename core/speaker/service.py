from __future__ import annotations

"""The one thing the rest of Nova talks to about speaker identity.

    PCM  ->  embedder  ->  registry  ->  matcher  ->  SpeakerMatch

Responsibilities stay separated (backend / registry / matcher / enrollment) but
callers get one object, so `/stt` never grows enrollment logic and the runtime
never grows a threshold.

TWO INVARIANTS THAT ARE NOT NEGOTIABLE
--------------------------------------
1. **Speaker identity is not authentication.** A match may personalise a reply
   and attribute a memory. It may never grant a capability the permission
   architecture would otherwise confirm or deny. There is deliberately no method
   on this class that returns "allowed" — it returns who Nova *thinks* is
   speaking, and `PermissionBroker` remains the only thing that decides what may
   happen. Voice is trivially recordable and increasingly synthesisable; P5 makes
   no anti-spoofing claim whatsoever.

2. **It is enrichment, never a prerequisite.** Every failure path returns a
   `SpeakerMatch` with `status="unavailable"`. Nothing here may raise into /stt:
   Whisper succeeding and the request failing because a speaker model did not
   load would be a self-inflicted outage.
"""

import asyncio
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from core.logging_setup import get_logger
from core.speaker import matcher as M
from core.speaker.backend import (EMBEDDER, MODEL_ID, MODEL_REVISION,
                                  command_quality, enabled)
from core.speaker.matcher import SpeakerMatch, check_sample
from core.speaker.registry import SpeakerProfile, SpeakerRegistry, new_profile_id

logger = get_logger(__name__)

#: How long a classified voice turn can be redeemed for its identity.
#: Short on purpose: this is an integrity handle, not a session.
VOICE_TURN_TTL_S = 300.0
#: Bounded so a burst of voice turns cannot grow memory without limit.
VOICE_TURN_MAX = 256


@dataclass
class VoiceTurn:
    turn_id: str
    match: SpeakerMatch
    created_at: float


class SpeakerService:
    """Identify, enrol, and hand out short-lived voice-turn handles."""

    def __init__(self, db_path: Path) -> None:
        self.registry = SpeakerRegistry(db_path)
        self._ready = False
        self._lock = asyncio.Lock()
        self._turns: "dict[str, VoiceTurn]" = {}
        self.stats: dict[str, int] = {
            "identify_calls": 0, "known": 0, "unknown": 0, "ambiguous": 0,
            "too_short": 0, "unavailable": 0, "enrolments": 0, "failures": 0,
        }

    async def initialize(self) -> None:
        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return
            try:
                await self.registry.initialize()
                self._ready = True
            except Exception as e:  # noqa: BLE001
                logger.warning("speaker_registry_init_failed", error=str(e)[:200])

    # ── identification ───────────────────────────────────────────────────────

    async def identify(self, audio: np.ndarray, sample_rate: int) -> SpeakerMatch:
        """Who is speaking? Never raises; `unavailable` is a valid answer."""
        if not enabled():
            return SpeakerMatch(status=M.STATUS_UNAVAILABLE, reason="disabled")
        self.stats["identify_calls"] += 1
        try:
            await self.initialize()

            # Quality gate BEFORE the model (V3 P5.1). Silence has an embedding
            # too, and it will score against something.
            ok, why = command_quality(audio, sample_rate)
            if not ok:
                short = "too short" in why
                self.stats["too_short" if short else "unavailable"] += 1
                return SpeakerMatch(
                    status=M.STATUS_TOO_SHORT if short else M.STATUS_UNAVAILABLE,
                    reason=why)

            # The model runs in a thread: it is 40-60 ms of CPU work and the
            # event loop is serving token streams.
            emb = await asyncio.to_thread(EMBEDDER.embed, audio, sample_rate)
            if emb is None:
                # Distinguish "not enough voice" from "model is broken" — the
                # caller may want to ask the user to repeat themselves in one
                # case and say nothing at all in the other.
                dur = len(np.asarray(audio).reshape(-1)) / max(sample_rate, 1)
                if EMBEDDER.available and dur < 1.0:
                    self.stats["too_short"] += 1
                    return SpeakerMatch(status=M.STATUS_TOO_SHORT,
                                        reason=f"{dur:.2f}s of audio")
                self.stats["unavailable"] += 1
                return SpeakerMatch(status=M.STATUS_UNAVAILABLE, reason="no embedding")

            profiles = await self.registry.matchable()
            result = M.match(emb, profiles)
            self.stats[result.status] = self.stats.get(result.status, 0) + 1
            return result
        except Exception as e:  # noqa: BLE001
            self.stats["failures"] += 1
            logger.warning("speaker_identify_failed", error=str(e)[:200])
            return SpeakerMatch(status=M.STATUS_UNAVAILABLE, reason="error")

    # ── voice-turn handles (integrity, NOT authentication) ───────────────────

    def issue_voice_turn(self, match: SpeakerMatch) -> str | None:
        """Mint a short-lived id the frontend can quote back on /chat.

        The browser must not be able to say `"speaker": "Marcus"` and be
        believed — identity is derived on the backend, so it stays on the
        backend. The client only ever carries an opaque handle, which expires
        and is bounded.

        This protects the INTEGRITY of speaker metadata. It is not a session
        token and grants nothing.
        """
        if match is None or match.status == M.STATUS_UNAVAILABLE:
            return None
        import uuid
        self._sweep()
        turn_id = f"vt-{uuid.uuid4().hex[:16]}"
        self._turns[turn_id] = VoiceTurn(turn_id, match, time.monotonic())
        while len(self._turns) > VOICE_TURN_MAX:
            oldest = min(self._turns.values(), key=lambda t: t.created_at)
            self._turns.pop(oldest.turn_id, None)
        return turn_id

    def redeem_voice_turn(self, turn_id: str | None) -> SpeakerMatch | None:
        """Resolve a handle back to backend-derived identity — ONCE.

        Redemption consumes the handle (V3 P5.1). One /stt classification backs
        exactly one chat turn: a handle that could be replayed would let a
        captured id keep asserting "Marcus" across later turns he never spoke,
        which is precisely the forgery this mechanism exists to prevent.

        Still not authentication. It grants nothing; it only keeps speaker
        metadata backend-derived.
        """
        if not turn_id:
            return None
        self._sweep()
        entry = self._turns.pop(str(turn_id), None)   # pop: single use
        if entry is None:
            return None
        if time.monotonic() - entry.created_at > VOICE_TURN_TTL_S:
            return None
        return entry.match

    def _sweep(self) -> None:
        now = time.monotonic()
        for tid in [t for t, e in self._turns.items()
                    if now - e.created_at > VOICE_TURN_TTL_S]:
            self._turns.pop(tid, None)

    # ── enrollment ───────────────────────────────────────────────────────────

    async def enrol(self, *, display_name: str, samples: list[tuple[np.ndarray, int]],
                    role: str = "guest", profile_id: str | None = None) -> dict[str, Any]:
        """Build a profile from several samples, or explain why it cannot.

        Returns a plain dict so the HTTP layer can hand it straight back — the
        failure text is written for a person mid-enrollment, not for a log.
        """
        await self.initialize()
        name = (display_name or "").strip()
        if not name:
            return {"ok": False, "error": "a display name is required"}
        if not enabled():
            return {"ok": False, "error": "speaker identification is disabled (NOVA_SPEAKER_ID=0)"}
        if not EMBEDDER.available or not EMBEDDER.warm():
            return {"ok": False, "error": "the speaker model could not be loaded"}

        embeddings: list[np.ndarray] = []
        rejected: list[dict[str, Any]] = []
        for i, (audio, sr) in enumerate(samples):
            chk = check_sample(audio, sr)
            if not chk.ok:
                rejected.append({"index": i, "reason": chk.reason,
                                 "duration_s": round(chk.duration_s, 2)})
                continue
            emb = await asyncio.to_thread(EMBEDDER.embed, audio, sr)
            if emb is None:
                rejected.append({"index": i, "reason": "could not embed this sample"})
                continue
            embeddings.append(emb)

        built = M.build_profile_embedding(embeddings)
        if not built.ok:
            return {"ok": False, "error": built.reason, "rejected": rejected,
                    "accepted": len(embeddings)}

        profile = SpeakerProfile(
            profile_id=profile_id or new_profile_id(),
            display_name=name, role=(role or "guest").strip() or "guest",
            model_id=MODEL_ID, model_revision=MODEL_REVISION,
            embedding_dim=int(built.centroid.size),
            centroid=built.centroid,
            # The raw AUDIO is never stored, under any setting. These are the
            # derived embeddings, kept so a future recalibration need not ask
            # for six new recordings.
            samples=built.kept,
            sample_count=len(built.kept), consistency=built.consistency,
        )
        try:
            await self.registry.save(profile)
        except Exception as e:  # noqa: BLE001
            logger.warning("speaker_enrol_save_failed", error=str(e)[:200])
            return {"ok": False, "error": "could not save the profile"}

        self.stats["enrolments"] += 1
        existing = await self.registry.by_name(name)
        return {
            "ok": True,
            "profile": profile.describe(),
            "rejected": rejected,
            "consistency": round(built.consistency or 0.0, 4),
            "dropped_outliers": built.dropped,
            # Duplicate names are allowed but flagged: two profiles called
            # "Marcus" is usually a mistake, and silently merging them would be
            # worse than saying so.
            "duplicate_name": len(existing) > 1,
        }

    async def delete(self, profile_id: str) -> bool:
        await self.initialize()
        return await self.registry.delete(profile_id)

    async def profiles(self) -> list[dict[str, Any]]:
        await self.initialize()
        return [p.describe() for p in await self.registry.all()]

    # ── diagnostics ──────────────────────────────────────────────────────────

    async def status(self) -> dict[str, Any]:
        await self.initialize()
        try:
            reg = await self.registry.stats()
        except Exception:  # noqa: BLE001
            reg = {"profiles": 0}
        return {
            **EMBEDDER.status(),
            **reg,
            "threshold": M.threshold(),
            "margin": M.margin(),
            "threshold_calibrated": False,   # until the live harness has run
            "raw_audio_retained": False,
            "voice_turns_cached": len(self._turns),
            "matches": dict(self.stats),
        }
