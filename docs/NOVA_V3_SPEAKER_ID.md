# Nova V3 P5 — Local speaker identity

Nova can tell who is speaking, locally, without shipping a voice anywhere.

**This document describes what is built and what is not.** P5 as specified is a
large phase.

| | |
|---|---|
| **P5 part 1** | the measured backend decision, the identification subsystem, `/stt` integration |
| **P5.1 (this pass)** | four pre-flight defects in part 1, each reproduced before being fixed |
| **P5.1a** | two remaining unverified-voice holes, both reproduced first |
| **P5.1b** | the same invariant when the speaker *service itself* fails |
| **P5.1 main** | live turn carriage, write isolation, read privacy, correction enforcement |

The scope table at the end says exactly what remains, and nothing here is
reported as finished when it is not.

---

## P5.1 — four defects part 1 shipped

Each was verified against the code before anything changed. All four reproduced.

**1. The model revision was metadata-only.** Part 1 stamped
`0f99f2d0…` into every profile and used it to decide compatibility, while
`from_hparams` was called with no revision at all — so Nova loaded whatever HEAD
happened to be and asserted a commit she had never requested. Had upstream
republished the weights, she would have compared new embeddings against old
centroids, confidently, forever. Now pinned through SpeechBrain's supported
`fetch_config=FetchConfig(revision=…)`, using the *same constant* profiles are
stamped with, and `status()` reports `revision_pinned` as an observation of a
real load rather than a claim.

**2. Ordinary identification had no quality gate.** Enrollment rejected silence,
clipping and fragments; `identify()` rejected nothing but length, so a
long-enough stretch of near-silence was embedded and scored against profiles
like any other audio. An empty room could return a match.

`command_quality()` now gates before the model — and deliberately at a *lower*
bar than enrollment. Enrollment can demand 1.5 s of clean speech because it
happens once and can ask for a retake; a real command is often "stop" or "yes",
and rejecting those to guard against silence would break normal use to fix a
problem only silence causes. Measured: 1.1 s / 1.5 s / 3.0 s commands accepted,
a 0.4 s fragment `too_short`, digital and near-silence refused, quiet-but-real
speech accepted.

**3. Voice-turn redemption was replayable.** `redeem_voice_turn` returned the
match and left the handle in the cache, so one captured id could assert the same
identity on every later turn within its 300 s TTL. Redemption now consumes the
handle: one `/stt` classification backs exactly one chat turn.

An identity *failure* still mints a handle — `unknown`, `ambiguous` and
`too_short` redeem back as themselves. **`unavailable` did not**, which P5.1's
report described as though it did; that gap is fixed in P5.1a below.

**4. The privacy setting did nothing.** `NOVA_SPEAKER_KEEP_AUDIO` promised
control over raw recordings that were never written, and the expression guarding
the derived embeddings read `keep_audio() or True` — so the flag changed
nothing, in either direction. A privacy setting that does nothing is worse than
no setting, because someone will rely on it. Removed rather than given a meaning
it never had. Raw audio is never retained; the derived embeddings are, and a
test asserts no audio file of any format is written during enrollment.

---

## P5.1a — two remaining ways a voice turn could lose its state

Both reproduced on `d9ecc5a` before anything changed.

### Hole 1: `unavailable` was the one outcome with no handle

`issue_voice_turn` refused `status == unavailable`, so four outcomes carried
structured evidence that a voice command had happened and a classifier failure
carried none. Once attribution is wired, **"no speaker metadata" is exactly the
state that would be read as typed-Marcus** — so the failure case is the one that
most needs the handle.

The fix is a distinction the code did not previously have. `SpeakerMatch` now
records whether identification was **attempted**:

| | meaning | handle? |
|---|---|---|
| `attempted=False` | the feature is **off**. Legacy Nova. No speaker question is being asked. | no |
| `attempted=True` | the feature is **on** and Nova tried. Every outcome — including `unavailable` — is a real backend-derived voice-turn result. | yes |

This is what keeps *"we could not tell"* distinguishable from *"nobody asked"*.
Removing the `unavailable` check alone would have made `NOVA_SPEAKER_ID=0` mint
handles and quietly turn every legacy voice turn into an unverified guest turn —
the opposite mistake, and just as wrong.

