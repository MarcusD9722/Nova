# Test program: every function, every connection, every situation

Started 2026-11-06 at Marcus's request: *test that each function in `core/`,
`memory/` and `backend/` does what it was written to do, that everything wired
together works as coded, and that Nova performs under strenuous combined use —
casual conversation, projects planned, built and run, switching back to
conversation, telling a question from a statement from something worth
remembering — with no silent failures or errors.*

This file is the record: what each layer proves, how to run it, what it found,
and what is still open.

## The six layers

| # | layer | proves | runs in | status |
|---|---|---|---|---|
| 1 | **Coverage map** | which functions no test ever reaches | offline | **done** — see below |
| 2 | **Function tests** | each function does what it is *meant* to do; every test mutation-checked | offline gate | **in progress** — items 1–4 done, see the end |
| 3 | **Wiring tests** | chains across modules, workers and restarts behave as coded | offline gate (real backend, scripted model) | planned |
| 4 | **Judgement corpora** | question / statement / remember-this / command / project request / small talk / status question, at scale, typed and voice-style | offline gate | planned |
| 5 | **Real-model scenarios** | the same behaviours with the real 9B, judged on what happened and on what was said | opt-in, GPU to itself | planned |
| 6 | **Combined endurance run** | a long mixed "day with Nova", restart included; pass rate, latency and memory over time | opt-in, GPU to itself | planned |

Across all of them: **no silent failures.** Every run records every log event;
an unexpected error, exception or degraded fallback fails the run and is
listed. Every `except` block that neither logs nor re-raises is reviewed and
either made audible or documented as a deliberate guard.

## Ground rules

- **Tests are written from what the code is meant to do** — its docstring, the
  design docs, or what Marcus has said — never from what it currently does. A
  test copied from the code locks the bug in.
- **Every new test is mutation-checked**: break the function on purpose, the
  test must fail, restore the file byte-identically. A mutant that survives
  means the test is rewritten, not the mutant excused. (The 2026-11-02 round
  found a test that stayed green while the code it covered made Nova mute.)
- **Where the intended behaviour is unclear, ask Marcus.** Do not guess.
- **Judge a suite by its exit code**, never by grepping its output.
- Every run uses a throwaway root: never `memory_data/` or `projects/`, no paid
  API calls, no control of the real desktop.

## Layer 1 — the coverage map

    venv\Scripts\python.exe -m pip install -r requirements-dev.txt   (once)
    venv\Scripts\python.exe tests\coverage_map.py run                (all suites, ~70 min)
    venv\Scripts\python.exe tests\coverage_map.py report             (~30 s)
    venv\Scripts\python.exe tests\coverage_map.py swallowed          (~45 min)

`run` executes exactly the `tests/test_*.py` set `run_tests.ps1` runs, each
under coverage.py with branch coverage and its child processes measured
(`patch = subprocess`; the restart suites do most of their work in children).
`report` maps the data onto **functions**: a function counts as run only when a
statement of its *own* body executed — the `def` line runs at import and does
not count, and a closure's body is the closure's. Output lands in
`coverage_map/` (gitignored): `REPORT.md`, `functions.json`, `handlers.json`,
and `html/index.html`, which shows which suite ran each line.

`report` also sorts every `except` block by whether anyone hears about the
failure: *raises*, *logs* (by level — a debug log is invisible at the default
level, so it is counted apart), *control-flow* (an empty queue, the end of an
iterator, a poll tick), or *quiet*. A call to a function whose whole job is
logging, like `log_worker_error`, counts as logging. Coverage can say a quiet
block ran but not what it caught, so `swallowed` re-runs the suites that
reached one with a recorder loaded into every Python process they start. It
writes down each exception a quiet block caught: its type, its message, where
it was raised, and the test function or background task that was running.
The output is `coverage_map/SWALLOWED.md`.

The tool lives in `tests/`, not `tools/`, because it sets the harness's own
`NOVA_IT_WATCHDOG_S`, and `test_settings_p04` scans `tools/` as Nova code.
Its rules are pinned by `tests/test_coverage_map_tool.py`: 39 deliberately
broken versions of the tool, all caught by the suite (32 at layer 1, seven
more for the `# quiet-ok:` marker added in layer 2). The pinned rules cover
the def line, closures, branches, endpoints, every handler rule (including
that `except TimeoutError: return None` is a swallowed failure, not control
flow, and that dropping an item on `QueueFull` is data loss), the
logging-helper rule, and the recorder. For the recorder that means worker
threads, where an exception was *raised* rather than where it was caught,
the test on the stack, the per-place limit, and reporting a block coverage
saw run but the recorder never did.

**Run is not tested.** Coverage says a line executed, not that anything checked
what it did. Layer 1 finds what nothing reaches; layer 2 asks whether the tests
that do reach a function would notice it breaking.

### Results — first full run, 2026-11-06, at `aa177f2`

**Every suite passes while measured: 197 of 197, in 69 minutes.** On the first
pass one failed, and the map itself was the cause. It lived in `tools/`, which
`test_settings_p04` scans as Nova code, and it names the harness's own
`NOVA_IT_WATCHDOG_S`. It moved to `tests/`, with the other scripts that drive
suites.

| tree | functions | never run | partly run | fully run | statements run | branches taken |
|---|---:|---:|---:|---:|---:|---:|
| `core/` | 1147 | 129 (11.2%) | 439 | 579 | 82.1% | 73.9% |
| `memory/` | 516 | 26 (5.0%) | 162 | 328 | 87.3% | 77.7% |
| `backend/` | 158 | 72 (45.6%) | 67 | 19 | 57.6% | 38.1% |
| `plugins/` | 34 | 7 (20.6%) | 10 | 17 | 67.9% | 41.1% |
| `services/` | 36 | 19 (52.8%) | 11 | 6 | 55.5% | 46.3% |
| **core + memory + backend** | 1821 | 227 (12.5%) | 668 | 926 | 81.1% | 71.6% |

