# Stage 13B — closure review round 2

Branch `nova-p10-preflight-stage13b-execution-recovery`. **Not merged.**

Three items were named. All three are closed, and closing them exposed one
further defect the round-1 work had left behind.

---

## Bookkeeping first

The previous report said "22 commits above main". The reviewer's GitHub compare
said 23. The reviewer is right; my number was hand-copied rather than computed.
Every count in this report comes from `git rev-list --count`, not from memory.

---

## A — the project-removal boundary

### Reproduced first, through `POST /chat`

`flappy-bird` set as the authoritative last-active project, on `683b7ab`:

| message | measured behaviour |
|---|---|
| `"Delete it from my projects."` | `improve("flappy-bird", "Delete it from my projects.")` — *"Got it — working on those improvements to flappy-bird now."* |
| `"Remove it from the projects list."` | the same |
| `"Remove the bird from flappy-bird."` | classified **whole-project removal** |
| `"Remove the tower from tower-defense."` | classified **whole-project removal** |
| `"Remove the project banner from flappy-bird."` | classified **whole-project removal** |
| `"Retire flappy-bird."` | classified **whole-project removal** |
| `"Archive flappy-bird."` | classified **whole-project removal** |

The first two are a removal command starting an autonomous **write**. Returning
`False` meant "leave the edit path in charge", which is not fail-closed for a
removal — it is D4 with a different sentence.

The next three are the opposite failure: every piece of a hyphenated slug over
two characters was in the whole-project target set, so `bird` identified
flappy-bird and a **feature**-removal request was aimed at a permission-gated
delete.

The last two mapped a lifecycle Nova does not have onto an irreversible one.
The live session that started this whole thread opened with "retire with-you".

### The classification

A boolean could not carry these cases, so it is gone:

    NOT_REMOVAL · WHOLE_PROJECT · INSIDE_PROJECT · AMBIGUOUS · UNSUPPORTED_LIFECYCLE

Decided by grammar, not by a longer list of phrases:

- the object ends at a **container preposition** (`from`, `in`, …) *and* at a
  **clause boundary** — "delete with-you, I mean it" names with-you, not "mean".
- the **head** of the object decides. English puts it last: "the project
  banner" is a banner; "the flappy-bird project" is a project. That is what
  stops a generic word appearing as a *modifier* from claiming the whole
  project.
- only the **full identity** names the project, hyphens and spaces normalised.
  "flappy bird" is flappy-bird; "bird" is not. Slug components identify
  nothing.
- a sentence asking for two different things is AMBIGUOUS rather than a licence
  to pick the destructive reading.

**Routing.** WHOLE_PROJECT → the gated tool. INSIDE_PROJECT → ordinary edit
work, with the *exact* instruction reaching it. AMBIGUOUS and
UNSUPPORTED_LIFECYCLE → **no side effect at all**, and a question.

The unsupported answer had to move **before** the mutation gate: "Retire
flappy-bird." is not an affirmative instruction by that grammar, so behind
`may_mutate` it never reached the explanation and fell through to the model to
answer however it liked.

### After

| | delete request | improve | project |
|---|---|---|---|
| ambiguous ×2 | none | none | intact, Nova asks which was meant |
| feature removal ×2 | none | 1, exact instruction | intact |
| retire / archive | none | none | intact, explains there is no such state |
| explicit delete, denied | 1 | none | intact |
| explicit delete, approved | 1 | none | deleted, in trash |

---

## B — progress-event lifecycle provenance

Goals and tasks are generation-fenced; progress events were not, so a retry
from run 6 read identically to activity on run 7.

`generation`, `task_id` and `attempt` are stamped **at write time by the
producer**. Never at read time from the goal's current generation — that would
relabel history the moment a goal resumed, which is the exact confusion the
column exists to end.

Pre-existing rows stay `NULL`. An unknown revision is reported as unknown and
never invented. They are still **included** in the default view and labelled,
because hiding them would make an older database look empty — its own
dishonesty.

