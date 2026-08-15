# Live speaker-ID acceptance — V3 P5

**Status: NOT RUN. Requires two real humans and a microphone.**

Nothing in the automated suite can establish that Nova recognises Marcus. The
offline tests prove the mechanism — embeddings extract, same-voice scores above
different-voice, profiles persist, thresholds behave, failures degrade. They say
nothing about a real voice in a real room, and no number produced offline may be
reported as recognition accuracy.

**The shipped thresholds are provisional.** `NOVA_SPEAKER_THRESHOLD=0.55` and
`NOVA_SPEAKER_MARGIN=0.10` were chosen from measured separation on synthetic
fixtures. They are starting points for this harness, not calibrated values, and
this document is how they get replaced with real ones.

---

## What you need

* Marcus, and **at least one other real person** ("the guest").
* The microphone and room Nova is actually used in — not a headset borrowed for
  the test. A profile enrolled on one mic and tested on another measures the
  microphone.
* ~25 minutes.

Enable it (on by default):

```bash
NOVA_SPEAKER_ID=1
```

---

## Part 1 — Enrol Marcus

Record **six** samples, each 3–5 seconds, **varied natural speech** — not the
same sentence six times. A profile built from one repeated phrase learns the
phrase.

Suggested prompts:

1. "Hey Nova, what's the weather looking like this afternoon?"
2. "Remind me to call the plumber back on Thursday morning."
3. "I've been thinking about upgrading the drives in the server."
4. "Can you pull up what we decided about the memory architecture?"
5. "It's been a long day — play something quiet."
6. "What did we end up choosing for the monitor?"

Record the reported **consistency** score: ______

If enrollment is rejected, write down the reason verbatim. A rejection is data,
not a bug to work around.

---

## Part 2 — Marcus, across conditions

Twenty utterances, **four in each condition**. For each, record the reported
status, the similarity, and the runner-up similarity.

| # | Condition | Status | Sim | 2nd | Notes |
|---|---|---|---|---|---|
| 1–4 | normal speaking voice | | | | |
| 5–8 | quiet / low voice | | | | |
| 9–12 | louder than usual | | | | |
| 13–16 | close to the mic (~20 cm) | | | | |
| 17–20 | farther away (~2 m) | | | | |

Use **different phrases** throughout, including ones not used during enrollment.

Totals:

* accepted as Marcus (`known`): ____ / 20
* **false rejects** (`unknown` when it was Marcus): ____ / 20
* `ambiguous`: ____ / 20
* `too_short`: ____ / 20

---

## Part 3 — The guest

The guest speaks **ten** utterances, same microphone, same room, normal voice,
several different phrases.

| # | Status | Sim | 2nd | Notes |
|---|---|---|---|---|
| 1–10 | | | | |

Totals:

* correctly **not** identified as Marcus: ____ / 10
* **falsely accepted as Marcus**: ____ / 10 ← the number that matters most
* `ambiguous`: ____ / 10

---

## Part 4 — Two enrolled speakers (optional but valuable)

Enrol the guest as a second profile, then repeat Parts 2 and 3.

This is the only way to exercise the **margin** rule — with one profile enrolled
there is no runner-up, so `ambiguous` can never fire. Record how often the top
two scores land within 0.10 of each other.

---

## Part 5 — Latency

From `/status`, after the runs above:

* speaker embedding median: ______ ms
* `/stt` total, speaker **off**: ______ ms
* `/stt` total, speaker **on**: ______ ms

The offline measurement was 41–58 ms on CPU for 3 s of audio, unaffected by GPU
load. If the live figure is far from that, say so rather than assuming.

---

## Part 6 — Calibration

**Do not tune from one utterance.** Write out both distributions:

```
Marcus similarities:  ________________________________________
Guest  similarities:  ________________________________________
overlap region:       ________________________________________
```

Then choose:

* **threshold** — above the guest distribution, below the bulk of Marcus's.
* **margin** — from how close the top two scores actually get in Part 4.

The bias is deliberate and should stay: **a guest wrongly accepted as Marcus is
much worse than an honest `unknown`.** The first silently writes a stranger's
statements into Marcus's personal memory; the second only asks. When the
distributions overlap, prefer `unknown` and `ambiguous` over a confident wrong
answer.

Record the chosen values and **why**:

```
NOVA_SPEAKER_THRESHOLD = ______   because ______________________________
NOVA_SPEAKER_MARGIN    = ______   because ______________________________
```

Then update the defaults in `core/speaker/matcher.py`, note the evidence in
`docs/NOVA_V3_SPEAKER_ID.md`, and flip `threshold_calibrated` in
`SpeakerService.status()` to reflect reality.

---

## What this harness does NOT establish

* **Anti-spoofing.** P5 identifies normal live speakers. It does not detect a
  recording of Marcus, a cloned or generated Marcus voice, or a replay attack,
  and no result here may be described as if it does. Speaker verification and
  liveness detection are separate problems.
* **Authorisation.** A match never grants a capability. Confirm during the run
  that a destructive action still asks for confirmation while Marcus is
  recognised at high similarity — if it does not, that is a P5 defect and stops
  the phase.

---

## Separately: P0 barge-in is still pending

`tests/live_barge_in_harness.md` has still never been run by a human. P5 did not
touch barge-in or echo classification and does not change that status.
