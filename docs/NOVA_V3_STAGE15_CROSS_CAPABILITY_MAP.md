# Stage 15 — cross-capability map

Read from the code, not from the architecture comments. Where a claim here came
from a docstring rather than a traced call path, it says so.

Base: `main` `6f575ed8f359dda3e395f5f7fdba6195aa221c6b`.

---

## The one pipeline

There is a single production turn path. `/chat` and `/chat/stream` are the same
code: `chat_turn` aggregates the stream that `chat_turn_stream` yields
([core/runtime.py:1461](core/runtime.py:1461)). That matters for Stage 15 —
a test that drives one is driving both.

```
POST /chat  /chat/stream
        │
        ▼
chat_turn ─────────────► chat_turn_stream ──► active_turn(identity)  ← ContextVar
                                            └► GATE.turn()           ← TurnGate
                                                    │
                                                    ▼
                                          _chat_turn_stream
                                                    │
   ┌────────────────────────────────────────────────┼───────────────────────────┐
   │ 1. _project_prepass          may RETURN EARLY (build/status/resume/improve) │
   │ 2. fact / lesson / mood capture      best-effort, logged on failure         │
   │ 3. _direct_live_reply        may RETURN EARLY (clock, weather, name)        │
   │ 4. identity → memory_entity  scope for every personal read/write            │
   │ 5. storytelling mode         may RETURN EARLY                               │
   │ 6. working context + recall gate → memory.search                            │
   │ 7. _episodic_context                                                        │
   │ 8. _build_grounding_context  ← the assembled truth handed to the model      │
   │ 9. deep mode (planner → executor → critic), opt-in                          │
   │10. ToolLoopExecutor.run      reason → act → observe                         │
   │11. artifact capture from tool results                                       │
   │12. streamed answer + work_context + _completion_context                     │
   │13. _finish: working ctx, state_store.record_turn, memory ingest QUEUE       │
   └─────────────────────────────────────────────────────────────────────────────┘
```

