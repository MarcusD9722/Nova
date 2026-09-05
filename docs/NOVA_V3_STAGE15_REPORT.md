# Stage 15 — Cross-capability integration

Branch `nova-p10-preflight-stage15-cross-capability`, based on `origin/main`
`6f575ed` (Stage 14, merged and frozen).

**Status: built and gated on the branch. NOT MERGED, and Stage 16 not begun.**
This document describes a branch awaiting review, not a change on `main`.

Stage 15 looks for the places where two individually correct subsystems produce
an incorrect combined result. Nothing here was fixed on suspicion: every defect
was reproduced first, traced to the production path that caused it, fixed at
the boundary that owned the information, and covered by a regression that was
shown to fail without the fix.

---

## 1. The architectural finding

The assumed pipeline

    task -> goal -> acceptance evidence

**does not exist in production.** It was searched for, not assumed away: the
only production writer of acceptance evidence is
`ProjectBuilder._validate_criteria`, which runs a check per criterion against
the artifact. No goal row or task row ever reaches it. Measured:

    goal done + 3 tasks done + tool results ok + a file on disk  ->  idea
      ("no durable requirement has been recorded")
    criteria recorded, no evidence                               ->  scaffolded
    evidence recorded                                            ->  complete
    the goal then marked failed                                  ->  still complete

What exists is two independent authoritative axes:

| axis | the truth about | written by |
|---|---|---|
| goal / task system | whether the planned WORK ran, and what it did | goal service, task claims, generations |
| completion system | whether the ARTIFACT satisfies what was agreed | requirements, criteria, evidence |

They may legitimately disagree. The question Stage 15 actually answers is
whether Nova can hold both truths at once without one silently answering for
the other.

---

## 2. Defects found, reproduced and fixed

Each was found by inspecting an authoritative record — a durable row, an event
payload, a stored proposal — not by reading a reply.

**D1 — a pending permission that would not say what it would delete.**
`pending()` returned `[{"request_id": ...}]` and nothing else, so anything
listing outstanding requests (an approval UI, the audit endpoint, a caller
choosing between two deletions) had to rebuild the target by matching ids
against the audit trail. That is how the wrong one gets approved. `pending()`
now carries capability, tier, details and requested_at, popped on resolve and
close-out.

**D2 — "Is it done?" was a question about a project called `one`.**
Identity matching was substring-based, so `one` matched inside "d**one**".
Fixed with token-boundary spans (`_mention_span` with lookarounds),
`_compact_windows` for multi-word names, and `_drop_covered` so a short name
inside a longer one is not a separate candidate.

**D3 — a build that failed ended in silence.** The completion announcement
fired less often than the state changed, so a failed build simply stopped
saying anything.

**D4/D5 — the scope bug, in both halves.** `describe_work_state` was missing
its project filter, and the completion context was computed for every project
rather than the one asked about. Fixed with a three-way question scope: a named
project resolves to that project, a referential question ("is **it** done?") to
the current project or to nothing, and a survey question to all contracted
projects, bounded.

**D6 — "delete the cat tracker" deleted the cat.** A destructive request was
resolved to the wrong project by prefix matching.

**D7 — "delete tracker" started editing the project it might have meant.**
An ambiguous destructive request was not merely mis-targeted; it was routed
into an EDIT of a guessed project. Ambiguous removals now fail closed: no
project is selected, no destructive permission is requested against an inferred
target, and Nova asks.

**D8 — "delete the sidebar" asked to delete a whole project.** A request to
remove a FEATURE was matched by the whole-project removal branch.

**D9 — "not yet though" edited the project immediately.** A trailing deferral
clause was invisible to the grammar, so a deferred instruction executed at
once. The clause must close the sentence, which is what keeps "It is not
working right now, fix it." an instruction.

**D10 — "No, do X instead" lost the correction and kept the old plan.** The
affirmative grammar is anchored at the start of the sentence, so a leading
denial hid the instruction behind it: the correction was never stored and the
plan it corrected stayed approvable. Measured with three sequential deferred
corrections — the SECOND one ran.

