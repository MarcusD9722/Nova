# Stage 14 — completion integrity

**Question:** when Nova says work is done, is it actually done?

**Answer before this stage:** no, and not by a small margin. A project was
"complete" when the build loop finished writing files. A calculator that could
not subtract was complete. A program that crashed on every run was complete,
and said so four lines below a request it had not met. Nothing in the system
distinguished *files were generated* from *what was asked for was delivered*,
because nothing in the system recorded what was asked for in a form anything
could check against.

**Answer after this stage:** completion is derived, never assigned. Seven
states — `idea`, `planned`, `scaffolded`, `partially_implemented`, `failing`,
`passing`, `complete` — are computed as a pure function of recorded facts:
what was asked, which acceptance criteria were agreed for it *before* any code
existed, and what evidence exists that each one is met *for the code that is
there now*. `complete` requires a sealed contract and current satisfying
evidence for every required criterion. Everything else is a lesser state with
a reason attached.

---

## What "complete" now requires

| Requirement | Enforced by |
|---|---|
| A durable requirement exists | `project_requirements`, revisioned |
| Every criterion quotes the request it derives from | `origin_quote`, verified as a span at write time |
| Criteria are recorded before implementation | `_establish_contract` runs before planning |
| The contract covers the whole request | `seal_contract` refuses while a clause is unquoted |
| Evidence describes the code that exists now | artifact digest fence |
| Evidence describes the current requirement | revision fence (see below) |
| A human criterion is settled by a human | `verify_kind`, `human_decisions` |
| A waiver has a decision behind it | `decision_id` on the evidence row |
| Saying "done" happens once per transition | durable `completion_announcements` |

An unsealed contract can never reach `complete`. That is deliberate: sealing
means "these are all of the things being asked for", and completing without it
would mean "everything we happened to write down is done" — which is exactly
how a forgotten requirement becomes a finished project.

---

## The defects

Nine were found and fixed, each reproduced on the pre-fix code against
authoritative rows before anything was changed.

**S14-1 — a partial improvement reported as a complete one.** Three features
requested; one skipped, one reverted for not compiling. The summary claimed all
three and the failures were discarded from the log.

**S14-2 — the calculator that could not subtract.** "A calculator that can add
and subtract two numbers." Only addition was implemented. Complete.

**S14-3 — the program that crashed on every run.** "Build complete." in the log
of a project that needed attention, and `project.completed` published.

**S14-4 — the episodic promoter read a field that no longer existed.** It read
`status` while the writer had moved to `state`, so completion never reached
long-term memory. Silent: nothing errors when a dict key is missing and the
default is falsy.

**S14-5 — announcement idempotence was process-scoped.** The ledger lived in a
dict. Within one process, exactly-once held. Across a restart, every new
process re-announced the same transition, because a fresh process starts with
an empty dict and `"" != "complete"` publishes. Exactly-once has to be scoped
to the transition, and a transition outlives the process that observed it.

**S14-6 — `status_text` read PROJECT.md.** The status tool parsed the file the
builder had written, so it reported whatever had last been written down rather
than what was true. A projection was being used as a source.

**S14-7 — completion never reached `/chat`.** The evaluator was correct and
nothing asked it. Asked "is it done?", Nova answered from the same vague
context it always had.

**S14-8 — a sealed contract could be extended.** `set_criteria` accepted new
criteria for a sealed revision, which made sealing a formality rather than a
commitment.

**S14-9 — a contract answer vanished silently.** The confirmation path dropped
the user's response with no error and no record.

---

## Mutation ledger (§14)

Thirty mutants, **M119–M148**, against the model, the fences, the contract, the
announcement ledger, the projections and the builder. The next unused number
was verified against the Stage 13C report rather than assumed.

**27 killed on the first suite. 3 survived. All three were investigated before
anything was changed, and they were three different things.**

### M142 — my own broken mutant

`return "" or (X)` evaluates to `X`. The mutation changed nothing, so it
survived a suite that was working perfectly. Rewritten as
`return "" if True else (X)`, which actually returns the empty string, it dies
immediately. **A mutant that cannot fail proves nothing about the test that
"missed" it** — counting it as a survivor would have sent me to strengthen a
test that was already correct.

### M140 — equivalent, and a guard nothing could reach

`shown = getattr(verdict, "state", None) or status` → `shown = status`.

Every call site that passes a verdict passes `status=verdict.state`; the sites
that pass a bare status pass no verdict. No production path can tell the two
apart, so the mutant is equivalent. But that is the finding: the `or status`
fallback is a defence against a divergence nothing currently creates — an
unreachable guard, which is a failure mode I keep producing. It is now
exercised by a unit-level test that hands the writer a status contradicting the
verdict, **labelled as exactly that** rather than dressed up as a production
scenario.

