"""P5.2 across the SERIALIZATION boundary — real ASGI, real Pydantic (§8).

The bug this file exists to catch has a specific shape: the Python object is
correct, and the HTTP response is missing a field. Nothing raises. Every unit
test still passes. The browser harness silently reads `undefined` and a human
spends forty minutes recording utterances that go nowhere.

It has now happened twice.

  * `SpeakerService.enrol()` returned `sample_count` only inside a nested
    `profile`, so every enrollment reported 0 kept samples over HTTP.
  * `SpeakerMatch.for_response()` emitted `threshold_source` and
    `second_best_profile_id`; `SpeakerInfo` did not declare them, and Pydantic
    drops undeclared keys by default. The model was right, the wire was wrong.

So the assertions here run against responses that have been through FastAPI
routing and Pydantic — not against the objects that feed them.

WHAT IS FAKED, AND WHY IT IS STILL A REAL TEST
----------------------------------------------
Only two things: the 89 MB ECAPA encoder (replaced by a deterministic vector
map) and ffmpeg decoding (replaced by synthetic PCM). Everything the phase is
actually about is real — the router, the service, the registry, the matcher, the
resolved policy, the permission broker, and the Pydantic models.

Run:  venv\\Scripts\\python.exe tests\\test_speaker_api_contract_v52.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")
os.environ.setdefault("NOVA_REPO_ROOT", str(REPO))

import numpy as np  # noqa: E402

from harness import Checks, boot, run  # noqa: E402

check = Checks()

DIM = 192


def _vec(seed: int) -> np.ndarray:
    r = np.random.RandomState(seed)
    v = r.randn(DIM).astype(np.float32)
    return (v / np.linalg.norm(v)).astype(np.float32)


#: Each "recording" is a tag the fake encoder maps to a fixed vector, so trials
#: are deterministic. Real audio never reaches a model in this suite.
VOICES = {"marcus": 1, "guest": 2, "stranger": 3}


def _pcm(tag: str, seconds: float = 3.0, sr: int = 16000):
    """Synthetic PCM that passes `command_quality` (long enough, loud enough)."""
    n = int(seconds * sr)
    r = np.random.RandomState(abs(hash(tag)) % 2**31)
    x = (r.randn(n) * 0.05).astype(np.float32)
    return np.clip(x, -0.9, 0.9), sr


def _install_fakes(nova, jitter: dict[str, float] | None = None):
    """Deterministic encoder + decoder. Returns a setter for the next voice."""
    from core.speaker import backend as B
    from backend.routers import speaker as R

    # `STATE` is a module singleton and `STATE.speaker` outlives a boot(), so
    # without this the second suite in a run keeps a service bound to the first
    # boot's deleted temp database and every enrollment fails to save.
    nova.state.speaker = None

    state = {"tag": "marcus"}
    jitter = jitter or {}

    def fake_embed(audio, sample_rate):          # noqa: ARG001
        tag = state["tag"]
        base = _vec(VOICES.get(tag, 9))
        # Optional pull toward another identity, for a controlled near-miss.
        towards, amount = jitter.get(tag, (None, 0.0)) if tag in jitter else (None, 0.0)
        if towards:
            mix = base + float(amount) * _vec(VOICES[towards])
            return (mix / np.linalg.norm(mix)).astype(np.float32)
        return base

    B.EMBEDDER._model = object()          # warm(): loaded, without loading
    B.EMBEDDER._load_failed = False
    B.EMBEDDER.embed = fake_embed         # type: ignore[method-assign]
    R._decode_to_pcm = lambda raw, suffix=".webm": _pcm(state["tag"])  # type: ignore[assignment]

    def speak_as(tag: str) -> None:
        state["tag"] = tag

    return speak_as


async def _enrol(nova, speak_as, tag: str, name: str, role: str) -> dict:
    speak_as(tag)
    files = [("files", (f"s{i}.webm", b"x" * 64, "audio/webm")) for i in range(6)]
    r = await nova.http.post("/speaker/enroll", data={"display_name": name,
                                                     "role": role}, files=files)
    return r.json()


# ── 1. enrollment ────────────────────────────────────────────────────────────

async def test_enroll_response_survives_http():
    check.section("enrollment: the flat contract crosses the wire")
    async with boot() as nova:
        speak_as = _install_fakes(nova)
        body = await _enrol(nova, speak_as, "marcus", "Marcus", "owner")

        check(body.get("ok") is True, f"enrollment succeeded ({body.get('error')})")
        # The exact keys the router and the browser read at TOP level.
        for key in ("profile_id", "display_name", "role", "sample_count"):
            check(key in body, f"HTTP response carries top-level `{key}`")
        check(isinstance(body.get("profile_id"), str) and body["profile_id"],
              "profile_id is a usable string, not null "
              f"({body.get('profile_id')!r})")
        check(body.get("sample_count", 0) >= 5,
              f"sample_count is the real kept count, not 0 ({body.get('sample_count')})")
        check(body.get("meets_p52_bar") is True,
              "and the 5-of-6 gate can therefore actually pass")
        check(body.get("role") == "owner", "role round-trips")
        check("profile" in body, "the nested shape is still there for old callers")

        # Never in an enrollment response, at any layer. `embedding_dim` is a
        # scalar and legitimately present, so the check is for the VECTORS —
        # a bare "embedding" substring would only catch that field's name.
        import json as _json
        blob = _json.dumps(body).lower()
        for banned in ("centroid", "\"samples\"", "audio", "waveform"):
            check(banned not in blob, f"no {banned} in the enrollment response")
        nested = body.get("profile") or {}
        check("centroid" not in nested and "samples" not in nested,
              "the nested profile describes itself without its vectors")
        floats = [v for v in _json.loads(blob).values() if isinstance(v, list)
                  and len(v) > 32 and all(isinstance(x, (int, float)) for x in v)]
        check(not floats, "no long float array anywhere in the response")


# ── 2. identify: an UNKNOWN keeps its score attribution ──────────────────────

async def test_identify_unknown_retains_top_scored_profile():
    check.section("identify: a rejection still says whose profile scored")
    async with boot() as nova:
        speak_as = _install_fakes(nova)
        m = await _enrol(nova, speak_as, "marcus", "Marcus", "owner")
        marcus_id = m["profile_id"]

        # A stranger: far from Marcus, so the honest answer is `unknown`.
        speak_as("stranger")
        r = await nova.http.post("/speaker/identify",
                                 files={"file": ("t.webm", b"x" * 64, "audio/webm")})
        out = r.json()

        check(out.get("status") == "unknown",
              f"a stranger is UNKNOWN, not the only enrolled person ({out.get('status')})")
        check(out.get("profile_id") is None,
              f"asserted identity is null ({out.get('profile_id')!r})")
        check(out.get("display_name") is None, "and so is the asserted name")
        # THE POINT OF §3.
        check(out.get("top_scored_profile_id") == marcus_id,
              "but the response still says Marcus's profile earned the score "
              f"({out.get('top_scored_profile_id')!r})")
        check(out.get("top_scored_display_name") == "Marcus",
              "with his name, for the human reading the report")
        check(isinstance(out.get("similarity"), (int, float)),
              f"and the score itself survives ({out.get('similarity')})")
        check(out.get("threshold") is not None and out.get("threshold_source"),
              "plus the threshold that rejected it, and where it came from")

        check(out["similarity"] < out["threshold"],
              "the numbers are self-consistent: score below threshold")


async def test_identify_known_agrees_with_itself():
    check.section("identify: for a KNOWN, asserted == top-scored")
    async with boot() as nova:
        speak_as = _install_fakes(nova)
        m = await _enrol(nova, speak_as, "marcus", "Marcus", "owner")

        speak_as("marcus")
        out = (await nova.http.post(
            "/speaker/identify",
            files={"file": ("t.webm", b"x" * 64, "audio/webm")})).json()

        check(out.get("status") == "known", f"his own voice is known ({out.get('status')})")
        check(out.get("profile_id") == m["profile_id"], "asserted identity is his")
        check(out.get("top_scored_profile_id") == out.get("profile_id"),
              "and for a known result the two fields agree, by construction")


async def test_identify_ambiguous_keeps_both_ranks():
    check.section("identify: an AMBIGUOUS keeps top AND runner-up")
    # A guest whose vector is pulled hard toward Marcus: both clear the bar,
    # neither wins by the margin.
    async with boot() as nova:
        speak_as = _install_fakes(nova, jitter={"guest": ("marcus", 6.0)})
        m = await _enrol(nova, speak_as, "marcus", "Marcus", "owner")
        g = await _enrol(nova, speak_as, "guest", "Guest", "guest")

        speak_as("guest")
        out = (await nova.http.post(
            "/speaker/identify",
            files={"file": ("t.webm", b"x" * 64, "audio/webm")})).json()

        if out.get("status") == "ambiguous":
            check(out.get("profile_id") is None, "ambiguous asserts nobody")
            check(out.get("top_scored_profile_id") in (m["profile_id"], g["profile_id"]),
                  "but names the top scorer")
            check(out.get("second_best_profile_id") in (m["profile_id"], g["profile_id"]),
                  "and the runner-up")
            check(out.get("second_best_similarity") is not None,
                  "with the runner-up's score, which the margin fit needs")
        else:
            # Do not fake the condition. Assert the invariant that must hold for
            # whatever the real matcher decided.
            check(out.get("top_scored_profile_id") is not None,
                  f"status {out.get('status')}: a scored result still names its top "
                  "profile")
            check(out.get("second_best_profile_id") is not None,
                  "and its runner-up, since two profiles were scored")


# ── 3. status ────────────────────────────────────────────────────────────────

async def test_status_reports_sources_over_http():
    check.section("status: sources and per-profile effective thresholds")
    async with boot() as nova:
        speak_as = _install_fakes(nova)
        m = await _enrol(nova, speak_as, "marcus", "Marcus", "owner")

        st = (await nova.http.get("/speaker/status")).json()
        for key in ("threshold", "threshold_source", "margin", "margin_source",
                    "threshold_calibrated", "profiles_detail", "raw_audio_retained"):
            check(key in st, f"status carries `{key}`")
        check(st["threshold_calibrated"] is False,
              "uncalibrated is reported as FALSE — no human run has happened")
        check(st["threshold_source"] == "provisional default",
              f"and the source says so ({st['threshold_source']})")
        check(st["raw_audio_retained"] is False, "no raw audio, stated in the response")

        detail = {p["profile_id"]: p for p in st["profiles_detail"]}
        check(m["profile_id"] in detail, "the enrolled profile appears")
        row = detail[m["profile_id"]]
        for key in ("effective_threshold", "stored_threshold", "compatible", "role"):
            check(key in row, f"profiles_detail row carries `{key}`")
        check(row["stored_threshold"] is None,
              "nothing is stored before calibration")

        # `embedding_dim` is a legitimate scalar; the vectors are what must not
        # appear.
        import json as _json
        blob = _json.dumps(st).lower()
        for banned in ("centroid", "\"samples\"", "waveform"):
            check(banned not in blob, f"no {banned} in /speaker/status")
        check(not any(isinstance(v, list) and len(v) > 32
                      and all(isinstance(x, (int, float)) for x in v)
                      for v in st.values()),
              "and no long float array in /speaker/status")


# ── 4. /stt SpeakerInfo serialization ────────────────────────────────────────

async def test_stt_speaker_info_drops_nothing():
    check.section("/stt: SpeakerInfo must not silently drop diagnostics")
    # This is the exact defect: `SpeakerInfo(**match.for_response(...))` is what
    # /stt builds, and undeclared keys vanish there. Asserted against a REAL
    # matcher result rather than a hand-built dict, so a field added to
    # for_response() and forgotten on the model fails here.
    from backend.app import SpeakerInfo, SttResponse
    from core.speaker import matcher as M
    from core.speaker.registry import SpeakerProfile
    import json

    def prof(pid, name, vec):
        return SpeakerProfile(profile_id=pid, display_name=name, centroid=vec,
                              embedding_dim=DIM, sample_count=6)

    P = [prof("p-marcus", "Marcus", _vec(1)), prof("p-guest", "Guest", _vec(2))]

    for label, emb, want_status in (("known", _vec(1), "known"),
                                    ("unknown", _vec(3), "unknown")):
        match = M.match(emb, P, thresh=0.55, min_margin=0.10)
        payload = match.for_response(model_id="test-model")
        info = SpeakerInfo(**payload)
        dumped = info.model_dump()

        lost = sorted(k for k in payload if k not in dumped)
        check(not lost, f"{label}: SpeakerInfo declares every emitted field "
                        f"(lost: {lost})")
        for key in ("threshold_source", "margin", "second_best_profile_id",
                    "second_best_name", "top_scored_profile_id",
                    "top_scored_display_name"):
            check(key in dumped, f"{label}: `{key}` survives the model")

        # Through the OUTER response model and real JSON, as the browser sees it.
        wire = json.loads(SttResponse(text="hi", duration_ms=1, sample_rate=16000,
                                      speaker=info).model_dump_json())
        check(wire["speaker"]["threshold_source"] == payload["threshold_source"],
              f"{label}: threshold_source reaches the wire")
        check(wire["speaker"]["threshold"] == payload["threshold"],
              f"{label}: and it is the threshold that actually decided")

        if want_status == "unknown":
            check(wire["speaker"]["status"] == "unknown", "unknown stays unknown")
            check(wire["speaker"]["profile_id"] is None,
                  "with no asserted identity on the wire")
            # Whichever profile actually ranked first — asserted against the
            # matcher's own answer rather than a guess about which vector wins.
            check(wire["speaker"]["top_scored_profile_id"] == match.top_scored_profile_id
                  and match.top_scored_profile_id in ("p-marcus", "p-guest"),
                  "but the top-scoring profile is retained on the wire "
                  f"({wire['speaker']['top_scored_profile_id']})")
            check(wire["speaker"]["similarity"] is not None,
                  "and so is the score it earned — this is the impostor evidence")

        for banned in ("centroid", "embedding", "samples", "audio"):
            check(banned not in json.dumps(wire).lower(),
                  f"{label}: nothing biometric on the wire ({banned})")


# ── 5. the permission probe ──────────────────────────────────────────────────

async def test_permission_probe_is_real_and_identity_blind():
    check.section("permission probe: the real broker, unmoved by identity")
    async with boot() as nova:
        speak_as = _install_fakes(nova)
        m = await _enrol(nova, speak_as, "marcus", "Marcus", "owner")

        # A. typed reference
        typed = (await nova.http.post("/speaker/permission-probe",
                                      json={"capability": "computer.type"})).json()
        check(typed["capability"] == "computer.type", "probes computer.type")
        check(typed["tier"] == "standard", f"which is STANDARD tier ({typed['tier']})")
        check(typed["decision"] == "needs_confirmation",
              f"guarded mode needs confirmation ({typed['decision']})")
        check(typed["executed"] is False, "and nothing executed")
        check(typed["execution_enabled"] is False, "execution is off")
        check(typed["adapter_installed"] is False, "no OS adapter is installed")
        check(typed["identity"]["source"] == "typed", "identity source is typed")

        # B. a real backend-issued handle for a recognised speaker
        speak_as("marcus")
        stt_like = (await nova.http.post(
            "/speaker/identify",
            files={"file": ("t.webm", b"x" * 64, "audio/webm")})).json()
        check(stt_like["status"] == "known", "the probe voice is recognised")

        # Mint a handle the way /stt does, through the production registry.
        from core.speaker.voice_turns import VOICE_TURNS
        from core.speaker import matcher as M
        svc = nova.state.speaker
        pcm, sr = _pcm("marcus")
        match = await svc.identify(pcm, sr)
        check(match.status == "known" and match.profile_id == m["profile_id"],
              "and identify() agrees it is Marcus")
        handle = VOICE_TURNS.issue(match)

        voice = (await nova.http.post("/speaker/permission-probe",
                                      json={"capability": "computer.type",
                                            "voice_turn_id": handle})).json()
        check(voice["identity"]["source"] == "voice", "identity came from the handle")
        check(voice["identity"]["profile_id"] == m["profile_id"],
              "resolved BACKEND-side to Marcus")
        # THE INVARIANT.
        check(voice["decision"] == typed["decision"],
              f"recognised Marcus gets the SAME decision ({voice['decision']} "
              f"vs {typed['decision']})")
        check(voice["tier"] == typed["tier"], "and the same tier")
        check(voice["executed"] is False, "still nothing executed")

        # C. the handle is single-use, so it cannot be replayed into the probe.
        again = await nova.http.post("/speaker/permission-probe",
                                     json={"capability": "computer.type",
                                           "voice_turn_id": handle})
        check(again.status_code == 400,
              f"a spent voice_turn_id is refused ({again.status_code})")

        # D. the probe cannot be pointed at something dangerous.
        bad = await nova.http.post("/speaker/permission-probe",
                                   json={"capability": "computer.delete"})
        check(bad.status_code == 400,
              f"an unlisted capability is refused ({bad.status_code})")

        # E. nothing is left pending for a human to be prompted about.
        broker = nova.runtime.permission_broker
        check(not broker.pending(),
              f"no confirmation left dangling ({broker.pending()})")


# ── 6. the Step-9 sentinel, through the real turn path ───────────────────────

async def test_sentinel_conversation_state_is_speaker_isolated():
    check.section("step 9: each speaker's own sentinel reaches their own prompt")
    # Human run 2 reported step 9 FAILED with BOTH positives false. Because the
    # privacy negatives are vacuously true when nobody receives anything, that
    # result could not distinguish a memory failure from a detection failure.
    # This pins the production side so the question never has to be re-litigated
    # from a browser run.
    from uuid import uuid4

    from core.speaker.matcher import STATUS_KNOWN, SpeakerMatch
    from core.turn_identity import TurnIdentity, active_turn

    M_S, G_S = "COBALT ORCHARD PINE", "SILVER HARBOR LANTERN"

    async with boot(default_reply="Noted.") as nova:
        cid = uuid4()
        marcus = TurnIdentity.typed()
        guest = TurnIdentity.from_match(
            SpeakerMatch(status=STATUS_KNOWN, profile_id="spk-guest",
                         display_name="Leslie", attempted=True), profile=None)
        stranger = TurnIdentity.voice_unverified("not recognised")

        check(marcus.memory_entity == "user", "Marcus is the owner")
        check(guest.memory_entity == "speaker:spk-guest", "the guest is scoped")
        check(stranger.memory_entity is None, "the stranger owns nothing")

        async def turn(ident, text):
            nova.llm.reset_calls()
            # `identity=` is how backend/app.py passes it, and the runtime enters
            # active_turn() itself. Wrapping the call from OUTSIDE does not work:
            # active_turn(None) resets to typed owner and silently overrides it.
            await nova.brain.chat(text, conversation_id=cid, identity=ident)
            return "\n".join(p for p in nova.llm.prompts if "You are Nova" in p)

        await turn(marcus, f"Remember that my calibration sentinel is {M_S}.")
        await turn(guest, f"Remember that my calibration sentinel is {G_S}.")
        pm = await turn(marcus, "What is my calibration sentinel?")
        pg = await turn(guest, "What is my calibration sentinel?")
        pu = await turn(stranger, "What is my calibration sentinel?")

        check(M_S in pm, "Marcus's ask prompt carries HIS sentinel")
        check(G_S not in pm, "and not the guest's")
        check(G_S in pg, "the guest's ask prompt carries HERS")
        check(M_S not in pg, "and not Marcus's — no leak")
        check(M_S not in pu and G_S not in pu,
              "an unverified speaker's prompt carries neither")

        # The isolation is structural: different storage keys, not filtering.
        store = nova.runtime._state_store
        with active_turn(marcus):
            km, rm = store._key(cid), await store.recent_chat_text(cid)
        with active_turn(guest):
            kg, rg = store._key(cid), await store.recent_chat_text(cid)
        with active_turn(stranger):
            ku, ru = store._key(cid), await store.recent_chat_text(cid)

        check(km != kg, f"owner and guest use different keys ({km} vs {kg})")
        check(str(cid) in km and "speaker:spk-guest" in kg,
              "the guest's key carries their scope")
        check(ku != km and ku != kg,
              f"and an unverified turn gets its own ephemeral key ({ku})")
        check(M_S in rm and G_S not in rm, "owner state holds only his")
        check(G_S in rg and M_S not in rg, "guest state holds only hers")
        check(not ru.strip(), f"unverified state is empty ({ru!r})")


async def test_permission_evaluate_takes_no_identity():
    check.section("permission: evaluate() has no identity parameter at all")
    import inspect

    from core.permissions import PermissionBroker, evaluate

    params = set(inspect.signature(evaluate).parameters)
    check(params == {"capability", "mode"},
          f"evaluate(capability, *, mode) and nothing else ({sorted(params)})")
    req = set(inspect.signature(PermissionBroker.request).parameters)
    check("speaker" not in req and "identity" not in req and "profile_id" not in req,
          f"broker.request takes no identity either ({sorted(req)})")


async def main():
    await test_enroll_response_survives_http()
    await test_identify_unknown_retains_top_scored_profile()
    await test_identify_known_agrees_with_itself()
    await test_identify_ambiguous_keeps_both_ranks()
    await test_status_reports_sources_over_http()
    await test_stt_speaker_info_drops_nothing()
    await test_permission_probe_is_real_and_identity_blind()
    await test_sentinel_conversation_state_is_speaker_isolated()
    await test_permission_evaluate_takes_no_identity()
    check.finish()


if __name__ == "__main__":
    run(main)