**D11 — "Sorry — do X instead" was dropped the same way.** The same mechanism,
wider than the one phrasing: apologetic openers were not in `strip_preamble`'s
vocabulary. Measured with four sequential corrections — the THIRD ran. Both
fixes are scoped to `carries_a_proposal`; `authorize_project_mutation` is
untouched, so nothing became executable that was not before.

**D12 — "Project alpha: complete." was the whole answer about failed work.**
The composition defect, and the most consequential. With the criteria
demonstrated and the only goal failed, `status_text` returned one sentence —
and that sentence is the ENTIRE answer for "what's the status of alpha?"
(measured: the project pre-pass answers outright, zero prompts reach the
model). Neither axis may speak for the other, so when they disagree the answer
now carries both:

    "Project alpha: complete. Note that the planned work did not all succeed:
     1 goal(s) failed (add the adder)."

Additive only, and **silent when the axes agree** — a complete artifact with a
successful goal says only that it is complete.

---

## 2b. Defects in my own tests and tooling — NOT Nova's

Kept separate on purpose. Everything in section 2 is a defect in the product.
Everything here is a defect in the instrument, found while pointing it at the
product, and none of it says anything about Nova's behaviour.

**H1 — five suites attributed events to the wrong step.** They read
`BUS.recent(900)[n0:]`, but that deque has `maxlen=100`, so positional slicing
silently stopped meaning anything past the hundredth event. Replaced with a
subscription `Recorder`; every affected suite was re-run and no other result
changed. The corrected instrument is the authoritative basis for the evidence
that follows it.

**H2 — a test that passed while proving nothing.** An orphan-task test queued
work against a non-existent goal and asserted nothing was claimable. It passed
because `enqueue_goal_task` refuses at WRITE time and returns `None`, so the
claim-time guard it was named after never ran — and a follow-up assertion sat
behind `if task is not None:` and was skipped every time. Split into the two
guards that actually exist, with a liveness proof beside them.

**H3 — the observer contaminated what it observed.** A TOCTOU fixture read
completion state through the same patched `list_projects` the test was
manipulating. Fixed with an explicit `slugs=` parameter.

**H4 — `str.replace` without an assertion** silently no-op'd twice while
reporting success. Every anchor is asserted now.

**H5 — escape mangling through shell heredocs**, repeatedly: `
` collapsing
into real newlines inside string literals and breaking modules at import.
Mitigated by writing patch scripts as files rather than heredocs.

**H6 — a barrier keyed on a project slug deadlocked the suite** (section 3).

**H7 — J14 assumed per-goal task ordering** that production does not provide,
and counted other goals' tasks as its own (section 5).

**H8 — the generator's model tracked one claimed task**, so a second claim
silently replaced the first; and its uniform operation choice made the run
vacuous until the coverage gate caught it (section 6).

**H9 — the harness watchdog killed a legitimately long suite** in a full gate
(section 9).

One more, which is not a defect but belongs in the same column: **I predicted
M201's mechanism wrongly** and said so rather than quietly correcting it — see
section 7.

---

## 2c. Unreachable in production, and labelled as such

Scenarios asked for that cannot occur, each measured rather than assumed. They
are recorded, not simulated: a test that appeared to exercise one of these
would be exercising something else.

* **A foreground chat turn concurrent with a build parked inside its model
  call.** One model context, calls serialised; the parked build holds the only
  slot (section 3).
* **The generation fence firing across a restart.** Startup writes a terminal
  outcome first, so the row guard refuses the late result before generations
  are ever compared (section 4).
* **The revision fence in `current_verdict_for`.** Measured in Stage 14: no
  rows cross a revision onto a live criterion. Kept as defence in depth and
  labelled in the source.
* **Evidence provenance by task.** `acceptance_evidence.task_id` is never
  written by any production path (section 7, M212).

---

## 3. Foreground and background

Ten deterministic scenarios, no sleeps: every interleaving is held at a barrier
wrapping the model call, keyed on a token that rides in one piece of work's own
text. 71 assertions.

Three facts were measured rather than assumed:

1. **Keying a barrier on a project slug deadlocks the suite.** The tool
   decider's prompt carries a Context blob naming every project, so A's
   foreground turn contains B's name and parks on B's barrier.
