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


# ── 7. profile GENERATION: evidence from one set of centroids is not evidence
#       about another ───────────────────────────────────────────────────────

def _trials_for(m_id, g_id, *, n=14):
    """A mathematically valid, separable two-speaker trial set."""
    rows = []
    for i in range(n):
        rows.append({"truth": m_id, "top_profile_id": m_id,
                     "top_score": 0.86 - i * 0.005, "second_profile_id": g_id,
                     "second_score": 0.12, "status": "known",
                     "condition": "normal", "phase": "B"})
    for i in range(n):
        rows.append({"truth": g_id, "top_profile_id": g_id,
                     "top_score": 0.84 - i * 0.005, "second_profile_id": m_id,
                     "second_score": 0.10, "status": "known",
                     "condition": "normal", "phase": "B"})
    return rows


async def test_stale_generation_calibration_is_refused():
    check.section("a fit for DELETED profiles must not apply, or claim it did")
    # The live failure: 56 trials scored against spk-601053c258fa /
    # spk-c96353f36365, current profiles spk-ccc5aafb945f / spk-4ebf6e6c6135.
    # Every threshold write was skipped by `profiles.get(stale) -> None`, the
    # record was persisted naming the STALE ids, and the API said applied: true.
    OLD_M, OLD_G = "spk-601053c258fa", "spk-c96353f36365"

    async with boot() as nova:
        speak_as = _install_fakes(nova)
        m = await _enrol(nova, speak_as, "marcus", "Marcus", "owner")
        g = await _enrol(nova, speak_as, "guest", "Leslie", "guest")
        NEW_M, NEW_G = m["profile_id"], g["profile_id"]
        check(NEW_M != OLD_M and NEW_G != OLD_G, "the fixture ids really differ")

        stale = _trials_for(OLD_M, OLD_G)

        # PROPOSAL must not look usable.
        prop = (await nova.http.post("/speaker/calibration",
                                     json={"trials": stale, "apply": False})).json()
        check(prop.get("generation_ok") is False,
              "a proposal on stale evidence is flagged, not presented as valid")
        check(prop.get("ok") is False,
              f"and is not reported as a good fit ({prop.get('ok')})")
        check(any("stale profile generation" in p
                  for p in prop.get("generation_problems", [])),
              f"naming the problem ({prop.get('generation_problems')})")
        check(OLD_M in str(prop.get("generation_problems"))
              and NEW_M in str(prop.get("current_profile_ids")),
              "showing BOTH the stale ids and the current ones")

        # APPLY must be refused outright.
        r = await nova.http.post("/speaker/calibration",
                                 json={"trials": stale, "apply": True})
        check(r.status_code in (400, 409),
              f"apply is refused ({r.status_code})")
        check("stale profile generation" in r.text,
              f"with a diagnostic ({r.text[:140]})")

        # NOTHING was written.
        st = (await nova.http.get("/speaker/status")).json()
        detail = {p["profile_id"]: p for p in st["profiles_detail"]}
        check(detail[NEW_M]["stored_threshold"] is None
              and detail[NEW_G]["stored_threshold"] is None,
              "no profile threshold was written")
        check(st["threshold_calibrated"] is False,
              "status still reports uncalibrated")
        cal = (await nova.http.get("/speaker/calibration")).json()
        check(cal["record_present"] is False,
              "and NO calibration record was created")
        check(cal["calibrated"] is False, "so nothing claims to be calibrated")