**Handle semantics, all outcomes:**

| outcome | handle | single-use | expires |
|---|---|---|---|
| `known` | yes | yes | yes |
| `unknown` | yes | yes | yes |
| `ambiguous` | yes | yes | yes |
| `too_short` | yes | yes | yes |
| `unavailable` (attempted) | **yes** | yes | yes |
| disabled (`attempted=False`) | **no** | — | — |

### Hole 2: an empty transcript was still classified

`/stt` called the speaker path whenever `identify_speaker` was set, with no
reference to `result.empty`. A buffer can carry enough energy to clear
`command_quality()` while Whisper returns nothing — so **background noise could
come back as a `known` speaker for a turn containing no words**.

An empty result now short-circuits the model entirely:

* **zero** embedding calls — asserted by counting them, not by timing
* status `unavailable`, reason `empty_transcript`
* no profile, no similarity, never `known`
* still `attempted=True` with a handle, so the turn keeps its unverified voice
  state rather than looking like typed input

No second decode, no second upload, no second Whisper call. The guard is about
the *transcript*, not the audio: the identical buffer treated as a real
utterance still classifies, exactly once.

`SpeakerInfo` gained `reason` and `attempted` — structured diagnostics that
distinguish `empty_transcript` from `disabled` from `no embedding`, and expose
nothing biometric.

### Call counts, asserted

| path | ffmpeg | Whisper | ECAPA |
|---|---|---|---|
| voice command | 1 | 1 | 1 |
| empty transcript | 1 | 1 | **0** |
| wake chunk | 1 | 1 | **0** |
| typed chat | 0 | 0 | **0** |
| speaker disabled | 1 | 1 | **0** (no model load) |

---

## P5.1b — when the subsystem itself fails

P5.1a made every *classification* outcome preserve its voice-turn state. It did
not cover the case where there is nothing to classify with. Reproduced on
`335d31a`, feature **enabled**, real command:

| situation | status | attempted | handle |
|---|---|---|---|
| `SpeakerService` cannot be constructed | `unavailable` | **False** | none |
| unexpected exception in the helper | `unavailable` | **False** | none |
| `NOVA_SPEAKER_ID=0` (disabled) | `unavailable` | False | none |

The first two were byte-for-byte the third in every field a consumer would read.
Only the `reason` string differed — and no attribution logic should ever have to
parse prose to decide whether personal memory may be written.

### The final semantics

```
disabled      nobody asked a speaker question.  Legacy Nova, typed semantics.
unavailable   the question WAS asked and could not be answered.
              Unverified voice — personal memory must not be written to Marcus.
```

`attempted` is the discriminator, and it is a boolean rather than a string
comparison. **A subsystem that fails must not be able to erase the evidence that
it was supposed to run**, because "no speaker metadata" is exactly what would
later read as typed-Marcus. Failures fail closed, toward *unverified*, never
toward an identity.

### Architecture: one cache, moved — not a second one

The handle cache lived inside `SpeakerService`, so it died with it. It has no
dependency on ECAPA, on SQLite, or on the registry — it is a dict of small
metadata records, and its placement was the whole bug.

`core/speaker/voice_turns.py` now owns it, process-wide, and `SpeakerService`
delegates. That is the same single cache in a place where it survives its
consumer, rather than a fallback cache with parallel semantics to keep in sync.
`core/voice/turn.py`'s `TurnRegistry` was deliberately not reused: it governs
live execution turns, cancellation and barge-in, with a different lifetime, and
coupling barge-in cancellation to speaker metadata would be a worse trade.

Every handle keeps its contract: opaque, backend-derived, single-use, expiring,
bounded, metadata-only, never authorisation.

### Behaviour now

| situation | status | attempted | handle |
|---|---|---|---|
| service cannot be constructed | `unavailable` | **True** | **yes** |
| unexpected exception | `unavailable` | **True** | **yes** |
| disabled | `unavailable` | False | no |
| disabled **and** service missing | `unavailable` | False | no |

