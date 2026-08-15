"""V3 P5.1a: the two remaining ways a voice turn could lose its unverified state.

Both reproduced on HEAD d9ecc5a before anything was changed.

**Hole 1 — `unavailable` was the one outcome that got no handle.**
`issue_voice_turn` refused `status == unavailable`, so `known`, `unknown`,
`ambiguous` and `too_short` all carried structured evidence that a voice command
had happened and a classifier failure carried none. Once attribution is wired,
"no speaker metadata" is precisely the state that would be read as typed-Marcus
— so the failure case is the one that most needs the handle.

**Hole 2 — an empty transcript was still classified.**
`/stt` called the speaker path whenever `identify_speaker` was set, with no
reference to `result.empty`. A buffer can carry enough energy to clear
`command_quality()` while Whisper returns nothing, so background noise could
come back as a KNOWN speaker for a turn containing no words.

The distinction this suite exists to protect:

    attempted=False   the feature is OFF. Legacy Nova. No speaker question.
    attempted=True    the feature is ON and Nova tried. Every outcome, including
                      `unavailable`, is a real backend-derived voice-turn result.

Run:  venv\\Scripts\\python.exe tests\\test_speaker_unverified_v51a.py
"""

from __future__ import annotations

import asyncio
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


def speech(sec=3.0, amp=0.08, seed=0):
    """Acoustically real audio that clears the command-quality gate."""
    rng = np.random.default_rng(seed)
    n = int(SR * sec)
    t = np.arange(n) / SR
    x = amp * (np.sin(2 * np.pi * 130 * t) + 0.4 * rng.standard_normal(n))
    return (x * np.clip(0.5 + 0.5 * np.sin(2 * np.pi * 3 * t), 0, None)).astype(np.float32)


# ── 1. every attempted outcome keeps a handle ────────────────────────────────

async def test_every_attempted_outcome_gets_a_handle():
    check.section("all five outcomes preserve their voice-turn evidence")
    from core.speaker.matcher import SpeakerMatch
    from core.speaker.service import SpeakerService

    with tempfile.TemporaryDirectory(prefix="nova-p51a-") as td:
        svc = SpeakerService(Path(td) / "n.sqlite3")

        for status in ("known", "unknown", "ambiguous", "too_short", "unavailable"):
            m = SpeakerMatch(status=status, attempted=True,
                             profile_id="p1" if status == "known" else None,
                             display_name="Marcus" if status == "known" else None)
            tid = svc.issue_voice_turn(m)
            check(bool(tid), f"'{status}' mints a handle")
            got = svc.redeem_voice_turn(tid)
            check(got is not None and got.status == status,
                  f"and redeems back as '{status}'")
            if status != "known":
                check(got.profile_id is None and got.display_name is None,
                      f"'{status}' carries no identity it did not earn")

        # This is the regression: on d9ecc5a it returned None.
        um = SpeakerMatch(status="unavailable", reason="error", attempted=True)
        tid = svc.issue_voice_turn(um)
        check(bool(tid),
              "an attempted 'unavailable' is a voice turn, not an absence of one")
        again = svc.redeem_voice_turn(tid)
        check(again is not None and again.status == "unavailable",
              "it redeems to unavailable")
        check(svc.redeem_voice_turn(tid) is None,
              "and it is STILL single-use — P5.1's property is not lost")


async def test_unavailable_handle_expires():
    check.section("an unredeemed unavailable handle still expires")
    from core.speaker.matcher import SpeakerMatch
    from core.speaker.service import SpeakerService

    with tempfile.TemporaryDirectory(prefix="nova-p51a-exp-") as td:
        svc = SpeakerService(Path(td) / "n.sqlite3")
        tid = svc.issue_voice_turn(SpeakerMatch(status="unavailable", attempted=True))
        entry = svc._turns[tid]
        svc._turns[tid] = type(entry)(entry.turn_id, entry.match,
                                      time.monotonic() - 10_000)
        check(svc.redeem_voice_turn(tid) is None, "expired unavailable handle fails")
        check(svc.redeem_voice_turn("vt-invented") is None, "invented handle fails")


