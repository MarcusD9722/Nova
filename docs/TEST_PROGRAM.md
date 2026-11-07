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
| 2 | **Function tests** | each function does what it is *meant* to do; every test mutation-checked | offline gate | next — order at the end of layer 1 |
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
Its rules are pinned by `tests/test_coverage_map_tool.py`: 32 deliberately
broken versions of the tool, all caught by the suite. The pinned rules cover
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
| 1 | `cloud_runtime.py:192` `_load_spend` | only a missing file (×398) | a truncated or unreadable spend file reads as "nothing spent today", which silently restarts the daily cloud cap. **Worse, found while checking this:** a spend file holding valid JSON that is not an object (`null`, `[1,2]`) raises `AttributeError` out of `CloudRuntime()`. `RuntimeManager.__init__` builds that unconditionally, cloud enabled or not, and the backend's startup builds `RuntimeManager` with no `try` around it, so one bad file in `memory_data/` should stop Nova booting. The `CloudRuntime()` crash is reproduced on a temp file; the boot failure is read from the code, not run |
| 2 | `completion_artifacts.py:55` `declared_scaffold` | only a missing manifest (×757) | a corrupt manifest reads as "no scaffolding". Nova's own validation files then count as project files, and the next `declare_scaffold` overwrites the manifest, losing what it listed (the write is not atomic) |
| 3 | `conversation_state.py:47` `from_obj` | the guard test's `"not json"` | a corrupt saved conversation state is replaced by an empty one |
| 4 | `project_builder.py:632` `_handle` | the test's injected router failure | a router that raises silently builds on the local model. That would be a bug: a misconfigured role is already warned about when the router is built |
| 5 | `project_builder.py:971` `_read_project_md` | only a missing file (×122) | any other read failure reads as "no PROJECT.md" |
| 6 | `memory_ingest.py:218` `_ingest_scoped` | the test's non-numeric confidence | **a fact whose database write fails is dropped without a word**: the "remember this" path |
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
   reported and the behaviour around it is unchanged.
2. **Memory: "remember this", recall and forget.**
   - `forget`, `recent_turns_text`, `facts_needing_reverification`;
   - reminders: reschedule, list, cancel;
   - `add_fact`'s supersession branches;
   - the 55 unreached quiet handlers in `unifier.py`.
3. **Understanding: question, statement, remember-this, command.**
   - the two known misses: `asks_about_work` misses "Did the build finish?",
     and "For blog, add a sidebar, but not yet" drops the proposal;
   - the untaken branches in `project_intent`, `understanding`, `recall_gate`
     and `episodic_recall`.
   
   Layer 4 tests the same judgements at corpus scale.
4. **Projects: plan, build, run, report.** The untaken branches of `_build`,
   `_generate_and_run_tests`, `_verify_and_fix_runtime`,
   `_fix_runtime_error_patch` and `_smoke_test_html`.
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
