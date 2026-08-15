"""V3 P5: local speaker identification.

WHAT THESE TESTS CAN AND CANNOT PROVE
-------------------------------------
They prove the mechanism: embeddings extract, same-voice scores above
different-voice, profiles persist and reload, thresholds and margins behave,
incompatible models are refused, bad enrollment audio is rejected, and every
failure path degrades instead of raising.

They CANNOT prove that Nova recognises Marcus. No offline test can: that needs
Marcus's real voice, a real second person, and the microphone and room they
actually use. `tests/live_speaker_id_harness.md` exists for exactly that, and
the thresholds here stay marked provisional until it has been run.

The fixtures below are one real human voice (voices/nova.wav, the XTTS
reference) plus deterministic synthetic voices. A synthetic voice is a
*controlled contrast*, not a second person — it is honest evidence that the
embedding separates dissimilar audio, and nothing more.

Run:  venv\\Scripts\\python.exe tests\\test_speaker_id_v5.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
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


# ── fixtures ─────────────────────────────────────────────────────────────────

def synth_voice(seconds=3.0, f0=110.0, formants=(700, 1220, 2600), seed=0):
    """A deterministic source-filter voice. NOT a person."""
    rng = np.random.default_rng(seed)
    n = int(SR * seconds)
    t = np.arange(n) / SR
    f0_t = f0 * (1 + 0.02 * np.sin(2 * np.pi * 3.1 * t) + 0.004 * rng.standard_normal(n))
    phase = np.cumsum(2 * np.pi * f0_t / SR)
    src = np.zeros(n)
    for k in range(1, 25):
        src += (1.0 / k ** 1.2) * np.sin(k * phase)
    out = np.zeros(n)
    for fc in formants:
        bw, r = fc * 0.10, np.exp(-np.pi * fc * 0.10 / SR)
        th = 2 * np.pi * fc / SR
        a1, a2 = -2 * r * np.cos(th), r * r
        y = np.zeros(n)
        for i in range(2, n):
            y[i] = src[i] - a1 * y[i - 1] - a2 * y[i - 2]
        out += y / (np.abs(y).max() + 1e-9)
    out *= np.clip(0.6 + 0.4 * np.sin(2 * np.pi * 2.3 * t), 0, None)
    return (out / (np.abs(out).max() + 1e-9) * 0.3).astype(np.float32)


def real_voice() -> np.ndarray | None:
    """The one real human voice available offline."""
    import soundfile as sf
    path = REPO / "voices" / "nova.wav"
    if not path.exists():
        return None
    x, sr = sf.read(str(path), dtype="float32")
    if x.ndim > 1:
        x = x.mean(axis=1)
    if sr != SR:
        x = np.interp(np.linspace(0, len(x) - 1, int(len(x) * SR / sr)),
                      np.arange(len(x)), x).astype(np.float32)
    return x


VOICE_A = synth_voice(3.0, f0=112, formants=(720, 1240, 2600), seed=1)
VOICE_A2 = synth_voice(3.0, f0=112, formants=(720, 1240, 2600), seed=2)
VOICE_A3 = synth_voice(3.0, f0=112, formants=(720, 1240, 2600), seed=3)
VOICE_A4 = synth_voice(3.0, f0=112, formants=(720, 1240, 2600), seed=4)
VOICE_B = synth_voice(3.0, f0=196, formants=(430, 2050, 2900), seed=9)


def samples_of(v, n=4, seeds=(11, 12, 13, 14)):
    return [(v, SR) for _ in range(n)]


# ── 1. matcher logic, with no model at all ───────────────────────────────────

async def test_open_set_decisions():
    check.section("open-set matching: known / unknown / ambiguous")
    from core.speaker.matcher import (STATUS_AMBIGUOUS, STATUS_KNOWN, STATUS_UNAVAILABLE,
                                      STATUS_UNKNOWN, match)
    from core.speaker.registry import SpeakerProfile
    from core.speaker.backend import EMBEDDING_DIM, MODEL_ID, MODEL_REVISION

    def prof(name, vec):
        v = np.asarray(vec, dtype=np.float32)
        v = v / np.linalg.norm(v)
        return SpeakerProfile(profile_id=f"p-{name}", display_name=name,
                              model_id=MODEL_ID, model_revision=MODEL_REVISION,
                              embedding_dim=EMBEDDING_DIM, centroid=v, sample_count=4)

    base = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    marcus_v = base.copy(); marcus_v[0] = 1.0
    alice_v = base.copy(); alice_v[1] = 1.0

    marcus, alice = prof("Marcus", marcus_v), prof("Alice", alice_v)

    # Speaking exactly like Marcus.
    r = match(marcus_v, [marcus, alice])
    check(r.status == STATUS_KNOWN and r.display_name == "Marcus",
          f"a clear match is known ({r.status}/{r.display_name})")
    check(r.second_best_similarity is not None, "the runner-up is reported too")

    # A stranger: orthogonal to everyone. argmax would still name somebody.
    stranger = base.copy(); stranger[5] = 1.0
    r = match(stranger, [marcus, alice])
    check(r.status == STATUS_UNKNOWN, f"a stranger is unknown, not argmax ({r.status})")

    # Two enrolled speakers, near-identical scores: the brief's .81/.79 case.
    between = (marcus_v + alice_v * 0.97)
    between /= np.linalg.norm(between)
    r = match(between, [marcus, alice])
    check(r.status == STATUS_AMBIGUOUS,
          f"two close candidates are ambiguous, not a coin toss ({r.status}: "
          f"{r.similarity:.3f} vs {r.second_best_similarity:.3f})")

    # Same top score, but the runner-up is nowhere near -> real evidence.
    r = match(marcus_v, [marcus, prof("Bob", base.copy() + np.eye(EMBEDDING_DIM)[7])])
    check(r.status == STATUS_KNOWN, "a distant runner-up leaves a confident match")

    r = match(None, [marcus])
    check(r.status == STATUS_UNAVAILABLE, "no embedding is 'unavailable', not 'unknown'")

    r = match(marcus_v, [])
    check(r.status == STATUS_UNKNOWN, "no enrolled profiles means unknown")


async def test_model_version_incompatibility():
    check.section("embeddings from another model are never compared")
    from core.speaker.matcher import STATUS_UNKNOWN, match
    from core.speaker.registry import SpeakerProfile
    from core.speaker.backend import EMBEDDING_DIM, MODEL_ID

    v = np.eye(EMBEDDING_DIM, dtype=np.float32)[0]
    stale = SpeakerProfile(profile_id="p-old", display_name="Marcus",
                           model_id=MODEL_ID, model_revision="some-older-revision",
                           embedding_dim=EMBEDDING_DIM, centroid=v, sample_count=4)
    check(not stale.compatible, "a profile from another revision is incompatible")
    check(stale.status == "needs_reenrollment",
          f"and reports why ({stale.status})")

    r = match(v, [stale])
    check(r.status == STATUS_UNKNOWN,
          "it is skipped entirely rather than scored — a cross-model number "
          f"would be confident nonsense ({r.status})")

    wrong_dim = SpeakerProfile(profile_id="p-dim", display_name="Alice",
                               model_id=MODEL_ID, embedding_dim=512,
                               centroid=np.ones(512, dtype=np.float32), sample_count=4)
    check(not wrong_dim.compatible, "a different embedding dimension is incompatible")


# ── 2. enrollment quality ────────────────────────────────────────────────────

async def test_enrollment_quality_gates():
    check.section("unusable enrollment audio is rejected before the model sees it")
    from core.speaker.matcher import check_sample

    check(not check_sample(np.zeros(SR * 3, dtype=np.float32), SR).ok, "silence rejected")
    check(not check_sample(VOICE_A[:int(SR * 0.5)], SR).ok, "too short rejected")
    check(not check_sample(np.ones(SR * 3, dtype=np.float32), SR).ok, "clipping rejected")
    check(not check_sample(np.array([], dtype=np.float32), SR).ok, "empty rejected")
    check(not check_sample(np.full(SR * 3, np.nan, dtype=np.float32), SR).ok,
          "malformed (NaN) rejected")
    check(check_sample(VOICE_A, SR).ok, "a normal 3s sample is accepted")


async def test_profile_consistency():
    check.section("a profile built from inconsistent samples is refused")
    from core.speaker.matcher import build_profile_embedding

    def unit(v):
        v = np.asarray(v, dtype=np.float32)
        return v / np.linalg.norm(v)

    dim = 192
    base = unit(np.eye(dim)[0] + 0.01 * np.eye(dim)[1])
    tight = [unit(base + 0.02 * np.eye(dim)[i]) for i in range(2, 7)]

    r = build_profile_embedding(tight)
    check(r.ok, f"consistent samples build a profile ({r.reason})")
    check(r.consistency and r.consistency > 0.9,
          f"with high self-similarity ({r.consistency:.3f})")
    check(abs(np.linalg.norm(r.centroid) - 1.0) < 1e-4, "the centroid is normalised")

    # One sample from a different voice entirely.
    r = build_profile_embedding(tight[:4] + [unit(np.eye(dim)[100])])
    check(r.dropped, f"an obvious outlier is detected ({r.dropped})")

    # Everything disagrees: there is no coherent voice here.
    scattered = [unit(np.eye(dim)[i * 20]) for i in range(5)]
    r = build_profile_embedding(scattered)
    check(not r.ok, f"scattered samples are refused ({r.reason[:60]})")

    r = build_profile_embedding(tight[:2])
    check(not r.ok, f"too few samples are refused ({r.reason[:50]})")


# ── 3. the real model ────────────────────────────────────────────────────────

async def test_embedding_and_separation():
    check.section("the real ECAPA model extracts and separates voices")
    from core.speaker.backend import EMBEDDER, EMBEDDING_DIM

    if not EMBEDDER.warm():
        check(False, "the speaker model could not load — cannot test embeddings")
        return

    e_a = EMBEDDER.embed(VOICE_A, SR)
    check(e_a is not None and e_a.shape == (EMBEDDING_DIM,),
          f"an embedding is {EMBEDDING_DIM}-d ({None if e_a is None else e_a.shape})")
    check(abs(float(np.linalg.norm(e_a)) - 1.0) < 1e-4, "and L2-normalised")

    from core.speaker.matcher import cosine
    e_a2, e_b = EMBEDDER.embed(VOICE_A2, SR), EMBEDDER.embed(VOICE_B, SR)
    same, diff = cosine(e_a, e_a2), cosine(e_a, e_b)
    check(same > diff,
          f"same voice scores above different voice ({same:.3f} > {diff:.3f})")

    real = real_voice()
    if real is not None and len(real) > SR * 4:
        half = len(real) // 2
        r1 = EMBEDDER.embed(real[:half], SR)
        r2 = EMBEDDER.embed(real[half:2 * half], SR)
        rr, rs = cosine(r1, r2), cosine(r1, e_a)
        check(rr > rs,
              f"a REAL human voice matches itself above synthetic ({rr:.3f} > {rs:.3f})")

    check(EMBEDDER.embed(VOICE_A[:int(SR * 0.3)], SR) is None,
          "audio too short to identify returns None rather than guessing")
    check(EMBEDDER.embed(np.array([], dtype=np.float32), SR) is None, "empty returns None")


# ── 4. registry persistence ──────────────────────────────────────────────────

async def test_registry_roundtrip():
    check.section("profiles persist, reload, and delete")
    from core.speaker.registry import SpeakerProfile, SpeakerRegistry, new_profile_id
    from core.speaker.backend import EMBEDDING_DIM, MODEL_ID, MODEL_REVISION

    with tempfile.TemporaryDirectory(prefix="nova-p5-") as td:
        db = Path(td) / "nova.sqlite3"
        reg = SpeakerRegistry(db)
        await reg.initialize()

        v = np.eye(EMBEDDING_DIM, dtype=np.float32)[3]
        pid = new_profile_id()
        await reg.save(SpeakerProfile(
            profile_id=pid, display_name="Marcus", role="owner",
            model_id=MODEL_ID, model_revision=MODEL_REVISION,
            embedding_dim=EMBEDDING_DIM, centroid=v,
            samples=[v, v], sample_count=2, consistency=0.91))

        # A brand new registry object, as after a restart.
        reg2 = SpeakerRegistry(db)
        got = await reg2.get(pid)
        check(got is not None, "the profile survived")
        check(got.display_name == "Marcus" and got.role == "owner", "with its metadata")
        check(got.centroid is not None and np.allclose(got.centroid, v),
              "and its centroid, exactly")
        check(got.compatible, "and it is matchable by this build")
        check(len(got.samples) == 2, "the per-sample embeddings survived too")

        dupes = await reg2.by_name("marcus")
        check(len(dupes) == 1, "lookup by name is case-insensitive")

        await reg2.save(SpeakerProfile(
            profile_id=new_profile_id(), display_name="Marcus",
            embedding_dim=EMBEDDING_DIM, centroid=v, sample_count=3))
        check(len(await reg2.by_name("Marcus")) == 2,
              "a duplicate display name is allowed, not silently merged")

        check(await reg2.delete(pid), "delete reports success")
        check(await reg2.get(pid) is None, "and the profile is gone")
        check(not await reg2.delete(pid), "deleting twice is not an error")

        # A profile whose describe() must never leak the vector.
        described = (await reg2.all())[0].describe()
        check("centroid" not in described and "samples" not in described,
              f"describe() exposes no embedding ({sorted(described)[:4]}…)")


async def test_service_enrol_and_identify():
    check.section("end-to-end: enrol a voice, then identify it")
    from core.speaker.backend import EMBEDDER
    from core.speaker.service import SpeakerService

    if not EMBEDDER.warm():
        check(False, "the speaker model could not load — cannot test enrollment")
        return

    with tempfile.TemporaryDirectory(prefix="nova-p5-svc-") as td:
        svc = SpeakerService(Path(td) / "nova.sqlite3")
        await svc.initialize()

        res = await svc.enrol(display_name="TestVoiceA", role="owner", samples=[
            (VOICE_A, SR), (VOICE_A2, SR), (VOICE_A3, SR), (VOICE_A4, SR)])
        check(res["ok"], f"a 4-sample enrollment succeeds ({res.get('error')})")
        if not res["ok"]:
            return
        check(res["profile"]["sample_count"] >= 3, "several samples were kept")
        check(res["consistency"] > 0.5, f"consistency recorded ({res['consistency']})")

        m = await svc.identify(VOICE_A2, SR)
        check(m.status == "known" and m.display_name == "TestVoiceA",
              f"the enrolled voice is recognised ({m.status}/{m.display_name}, "
              f"sim={m.similarity})")

        m2 = await svc.identify(VOICE_B, SR)
        check(m2.status in ("unknown", "ambiguous"),
              f"a different voice is NOT accepted as the enrolled one ({m2.status}, "
              f"sim={m2.similarity})")

        m3 = await svc.identify(VOICE_A[:int(SR * 0.4)], SR)
        check(m3.status == "too_short",
              f"a fragment is 'too_short', distinct from 'unknown' ({m3.status})")

        # Rejected enrollment: silence.
        bad = await svc.enrol(display_name="Silence",
                              samples=[(np.zeros(SR * 3, dtype=np.float32), SR)] * 4)
        check(not bad["ok"], f"enrolling silence fails ({bad.get('error', '')[:50]})")

        st = await svc.status()
        check(st["profiles"] == 1, f"status reports the profile count ({st['profiles']})")
        check(st["threshold_calibrated"] is False,
              "and states plainly that the threshold is NOT calibrated")
        check("centroid" not in str(st), "status leaks no embedding")


# ── 5. voice-turn integrity ──────────────────────────────────────────────────

async def test_voice_turn_integrity():
    check.section("the client cannot invent an identity")
    from core.speaker.matcher import SpeakerMatch
    from core.speaker.service import SpeakerService, VOICE_TURN_MAX

    with tempfile.TemporaryDirectory(prefix="nova-p5-vt-") as td:
        svc = SpeakerService(Path(td) / "nova.sqlite3")
        m = SpeakerMatch(status="known", profile_id="p-1", display_name="Marcus",
                         similarity=0.88)
        tid = svc.issue_voice_turn(m)
        check(bool(tid), "a classified turn gets a handle")

        back = svc.redeem_voice_turn(tid)
        check(back is not None and back.display_name == "Marcus",
              "which resolves to the BACKEND's identity")

        check(svc.redeem_voice_turn("vt-i-made-this-up") is None,
              "an invented handle resolves to nothing")
        check(svc.redeem_voice_turn(None) is None, "and so does no handle at all")

        # Expiry: the handle is not a session. A FRESH handle is needed here —
        # redemption is single-use since P5.1, so the one above is already
        # consumed. `test_speaker_preflight_v51.py` asserts that directly.
        tid2 = svc.issue_voice_turn(m)
        entry = svc._turns[tid2]
        import time as _t
        svc._turns[tid2] = type(entry)(entry.turn_id, entry.match,
                                       _t.monotonic() - 10_000)
        check(svc.redeem_voice_turn(tid2) is None, "an expired handle is refused")

        for i in range(VOICE_TURN_MAX + 40):
            svc.issue_voice_turn(SpeakerMatch(status="known", profile_id=f"p{i}",
                                              display_name=f"n{i}"))
        check(len(svc._turns) <= VOICE_TURN_MAX,
              f"the cache stays bounded ({len(svc._turns)} <= {VOICE_TURN_MAX})")

        check(svc.issue_voice_turn(SpeakerMatch(status="unavailable")) is None,
              "an unclassified turn mints no handle")


# ── 6. it is not authentication ──────────────────────────────────────────────

async def test_identity_is_not_authorisation():
    check.section("a voice match grants nothing")
    from core.speaker import service as svc_mod
    from core.speaker.matcher import SpeakerMatch

    api = [a for a in dir(svc_mod.SpeakerService) if not a.startswith("_")]
    forbidden = [a for a in api
                 if any(w in a.lower() for w in ("auth", "allow", "permit", "grant",
                                                 "authorise", "authorize"))]
    check(not forbidden,
          f"the service exposes no authorisation-shaped method ({forbidden or 'none'})")

    m = SpeakerMatch(status="known", display_name="Marcus", similarity=0.99)
    rendered = m.for_prompt()
    check("0.99" not in rendered and "similarity" not in rendered.lower(),
          f"prompt context carries identity, not biometric scores ({rendered!r})")
    check("Marcus" in rendered, "but it does name the speaker")

    # The permission machinery is untouched by P5 and still defaults unknown
    # capabilities to ADMIN — a 99% voice match cannot change that. `evaluate`
    # takes a capability and a MODE and nothing else: there is no parameter a
    # speaker match could be threaded through even if someone wanted to.
    import inspect

    from core.permissions import ADMIN, evaluate, tier_of

    check(tier_of("some.unknown.destructive.capability") == ADMIN,
          "unknown capabilities still default to the ADMIN tier")
    check(evaluate("some.unknown.destructive.capability", mode="guarded") == "confirm",
          "and still require confirmation")

    params = set(inspect.signature(evaluate).parameters)
    check(not (params & {"speaker", "speaker_id", "identity", "profile_id", "voice"}),
          f"the permission decision takes no speaker argument ({sorted(params)})")


# ── 7. failure isolation and the disable switch ──────────────────────────────

async def test_failures_degrade():
    check.section("nothing here may break transcription")
    from core.speaker.backend import SpeakerEmbedder
    from core.speaker.service import SpeakerService

    broken = SpeakerEmbedder()
    broken._load_failed = True
    check(broken.embed(VOICE_A, SR) is None, "a model that will not load returns None")
    check(not broken.available, "and reports itself unavailable")
    st = broken.status()
    check(st["load_failed"] is True, "status says so rather than pretending")

    with tempfile.TemporaryDirectory(prefix="nova-p5-fail-") as td:
        svc = SpeakerService(Path(td) / "nova.sqlite3")
        m = await svc.identify(np.array([1.0, 2.0], dtype=np.float32), 0)
        check(m.status in ("unavailable", "too_short"),
              f"malformed audio degrades rather than raising ({m.status})")

        # A corrupt profile row must not poison matching.
        from core.speaker.registry import SpeakerProfile
        corrupt = SpeakerProfile(profile_id="bad", display_name="X",
                                 embedding_dim=192, centroid=None)
        from core.speaker.matcher import match
        r = match(np.eye(192, dtype=np.float32)[0], [corrupt])
        check(r.status == "unknown", f"a corrupt profile is skipped ({r.status})")


async def test_disable_switch():
    check.section("NOVA_SPEAKER_ID=0 does nothing at all")
    from core.speaker.backend import enabled
    from core.speaker.service import SpeakerService

    prev = os.environ.get("NOVA_SPEAKER_ID")
    os.environ["NOVA_SPEAKER_ID"] = "0"
    try:
        check(not enabled(), "the switch reads as off")
        with tempfile.TemporaryDirectory(prefix="nova-p5-off-") as td:
            svc = SpeakerService(Path(td) / "nova.sqlite3")
            before = svc.stats["identify_calls"]
            m = await svc.identify(VOICE_A, SR)
            check(m.status == "unavailable", "identify returns unavailable")
            check(svc.stats["identify_calls"] == before,
                  "and does not even count as an attempt")
            res = await svc.enrol(display_name="X", samples=[(VOICE_A, SR)] * 4)
            check(not res["ok"], "enrollment refuses while disabled")
    finally:
        if prev is None:
            os.environ.pop("NOVA_SPEAKER_ID", None)
        else:
            os.environ["NOVA_SPEAKER_ID"] = prev


async def main():
    await test_open_set_decisions()
    await test_model_version_incompatibility()
    await test_enrollment_quality_gates()
    await test_profile_consistency()
    await test_embedding_and_separation()
    await test_registry_roundtrip()
    await test_service_enrol_and_identify()
    await test_voice_turn_integrity()
    await test_identity_is_not_authorisation()
    await test_failures_degrade()
    await test_disable_switch()
    check.finish()


if __name__ == "__main__":
    run(main)
