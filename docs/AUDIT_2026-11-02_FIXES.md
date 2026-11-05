# Audit 2026-11-02 — what was fixed

Companion to `docs/AUDIT_2026-11-02.md`. That document says what was wrong;
this one says what changed, how it was proved, and what is still owed.

All 59 findings are addressed. Every fix has a regression test that fails when
the fix is removed — that is what the mutation column means, and it is the
point: a green test that also passes with the fix reverted proves nothing.

**Honest limits, stated once.** One thing here is still unmeasured: L3's cloud
token counting is not verified against the provider, because that costs money
and nobody asked for it. Everything else was run, including the two claims that
were deferred for VRAM — the estimate that wrote them off was wrong (it counted
the vision projector, which the text path does not load; the text model is
6.85 GB, not ~11.5 GB), and measuring them found two regressions in the fixes
themselves — and, behind those, a retry defect the audit never reached because
the leak had made its failure path unreachable. All three are in **Two
regressions the M10 fix caused, and the defect they hid**, below.

---

## Critical

| # | What changed | Proved by |
|---|---|---|
| C1 | `/dev/propose` and `/dev/apply` go through `PermissionBroker` (`dev.apply`, ADMIN), both check a local origin, and `start_nova.ps1` mints `NOVA_API_TOKEN` and passes it to the backend, Vite and Electron | `test_audit_security_2026_11.py`; 5/5 mutants caught |
| C2 | A `threading.Lock` held inside the worker thread for the whole stream, plus `stream.close()` on cancel and `aclosing` on the generators. `GPU_SEM` could not do this: the work runs in `asyncio.to_thread`, which cancellation cannot reach | **Real weights: 3 boots × 6 mid-reply disconnects = 18/18 survived, exit 0, zero CUDA asserts** (previously 7/7 fatal on the first close). A mutant without the lock reaches 5 threads inside the model |
| C3 | `references_project_work()` gates the project pre-pass; the identity choke point vetoes any mutation by a non-owner | 19/19 of the audit's own sentences reach conversation; 11/12 real instructions still act |
| C4 | Deterministic status answers come from the authoritative record, and `_claims_completion()` prefixes a notice when a reply claims work that did not happen | `test_audit_routing_2026_11.py` |

## High

| # | What changed | Proved by |
|---|---|---|
| H1 | `ATTACHMENT_HEADER` is one definition; `_sanitize_user_text` partitions on it and passes attachment content through verbatim | 11/11 hijacks reach the model, 11/11 real requests still handled |
| H2 | Clock, weather, maps and nearest patterns are end-anchored and require a real signal; `looks_like_code_question()` keeps code out | `test_audit_routing_2026_11.py` |
| H3 | `core/prompt_budget.py`: a token budget with per-block floors and a trim order. Persona, grounding and the newest turns are kept | 13,438 → 6,380 tokens, newest lines kept, UI can start a new conversation |
| H4 | Weather tail/adjective rules, so "bad weather is the worst" is not a city | routing suite |
| H5 | `_term_coverage()` ranks by how much of the query a fact covers | the locker-code fact went from unranked to rank 1 |
| H6 | `memory.forget`, `reminder.list` and `reminder.cancel` exist, so the questions have tools | `test_audit_memory_2026_11.py` |
| H7 | UTC bounds in `memory.recall` | 4/4 of yesterday evening's turns found |
| H8 | `_load_upcoming_dates` and the exec block gated on `personal_ok` | memory suite |
| H9 | `_as_notes()` + a `field_validator`, and `summarizer_invalid` at warning | real model output validates |
| H10 | Tiers for `shell.read`/`shell.exec`/`code.write`/`dev.apply`/`discord.send`; `_gate` reads the broker at call time and **fails closed**; a read-only command allowlist; `code.read`/`code.write` refuse secret paths | security suite; 5/5 mutants caught |
| H11 | Question first in the embedding text (`core/tools/selector.py`) | cos 1.0000 → 0.8719 between two different questions |
| H12 | `null` origin is allowed only when a token is set, and `role=owner` can no longer be asked for by a request | import-time check + the router refuses 403 |
| H13 | MCP reads `verdict["decision"]`, waits on `needs_confirmation`, and fails closed on an unrecognised shape | `test_audit_mcp_2026_11.py` **against the real broker**; 4/4 mutants caught |
| H14 | One-shot weekdays and dates; the date expression is removed before a time is read; a bare hour takes the sooner reading, or asks | `test_audit_reminders_2026_11.py`, 28 phrasings + 11 that must ask; 10/10 mutants caught |
| H15 | `_python_stack_note()` names what is actually importable; planner attempts escalate to thinking-off | `test_audit_projects_2026_11.py` |
| H16 | A relative clause after an explicit name ends the title; `as`/`like` are ambiguous; pending-name state makes the answer start the build. **Found while fixing it:** starting a project had no identity check at all | projects suite; 8/8 mutants caught |
| H17 | `_run_test_file` runs pytest and requires collected > 0 | `test_audit_verification_2026_11.py` |
| H18 | Checks run on a throwaway copy; a check's own crash is inconclusive (an `AssertionError` is still a finding); the verdict is read from a `PASS`/`FAIL`/`CANNOT_CHECK` line; `CANNOT_CHECK` is recognised anywhere in a reply **that has no code fence** | verification suite; 12/12 mutants caught |
| H19 | `core/project_html_check.py`: `node --check`, then the page's scripts against a DOM stub, driven 60 ticks with arrow keys, with one repair round. Says plainly when nothing ran | **Verified read-only against Marcus's own projects: `aetherlings` 13 local scripts load and draw 11,007 times; `last-stand` 11,025** |
| H20 | One definition of the combined request text, used by the planner and by `carry_forward`; a contract-less revision is stepped over; `memory.requirement_at()` added | verification suite drives build → improve → improve |
| H21 | Vision unloads after use and takes the C2 lock | GPU suite |

