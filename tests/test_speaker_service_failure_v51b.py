"""V3 P5.1b: an ENABLED subsystem failure must not look like DISABLED mode.

Reproduced on HEAD 335d31a before anything changed. With the feature enabled and
a real command:

    SpeakerService cannot be constructed  -> attempted=False, no handle
    unexpected exception in the helper    -> attempted=False, no handle
    NOVA_SPEAKER_ID=0 (disabled)          -> attempted=False, no handle

The first two were byte-for-byte the third in every field downstream would read.
Only the `reason` string differed, and no attribution logic should ever have to
parse prose to decide whether personal memory may be written.

The invariant these tests pin:

    disabled     nobody asked a speaker question. Legacy Nova, typed semantics.
    unavailable  the question WAS asked and could not be answered. Unverified
                 voice — personal memory must not be written to Marcus.

A subsystem that fails must not be able to erase the evidence that it was
supposed to run, because "no speaker metadata" is exactly what would later be
read as typed-Marcus.

Run:  venv\\Scripts\\python.exe tests\\test_speaker_service_failure_v51b.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")
os.environ.setdefault("NOVA_REPO_ROOT", str(REPO))
os.environ.setdefault("NOVA_SPEAKER_ID", "1")

import numpy as np  # noqa: E402

from harness import Checks, run  # noqa: E402

check = Checks()
SR = 16000


def speech(sec=3.0, amp=0.08, seed=0):
    rng = np.random.default_rng(seed)
    n = int(SR * sec)
    t = np.arange(n) / SR
    x = amp * (np.sin(2 * np.pi * 130 * t) + 0.4 * rng.standard_normal(n))
    return (x * np.clip(0.5 + 0.5 * np.sin(2 * np.pi * 3 * t), 0, None)).astype(np.float32)


class _speaker_id_env:
    """Set NOVA_SPEAKER_ID for a block and restore it."""

    def __init__(self, value: str) -> None:
        self.value = value

    def __enter__(self):
        self.prev = os.environ.get("NOVA_SPEAKER_ID")
        os.environ["NOVA_SPEAKER_ID"] = self.value
        return self

    def __exit__(self, *exc):
        if self.prev is None:
            os.environ.pop("NOVA_SPEAKER_ID", None)
        else:
            os.environ["NOVA_SPEAKER_ID"] = self.prev


class _no_service:
    """Make `_speaker_service()` behave as if construction failed."""

    def __init__(self, mode="none"):
        self.mode = mode

    def __enter__(self):
        import backend.app as app
        self.app = app
        self.real = app._speaker_service
        if self.mode == "none":
            app._speaker_service = lambda: None
        else:
            def boom():
                raise RuntimeError("service construction exploded")
            app._speaker_service = boom
        return self

    def __exit__(self, *exc):
        self.app._speaker_service = self.real


# ── 1. service construction failure ──────────────────────────────────────────

async def test_missing_service_is_an_unverified_voice_turn():
    check.section("SpeakerService missing: enabled failure keeps voice provenance")
    import backend.app as app
    from core.speaker.voice_turns import VOICE_TURNS

    with _speaker_id_env("1"), _no_service("none"):
        info = await app._identify_speaker(speech(3.0), SR)

        check(info.status == "unavailable", f"status is unavailable ({info.status})")
        check(info.attempted is True,
              "attempted=True — the speaker question WAS asked, and a subsystem "
              "failure may not pretend otherwise")
        check(info.reason and "service" in info.reason.lower(),
              f"the reason identifies the service failure ({info.reason!r})")
        check(bool(info.voice_turn_id),
              "and an opaque handle preserves the unverified voice provenance")
        check(info.display_name is None and info.profile_id is None,
              "carrying no identity it did not earn")
        check(info.similarity is None, "and no similarity")

        # The handle is real: it resolves, once, to an unverified match.
        back = VOICE_TURNS.redeem(info.voice_turn_id)
        check(back is not None, "the handle redeems")
        check(back.status == "unavailable" and back.attempted is True,
              "to an attempted-but-unresolved voice turn")
        check(back.display_name is None,
              "which can never later be read as Marcus")
        check(VOICE_TURNS.redeem(info.voice_turn_id) is None,
              "and it is still single-use")


async def test_helper_exception_is_an_unverified_voice_turn():
    check.section("unexpected exception: same fail-closed semantics")
    import backend.app as app
    from core.speaker.voice_turns import VOICE_TURNS

    with _speaker_id_env("1"), _no_service("raise"):
        info = await app._identify_speaker(speech(3.0), SR)

        check(info.status == "unavailable", f"status is unavailable ({info.status})")
        check(info.attempted is True, "attempted=True through the broad except")
        check(bool(info.voice_turn_id), "a handle is still minted")
        check(info.display_name is None and info.profile_id is None,
              "and no identity is invented from the failure")

        back = VOICE_TURNS.redeem(info.voice_turn_id)
        check(back is not None and back.attempted is True,
              "the handle resolves to an attempted voice turn")


async def test_enabled_failure_is_distinguishable_from_disabled():
    check.section("the two states are not the same shape")
    import backend.app as app

    with _speaker_id_env("1"), _no_service("none"):
        failed = await app._identify_speaker(speech(3.0), SR)
    with _speaker_id_env("0"):
        disabled = await app._identify_speaker(speech(3.0), SR)

    check(failed.status == disabled.status == "unavailable",
          "both report unavailable — status alone cannot tell them apart")
    check(failed.attempted != disabled.attempted,
          f"but `attempted` does ({failed.attempted} vs {disabled.attempted})")
    check(failed.attempted is True and disabled.attempted is False,
          "enabled-and-failed is attempted; disabled is not")
    check(bool(failed.voice_turn_id) and not disabled.voice_turn_id,
          "and only the attempted one carries a handle")

    # This is the property attribution will depend on: a downstream consumer
    # must not need to parse `reason` prose to decide whether personal memory
    # may be written.
    check(failed.attempted != disabled.attempted,
          "the discriminator is a boolean, not a string comparison")


async def test_disabled_stays_legacy():
    check.section("NOVA_SPEAKER_ID=0 loads nothing and mints nothing")
    import backend.app as app
    from core.speaker import backend as B
    from core.speaker.voice_turns import VOICE_TURNS

    with _speaker_id_env("0"):
        before_calls = B.EMBEDDER.calls
        before_cached = len(VOICE_TURNS)

        info = await app._identify_speaker(speech(3.0), SR)
        check(info.attempted is False, "attempted=False — no speaker question")
        check(info.reason == "disabled", f"reason says so ({info.reason!r})")
        check(not info.voice_turn_id, "no handle is minted")
        check(B.EMBEDDER.calls == before_calls, "the model was not invoked")
        check(len(VOICE_TURNS) == before_cached, "and nothing entered the cache")

        # Even the service-failure path must respect disabled mode.
        with _no_service("none"):
            info2 = await app._identify_speaker(speech(3.0), SR)
        check(info2.attempted is False and not info2.voice_turn_id,
              "a service failure while DISABLED is still legacy, not a guest turn")


# ── 2. the registry survives its consumer ────────────────────────────────────

async def test_registry_is_independent_of_the_service():
    check.section("one cache, and it outlives SpeakerService")
    from core.speaker import service as svc_mod
    from core.speaker.voice_turns import VOICE_TURNS, VoiceTurnRegistry

    check(hasattr(svc_mod, "VOICE_TURNS"),
          "SpeakerService delegates to the shared registry rather than owning one")

    import inspect
    src = inspect.getsource(svc_mod.SpeakerService.issue_voice_turn)
    check("VOICE_TURNS.issue" in src, "issue delegates")
    src = inspect.getsource(svc_mod.SpeakerService.redeem_voice_turn)
    check("VOICE_TURNS.redeem" in src, "redeem delegates")

    # A private registry keeps the same contract, which is what makes it safe
    # to hold one process-wide.
    from core.speaker.matcher import SpeakerMatch
    reg = VoiceTurnRegistry(ttl_s=300.0, max_entries=8)

    check(reg.issue(SpeakerMatch(status="unavailable")) is None,
          "an unattempted match mints nothing")
    tid = reg.issue(SpeakerMatch(status="unavailable", attempted=True))
    check(bool(tid), "an attempted one does")
    check(reg.redeem(tid) is not None, "it redeems once")
    check(reg.redeem(tid) is None, "and only once")

    for i in range(30):
        reg.issue(SpeakerMatch(status="known", profile_id=f"p{i}", attempted=True))
    check(len(reg) <= 8, f"the cache stays bounded ({len(reg)} <= 8)")

    tid = reg.issue(SpeakerMatch(status="known", attempted=True))
    entry = reg._turns[tid]
    reg._turns[tid] = type(entry)(entry.turn_id, entry.match, time.monotonic() - 10_000)
    check(reg.redeem(tid) is None, "and expiry still applies")


# ── 3. nothing else moved ────────────────────────────────────────────────────

async def test_no_extra_work_added():
    check.section("no additional decode, upload, Whisper call or embedding")
    import inspect

    import backend.app as app
    from core.speaker import backend as B

    src = inspect.getsource(app._stt_transcribe)
    check(src.count("subprocess.run") <= 1, "ffmpeg is still invoked at most once")
    check(src.count("_run_asr") == 2, "Whisper still runs once")
    check(src.count("_identify_speaker") == 1, "one speaker call site")
    check(src.count("upload.read") <= 1, "and one upload read")

    # The failure paths must not reach the model at all.
    calls = {"n": 0}
    real = B.EMBEDDER.embed

    def counting(a, sr):
        calls["n"] += 1
        return real(a, sr)

    B.EMBEDDER.embed = counting
    try:
        with _speaker_id_env("1"), _no_service("none"):
            calls["n"] = 0
            await app._identify_speaker(speech(3.0), SR)
            check(calls["n"] == 0,
                  f"a missing service embeds nothing ({calls['n']})")

        with _speaker_id_env("1"):
            calls["n"] = 0
            await app._identify_speaker(speech(3.0), SR, skip_reason="empty_transcript")
            check(calls["n"] == 0, f"an empty transcript still embeds nothing ({calls['n']})")

            calls["n"] = 0
            await app._identify_speaker(speech(3.0), SR)
            check(calls["n"] == 1, f"a real command embeds exactly once ({calls['n']})")
    finally:
        B.EMBEDDER.embed = real


async def main():
    await test_missing_service_is_an_unverified_voice_turn()
    await test_helper_exception_is_an_unverified_voice_turn()
    await test_enabled_failure_is_distinguishable_from_disabled()
    await test_disabled_stays_legacy()
    await test_registry_is_independent_of_the_service()
    await test_no_extra_work_added()
    check.finish()


if __name__ == "__main__":
    run(main)