**Authoritative-source rule of thumb, as actually implemented:** durable rows
are authority; `PROJECT.md` and frontend state are projections; the model's
prose is never authority (Stage 14 §S14-6, and `_unapproved_notice` composes the
refusal text from the tool's structured payload rather than trusting the model).

---

## Capability nodes and where they live

| capability | module | authoritative state |
|---|---|---|
| Conversation / turn | `core/runtime.py` | `ConversationStateStore` |
| Speaker identity | `core/turn_identity.py` | ContextVar, per turn |
| Memory (structured) | `memory/unifier.py`, `memory/backends/sqlite_backend.py` | SQLite |
| Semantic retrieval | `memory/backends/chroma_backend.py`, `memory/episodic_recall.py` | Chroma + SQLite |
| Projects | `core/project_builder.py`, `core/project_manager.py` | filesystem + SQLite |
| Project selection | `core/project_intent.py`, `core/project_names.py` | `last_active` pointer |
| Planning | `core/goal_planner.py`, `core/orchestrator/deep_mode.py` | proposal rows |
| Completion | `core/completion*.py` | `project_requirements`, `acceptance_*` |
| Goals / tasks | `core/agent_supervisor.py` | goal + task rows, generation-fenced |
| Autonomy | `core/autonomy*`, `backend/routers/autonomy.py` | task rows |
| ToolRouter | `core/tool_router.py` | none (stateless) |
| Tool loop | `core/orchestrator/agent.py` | none (per turn) |
| Permissions | `core/permissions.py` | in-memory `_pending` + audit file |
| Event bus | `core/event_bus.py` | in-process ring buffer |
| Completion events | `core/completion_events.py` | `completion_announcements` (durable) |
| Human decisions | `core/completion_service.py` | `human_decisions` (durable) |
| Artifacts | `memory/artifacts.py` | SQLite |
| API | `backend/app.py`, `backend/routers/*` | — |
| Frontend | `frontend/src` | — |

---

## The seams, and what each one is trusted to carry

Each row is a handoff. "Carries identity/project/revision" means the value is
*passed*, not merely available somewhere in the process.

| # | producer → consumer | carries | authority after the hop | notes from the code |
|---|---|---|---|---|
| H1 | HTTP → `chat_turn_stream` | identity | ContextVar for the whole turn | scoped at the single choke point so early returns cannot lose it |
| H2 | turn → `_project_prepass` | text, conversation | may return early | an explicitly named unknown project is refused, never silently replaced (`_no_such_project`) |
| H3 | identity → memory read | `memory_entity` | `may_read_entity`, `remap_entity_for` | personal namespace enforced in `turn_identity`, not at each call site |
| H4 | turn → memory ingest | identity **snapshot** | queue | deliberately snapshotted: the worker never inherits a live speaker |
| H5 | grounding → model | assembled context | prose only | model output is never authority |
| H6 | model → ToolRouter | tool name + args | `ToolResult.ok` | router owns timeout and retry-safety |
| H7 | ToolRouter → PermissionBroker | capability + details | `decision` | **broker records no requester identity** |
| H8 | PermissionBroker → tool | approved bool | `settled_as` | refusal is an answer; the loop fences the tool for the turn |
| H9 | tool result → answer text | structured payload | `_unapproved_notice` | refusal wording composed from the payload, not the model |
| H10 | tool result → artifacts | args + result | SQLite | provenance keeps the query, not just the answer |
| H11 | builder → completion | request, criteria, digest | `acceptance_*` rows | Stage 14; criteria recorded before code |
| H12 | completion → chat | `Verdict` | evaluator | `_completion_context`; PROJECT.md is a projection |
| H13 | completion → events | `Verdict` | `completion_announcements` | durable, one claim per transition |
| H14 | supervisor → tasks | goal id + **generation** | task rows | generation fencing, stale decisions discarded |
| H15 | restart → everything | durable rows only | SQLite + files | no process-local object may explain state |
| H16 | events → frontend | payload | backend rows | frontend must not outrank backend |

---

## Where I expect Stage 15 to find things

These are not claims of defects. They are the seams where two individually
correct subsystems could still combine badly, ranked by how little the existing
frozen stages constrain them.

**S-1 — permission has no identity (H7/H8).** `PermissionBroker.request()`
records capability, tier and details; it does not record who asked.
`resolve(request_id, approved, by="user")` takes a free-text `by` and checks
nothing about it, and `POST /permissions/resolve` sends no identity at all
([backend/routers/dev.py:86](backend/routers/dev.py:86)). Stage 14 deliberately
derived `channel` server-side for *completion* decisions; the permission broker
predates that and did not get the same treatment. Whether this is exploitable
depends on whether a non-owner turn can reach a gated capability at all — which
is S-2.

**S-2 — destructive project tools are not owner-gated.** `documents`
([core/runtime.py:704](core/runtime.py:704)) and `skills`
([core/runtime.py:946](core/runtime.py:946)) both refuse a non-owner with
`scoped_unavailable`. `project.delete` / `project.trash` / `project.purge` have
no such check — they rely entirely on the permission prompt, which per S-1 is
identity-free. To be tested, not assumed.

**S-3 — memory scope across project switching (H3).** Scoping is enforced in
`turn_identity` helpers rather than at each call site, so any read that bypasses
`may_read_entity`/`remap_entity_for` is unscoped by construction.

**S-4 — background vs foreground attribution (H4/H14).** The ingest snapshot is
right; the question is whether every other background producer does the same,
and whether event consumers attribute by payload rather than by arrival order.

**S-5 — the event bus is a process-local ring buffer.** `BUS.recent()` is
process-wide. Stage 14 twice attributed other tests' events to the code under
test using it. Any *production* consumer that reads `recent()` rather than
subscribing is exposed to the same class of error.

**S-6 — `last_active` as a fallback (H2).** An explicitly named project must
always beat the pointer. Stage 13A covers the selection path; Stage 15 must
check the *mutating* paths reach the same answer.

**S-7 — restart across a seam mid-flight (H15).** Frozen stages restart between
whole operations. Stage 15 restarts *between capabilities* — e.g. after a
permission request but before its answer, where the broker's pending map is
in-memory and its audit file is the only durable trace.

---

## What is already constrained, and by which frozen stage

Recorded so Stage 15 does not re-litigate settled ground, and so a failure in
one of these is recognised as a *regression* rather than a new finding.

- **13A** — intent, project selection, proposal lifecycle, correction/deferral.
- **13B** — task outcome truth, generation fencing, permission lifecycle,
  project delete, artifact drift, identity isolation.
- **13C** — restart states, crash windows, migration, reconstruction,
  permission durability across restart.
- **14** — completion derivation, acceptance contracts, evidence fencing, human
  confirmation, completion events, projections, chat truth, endpoint matrix.

Stage 15 owns everything *between* these.