## Medium

| # | What changed |
|---|---|
| M1 | Real tool descriptions and an allowlist for the unattended supervisor; `_NEVER_UNATTENDED` |
| M2 | The twelve plugin READ tools are retry-safe — each verified in its plugin source, not from its name. `discord.send` and `email.draft_reply` deliberately are not |
| M3 | `shell.exec` catches `CancelledError` and kills the process tree (`taskkill /T /F`) |
| M4 | The per-call cap counts work attempted, not files looked at, so "call again to continue" advances |
| M5 | Birthday heads-ups fire at 9am **local**, their wording is decided when they fire, and a missed one is no longer swallowed by the `__nova_` filter |
| M6 | Recurrence steps on the local wall clock — **a daily 8am stays 8am across the 1 November DST change** |
| M7 | The agent loop records each successful tool call, so skill learning has the input it never had (owner-scoped) |
| M8 | An identical successful call stops the loop; the decide step is told not to write the answer it will never be shown |
| M9 | `thinking=False` for the story stream, its retry and the story bible |
| M10 | The fast path uses the template's own `enable_thinking=false`. **Measured against the real template:** the old prefill rendered as a finished assistant turn plus a new one with `<think>` reopened, exactly as the audit said |
| M11 | `split_people_value()`: `child="Mateo\|Liam"` becomes two facts; `pet="Mochi\|cat"` deliberately does not |
| M12 | `chat_stream_failed` reaches the bus, and **56 real failure events moved from debug to warning**. Nine stay at debug, each with the reason written next to it |
| M13 | UTF-16 only with a BOM, then cp1252, then latin-1. `"Zoë Sanchez, manager"` decoded as `潚⃫慓据敨ⱺ洠湡条牥`; it does not now |
| M14 | The index walk prunes with `os.walk` and runs in a thread. **24.1 s → 2.2 s** |
| M15 | Methods are symbols (`impact_of("initialize")`: `none` → `high`, 15 files); the skip list tests the relative path (a project under `build/` indexed 0 files); truncation is reported; `latest.py` is not a test |
| M16 | The exposure note and boot warning say what actually locks the UI out |
| M17 | One `_as_local` / `_local_day` helper: habit hours, mood days and wellbeing days are Marcus's |
| M18 | An attribute-shaped topic is stored as an attribute, so a move supersedes it. **Dallas → Austin now leaves one row, not two** |
| M19 | `thinking=False` on the deep-mode revision, and an empty revision says what the reviewer objected to instead of sending the draft as if it passed |
| M20 | `.env` is loaded before any core import, so the six module-scope settings read it |
| M21 | No per-call `redirect_stdout`; `verbose=False` at load |