async def test_calibration_endpoint_distinguishes_record_from_usable():
    check.section("a record that exists is not a calibration that applies")
    async with boot() as nova:
        speak_as = _install_fakes(nova)
        m = await _enrol(nova, speak_as, "marcus", "Marcus", "owner")
        g = await _enrol(nova, speak_as, "guest", "Leslie", "guest")
        NEW_M, NEW_G = m["profile_id"], g["profile_id"]

        # Plant a stale record directly, the way the old buggy apply would have.
        from core.speaker.calibration import CalibrationRecord, CalibrationStore
        db = nova.state.speaker.registry._db_path
        await CalibrationStore(db).save(CalibrationRecord(
            margin=0.29, profile_ids=["spk-601053c258fa", "spk-c96353f36365"],
            metrics={}))

        cal = (await nova.http.get("/speaker/calibration")).json()
        check(cal["record_present"] is True, "the record is reported as present")
        check(cal["valid_for_build"] is True, "and valid for this model build")
        check(cal["covers_current_profiles"] is False,
              "but it does NOT cover the current profiles")
        check(cal["effective"] is False and cal["calibrated"] is False,
              "so `calibrated` is FALSE — the operationally useful answer")
        check("stale_reason" in cal, f"with an explanation ({cal.get('stale_reason')})")
        check(sorted(cal["current_profile_ids"]) == sorted([NEW_M, NEW_G]),
              "showing the current ids")

        # And it agrees with /speaker/status, which was the contradiction.
        st = (await nova.http.get("/speaker/status")).json()
        check(st["threshold_calibrated"] == cal["calibrated"],
              f"/calibration and /status agree ({cal['calibrated']} / "
              f"{st['threshold_calibrated']})")
        check(st["threshold_source"] == "provisional default",
              "the runtime falls back to provisional")

        # Reading it must NOT delete the evidence.
        again = (await nova.http.get("/speaker/calibration")).json()
        check(again["record_present"] is True,
              "a stale record survives being read — it is history, not litter")


async def test_current_generation_applies_and_survives_restart():
    check.section("a CURRENT-generation fit applies, and reloads after restart")
    async with boot() as nova:
        speak_as = _install_fakes(nova)
        m = await _enrol(nova, speak_as, "marcus", "Marcus", "owner")
        g = await _enrol(nova, speak_as, "guest", "Leslie", "guest")
        M, G = m["profile_id"], g["profile_id"]

        r = await nova.http.post("/speaker/calibration",
                                 json={"trials": _trials_for(M, G), "apply": True})
        body = r.json()
        check(r.status_code == 200 and body.get("applied") is True,
              f"the current generation applies ({r.status_code}, {body.get('reason')})")
        check(body.get("generation_ok") is True, "with a clean generation check")

        st = (await nova.http.get("/speaker/status")).json()
        detail = {p["profile_id"]: p for p in st["profiles_detail"]}
        check(detail[M]["stored_threshold"] is not None
              and detail[G]["stored_threshold"] is not None,
              "both thresholds persisted")
        check(st["threshold_calibrated"] is True, "status says calibrated")
        check(st["threshold_source"] == "calibrated"
              and st["margin_source"] == "calibrated",
              f"and BOTH sources are calibrated ({st['threshold_source']} / "
              f"{st['margin_source']})")
        saved = {M: detail[M]["stored_threshold"], G: detail[G]["stored_threshold"]}

        # RESTART. The original defect only surfaced after a reboot, so the
        # policy has to be proven to survive one.
        db = nova.state.speaker.registry._db_path
        from core.speaker.service import SpeakerService
        fresh = SpeakerService(db)
        await fresh.initialize()
        st2 = await fresh.status()
        check(st2["threshold_calibrated"] is True,
              "a freshly constructed service reloads the calibration")
        check(st2["threshold_source"] == "calibrated"
              and st2["margin_source"] == "calibrated",
              f"with both sources still calibrated ({st2['threshold_source']} / "
              f"{st2['margin_source']})")
        d2 = {p["profile_id"]: p for p in st2["profiles_detail"]}
        for pid, val in saved.items():
            check(abs(d2[pid]["effective_threshold"] - val) < 1e-9,
                  f"{pid} still judged by {val} after restart "
                  f"({d2[pid]['effective_threshold']})")


