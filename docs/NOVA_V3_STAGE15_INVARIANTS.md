# Stage 15 — cross-capability invariants

Forty invariants, each with the code that enforces it and the test that would
catch it breaking. Status is honest: **held** means a Stage 15 test drives the
production path and asserts it; **frozen** means an earlier stage pins it and
Stage 15 has re-run that suite; **open** means it is not yet demonstrated here.

An invariant with no enforcement point named is one I could not find enforced
anywhere, which is itself a finding.

| # | invariant | enforced at | status |
|---|---|---|---|
| I1 | identity persists across a multi-turn workflow | `active_turn` ContextVar around the whole turn | frozen (13B identity isolation) |
| I2 | speaker identity never leaks private memory across identities | `may_read_entity`, `remap_entity_for` | frozen (13B) |
| I3 | project identity persists across topic changes | `last_active` + `known_slug_in_text` | **held** (s15 identity tokens) |
| I4 | explicit project names override current-project context | named-beats-pointer in `_completion_context`, prepass | **held** (s15 identity tokens, work scope) |
| I5 | a project switch updates only what should change | prepass selection path | frozen (13A) |
| I6 | unrelated conversation does not alter project state | token-boundary slug matching | **held** (s15 identity tokens) |
| I7 | planning does not silently execute | proposal rows; execution needs a separate turn | frozen (13A proposal lifecycle) |
| I8 | approval does not imply execution | `_gate` returns, tool still must run | frozen (13B permission delete) |
| I9 | execution does not imply success | `ToolResult.ok`, two-axis task state | frozen (13B outcome truth) |
| I10 | tool success does not imply task/goal/project completion | completion derived from criteria only | frozen (14) |
| I11 | completion requires current acceptance evidence | `derive_state` + digest fence | frozen (14) |
| I12 | stale evidence cannot affect current completion | artifact digest fence | frozen (14 fencing) |
| I13 | permissions follow their authoritative lifecycle | `_close_out`, `_settle`, `settled_as` | **held** (s15 permission targeting) |
| I14 | timed-out authorization cannot be retried silently | tool-loop fence on `not_approved` | frozen (13B) |
| I15 | denied destructive operations cannot silently reappear | same fence, per turn | frozen (13B) |
| I16 | one destructive request creates at most one execution path | `_fence_reason`, one prompt per ask | **held** (s15 permission targeting) |
| I17 | retry-safe tools may retry; side-effecting ones may not | `ToolRouter.is_retry_safe`, `execute(retries=0)` | frozen (13B) |
| I18 | background failure cannot become foreground success | — | open (§8) |
| I19 | foreground success cannot conceal background failure | — | open (§8) |
| I20 | failures stay associated with their real project/task/revision | generation fencing; event payloads | frozen (13B) / open at chat layer |
| I21 | cancellation prevents stale work becoming live again | `_close_out("abandoned")`, supervisor generation | frozen (13B/13C) |
| I22 | restart does not manufacture success | recovery writes terminal entries only | frozen (13C) |
| I23 | restart does not discard authoritative progress | durable rows | frozen (13C) |
| I24 | restart does not revive stale work | `_pending` stays empty after recovery | frozen (13C) |
| I25 | restart keeps the context needed to answer honestly | `describe_work_state`, `_completion_context` | frozen (13C reconstruction) |
| I26 | correction supersedes prior intent | requirement revisions, `carry_forward` | frozen (14) |
| I27 | stale planner output cannot execute after correction | supervisor generation fence | frozen (13B) |
| I28 | project A never completes or modifies project B | per-project rows; permission target | **held** (s15 permission targeting) |
| I29 | memory scope stays correct during project switching | entity namespaces | open (§6) |
| I30 | event payloads identify their true origin | `BUS.publish` payloads carry project/ids | open (§8/§15) |
| I31 | chat answers derive from authoritative state | `_completion_context`, `describe_work_state` | **held** (s15 work scope) |
| I32 | frontend cannot claim a success backend does not have | — | open (§23) |
| I33 | model prose cannot override authoritative refusal | `_unapproved_notice` composes from payload | frozen (13B) |
| I34 | destructive actions require the correct permission and target | `_gate` + `pending()` provenance | **held** (s15 permission targeting) |
| I35 | artifact identity stays consistent across validation/completion | `implementation_digest` | frozen (14) |
| I36 | no subsystem silently converts unknown into success | two-axis outcome | frozen (13B) |
| I37 | no subsystem silently converts partial into complete | completion derivation; survey scope | **held** (s15 work scope) |
| I38 | no subsystem converts a stale event/result into current truth | announcement ledger; digest fence | frozen (14) |
| I39 | no answer is assembled from unscoped fragments | scope rule in `_completion_context` | **held** (s15 work scope) |
| I40 | identical concepts use consistent identity keys | slug canonicalisation, token matching | **held** (s15 identity tokens) |

## Added while mapping

These are not in the brief's list. They came out of reading the code, and each
one is a place where two subsystems could disagree without either being wrong.

**I41 — a question's SCOPE is a property of the question, not of the answerer.**
Two context builders answering the same turn must agree about how many projects
the turn is about. Violated and fixed: `describe_work_state` surveyed every
project while `_completion_context` described one.

**I42 — a name match must be a whole token.** An identity key that matches
inside a longer word is not an identity. Violated and fixed: `one` matched
"d-one".

**I43 — a request that is waiting must say what it is waiting to do.** Anything
that lists pending decisions has to carry enough to decide, or the consumer
guesses. Violated and fixed: `pending()` returned bare ids.