## Low

| # | What changed |
|---|---|
| L1 | `"thur": 3` |
| L2 | "day before yesterday" is checked before "yesterday" |
| L3 | The daily token cap persists to disk and survives a restart; streamed calls are counted (estimated from characters, and labelled as an estimate) ⚠ |
| L4 | `McpManager.close()` is called at shutdown; the child's stderr is drained so a chatty server cannot block |
| L5 | `system.warning` and `cloud.capped` have handlers; the handler for the never-published `memory.read` is replaced by ones for the events that are published |
| L6 | `active_project` has a writer — a project named in the message wins, else `last_active` |
| L7 | `system.time`, `email.recent`, `calendar.today`/`upcoming` replace names that do not exist; `emails`, `events`, `messages`, `reminders` added to the list keys so "the second one" resolves |
| L8 | "our" means the speaker, and a modifier may sit between it and the date word |
| L9 | `/autonomy/stop` stops the goal supervisor too, and a running task is recorded as stopped **on request** rather than by a restart that did not happen |
| L10 | The speaker registry creates its own schema, so a read on a fresh database does not 500 |
| L11 | `reject_proposal` refuses anything not pending |
| L12 | Dead `Planner` removed; `audit.jsonl` keeps the newest 20,000 lines; the TTS phrase cache is bounded; `prune_episodes` has a caller; the upcoming-dates list uses the local date; `repeat_penalty` is 1.0 for code and 1.15 for prose |
| L13 | Lesson capture requires a directive about **Nova's** behaviour: "Don't forget the milk" is not a lesson, "Don't forget to be concise" is |

---

## What the full sweep changed

The 195-suite sweep came back 172/195 the first time. All 23 failures were
caused by this round, and the diagnosis is worth keeping, because two of them
were my fixes going further than the findings asked:

**Pulled back.** `Origin: null` is allowed again by default — dropping it
broke `/permissions/resolve`, so with no token Electron could not approve
anything, which is a silent failure of the path the whole permission system
rests on. `NOVA_DROP_NULL_ORIGIN=1` is the opt-in and the minted token is the
real boundary. And `discord.send` is no longer brokered: in `guarded` mode a
STANDARD capability needs a click, so every Discord message would have waited
for one. M1 asked only that the *unattended* planner not be offered it, and
`_NEVER_UNATTENDED` already does that.

**Over-reach in H18, corrected.** A check that exits 0 without printing a
verdict is evidence again: requiring a declared verdict discarded every
assert-based check, which is most of them, and H18's actual false pass was a
script that *printed* `CANNOT_CHECK` and exited 0. And
`ImportError: cannot import name` is a finding about the program, not a
crashed check — the new crash attribution was shadowing a distinction the
original code made deliberately.

**C3's gate was one branch too wide.** Requiring work vocabulary to resolve a
project target silenced the whole deferral feature ("Add a parallax
background, but don't change anything yet" stored nothing, so every later
"okay, do it" found nothing) and sent `delete tracker` to the model, which
answered "Sure." — the one answer a destructive request must never get. The
gate now sits only on immediate execution, with the conversation as a second
way to qualify, and a removal resolves a target without it because what it
reaches is a question, never an edit.

The rest were tests encoding a contract that genuinely changed, each updated
with the reason in a comment. One of them is worth noting on its own:
`test_episodic_integration_v41` held a *second* `{"allowed": True}` fake
broker — the same wrong shape that let H13 hide for as long as it did.

---

## Mutation-tested again, against the final code

The first mutation scores were measured before the sweep corrections above, so
they described code that is not what got committed. Re-run against the final
tree: **48/48 caught** — H14 9/9, H13 4/4, H15/H16 8/8, H17–H20 12/12,
Medium 15/15.

Two mutants skipped on a missing anchor the first time through, which the
harness reports as *survived* rather than passed. That is the right default and
it earned its keep here, because the two turned out to be different things:

- **The 60-character `CANNOT_CHECK` window** was a live requirement whose
  anchor had moved — the H18 correction rewrote that line. Re-pointed at the
  current line, and caught.
- **The silent-`exit 0` mutant was deleted, not repaired.** It installs
  `exit_zero -> PASSED`, which is now what the code is *meant* to do. Repairing
  it so it "passes" would have dressed up a tautology.

Replacing it with a mutant aimed at H18's actual false pass — make
`_declared_verdict` return `None` for `CANNOT_CHECK`, so a script that prints
it and exits 0 reaches the exit-code branch — showed the final code has **two**
separate `CANNOT_CHECK` readings, which the stale mutants had conflated: one on
the model's reply at generation time (a refusal to write a check), one on the
check's output at run time. The exit-0 branch still believes exit codes, so
reading the output verdict first is the only thing standing between that script
and a recorded PASS.

**One mutant genuinely survived, and that found a hole in the tests.** Deleting
the fence exemption — `and not _fenced_blocks(body)` — broke no check in the
suite. Reading the word anywhere in a reply is only safe *because* a code fence
overrides it: a model that writes a real check and gives it a can't-decide
branch of its own mentions `CANNOT_CHECK` inside working Python. Without the
exemption every such check is discarded unrun, and nothing in the suite would
have said so. `test_cannot_check_anywhere_is_not_a_pass` now asserts both
directions, and the mutant is caught.

---

## Two regressions the M10 fix caused, and the defect they hid

M10 was four lines: resolve `eos_token`/`bos_token` from model metadata, build a
`Jinja2ChatFormatter`, strip the fast-path marker, pass the template's own
`enable_thinking`. Both regressions came out of those four lines, both were
invisible to all 195 suites, and both were found by running the real model.

**1. Nova went mute on every chat call.** `llama.detokenize([eos_id])` returns
**empty bytes** for a control token unless `special=True`, and it does not
raise. The guard caught exceptions, not empty results, so `eos_token` became
`""` — and `Jinja2ChatFormatter` puts its `eos_token` into the stop list, where
an empty string matches at position 0. Every call stopped after one token with
no text.

The M10 test was green throughout, because it builds its own formatter with
`eos_token="<|im_end|>"` **hardcoded** and so never touches the line that
resolves it. The fix tries `special=True` first and treats a blank result as a
failure to resolve, falling back to `<|im_end|>`.
`test_the_eos_token_survives_detokenisation` asserts the real model's
`detokenize([eos_id]) == b""` is genuinely the triggering condition, and that
the resulting stop list contains no empty string. Reverting the guard fails 3
checks.

**2. The chain of thought reached the reader.** With thinking ON the template
writes the OPENING tag into the generation prompt —

    <|im_start|>assistant\n<think>\n

— so the model's output begins inside the block and never emits `<think>`.
`_ThinkStreamFilter` entered the block only on *seeing* an opening tag, and
`_strip_think`'s regexes both need one, so neither removed anything. Measured,
asking for a story with thinking on, the stream began:

    'Thinking Process:\n\n1.  **Analyze the Request:**\n'

This was not cosmetic. `Agent.decide` runs with `thinking=True` and takes
`extract_first_json_object(raw)` — the **first** object in the reply. While the
reasoning leaked, that first object was whatever the model drafted inside its
own scratchpad and then rejected, not its conclusion, so the agent could select
a tool its own reasoning had ruled out. `ProjectBuilder._llm_json` parses the
same way. (The permission broker still gated every call, so nothing ran
unapproved — the defect is tool *selection*, not authorisation.)

It also reached further than `thinking=True` callers: `chat_stream_ex`
escalates, and its third attempt asks for real reasoning even when the caller
asked for none, so an ordinary turn that failed the cheap path twice leaked too.
The `NOVA_LLM_FAST_PREFILL=0` fallback leaked on every turn, because `/no_think`
is a soft instruction that does nothing to the template.

The fix derives the consumer's starting state from the **same** condition the
handler uses to set `enable_thinking=False` — the fast-path marker being the
last message — so the producer and the consumer cannot drift apart, and reads it
per *attempt* rather than once per call. `tests/test_think_block_leak.py`
covers it end to end through `chat`, `chat_stream` and the escalated attempt;
**10/10 mutants caught**, including one that computes the state per call
instead of per attempt, one that stops marking the model at all, and one that
lets the no-template fallback go silent again.