### M145 — a real gap, and worth more measured than killed

Removing the per-item quote filter in `derive_criteria`. Measured on both
sides, with a model that fabricates one quote out of three:

| | criteria recorded | contract |
|---|---|---|
| filter present (production) | **2** — `adds an entry`, `shows a total` | **sealed** |
| filter removed (M145) | **0** | **unsealed** |

`set_criteria` is atomic, and `_establish_contract` catches its refusal and
carries on. So one hallucinated quote among three does not cost the model its
invented criterion — **it costs the user both of the truthful ones**, and the
project ends with no contract at all. The per-item filter is the only thing
between a fabricated quote and a silently empty contract.

Nothing asserted the surviving half, which is the half that distinguishes "the
bad one was dropped" from "the whole batch was refused". It does now.

**Final: 30 mutants, 30 killed, 0 surviving, 0 withdrawn as equivalent-and-left
(M140 was made reachable instead).**

---

## The revision fence cannot fire, and that is now written down

Five real defects were injected into the production evaluator to find out
whether the generated model is a detector or a mirror. Three were caught
immediately; two were not.

One was a gap in the sequences — a waiver with no decision behind it, which the
service refuses to write, so no sequence could produce one. Fixed by adding an
action that writes such a row directly (an older schema, a repair script, a
corrupted store). Now caught.

The other is a finding. Measured through the public service:

- a stale `CheckContext` redeemed after a correction, and
- a human decision asked before a correction and resolved after it

both produce evidence bound to the **old** criterion id, and `carry_forward`
mints a **new** id at every revision. **Zero rows crossed a revision onto a
live criterion.** The revision fence in `current_verdict_for` cannot fire in
production; the artifact digest fence is what actually catches staleness.

The fence stays — it costs nothing, and it stops being unreachable the moment
anyone makes `carry_forward` preserve ids, a plausible refactor that would
otherwise silently admit evidence for superseded requirements. It is now
labelled in the source as defence in depth covered by unit tests only, rather
than left looking like a live guard.

---

## Where my own tests were wrong

The recurring failure of this campaign is tests that pass while proving
nothing. Every one below was found by instrumenting rather than by a red run.

**The generated model reached `complete` zero times.** Twenty sequences held
every invariant. Then I counted the states they had reached: 358 `idea`, 0
`complete`. The random walk could not chain request → cover every clause →
seal → implement → prove, so the invariant the whole stage exists for was never
evaluated. The generator now weights whatever a project still lacks, and **the
run fails if any state goes unexercised**. A pass count with no coverage count
underneath it is a number, not evidence.

**A journey "proved" multiplication against code with no multiply.** `_prove`
records a verdict; it does not run a check. J1 recorded a pass for a criterion
the code could not satisfy and then asserted the project was not complete — the
test telling exactly the lie the stage exists to catch. The criterion is now
left unproven, which is what was true.

**An expectation that contradicted the documented rule.** J6 asserted
`partially_implemented` where the product returns `passing`: when *every*
outstanding criterion is human-blocked, the state is "every machine-checkable
criterion passes; N awaiting a person". The code was right, the test was wrong,
and the test now asserts the real rule and what it must still deny.

**Escape mangling, repeatedly.** Patch scripts kept collapsing `\n` into real
newlines mid-string, producing syntax errors and, worse, string literals that
silently differed from what was intended. Assertions that split on a marker and
compare the value are used instead of literals containing escapes.

Earlier in the campaign, and recorded here because the pattern is the point:
process-wide `BUS.recent()` used for attribution (twice), vacuous assertions
over empty collections, a prompt-wide matcher that let one file prove three
criteria, fixtures under the 40-character floor that silently took a skip path,
unawaited coroutines whose truthiness stood in for a value, and a baseline
captured after the setup call that changed it.

---

## Verification

### Generated completion sequences (§14)

500 sequences per run, each a random walk of 14–34 actions over two projects,
checked after **every step** against an oracle that tracks admissibility from
what the sequence did rather than re-implementing `derive_state`.

| | |
|---|---|
| sequences | **500/500 held every invariant** |
| `complete` reached | 407 |
| `failing` | 1,017 |
| `passing` | 1,484 |
| `partially_implemented` | 1,363 |
| `scaffolded` | 4,151 |
| `planned` | 3,720 |
| `idea` | 11,712 |

The run **fails** if any state goes unexercised, because the first version of
this suite passed 20/20 while reaching `complete` zero times.

### Six journeys (§14)