**The HTTP layer is the least-proven part of Nova.** The suites call the
runtime directly, so 30 of the 85 endpoints are never called by any suite, 48
are only partly run, and 7 are fully run. The 30 are:

- status: `/health`, `/status`, `/ws/events`;
- voice: `/speak`, `/stt`, `/voice/interrupt`, `/tts/{id}`;
- uploads: `/file-upload`, `/uploads/{name}`;
- vision: both `/vision/*` routes;
- autonomy: six `/autonomy/*` routes and `/goals`;
- dev tools: seven `/dev/*` and `/code/*` routes;
- speaker: `/profiles`, `DELETE /calibration`;
- `/api/web/search`.

`backend/app.py` runs 41% of its statements, and `backend/serve.py` never runs
at all.

**Whole behaviours that nothing runs:**

- **forgetting:** `MemoryUnifier.forget` (41 statements) and the
  `memory_forget` tool;
- **reminders:** the list and cancel tools, `reschedule_reminder`, and the
  briefing (`_compose_briefing`, `_weather_line`, `_next_future_occurrence`);
- **weather replies:** `_format_weather_reply`;
- **voice:** synthesis (`_tts_bytes`), transcription (`_stt_transcribe`), and
  the speech queue inside `/chat/stream` (`tts_worker`, `queue_for_speech`);
- **uploads:** `/file-upload`, `_build_attachment_context`, and
  `_safe_upload_path`, which is a path-safety guard;
- **maps:** `Navigation.run_route`, `run_nearby`, `format_directions_reply`;
- **self-improvement:** `_improve_cycle`, `_reflect`, `_prune_episodes`;
- **cloud streaming:** `CloudRuntime.chat_stream`.

**Twenty functions look dead.** No suite runs them, and their names appear
nowhere else in `core`, `memory`, `backend`, `plugins`, `services` or
`frontend/src`. The search matches whole words, so a name shared with any other
function counts as used, and the list errs short.

- `core/llm_runtime.py`: `LLMRuntime._emit_perf_lines`
- `core/mcp/registry.py`: `Capability.to_status`, `CapabilityRegistry.for_server`
- `core/mcp/session.py`: `StdioTransport.stderr_tail`
- `core/project_builder.py`: `write_repro_check`, `_mentions_token`
- `core/project_manager.py`: `append_chat_log`, `save_artifact`,
  `get_project_context`
- `core/runtime.py`: `model_routing`, `_try_tool_assist`, `_natural_join`
- `core/speaker/matcher.py`: `SpeakerMatch.is_known`
- `core/speaker/service.py`: `SpeakerService.effective_margin`
- `core/turn_identity.py`: `personal_scope_note`
- `core/voice/speech_text.py`: `split_display_and_spoken`
- `core/voice/turn.py`: `TurnRegistry.mark_playing`
- `core/workers/episodic_ingest.py`: `_speaker_fields`
- `memory/backends/sqlite_backend.py`: `find_fact_ids`
- `memory/episodes.py`: `EpisodicStore.touch_episode`

Marcus decided on 2026-11-06 to keep them, so each one gets a test of what it
is meant to do (layer 2, item 7). `_SemanticEmbeddingFunction.get_config` matched too, but it is part of
Chroma's embedding-function interface, which newer Chroma calls, so it is not
dead.

**Error handlers in core, memory and backend: 601.**

| kind | count | reached by a suite | never reached |
|---|---:|---:|---:|
| quiet | 376 | 55 | 321 |
| logs-debug | 7 | 2 | 5 |
| logs-info | 3 | 2 | 1 |
| logs-warning | 133 | 27 | 106 |
| logs-error | 14 | 4 | 10 |
| raises | 63 | 27 | 36 |
| control-flow | 5 | 4 | 1 |

The first count said 389 quiet and 1 logs-error. Thirteen worker handlers call
`log_worker_error`, which logs at error level, so the map now recognises
logging helpers.

**What the 58 quiet blocks that ran actually caught.** These are the 57 above
plus one in `services/`. `swallowed` ran the 114 suites that reach them, in
48 minutes, across 362 Python processes. The recorder saw every block that
coverage saw run. Each block falls into one of three kinds:

- **Reported: 23.** The failure becomes something the caller shows or acts on.
  That can be an error string, `ok: False`, a reply, a status field, an event,
  or a fallback that warns (`_fallback_chat` logs `cloud_fallback_to_local`).
  Every one was reached by a suite proving the guard.
- **Expected: 22.** The exception is the normal way to ask the question:
  - `relative_to` raising means "not under this root";
  - `get_running_loop` raising means "no loop here";
  - `json.loads` is tried before the slower extraction;
  - `decode` is tried with one encoding after another;
  - a missing journal means "no promotion in flight";
  - a poll or sleep timeout;
  - `CancelledError` after our own `cancel()`.
- **A real failure can vanish: 13.** These are the layer 2 starting list, each
  with what the recorder actually saw. In most of them the tests only ever hit
  the harmless case.