2. **A foreground chat turn cannot run beside a build parked inside its model
   call.** There is one model context and calls through it are serialised, so
   the parked build holds the only slot. That is the single-GPU design, not a
   defect — but the interleaving is unreachable, and a test that appeared to
   exercise it would be testing something else. Where a real foreground turn is
   needed, the background work is a claimed goal task: genuinely in flight,
   without holding the model.
3. **`_improve` records a NEW requirement revision** before touching a file.
   The first version of the suite asserted the opposite; what matters is whose
   revision it is.

Attribution is read from the authoritative payload or row throughout —
conversation_id, project, goal_id, task_id, generation, revision, permission
target, tool outcome, completion state — never from the reply text. Every
absence carries a liveness proof.

---

## 4. The fresh-process restart matrix

Twelve crash boundaries (13 cases), each across a real process boundary: a
separate interpreter booted against the same durable root, dying by
`os._exit(0)` with work in flight. 66 assertions, 30+ processes.

**The finding is boundary 10.** A worker from the dead run reporting success
comes back `ignored`, not `superseded`. Across a restart the generation fence
never gets to speak: startup has already written a terminal outcome onto the
row, and `complete_task` refuses anything no longer `running` before it
compares generations. Two independent guards, and the restart makes the
stronger one fire first. The fence's own `superseded` is proved in-process, so
there is no restart-shaped hole between them.

Also established: a tool that already acted never acts twice; an interrupted
task records `unknown` rather than success; queued work is cancelled and its
goal paused rather than resumed; a completed task is never handed out again;
completion state and its reason reconstruct identically from disk with no
announcement to help; A's and B's states stay apart; a codeword stays in the
thread that said it; and a permission request nobody answered is not approvable
in the next life, while a new one still is.

---

## 5. Long integrated journeys

Fourteen journeys, 295 assertions, driving both axes at once and deliberately
driving them apart: successful work with nothing promised, failed work with a
complete artifact, ten corrections each demolishing the proof before it, three
projects ending three different ways, a restart with the axes already
disagreeing.

**Counting is computed, not typed.** A transition is one ENTITY moving under
one ACTION, read back from storage. `goal:<id>:status` and `:generation`
changing together are one goal transition; `reason:<slug>` is evidence and
never a transition of its own; reading twice counts nothing.

    330 counted authoritative transitions across 14 journeys
     91 of them off the work axis
     24 single actions that moved two or more capabilities at once

    by axis: task 239, completion 45, goal 24, requirement 14, permission 8

The last two figures are reported separately so the headline number is never
doing the strict number's work.

Three things production taught this suite, each after a wrong version of it:

* A machine cannot waive a criterion — `record_verdict` refuses WAIVED
  outright; the only route is a question Nova asked and a person redeemed.
* Acceptance criteria must quote spans of the request they belong to.
* `claim_next_goal_task` reads ONE GLOBAL QUEUE ordered by `updated_at` across
  every active goal. J14 originally planned ten goals up front and "ran goal
  1's four steps", which actually claimed whatever was oldest — other goals'
  tasks, counted as this one's.

---

## 6. Generated sequences

1000 sequences, 6499 operations, no invariant violations. Each sequence builds
its own project and applies randomly chosen operations from every capability at
once, checking the store against what the sequence knows it did after every
operation. Seeded and reproducible (`NOVA_S15_SEED`).

**The coverage gate is why the file is worth anything.** Choosing uniformly
from every operation spent the run on no-ops: measured at 40 sequences, 24
empty claims to 1 hit, so `applied`, `superseded` and `ignored` were NEVER
REACHED while the run passed. The generator now walks the reachable state
space, keeps a sixth of its choices precondition-blind so the refusal paths
still happen, and FAILS if any counted branch goes unexercised.

    claim hit/miss           473 / 76
    complete applied/superseded/ignored   118 / 122 / 91
    verdict passed/failed/inconclusive    453 / 230 / 208
    human accepted/refused   224 / 226
    permission approved/rejected  577 / 566
    states reached: complete 688, failing 672, planned 586,
                    scaffolded 1846, idea 2578

