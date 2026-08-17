# V3 P5.2 — Speaker calibration

P5 shipped `threshold 0.55` / `margin 0.10` as **provisional** numbers fitted to
offline synthetic fixtures, and `status()` has reported
`threshold_calibrated: false` ever since. This is how that becomes true.

**Two humans are required.** Marcus, and one other real person. Nothing in this
phase can be completed with recorded or synthetic audio, and the automated
suite deliberately cannot mark it passed.

---

## How to run it

1. Start Nova's backend as usual (default `http://localhost:8008`).
2. Open `tests/live_speaker_calibration.html` in a browser — **directly is
   fine**, it is a single static file with no build step.
3. Set the backend URL (and the API token, if `NOVA_API_TOKEN` is configured).
4. Press **Check**, then work down the twelve steps.

Steps 1–8 are recognition; 9–11 are the acceptance checks that make the result
mean something; 12 is the report.

### Guided batches — one click per block, not per utterance

The run is **107 recordings**. Clicking through them one at a time, each behind a
modal dialog, was most of the old workflow's cost — the audio itself is a small
fraction of it. So a block now runs itself:

```
START MARCUS PHASE A
  3 · 2 · 1
  QUIET 1/4    "Name three cities you have visited."
  ● RECORDING
  processing…  ✓ captured · known · 0.871
  QUIET 2/4    …
```

You click once to start a block, and once more at each **speaker handoff** —
which is a full-width inline banner naming who speaks next, not a dialog. There
are no `alert()`s left anywhere in the harness.

Handoffs gate **every** change of human, including the ones *inside* a block:
the sentinel alternates Marcus → guest → Marcus → guest → third person, and each
of those four changes waits for a deliberate Continue before the countdown
starts. Recording only auto-advances *within* one speaker.

Acoustic conditions are **grouped**, not alternated — four `near` takes in a row,
then four `far`. Same counts, but you settle into a position instead of moving on
every single sample.

**Pause**, **Retry this sample** and **Stop block** are always available. A
retried or failed sample repeats the *same index*: nothing is silently skipped,
and nothing is counted twice. A cough does not cost you the batch.

The stage shows whose turn it is, the acoustic condition, the prompt, a block
progress bar, an overall `n / 107` count, elapsed time, and an estimate of the
time remaining **computed from your actual observed pace** — not a promise.

### Who needs to be there, and when

- **Marcus alone** for steps 1–3.
- **The guest joins at step 4** and is needed through step 10. They do not need
  to be present from the beginning.
- **A third, unenrolled voice** is needed for exactly **one recording** — the
  final turn of the step 9 sentinel. See the fallback below.

### Recovery

The harness autosaves *non-audio* progress to `localStorage`, so an accidental
refresh does not cost completed utterances — a block resumes at the sample it
reached, and the step list shows `13 / 20 recorded — resume to finish`. **Audio
is never saved**: each sample is recorded, uploaded, embedded, and discarded.

Three blocks are deliberate **non-resumable** exceptions:

| block | why it cannot resume |
|---|---|
| **enrollment** (both) | Its six clips are held as in-memory `Blob`s until all six exist and go up in **one** request — and audio is never persisted. A Stop or refresh at 3/6 destroys three recordings while leaving `done: 3` on disk. Resuming would upload a half-enrollment while the UI showed 6/6. |
| **sentinel** | Its five results accumulate in memory; a half-resumed run would report an incomplete privacy check as a complete one. |
| **permission** | Same — two results, in memory. |

For enrollment the harness does not merely decline to resume: on load it
**reconciles**, resetting any enrollment block whose progress is not backed by a
completed server enrollment, and telling you plainly that those six clips are
gone and must be recorded again. The fix is refusing to resume, *not* persisting
the audio.

If `/speaker/enroll` succeeds but the profile is **below the P5.2 bar** (fewer
than 5 of 6 clips survived the quality gate), the harness stops there. It does
not advance, and it does not silently delete the profile the backend just
created — it names the profile id and leaves deletion to you, in Preflight.

If `localStorage` is unavailable (an opaque origin, a locked-down profile) the
harness still runs — it just loses resume.

---

## The protocol