**Live verdict on both.** 0 leaked markers across 12 story generations (two
budgets), 3 reasoning questions on the non-streaming path, and the fast-path
regression check — and the fast path is unchanged at 0.11 s to first word,
248-484 words, `finish_reason=stop` 3/3.

**And the defect the leak was hiding.** Once the reasoning stopped being
mistaken for an answer, the retry loop started running — and it was repeating
itself. With thinking on at the audit's 1200-token budget, all three story
attempts spent the whole budget inside `<think>`:

| attempt | time | finish | visible words |
|---|---|---|---|
| 1 | 22 s | `length` | 0 |
| 2 | 22 s | `length` | 0 |
| 3 | 22 s | `length` | 0 |

**65.9 s of silence, then the caller's apology, on 3 of 3 stories.** One of the
three reasoning questions on the non-streaming path did the same thing in
55.7 s. The same prompts at 2400 tokens produced 137, 336 and 431 words, so the
*budget* is what fails and repeating it identically buys only the wait.

**That story measurement forced a configuration production does not use**, and
saying otherwise would overstate it: both streaming callers in `core/runtime.py`
pass `thinking=False`, the storyteller explicitly so, with a comment naming this
exact failure from audit M9. Forcing it on was how to exercise the escalation,
not a reproduction of something users saw. Every *production* `thinking=True`
caller goes through `chat()` — the path that had no escalation at all — and that
is where the damage was real.

`chat_stream_ex` already escalated — but only for a `thinking=False` caller;
when the caller asked for reasoning, both retries fell through to the same
branch and repeated it. `chat()` had no escalation at all. Both now use the
policy `ProjectBuilder._llm_json` already used for this exact failure (audit,
H15): **the caller's budget, then double it for a thought that was merely long,
then stop paying for reasoning** — a model that cannot stop deliberating cannot
be out-budgeted, and the fast contract measures 0/18 empty. The `thinking=False`
chain keeps its two cheap tries first, because a fast attempt costs ~36 ms and
*its* failure really is stochastic.

Giving `chat()` the chain it was missing also fixed an asymmetry nobody had
noticed: a twice-failed cheap turn was rescued by the deep attempt on the
streaming path and came back empty on the non-streaming one. **9/9 mutants
caught**, including one that escalates the cheap chain too (which would put the
measured 19.7 s P90 back on "Good morning") and one that denies the deep attempt
to a twice-failed turn.

**Post-fix, on real weights, the same prompts that were silent.** Stories with
thinking forced on at 1200 tokens: **0 of 3 empty**, against 3 of 3 — 435 words
(`stop`), 51 words (`length`), 563 words (`stop`). The retry log says which
attempt earned each one: two were rescued by the doubled budget, and the third
needed the full chain, with the reasoning-off attempt producing the longest and
cleanest story of the set. On the non-streaming path, **0 of 3 empty** against
1 of 3, and 3 of 3 answers correct, one of them rescued by the doubled budget.