| # | where | what the suites hit | what goes unheard |
|---|---|---|---|
| 1 | `cloud_runtime.py:192` `_load_spend` | only a missing file (×398) | a truncated or unreadable spend file reads as "nothing spent today", which silently restarts the daily cloud cap. **Worse, found while checking this:** a spend file holding valid JSON that is not an object (`null`, `[1,2]`) raises `AttributeError` out of `CloudRuntime()`. `RuntimeManager.__init__` builds that unconditionally, cloud enabled or not, and the backend's startup builds `RuntimeManager` with no `try` around it, so one bad file in `memory_data/` should stop Nova booting. The `CloudRuntime()` crash is reproduced on a temp file. The boot failure was read from the code here; layer 2 confirmed it with a real startup |
| 2 | `completion_artifacts.py:55` `declared_scaffold` | only a missing manifest (×757) | a corrupt manifest reads as "no scaffolding". Nova's own validation files then count as project files, and the next `declare_scaffold` overwrites the manifest, losing what it listed (the write is not atomic) |
| 3 | `conversation_state.py:47` `from_obj` | the guard test's `"not json"` | a corrupt saved conversation state is replaced by an empty one |
| 4 | `project_builder.py:632` `_handle` | the test's injected router failure | a router that raises silently builds on the local model. That would be a bug: a misconfigured role is already warned about when the router is built |
| 5 | `project_builder.py:971` `_read_project_md` | only a missing file (×122) | any other read failure reads as "no PROJECT.md" |
| 6 | `memory_ingest.py:218` `_ingest_scoped` | the test's non-numeric confidence | a policy fact whose database write fails is dropped without a word. **Corrected in layer 2:** this is not the "remember this" path, because nothing sends policy facts today |
| 7 | `chroma_backend.py:538/542` `_read_journal` | the test's corrupt journals | the outcome is safe (not adopted) but nothing says why |
| 8 | `memory_ingest.py:339` `_drain_summarize_hints` | only `QueueEmpty` (×675) | `except Exception` would hide anything else |
| 9 | `chroma_backend.py:769/801` staging cleanup | only "does not exist" (×41) | `except Exception` would hide a real failure to delete |
| 10 | `project_builder.py:1852` `_generate_and_run_tests` | a reviewed test that did not compile | right to keep the original test, but nothing records that the review produced unusable code |
| 11 | `unifier.py:642` `_ensure_semantic_index` | a semantic **recovery** failure from `count()` | read as "the index is empty", which triggers a rebuild that then refuses and logs `semantic_rebuild_aborted`. So it is heard, late and under the wrong name |

Rows 7 and 9 are two handlers each, so the 11 rows hold 13 handlers. Row 1
came with a question: when the counter cannot be read, should the daily cap
fail closed and treat today as spent? Marcus's answer, 2026-11-06: no. Nova is
local-first and he rarely uses the cloud, so an unreadable counter warns and
counts from zero. It must still never stop Nova booting.

The 321 quiet handlers no suite reaches can't be judged this way at all,
because nothing has ever made them run. The most are in `memory/unifier.py`
(55), `core/runtime.py` (34), `backend/app.py` (18), `core/project_builder.py`
(16), `core/workers/self_improve.py` (16) and `core/workers/reminder_worker.py`
(14).

### Layer 2 order, from this map

1. **The 13 handlers above.** Make each one heard, or narrow it to the
   exception it means, with a mutation-checked test that the failure is now
   reported and the behaviour around it is unchanged. **Done 2026-11-06**,
   see *Layer 2* below.
2. **Memory: "remember this", recall and forget.** **Done 2026-11-06**, see
   *Layer 2* below.
   - `forget`, `recent_turns_text`, `facts_needing_reverification`;
   - reminders: reschedule, list, cancel;
   - `add_fact`'s supersession branches;
   - the 55 unreached quiet handlers in `unifier.py`;
   - the extractor loop in `MemoryIngestWorker._ingest_scoped`: one failed
     `add_fact` ends the rest of that turn's facts. It is logged as
     `memory_ingest_failed`, but nothing says which facts were lost;
   - `_reconcile`, where a failed `find_conflict_candidates` returns quietly.
3. **Understanding: question, statement, remember-this, command.** **Done
   2026-11-07**, see *Layer 2* below.
   - the two known misses: `asks_about_work` misses "Did the build finish?",
     and "For blog, add a sidebar, but not yet" drops the proposal;
   - the untaken branches in `project_intent`, `understanding`, `recall_gate`
     and `episodic_recall`.
   
   Layer 4 tests the same judgements at corpus scale.
4. **Projects: plan, build, run, report.** The untaken branches of `_build`,
   `_generate_and_run_tests`, `_verify_and_fix_runtime`,
   `_fix_runtime_error_patch` and `_smoke_test_html`. **Done 2026-11-07**,
   see *Layer 2* below.
5. **The HTTP layer.**
   - every endpoint driven through FastAPI's `TestClient` on a throwaway root,
     checking status codes and validation;
   - `_safe_upload_path` against traversal, absolute paths, drive letters and
     UNC paths;
   - `/health`, `/status`, `/ws/events`.
6. **The remaining behaviours.** Voice, the reminder briefing, weather, maps
   and the self-improvement cycle, each with fakes for the model and devices.
7. **The twenty unused functions.** Marcus is keeping them, so each gets a
   test of what its name, docstring and callers' needs say it does.
8. **The rest of the known small fixes.**
   - four modules create a logger they never call;
   - deprecated `@app.on_event` and `asyncio.get_event_loop()`;
   - the stale `docs/NEXT_SESSION.md`.

The map is re-run after layer 2, so the movement shows.

## Layer 2 — function tests

### Item 1: the 13 quiet handlers — done 2026-11-06

    venv\Scripts\python.exe tests\test_quiet_handlers_2026_11.py     (89 checks, ~6 s)

For every handler the suite checks three things:

- the harmless case still says nothing, because a missing file is not news;
- the bad case is now reported, under its own event name, with the reason;
- the behaviour around it is unchanged, or changed on purpose where the old
  behaviour was itself the bug.

I then broke the fixes deliberately, in 42 different ways. The suite caught
every one, and the files were restored byte-identically after each. Two of
the 42 used to crash the suite instead of failing a check. Both now fail a
check.

