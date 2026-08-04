# Next session: U10 — Verification & Structure

**Start here.** No new features. This phase makes every future change safer.

## Why this, and why now

Two bugs reached Marcus in normal use — a routing misroute ("we're about to go to
sleep" → "happy to route you to sleep") and a CUDA crash on cloud→local fallback.
**Both walked through a fully green 46-suite test run.**

The measured reason:

```
46 test suites · 0 that boot a real backend or model · 33 that use only fakes/tempdirs
core/runtime.py: 2,209 lines · 47 imports · 47 regexes · 40 methods · 14 tools registered inline
```

The tests are rigorous but an entire *category* is missing — the one that catches
what a user actually experiences. Meanwhile `runtime.py` keeps accumulating
reasons to change (recent sessions added slot extraction, project-delete tools,
and cloud wiring to it).

## Do these in order — the order is the point

### 1. Integration tests FIRST (5–8 of them)
Boot the real backend and drive real turns. Minimum coverage:
- chatting about your evening does **not** trigger navigation (the misroute)
- a cloud→local fallback under background-worker load does **not** crash the GPU
  (the CUDA bug — needs concurrent worker activity to reproduce)
- a normal chat turn returns text and doesn't regress latency
- a project build writes files and reports honestly

These must be runnable without a paid API key (point cloud at a local stub or
leave it disabled).

### 2. THEN strangle `runtime.py`
Target shape:
```
core/interaction/   coordinator.py · context.py · result.py
core/capabilities/  identity/ · weather/ · navigation/ · projects/
```
Extract **one capability at a time**, each behind the tests from step 1.
Start with **navigation** — it's self-contained and just proved buggy.

**Do not big-bang this.** Refactoring the highest-traffic file without
integration coverage is exactly how you ship a subtly broken assistant that
still passes 46 green suites.

## Deliberately NOT in this phase
- **U9** (vision→code, cross-project reuse) — real capability, but adds surface
  before the foundation can catch regressions. Spec is in `UPGRADE_AUDIT_2.md`.
- **Unattended autonomy** — the low rating is deferred Phase 3 + a deliberately
  dry-run computer-control layer, not a defect. Arming actuators is a separate
  decision Marcus should make on purpose.

## State as of handoff
- Suite **46/46**; `main` current; PR #21 (misroute fix) open, ready to merge.
- Cloud: `coder`+`planner` → GPT-4o, firewalled, token cap available.
- TTS: was failing with a dtype error, **now working** — dropped, not diagnosed.
  Two hypotheses were tested and **disproven** (version drift; global-dtype leak
  from the fp16 embedding load). Don't re-chase those two.
- Known good: U1–U8 all shipped and merged.
