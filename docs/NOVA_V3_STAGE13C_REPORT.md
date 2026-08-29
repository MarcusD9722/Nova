# Stage 13C — restart, durability, reconstruction, extended E2E

The question the stage exists to answer:

> If Nova's process dies, all ephemeral state disappears, and Nova starts
> again, can she reconstruct the exact authoritative truth of what happened,
> what did not happen, what may continue, what must never continue, and what
> she should tell the user next?

Base: `main` at `76ab15e2a1024db0621df5875743a58e95bcf5ba` (the Stage 13B
merge). Branch: `nova-p10-preflight-stage13c-restart-durability`.

**Every restart in this stage is a real one.** A scenario runs in a fresh
interpreter, is handed nothing but a directory path, and talks to its siblings
only through what reached the disk. `runtime.stop()` followed by
`runtime.start()` on the same object proves very little: the globals are still
there, the workers are the same objects, the caches are warm, and anything
"reconstructed from durable state" may in fact have been reconstructed from
itself. A crash is a placed `CRASH()` — `os._exit`, so no finally block, no
atexit hook, no buffer flush, nothing drains — rather than a kill after N
seconds in the hope of landing somewhere interesting.

Across the stage that comes to **1,832 interpreter lives** in the generated
model alone, plus the hand-written suites and the journeys.

---

## The defects

### S13C-1 — a claim about the work was answered from nothing

Stage 13B taught Nova to answer *questions* about her work from the durable
record. It did not cover *claims* about it, and the claim is the dangerous
form: a question invites her to look something up, a premise invites her to
agree. Reproduced through a real second process with an empty transcript,
while a step sat `failed` with its error on disk:

| turn | work record attached? |
|---|---|
| `"What failed?"` | yes |
| `"So everything finished successfully, right?"` | **no** |
| `"Everything worked, right?"` | **no** |
| `"That all went through, yes?"` | **no** |
| `"Did it all work?"` | **no** |

With no state in front of it, agreeing is what a model does — and agreeing
that everything succeeded when a step failed is a worse answer than any amount
of vagueness.

Fixed with a premise detector requiring BOTH a scope word (*everything, it,
that, the tasks*) and an outcome word (*finished, worked, went through*), so
"Everything is fine, thanks." and "I finished my coffee." stay untouched. A
bare `work` alternative was deliberately rejected: it would swallow "Does that
work for you?".

### S13C-2 — a restart was recorded as an answer nobody gave

A permission request lives entirely in memory except one line in an
append-only audit file: the pending future, who answered, how it ended. When
the process died holding one, all of that went with it — and the durable trail
was left saying `pending`, for ever, with nothing after it. Reading that file
later there was no way to tell "still waiting for you" from "died while
waiting".

Worse, an answer arriving after the restart was audited as `unknown_request`,
reason *"no request with this id was ever pending"*. It **had** been pending.
The file said so, one line up. That is not a gap in a security log; it is a
false entry in one. And `settled_as` returned `""`, so a user who had
**declined** a deletion before the restart was told only that their click did
nothing — never that their refusal still stood.

Reproduced across a real process boundary: a `project.delete` held at the
approval prompt, `CRASH()`, then a fresh interpreter approving the same id.

The broker now reads the tail of its own trail when it is constructed —
recovering how each request ended, and closing out the ones that ended
nowhere with one terminal entry each. `/permissions/resolve` names which "no"
it was instead of listing every possibility to someone owed one specific
answer.

Nothing here restores a future. `_pending` stays empty, `pending()` offers
nothing to click, and an interrupted request cannot be approved afterwards —
proved by the project standing untouched on disk after the late approval.

### S13C-3 — an old database was told it was already up to date

`_apply_migrations` short-circuited on any database with no version stamp, on
the stated grounds that "a pre-versioning DB by definition matches today's
create block". It does not. `CREATE TABLE IF NOT EXISTS` leaves an existing
table exactly as it was, so a database whose `tasks` table predates goal
generations was stamped as fully migrated — marking all eight migrations done
and applying none of them.

Migration 8 is the one that adds `tasks.generation` and `goals.generation`. So
the column never arrived, and the first read of a goal or a task failed with
`sqlite3.OperationalError: no such column: generation`. **Not degraded —
unreadable, on every start**, for a user with work already recorded.

What hid it: `_migrate_tasks_schema` back-fills nearly every other column on
that table by hand (including `outcome`, added in 13B). It does not back-fill
`generation`, and it covers no other table.

An unstamped database that already existed now replays from the beginning; one
this call created still takes the shortcut, because there the premise is true.
Verified read-only that the real local database is already at version 8 with
the column present, so this path does not touch it.

### S13C-4 — "is anything still running?" attached no record