The last row matters: a service failure while the feature is off is still legacy
Nova, not a guest turn.

No additional decode, upload, Whisper call or embedding. The failure paths reach
the model zero times.

---

## P5.1 main body — identity now changes what Nova does

The failure this exists to prevent, stated plainly:

> A guest says *"my name is Alex"* and Nova rewrites **Marcus's** name.

Until now `entity="user"` meant Marcus, because only Marcus could speak.

### The attribution matrix

| turn | personal-memory entity |
|---|---|
| typed | `user` (legacy) |
| voice, speaker ID **disabled** | `user` (legacy — nobody asked) |
| voice, known profile with role `owner` | `user` |
| voice, known **non-owner** | `speaker:<profile_id>` |
| voice, unknown | **none** |
| voice, ambiguous | **none** |
| voice, too_short | **none** |
| voice, unavailable | **none** |
| voice, handle missing / invalid / expired / **replayed** | **none** |

`memory_entity` returns `None` for anything Nova could not attribute, and **None
is never substituted for a default**. Every failure lands on the safe side.

`stored_role` comes from the durable enrolled profile, never the request — a
client asserting `role=owner`, or a profile merely *named* "Marcus", does not get
the owner namespace.

### Two boundaries, not one

**WRITE.** `_extract_quick_facts` and the name-replacement helper consult turn
identity before every write. Name replacement is gated separately because it
*purges* first: a guest reaching it with the owner entity would have deleted
Marcus's name outright.

**READ.** Blocking writes alone would be half a boundary — reciting Marcus's
family, spouse and children to whoever is standing there leaks his profile just
as thoroughly. Grounding loads his personal context only for a turn that is
actually his. A known guest gets **their own** stored profile instead; an
unrecognised speaker gets none. Shared context (date, tools, capabilities) stays
for everyone, because it is not personal.

### `memory.correct` is enforced in code

The tool defaults to `entity="user"`. A guest saying *"no, my GPU is a 4090"*
would have superseded his. The model is **not** asked to pick a safe entity — it
does not know who is in the room, and making a safety boundary probabilistic is
the mistake. Personal corrections are redirected to the speaker's own namespace,
and an unverified speaker is refused with a result Nova can explain. Corrections
about explicitly named third parties are untouched.

### Carriage and concurrency

Identity is scoped in `chat_turn_stream`'s wrapper — one choke point, so every
early return inside (project prepass, direct replies, storytelling, error paths)
keeps it. A `ContextVar` rather than an attribute: concurrent turns each keep
their own view, verified by a test running four speakers in parallel, and the
`finally` guarantees background work never inherits a stale human.

### Still not authentication

Every speaker — typed, owner, guest, unknown — receives the **identical**
`PermissionBroker` decision, asserted by test. `evaluate()` takes no identity
argument. Owner role is a memory-routing label, not authority.

---

## The one rule that outranks the rest

**Speaker identity is not authentication.**

A voice match may personalise a reply and attribute a memory. It may never grant
a capability that `PermissionBroker` would otherwise confirm or deny. Marcus
recognised at 0.99 similarity still gets asked before a destructive action.

This is enforced structurally, not by convention:

* `SpeakerService` exposes no method shaped like authorisation — no `allow`,
  `permit`, `grant`, `authorise`. A test asserts that.
* `permissions.evaluate()` takes a capability and a mode. There is no parameter a
  speaker match could be threaded through even if someone wanted to. A test
  asserts that too.
* Unknown capabilities still default to `ADMIN`.

The reason is simple: a voice is trivially recordable and increasingly
synthesisable. **P5 makes no anti-spoofing claim of any kind** — it does not
detect a recording of Marcus, a cloned voice, or a replay. Speaker verification
and liveness detection are different problems, and conflating them would build a
security boundary out of something that is not one.

Where identity *does* matter is a correctness and privacy boundary: an
unrecognised speaker must not have their statements written into Marcus's
personal memory. That is not the same claim as "Marcus's data is secure because
Nova knows his voice", and this document does not make the second one.

---

## Model choice — measured, not assumed

`speechbrain/spkrec-ecapa-voxceleb` (ECAPA-TDNN).