| # | where | what it does now |
|---|---|---|
| 1 | `CloudRuntime._load_spend` | missing file: quiet. Unreadable: `cloud_spend_unreadable`, and counting restarts from zero (Marcus: no fail-closed). JSON that is not an object no longer crashes. Counts that only half-read are both zero, not one of them |
| 2 | `declared_scaffold`, `declare_scaffold` | unreadable: `scaffold_manifest_unreadable`. Declaring over an unreadable manifest keeps the old bytes as `scaffold.json.unreadable` and logs `scaffold_manifest_replaced`. The write is atomic, so a crash part-way through can't corrupt it |
| 3 | `ConversationState.from_obj` | damage: `conversation_state_unreadable`, naming the damaged field; what is intact is kept |
| 4 | `ProjectBuilder._handle` | `builder_model_route_failed` with the role and the error; still builds on the local model |
| 5 | `ProjectBuilder._read_project_md` | missing: quiet. Unreadable: `project_md_unreadable` |
| 6 | policy facts in `MemoryIngestWorker._ingest_scoped` | malformed: `policy_fact_malformed` (warning). Failed write: `policy_fact_write_failed` (error). The facts after it still land |
| 7 | `ChromaMemoryBackend._read_journal` | `semantic_journal_unreadable`, once per distinct reason, because recovery retries on every open |
| 8 | `MemoryIngestWorker._drain_summarize_hints` | only an empty queue is quiet. Anything else logs `summarize_hint_read_failed` (error), and the worker keeps running |
| 9 | staging cleanup | `begin_staged_rebuild` deletes a leftover only if one exists. A real failure to delete it aborts the rebuild with that reason. `abort_staged_rebuild` logs `semantic_staging_cleanup_failed` instead of raising over the failure that called it |
| 10 | `ProjectBuilder._generate_and_run_tests` | a review that does not compile: `builder_test_review_discarded`; the original test is kept |
| 11 | `MemoryUnifier._ensure_semantic_index` | a recovery refusal: `semantic_index_unavailable`, and no rebuild starts, because it would refuse for the same reason. Any other count failure: `semantic_index_count_failed`, and the index is still rebuilt |

**What fixing them turned up:**

- **The spend-file boot failure is real.** With the object check removed, the
  real startup (`backend.app._startup`, run through `tests/harness.boot`)
  fails with `AttributeError: 'NoneType' object has no attribute 'get'`.
  Layer 1 had only read that from the code.
- **One damaged journal blocked semantic-index rebuilds for good.** When no
  backup was present, recovery cleared the promotion journal only if it could
  read it. `_unfinished_promotion` refuses while the file exists. Reproduced:
  every `begin_staged_rebuild` was refused, on every boot, with "a previous
  promotion journal has not been cleared". An unreadable journal with no
  backup is now cleared, as the leftover it is. The neighbouring handler that
  hid a failure to *remove* the journal (`_write_journal`, never reached by a
  suite) now logs `semantic_journal_not_removed`.
- **A leftover staging collection that could not be deleted was built on.**
  `get_or_create_collection` handed back the old collection, still holding the
  old generation id.
- **Two more crashes.**
  - A scaffold manifest that is not valid UTF-8 raised `UnicodeDecodeError`
    out of `implementation_files()`, because only `JSONDecodeError` was caught.
  - A saved conversation state with a number where a list belongs raised
    `TypeError` out of `load()`. One with text there was replayed as single
    characters.

**Corrections to layer 1:**

- **Row 6 overstated the risk.** Nothing in Nova sends policy facts: the
  runtime's only `MemoryIngestEvent` passes `policy_memory_facts=[]`. The
  real "remember this" path is the extractor, and a failed write there
  already reaches `memory_ingest_failed` at error level. It does take the
  rest of that turn's facts with it, which is on the item 2 list. The
  handler is fixed anyway, because the field is part of the event's
  contract.
- **Row 3 is narrower than it read.** The store saves objects, so "text that
  is not JSON" needs a value no current code writes. The reachable damage is
  the `TypeError` above.

### Item 2: memory — remember, recall, forget, reminders — done 2026-11-06

Two suites:

    venv\Scripts\python.exe tests\test_memory_functions_2026_11.py   (69 checks)
    venv\Scripts\python.exe tests\test_memory_handlers_2026_11.py    (64 checks)

I broke the fixes deliberately: 23 broken versions for the first suite and 58
for the second. Every one was caught, and the files were restored
byte-identically after each. The mutation driver now runs the unbroken suite
first and refuses to judge anything if that fails. A suite that already fails
"catches" every mutant, and one round of this work nearly counted 23 of those.

**What each memory function was meant to do, and the six places it didn't:**

| function | meant to | what was wrong |
|---|---|---|
| `forget` | delete exactly what the speaker named, from their own memory only | **Its own docstring's request matched nothing.** "Please forget my locker code, I don't want you storing that" required "please", "forget", "want" and "storing" to appear in the stored fact. Request words no longer count. **And the owner's forget also deleted a guest's matching fact**, against its first promise that nobody forgets anybody else's memory. It no longer touches `speaker:` namespaces, in either shape. *Marcus confirmed this on 2026-11-07: his forget stays out of guests' memories* |
| `add_fact` replacing a single-valued fact | the new value replaces the old one | **The old value was deleted, in its own committed transaction, before the new one was written.** A write that failed left neither. The old value is now retired only after the new one is stored |
| `recent_turns_text`, for the reflection pass | lessons about Marcus, from Marcus | **It read every speaker's turns and labelled each one "Marcus".** Reflection, running as the owner, wrote lessons about how to treat him from what guests said: the leak `scoped_entity` exists to prevent. It now reads only the current speaker's exchanges |
| `episodes_for_consolidation` | insights about Marcus's routine, from Marcus | **Same leak:** a guest's messages fed insights stored as his. Fixed the same way |
| `digital_twin_profile`, active hours | Marcus's working pattern | **Same leak, smaller:** a guest's turn times counted toward his peak hours. It now reads only his turns |
| `reminder.list` | list his reminders, truthfully | **The limit was applied before Nova's own bookkeeping reminders were filtered out.** Birthday heads-ups and habit prompts used up the slots, so his were cut off under "These are the only reminders that exist". With 20 or more of hers due first, it answered "Nothing is scheduled". It now filters first and says when a list is cut short |
| `MemoryIngestWorker`, extracting facts | save every fact it extracted | **One failed write ended the loop**, losing every fact after it under one generic error. Each fact now fails alone, by name |