| step | who | what | why |
|---|---|---|---|
| 1 | — | preflight; review and explicitly delete stale profiles | enrollment never silently replaces one |
| 2 | Marcus | 6 enrollment samples, ≥5 must survive | see *the 5-of-6 bar* below |
| 3 | Marcus | 20 trials — 4 each normal/quiet/loud/near/far | his **genuine** distribution |
| 4 | guest, **not yet enrolled** | 12 trials | the **real impostor** distribution |
| 5 | guest | 6 enrollment samples | now exactly two compatible profiles |
| 6 | both | 12 + 12 trials | calibrates the **margin** between two real people |
| 7 | — | review the proposed fit, then apply it | fitting and applying are separate |
| 8 | both | 10 + 10 **fresh** utterances | validation may not reuse calibration audio |
| 9 | both **+ a third, unenrolled person** | memory sentinel, 5 turns in ONE conversation | proves attribution is real, not per-conversation luck |
| 10 | both | permission probe — typed, Marcus voice, guest voice | identity must change no decision |
| 11 | one person | 12 recordings, `/stt` only | what identification costs, off vs on |
| 12 | — | copy the JSON report | the artefact to hand back |

**The evidence counts above are unchanged** by the guided-batch rewrite, and
`tests/test_calibration_harness_v52.py` asserts every one of them against the
harness's own plan. The redesign removes human interaction overhead — clicks,
dialogs, handoffs, fixed waits — and nothing else. It is intended to cut
substantially into the previous manual 30–40 minute workflow; the harness
reports measured progress rather than promising a duration.

**Latency stays 6 OFF + 6 ON.** Speaker-ON `/stt` timings do exist elsewhere in
the session (the sentinel turns), and they are measured on the same code path —
but those recordings are 4–5 s and `/stt` scales with audio length through
Whisper, so pooling them against a 2.5 s OFF control would bias the delta.
Unlike inputs are not a control, so they are not reused.

**Step 9 needs a third voice** for exactly one recording — someone not enrolled,
for the unverified turn. If no third person is available, a genuinely unverified
acoustic condition also works (a deliberately poor or too-short recording), but
**only** if `/stt` actually returns something other than `known`. The harness
checks this and fails the privacy result rather than accepting a `known` third
speaker, because in that case the question was never asked.

### Recording windows, and why the phrases got shorter

| what | window |
|---|---|
| enrollment | 3.0 s |
| calibration trials (phase A / B) | 3.0 s |
| validation | 3.0 s |
| permission voice probes | 3.0 s |
| `/stt` latency probes | 2.5 s (OFF and ON identical) |
| sentinel — store | 5.0 s |
| sentinel — ask | 4.0 s |

`MediaRecorder` records a **fixed window**, so shortening it without shortening
the prompt would simply truncate speech mid-word and hand ECAPA a clipped
sample. The enrollment phrases were rewritten to ~8 words (~2.5 s) to match. The
sentinel keeps its longer windows because those utterances carry a token that
must not be clipped.

3.0 s still leaves 2× margin over the backend's 1.5 s enrollment floor
(`matcher.MIN_SAMPLE_S`) — near the floor is not the operating point.

Every trial utterance must be **new speech**, not an enrollment phrase. The
harness rotates prompts from two **disjoint** pools, so a calibration sentence
cannot become a validation sentence. Trials and validation are also stored
separately, so the fit can never see the audio it will later be judged against.
One trial is always one genuinely new utterance — recordings are never sliced
into several samples.

---

## The algorithm

Deterministic, in `core/speaker/calibration.py`, tested in
`tests/test_speaker_calibration_v52.py`. Not JavaScript arithmetic — the fit is
production code so it can be regression-tested.

### The asymmetry that drives every choice

|  |  |
|---|---|
| false **reject** | Nova says "I don't recognise you" to Marcus. Annoying; he repeats himself. |
| false **accept** | Nova files a stranger's words into his memory, or reads his private facts to them. |

The whole P5.1 boundary exists to prevent the second. So the fit optimises for
**zero observed false accepts first**, and only then for genuine acceptance.
`unknown` and `ambiguous` are *successful* outcomes of a system that is unsure —
counted as rejects, never as errors.

### The candidate range

Scores are **true cosine similarities** between L2-normalised ECAPA embeddings
(`matcher.cosine`), so the metric is bounded **[-1, 1]** — not [0, 1].

The grid is **0.01 → 1.00 at 0.01 resolution** (100 candidates). It used to be
0.30 → 0.90, which was an unexamined guess carried over from synthetic fixtures
whose scores happened to be high. Real speech is not obliged to agree with it,
and the first human run proved it does not — see *Attempt 1* below.