The most ordinary question of all — the one a person asks on coming back to a
machine that restarted — was not recognised:

    Is anything still running?          not recognised
    Are the tasks still running?        not recognised
    Are you still working on the menu?  not recognised
    What's going on with my work?       not recognised

Reproduced over real `/chat` in a fresh process with an empty transcript and a
step sitting `failed`. None attached the record, so the model was answering
from nothing at all.

Three closed shapes added, each requiring a subject or a work noun, so "is the
tap still running", "are you working on Sunday" and "what's going on with your
day" stay ordinary conversation.

### The performance defect inside S13C-2's fix

Measuring rather than assuming turned up a shape problem in my own fix: it
read the WHOLE audit file and then sliced the tail off it. Fine for a week,
not fine for a year — the price of every boot grew with everything the machine
had ever asked. It now seeks to the last megabyte:

| audit size | before | after |
|---|---|---|
| 10,000 requests (2.7 MB) | grows with the file | P50 10.8 ms |
| 100,000 requests (27 MB) | grows with the file | P50 7.7 ms |

Flat across a tenfold difference in file size. A limit comes with it and is
written down rather than assumed: a request older than the read window is not
recovered — left alone rather than guessed at, which is the right way to
degrade.

---

## Mutation ledger

| # | mutant | result |
|---|---|---|
| M80 | a still-open request counts as an ending | **withdrawn — equivalent** (branch order makes `_ENDINGS` unreachable for `pending`) |
| M80′ | a request left open is simply not noticed | killed |
| M81–M87 | close-out not recorded; endings forgotten; trail not carried into `audit_log()`; close-out repeated per boot; the note forgets which "no"; recovered ending claims approval; only the last line read | all killed |
| M88–M93 | every unstamped db stamped current; an existing db counted as fresh; replay on a current db; legacy progress back-filled; cancelled read as succeeded; failed read as succeeded | all killed (M88/M90 by reproducing the original crash) |
| M94 | the "still running" question unrecognised | invalid (unbalanced parens) → rewritten |
| M94′, M95–M99 | question branch removed; "working on" matches a diary; "going on with" needs no work noun; sweep leaves claimed rows; sweep calls interrupted work never-started; the bare branch dropped | all killed |
| M100, M102–M104 | recovery leaves in-flight rows; cancel winds the revision back; queued swept as succeeded; a superseded write recorded as reported | all killed |
| M101, M101′ | one half of the completion fence disabled | **withdrawn — wrong reason.** The fence is doubly guarded (the step's own revision AND the goal's current one); disabling either half alone is unobservable |
| M101″ | both halves disabled | killed immediately |
| M105 | the terminal-write guard dropped | **withdrawn — equivalent.** The outer `UPDATE ... AND status='running'` enforces the same idempotency, as `complete_task`'s docstring already states |
| M106–M110 | the work record drops the outcome axis; drops the error text; presents itself as recollection; forgets the revision; hides cancelled goals | all killed |
| M111–M113 | read window shrunk to nothing; seek past the end; tail limit dropped | all killed |
| M114, M115, M117, M118 | the premise detector knows only "everything"; "working on" is not a work question; every turn gets the record; no turn does | all killed |
| M116 | an unmatchable alternative added to the outcome group | **withdrawn — equivalent by construction.** Adding `zz` to an alternation loosens nothing; my error in writing it |
| M116′ | the outcome half of a premise is optional, so scoping alone is enough | killed ("Everything is fine, thanks." would have attached the record) |

**31 killed, 5 withdrawn.** A mutant counts only when it is syntactically
valid, the production path executes, and the *named* property fails. Four of
the five withdrawals were mine to make honestly: two wrong-reason (M101,
M101′), two equivalent (M80, M105), and one — M116 — simply a mutant that
mutated nothing.

---

### The detector's breadth was validated but not pinned

Both S13C-1 and S13C-4 were widened after checking a few dozen phrasings by
hand. Then I wrote "17 phrasings validated in both directions" in this report,
went looking for the evidence, and found that the committed suites assert
**two** premise phrasings between them. The breadth was real when I checked it
and protected by nothing afterwards: an edit narrowing the detector back to the
one sentence the end-to-end suites use would have left every test green.

The end-to-end suites are right to be narrow — each turn there costs a whole
interpreter — so the breadth now lives in a suite where a phrasing costs a
function call: **46 recognised shapes** (18 questions, 14 claims, 14
"still running") and **18 negatives**.

The negatives are chosen as NEAR MISSES rather than unrelated sentences — "is
the tap still running", "are you working on Sunday", "does that work for you" —
because a negative list of unrelated sentences would pass against almost any
regex, including a broken one. The suite asserts that too.