| | |
|---|---|
| revision | `0f99f2d0ebe89ac095bcc5903c4dd8f72b367286` |
| parameters | 20.8 M |
| on disk | 89 MB |
| embedding dimension | 192 |
| load time | 4.5 s cold (with download), 0.2 s warm |
| RSS | +222 MB CPU / +126 MB CUDA |

**Dependency impact was checked before installing anything.** The hard
constraint is that Nova's working Torch/CUDA/XTTS/STT stack must not be
disturbed to satisfy a speaker library, so `pip install --dry-run speechbrain`
came first:

```
Would install HyperPyYAML-1.2.3 ruamel.yaml-0.18.17
              ruamel.yaml.clib-0.2.15 sentencepiece-0.2.2 speechbrain-1.1.0
```

torch 2.11.0+cu128, torchaudio 2.11.0 and numpy 1.26.4 all reported *already
satisfied*. No downgrade, no CUDA change. A candidate that had demanded a torch
downgrade would have been rejected on that evidence alone — which is the whole
reason the dry run happened before the install.

**NVIDIA NeMo TitaNet was not benchmarked.** `nemo_toolkit` pulls a large
dependency tree, and the brief is explicit that a huge tree must not be imported
merely to complete a comparison. ECAPA met every requirement with a five-package
footprint, so the second candidate was not worth the risk to a working
environment. That is a decision made on cost, and it is recorded rather than
presented as a finished comparison.

## CPU, deliberately — and this is the measurement that decided it

| | 3 s of audio |
|---|---|
| CPU | **41–58 ms** |
| CUDA | 5.7 ms (+90 MB VRAM) |
| **CPU, while the GPU is saturated** | **46 ms — unchanged** |

CUDA is ~7× faster in isolation, and that is not the deciding number.

V3 P1 measured what a third CUDA consumer costs on this machine while the 9B
model generates: **whisper +185%, XTTS +209%**. Speaker ID would be that third
consumer, on the card that D1 records aborting the backend when pushed. Against
that, the CPU path is *provably indifferent* to GPU load — 57.9 ms idle, 46.3 ms
with the GPU pinned by a matmul loop — and 41 ms sits inside an `/stt` request
that already spends hundreds of milliseconds on ffmpeg and Whisper.

So CPU is the choice, not a fallback. `NOVA_SPEAKER_DEVICE=cuda` exists for
anyone who wants to re-measure the trade on different hardware.

## Does it actually separate voices?

Measured on the fixtures available offline:

| pair | cosine |
|---|---:|
| real human voice, segment 1 vs segment 2 | **+0.784** |
| synthetic voice A vs A′ | +0.979 |
| real human vs synthetic B | −0.034 |
| real human vs synthetic C | −0.045 |
| synthetic B vs synthetic C | +0.472 |

`min(same) = 0.784` versus `max(different) = 0.472`. Separated, with margin.

**What this does not prove:** that Nova recognises Marcus. The "real human voice"
is one speaker (the XTTS reference in `voices/nova.wav`), and the contrasts are
synthetic — a controlled contrast, not a second person. Real accuracy needs real
people, which is what `tests/live_speaker_id_harness.md` collects.

---

## Architecture

```
/stt upload
   │
   ├── ffmpeg  ──►  mono float32 PCM @ 16 kHz     ← decoded ONCE
   │                      │
   │                      ├──►  faster-whisper  ──►  text
   │                      │
   │                      └──►  ECAPA (CPU)     ──►  192-d embedding   [opt-in]
   │                                                      │
   │                                            registry ─┴─ matcher
   │                                                      │
   └──────────────────────────────────────────►  SpeakerInfo + voice_turn_id
```

One decode. The PCM Whisper already receives is handed back from the ASR thread
and reused — a second `ffmpeg` invocation, or a second upload of the same audio,
would double the most expensive part of the path to recompute bytes already in
memory.

Responsibilities stay separate so `/stt` never grows enrollment logic:

| module | job |
|---|---|
| `core/speaker/backend.py` | PCM → normalised embedding; lazy load; never raises |
| `core/speaker/registry.py` | durable profiles, model-compatibility, delete |
| `core/speaker/matcher.py` | open-set decision + enrollment quality rules |
| `core/speaker/service.py` | the façade the rest of Nova talks to |

## Opt-in per request — the wake loop stays free

Nova's fallback wake system calls `/stt` continuously on short chunks while
waiting for "Hey Nova". Embedding every one of those would burn CPU forever to
identify the speaker of a word that is about to be discarded.

So speaker identification is **opt-in per request**: `POST /stt` with
`speaker=true`. Default off.

* wake chunk → transcribe only, **zero** embedding calls
* real command → transcribe **and** identify
* typed chat → no speaker inference at all

## Open-set matching — why not `argmax`

`argmax(scores)` always names somebody. Point a stranger at a registry with one
enrolled speaker and the stranger *is* that speaker, at whatever score falls out.

The decision uses the top score, a threshold, **and** the margin to the
runner-up:

| | |
|---|---|
| Marcus .81, Alice .79 | passes a threshold and means nothing → **ambiguous** |
| Marcus .86, Alice .31 | passes, runner-up nowhere near → **known** |

Five outcomes, deliberately distinct, because "I don't know", "nobody I know"
and "the model isn't running" are different things a caller may want to handle
differently:

`known` · `unknown` · `ambiguous` · `too_short` · `unavailable`

**Thresholds are provisional** — 0.55 similarity, 0.10 margin, chosen from the
separation measured above. They are starting points for the live harness, not
calibrated values, and `SpeakerService.status()` reports
`threshold_calibrated: false` rather than implying otherwise.

The bias is deliberate and should survive calibration: **a guest wrongly
accepted as Marcus is much worse than an honest `unknown`.**

## Enrollment

Several samples, not one. A single recording encodes whatever the voice was
doing in those three seconds, and matching then depends on repeating that mood.

Rejected before the model ever sees them: silence, clipping, too short, empty,
malformed. Cheap deterministic signal checks — no VAD model, no LLM.

Consistency is then checked with **leave-one-out similarity**: each sample
against the centroid of the others, which catches the sample that does not belong
without being fooled by its own contribution to the mean. Obvious outliers are
dropped; a profile whose samples fundamentally disagree is refused with an
explanation written for the person recording it.

The profile stores a normalised centroid plus the individual embeddings, so a
future recalibration need not ask for six new recordings.

## Storage and privacy

Profiles live in Nova's existing SQLite database — the same reasoning D5 gave for
episodes.

**Raw enrollment audio is not stored.** Samples are embedded and discarded.
`NOVA_SPEAKER_KEEP_AUDIO=1` exists for someone deliberately building a
calibration set and is off by default: a voice recording is among the most
personal things Nova could hold, and P5 does not need it after enrollment.

Deleting a profile removes its embeddings. That is what a delete is for.

Nothing exposes an embedding: `describe()` returns metadata, `/stt` returns
metadata, `/status` returns counters. Tests assert it.

## Model-version compatibility

Every profile records `model_id`, `model_revision` and `embedding_dim`.
Embeddings from different models do not share a vector space, so comparing them
is not "slightly less accurate" — it is meaningless, and it would be meaningless
*confidently*. A profile whose model no longer matches reports
`needs_reenrollment` and is **skipped entirely** during matching rather than
scored badly.

## Integrity: the client cannot claim to be Marcus

The browser must not be able to send `"speaker": "Marcus"` and be believed.
Identity is derived on the backend, so it stays there:

1. `/stt` classifies and mints a short-lived `voice_turn_id`.
2. The frontend carries that opaque handle.
3. The backend resolves identity from a bounded, expiring cache.

TTL 300 s, at most 256 entries, expired handles refused, invented handles
resolve to nothing. **This is not authentication** — it protects the integrity of
speaker metadata and grants nothing.

## Failure behaviour

Speaker ID is enrichment and may never break voice interaction. Whisper
succeeding while the request returns HTTP 500 because an optional biometric
model misbehaved would be a self-inflicted outage.