The lower bound is **strictly positive**, and that is a deliberate floor rather
than the metric's true minimum. A cosine threshold at or below zero cannot
express an identity claim: it admits vectors with no directional agreement with
the centroid at all, which for 192-d unit vectors is roughly half of everything.
The empirical bars ("no impostor in *this* sample cleared it") cannot protect
against that, because the sample is a couple of dozen utterances and the runtime
population is every voice that ever speaks.

The floor is `0.01`, not `0.00`, for a concrete reason. With zero on the grid the
fitter could **select** it whenever every observed impostor happened to score
negative and ≥90% of genuine samples cleared zero — a configuration that
satisfies both bars on paper while asserting a claim this document calls
meaningless. Zero is therefore excluded from the candidate set rather than
merely discouraged. If no strictly positive threshold satisfies both bars, the
fit **fails closed** and says which boundary it hit.

### Per-profile threshold

Walks `1.00 → 0.01` and takes the **first** value with zero impostor accepts and
≥90% genuine acceptance. Strictest-first is deliberate: among candidates that
satisfy both bars, the tightest leaves the most headroom for a voice the fit has
never seen.

Impostor evidence for a profile is every score it earned while *someone else*
was speaking — whether it ranked top **or runner-up**. That second half matters:
with two enrolled people, a guest speaking makes Marcus the runner-up, never the
top match, so a top-only bound would leave the second profile with no evidence at
all.

### Genuine scores

Every score a person's own profile earned **while they were speaking** — top or
runner-up. Dropping a trial because the true speaker ranked second would discard
exactly the hard cases the threshold exists to handle, and bias the genuine
distribution upward.

### Margin

Grid-searched `0.30 → 0.02`, most conservative first, simulating the **real
classifier**: a trial counts as a correct `known` only when the top score clears
that profile's proposed threshold *and* the gap clears the candidate margin.
Scoring on the gap alone credited trials the live matcher would have returned as
`unknown`, so a margin could show ≥90% while the shipped classifier did not.

A candidate producing even one **wrong-person** call is rejected outright.
Ambiguity beats a confident mistake.

### If it does not fit

**Calibration fails and says which boundary it hit.** The reason is derived from
the data, never inferred from "the search found nothing" — the first human run
was told the distributions overlapped when they were separated by +0.04, and the
real fault was the search floor. Four distinct outcomes:

| when | what it says |
|---|---|
| the 90% accept ceiling is at or below the impostor max | the distributions **genuinely overlap** — any bar loose enough for this speaker admits somebody else |
| a valid threshold exists only at or below `0.01` | **refusing to fail open** — that bar admits voices with no directional agreement; this sample's impostors all scoring lower is a property of the sample, not of every voice |
| a valid interval exists but no candidate lands inside it | a **fitter limitation**, named as such, not a property of the voices |
| too few genuine trials, or no impostor evidence | said plainly, and neither mentions overlap |

Thresholds are never loosened to make the screen green. A threshold that only
passes because it was fitted loosely carries a claim it cannot support, which is
worse than an honest provisional one.

---

## The 5-of-6 bar

`SpeakerService.enrol` can build a centroid from **3** usable samples. P5.2
acceptance requires **5 kept out of 6 recorded**. A profile built from the bare
minimum has no margin for one bad room, and re-recording is cheaper than a weak
profile that quietly widens every later decision.

Rejected samples are reported with their reason. The fix is to re-record, never
to relax the quality gate.

---

## What is persisted

| where | what |
|---|---|
| `speaker_profiles.threshold` | the per-profile calibrated threshold |
| `speaker_calibration` (one row) | margin, model id/revision, protocol version, covered profile ids, aggregate metrics, timestamp |

**Never stored:** raw audio, embeddings, centroids, transcripts. The calibration
record holds thresholds and counts.

### When calibration becomes stale, automatically

* the model id, revision, or embedding dimension changes — scores from a
  different encoder are not comparable, so an old threshold is not merely stale
  but meaningless;
* the protocol version changes;
* a profile is **added**, **deleted**, or **replaced** — the fit described a
  population that no longer exists.

Coverage is **exact set equality**, not containment:

```
set(record.profile_ids) == { every current compatible profile id }
```

