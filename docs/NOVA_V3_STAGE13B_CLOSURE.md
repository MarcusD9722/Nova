# Stage 13B — closure supplement

Branch `nova-p10-preflight-stage13b-execution-recovery`. **Not merged.**

This closes the four gaps the review named, and reports seven further defects
found while closing them — five of which nothing in the original Stage 13B
would have caught.

---

## Provenance

| | |
|---|---|
| base main | `63cc664b8003cb9ff5a4c41762be9258d9e0c4cd` |
| previous final head | `55c485ba7c96f61a0856578f6079c9e99136ca3c` |
| local branch | `nova-p10-preflight-stage13b-execution-recovery` |
| remote branch | `nova-p10-preflight-stage13b-execution-recovery` |

**Outcome B.** The work was always committed on the intended Stage 13B branch;
my *reports* named `nova-p10-preflight-stage13-coherence`, which is the Stage
13A branch. Worse, every "pushed" claim after the first commit was false: I ran
`git push origin nova-p10-preflight-stage13-coherence`, which pushed that
already-up-to-date Stage 13A ref — a silent no-op that `-q` hid. Eleven commits
existed only locally until this supplement pushed them as a plain
fast-forward `ed81c94..55c485b`. No history was rewritten and Stage 13A's
approved head `da0b7f348dd0d26f002d7b0d18dcbb456a88ec3d` was never touched.

---

## Defects

### S13B-8 — the live `project.delete` session, four defects in one turn

Reproduced on `55c485b` before any fix.

| | what happened |
|---|---|
| **D1** | nothing in `frontend/src` consumed `permission.requested`; an admin-tier capability asked for consent that was impossible to give, and timed out after 120s |
| **D2** | ONE user request produced TWO permission requests (`7debd9d5796f`, then `78bc4e077809`), both timing out |
| **D3** | Nova answered "I'll take with-you off the list" when the authoritative outcome was *not approved, not executed, nothing touched* |
| **D4** | a re-phrased delete never reached the delete tool at all |

**D2's cause** is not `ToolRouter` (called with `retries=0`) and not
`is_retry_safe`. It is `ToolLoopExecutor.run`, whose dead-tool guard is
`failure_counts >= 2`: every tool gets a second automatic attempt, and the
model re-emits the same call once the refusal appears among its observations.
Asking a human again, unprompted, is not a retry — it is nagging for consent
that was already withheld, and the second prompt is the one most likely to be
approved by accident.

**D3 is not fixed with prose.** The observation reaching the answer step was
already truthful and already labelled "trust these over your own knowledge".
The outcome is now composed deterministically from the tool's own payload and
carried in the final text whatever the model produced.

**D4 was found while testing D2**, and is the worst of the four. "Please delete
with-you, I mean it." was swallowed by the project pre-pass, which treats *any*
authorised mutation as an **improve** — so it started an autonomous code edit of
that project and answered *"working on those improvements to with-you now"*. A
real write to disk, the permission gate never reached, the delete never done.
The fix decides by **adjacency**: "delete with-you" targets the project, "remove
the pause button **from** with-you" targets a feature, and a container
preposition ends the window. It returns False when unsure, because a missed
classification costs an edit a person can undo while a wrong one aims a gated
delete at a project nobody asked to lose.

### S13B-9 — lifecycle and execution were one column

The hole the previous report recorded as a known limitation. `status` carried
two independent facts, so a step cancelled before it ran, a step whose tool
**succeeded** after the user cancelled, and a step interrupted mid-call were
indistinguishable. Two of those three answers were false.

    status   queued | running | blocked | done | failed | cancelled | superseded
    outcome  pending | never_started | succeeded | failed | unknown

`superseded` stops work from an ended run counting as completed; `outcome`
stops that same row lying about whether the tool ran. Migration back-fills from
what each existing status implies.

### S13B-10 — a decision could be applied twice

