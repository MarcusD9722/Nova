# Nova task state model (P10 pre-flight, Stage 13B)

The reference for what Nova is allowed to believe about background work. Written
against the code, not from memory: every transition below is one that exists in
`memory/backends/sqlite_backend.py`, `core/workers/autonomy_supervisor.py` or
`core/agent_supervisor.py`, and every "was" describes a defect that was
reproduced against an authoritative row before it was fixed.

There are **two independent task systems**. They are not variants of each other
and their differences are load-bearing.

| | `autonomy_tasks` | goal `tasks` |
|---|---|---|
| driven by | `AutonomySupervisorWorker` | `AgentSupervisor` |
| parent | none — a task stands alone | a `goal` row |
| lifecycle run | none | `generation`, fenced |
| retries | `bump_autonomy_task_attempt` | `bump_task_attempt`, fenced |
| statuses | queued, running, blocked, done, failed, cancelled | queued, running, done, failed, cancelled |

---

## 1. The twelve things that must stay true

Stage 13B exists to keep these answerable at all times, across interruption,
restart, cancellation and revision:

what project · what was requested · which revision · what executed · what
succeeded · what failed · what is pending · what was cancelled · what was
superseded · what may resume · what MUST NEVER resume · what Nova should say
next.

Every rule below is downstream of one of those.

---

## 2. `autonomy_tasks`

### Valid states

| status | means | claimable | terminal |
|---|---|---|---|
| `queued` | will run on its own | yes | no |
| `running` | a worker holds the claim | no | no |
| `blocked` | waiting for a person to answer | **no** | no |
| `done` | finished, successfully | no | yes |
| `failed` | did not deliver; `last_error` says why | no | yes |
| `cancelled` | never ran, and never will | no | yes |

### Valid transitions

```
  enqueue ──────────────► queued
  queued  ──claim───────► running          guarded: status='queued'
  running ──success─────► done             guarded: status='running'
  running ──failure─────► failed           guarded: status='running'
  running ──question────► blocked          guarded: status='running'
  running ──retry───────► queued           bump_autonomy_task_attempt
  blocked ──answer──────► queued           guarded: status='blocked'
  queued  ──boot────────► cancelled        it provably did nothing
  running ──boot────────► failed           interrupted; outcome UNKNOWN
```

### Invalid, and why each one was possible

- **`done` from a path that is not success.** Every terminal path called
  `mark_task_done`, including a failed tool and an unhandled exception. The
  honest outcome lived in `result_json`, which `list_autonomy_tasks` did not
  return — so nothing that read a task could tell success from failure
  (**S13B-1**).
- **`done` while waiting on a person.** `ask_user` marked the task done, so
  "what is pending?" excluded it (**S13B-4**).
- **`done` for a plan that was refused.** `fanout_blocked` marked the task done
  although its only proposed action was refused and nothing ran (**S13B-4**).
- **A second terminal write overwriting the first.** `complete_autonomy_task`
  was `UPDATE ... WHERE task_id=?`. A terminal state is a fact about something
  that already happened; the first write is the true one (**S13B-3**).
- **Stranded in `running` forever.** `except CancelledError: return` recorded
  nothing (**S13B-3**).
- **`cancelled` after a restart, for work that may have acted.** Boot recovery
  wrote queued and running rows identically (**S13B-3**).

### Durable vs ephemeral

Durable: `status`, `last_error`, `result_json`, `attempts`, `run_after`,
`initiated_by_user`, `project_name`.
Ephemeral: the plan, the tool results in memory, `in_flight` — all lost on
restart, which is exactly why the interruption record exists.

### Authoritative signals

- **Completion**: the `autonomy_tasks` row. Not the bus event, not the result
  payload, not model prose.
- **The announcement follows the write.** `mark_task_done` / `mark_task_failed`
  publish only if the guarded write landed. A `task.completed` for a write that
  did not happen is the same lie one layer out.
- **Failure**: `status='failed'` plus `last_error`. `task.updated`, never
  `task.completed`.
- **Unknown**: `status='failed'` with `last_error` stating the uncertainty in
  words. See §4.

---

## 3. Goal `tasks`

### The lifecycle run (`generation`)

A generation is a property of **the task row**, not of the goal at claim time.
Reading it from the goal is a defect this codebase has already made once.

`cancel_goal` bumps the goal's generation and cancels queued tasks; a running
task keeps its own generation, because a tool that is already executing cannot
be un-executed. `resume_goal` bumps again when resuming from `paused`.

