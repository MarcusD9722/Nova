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
    """The process-wide speaker service, CREATING it if this is the first use.

    It used to just read `STATE.speaker` — which is populated lazily by the
    `/stt` path and by nothing else. On a freshly-booted backend every route in
    this file therefore answered 503 until some voice turn happened to run
    first, so the calibration harness 503'd on its very first preflight check
    and there was no order of operations a human could follow to avoid it.

    Same lazy factory `/stt` uses, so there is exactly one service and one
    profile registry either way. Imported inside the function because
    `backend.app` imports this router.
    """
    svc = getattr(STATE, "speaker", None)
    if svc is None:
        try:
            from backend.app import _speaker_service
            svc = _speaker_service()
        except Exception as e:  # noqa: BLE001
            logger.warning("speaker_service_create_failed", error=str(e)[:200])
            svc = None
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
    # SpeakerService.delete() invalidates the calibration centrally.
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

    # Top-level `sample_count` is part of the enrollment contract (see
    # SpeakerService.enrol); the nested profile is the fallback for any older
    # caller. Reading only the nested one reported every success as 0 kept.
    kept = int(result.get("sample_count")
               or (result.get("profile") or {}).get("sample_count") or 0)
    result["min_kept_required"] = MIN_KEPT_SAMPLES
    result["meets_p52_bar"] = kept >= MIN_KEPT_SAMPLES
    if not result["meets_p52_bar"]:
        # Reported, not silently accepted. The caller is told which samples
        # failed and why so the human can re-record rather than proceed with a
        # profile that P5.2 would not stand behind.
        result["note"] = (f"kept {kept} of {len(samples)}; P5.2 requires "
                          f"{MIN_KEPT_SAMPLES}. Re-record the rejected samples.")
    # Invalidation lives in SpeakerService.enrol now, so a direct non-HTTP
    # caller cannot leave a stale fit standing.
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


class PermissionProbeIn(BaseModel):
    #: A handle from `/stt` with `speaker=true`. Omit it for the TYPED reference
    #: case. The client never asserts who is speaking — the handle is redeemed
    #: here, backend-side, exactly as `/chat` redeems it.
    voice_turn_id: str | None = None
    #: Fixed by default and validated against an allow-list below.
    capability: str = "computer.type"


#: The only capabilities this probe may ask about. `computer.type` is the
#: intended one: STANDARD tier, so guarded mode answers `needs_confirmation`;
#: no platform adapter ships and `NOVA_COMPUTER_CONTROL` is off by default, so
#: even an approval could not synthesize a keystroke. The probe additionally
#: never approves — see below. Nothing here can type on Marcus's machine.
PROBE_CAPABILITIES = {"computer.type", "computer.observe"}

#: Capability -> ComputerControl action kind, so the probe goes through the
#: SHIPPED mapping in core/computer_control.py rather than a copy of it.
_PROBE_ACTION = {"computer.type": "type"}


@router.post("/permission-probe")
async def permission_probe(body: PermissionProbeIn) -> dict[str, Any]:
    """Prove that speaker identity changes NO permission decision (V3 P5.2 §6).

    This exists because the previous harness asserted `pass: true` as a literal.
    A privacy invariant asserted by a constant is not tested, it is announced.

    What actually runs here is production code: the real `PermissionBroker`
    built at startup with Marcus's configured mode, reached through the real
    `ComputerControl.act()` and its real capability mapping. Nothing about
    `evaluate()` is reimplemented — the point is that it takes no identity
    argument at all, and the only way to show that is to call it.

    SAFETY. `wait_for_confirm=False`, so the probe never waits for and never
    receives an approval. Any pending request it creates is immediately resolved
    as REJECTED, so no confirmation prompt is left hanging in the UI and no
    action can proceed. With guarded mode, no adapter and execution disabled,
    there are three independent reasons nothing can be typed; the probe relies
    on all three rather than any one.
    """
    cap = (body.capability or "computer.type").strip()
    if cap not in PROBE_CAPABILITIES:
        raise HTTPException(status_code=400,
                            detail=f"'{cap}' is not probeable; use one of "
                                   f"{sorted(PROBE_CAPABILITIES)}")

    rt = getattr(STATE, "runtime", None)
    if rt is None:
        raise HTTPException(status_code=503, detail="runtime unavailable")
    broker = rt.permission_broker
    computer = rt.computer

    # Identity is RESOLVED BY THE BACKEND from the opaque handle, never taken
    # from the request body. An unredeemable or absent handle is the typed
    # owner reference case, which is what we compare the voice cases against.
    identity: dict[str, Any] = {"source": "typed", "status": None,
                                "profile_id": None, "display_name": None,
                                "attempted": False}
    if body.voice_turn_id:
        match = _service().redeem_voice_turn(body.voice_turn_id)
        if match is None:
            raise HTTPException(status_code=400,
                                detail="voice_turn_id is unknown, expired, or already used")
        identity = {
            "source": "voice",
            "status": match.status,
            # Asserted identity only — `top_scored_profile_id` is deliberately
            # NOT consulted for anything here.
            "profile_id": match.profile_id,
            "display_name": match.display_name,
            "attempted": match.attempted,
        }

    from core.permissions import TIER_NAMES, tier_of

    kind = _PROBE_ACTION.get(cap)
    if kind is not None:
        result = await computer.act(kind, target="", details={"probe": "v3-p5.2"},
                                    wait_for_confirm=False)
        decision = str(result.get("status") or "")
        request_id = result.get("request_id")
    else:
        result = await computer.observe("windows")
        decision = "allowed" if result.get("ok") else str(result.get("status") or "")
        request_id = None

    # Never leave a live confirmation behind. Resolving as rejected also leaves
    # an honest audit line: this probe asked and declined its own request.
    if request_id:
        try:
            broker.resolve(str(request_id), False, by="p52-permission-probe")
        except Exception:  # noqa: BLE001
            pass

    from core.computer_control import execution_enabled

    return {
        "capability": cap,
        "tier": TIER_NAMES.get(tier_of(cap), "unknown"),
        "mode": broker.mode,
        "decision": decision,
        "identity": identity,
        # Proof, in the response, that nothing could have run.
        "executed": decision == "executed",
        "execution_enabled": execution_enabled(),
        "adapter_installed": bool(getattr(computer, "_adapter", None) is not None),
        "note": ("PermissionBroker.evaluate() takes no identity argument. This "
                 "decision must be identical for typed, Marcus-voice and "
                 "guest-voice turns; the harness compares all three."),
    }


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
