# Stage 13B — long project execution, interruption, recovery, checkpoint integrity

Branch `nova-p10-preflight-stage13-coherence`, head `e55b0e9`, twelve commits
above `main`. **Not merged.** Stage 13B requires independent review first.

The stage asks one question: across success, failure, interruption, cancel,
pause, resume, restart, revision and stale work, can Nova still answer the
twelve things it is never allowed to lose —

> what project · what was requested · which revision · what executed · what
> succeeded · what failed · what is pending · what was cancelled · what was
> superseded · what may resume · what MUST NEVER resume · what Nova should
> say next

Seven defects say it could not. Each was reproduced against authoritative rows
**before** anything was changed.

---

## The defects

### S13B-1 — a background task that failed was recorded as completed
`ed81c94`

`AutonomySupervisorWorker` reached exactly one terminal call, `mark_task_done`,
from every path — a failed tool, an unhandled exception, a planner that
produced no usable plan. The row said `done`, `last_error` was cleared, and
`task.completed` went out on the bus. The honest outcome survived only inside
`result_json`, which `list_autonomy_tasks` did not return, so nothing that read
a task could tell success from failure and `list_tasks(status='failed')` was
always empty.

`MemoryUnifier.mark_task_failed` already existed with correct semantics and had
**zero callers**. The non-success paths now use it. The `idle` branch splits on
whether the planner was degraded, because "there was nothing to do" and "I
could not read the plan" are different outcomes.

Mutations M30–M32, M34 killed. **M33 withdrawn**: the `unknown_action`
fallthrough is unreachable — `AutonomyPlannerOutput.action` is a `Literal` of
the four values the loop handles. A mutant on dead code proves nothing.

### S13B-2 — a worker from a run that had ended could still finish it
`f929606`

The goal-task lifecycle was fenced in three places and unguarded in the fourth.
`complete_task` was `UPDATE tasks SET status=? WHERE task_id=?`.

| sequence | recorded |
|---|---|
| claim run 0 → cancel (run 1) → worker returns ok | `done` — a cancelled goal had completed work, announced as progress |
| claim → cancel → resume → run-0 worker returns ok | `done`, on the current run's behalf |
| `failed('disk full')` → duplicate callback says done | `done`, error cleared — a failure became a success |
| cancel marks a queued task `cancelled` → late completion | `done` — the cancellation erased |

Terminal bookkeeping had been treated as harmless because "the tool already ran
and that cannot be undone". But what is *recorded* about a run is not the same
thing as whether it executed, and it is what every later answer reads.

One guarded write returning `applied` / `superseded` / `ignored`, with
`expected_generation` **required** — an opt-in fence is the same defect with
more steps. A pause is deliberately not a supersession: it does not bump the
generation and does not invalidate work in flight, so a tool that finished
during a pause finished honestly and resume must not redo it.

`AgentSupervisor` passes the run it claimed at all six completion sites and
announces progress only when the completion applied; a superseded one says so
instead, because silence would be its own lie.

**M35 survived the first run, and that was the useful result.** The two halves
of the ownership predicate went stale together in every scenario I had written,
so either half alone caught them and neither was proved load-bearing. Killing it
took a new test where the goal is active on run N+1 while the task still belongs
to run N. M35/M36 also had to be rewritten first: each dropped a `?` and died on
`ProgrammingError` rather than on the property.

### S13B-3 — an interrupted task was recorded as one that never ran
`9408c55`

`except asyncio.CancelledError: return`. Five cancellation points, five
different truths, one identical record:

| cancelled | truth | recorded |
|---|---|---|
| after claim, before planning | nothing ran | `cancelled_on_startup` |
| during planning | nothing ran | `cancelled_on_startup` |
| inside execute, before the tool body | a side effect is possible | `cancelled_on_startup` |
| during the tool | a side effect is possible | `cancelled_on_startup` |
| after the tool, before bookkeeping | the tool **did** succeed | `cancelled_on_startup` |

