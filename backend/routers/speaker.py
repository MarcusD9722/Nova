from __future__ import annotations

"""Local speaker enrollment and calibration endpoints (V3 P5.2).

These exist so a human can run the calibration protocol without editing Python,
SQLite or `.env`. They are deliberately small and local: the whole surface is
"show me the profiles, take these six samples, fit the numbers, apply them".

AUDIO POLICY
------------
Enrollment audio is decoded, embedded, and deleted. It is never stored, never
written to memory, never committed. The derived embeddings live in the profile
store under the existing policy; nothing here changes that.

NOT AUTHENTICATION
------------------
Nothing in this file consults or affects `PermissionBroker`. Calibrating a
threshold makes recognition *measured*; it does not make it *authorisation*.
These routes sit behind the same API-token middleware as every other endpoint
when `NOVA_API_TOKEN` is configured.
"""

import asyncio
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from backend.state import STATE
from core.logging_setup import get_logger

router = APIRouter(prefix="/speaker", tags=["speaker"])
logger = get_logger(__name__)

#: Enrollment needs more than the algorithm's floor. `SpeakerService.enrol`
#: can build a centroid from 3 usable samples, but P5.2 acceptance requires 5
#: kept out of 6 recorded — a profile built from the bare minimum has no margin
#: for one bad room, and re-recording is cheaper than a weak profile (§5).
MIN_KEPT_SAMPLES = 5


def _service():
    svc = getattr(STATE, "speaker", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="speaker subsystem unavailable")
    return svc


def _decode_to_pcm(raw: bytes, suffix: str = ".webm"):
    """Browser audio -> mono float32 PCM at the model's rate. Temp file only."""
    import numpy as np
    import soundfile as sf  # type: ignore

    from core.speaker.backend import TARGET_SR

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise HTTPException(status_code=503, detail="ffmpeg not available")

    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / f"in{suffix}"
        dst = Path(td) / "out.wav"
        src.write_bytes(raw)
        proc = subprocess.run(
            [ffmpeg, "-y", "-i", str(src), "-ac", "1", "-ar", str(TARGET_SR),
             "-f", "wav", str(dst)],
            capture_output=True,
        )
        if proc.returncode != 0 or not dst.exists():
            raise HTTPException(status_code=400, detail="could not decode audio")
        audio, sr = sf.read(str(dst), dtype="float32")
        if getattr(audio, "ndim", 1) > 1:
            audio = np.mean(audio, axis=1)
        return np.asarray(audio, dtype=np.float32), int(sr)
    # TemporaryDirectory removes the source and the wav on the way out; nothing
    # written here outlives the request.


@router.get("/status")
async def speaker_status() -> dict[str, Any]:
    return await _service().status()


@router.get("/profiles")
async def speaker_profiles() -> dict[str, Any]:
    svc = _service()
    return {"profiles": await svc.profiles()}


@router.delete("/profiles/{profile_id}")
async def delete_profile(profile_id: str) -> dict[str, Any]:
    """Explicit, human-initiated deletion.

    Enrollment never silently replaces a stale or duplicate profile — the
    harness shows what exists and makes the human choose (§5).
    """
    ok = await _service().delete(profile_id)
    if not ok:
        raise HTTPException(status_code=404, detail="no such profile")
    # A removed profile invalidates any calibration that covered it: the fit
    # described a population that no longer exists.
    try:
        await _service().calib.clear()
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "deleted": profile_id}


@router.post("/enroll")
async def enroll(
    display_name: str = Form(...),
    role: str = Form("guest"),
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    """Enrol one human from MULTIPLE samples in a single logical enrollment."""
    svc = _service()
    if not files:
        raise HTTPException(status_code=400, detail="no samples")

    samples: list[tuple[Any, int]] = []
    for f in files:
        raw = await f.read()
        if not raw:
            continue
        suffix = Path(f.filename or "s.webm").suffix or ".webm"
        pcm, sr = await asyncio.to_thread(_decode_to_pcm, raw, suffix)
        samples.append((pcm, sr))

    try:
        result = await svc.enrol(display_name=display_name, samples=samples,
                                 role=(role or "guest"))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(e)[:300]) from e

    kept = int(result.get("sample_count") or 0)
    result["min_kept_required"] = MIN_KEPT_SAMPLES
    result["meets_p52_bar"] = kept >= MIN_KEPT_SAMPLES
    if not result["meets_p52_bar"]:
        # Reported, not silently accepted. The caller is told which samples
        # failed and why so the human can re-record rather than proceed with a
        # profile that P5.2 would not stand behind.
        result["note"] = (f"kept {kept} of {len(samples)}; P5.2 requires "
                          f"{MIN_KEPT_SAMPLES}. Re-record the rejected samples.")
    # Enrolling anyone changes the population the calibration was fitted on.
    try:
        await svc.calib.clear()
    except Exception:  # noqa: BLE001
        pass
    return result