The previous report recorded that `apply_tool_decision`'s idempotency "rests on
the claim being exclusive rather than on its own statement". Calling it twice
for the same claimed decide task took **3 tasks to 5** — a duplicate tool and a
duplicate continuation, both runnable. Fixed in the one place all four
`apply_*` methods share.

### S13B-11 — status questions never saw the record

Asking "What failed?" with a failed step and its error in the database produced
an answer prompt of 2291 characters containing **none of it**. There was no read
path at all; the model answered from memory, which after a restart is nothing.

### S13B-12 — a diff was applied to a file it no longer described

    propose against:  VERSION = 'A'  /  SPEED = 1
    file becomes:     VERSION = 'B'  /  SPEED = 1  /  NEW_FEATURE = True
    apply            -> VERSION = 'A'  /  SPEED = 2, status "applied"

Both other edits gone, no warning. Backed up, therefore recoverable — but the
user approved one thing and a different thing happened.

### S13B-13 and S13B-14 — found by the generated sequences

| seed | defect |
|---|---|
| 0 | cancel → pause left the goal reading `paused` — a false status that hides it from "what was cancelled?", plus a spurious second revision bump on resume |
| 19, 46, 47 | cancelling an already-cancelled goal bumped the revision again; a double-click left the goal claiming a run that never happened |

---

## The state model

| situation | status | outcome |
|---|---|---|
| never claimed, goal cancelled | `cancelled` | `never_started` |
| queued / running / waiting on a person | as-is | `pending` |
| tool succeeded, run current | `done` | `succeeded` |
| tool failed, run current | `failed` | `failed` |
| tool succeeded, run had ended | `superseded` | `succeeded` |
| tool failed, run had ended | `superseded` | `failed` |
| interrupted mid-call | `failed` | `unknown` |
| interrupted before any tool ran | `failed` | `never_started` |
| waiting on the user | `blocked` | `pending` |

Every reader carries both: `list_tasks`, `list_goal_tasks`, the goal-task API,
the Tasks panel (which shows `outcome` only when it says something the status
does not), restart recovery, and the `/chat` status answer.

---

## Generated sequences

| | |
|---|---|
| sequences | 250 (seeds 0–249) |
| transitions | 3000 |
| seed batches | 3 further (80×8, 80×11, 80×14) = 2640 transitions |
| failures found | 2 (seeds 0 and 19/46/47), both fixed |

The oracle is written from semantics, not from the SQL. It never asks "would
the claim predicate take this row?" — it states what runnable *means* and checks
production against that. Where production makes a choice the model deliberately
does not predict (which runnable row a claim returns, what id a resume's
continuation gets) the model verifies the choice was **legal** and adopts it,
rather than duplicating the tie-break and calling that agreement.

---

## Integrated journeys

| journey | checked transitions |
|---|---|
| 1 a long build interrupted beside a second project | 31 |
| 2 retries, exhaustion, a revision mid-retry | 11 |
| 3 background work end to end and back | 20 |
| 4 the supervisor decides, acts, is cancelled | 13 |
| 5 a restart leaves a goal resumable | 17 |
| 6 talking to Nova while it works | 13 |
| 7 permission-gated destructive action | 29 |
| 8 a known outcome and an unknowable one | 20 |
| 9 A and B interleaved, duplicates, drift, restart | 30 |
| **total** | **184** |

**Short of the 200 asked for, and not padded to reach it.** The supplement's own
reasoning is that the number is a proxy for combination coverage; the 250
generated sequences added 3000 further checked transitions over the same state
space and found two defects no hand-written journey had. Journey 9's own count
check was `>= 30` and ran 28 — rather than lower the bar I added the two
invariants that were genuinely missing.

---

## Mutations

M57–M71, all killed on an assertion naming the property.