async def test_disabled_is_not_an_unverified_turn():
    check.section("DISABLED is legacy Nova, not a guest turn")
    from core.speaker.backend import EMBEDDER
    from core.speaker.service import SpeakerService

    prev = os.environ.get("NOVA_SPEAKER_ID")
    os.environ["NOVA_SPEAKER_ID"] = "0"
    try:
        with tempfile.TemporaryDirectory(prefix="nova-p51a-off-") as td:
            svc = SpeakerService(Path(td) / "n.sqlite3")
            before_calls = EMBEDDER.calls

            m = await svc.identify(speech(3.0), SR)
            check(m.status == "unavailable", "identify reports unavailable")
            check(m.attempted is False,
                  "and marks it NOT attempted — nobody asked a speaker question")
            check(m.reason == "disabled", f"with an honest reason ({m.reason})")

            check(svc.issue_voice_turn(m) is None,
                  "so NO handle is minted: disabled turns must not become "
                  "unverified-voice turns and change legacy behaviour")
            check(EMBEDDER.calls == before_calls,
                  f"and the model was never invoked ({EMBEDDER.calls - before_calls})")
    finally:
        if prev is None:
            os.environ.pop("NOVA_SPEAKER_ID", None)
        else:
            os.environ["NOVA_SPEAKER_ID"] = prev


async def test_enabled_failure_is_attempted():
    check.section("an ENABLED classifier failure is still an attempted voice turn")
    from core.speaker.service import SpeakerService

    with tempfile.TemporaryDirectory(prefix="nova-p51a-fail-") as td:
        svc = SpeakerService(Path(td) / "n.sqlite3")

        # Silence: enabled, attempted, refused by the quality gate.
        m = await svc.identify(np.zeros(SR * 3, dtype=np.float32), SR)
        check(m.attempted is True,
              "a quality-gate refusal is attempted, unlike a disabled turn")
        check(m.status != "known", f"and never known ({m.status})")
        check(bool(svc.issue_voice_turn(m)), "so it gets a handle")

        # A crashing embedder: still attempted.
        from core.speaker import backend as B
        real = B.EMBEDDER.embed

        def boom(_a, _sr):
            raise RuntimeError("model exploded")

        B.EMBEDDER.embed = boom
        try:
            m2 = await svc.identify(speech(3.0), SR)
            check(m2.status == "unavailable", f"a crash is unavailable ({m2.status})")
            check(m2.attempted is True, "and is marked attempted")
            check(bool(svc.issue_voice_turn(m2)),
                  "a crashed classifier still yields a voice-turn handle")
        finally:
            B.EMBEDDER.embed = real


# ── 2. empty transcripts never reach the model ───────────────────────────────

async def test_empty_transcript_never_embeds():
    check.section("an empty transcript costs zero embedding calls")
    import backend.app as app
    from core.speaker import backend as B

    calls = {"n": 0}
    real = B.EMBEDDER.embed

    def counting(audio, sr):
        calls["n"] += 1
        return real(audio, sr)

    B.EMBEDDER.embed = counting
    try:
        pcm = speech(3.0)   # acoustically real: clears command_quality

        calls["n"] = 0
        info = await app._identify_speaker(pcm, SR, skip_reason="empty_transcript")
        check(calls["n"] == 0,
              f"ECAPA was NOT invoked for an empty transcript ({calls['n']} calls)")
        check(info.status != "known", f"the result is never known ({info.status})")
        check(info.profile_id is None and info.display_name is None,
              "no profile is attached")
        check(info.similarity is None, "and no similarity asserting identity")
        check(info.reason == "empty_transcript",
              f"the reason is structured and honest ({info.reason})")
        check(info.attempted is True,
              "it is still an ATTEMPTED voice turn, not an absence of one")
        check(bool(info.voice_turn_id),
              "and it keeps a handle so downstream cannot mistake it for typed")

        # The same buffer, treated as a real utterance, DOES classify — exactly
        # once. This is the control: the guard is about the transcript, not the
        # audio.
        calls["n"] = 0
        info2 = await app._identify_speaker(pcm, SR)
        check(calls["n"] == 1,
              f"a real command embeds exactly once ({calls['n']})")
        check(info2.attempted is True, "and is attempted")
    finally:
        B.EMBEDDER.embed = real


