# Audit 2026-11-02 — what was fixed

Companion to `docs/AUDIT_2026-11-02.md`. That document says what was wrong;
this one says what changed, how it was proved, and what is still owed.

All 59 findings are addressed. Every fix has a regression test that fails when
the fix is removed — that is what the mutation column means, and it is the
point: a green test that also passes with the fix reverted proves nothing.

**Honest limits, stated once.** Three things could not be measured here and are
marked ⚠ below: the model weights would not fit in the free VRAM at the time
(something else held 7.7 GB of 16.3 GB), and the paid cloud API is not called
without asking. Everything else was run.

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
| H18 | Checks run on a throwaway copy; a check's own crash is inconclusive (an `AssertionError` is still a finding); the verdict is read from a `PASS`/`FAIL`/`CANNOT_CHECK` line; `CANNOT_CHECK` is recognised anywhere | verification suite; 11/11 mutants caught |
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

## Still owed

Three claims in this document rest on code and template inspection rather than
a live run, because the model would not fit in the free VRAM at the time:

1. **M10, end to end.** The prompt is proved correct against the real chat
   template, and the old prefill is proved wrong the same way. The *empty-reply
   rate* — the audit measured 4 of 22, all after tools — has not been
   re-measured. Worth one run of 22 tool-using turns.
2. **M9's latency.** `thinking=False` is the fix the audit asked for; the
   47 s-to-first-word figure has not been re-measured.
3. **L3's cloud counting.** Verified with a fake usage payload and a simulated
   restart. Not verified against the provider, because that costs money and
   nobody asked for it.

And one thing this round did not touch, because it is not a defect:
**P5.2 step 9 still needs one clean human calibration run**, and the P0
barge-in check is still owed by Marcus. Those were already outstanding.