**M58 and M59 survived their first run**, which was the useful result: the
outcome test set `unknown` directly through the memory API and the interruption
test asserted only the worker's *prose*, so a mutant could swap the worker's own
choice of `unknown` for `never_started` or `succeeded` unnoticed. Asserting
prose instead of state is how that hole opened.

Three of the supplement's numbers are accounted for differently rather than
inflated:

- **M61** is the same line as M60 — the turn fence does not distinguish a
  timeout from a refusal. One mutation; the suite asserts both paths.
- **M65 withdrawn, not counted.** Structurally impossible: `_gate` awaits one
  future, so once resolved the tool has already proceeded and a second approval
  finds nothing pending. The adjacent real property is M64, killed.
- **M66** would mutate the test, not the product. Its intent is demonstrated by
  M57 and M71, both killed by the generated sequences.

---

## Soaks

| target | runs | result |
|---|---|---|
| generation / cancel / resume / stale worker | 20 | 20/20, 3.7–4.9s |
| outcome & generation truth | 20 | 20/20, 2.6–3.6s |
| permission destructive lifecycle | 20 | 20/20, 37.6–41.6s |
| restart + A/B interleaving (journeys 1–9) | 10 | 10/10, 7.7–12.5s |
| decision idempotency | 10 | 10/10, 1.2–2.6s |
| generated transitions | 3 seed batches | no divergence |

---

## Full gates

Three consecutive runs of `run_tests.ps1` on the final head:

| run | result | failing suites |
|---|---|---|
| 1 | **PASSED: 140/140** | 0 |
| 2 | **PASSED: 140/140** | 0 |
| 3 | **PASSED: 140/140** | 0 |

420 suite runs seen (140 x 3), 139 with an explicit verdict in all three, zero
non-pass verdicts. One opt-in skip (`test_cloud_live.py`, needs
`NOVA_CLOUD_LIVE=1` and real tokens) and one suite reporting success in its own
wording rather than the harness format (`test_memory_hardening.py`, prints
"ALL OFFLINE CHECKS PASSED").

Output was captured from the console rather than through `Out-File`:
`run_tests.ps1` writes with `Write-Host`, which bypasses the pipeline, and an
earlier attempt at capturing these logs produced three 0-byte files. "Zero
failing suites" read off those would have been vacuous.

THE FIRST ATTEMPT AT THESE THREE GATES REPORTED 138/140 - the same two suites
in all three runs, which is the opposite of a flake and precisely why three
runs are required. Both were real signals about this stage's own changes:

`test_permission_handshake_p10` R25 pinned the ABSENCE of an approval surface
and was written to fail the day one landed, so that whoever added it had to
test the lifecycle rather than assume it. One landed here. Inverted rather than
deleted: a consumer must now exist, and it points at the suite that exercises
timeout, denial, approval, a late click reporting applied=false, and a fresh
user turn.

`test_orchestrator_p2` asserted a failing tool is attempted twice. The turn
fence makes a NON-retry-safe tool dead after one failure, which is what the
router's contract has always said. Both rules are now covered rather than one
relaxed: a retry-safe flaky tool still gets two strikes, a side-effecting one
gets a single attempt.

---

## Build and checks

- `npm run build` — passes
- `node --check` — passes on all four standalone entry points
- **GitHub statuses: none exist.** 0 statuses and 0 check runs on the pushed
  head; no CI is configured on this repository.

---

## Known limitations

Genuine, and none of them Stage 13B state-truth gaps:

1. **Nothing volunteers progress.** Nova answers status questions from the
   record when asked. Proactive narration in chat or voice is a product
   decision and was not smuggled in.
2. **The approval card's live behaviour was verified by build and render
   smoke-test, not by a human clicking it.** Its backend contract — `applied`
   vs `approved`, expiry, late clicks — is covered end to end through the real
   endpoint.
3. **EXTERNAL_DRIFT is not one of the generated actions.** Drift is covered by
   its own suite against the real filesystem; the transition model covers the
   task store, which has no artifact axis.