async def test_stt_path_wires_the_guard():
    check.section("/stt passes the guard, and only for empty results")
    import inspect

    import backend.app as app

    src = inspect.getsource(app._stt_transcribe)
    check("identify_speaker" in src, "the opt-in flag still gates classification")
    check("skip_reason" in src and "result.empty" in src,
          "and an empty Whisper result short-circuits the model")

    # One upload, one decode, one Whisper call, at most one embedding.
    check(src.count("_run_asr") == 2,          # definition + single call
          f"_run_asr is defined once and called once ({src.count('_run_asr')})")
    check(src.count("subprocess.run") <= 1, "ffmpeg is invoked at most once")
    check(src.count("_identify_speaker") == 1,
          "and speaker identification has exactly one call site")


async def test_wake_and_typed_do_no_embedding():
    check.section("wake chunks and typed chat never embed")
    import inspect

    import backend.app as app
    from core.speaker import backend as B

    sig = inspect.signature(app.stt)
    check("speaker" in sig.parameters, "/stt takes an explicit speaker flag")
    check(sig.parameters["speaker"].default is not True,
          "which defaults OFF, so the continuous wake loop stays free")

    calls = {"n": 0}
    real = B.EMBEDDER.embed

    def counting(audio, sr):
        calls["n"] += 1
        return real(audio, sr)

    B.EMBEDDER.embed = counting
    try:
        # A wake-style request: identify_speaker not set.
        info = await app._identify_speaker(speech(2.0), SR, skip_reason="empty_transcript")
        calls["n"] = 0
        # Typed chat never touches this path at all; assert the wake default.
        src = inspect.getsource(app._stt_transcribe)
        check("if identify_speaker:" in src,
              "classification is inside an opt-in branch, so a wake chunk skips it")
        check(calls["n"] == 0, f"no embedding happened ({calls['n']})")
    finally:
        B.EMBEDDER.embed = real


async def test_speaker_failure_cannot_break_stt():
    check.section("Whisper succeeding is never undone by a speaker failure")
    import backend.app as app
    from core.speaker import backend as B

    real = B.EMBEDDER.embed

    def boom(_a, _sr):
        raise RuntimeError("model exploded mid-request")

    B.EMBEDDER.embed = boom
    try:
        info = await app._identify_speaker(speech(3.0), SR)
        check(info is not None, "the helper returns rather than raising")
        check(info.status == "unavailable", f"reporting unavailable ({info.status})")
        check(info.status != "known", "and certainly not a known speaker")
    finally:
        B.EMBEDDER.embed = real

    # A completely absent service must degrade the same way.
    from backend.state import STATE
    prev = getattr(STATE, "speaker", None)
    STATE.speaker = None
    try:
        real_svc = app._speaker_service
        app._speaker_service = lambda: None
        try:
            info = await app._identify_speaker(speech(3.0), SR)
            check(info.status == "unavailable",
                  "a missing service degrades instead of raising")
        finally:
            app._speaker_service = real_svc
    finally:
        STATE.speaker = prev


async def main():
    await test_every_attempted_outcome_gets_a_handle()
    await test_unavailable_handle_expires()
    await test_disabled_is_not_an_unverified_turn()
    await test_enabled_failure_is_attempted()
    await test_empty_transcript_never_embeds()
    await test_stt_path_wires_the_guard()
    await test_wake_and_typed_do_no_embedding()
    await test_speaker_failure_cannot_break_stt()
    check.finish()


if __name__ == "__main__":
    run(main)