| failure | result |
|---|---|
| model will not load | `status: unavailable`, transcription unaffected |
| embedding raises | `unavailable`, counted |
| corrupt profile | skipped, other profiles still matched |
| audio too short | `too_short` — distinct from `unknown` |
| `NOVA_SPEAKER_ID=0` | no model load, no embedding, `/stt` exactly as before |

## Barge-in is untouched

P0's acoustic barge-in and echo classification were **not modified**. Barge-in
audio can contain Marcus, Nova's own TTS leakage, or both — a different input
distribution from a clean command, and letting it label Nova's TTS as a guest (or
reject Marcus) would corrupt identity for no gain. Speaker ID applies to command
audio; the clean utterance after playback stops is the one worth classifying.

**Wake behaviour is also unchanged.** Anybody may still wake Nova. Restricting
the wake word to Marcus is not a P5 default and should not be considered before
the live harness produces real false-reject numbers.

---

## Scope: what is built, and what is not

Reported honestly rather than as a completed phase.

### Built and tested

* measured model + device decision (the gating architectural work)
* `core/speaker/` — backend, registry, matcher, enrollment, service
* durable profiles with model-compatibility gating; delete removes embeddings
* open-set matching with threshold + margin; five distinct outcomes
* enrollment quality gates and leave-one-out consistency
* `/stt` additive `speaker` field, opt-in per request, **single decode**
* `voice_turn_id` integrity handle: bounded, expiring, unforgeable
* failure isolation and `NOVA_SPEAKER_ID=0`
* 11 test groups (`tests/test_speaker_id_v5.py`), all passing
* `tests/live_speaker_id_harness.md`

### Fixed in P5.1

* model revision genuinely pinned at load, not just recorded
* command-audio quality gate — silence can no longer become `known`
* one-time voice-turn redemption
* the dead `NOVA_SPEAKER_KEEP_AUDIO` setting removed

### Fixed in P5.1a

* every *attempted* outcome preserves a voice-turn handle, `unavailable` included
* disabled mode stays legacy — it mints nothing and loads no model
* an empty transcript can never become `known`, and costs zero embedding calls

### Fixed in P5.1b

* a `SpeakerService` that cannot be constructed still yields an unverified
  *voice* turn, not a disabled-looking one
* the same for any unexpected fault in the `/stt` speaker helper
* the handle cache moved out of `SpeakerService` so it outlives it

### NOT built — do not assume these work

* **Enrollment HTTP endpoints and frontend UX.** The service can enrol; nothing
  exposes it over HTTP or in the UI yet, so enrollment is currently programmatic.
* **Frontend `transcribeBlobDetailed()` wiring.** `/stt` returns speaker metadata;
  the frontend still collapses the response to a string, so the metadata is
  discarded before it reaches `/chat`.
* **`RuntimeManager` identity carriage.** Voice turns do not yet carry
  `input_source`, `speaker_status` or `speaker_profile_id` into the turn path.
* **Memory attribution (the critical requirement).** Because identity does not
  yet reach `RuntimeManager`, an unknown speaker's statements are still handled
  exactly as before. **Nova does not yet protect Marcus's personal memory from a
  guest's utterances** — the boundary is designed and documented but not wired.
* **Episodic speaker provenance.**
* **The P5 pipeline benchmark** (`/stt` before/after, parallel execution).

Until the memory-attribution work lands, P5 should be treated as *identification
without consequence*: Nova can tell who is speaking, and does not yet act on it.
That ordering is safe — it cannot mis-attribute what it never uses — but it is
not the finished phase.

**P5.1's main body is still outstanding.** The pre-flight defects are fixed and
the substrate is now trustworthy enough to build on, but `RuntimeManager` still
receives no speaker context, so the attribution matrix, `memory.correct`
protection, speaker-scoped grounding and episodic attribution remain unbuilt.
A guest's utterances are still handled exactly as Marcus's.

## Live validation

**Unvalidated.** No accuracy figure for Marcus exists and none may be quoted
until `tests/live_speaker_id_harness.md` has been run with Marcus and at least
one other real person.

Separately, **P0 live barge-in acceptance is still pending a human run.** P5 did
not touch barge-in and does not change that.
