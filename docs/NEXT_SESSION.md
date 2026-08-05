# Next session: U10 continues — keep strangling `runtime.py`

**U10 phase 1 is done.** Integration coverage exists, it found real bugs, and the
first capability is extracted. This document is the handoff for phase 2.

## What U10 delivered

### 1. Integration tests that boot the real backend

`tests/harness.py` runs `backend.app._startup()` for real — memory, tool router,
RuntimeManager, every background worker, the HTTP routes — into a throwaway temp
root. Four suites drive it:

| Suite | Pins |
|---|---|
| `test_it_chat_pipeline.py` | a plain turn returns text, is recorded in state *and* SQLite, `/chat` + `/chat/stream` agree with the pipeline, per-turn overhead stays under budget, shutdown stops every worker |
| `test_it_navigation.py` | chatting about your evening never routes; a real request still does; a pending origin resolves honestly; a stale pending request is dropped |
| `test_it_gpu_serialization.py` | a cloud→local fallback under concurrent load never drives the local model concurrently (the CUDA crash), and a healthy cloud still overlaps |
| `test_it_project_build.py` | a build writes real files, runs them, and reports `complete`/`error` honestly |

Only three things are substituted, each for a stated reason (see the harness
docstring): **model weights** (`ScriptedLLM`), the **chroma index** (it loads a
transformer onto CUDA; SQLite is the declared source of truth), and **API keys**
(blanked, so no test can reach the network). Everything else is the real thing.

No paid API key is needed anywhere — `harness.CloudStub` is a real HTTP server on
localhost speaking the OpenAI shape.

**These tests are load-bearing, verified:** reverting the CUDA fix
(`fallback_semaphore=None`) makes `test_it_gpu_serialization` fail with peak
concurrency 5. It is not a vacuous assertion — the suite also proves its own
detector can see concurrency when there is any.

### 2. Four real defects, found by that coverage alone

1. **Shutdown aborted halfway.** Five workers let the `CancelledError` from their
   own `task.cancel()` escape `stop()` — it is a `BaseException`, so `except
   Exception` missed it. `RuntimeManager.stop()` then died on the first worker
   and never stopped the rest.
2. **The process could never exit.** Cancelling the memory-ingest worker
   *mid-transaction* leaked aiosqlite's **non-daemon** connection thread, and
   `threading._shutdown()` joins it forever. Reproduced ~1 run in 6; after the
   fix, 12/12 clean. Fixed properly in `core/workers/lifecycle.py`: ask the
   worker to stop and let it finish its step, cancel only if it won't.
3. **A pending route hijacked the next turn.** `_looks_like_location_answer`
   used the same first-word anchor that caused the old "I meant what other
   improvements…" misroute, so "actually, what did the kids have for lunch" was
   geocoded as an address — and stayed pending, poisoning the turn after that.
   Now uses `core.intent.is_question`.
4. **Maps lied about why it failed.** With no `GOOGLE_MAPS_API_KEY`, Nova said
   "I couldn't find 'Austin, Texas' on the map — give me a full address", which
   is false and unfixable by retyping. It now names the real reason (invariant
   #1), and the geocode call is no longer dropped from `tool_calls`.
5. **"Nevermind" didn't cancel.** Found by the live boot check, not by a test:
   the cancel list was exact-match, so "nevermind, forget the directions" was
   geocoded as an address. Now matched as a leading phrase.

### 3. Navigation extracted

`core/capabilities/navigation.py` owns maps end to end: patterns, pending-request
state, tool dispatch, reply wording. `runtime.py` went **2,209 → 1,868 lines** and
now calls two entry points (`resolve_pending` before the identity/clock/weather
pre-passes, `handle` after) — the seam exists because that ORDER is behavior.
One dead helper (`_looks_like_place_search_term`) went with it.

## Do next, in this order

### 1. Keep extracting — weather, then identity/clock
Both are smaller than navigation and live in the same `_direct_live_reply`
dispatcher. Same shape: `core/capabilities/<name>.py`, one capability per commit,
each behind the integration suites. Add a weather scenario to
`test_it_chat_pipeline.py` (or a new `test_it_weather.py`) **before** moving it —
right now weather has no integration coverage, only the misroute path does.

### 2. Then `core/interaction/`
Once 2-3 capabilities are out, `_direct_live_reply` is a short ordered list and
the coordinator/context/result split from the original U10 sketch becomes an
obvious mechanical move rather than a redesign.

### 3. Then U9 (vision→code, cross-project reuse)
Spec in `UPGRADE_AUDIT_2.md`. It was deferred until the foundation could catch
regressions. It now can.

## OPEN: one CUDA crash during the live boot check — unexplained

The boot-and-verify ritual booted the real 9B on the GPU and drove three turns.
Turns 1 and 2 were correct. Turn 3 killed the process:

```
2026-08-04T03:07:03Z info  embedding_model_loaded  device=cuda  model=BAAI/bge-small-en-v1.5
D:\a\llama-cpp-python\...\ggml\src\ggml-cuda\ggml-cuda.cu:102: CUDA error
```

**What is established:**
- It is **not** caused by the U10 changes. An A/B was run — committed `HEAD`
  vs. this working tree, identical env, same three turns — and both arms
  produced identical replies and **neither** crashed. The U10 diff does not
  touch any GPU path.
- It did **not** reproduce in either follow-up run. One occurrence, three total
  boots.

**The standing hypothesis** (untested, do not treat as fact): the crash line
lands immediately after `bge-small` is loaded onto **cuda** by the memory-ingest
worker's semantic write, while llama.cpp is mid-generation. Two CUDA consumers,
one GPU, and only llama.cpp calls are serialized on `_llm_sem` — `torch`
embedding work is not. That is the same *class* as the bug `d1f407e` fixed for
the cloud→local fallback, arriving through a different door.

**If you pick this up:** the cheap experiment is `NOVA_EMBED_DEVICE=cpu` for a
day of normal use. If the crash stops, the hypothesis holds and the fix is to
put embedding work behind the same GPU semaphore (or move it off the GPU). The
integration harness cannot see this — it runs with chroma off, precisely
because the embedding model wants CUDA.

## Known gaps — say these out loud, don't paper over them

- **The harness does not test the model.** Reply quality, latency under a real
  9B, and genuine CUDA behavior are all outside it. A green suite means the
  wiring is right, not that Nova sounds right.
- **Weather, reminders, goals, dev-mode and the WS event stream** have unit
  coverage but no integration coverage yet.
- **Project naming is greedy.** "called Countdown that counts down from ten"
  becomes the project name. Tracked separately; `test_it_project_build.py`
  works around it with a comment.

## State as of handoff
- Suite **50/50** (46 existing + 4 integration).
- TTS: was failing with a dtype error, **now working** — dropped, not diagnosed.
  Two hypotheses were tested and **disproven** (version drift; global-dtype leak
  from the fp16 embedding load). Don't re-chase those two.
- Cloud: `coder`+`planner` → GPT-4o, firewalled, token cap available.
- Known good: U1–U8 shipped and merged.

## If a suite ever hangs
`harness.run()` arms a faulthandler watchdog (`NOVA_IT_WATCHDOG_S`, default 180s)
that dumps every thread's stack and exits non-zero. That is how defect #2 above
was found — a hang is worse than a failure, because it stalls the runner with
nothing to read.
