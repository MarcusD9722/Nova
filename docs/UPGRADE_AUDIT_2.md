# Nova Upgrade Audit — Round 2 (post U1–U5)

**Date:** 2026-08-01 · Scope: `memory/`, `core/`, `backend/`, `plugins/`
**Baseline:** everything through U5 + project deletion + the CUDA fallback fix.
**Constraint carried forward:** project/code building stays **cloud-LLM driven**
(`coder` + `planner` → cloud); chat, memory and decisions stay local.

## What round 1 actually moved

| Metric | Before U1 | Now |
|---|---|---|
| `asyncio.gather` calls | 4 | **12** (10 unifier, 2 runtime) |
| Regexes in `core/runtime.py` | 46 | **46 — unchanged** |
| Test suites | 36 | **44** |

The async work landed. The **understanding** work landed only partially: U3 did
task-vs-chat routing (A2), specialist selection (A3) and query expansion (A6) —
but **A1, which round 1 called the single highest-impact item, was never built.**
That's the headline of this round.

---

## A. Hardcode that remains

### A1 (carried over, still the biggest) — intent & slot extraction, `core/runtime.py`
14 pattern constants / 46 regexes still decide what Marcus meant:
`_PLACE_LOOKUP_PATTERNS`, `_TO_DESTINATION_PATTERNS`, `_NEAREST_QUERY_RE`,
`_WEATHER_CITY_RE`, `_NAME_STATEMENT_PATTERNS`, `_USE_DEVICE_LOCATION_RE`,
`_READ_STEPS_RE`, `_TIME_QUERY_RE`, `_DATE_QUERY_RE`, `_DIRS_*`.

Real failures this causes today:
* *"what's the best way over to Chipotle"* — no match, falls through.
* *"my name is marcus"* (lowercase) — `_NAME_STATEMENT_PATTERNS` requires a
  capital letter, so it silently doesn't register.
* *"how's the weather looking out in Austin tomorrow"* — city extraction is
  brittle around trailing words.

**Important nuance discovered this round:** these are a *fast path*, not the only
path. A miss falls through to the LLM tool loop, which usually still handles it —
so the cost is lost speed and occasionally a wrong slot, not total failure. That
lowers the urgency from "broken" to "brittle", but it's still the largest single
block of hardcoded understanding left.

→ **Fix shape:** keep the regexes as a confident fast path; on a miss, one
`Understanding.extract()` call (already built in U3) fills `{intent, slots}`.
No latency cost on common phrasings, no silent miss on uncommon ones.

### A2 — `core/dates.py` (23 regexes)
Date *arithmetic* must stay exact and deterministic. Only unusual *phrasing*
("the Friday after next", "end of the month") deserves an LLM fallback that then
hands a concrete date back to the deterministic math.

### A3 — `core/response_composer.py` (12) and `core/mood.py` (8)
Mood still has a fixed `MOOD_LABELS` set; U4 improved the *stress read* in the
twin but not mood capture itself.

---

## B. Async that remains

### B1 — Project files are generated ONE AT A TIME (biggest practical win)
`core/project_builder.py:404` — `for spec in files:` → `await self._llm_file(...)`.
A 5-file project is 5 sequential model round-trips.

This is now worth fixing precisely *because* of the cloud work: the cloud handle
carries `NOVA_CLOUD_CONCURRENCY` (default 4) permits, so gathering the per-file
generations would build ~4x faster — **and it degrades correctly by construction**:
routed locally, the same code serializes on the 1-permit GPU semaphore, exactly
as today. The semaphore travelling with the model (U2) makes this safe for free.

Caveats to respect: keep a deterministic write order, cap concurrency at the
plan size, and keep the existing per-file run-check/fix loop sequential (it
mutates shared project state).

### B2 — `core/workers/reminder_worker.py`: 34 awaits, 0 gathers
Its tick does birthday checks, habit detection, briefing composition and due-
reminder scanning strictly in sequence. Off the hot path, so lower value than
B1, but it's the last big sequential block.

---

## C. New capabilities worth adding

Ranked by value-to-effort for how Nova is actually used:

1. **Cloud cost + token tracking with a budget cap.** *(Real gap — flagged as a
   risk.)* Every project build now hits GPT-4o and there is **no spend
   visibility and no cap**. `CloudRuntime` already counts calls; it should also
   accumulate prompt/completion tokens, expose them on `/status`, and support
   `NOVA_CLOUD_DAILY_TOKEN_CAP` that falls back to local when exceeded — reusing
   the existing honest-fallback path.
2. **Streaming build progress.** `project.progress` events already fire per
   stage; surfacing them live ("writing main.py… running check…") would remove
   the silent multi-minute gap that made the flappy-bird session so opaque.
3. **Vision → code.** The mmproj vision model is loaded and the cloud coder is
   wired: screenshot of a UI → working code is now a small orchestration on top
   of two things that already exist.
4. **Cross-project reuse.** `code_intel` already indexes every project; "you
   solved this in gravity-runner" is a retrieval away and would make builds
   compound instead of starting cold.
5. **Test-first repair.** On a bug report, generate a failing check *first*, then
   iterate until it passes — this is the direct structural answer to the
   flappy-bird "claimed fixed four times" failure.
6. **Memory dreaming.** Idle-time consolidation of the day's turns into durable
   facts/edges (extends the existing reflection worker).

---

## ⚠️ Unchanged: what must stay deterministic

Re-affirmed this round, for the same reason as before — making these
probabilistic makes Nova *less* trustworthy: `core/permissions.py`, the dev-mode
deny-lists, `core/experiments.py` comparison math, `code_intel` AST parsing,
schema migrations, date *arithmetic*, GPU enforcement, and the new
project-deletion path safety (`ensure_safe_subdir`).

---

## Proposed order

| Phase | Contents | Why |
|---|---|---|
| **U6** | B1 parallel file generation + C1 cloud cost/cap | Fastest felt win on the thing he uses most, plus closes a live financial risk |
| **U7** | A1 intent/slot extraction via `Understanding.extract()` | The last big hardcode block; fast-path preserved |
| **U8** | C2 streaming build progress + C5 test-first repair | Directly targets the opacity + false-success failures |
| **U9** | C3 vision→code, C4 cross-project reuse | New capability, builds on what's now in place |
