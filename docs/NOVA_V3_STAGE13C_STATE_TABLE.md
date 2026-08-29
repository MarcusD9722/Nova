# What survives a restart

Stage 13C, §3. One table for the question "the process died — now what?"

Every row here was **observed**, not inferred from reading the code: each was
produced by a real process boundary (a placed `CRASH()`, then a fresh
interpreter against the same directory) in one of the Stage 13C suites, and the
"state after restart" column is what authoritative rows actually said. Where a
row is a judgement rather than a measurement, it says so.

Two columns need defining before the table makes sense.

**Durable** means it reached SQLite (or, for permissions, the append-only audit
file) before the process stopped. **Ephemeral** means it lived in Python and is
simply gone — an `asyncio` task, a pending future, a worker holding a claimed
row, a conversation in memory, a cached model. The whole design problem is that
ephemeral state is often the only thing that knew what the durable state
*meant*, and after a restart nobody is left who knows.

---

## Goals and their steps

| State before the crash | Durable | Ephemeral | Boot recovery action | State after restart | May resume? | Needs the user? | What Nova should say |
|---|---|---|---|---|---|---|---|
| Goal `active`, nothing queued | goal row, `generation` | the supervisor loop | goal → `paused` | `paused`, same revision | yes, on request | yes — to ask for it | "Paused when Nova restarted. Resume it to carry on." |
| Step `queued` | task row | the queue in memory | step → `cancelled` / `never_started` | `cancelled`, work **never started** | not that row; a resume opens a new run | yes | "It never started, so nothing was done." |
| Step `running` (claimed, tool in flight) | task row + `generation` | the worker, the tool, its result | step → `failed` / `unknown` | `failed`, work **unknown** | no — it may already have acted | yes | "It was interrupted mid-flight. Whether its tool completed is unknown." |
| Step finished, terminal write landed | `done`/`succeeded` or `failed`/`failed` | nothing that matters | untouched | exactly as written | n/a | no | the real outcome, and the real error text |
| Step finished, crash *before* the write | nothing | the result | nothing to act on | still shows as interrupted → `unknown` | no | yes | "Unknown" — and this is honest, not a gap: the evidence may exist on disk while the record cannot see it |
| Goal `cancelled` | `cancelled`, revision bumped | — | untouched | `cancelled` | yes, explicitly — resume does **not** bump again | yes | "You cancelled it; nothing further ran." |
| Goal `paused` (by an earlier restart) | `paused` | — | untouched | `paused` | yes — resume opens exactly one new run | yes | which revision it is on |
| A worker from before the crash reports afterwards | — | the worker | — | refused: `ignored` or `superseded` | no | no | nothing changed; the report belonged to a run that ended |

**Why an interrupted step is `failed`/`unknown` and not `cancelled`.** `cancelled`
means *never started*, and a claimed step may have run its tool to completion
before the power went. The two axes exist precisely so this can be said: the
row is over (`failed`) and what happened to the work is genuinely not known
(`unknown`). Guessing either way would be a fabrication.

**Why nothing resumes itself.** Recovery terminates the queued work it inherits
rather than continuing it, so that no tool runs because a machine rebooted. The
consequence is worth stating plainly: after any restart, the only claimable
work is what a resume creates. A journey that queues steps in one life and
expects to claim them in the next finds nothing — which is the product being
right, and was a real bug in this stage's own tests before it was a line in
this table.

---

## Revisions (the generation counter)

| Event | Effect on the goal's revision | Why |
|---|---|---|
| `cancel` a goal that is not already cancelled | **+1** | the cancel itself opens the new run, so an in-flight decision is stale the moment it is cancelled |
| `cancel` a goal already cancelled | no change | idempotent; eight concurrent cancels must not produce eight runs |
| `resume` from `cancelled` | no change | the cancel already opened the run |
| `resume` from `paused` | **+1** | nothing had opened one, and the step budget is counted per run |
| `resume` an `active` goal | no change | already running |
| restart | no change | a restart is not a decision |

Revisions only ever go up. That is what makes "a completion aimed at revision
N−1" provably illegitimate without asking the product's opinion, and it is the
property the generated sequences lean on hardest.

The fence is **doubly guarded**: a completion must match both the step's own
revision and the goal's current one. Disabling either half alone changes
nothing observable, which is why two mutants against it had to be withdrawn as
wrong-reason before a third, disabling both, died immediately.

---

## Permission requests

| State before the crash | Durable | Ephemeral | Boot recovery action | State after restart | What Nova should say |
|---|---|---|---|---|---|
| Waiting for a human | one `pending` audit line | the future, `pending()`, the waiting tool | closed out as `interrupted_by_restart`, once | not pending, not approvable | "Nova restarted before that was answered, so the request is gone — nothing was executed." |
| Approved and executed | `approved`, and the action's own effects | — | recovered as `approved` | unchanged; not replayed | "You already approved that one; it has already run." |
| Declined | `rejected` | — | recovered as `rejected` | a later "yes" is refused | "You already declined that one — nothing was executed." |
| Timed out | `timeout` | — | recovered as `timeout` | refused | "That request timed out — nothing was executed." |
| Policy mode raised at runtime | **nothing** | the mode | — | back to the configured mode | privilege never rises across a restart; a *configured* elevation does survive |

A pending request left as `pending` for ever is not a neutral gap — it reads as
still waiting. Closing it out is what makes "you were asked and never answered"
a thing the record can say.

---

## Checkpoints (dev-mode proposals)

| State before the crash | Durable | After restart | What Nova should do |
|---|---|---|---|
| Proposal made, file untouched since | the diff **and the baseline digest** | still applies | apply it |
| Proposal made, file changed while Nova was gone | the diff and the baseline digest | refused: "has changed since this diff was computed" | say the file moved on; re-propose against what is there now |

A checkpoint that forgot what it was planned against would be worse than none,
because it would still look valid.

---

## Old databases

| Column the old row lacks | What migration may fill in | What it must leave alone |
|---|---|---|
| `outcome` | what the status already implies: `done`→`succeeded`, `failed`→`failed`, `cancelled`→`never_started` | anything the status does not imply |
| `generation` (goals, tasks) | `0` — the first run there can be | never a later one |
| progress `generation` / `task_id` / `attempt` | nothing | stays `NULL`, is still shown, and is labelled unknown rather than guessed |

An old `running` row gets the same restart truth as a new one: `failed` /
`unknown`. A legacy progress line stamped with the goal's *current* revision
would be a fabricated claim about which run it came from, indistinguishable
from a real one — so it is left NULL.

---

## The conversation

The transcript is ephemeral in the sense that matters: after a restart it
begins empty while the work is still there. So a question about the work is
answered from the record, never from what Nova remembers saying — and the
dangerous phrasings are the ones that invite agreement rather than a lookup:

- *"What happened?" / "What failed?" / "What's left?"* — questions.
- *"Is anything still running?" / "Are you still working on it?"* — the first
  thing asked on coming back to a machine that restarted.
- *"So everything finished, right?"* — a **claim**. With no state attached, a
  model agrees. Agreeing that everything succeeded when a step failed is worse
  than any amount of vagueness.

Ordinary talk is left alone: "is the tap still running", "are you working on
Sunday", "everything is fine, thanks" attach nothing.