Also fixed, each pinned by a check:

- A reminder's due time is checked when it is written and stored as UTC.
  "Tomorrow" was stored as text and never fired, silently. A time with an
  offset fired early, because due times are compared as text.
- `reschedule_reminder` now says whether it moved anything, and the worker
  logs it when it didn't.
- `_reconcile`'s two silent returns are now logged.
- `forget`, reverification and the reminder set were all tested against
  their docstrings and work as written.

**The 56 quiet handlers in `memory/unifier.py`.** None is left unreviewed. A
structural check in the suite fails if one appears.

- **24 best-effort sections** (the digital twin, the executive layer, the
  catch-up summary, known names and others) still leave a failed section out.
  They now log `memory_section_failed`, naming the section.
- **13 readers of stored JSON** that no longer parses still fall back. They
  now log `memory_blob_unreadable`. Three of them were read-modify-write paths
  that then overwrote the unreadable value without a trace: a person's
  attributes, an experiment's trials, the activity log. Those now copy the old
  bytes into the audit log first.
- **9 guards for an unimportable `core.turn_identity`** fall back to treating
  every speaker as Marcus. They now say so every time. This cannot happen in
  the running app, which imports that module first.
- **10 were reviewed and are right as they are**, such as a timestamp parse
  whose answer is the exception. Each was narrowed to the exception it means
  and marked `# quiet-ok: <reason>`.

**A data-loss bug behind them: five writers cut serialized JSON.**
`json.dumps(x)[:20000]` is not JSON past the limit. Each value was read back
with `json.loads`, the read fell back to "nothing", and the next write saved
that:

- the activity log and experiment trials kept resetting;
- a plan over 20,000 characters read back as no plan;
- a skill edited often enough vanished, because it keeps every earlier
  version.

Values are now kept under the limit by dropping their oldest entries, never by
cutting the text. Measured: the activity log keeps its newest 192 events
instead of resetting to a handful, and a skill edited 200 times survives at
version 201.

**The coverage map counts reviewed handlers now.** A quiet `except` marked
`# quiet-ok: <reason>` on its header is counted as *deliberate*. The marker is
read from real comment tokens, so the text inside a string does not count, and
a marker with no reason does not count. Seven more broken versions of the tool
pin this, 39 in all.

**A test bug the full run found.** `test_memory_consolidation` expected UTC
dates from a function that has grouped by local day since M17, so it failed
every evening from 6 pm to midnight Central. It now expects local dates. The
committed code behaved the same, so this was never a regression.

**One slip of mine, recorded:** both memory patches were applied while the
full run was still going. A setting meant to aim them at a copy did not take
effect. That run's later suites therefore tested the new code, and the final
full run at the end of layer 2 is the one that counts.

**The final full run for items 1–3, 2026-11-06: 201 of 202 suites passed.**
The one failure was `test_cg1_continuity`. It created reminders with
`datetime.now()`, a local time with no timezone, which the new check now
refuses. Read back as UTC, that time was six hours off: the M5 bug. The test
now passes UTC, and run alone it passes. No Nova code changed after the run.

### Item 3: understanding — done 2026-11-07

    venv\Scripts\python.exe tests\test_understanding_branches_2026_11.py   (37 checks)

23 deliberately broken versions; 22 caught. **The one that survived:**

- **What it changed:** it removed the guard in `strip_preamble` that stops
  before stripping a message down to nothing.
- **Why no test catches it:** a fallback at the end of the same function
  already returns the original text in that case, so the message is never
  emptied either way.
- **Where the two versions differ:** only on input made entirely of openers.
  For "so so", the original returns "so" and the broken one returns "so so".
- **Why no check was added:** the docstring does not say which is right, so
  pinning either would lock in a guess. It is recorded here instead.

Every branch the map found untaken in `intent`, `understanding`,
`recall_gate` and `episodic_recall` is tested for what it is meant to do:

- empty input is never a question, a recall or a history search;
- a model answer the code did not offer falls back;
- the prompt budget is a budget;
- an episode with no owner is Marcus's, never a guest's;
- a reference into a past result set resolves, reports that it is
  ambiguous, or says why not.

**One of those branches hid a bug.** When a question had no usable terms,
`retrieve` fell back to the newest episodes, as its comment promises. It then
scored each one by term overlap, which is 0 with no terms, and dropped them
all, so the fallback never returned anything. Recency mode now keeps the
store's newest-first order.

### The two known misses — fixed 2026-11-07

    venv\Scripts\python.exe tests\test_work_questions_2026_11.py    (22 checks)
    venv\Scripts\python.exe tests\test_s15_proposal_lifecycle.py    (55 checks)

The Stage 15 report recorded Marcus's standing instruction not to broaden the
intent grammar without a production defect requiring it, and a test pinned one
of the two misses as a known limitation, so both waited for him. He approved
fixing both on 2026-11-07.

