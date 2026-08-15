"""V3 P5.1 pre-flight: four defects P5 part 1 shipped, each reproduced first.

All four were verified against the code before anything was changed:

  1. MODEL REVISION WAS METADATA-ONLY. `from_hparams` was called with no
     revision, so Nova loaded whatever HEAD happened to be while persisting
     `0f99f2d0…` into every profile and using it to decide compatibility. The
     string was an assertion nobody checked.

  2. NO COMMAND-AUDIO QUALITY GATE. Enrollment rejected silence and clipping;
     ordinary identification did not. A long-enough stretch of near-silence was
     embedded and scored against profiles like any other audio.

  3. REDEMPTION WAS REPLAYABLE. `redeem_voice_turn` returned the match and left
     the handle in the cache, so one captured id could assert the same identity
     on every later turn within its TTL.

  4. THE PRIVACY SETTING DID NOTHING. `NOVA_SPEAKER_KEEP_AUDIO` promised control
     over raw recordings that were never written, and the expression guarding
     the derived embeddings read `keep_audio() or True`.

Run:  venv\\Scripts\\python.exe tests\\test_speaker_preflight_v51.py
"""

from __future__ import annotations

import inspect
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")
os.environ.setdefault("NOVA_REPO_ROOT", str(REPO))

import numpy as np  # noqa: E402

from harness import Checks, run  # noqa: E402

check = Checks()
SR = 16000


def speech_like(seconds=3.0, amp=0.08, seed=0):
    rng = np.random.default_rng(seed)
    n = int(SR * seconds)
    t = np.arange(n) / SR
    sig = amp * (np.sin(2 * np.pi * 130 * t) + 0.4 * rng.standard_normal(n))
    return (sig * np.clip(0.5 + 0.5 * np.sin(2 * np.pi * 3 * t), 0, None)).astype(np.float32)


# ── 1. the revision is actually pinned ───────────────────────────────────────

async def test_revision_is_really_pinned():
    check.section("the model revision is requested, not just recorded")
    from core.speaker import backend as B

    src = inspect.getsource(B.SpeakerEmbedder._ensure_model)
    check("fetch_config" in src and "revision" in src.lower(),
          "the loader passes a revision to SpeechBrain")
    check("MODEL_REVISION" in src,
          "and it is the SAME constant that profiles are stamped with — a "
          "second literal here is how they drift apart")

    # The regression this guards: a loader that stops requesting the revision
    # while compatibility metadata keeps claiming it.
    from speechbrain.utils.fetching import FetchConfig  # type: ignore
    check("revision" in inspect.signature(FetchConfig).parameters,
          "SpeechBrain's FetchConfig still supports revision pinning")

    if B.EMBEDDER.warm():
        st = B.EMBEDDER.status()
        check(st["revision_pinned"] is True,
              "a real load reports that it went through the pinned path")
        check(st["model_revision"] == B.MODEL_REVISION[:12],
              f"and reports the pinned revision ({st['model_revision']})")
    else:
        check(False, "the model could not load — cannot verify the live pin")


async def test_stale_revision_profiles_are_not_matched():
    check.section("a profile from another revision is never scored")
    from core.speaker.backend import EMBEDDING_DIM, MODEL_ID, MODEL_REVISION
    from core.speaker.matcher import match
    from core.speaker.registry import SpeakerProfile

    v = np.eye(EMBEDDING_DIM, dtype=np.float32)[0]
    good = SpeakerProfile(profile_id="ok", display_name="Marcus", model_id=MODEL_ID,
                          model_revision=MODEL_REVISION, embedding_dim=EMBEDDING_DIM,
                          centroid=v, sample_count=4)
    stale = SpeakerProfile(profile_id="stale", display_name="MarcusOld",
                           model_id=MODEL_ID, model_revision="deadbeef" * 5,
                           embedding_dim=EMBEDDING_DIM, centroid=v, sample_count=4)

    check(good.compatible and not stale.compatible, "compatibility follows the revision")
    check(stale.status == "needs_reenrollment", f"stale reports {stale.status}")

    r = match(v, [stale])
    check(r.status == "unknown",
          f"a stale profile alone yields unknown, never a match ({r.status})")

    r = match(v, [stale, good])
    check(r.profile_id == "ok",
          "and a stale profile cannot win against a compatible one")


