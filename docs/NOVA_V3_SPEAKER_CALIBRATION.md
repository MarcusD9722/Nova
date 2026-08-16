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
4. Press **Check**, then work down the nine steps.

The harness autosaves *non-audio* progress to `localStorage`, so an accidental
refresh does not cost twenty utterances. **Audio is never saved** — each sample
is recorded, uploaded, embedded, and discarded.

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
| 9 | — | copy the JSON report | the artefact to hand back |

Every trial utterance must be **new speech**, not an enrollment phrase.

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

### Per-profile threshold

Walks `0.90 → 0.30` and takes the **first** value with zero impostor accepts and
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

**Calibration fails and says why**, reporting the measured overlap. The
thresholds are not loosened to make the screen green — a threshold that only
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

## Status of this phase

**Implementation: complete. Human calibration: NOT RUN.**

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