The first submission reported 282 authoritative *observations* and only 174
transitions, and the review was right to block on it: the requirement is more
than 225 meaningful **transitions**, and half of what there was came from one
loop repeating a single drift forty times. Repetition is not coverage.

The journeys were rebuilt around distinct Stage 14 properties, and the counter
was made stricter rather than looser:

- a transition is counted **only** when an authoritative read differs from the
  previous authoritative read. Reading twice, asserting twice about one read,
  or acting without changing anything all count zero;
- **the first read of a project is not counted at all.** It is the baseline —
  the project did not move to get there — and counting one per project was
  seven transitions of bookkeeping. Excluding them cost 7 and is correct;
- a step that only *observes* a project may never record a transition, which
  is asserted, and which caught a real instance the moment it was added;
- every counted transition prints as `from -> to (what caused it)`, so the
  total is auditable line by line rather than asserted;
- the totals are **summed from per-journey counters**, never typed in.

| journey | transitions | what it walks |
|---|---|---|
| J1 | 32 | build, refute, repair, six corrections, re-anchoring, retirement, undecided checks, an optional-only contract |
| J2 | 42 | human confirmation: stale by drift, stale by *requirement change*, refused then granted, retired, and a decision that goes stale before it is answered |
| J3 | 48 | seventeen distinct kinds of artifact change, three of which must NOT invalidate, plus drift against a *failure* |
| J4 | 32 | sealing, a correction un-covering a sealed contract, explicit retirement, undecided checks at two revisions |
| J5a | 37 | two live projects alternating, with attribution asserted after every action |
| J5b | 15 | the other half of that, including a correction that moves one and not the other |
| J6 | 28 | a restart between every *kind* of transition, five criteria settled one at a time across processes, drift while down, ledger reconciliation |
| **TOTAL** | **234** | 469 authoritative reads, 7 baselines excluded |

**22 distinct kinds of transition** (`from -> to` pairs), and every one of the
seven states is the **destination** of a real transition, not merely observed
in passing. `idea` as a destination exists only in one place and had to be
constructed deliberately: a correction recorded *before the first file exists*,
where nothing has been agreed for the new revision and nothing has been built.

**Do the new assertions kill anything?** Twelve mutants relevant to the new
journey material were run against the journeys suite **alone**: ten died there,
including `an undecided check counts as satisfied`, `an unsealed contract can
complete`, `a machine check can satisfy a human criterion`, `the announcement
ledger is ignored`, and both artifact-set exclusions. Two survived — an empty
evidence digest, and a waiver with no decision behind it — because no sequence
of journey actions can write either row: the service refuses. Those two claims
were **withdrawn** from the journeys rather than left standing.

Two failures found while extending, both mine, both traced before being fixed:
a project asserted COMPLETE on a contract nobody had sealed (it is `passing`,
by design, and the reason string says so), and two deletion cases that returned
the project to an artifact set that had **already been proven**, so the earlier
evidence legitimately described it again — the same property asserted
deliberately three lines further down.

### Restart and durability (§16)

**46 distinct interpreters**, each spawned, run, and allowed to exit, sharing
only files on disk:

- all seven states established in one process and read back in the next
- a human question asked in one process, open and findable in three later ones,
  answered in a fifth — and not complete in any of the three
- a pass recorded before a restart does not certify a file edited while **no
  process was running**; adding a file invalidates it, and so does deleting one
- six separate processes evaluate a completed project and announce it **once**
  between them

### Soak (§15)

**209 runs, 85.5 minutes, no failures.** One red run in twenty would have been
a finding, not noise.

| suite | runs | median | p90 | slowest |
|---|---|---|---|---|
| model | 20 | 0.2s | 0.2s | 0.3s |
| service | 20 | 2.7s | 3.1s | 3.4s |
| fencing | 20 | 3.5s | 3.8s | 3.9s |
| truth | 20 | 3.9s | 4.3s | 4.6s |
| events | 20 | 6.7s | 7.4s | 7.5s |
| projections | 20 | 3.9s | 4.7s | 4.9s |
| chat | 20 | 39.1s | 40.4s | 41.2s |
| semantics | 15 | 14.8s | 15.8s | 16.3s |
| isolation | 15 | 29.8s | 31.6s | 33.3s |
| endpoint matrix | 15 | 11.7s | 13.2s | 13.5s |
| journeys | 10 | 14.4s | 19.3s | 19.3s |
| restart | 10 | 63.8s | 68.1s | 68.1s |

Plus **1,400 generated sequences across four fresh seed sets** (0, 100k, 250k,
750k) — 300/300, 500/500, 300/300, 300/300. Fresh seeds, not repeats: running
the same 500 sequences again proves only that they are deterministic.

### Performance and storage (§17)