**Miss 1: asking about the work by its own name.** On 2026-11-06,
`asks_about_work` returned False for all eight of these:

- "Did the build finish?"
- "Did the planned work succeed?"
- "did the tests pass?"
- "Did everything work?"
- "did the project build ok?"
- "Has the build finished yet?"
- "Did the job run?"
- "Did my game build work?"

A miss means the reply is written with no work record attached. That is the
condition behind the audit's C4 false claim, where Nova said a failed build
was "all set up and ready to go".

Measured before changing anything, the gap was wider than the eight. 43 of 51
natural ways of asking whether the work went right matched nothing, including
"Did the build pass?", "Are the tests passing?", "Did anything fail?" and "Any
errors?". Every shape took a pronoun or "everything" as its subject, and none
took the work's own name.

The fix adds shapes keyed to what the work is called: *did / has / is / does*,
then the work's name, then how it went.

- **Nouns that also mean something outside software take only software
  verbs.** "Did the tests pass?" counts; "Is the pregnancy test done?" and "Did
  the Chiefs game finish?" do not.
- **Nouns that are often somebody else's take only a closed set of
  modifiers.** "The planned work" counts; "the road work", "the school
  project", "the promo code" and "her new job" do not.
- **Claims count as well as questions:** "So the build passed?", "My game
  build worked, right?".

**A false match costs more than tokens, and that shaped all of it.** A matched
turn carries the record. If the reply then claims completion, the C4 fix puts
"To be accurate: <project> is not done" in front of it. So "Did the laundry
finish?" answered "It's all done!" would have been corrected against a failing
project. 40 near misses pin the boundary, and each shares vocabulary with a
shape that does match.

**Two false matches were already live, and are fixed.** Both came from the
existing claim detector, and both are things Marcus is likely to say:

- "They finished their homework." was read as "they … finished", a claim
  about the work.
- "Everything worked out with the dentist." was read as "everything …
  worked".

With a failing project, an agreeing reply to either one was corrected with "To
be accurate: … is not done". Now an outcome is not a claim about the work when
it is followed by:

- somebody's thing: "their homework", "my car";
- a person or a place outside the work: "with the dentist", "at the bank",
  "at work".

"With the new build", "with Chrome" and "in the end" still count.

**The C4 correction had no test at all.** `docs/AUDIT_2026-11-02_FIXES.md`
listed `test_audit_routing_2026_11.py` as its proof. That suite tests the
pre-model routing patterns and never reaches `_false_completion_notice`. The
table now says so.

The new suite drives the correction end to end through `/chat`:

- a project whose authoritative state is FAILING;
- a scripted reply that says "It's all done and working!";
- the reply that comes back, which starts "To be accurate: calculator is not
  done — it has failing criteria."

**Deliberate boundaries:**

- **Not matched:** "Is it working?" and "Is this working?". People say these
  to check a microphone, and a voice assistant hears them constantly. "Does it
  work now?" still matches.
- **Matched, and accepted:** "Did the Windows update finish?" and "Is the Uber
  app working?". Updates and apps are Nova's work often enough to keep. As
  questions they carry little risk, because Nova cannot claim to know the
  answer. As statements ("the Windows update finally finished") they are left
  out.
- **Matched, and accepted:** "The car fix worked." and "They finished the
  race." A modifier or a plain object from outside the work cannot be told
  apart without knowing every project.

**Miss 2: "For blog, add a sidebar, but not yet."** The proposal grammar is
anchored at the start of the sentence, so a fronted "For blog," hid the
instruction and the proposal was dropped. The same sentence without "but not
yet" was already kept, so the deferral alone made it vanish.

The fronted phrase is now stripped, but only when it names the project that the
turn resolved from that same phrase:

- said while flappy-bird is current, the plan is filed against blog, and "Go
  ahead" on flappy-bird runs only flappy-bird's own plan;
- the fronted project outranks a longer project name later in the sentence:
  "For blog, add a footer like flappy-bird's" is blog's;
- "For the blog, …" with no blog project files nothing anywhere, and neither
  does "On second thought, …", exactly as before;
- every veto still applies behind the phrase: "For blog, I changed the menu
  yesterday, but don't change anything yet." proposes nothing.

While there I removed a comment paragraph that `carries_a_proposal` carried
twice.

**Mutation checks: 57 of 57 caught.**

- **The detector, 47 ways.** Each new shape was removed, each guard widened,
  and the two consumers switched off: the correction, and the completion
  record's use of the detector.
- **The fronted target, 10 ways.**

Every file was restored byte-identically.

**One slip of mine, recorded.** I first pinned "Did they all finish?" as a near
miss and called it a decision, without measuring it. The committed code
already matched it, on purpose: Stage 13C reads "they … finished" as a claim
about the work and lists "They all finished?" among its claims. The suite
caught the contradiction on its first run. The pin now covers what this change
actually decided: the new verbs were not given to "did they all …?", so "Did
they all run the race?" stays ordinary.

**Still open, measured, not fixed: "For the blog, add a sidebar." with no
blog project.** Without "not yet" this sentence never reaches the grammar
changed here. The project pre-pass reads it as a plain description of a
change, and holds it as the CURRENT project's plan. Measured with flappy-bird
current, it was held as flappy-bird's plan, so a later "Go ahead" would run it
there.

Nothing runs before that approval, and the approval's reply names flappy-bird,
so the mistake would be visible. It is still the wrong project. It predates
this work: the committed code does the same. "On second thought, add a
sidebar." is held as flappy-bird's too, and that one is right.

Telling the two apart, when the fronted phrase names no known project, is a
new rule about sentence shapes. So it is recorded here, not added to this
change.

**Full run after the fix, 2026-11-07: 203 of 203 suites passed.**