# ── 2. command-audio quality ─────────────────────────────────────────────────

async def test_command_quality_gate():
    check.section("silence never becomes a known speaker")
    from core.speaker.backend import command_quality

    ok, why = command_quality(np.zeros(SR * 3, dtype=np.float32), SR)
    check(not ok, f"digital silence is rejected ({why})")

    ok, why = command_quality((1e-5 * np.random.randn(SR * 3)).astype(np.float32), SR)
    check(not ok, f"near-silence is rejected ({why})")

    ok, why = command_quality(np.full(SR * 3, np.nan, dtype=np.float32), SR)
    check(not ok, f"malformed PCM is rejected ({why})")

    ok, why = command_quality(np.array([], dtype=np.float32), SR)
    check(not ok, f"empty audio is rejected ({why})")

    ok, why = command_quality(np.ones(SR * 3, dtype=np.float32), SR)
    check(not ok, f"a fully clipped buffer is rejected ({why})")

    # ...and the bar must NOT be enrollment's. Real commands are short.
    for secs, label in ((1.1, "'stop it'"), (1.5, "'yes please'"), (3.0, "a sentence")):
        ok, why = command_quality(speech_like(secs), SR)
        check(ok, f"a normal {secs}s command is accepted ({label}) — {why}")

    ok, why = command_quality(speech_like(0.4), SR)
    check(not ok and "short" in why, f"a 0.4s fragment is too_short ({why})")

    quiet = speech_like(2.0, amp=0.02)
    ok, why = command_quality(quiet, SR)
    check(ok, f"a genuinely quiet but real command still passes ({why})")


async def test_identify_refuses_silence():
    check.section("identify() applies the gate before the model")
    from core.speaker.service import SpeakerService

    with tempfile.TemporaryDirectory(prefix="nova-p51-") as td:
        svc = SpeakerService(Path(td) / "nova.sqlite3")
        m = await svc.identify(np.zeros(SR * 3, dtype=np.float32), SR)
        check(m.status in ("unavailable", "unknown", "too_short"),
              f"silence does not come back known ({m.status})")
        check(m.status != "known", "and specifically never 'known'")
        check(m.profile_id is None, "with no profile attached")

        m2 = await svc.identify(speech_like(0.3), SR)
        check(m2.status == "too_short", f"a fragment is too_short ({m2.status})")


# ── 3. one-time redemption ───────────────────────────────────────────────────

async def test_voice_turn_is_single_use():
    check.section("a voice-turn handle is redeemable exactly once")
    from core.speaker.matcher import SpeakerMatch
    from core.speaker.service import VOICE_TURN_MAX, SpeakerService

    with tempfile.TemporaryDirectory(prefix="nova-p51-vt-") as td:
        svc = SpeakerService(Path(td) / "nova.sqlite3")
        m = SpeakerMatch(status="known", profile_id="p1", display_name="Marcus",
                         similarity=0.9, attempted=True)
        tid = svc.issue_voice_turn(m)

        first = svc.redeem_voice_turn(tid)
        check(first is not None and first.display_name == "Marcus",
              "the first redemption succeeds")

        second = svc.redeem_voice_turn(tid)
        check(second is None,
              "the SECOND redemption fails — a captured id cannot keep asserting "
              "an identity on later turns")

        check(svc.redeem_voice_turn("vt-invented") is None, "an invented handle fails")
        check(svc.redeem_voice_turn(None) is None, "and no handle at all fails")

        # Expiry still applies to an unredeemed handle.
        tid2 = svc.issue_voice_turn(m)
        entry = svc._turns[tid2]
        svc._turns[tid2] = type(entry)(entry.turn_id, entry.match,
                                       time.monotonic() - 10_000)
        check(svc.redeem_voice_turn(tid2) is None, "an expired handle fails")

        for i in range(VOICE_TURN_MAX + 50):
            svc.issue_voice_turn(SpeakerMatch(status="known", profile_id=f"p{i}",
                                              display_name=f"n{i}", attempted=True))
        check(len(svc._turns) <= VOICE_TURN_MAX,
              f"the cache stays bounded ({len(svc._turns)})")