### Fences — all four, since three of them once masked the fourth

```
claim_next_task     t.status='queued' AND t.run_after<=?
                    AND g.status='active' AND t.generation = g.generation
bump_task_attempt   status='running' AND generation=?
                    AND goal active on that generation
update_goal_status  optional expected_generation, checked IN the UPDATE
complete_task       status='running' AND generation=?
                    AND goal at that generation, active or paused
```

`complete_task` was unguarded until **S13B-2**. Terminal bookkeeping was treated
as harmless because "the tool already ran and that cannot be undone" — but what
is *recorded* about a run is not the same thing as whether it executed, and it
is what every later answer reads.

`expected_generation` is **required**, not optional. An opt-in fence is the same
defect with more steps: whoever forgets it is silently unfenced again.

### The three completion outcomes

| outcome | when | effect |
|---|---|---|
| `applied` | the caller owns the run | the reported status is written |
| `superseded` | still running, but the run ended underneath it | resolved as `failed`; the reported outcome kept in `result_json` |
| `ignored` | the row is already terminal | nothing; the first write wins |

**A pause is not a supersession.** It does not bump the generation and does not
invalidate work already in flight, so a tool that finished during a pause
finished honestly and resume must not redo it.

**A superseded completion is not announced as progress** — and not announced as
nothing either. `AgentSupervisor._note_superseded` says the work finished after
the run ended and was not counted. Silence would be its own lie: the work ran.

---

## 4. Unknown is a first-class outcome

The rule that generated most of Stage 13B's findings:

> When Nova cannot prove what happened, it must not record something it can
> prove instead.

Both directions are failures, and both were real:

- **unknown → success** (S13B-1): a failed tool, a crash and a degraded planner
  all recorded `done`.
- **unknown → never happened** (S13B-3): a task interrupted mid-tool recorded
  `cancelled`, which asserts the work did not occur.

The worker distinguishes three cases at cancellation, using `in_flight`, set
immediately before `router.execute` and cleared immediately after:

| what it knows | record |
|---|---|
| a call was in flight | UNKNOWN, naming the tool. It may or may not have completed and nothing here can find out. |
| calls had completed | known; the completed calls and their results are kept |
| neither | nothing executed, and that is safe to state |

Boot recovery draws the same line: a `queued` row provably did nothing, so
"cancelled" is true; a `running` row had been claimed and may have acted.

---

## 5. Known gap: there is no status for "ran, but its run had ended"

Not fixed. Recorded here deliberately, with the reasoning, so it is decided once
rather than drifted into.

A superseded goal task resolves to `failed`. That is the label this codebase
already uses for the concept — `_discard_stale_decision` chose it, and the
generation step counter reads `status IN ('done','failed')` — but neither
existing label is precise:

- `failed` says the work went wrong. It may have succeeded.
- `cancelled` says it never ran. It ran.

The truthful status is a sixth value (`superseded`). Adding one touches the
schema, `list_tasks` consumers, and the Tasks panel's status map — and that map
is exactly where such a change dies quietly: its fallback renders any unknown
status as a **Queued** chip, which claims the task will run on its own.
`cancelled` was being mislabelled that way until S13B-4.

So the cost of the sixth status is not the column; it is every reader of the
column. It should be taken deliberately, in one change, with the UI included.

Until then the uncertainty is carried in words: `last_error` says the work
finished after its run ended, and `result_json` keeps `{"superseded": true,
"reported_status": ..., "reported_result": ...}` so nothing is thrown away.

---

## 6. Rules for anyone changing this

1. **Do not solve durable state with conversation context.** A restart has no
   conversation.
2. **Do not solve task ownership with current-project state.** Ownership is the
   ids on the row: goal, task, generation, attempt.
3. **Do not solve execution truth with model prose.** The row is the fact.
4. **Do not put an LLM decision inside a side-effect safety boundary.**
5. **A guarded write returns whether it landed, and the caller must use it** —
   for the row, the announcement, and what Nova says next.
6. **Attribute by id, never by order.** Not list position, not slicing a shared
   call list, not timestamp proximity. A test that cannot prove which operation
   produced an observation is not evidence of a defect. Two of this stage's
   "findings" were my own instrumentation: a local-time clock against a UTC
   product, and a sliced global prompt list under concurrency that read as a
   privacy leak.