### Item 4: projects — plan, build, run, report — done 2026-11-07

    venv\Scripts\python.exe tests\test_project_pipeline_2026_11.py   (175 checks, about 40 s)

Real programs run in real subprocesses in throwaway directories; only the
model's text is scripted. The scripted model raises on any prompt no case
expected, so a test cannot pass through a model call it did not mean to make.
`PP_ONLY=4,5` runs only the named sections.

**What a fresh map showed.** The project suites were measured again: 31
suites, 12.7 minutes, all passing under coverage. The builder had moved since
layer 1. Whole paths had never run in any suite:

- `_fix_runtime_error_patch` never applied a patch. Only its refusals ran.
- The fix loop in `_generate_and_run_tests` never ran. So its promise had no
  test: the file ends "either improved so the tests pass, or byte-for-byte as
  it started".
- The repair round in `_smoke_test_html` never ran.
- No suite ever reached "pytest ran a test and it passed" in
  `_run_test_file`. The only pass any suite reached was module-level asserts.
- In `_run_check`: the timeout, a program waiting on `input()`, and a GUI
  traceback swallowed by a program that keeps running.
- In `_build`: loose plan shapes, unsafe paths, the syntax repair, "no files
  were written", and the failure report itself failing.
- In `_improve`: unsafe paths, and "no files were successfully improved".

**What they were meant to do, and the fourteen places they didn't.** Each
was measured on a throwaway root against the committed code first. The suite
runs against the committed builder too, apart from the section that tests
`failure_line`, which does not exist there. 47 of the 167 checks it can run
fail there. Two of those 47 fail only because a message was reworded.

1. **A planned file could land in another project.** The path check
   compared strings: `str(target).startswith(str(path))`.
   - Building `calc` with a planned absolute path into `calculator/` passed
     the check, and overwrote `calculator/main.py` (on a throwaway root).
   - The same check guarded improvements, and the HTML smoke test's script
     loader, which ran a sibling project's script as part of the page.
   - A plan entry of `.` passed too, and crashed the build trying to write
     the folder.

   Now the check compares path components (`is_relative_to`), and the folder
   itself is not a file.
2. **Long files lost their end, silently.** Every whole-file prompt shows
   the file cut to a window (6,000, 7,000 or 9,000 characters) and asks for
   "the COMPLETE corrected file". The model cannot return what it was not
   shown. Measured on an 11,152-character `main.py`:
   - an improvement kept the 51 of 80 functions it had been shown, and
     logged "Changed: main.py. Run check passed.";
   - when the edit did not compile, the "revert" wrote back only the 7,000
     characters the prompt had held. The file was cut mid-string, so a
     working project no longer compiled, and nothing said so.

   Five places do this: the syntax repair, the crash fix, the logic fix, the
   HTML repair and improvements. A file that does not fit is now left as it
   was, with the reason in the log. The minimal patch edits the full file in
   place, so it still works at any length. **This needs a decision from
   Marcus;** see the end of this item.
3. **An empty repair passed as a working program.** A generated file with a
   syntax error got a repair that came back empty. An empty file compiles,
   so it was written, and the run check passed it: "Run check passed
   (main.py)." Now the build fails and says the repair came back empty. The
   file keeps what was generated.
4. **The crash fix was shown the wrong code.** The patch prompt took the
   last "line N" anywhere in the error.
   - For a crash raised inside a library call (`random.choice([])`), that is
     the library's line, past the end of `main.py`. The model was shown an
     empty code window.
   - For a JSON error, "line 1 column 1" in the message made `import json`
     the crash line.

   Now the crash line is the deepest frame in the program's own file.
5. **The notes never said what failed.**
   - "Run check still failing after fixes: …" quoted the first 160
     characters of the traceback. Those are always "Traceback (most recent
     call last):" and a file path.
   - The logic-test note quoted pytest's last line, "1 failed in 0.06s".
     With pytest-asyncio installed, as it is here, that line is a
     deprecation warning, because stderr was appended after pytest's report.
   - The fix prompts were given the start of the traceback, which cuts the
     exception off a long one.

   Now `failure_line` picks the line that says what went wrong. pytest's
   report comes last, and the prompts get the end of the traceback.
6. **The logic-test note blamed tests that passed.** Tests passed on a
   program whose main block crashes, and the note read "Logic tests
   inconclusive (code left unchanged; a test may be wrong): unknown". Now it
   reads "Logic tests passed (test_main.py), but the program itself does not
   start", followed by the TypeError. A fix that made the tests pass but
   stopped the program starting now says that.
7. **An xfailed test counted as passing.** Exit 0 with "1 xfailed" means the
   only test failed, as it was marked to. It read as "Logic tests passed".
8. **A new file that did not compile was reported as a change.** An
   improvement that added `helper.py` with a syntax error left it on disk and
   logged "Changed: helper.py". Now the file is removed and logged as not
   changed.
9. **A failed improvement left no record.** It was logged, and the UI showed
   it, but PROJECT.md, `last_error` and the announcer heard nothing. So
   "what's the status?" answered with the last thing that had worked. Now it
   is recorded the way a failed build is, keeping everything PROJECT.md
   already said.
10. **Every improvement reset PROJECT.md's file list and How to run** to
    "(none yet)" and "(pending)". Both are now kept, and new files are added
    to the list.
11. **A change given as a bare path failed the improvement** with "'str'
    object has no attribute 'get'". The build accepts the same shape in its
    plan.
12. **Suggestions in an unexpected shape.** One suggestion given as a string
    became three letters: "A", "d", "d". A `null` failed the build after its
    files had been written and verified.
13. **A failed build kept generating.** When one file's generation failed,
    the others carried on, holding the GPU and publishing progress for a
    build already reported failed. Two finished after the end. Now they are
    cancelled.