@router.post("/identify")
async def identify(file: UploadFile = File(...)) -> dict[str, Any]:
    """Score one utterance and return full diagnostics, for calibration trials.

    Separate from `/stt` on purpose: a trial needs the scores and no transcript,
    and running Whisper 64 times to collect them would be pure waste.
    """
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="empty audio")
    suffix = Path(file.filename or "s.webm").suffix or ".webm"
    pcm, sr = await asyncio.to_thread(_decode_to_pcm, raw, suffix)
    import time
    t0 = time.perf_counter()
    match = await _service().identify(pcm, sr)
    ms = round((time.perf_counter() - t0) * 1000.0, 2)
    from core.speaker.backend import MODEL_ID
    out = dict(match.for_response(model_id=MODEL_ID))
    out["embed_ms"] = ms
    out["duration_ms"] = int(round(len(pcm) / max(sr, 1) * 1000))
    return out


class TrialIn(BaseModel):
    truth: str
    top_profile_id: str | None = None
    top_score: float
    second_score: float | None = None
    second_profile_id: str | None = None
    status: str = ""
    condition: str = "normal"
    phase: str = "A"


class CalibrateIn(BaseModel):
    trials: list[TrialIn] = Field(default_factory=list)
    apply: bool = False


@router.get("/calibration")
async def get_calibration() -> dict[str, Any]:
    rec = await _service().calibration()
    if rec is None:
        return {"calibrated": False}
    return {"calibrated": True, "margin": rec.margin,
            "profile_ids": rec.profile_ids, "metrics": rec.metrics,
            "calibrated_at": rec.calibrated_at,
            "model_id": rec.model_id, "model_revision": rec.model_revision}


@router.post("/calibration")
async def post_calibration(body: CalibrateIn) -> dict[str, Any]:
    """Fit thresholds and margin from labelled trials; optionally persist.

    Fitting and applying are separate on purpose: the human sees the proposed
    numbers, and the reasons, before anything changes.
    """
    from core.speaker.calibration import (CalibrationRecord, Trial, calibrate)

    svc = _service()
    trials = [Trial(**t.model_dump()) for t in body.trials]
    profiles = {p.profile_id: p for p in await svc.registry.all()}
    names = {pid: p.display_name for pid, p in profiles.items()}

    result = calibrate(trials, names=names)
    out = result.to_dict()
    out["applied"] = False

    if body.apply:
        if not result.ok:
            # Refusing to apply a failed fit is the entire point. A threshold
            # that only passes because it was loosened carries a claim it cannot
            # support.
            raise HTTPException(status_code=400,
                                detail=f"calibration failed: {result.reason}")
        for fit in result.profiles:
            prof = profiles.get(fit.profile_id)
            if prof is None or fit.threshold is None:
                continue
            prof.threshold = float(fit.threshold)
            await svc.registry.save(prof)
        await svc.calib.save(CalibrationRecord(
            margin=float(result.margin or 0.0),
            profile_ids=[f.profile_id for f in result.profiles],
            metrics={
                "protocol_version": result.protocol_version,
                "margin_correct_rate": result.margin_correct_rate,
                "profiles": [
                    {k: v for k, v in vars(f).items()
                     if k in ("profile_id", "display_name", "threshold",
                              "genuine_n", "impostor_n", "genuine_accept_rate",
                              "false_accepts", "separation")}
                    for f in result.profiles
                ],
                "trials": len(trials),
            },
        ))
        out["applied"] = True
        logger.info("speaker_calibration_applied", margin=result.margin,
                    profiles=len(result.profiles))
    return out


@router.delete("/calibration")
async def clear_calibration() -> dict[str, Any]:
    svc = _service()
    await svc.calib.clear()
    for p in await svc.registry.all():
        if p.threshold is not None:
            p.threshold = None
            await svc.registry.save(p)
    return {"ok": True, "cleared": True}