One boundary is recorded rather than fixed: **"All set then?"** is not
recognised. "all set" is an outcome phrase, but with nothing scoping it the
sentence is as likely to mean "are you ready?" as "did the work finish?". It
sits in the negative list with that reasoning attached, so it is a decision
rather than an accident.

---

## Where my own tests were wrong

This is the part worth reading, because it is where the stage nearly passed
while proving less than it claimed.

**The journeys were exercising one code path.** The first version queued work
in one life and tried to claim it in the next. Every boot cancels the queued
work it inherits — deliberately, so nothing runs unasked — so by the next life
the only claimable row was the `__decide__` continuation a resume creates. Six
"passing" journeys, almost nothing tested. Diagnosed by dumping the claim
sequence rather than reasoning about it: the claims came back `['__decide__']`
and then `None`. A running Nova plans and executes in the same process, so the
journeys now do too.

**Four assertions would have held over an empty collection.** The
reconstruction suite filtered with `if v`, so an answer that came back *empty*
was skipped rather than counted — a turn producing no grounding at all would
have been recorded as compliant. Three others ("never offered for execution
again", "nothing was reattributed anywhere else", "nothing still in flight")
would each have held with nothing there. M108′ — the work record rewritten to
present itself as recollection — is killed by the fixed version and survives
the original.

**Two §18 checks passed for the wrong reason.** One matched the word "running"
inside an *honest explanation* ("interrupted by a restart while it was
running") instead of the status token. The other asserted the absence of work
in a grounding that had never been attached at all, because the question I
chose was not one the detector recognises.

**Four §19 invariants were too weak,** and only mutation testing said so: the
"refused completion" invariant asked the product for its own verdict, so a
mutant that disabled the fence also moved the expectation; revision
monotonicity was sampled only where snapshots happened to land; the stale
action mostly hit already-terminal rows, where first-write-wins refused it
before the fence was consulted; and it fired three times in twenty sequences.

**One assertion was mis-specified against a correct product.** My §10 check
"R3 is newer than anything on disk" ignored that `resume_goal` deliberately
inserts one `__decide__` continuation *at the new generation*. The contract was
already right; the assertion was reworded to the property that matters — the
only *runnable* continuation belongs to R3.

---

## The one red run, and what it is honestly worth

The soak produced exactly one failure in 160 suite runs: the
foreground/background suite hung and was killed at its 240 s timeout, against
a median of 32.7 s. That suite is the one that boots the real backend against
a root whose previous writer was `os._exit`-killed, so it is the right place
to be suspicious.

**The evidence it left was the word "timeout"** — and chasing that turned up a
diagnostic blind spot three faults deep, each of which alone would have
defeated the other two:

1. children never armed a watchdog at all. `harness.run()` arms faulthandler,
   but the restart-harness preamble calls `asyncio.run(_main())` directly, so
   `NOVA_IT_WATCHDOG_S` in the child environment did nothing for the whole
   stage;
2. even armed, 300 s sat ABOVE the 240 s parent timeout, so the parent would
   always have killed the child first;
3. and `run_step` discarded the child's stdout and stderr on timeout, throwing
   away anything it had managed to say.

All three are fixed: faulthandler is armed inside both preambles, the watchdog
is derived from each step's own timeout so it always fires first, and the
child's output is preserved in the failure. Verified by handing the harness a
child that hangs on purpose — the watchdog fired at 25 s of a 45 s budget and
produced every thread's stack, where before there was one word.

**The hang itself is unreproduced.** 25 dedicated runs afterwards: zero
failures, 29–35 s, no outlier at all. Total exposure is one failure in 40
observed runs of that suite.

What can honestly be said:

- the failing run was the FIRST test process after a Windows reboot, and was
  7× the median while the 34 runs around it clustered within 6 s of each other;
- a latent race would more usually show a tail — some runs at 60 s, some at
  90 s — rather than one run at 245 s and everything else inside a 6 s band;
- but 25 clean runs do not prove absence, and "the machine was busy" is the
  convenient answer, which is reason to distrust it rather than to adopt it.

So it is recorded as **unexplained, not resolved**. The material change is that
the next occurrence — in a gate, a soak, or a user's machine — will produce
every thread's stack instead of a word. If it never recurs, that costs nothing;
if it does, the diagnosis is already waiting.

---

## Recorded, not fixed

- **A request older than the audit read window is never closed out.** It stays
  `pending` in the trail for ever. Bounded-cost boots were judged worth more
  than closing out a request nobody can click on any longer; the limit is
  tested, so it cannot drift silently.
- **`describe_work_state` reports the first 12 goals and 12 steps each.** A
  machine with more than that in flight would have the rest omitted from an
  answer. Not reached in any scenario here, and unchanged from 13B.
- **`superseded` still resolves to `failed` in the status axis.** Neither label
  is precise — the work did not fail. This is the missing sixth status already
  recorded in the 13B state-model note; it is a schema and UI change and should
  be decided once, with the `ask_user` missing-state work.

---

## Frontend and startup (§24)

Stage 13C changed no frontend file — the branch diff is four Python modules
and the test tree — but the gate is worth stating rather than assuming.

- `npm run build` passes (vite, 22.6 s, no errors).
- `node --check` is **not** a valid gate for `.jsx`: JSX is not JavaScript, and
  node rejects the extension outright. The vite build is the syntax gate that
  actually covers those files, and it is green.
- **Real startup smoke** is not a separate script here because it is what most
  of the stage already does: `boot()` starts the real backend — routes, runtime,
  memory, ASGI — against a real directory, and the S13C suites drive real
  `POST /chat`, `POST /permissions/resolve` and `POST /autonomy/stop` through
  it. A boot that failed would take the suites with it.
- **Abrupt-start recovery smoke** is §18's first case: a step left claimed and
  another left queued, `CRASH()`, then a brand-new process whose very first
  action is a person asking "What happened?" over real HTTP. It comes up, it
  finds nothing in flight, and the answer it composes is handed a work record
  in which no step is running or queued.

## Verification

### Three consecutive full gates

All three on the same head, `c0fa4604c8b369a3366dffdc43fab34bbd391a26`, with
the working tree unchanged between them (verified before each):

| gate | result | exit |
|---|---|---|
| 1 | 152 / 152 | 0 |
| 2 | 152 / 152 | 0 |
| 3 | 152 / 152 | 0 |

No suite went red in any of the three. Captured from the child process rather
than with `Out-File`: `run_tests.ps1` uses `Write-Host`, which bypasses the
pipeline, and piping it inside PowerShell produces a 0-byte log — a trap this
programme has already fallen into once.

**Ordering, stated rather than implied.** This report is the only thing that
changed after those gates ran, in a `docs/`-only commit. A report cannot
contain the results of a run over itself, and the alternative — gating a head
whose report claims results it does not yet have — would be worse. Nothing
executable differs between the gated head and the final one.

### Soak (§21)

160 suite runs, each a fresh process against a fresh directory:

| section | runs | median | result |
|---|---|---|---|
| restart states | 20 | 27.1 s | clean |
| crash windows | 20 | 50.9 s | clean |
| replay safety | 20 | 87.9 s | clean |
| revision isolation | 20 | 28.7 s | clean |
| permission durability | 20 | 57.7 s | clean |
| migration | 10 | 14.0 s | clean |
| reconstruction | 10 | 60.2 s | clean |
| foreground / background | 10 | 32.7 s | **1 hang** (see above) |
| journeys | 10 | 100.7 s | clean |

Plus 25 further dedicated runs of the foreground/background suite while hunting
that hang: all clean, 29–35 s.

### Generated restart sequences (§19)

| seed | sequences | result |
|---|---|---|
| 0 | 300 | 300/300 held every invariant |
| 100,000 | 500 | 500/500 |
| 250,000 | 300 | 300/300 |
| 500,000 | 300 | 300/300 |

**1,400 sequences**, 1,832 interpreter lives in the first two batches alone,
with RESTART_CLEAN and RESTART_CRASH as ordinary members of the action
alphabet.

### Journeys (§13)

Six journeys, **231 genuine state transitions**, counted by diffing
authoritative rows by identity after every life. J1 56, J2 32, J3 63, J4 30,
J5 25, J6 25.

### Frontend (§24)

`npm run build` green (vite, 22.6 s). No frontend file changed in this stage.

---

## What this does and does not establish

**Established.** Across real process boundaries — not `stop()` then `start()` on
a live object — Nova reconstructs what happened, what did not, what may
continue and what must never continue, from durable rows alone. Interrupted
work is `unknown` rather than guessed either way; a worker from a run that
ended cannot finish it; a permission request the crash interrupted cannot be
approved afterwards and is no longer misdescribed in the audit; an old database
opens and answers honestly instead of failing to open; and a person asking any
of the ordinary questions after a restart is answered from the record rather
than from an empty transcript.

**Not established.** One hang, once, unreproduced in 25 dedicated attempts and
absent from all three gates — recorded above as unexplained rather than
resolved. And the limits under "Recorded, not fixed" are limits, not proofs.

Three green gates are not by themselves an argument that this is right; they
are an argument that it is not obviously wrong. The argument that it is right
is the mutation ledger and the sequences, and both are written down above
including the parts where my own tests were the thing at fault.

**Not merged, and not proposed for merge here.** Stage 13C is submitted for
independent review.
