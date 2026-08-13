# P0 live barge-in acceptance run

> ## ⚠ NOT YET RUNNABLE
>
> This document describes the acceptance run. **The harness it describes is not
> finished yet.** Built and build-verified so far:
>
> * `frontend/src/voice/bargeIn.ts` — the stage-1 acoustic gate
>   (`watchForSpeechOverPlayback`, `frameIsSpeechLike`), the telemetry types,
>   and the summary maths (`summarize`, `stopLatencyMs`, `replyLatencyMs`).
>
> Still to do before you can run this:
>
> * call `watchForSpeechOverPlayback` from the voice session loop in
>   `frontend/src/App.jsx`, wired to attenuate playback and hand off to the
>   existing `interruptActiveTurn()`
> * restore volume when the backend returns ECHO
> * expose `window.novaBargeIn` (`report()` / `reset()`)
>
> **Do not attempt the run below yet — `window.novaBargeIn` does not exist.**
> I will say explicitly when it is ready.

**Status: UNVALIDATED LIVE.** Full-duplex barge-in stays OFF by default until
this run passes. Nothing below can be simulated — it needs your voice, your
speakers and your microphone.

Everything is logged and summarised automatically. You do not calculate a single
timing.

---

## Before you start

1. Nova running normally (backend + frontend).
2. Speakers **on and audible** — the whole point is that the microphone hears
   Nova at the same time you talk.
3. Open the browser devtools console (this is where the log appears).

Enable barge-in and the harness:

```js
localStorage.setItem("nova.bargeIn", "1");
localStorage.setItem("nova.bargeInHarness", "1");
location.reload();
```

You should see `[barge-in] harness armed — 20 attempts` in the console.

---

## The run: 20 attempts

Each attempt is the same shape:

1. Ask Nova something with a **long** answer, so you have room to interrupt.
   Good prompts: *"Explain how RAID works."* / *"Tell me about the RTX 5090."*
   / *"What should I look for in a NAS drive?"*
2. **Wait until she is clearly mid-sentence.**
3. Start talking. Say something unambiguous and unrelated:
   *"Actually, compare the warranty instead."*
4. Note what happens, then start the next attempt.

Vary the conditions as you go — this is what makes the result meaningful rather
than 20 repetitions of one easy case:

| Attempts | Condition |
|---|---|
| 1–5 | normal speaker volume, normal mic distance |
| 6–9 | **loud** speakers (this is the self-interruption stress case) |
| 10–12 | quiet room, quiet speaking voice |
| 13–15 | microphone close to the speaker |
| 16–17 | microphone far from the speaker |
| 18 | interrupt a **short** answer |
| 19 | **say nothing** — just let a long answer finish (tests false triggers) |
| 20 | speak the **echo case**: repeat back a few words Nova just said, then ask something new |

Attempt 19 is deliberately a non-interruption. If Nova stops talking during it,
that is a false self-interrupt and the harness will record it.

---

## What the harness records per attempt

Automatically, with no input from you:

* attempt number
* Nova's sentence at the moment of interruption
* playback start time
* speech-detected time (stage-1 acoustic gate)
* playback stop time → **stop latency**
* STT transcript
* echo classification (ECHO / USER / MIXED)
* salvaged text, when MIXED
* whether the old turn was cancelled
* whether any stale audio leaked after cancellation
* time to the new reply's first audible word

---

## Finishing

After 20 attempts:

```js
window.novaBargeIn.report()
```

It prints, and copies to your clipboard:

* successes / 20
* missed interruptions
* **false self-interruptions**
* pure echoes correctly rejected
* mixed speech salvaged
* stale audio leaks (must be 0)
* **median stop latency**
* **P90 stop latency**
* median interruption → reply latency

Paste that back to me.

To abandon a run and start over: `window.novaBargeIn.reset()`.

---

## What counts as passing

My proposed bar — argue with it if you disagree, these are judgement calls not
measurements:

| Metric | Bar | Why |
|---|---|---|
| Successful interrupts | ≥ 17/20 | below this it feels unreliable |
| **False self-interrupts** | **0** | Nova cutting herself off is worse than a missed interrupt |
| Stale audio leaks | **0** | a cancelled turn speaking over the new one is a correctness bug |
| Median stop latency | ≤ 400 ms | above this the interruption feels ignored |
| P90 stop latency | ≤ 700 ms | |
| Echo correctly rejected | attempt 20 not treated as a fresh question | |

**If false self-interrupts > 0**, the acoustic gate is too eager. Raise
`overTtsRatio` in `frontend/src/voice/bargeIn.ts` (1.6 → 2.0) and re-run. That is
the single most likely adjustment, and it is why the value is a named constant
with a comment rather than a magic number.

**If interrupts are missed**, lower `overTtsRatio` (1.6 → 1.3) or `sustainMs`
(180 → 120). Lower both only one at a time — they trade against each other.

---

## After the run

Send me the report output. I will:

* record the real numbers in `docs/NOVA_V3_PERFORMANCE.md`
* tune the constants against what actually happened
* enable barge-in by default **only** if the bar is met
* mark P0 validated in `docs/NOVA_V3_FINAL_REPORT.md`

Until then P0 stays **UNVALIDATED LIVE**, and the feature stays off.