async def test_enrolment_invalidates_and_stale_cannot_be_recreated():
    check.section("enrol/delete invalidates; stale evidence cannot resurrect it")
    async with boot() as nova:
        speak_as = _install_fakes(nova)
        m = await _enrol(nova, speak_as, "marcus", "Marcus", "owner")
        g = await _enrol(nova, speak_as, "guest", "Leslie", "guest")
        M, G = m["profile_id"], g["profile_id"]
        await nova.http.post("/speaker/calibration",
                             json={"trials": _trials_for(M, G), "apply": True})
        check((await nova.http.get("/speaker/status")).json()["threshold_calibrated"],
              "baseline: calibrated")

        # Replace the guest, the way the protocol says: DELETE the stale profile
        # deliberately, then enrol again. (Enrolling without deleting leaves
        # three compatible profiles, and a two-profile fit correctly fails to
        # cover them — checked at the end.)
        d = await nova.http.delete(f"/speaker/profiles/{G}")
        check(d.status_code == 200, f"the old guest profile is deleted ({d.status_code})")
        g2 = await _enrol(nova, speak_as, "guest", "Leslie 2", "guest")
        G2 = g2["profile_id"]
        check(G2 != G, "re-enrolment produced a new profile id")

        st = (await nova.http.get("/speaker/status")).json()
        check(st["threshold_calibrated"] is False,
              "the calibration is no longer usable")
        check(st["threshold_source"] == "provisional default",
              f"and the runtime falls closed ({st['threshold_source']})")

        # The OLD trials must not be able to rebuild the old record.
        r = await nova.http.post("/speaker/calibration",
                                 json={"trials": _trials_for(M, G), "apply": True})
        check(r.status_code in (400, 409),
              f"stale trials cannot recreate a calibration ({r.status_code})")
        cal = (await nova.http.get("/speaker/calibration")).json()
        check(cal["calibrated"] is False,
              "and nothing usable exists afterwards")

        # A fresh fit for the CURRENT generation works.
        r2 = await nova.http.post("/speaker/calibration",
                                  json={"trials": _trials_for(M, G2), "apply": True})
        check(r2.status_code == 200 and r2.json().get("applied") is True,
              f"a current-generation fit applies ({r2.status_code})")
        st2 = (await nova.http.get("/speaker/status")).json()
        check(st2["threshold_calibrated"] is True
              and st2["threshold_source"] == "calibrated",
              "and the runtime is calibrated again")

        # A THIRD profile must invalidate it again, and a two-profile fit must
        # not cover a three-profile population.
        speak_as("stranger")
        g3 = await _enrol(nova, speak_as, "stranger", "Third", "guest")
        st3 = (await nova.http.get("/speaker/status")).json()
        check(st3["threshold_calibrated"] is False,
              "a third enrolment invalidates the calibration")
        r3 = await nova.http.post("/speaker/calibration",
                                  json={"trials": _trials_for(M, G2), "apply": True})
        check(r3.status_code == 409,
              f"and a two-profile fit no longer covers the population "
              f"({r3.status_code})")
        check("does not cover every current profile" in r3.text,
              f"saying which profile is uncovered ({r3.text[:120]})")


async def test_apply_is_atomic_when_persistence_fails():
    check.section("a failed write leaves NO half-calibrated state")
    async with boot() as nova:
        speak_as = _install_fakes(nova)
        m = await _enrol(nova, speak_as, "marcus", "Marcus", "owner")
        g = await _enrol(nova, speak_as, "guest", "Leslie", "guest")
        M, G = m["profile_id"], g["profile_id"]
        svc = nova.state.speaker

        # Break the SECOND persistence step: thresholds save, the record does not.
        original = svc.calib.save
        async def boom(_rec):
            raise RuntimeError("simulated disk failure")
        svc.calib.save = boom          # type: ignore[assignment]
        try:
            r = await nova.http.post("/speaker/calibration",
                                     json={"trials": _trials_for(M, G), "apply": True})
        finally:
            svc.calib.save = original  # type: ignore[assignment]

        check(r.status_code == 500, f"the failure surfaces ({r.status_code})")
        check("applied" not in r.text or '"applied":true' not in r.text.replace(" ", ""),
              "and applied is NOT reported true")

        st = (await nova.http.get("/speaker/status")).json()
        detail = {p["profile_id"]: p for p in st["profiles_detail"]}
        check(detail[M]["stored_threshold"] is None
              and detail[G]["stored_threshold"] is None,
              f"both thresholds were rolled back "
              f"({detail[M]['stored_threshold']}, {detail[G]['stored_threshold']})")
        check(st["threshold_calibrated"] is False,
              "and nothing claims to be calibrated")
        cal = (await nova.http.get("/speaker/calibration")).json()
        check(cal["record_present"] is False, "no record was left behind")


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
    await test_stale_generation_calibration_is_refused()
    await test_calibration_endpoint_distinguishes_record_from_usable()
    await test_current_generation_applies_and_survives_restart()
    await test_enrolment_invalidates_and_stale_cannot_be_recreated()
    await test_apply_is_atomic_when_persistence_fails()
    await test_permission_evaluate_takes_no_identity()
    check.finish()


if __name__ == "__main__":
    run(main)
