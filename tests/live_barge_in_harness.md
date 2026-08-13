# P0 live barge-in acceptance run

> ## ✅ READY TO RUN
>
> Wired and verified offline: the stage-1 acoustic gate, duck/restore, backend
> ECHO/USER/MIXED verification, turn cancellation, and `window.novaBargeIn`.
> Frontend builds clean; `npm run test:voice` passes.
>
> **A bug was fixed before this run existed, and it matters for what you are
> about to tune.** The gate originally compared raw mic RMS against
> `getTtsOutputLevel()`, which is a *display* signal for avatar lip sync (raw
> RMS × 3.4, clamped to 1.0). Above output RMS 0.294 it saturates, making the
> threshold 1.6 — unreachable, since mic RMS is ≤ 1 by definition. **Barge-in
> was mathematically impossible exactly when Nova was loudest.** Tuning it live
> would have chased a broken denominator forever.
>
> It now uses raw output RMS and a **measured** acoustic-coupling estimate
> rather than a guessed constant, because how much of Nova the mic hears depends
> on speaker volume, mic distance and the room — the very things this run
> sweeps.

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

**If false self-interrupts > 0**, the gate is too eager. Raise `excessMargin` in
`frontend/src/voice/bargeIn.ts` (2.0 → 2.6) and re-run.

**If interrupts are missed**, lower `excessMargin` (2.0 → 1.6) or `sustainMs`
(180 → 120). Change one at a time — they trade against each other.

Check `window.novaBargeIn.coupling()` during a run. It should settle to a
plausible number for your room (roughly 0.1–0.9). If it stays `null`, the gate
never gathered enough clean frames and is running on `fallbackCoupling` — tell
me, because that is a different problem from a badly-tuned margin.

---

## After the run

Send me the report output. I will:

* record the real numbers in `docs/NOVA_V3_PERFORMANCE.md`
* tune the constants against what actually happened
* enable barge-in by default **only** if the bar is met
* mark P0 validated in `docs/NOVA_V3_FINAL_REPORT.md`

Until then P0 stays **UNVALIDATED LIVE**, and the feature stays off.