A **witness** project is set up once, never touched again, and re-read after
every single operation: nothing any sequence did ever moved it.

Two things production taught the model:

* A project with NOTHING BUILT stays `planned` however much evidence is filed
  against it. Proof without an artifact is not completion.
* The model tracked one claimed task, so a second claim silently replaced the
  first. It tracks every claim now, and flags claims that should not have been
  possible at all.

---

## 7. Mutation campaign (M201–M212)

Aimed at handoff loss: the things that carry identity across a seam — the
generation a claim was made under, the row a result belongs to, the revision
evidence was filed against, the project evidence is about, the target a
permission names, the axis a status line reports.

**Final: 11 killed, 1 withdrawn as equivalent.** The first pass came back 10
killed / 2 survived; both survivors were investigated before anything was
touched, and they needed opposite responses.

For runtime, the campaign runs the generator at **150** sequences, not 1000 —
enough to reach every branch its coverage gate asserts. The 1000-sequence
figures in section 6 come from dedicated runs and from the counted gates, not
from the mutation campaign. A mutant surviving the targeted suites is escalated
to the full set before survival is believed.

| id | mutant | outcome |
|---|---|---|
| M201 | the claim query offers rows the write will always refuse | KILLED (after a new regression) |
| M202 | a row that already ended can be written again | KILLED — generated |
| M203 | every result is treated as owning its run | KILLED — foreground/background |
| M204 | an interrupted task is recorded as a success | KILLED — restart matrix |
| M205 | queued work resumes by itself after a restart | KILLED — restart matrix |
| M206 | evidence from an older revision still counts | KILLED — journeys |
| M207 | another project's evidence counts as this one's | KILLED — independence |
| M208 | a cancelled goal keeps its generation | KILLED — foreground/background |
| M209 | a pending request is an id and nothing else | KILLED — foreground/background |
| M210 | the status line reports the artifact and nothing else | KILLED — journeys |
| M211 | a request from a dead process is still approvable | KILLED — restart matrix |
| M212 | evidence is filed without the task that produced it | WITHDRAWN (equivalent) |

### M201 — a real gap, with a mechanism I had wrong

Deleting `AND t.generation = g.generation` from `_CLAIM_SELECT` was noticed by
no suite. Cancelling a goal already excludes its tasks through
`g.status='active'`, so that condition does work in exactly one place: **pause
then resume**, the single transition that leaves a goal ACTIVE on a NEW
generation with the previous run's tasks still queued underneath it. Nothing
in Stage 15 exercised it.

What the deletion actually causes was measured, not assumed, and it is not
what I predicted. It does **not** re-run the old work: `_CLAIM_UPDATE` carries
its own generation fence (`g.generation = tasks.generation`), so a stale row is
refused at the write regardless. The two halves are a matched pair —

* the **UPDATE** is what makes a stale hand-out impossible (safety);
* the **SELECT**'s condition is what stops the queue **starving** on rows the
  UPDATE will always refuse (liveness).

Without it the candidate query returns the same wedged row for all eight claim
attempts, `claim_next_task` gives up, and the resumed goal makes no progress at
all — its own `__decide__` task is never reached. The regression asserts both
halves: nothing from the old run is handed out, and the new run's own work does
run.

### M212 — equivalent, and the investigation is the finding

`record_acceptance_evidence` takes a `task_id`, `Evidence` carries one, and
**nothing ever sets it**: production's only evidence writer
(`ProjectBuilder._validate_criteria`) calls `record_verdict` without a task,
and no consumer — `derive_state`, the events, the projections — reads the
field. Replacing an always-`None` value with `None` cannot change any
observable behaviour, so no state assertion can kill this mutant, and one that
appeared to would be asserting something else. The claim was withdrawn rather
than left standing. The dead provenance is recorded below rather than wired up:
filling it in would be inventing the task → evidence pipeline that does not
exist.

---

## 8. Recorded, not fixed

* **"Did the planned work succeed?"** is not matched by `asks_about_work`, so
  that phrasing reaches the model with neither axis attached. A grammar gap,
  not a composition defect; not patched, per the standing instruction not to
  broaden the intent grammar without a production defect requiring it.