14. **The HTML repair accepted a stub.** It was not compared with the page
    it replaced, so a ten-line page that draws one rectangle could "repair"
    a whole game. It is now judged like every other repair.

**The quiet handlers in the builder and the HTML check, reviewed.** Layer 1
counted 16 unreached quiet blocks in `project_builder.py`. With this suite
reaching them, each was judged:

- **Three could lose a real failure, and now say it.**
  - `status_text` read a goal store that raised as "no planned work". The
    status then let the completion state speak for both axes, which its own
    comment forbids. It now logs `project_goals_unreadable`, and the answer
    says it could not check the planned work.
  - `last_active` read a fact store that raised as "nothing is current". It
    now logs `last_active_unreadable`.
  - `_validate_criteria` checked criteria against an empty program when the
    entry file could not be read. It now logs `criteria_entry_unreadable`.
- **One behaved differently from its neighbour.** A current-project pointer
  that is not a project name at all (`..`) was returned as "nothing current"
  and left in place. A pointer to a deleted project is cleared. Both are now
  cleared.
- **The rest are right as they are**, and carry a `# quiet-ok:` reason. In
  each, the exception IS the answer, or the failure is returned to a caller
  that reports it: a run check's crash text, a compile error, a smoke-test
  note.

One quiet block is left in the builder, in `write_repro_check`. That is one
of the twenty unused functions, and item 7 tests it.

**Mutation checks: 117 deliberate breaks, all caught, after four of my
checks were corrected.**

The first run broke 105 places: every fix reverted on its own, and every
branch this suite reaches for the first time. 101 were caught. Each of the
four that survived was a check something else could satisfy, not a gap in
the code:

- **The cancel test** checked that nothing finished after the build ended.
  A version that waited for the other generations instead of cancelling
  them made them finish before the end, so the check held. It now counts
  the calls: four, not six.
- **The patch prompt** states the error on a line of its own. So "the error
  is in the prompt" passed with the traceback cut. The check now looks
  inside the traceback block.
- **Two `failure_line` samples** were too simple to tell its rules apart.
  - One had a single `E` line, where the rule picks the last of several.
  - One was a single line, which the fallback returns anyway.

  Each now has a sample that passes only by the rule it pins.

A second run broke those four again, and nine new places: the status answer,
the current-project pointer and the criteria entry read. 13 of 13 were
caught.

**Then my own `failure_line` turned out too loose.** Reviewing the diff, I
saw that it read pytest's rules ("FAILED …", "E   …") in ANY text. A crashing
program that prints a board row "E . . X", or a log line "ERROR - lost
connection", would have had that line quoted as its error. Those rules now
apply only inside a pytest report. A third run broke the new conditions three
ways, and re-broke four of the function's earlier rules against the new
shape: 7 of 7 caught. Every file was restored byte-identically after every
run.

**The first full run found a suite that finding 9 broke, and the cause was
the test's timing.** In `test_pending_plan_s13`, "Update the README." started
nothing. The three S13 pending-plan suites send their next instruction the
moment the previous one's plan prompt appears. Their scripted planner returns
no changes, so every improvement fails at once. Before finding 9, a failed
improvement did nothing after its plan, so it was always over before the next
message arrived.

It now records the failure first, which takes 30 to 50 ms, measured. An
instruction sent inside that window was told, truthfully, "I'm still working
on flappy-bird". That happened three times out of three when the test's
timing was replayed. What the suites test, the routing, is unchanged. Their
wait helpers now also wait for the builder to be free, as a person's next
message would. Every assertion is as it was. The second full run found the
same race in `test_project_selection_s13`, and it got the same wait.

Freeing the project BEFORE recording the failure was considered and
rejected. A new improvement could then start mid-report. The failed one's
`evaluate` and announcement could then land on the new requirement
revision, and so suppress that revision's own announcement.

**Full run, 2026-11-07, on the final builder: 204 of 204 suites passed.**
203 passed in the second full run. The 204th,
`test_project_selection_s13`, failed there on the timing race above. It
passed after its test-only fix, against the same builder.

**Recorded, not changed:**

- **The long-file guard (finding 2) is a decision for Marcus.** The real
  projects are already past the window, read from the working tree:
  `flappy-bird/main.py` is 12,451 characters and `gravity-runner/main.py` is
  8,244. At roughly 3 to 4 characters per token, `flappy-bird` is also about
  as long as the 3,000 tokens `_llm_file` has to write a whole file in, a
  budget its reasoning spends from too (estimated, not tokenized). So a whole
  rewrite of it could hardly ever have come back complete, whatever the
  window. Improvements to both projects now stop with a reason instead of
  silently cutting the files. The lasting fix is to
  edit long files in pieces, or by find/replace patches as the crash fix
  already does.
- After a successful improvement the previous suggestions are still
  dropped. Keeping them could offer again one the improvement just did, and
  dropping them loses the rest. Marcus's call.
- A missing project module is reported as "package 'game_logic' is not
  installed on this machine". The run check cannot tell a pip package from a
  file the model forgot to write.
- `_validate_criteria` passes a `declare_scaffold` callback to
  `check_criterion`. That function has ignored it since checks moved to a
  copy of the project (audit H18), so the callback can never run.
- `_verify_and_fix_runtime` ends with a `return None` that no path reaches.
- `start()` accepts a `requested_by` that no caller passes and nothing
  reads. That is one for item 8's small fixes.
- The `..` test in `_build` refuses nothing the containment check would
  allow, apart from paths that resolve inside the project anyway. It was
  kept.
- Asserts inside `if __name__ == "__main__":` in a generated test count as
  checks only when they fail. A main block whose asserts all pass reads as
  "No test actually passed". That fails safe, so it was left.