Containment was wrong in the direction that matters. A fit over
{Marcus, Guest} still "covered" a population of {Marcus} alone — but Marcus's
threshold exists *because* the guest's voice supplied the impostor evidence that
bounded his false-accept rate. Delete the guest and the number outlives the only
measurement that justified it.

Set equality is also the **fail-closed backstop for a failed clear**. If
`_invalidate_calibration()` cannot delete the row — locked database, disk error —
the stale row is still read on the next boot. It is then inert rather than
trusted, so a failed delete degrades to provisional defaults instead of a silent
stale claim.

In each case `threshold_calibrated` returns to `false`. Partly-calibrated is not
a claim worth making.

---

## Threshold precedence

**One resolved policy** (`resolve_policy`) is computed once and handed to
`match()`. The matcher no longer reads `profile.threshold` on its own — a stored
threshold survives its calibration going stale, and a number nobody stands
behind must not keep deciding.

Explicit, and `status()` names which is in force:

```
NOVA_SPEAKER_THRESHOLD / NOVA_SPEAKER_MARGIN   env override
        ↓
persisted calibration                          calibrated
        ↓
DEFAULT_THRESHOLD 0.55 / DEFAULT_MARGIN 0.10   provisional default
```

`status()` reports `threshold_source`, `margin_source`, and per profile both
`effective_threshold` (what will actually judge it now) and `stored_threshold`
(history). There is no hidden threshold source, and `status()` and the matcher
read the same policy object, so they cannot disagree.

**A stale calibration is inert.** Adding a profile, deleting a covered one, a
model/revision change, or clearing the record all return every decision to the
provisional defaults — the stored per-profile numbers stay in SQLite as history
but stop voting. Invalidation lives in `SpeakerService.enrol()`/`delete()`, not
in the HTTP router, so a direct non-HTTP caller cannot leave a stale fit
standing.

`/stt` diagnostics now report the **effective** threshold that actually decided —
previously they reported the global fallback even when a per-profile value was
used, describing a decision that was never made.

**`resolve_policy()` is the only door.** `match()` does not read
`profile.threshold` at all, even when no policy is passed. It used to, as a
fallback — which meant a stored number kept deciding after its calibration went
stale, and any direct caller (a tool, a test, a future code path that forgets to
pass the policy) silently reactivated it. With no policy the matcher uses an
explicitly supplied threshold, or the global/env default, and nothing else.

---

## Score attribution is not identity

Two different questions, two different fields:

| field | means | populated when |
|---|---|---|
| `profile_id` / `display_name` | the **asserted identity** | only for `known` |
| `top_scored_profile_id` / `top_scored_display_name` | who merely **ranked first** | whenever any compatible profile was scored |

For a correct rejection:

```
status                 unknown
profile_id             None          ← Nova asserts nobody
top_scored_profile_id  p-marcus      ← but his profile earned the score
similarity             0.43
threshold              0.55
```

That 0.43 is precisely the impostor evidence that bounds Marcus's false-accept
rate. Before this split, a rejected trial carried no attribution at all — the
harness read `profile_id`, got `null`, and the score was collected against nobody
and dropped from the fit. Exactly the hard cases a threshold exists to handle
were the ones being discarded.

**Never treat `top_scored_*` as identity.** It authorises nothing, attributes no
memory, and names nobody in a prompt. The browser keeps both:
`predicted_profile_id` drives the recognition confusion matrix; `top_profile_id`
and `second_profile_id` drive the calibration fit.

---

## Still not authentication

Calibration makes recognition *measured*. It does not make it *authorisation*.
`PermissionBroker` is untouched, `evaluate()` still takes no identity argument,
and a recognised Marcus at 0.99 similarity is asked to confirm exactly what typed
Marcus is asked to confirm.

This is now **measured rather than asserted**. `POST /speaker/permission-probe`
runs the real broker, through the real `ComputerControl` mapping, on
`computer.type` — a STANDARD-tier production actuator — three ways: typed, as a
recognised Marcus voice, and as a recognised guest voice. Identity is resolved
backend-side from an opaque `voice_turn_id`; the browser never says who is
speaking. The run passes only when both voice handles were genuinely recognised
**and** all three decisions match the typed reference. The previous harness
asserted `pass: true` as a literal, which is not a test.

Nothing can be typed by the probe: it never waits for approval, it immediately
resolves its own request as rejected, execution is disabled by default, and no
platform adapter ships. The probe refuses any capability outside a small
allow-list.