`GET /goals/{id}/progress` defaults to the current run and reports
`current_generation`, the selected `generation`, and `history`.
`?history=true` or `?generation=N` reaches the rest. Reads remain
non-destructive; `fetch_unacked_progress` remains the separate once-only
delivery.

**Journey 11 found the last writer with no provenance at all**: boot recovery's
own "paused by a restart" note. It now carries the run it interrupted.

---

## C — integrated transitions

| suite | journeys | checked |
|---|---|---|
| `test_long_journey_s13b` | 1–6, 8, 9 | 142 |
| `test_removal_and_provenance_journeys_s13b` | 10, 11 | 53 |
| `test_permission_delete_s13b` | 7 | 29 |
| **total** | **11** | **224** |

Past the >200 gate on new combination coverage. Journey 11 grew from 22 to 27
when a mutant showed a whole code path was unasserted; nothing was added merely
to reach a number.

---

## Mutations M72–M79

All killed on an assertion naming the property.

    M72  an ambiguous removal falls into the edit path again
    M73  one component of a slug identifies the whole project
    M74  retire/archive silently means delete
    M75  a feature removal is treated as a whole-project removal
    M76  a progress event is written with no run at all
    M77  progress is stamped with the goal's CURRENT run at READ time
    M78  the default progress read mixes stale runs in as current
    M79  a legacy event with no run is relabelled as the current one

**M76 survived its first run.** Journey 11 was writing its events through the
standalone writer, so the *fenced* path — the supervisor's decision events,
which is the path that actually knows the run it is applying and therefore the
one most able to get this wrong silently — had no assertion on it at all.

That is the third time in this stage a mutant has found a test asserting the
easy half of a property, and the cause is always the same: the assertion
followed whichever code path the test happened to use.

---

## Soaks

| target | runs | result |
|---|---|---|
| removal + provenance journeys | 20 | 20/20, 5.3–6.9s |
| permission destructive lifecycle | 20 | 20/20, 37.6–41.7s |
| integrated journeys | 10 | 10/10, 7.0–8.0s |
| generated sequences | 2 further batches | 720 + 960 transitions, no divergence |

## Stage 13A regressions

Intent compound, pending plan, pending withdrawal, pending deferral, project
selection, project delete, and the Stage 13A coherence journey — **all pass**.
Stage 13A remains frozen; these were regression checks.

---

## Full gates

Three consecutive runs of `run_tests.ps1` on the final head, captured from the
console (not `Out-File`, which misses `Write-Host`):

| run | result | failing suites |
|---|---|---|
| 1 | **PASSED: 141/141** | 0 |
| 2 | **PASSED: 141/141** | 0 |
| 3 | **PASSED: 141/141** | 0 |

423 suite runs (141 x 3), 140 with an explicit verdict in all three, zero
non-pass verdicts. No run was red at any point in this round.

One opt-in skip: `test_cloud_live.py` (needs `NOVA_CLOUD_LIVE=1` and real
tokens). One suite reporting success in its own wording rather than the harness
format: `test_memory_hardening.py` ("ALL OFFLINE CHECKS PASSED").

## Build and checks

- `npm run build` — passes
- `node --check` — passes on all four standalone entry points
- **GitHub: no CI exists.** 0 check runs, 0 statuses, and `gh run list` returns
  no workflow runs for this repository.

## Known limitations

1. Nothing volunteers progress. Nova answers status questions from the record
   when asked; proactive narration in chat or voice remains a product decision.
2. The approval card's live click was verified by build and render smoke-test,
   not by a human clicking it. Its backend contract - `applied` vs `approved`,
   expiry, late clicks - is covered end to end through the real endpoint.
3. `EXTERNAL_DRIFT` is not one of the generated transition actions. Drift has
   its own suite against the real filesystem; the transition model covers the
   task store, which has no artifact axis.
4. `INSIDE_PROJECT` removals are routed to the edit path but the edit itself is
   still whatever `ProjectBuilder.improve` makes of the instruction. Nova does
   not verify that a feature named in a removal actually exists first.