`cancelled` asserts the work never happened. For the last three that is a claim
Nova is in no position to make — S13B-1 pointing the other way.

`in_flight` is set immediately before `router.execute` and cleared immediately
after, so a cancellation lands on one side or the other and three states are
distinguishable: **unknown** (naming the tool), **known-completed** (results
kept), **nothing ran**. The handler `raise`s rather than returning, and the
write is shielded, because losing the record of an interruption to a second
interruption is the same bug again.

Fixing it exposed two more, fixed here: `complete_autonomy_task` was itself
unguarded (the handler would have overwritten a genuine `done`), and
`mark_task_done` published its bus event unconditionally, so a no-op write
still announced a completion.

M40–M44 killed.

### S13B-4 — a task waiting on a person was filed as finished
`4c49771`

`ask_user` wrote the question into a fact and called `mark_task_done`. "What is
pending?" excluded it; the question survived only as a fact with nothing linking
it to the task that asked.

The four statuses could not express it, so `blocked` was added: not claimable,
survives a restart, and **has a way out** — `answer_task_question` appends the
answer where the next plan reads it and returns the row to `queued`. A state
with no exit is not an improvement on a state that lies.

`fanout_blocked` was analysed separately and is **not** the same case: nothing
waits on a person and nothing will unblock it, so it is `failed` with the policy
stated — but not `done`, because the task's own work never happened.

The UI is where this would have died quietly. `TasksSheet`'s status map falls
back to a **Queued** chip for anything it does not know, and filed everything
not queued/running under History. `cancelled` was already mislabelled that way.

M45–M49 killed.

### S13B-5 — a goal list that could not say which run it was on
`db59e32`

`get_goal` returned `generation`; `list_goals` did not. Anything enumerating
goals — including `GET /goals` — could not tell a goal cancelled and resumed
twice from a fresh one.

Found by journey 1, which also exposed **a defect in my own test model**: I had
defined "what may resume" as *queued on an active goal*, which is more
optimistic than the product. The codebase already contains the counter-example —
a run-6 task queued while its goal is active on run 7 — which the claim
predicate would never take. A test model weaker than the code cannot catch the
code drifting.

M50–M52 killed.

### S13B-6 — the progress Nova records was unreadable
`386e8da`

`AgentSupervisor` writes progress from seven places. Nothing in production read
`progress_events` back. The only reader, `fetch_unacked_progress`, has no
product caller and *acknowledges what it returns*, so polling it would consume
the history it displayed.

This also bounds a claim from S13B-2: the superseded test proves the **record**
is honest; it could not prove anyone could see it.

`list_progress_events` reads without consuming; `GET /goals/{id}/progress`
exposes it. `fetch_unacked_progress` is untouched — once-only delivery is a real
thing to want, just not what a reader wants.

M53–M54 killed. `SET acknowledged=1` on read appears exactly once in the memory
layer, so the consume-on-read channel is isolated to this one.

### S13B-7 — a restart stranded a goal in a state nothing could leave
`a30db95`

An active goal with queued steps, restarted: goal `active`, every task
`cancelled`, nothing claimable, zero progress events. It would never progress,
nothing would ever mention it, and "what are you working on?" still answered
with it.

Boot recovery cancels queued and running work so nothing runs unasked — right
for the *work*, silent about the *goal*. Such goals are now paused and told so.
`paused` is the useful word as well as the honest one: `resume_goal` already
opens exactly one new bounded run with a fresh `__decide__`.

M55–M56 killed.

---

## Mutation ledger

M30–M56 written, **27 mutants, all killed**, with three corrections that matter
more than the count:

- **M33 withdrawn** — equivalent mutant on unreachable code, not counted.
- **M30/M31 rewritten** — swapping `mark_task_failed` for `mark_task_done` left
  an `error=` kwarg, so the call raised and the exception handler recorded
  `failed` anyway. The suite passed for the wrong reason.