**No anti-spoofing claim.** Nothing here detects a recording, a cloned voice, or
a replay.

---

## P5.2 HUMAN CALIBRATION ATTEMPT 1 — INCOMPLETE / FITTER RANGE DEFECT

**2026-08-16. Marcus + Leslie. Stopped at step 7 (proposed calibration).**

This was **not** a speaker-model failure and **not** a successful calibration.
The recordings were fine; the fitter could not express the answer.

| | Marcus | Leslie |
|---|---|---|
| genuine n | 32 | 12 |
| impostor n | 23 | 12 |
| genuine min | 0.1092 | 0.3194 |
| genuine p05 | 0.2405 | 0.3853 |
| genuine median | 0.55495 | 0.53485 |
| impostor max | 0.2001 | 0.1503 |
| separation | **+0.0404** | +0.235 |
| fitted threshold | **null** | 0.38 |
| genuine accept rate | — | 0.9167 |
| false accepts | — | 0 |
| result | **FAILED** | PASS |

Shared margin fitted at **0.29**, correct rate 0.9167, zero wrong-person calls.

Marcus's separation was **positive**: a threshold near 0.21–0.25 satisfied both
bars. The fitter never evaluated anything below **0.30**, so it returned no
threshold — and then reported *"the distributions overlap"*, which was false and
would have sent a person to re-record forty perfectly good utterances.

Both faults are fixed: the candidate range now spans the metric's usable range,
and the failure diagnostic is derived from the data instead of asserted whenever
no candidate was found. `tests/test_speaker_calibration_v52.py` pins this exact
dataset, and proves the old floor would still fail on it.

**Attempt 1 produced no calibration and no accuracy figure. A fresh run is
required from step 1** — thresholds are fitted from the trials of a single
session, and the prior session's scores were not retained.

---

## Status of this phase

**Implementation: complete. Human calibration: NOT RUN (attempt 1 incomplete,
see above).**

No live accuracy figure exists, and none may be quoted until Marcus and a second
person have completed the harness and the fresh validation set has passed:

* **0 / 20** identity swaps,
* **≥ 9 / 10** correct for Marcus,
* **≥ 9 / 10** correct for the guest.

An `unknown` or `ambiguous` result counts as a false reject, not a false
identity. A single swap in either direction is an immediate fail.

### Recognition PASS is not P5.2 PASS

The harness reports two verdicts, and they are not the same thing:

```
RECOGNITION VALIDATION   PASS | FAIL
P5.2 HUMAN ACCEPTANCE    NOT COMPLETE | PASS | FAIL
```

Acceptance requires **all** of: exactly two compatible profiles; ≥5 kept samples
each; calibration applied; `threshold_calibrated == true`; 0/20 swaps with ≥9/10
per speaker; the live memory-attribution sentinel; the unverified privacy check;
the permission regression; and the latency measurement. Anything not yet run
reads **NOT COMPLETE** — never PASS.

Steps 9–11 cover those: memory attribution through the real
`/stt → voice_turn_id → /chat` pipeline (Marcus and the guest each retrieve only
their own sentinel, an unverified turn gets neither), the permission regression,
and `/stt` latency with speaker OFF vs ON plus the delta. Latency is reported as
`identify_ms` — the whole `identify()` call — not mislabelled as embedding-only.

### Two conditions the sentinel run must meet

**One conversation.** All five sentinel turns share a single browser-generated
`conversation_id`, and the id the server echoes back is checked on every turn.
Without it `Brain.chat()` mints a fresh UUID per call, so each turn ran in its
own conversation — where of course nobody can see anybody else's memory. The
privacy claim only means something when Marcus, the guest and the unverified
speaker share one conversation.

**The third speaker must actually be unverified.** Their `/stt` result has to be
one of `unknown`, `ambiguous`, `unavailable` or `too_short`. If it comes back
`known`, the privacy check does **not** pass — it was never asked. Re-run it with
a genuinely unenrolled person. The two enrolled speakers must likewise be
recognised as *themselves*, or "the guest did not receive Marcus's sentinel"
proves nothing.

### Exactly two profiles

Acceptance checks the compatible profile set directly — count is 2, the ids are
exactly the two this run enrolled, Marcus is `owner` and the guest is `guest`.
This is not inferred from `threshold_calibrated`: a third profile left over from
an earlier session changes what every threshold means, and "calibrated" would not
necessarily say so.