async def test_failed_identity_stays_a_voice_turn():
    check.section("an identity failure never becomes Marcus")
    from core.speaker.matcher import SpeakerMatch
    from core.speaker.service import SpeakerService

    with tempfile.TemporaryDirectory(prefix="nova-p51-fail-") as td:
        svc = SpeakerService(Path(td) / "nova.sqlite3")

        # `unavailable` is included since P5.1a: it was the one outcome P5.1
        # refused a handle to, which is exactly the case that would later read
        # as typed-Marcus.
        for status in ("unknown", "ambiguous", "too_short", "unavailable"):
            tid = svc.issue_voice_turn(SpeakerMatch(status=status, attempted=True))
            check(bool(tid), f"a '{status}' outcome still mints a handle")
            got = svc.redeem_voice_turn(tid)
            check(got is not None and got.status == status,
                  f"and redeems back as '{status}', not as an owner")
            check(got.profile_id is None and got.display_name is None,
                  "carrying no identity it did not earn")


# ── 4. the privacy setting is gone, and no audio is retained ─────────────────

async def test_no_dead_privacy_setting():
    check.section("the misleading KEEP_AUDIO setting is gone")
    from core.settings import CATALOG
    from core.speaker import registry, service

    check("NOVA_SPEAKER_KEEP_AUDIO" not in CATALOG,
          "it is no longer advertised in the settings catalog")
    check(not hasattr(registry, "keep_audio"),
          "and the dead accessor is removed")
    src = inspect.getsource(service.SpeakerService.enrol)
    check("or True" not in src,
          "the `keep_audio() or True` expression is gone")


async def test_no_raw_audio_retained():
    check.section("enrollment keeps embeddings, never recordings")
    from core.speaker.backend import EMBEDDER
    from core.speaker.service import SpeakerService

    if not EMBEDDER.warm():
        check(False, "model unavailable — cannot run enrollment")
        return

    with tempfile.TemporaryDirectory(prefix="nova-p51-audio-") as td:
        root = Path(td)
        svc = SpeakerService(root / "nova.sqlite3")
        samples = [(speech_like(3.0, seed=i), SR) for i in range(4)]
        res = await svc.enrol(display_name="AudioTest", samples=samples)
        check(res["ok"], f"enrollment succeeded ({res.get('error')})")

        audio_files = [f for f in root.rglob("*")
                       if f.suffix.lower() in {".wav", ".webm", ".mp3", ".flac",
                                               ".ogg", ".m4a", ".raw", ".pcm"}]
        check(not audio_files,
              f"no audio file was written anywhere ({[f.name for f in audio_files]})")

        st = await svc.status()
        check(st.get("raw_audio_retained") is False,
              "and status states plainly that raw audio is not retained")

        # The derived embeddings ARE kept — that is the point, and it is not
        # the same thing as keeping a recording.
        profiles = await svc.registry.all()
        check(profiles and len(profiles[0].samples) >= 3,
              "the derived per-sample embeddings are retained for recalibration")


async def main():
    await test_revision_is_really_pinned()
    await test_stale_revision_profiles_are_not_matched()
    await test_command_quality_gate()
    await test_identify_refuses_silence()
    await test_voice_turn_is_single_use()
    await test_failed_identity_stays_a_voice_turn()
    await test_no_dead_privacy_setting()
    await test_no_raw_audio_retained()
    check.finish()


if __name__ == "__main__":
    run(main)
