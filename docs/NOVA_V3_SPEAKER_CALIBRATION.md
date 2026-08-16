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

### Margin

Grid-searched `0.30 → 0.02`, most conservative first. A candidate is rejected
outright if it produces even one **wrong-person** call. Ambiguity is a better
answer than a confident mistake.

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
* a profile is **added** or **deleted** — the fit described a population that no
  longer exists.

In each case `threshold_calibrated` returns to `false`. Partly-calibrated is not
a claim worth making.

---

## Threshold precedence

Explicit, and `status()` names which is in force:

```
NOVA_SPEAKER_THRESHOLD / NOVA_SPEAKER_MARGIN   env override
        ↓
persisted calibration                          calibrated
        ↓
DEFAULT_THRESHOLD 0.55 / DEFAULT_MARGIN 0.10   provisional default
```

`status()` reports `margin_source` and, per profile, `threshold_source`. There is
no hidden threshold source.

`/stt` diagnostics now report the **effective** threshold that actually decided —
previously they reported the global fallback even when a per-profile value was
used, describing a decision that was never made.

---

## Still not authentication

Calibration makes recognition *measured*. It does not make it *authorisation*.
`PermissionBroker` is untouched, `evaluate()` still takes no identity argument,
and a recognised Marcus at 0.99 similarity is asked to confirm exactly what typed
Marcus is asked to confirm.

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