* **"For blog, add a sidebar, but not yet."** — a leading TARGET phrase is a
  sentence shape the proposal grammar has never supported. Teaching it one
  would be broadening the grammar to fit a test.
* **"Do it now."** is not an authorised instruction: `do` is not in Stage 13A's
  imperative vocabulary. An assumption of mine, measured and then recorded
  rather than "fixed".
* **The revision fence in `current_verdict_for` is unreachable in production**
  (measured: no rows cross a revision onto a live criterion). Kept as defence
  in depth and labelled as such in the source.
* **`acceptance_evidence.task_id` is dead provenance** — plumbed through the
  schema and the model, never written, never read (see M212). Not wired up,
  because wiring it would invent the pipeline section 1 says does not exist.
* **One Stage 13C hang**, seen once in 40 runs and never reproduced, remains
  UNEXPLAINED.

---

## 9. Gates

Executable head `d3ed33c71e10a0b8bce7a3cea1f585699c72a912`, tree
`49cde60e91d4cc184247d49faa713a2d05d9f89b`. Before each gate the head, the tree
hash and the working tree are re-checked, with the working-tree check scoped to
the **executable** paths (`core`, `backend`, `memory`, `tests`, `frontend`) so
that a docs-only difference cannot pass as "unchanged" and nothing executable
can silently differ.

| gate | started | finished | result | exit |
|---|---|---|---|---|
| 1 | 23:16 | 01:03:33 | **183 / 183** | 0 |
| 2 | 01:11:37 | 02:22:42 | **183 / 183** | 0 |
| 3 | 02:22:43 | 03:35:45 | **183 / 183** | 0 |

No suite went red in any of the three. Before gates 2 and 3 the recorded
executable working tree was verified clean (`executable-dirty: 0`) against the
same head and tree hash.

**An earlier full run on the same tree came back 182/183, and the one failure
was real.** `test_s15_generated.py` was reported FAILED with no failing
assertion anywhere: the harness watchdog is a hard process timeout defaulting
to 180s that `run_tests.ps1` does not raise, so a suite doing twenty minutes of
legitimate work was killed mid-run and its thread stacks dumped. That run is
therefore **not** counted — a gate describes the head it ran on, and the fix
changed the head. The counted gates all ran after it.

Inside **each** of the three gates:

* **38 of 38** frozen Stage 13A / 13B / 13C / 14 suites pass, so the
  cross-capability work did not disturb the restart, durability or
  reconstruction guarantees they pin.
* **18 of 18** Stage 15 suites pass, including the 1000-sequence generator, the
  restart matrix's 30+ real processes, and the fourteen journeys.

**Frontend.** `npm run test:events` passes (27 assertions over the project
event projections) and `npm run build` succeeds — 40.0s, chunk-size advisories
only, all pre-existing.

### Exact-tree verification of what is proposed for merge

Performed after the three gates. Every commit after the gated executable head
`d3ed33c` is documentation-only, so this holds for the branch head as it
stands; any later executable change would invalidate the gates and require all
three to be re-run.

| check | result |
|---|---|
| `core` / `backend` / `memory` / `tests` / `frontend` subtree hashes, gated head `d3ed33c` vs branch head | **identical**, all five |
| whole-repo diff `d3ed33c..HEAD` | documentation only — this report |
| executable files changed after the gates | **0** |
| commits after the gated head | one, docs-only |
| working tree, executable paths | clean (0 changes) |
| working tree, anything else | clean apart from Marcus's own pre-existing `projects/` state |
| local vs `origin/…stage15-cross-capability` | equal, re-verified after a fresh fetch |
| Marcus's `projects/` deletions and `projects/.trash/` | untouched — 9 deletions still unstaged, `.trash` still untracked with 4 entries, nothing staged, restored, cleaned or committed |

So the three 183/183 gates describe exactly the executable tree proposed for
merge. Any later executable change would invalidate them and require all three
to be re-run.

**Assertion volume.** 586 `check()` call sites across the 18 Stage 15 suites;
executed counts are higher where sites sit inside loops (the journeys alone
report 295 executed assertions, foreground/background 71, the restart matrix
66).