The question was not "is it fast" but "does it get slower as a project
accumulates history", because `evaluate()` reads every evidence row for a
project with no bound, and Stage 13C already found that shape once.

| evidence rows | p50 | p90 | max | store |
|---|---|---|---|---|
| 100 | 26.70 ms | 51.27 ms | 64.31 ms | 0.4 MB |
| 500 | 32.28 ms | 66.78 ms | 101.04 ms | 0.6 MB |
| 2,000 | 13.18 ms | 33.17 ms | 67.05 ms | 1.2 MB |
| 8,000 | 17.53 ms | 42.50 ms | 71.16 ms | 3.5 MB |

**80× the history, 0.8× the p90.** The evidence scan is not the dominant term
at this scale — digesting the project directory and opening the store are — so
the early rungs are warm-up, not a trend. **That is a measurement of the range
tested, not a proof of constant time.** The query is still unbounded in
principle and would matter at some much larger scale; the numbers are recorded
so the next person has them rather than my reassurance.

A hundred projects in one store cost **0.5 MB**, and one project's state is
unaffected by the other 99 (28–38 ms p90). Each recorded check costs 0.38 KB.

### What gate 1 found

The first gate run came back **164/165**. The failure was real and is written
up above: a pre-Stage-14 integration test expecting `complete`, a state machine
reporting `idea` for a directory of working code, and a `PROJECT.md` that
contradicted itself in the understating direction. Both were fixed, the full
mutation set was re-run on the new head (**30/30 killed**), and the three gates
below were run from scratch on that head.

### Three consecutive full gates

An earlier set of three gates passed 165/165 on `c1db7d1`. They are **not**
counted. The journey work changed the test tree afterwards, which makes those
runs preliminary by definition: a gate describes the head it ran on, and that
head is no longer the final one.

The counted gates all ran on head
`cbfef0d4f18310de3779974728fde6790cc9f399`, tree
`a26d131b603bdc962c48d63fd4de9735bc6eca9a`. Before each one, both the tree hash
and the working tree were re-checked, with the check scoped to the
**executable** paths (`core`, `backend`, `memory`, `tests`, `frontend`) so that
a docs-only difference could not silently pass as "unchanged" and nothing
executable could silently differ:

| gate | started | result | exit |
|---|---|---|---|
| 1 | 09:57:48 | **165 / 165** | 0 |
| 2 | 10:51:35 | **165 / 165** | 0 |
| 3 | 11:45:46 | **165 / 165** | 0 |

No suite went red in any of the three. That includes the frozen Stage 13A, 13B
and 13C suites, which run in every gate; they were also run individually
against this tree beforehand — **25 of 25 pass**, so the completion work did
not disturb the restart, durability or reconstruction guarantees they pin.

Output was captured from the child process rather than piped inside
PowerShell: `run_tests.ps1` uses `Write-Host`, which bypasses the pipeline and
produces a 0-byte log, a trap this programme has fallen into before.

**Frontend.** `npm run build` in `frontend/` succeeds on this tree (24.1s,
chunk-size advisories only, all pre-existing). `node --check` is not a valid
gate for `.jsx` — node rejects the extension outright — so the vite build is
the syntax gate. No frontend file changed in this stage; the build was re-run
because the executable tree did.

**Ordering, stated rather than implied.** This report is the only thing that
changed after those gates ran, in a `docs/`-only commit. A report cannot
contain the results of a run over itself, and gating a head whose report claims
results it does not yet have would be worse. Nothing executable differs between
the gated head and the final one.

---

## What this establishes, and what it does not

**Established.** Completion is derived from recorded facts and cannot be
assigned. Files existing is not completion, at any point in the code, and the
mutants that try to make it so all die. Evidence is bound to the artifact it
examined and the requirement it was gathered under. A criterion only a person
can settle is not settled by a machine, and a waiver has a decision behind it.
Everything above survives real process restarts, and none of it depends on
process-local state. Saying "done" happens once per real transition, across
restarts.

**Not established.** That the acceptance criteria mean what the user meant.
Coverage counts clauses that are *quoted*, not clauses that are *understood* —
a weak-but-quoted criterion, a check that proves something adjacent, and one
broad criterion swallowing three features all reach COMPLETE. That boundary is
asserted deliberately in `test_completion_semantics_s14.py` as an executable
record of where the guarantee stops. Only an **unquoted** clause is caught.

Nor does anything here establish that a real language model produces good
criteria: every criterion in these tests is scripted. What is established is
that whatever criteria exist are recorded before implementation, verified as
spans of the request, and enforced afterwards.

**Not claimed.** No `actor` or `channel` string in this system is proof that a
physical human did anything. They are attribution, not authentication.