- **M35/M36 rewritten** — each dropped a bound parameter, so the statement
  raised instead of letting a stale write through.
- **M40 rewritten** — removing one line of a two-line call left a dangling
  continuation; a SyntaxError kill exercises nothing.
- **M52** also crashes `test_goal_completion_fence_s13b` with a `TypeError`.
  That kill is **not** counted; it dies on journey 1's assertion, which names
  the property.

A mutant is killed only when a suite fails because the protected **property**
was violated.

---

## Verification

| | |
|---|---|
| full gate | **134/134, three consecutive runs, zero failing suites** |
| soak | 35/35 clean across seven Stage 13B suites, tight runtime bands |
| journeys | 6 journeys, **105 checked transitions** |
| frontend | `npm run build` passes |
| JS syntax | `node --check` passes on all four standalone entry points |

The three gate runs are `PASSED: 134/134` each. One suite is an opt-in skip
(`test_cloud_live.py`, requires `NOVA_CLOUD_LIVE=1` and real tokens) and one
(`test_memory_hardening.py`) reports success in its own wording rather than the
harness format.

A caution about that number: my first attempt to capture the gate logs produced
three **0-byte files**, because `run_tests.ps1` uses `Write-Host`, which
bypasses the pipeline. "Zero failing suites" from those files would have been
vacuous. The counts above come from the captured console output, verified
per-suite: 133 suites with a verdict in all three runs, zero non-pass verdicts.

---

## Recorded, not fixed

1. **No status for "ran, but its run had ended."** A superseded goal task
   resolves to `failed`, the label this codebase already uses for the concept.
   Neither existing label is precise: `failed` says it went wrong, `cancelled`
   says it never ran. The truthful sixth status costs every reader of the
   column, including a UI map whose fallback claims a task will run on its own.
   It should be one deliberate change. See the state model, §5.

2. **`apply_tool_decision`'s gate is `active → active`** and its inserted ids
   are fresh per call, so applying the same decision twice would create
   duplicate work. It cannot happen — the `__decide__` task is claimed
   exclusively and completed in the same transaction, and no caller retries —
   but that rests on an argument about the claim rather than on the statement.
   Not hardened speculatively: a second guard on an unreachable path is how
   redundant guards start masking each other.

3. **Nothing yet *says* progress in chat or voice.** The record is reachable;
   deciding when Nova volunteers it is a product decision and was not smuggled
   in here.

4. **`projects/blue-and-tower-defense-and-i-want-you-to/`** is real usage from
   2026-08-16, not a test writing to the real projects folder.
   `resolve_name_boundary` already handles that exact utterance today. The
   folder is Marcus's data and was left alone.

---

## Where this falls short of the brief

The spec asked for six journeys and **200+ transitions**; there are six
journeys and **105**. I did not pad. The review's own warning is that the
expensive outcome is a superficially green Stage 13B, and inflating a
transition count with shallow steps is that failure mode exactly. Every
transition here re-derives the twelve truths from authoritative rows.

Generated / property-based transition sequences with fixed seeds were **not**
written. The existing seeded fuzz already found the `enqueue_task` fence
(seeds 13, 99, 31337); extending it is the obvious next increment and is not
claimed as done.

**No claim is made that all defects are found.** Three of the seven were found
only by combination — journeys, not seams — which is the strongest available
evidence that more remain.

---

## Two false findings of my own

Recorded because they shaped the method, and because the next person will make
them too.

- A probe used local time against a product that stores UTC: 40/40 false
  "the digest never appears".
- A concurrency test sliced a shared global prompt list. The "leaked" prompt
  was the other participant's own system prompt, sitting where I assumed mine
  would be. It was nearly reported as a privacy defect.

Hence the rule now stated in the state model and in the journey docstrings:
**attribute by id — project, goal, task, generation, attempt, conversation,
worker — never by list position, arrival order or timing.** A test that cannot
prove which operation produced an observation is not evidence of a defect.