**One of those answers was scored wrong by my own checker, and was not wrong.**
The check was `"12" in text`; the model had written *"you are left with
twelve."* Re-asked three times with the full reply printed, it was right 3/3
("You have 12 apples left because you give away five, which is the result of
rounding down one-third of seventeen."). A correct answer marked as a failure by
a string match is the same defect this audit documents in the test suite — and
it came within one commit of being recorded here as a model arithmetic error.
The lesson stands on its own: a grep for prose is not a measurement of
correctness, including when I am the one writing the grep.

### What it was costing: deep mode reviewed nothing

The tightest `thinking=True` budget in the codebase is deep mode's, and both of
its bookends degrade to a no-op without raising anything:

```
DeepPipeline.plan()      max_tokens=400, thinking=True  -> ""         on empty
DeepPipeline.critique()  max_tokens=400, thinking=True  -> "approve"  on empty
```

An empty plan is indistinguishable from the planner's own `DIRECT` ("no plan
needed"), so deep mode just proceeds. An empty critique parses to `{}`, whose
verdict is not `"revise"`, and the code treats anything unparseable as approval
— deliberately, so a flaky critic can never block a reply.

Driving the **real** `DeepPipeline` on real weights, same prompts, pre-fix
policy in `chat()` against post-fix:

| | pre-fix (3 identical attempts) | post-fix (escalating) |
|---|---|---|
| `plan()`, 3 requests | **empty 3/3**, ~21.8 s each | 3/3 real plans, ~22.4 s |
| `critique()`, honest draft | `approve` *by default, unreviewed* | `approve`, reviewed |
| `critique()`, honest draft about a failed tool | `approve` *by default, unreviewed* | `approve`, reviewed |
| **`critique()`, draft claims a FAILED tool succeeded** | **`approve`** | **`revise`**, notes name the contradiction |

Every one of the six calls came back empty on attempt 1. So deep mode — which
is **opt-in**, triggered by the user saying "be thorough", "think carefully",
"take your time" — ran with no plan and a review stage that approved
everything, including a draft presenting invented weather as fact after the
tool had failed with `connection refused`. Asking for the careful treatment
bought *less* verification than an ordinary turn, because an ordinary turn at
least gets a reply.

It cost nothing to fix. The escalation makes the critic **faster** (15.6–20.7 s
against 21.3–21.5 s), because attempt 2 succeeds at double the budget instead
of a third full attempt being wasted.

**The cause is fixed in `core/llm_runtime.py`. What let it hide is fixed in
`core/orchestrator/deep_mode.py`**, which imported a `logger` and never called
it once, leaving both fallbacks with no trace in the record. An empty plan and
a deliberate `DIRECT` are now distinguishable, and an approval the critic never
gave is recorded as a warning. Neither change alters a control-flow decision:
the draft still ships, it just no longer ships unrecorded.
`tests/test_deep_mode_silent_fallbacks.py`, **11/11 mutants caught**, including
one that logs the empty case as a `DIRECT`, one that records it at `debug`
where nobody looks, one that flags every approval so the flag means nothing,
and one that lets the critic block a reply.

**What this says about the test suite.** Every suite substitutes `ScriptedLLM`;
not one asks the real model for a sentence. A fix that changes how the prompt is
*built* changes what the output *looks like*, and the output-side code has no way
to know. Both regressions sat on the far side of the layer that changed.

The third finding was invisible for a different reason, and it is the more
useful one: **a bug that makes a failure path unreachable also makes it
untested.** While reasoning leaked, nothing came back empty, so the retry loop
never ran and no test ever had reason to cover it. And the damage downstream was
invisible for a third reason again — deep mode's fallbacks are *correct*
behaviour for a flaky model, so there was no exception, no error, and no log
line. The only way to see any of it was to run the real model and check not
that it answered, but **what it answered**.

---

## Still owed

One claim in this document is still unverified:

**L3's cloud counting.** Verified with a fake usage payload and a simulated
restart, not against the provider, because that costs money and nobody asked
for it.

The other two were measured on real weights, once the eos regression above was
fixed and the model would run at all. Both exercise the fast path, which the
think-leak fix leaves byte-for-byte unchanged, so the numbers stand:

- **M10, end to end.** **0 of 22 empty**, against the audit's 4 of 22. Median 0.48 s, P90
  0.59 s, worst 0.65 s, and all 22 replies grounded in their tool result
  ("It is currently 71°F and clear in Austin.", "I recall that your locker
  code is 4471.").
- **M9's latency.** With thinking off the stories **finish**: first word at
  **0.11 s** median, `finish_reason=stop` 3/3, 290–498 words, 6.5–11.1 s end
  to end. With thinking on they never did — `finish_reason=length` 3/3 at
  ~22 s, the whole budget spent reasoning.

  The audit's specific **47 s to first word is not reproduced here and is
  not claimed**. On this model the first visible token with thinking on
  arrived in ~0.1 s — but that token was scratchpad, which is regression 2
  above, not speed. What is confirmed is M9's substance: thinking off
  completes a story, thinking on did not.

And one thing this round did not touch, because it is not a defect:
**P5.2 step 9 still needs one clean human calibration run**, and the P0
barge-in check is still owed by Marcus. Those were already outstanding.
