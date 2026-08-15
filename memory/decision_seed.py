from __future__ import annotations

"""Nova's architectural decisions, as data.

`docs/NOVA_DECISIONS.md` stays the human-readable record — it is easier to read,
easier to review in a diff, and it is where a person will actually look. This
module is the same content in a form Nova can *retrieve*, so that

    "Why do unknown MCP capabilities require confirmation?"

is answerable from memory with the rationale attached, rather than by reading
`permissions.py` and inferring intent from the implementation. Inferring intent
from an implementation is exactly how a deliberate decision gets undone by
someone who assumed it was an accident.

The two views are kept deliberately, not redundantly: prose for humans,
structure for retrieval and supersession. Seeding is idempotent and never
overwrites a decision that already exists in the database, so anything Nova (or
Marcus) records later is not clobbered by a restart.
"""

from typing import TYPE_CHECKING

from core.logging_setup import get_logger
from memory.episodes import Decision

if TYPE_CHECKING:
    from memory.episodes import EpisodicStore

logger = get_logger(__name__)


def seed_decisions() -> list[Decision]:
    """The decisions recorded during V2/V3, mirroring docs/NOVA_DECISIONS.md."""
    return [
        Decision(
            id="D1",
            title="XTTS runs in an isolated child process",
            decision="XTTS synthesis runs in a dedicated child process with its own "
                     "CUDA context, not in the backend process.",
            rationale="In-process CUDA XTTS beside llama.cpp aborts the backend with "
                      "'CUDA error: an illegal memory access was encountered' in "
                      "ggml_backend_cuda_synchronize. It cannot be serialised behind "
                      "GPU_SEM either: sentence-streamed TTS overlaps generation by "
                      "design and the reply stream holds the permit for the whole "
                      "generation, so taking it deadlocks (measured: 195s hang, then "
                      "access violations on every later turn).",
            alternatives=["CPU-only XTTS (4x slower, and the config claiming it was "
                          "unreachable)",
                          "Serialising on GPU_SEM (deadlocks)",
                          "A second GPU (hardware not available)"],
            evidence=["tests/live_voice_validation.py: 10/10 turns, 28 concurrent CUDA "
                      "clips, 0 aborts, +0.05 GB VRAM drift, RTF 0.73",
                      "core/gpu.py records the original failure"],
            subsystem="voice",
            source_refs=["services/tts_worker.py", "services/tts_client.py", "core/gpu.py"],
            constraints="Revisit only if a future llama.cpp/torch combination is PROVEN "
                        "to share a CUDA context safely under the same ten-turn "
                        "concurrent test.",
            decided_at="2026-08-12T00:00:00+00:00",
        ),
        Decision(
            id="D2",
            title="STT stays on CUDA",
            decision="faster-whisper keeps ('cuda','float16') as its first attempt, in "
                     "the backend process, despite CTranslate2 being a second CUDA "
                     "runtime there.",
            rationale="The risk is real in shape but was not borne out by measurement. "
                      "Moving STT to CPU on suspicion would be a silent regression with "
                      "nothing behind it.",
            alternatives=["Move STT to CPU on suspicion (rejected: no evidence)"],
            evidence=["tests/bench_cuda_coexist_v3.py: seven configurations, zero "
                      "inference errors, zero aborts, zero XTTS restarts, VRAM flat at "
                      "9.04 GB",
                      "Contention measured: llama.cpp +6%, whisper +185%, XTTS +209%"],
            subsystem="stt",
            source_refs=["backend/app.py::_load_stt_engine"],
            constraints="Re-run the coexistence matrix before changing the device. A "
                        "single observed CUDA abort with STT on GPU reopens this.",
            decided_at="2026-08-12T00:00:00+00:00",
        ),
        Decision(
            id="D3",
            title="The FAST reasoning contract is a closed-block prefill",
            decision="Ordinary conversational replies (thinking=False) prefill the "
                     "assistant turn with an already-closed reasoning block so the model "
                     "continues from after it. Decision-making paths (decide(), deep "
                     "mode) keep native reasoning.",
            rationale="Model compute TTFT is 35ms; roughly 10.3s per turn was hidden "
                      "reasoning that was generated and then discarded. Prompt wording "
                      "does not control it — five rewordings were measured and the "
                      "shipped prompt was the best of them. The chat template is what "
                      "opens the block, so the control has to live there.",
            alternatives=["Prompt rewording (measured; shipped prompt was best)",
                          "A short token budget (79ms median but 17/18 EMPTY replies — "
                          "fast because it fails fast)",
                          "/no_think alone (barely helped, raised empties to 6/18)"],
            evidence=["tests/bench_ttft_v3c.py: first-visible-token 8169ms -> 36ms "
                      "simple median, empty replies 5/18 -> 0/18",
                      "tests/bench_nova_v3.py: end-to-end TTFT median 12127ms -> 130ms"],
            subsystem="llm",
            source_refs=["core/llm_runtime.py::_apply_no_think", "core/runtime.py"],
            # Ordered most-operative first, deliberately. Retrieval clips long
            # fields, so the two facts that change what a future agent DOES —
            # it is Qwen-specific, and a model swap must re-measure — have to
            # survive the clip. The full reasoning stays in
            # docs/NOVA_DECISIONS.md, which is not budget-constrained.
            constraints="MODEL-SPECIFIC AND TEMPLATE-SPECIFIC: this is Qwen3-family "
                        "syntax, validated only against Qwen3.5-9B and its chat template. "
                        "Any model swap MUST re-measure with tests/bench_ttft_v3c.py "
                        "before the contract is trusted, and must never port the 36ms "
                        "figure across models. A reasoning contract is selected per model "
                        "/ per chat template, never applied globally. Applied blindly "
                        "elsewhere it would at best do nothing and at worst leak a literal "
                        "reasoning tag into the spoken answer, or trigger the very "
                        "pathology it prevents. NOVA_LLM_FAST_PREFILL=0 is an escape "
                        "hatch, not model selection.",
            decided_at="2026-08-13T00:00:00+00:00",
        ),
        Decision(
            id="D4",
            title="Unknown MCP capabilities require ADMIN confirmation",
            decision="MCP servers feed Nova's existing capability registry, ToolSelector, "
                     "permission broker, context firewall and artifact system. MCP tools "
                     "are ordinary ToolRouter tools with namespaced identities, and "
                     "unknown capabilities default to the ADMIN permission tier, which "
                     "requires confirmation.",
            rationale="An MCP server is third-party code running on Marcus's machine. "
                      "core/permissions.py::tier_of() already defaults unknown "
                      "capabilities to ADMIN, so an unrecognised remote action is never "
                      "auto-run. Server-supplied annotations (readOnlyHint, "
                      "destructiveHint) are hints FROM the thing being governed, so they "
                      "may only make a tool more restricted, never less. With no "
                      "permission broker wired at all, anything not explicitly read-only "
                      "is refused — governance being absent must not mean governance "
                      "being skipped.",
            alternatives=["A blanket MCP_ALLOWED flag (rejected: no granularity, and one "
                          "hostile tool would inherit every permission)",
                          "Trusting server annotations to lower a tier (rejected: the "
                          "governed party does not get to set its own tier)"],
            evidence=["tests/test_mcp_v3.py::test_permissions_are_enforced",
                      "tests/test_mcp_v3.py::test_hostile_metadata",
                      "docs/NOVA_V3_MCP.md"],
            subsystem="permissions",
            source_refs=["core/permissions.py", "core/mcp/manager.py::_check_permission"],
            constraints="Revisit only if the MCP specification adds a capability that "
                        "genuinely cannot be expressed as a governed Nova tool.",
            decided_at="2026-08-13T00:00:00+00:00",
        ),
        Decision(
            id="D5",
            title="Episodic memory lives in Nova's existing SQLite database",
            decision="Episodes, artifacts and decisions are tables in the existing "
                     "memory database (schema v7). Only heavy evidence goes to a "
                     "content-addressed filesystem store.",
            rationale="SQLite is already Nova's authoritative structured store and the "
                      "warm records are small, relational and want the same "
                      "transactional guarantees as facts. A second database would add "
                      "another thing to keep consistent, back up and migrate, for no "
                      "measured benefit. Cold evidence is different: it is large, read "
                      "rarely, never queried by content, and would sit inside every "
                      "backup and VACUUM of a database that is otherwise a couple of "
                      "megabytes.",
            alternatives=["A dedicated vector/document database for episodes (rejected: "
                          "no measured benefit, and it splits the source of truth)",
                          "Storing evidence blobs in SQLite (rejected: bloats every "
                          "backup for data that is read rarely)"],
            evidence=["tests/bench_episodic_v4.py: 2001 episodes = 1.22 MB database; "
                      "greeting adds 0.01ms and 0 prompt characters",
                      "tests/test_episodic_memory_v4.py"],
            subsystem="memory",
            source_refs=["memory/episodes.py", "memory/cold_store.py",
                         "memory/episodic_schema.py"],
            constraints="A missing or corrupt cold payload must never break memory: warm "
                        "records are self-sufficient and cold hydration is an optional "
                        "enrichment that returns None rather than raising.",
            decided_at="2026-08-13T00:00:00+00:00",
        ),
        Decision(
            id="D6",
            title="Episodic search fails CLOSED, unlike fact recall",
            decision="needs_episodic_memory() requires positive evidence that a turn "
                     "references the past. The fact-recall gate (should_recall) does the "
                     "opposite and fails open.",
            rationale="The two gates guard different failure modes. Forgetting a fact "
                      "Nova knows is the worst thing she can do, so that gate errs toward "
                      "searching. Episodic search is a database query for things that "
                      "HAPPENED; running it on every turn costs latency and buys almost "
                      "nothing, and the cost lands on exactly the conversational turns "
                      "P2.5 worked to make fast.",
            alternatives=["Reuse the fact-recall gate directly (rejected: it fails open, "
                          "which would put a database query on every greeting)",
                          "Always search and rely on the budget to trim (rejected: the "
                          "cost is the query, not the characters)"],
            evidence=["tests/bench_episodic_v4.py: a greeting costs 0.01ms and adds 0 "
                      "prompt characters against a 2001-episode corpus",
                      "tests/test_episodic_memory_v4.py::test_fast_path_isolation"],
            subsystem="memory",
            source_refs=["memory/episodic_recall.py::needs_episodic_memory",
                         "memory/recall_gate.py::should_recall"],
            constraints="If users report Nova failing to recall something she demonstrably "
                        "stored, check this gate BEFORE touching ranking — a closed gate "
                        "fails silently and looks like a retrieval-quality problem.",
            decided_at="2026-08-13T00:00:00+00:00",
        ),
        Decision(
            id="D7",
            title="Durable memory is promoted by ONE hook on the hot artifact store",
            decision="Anything that produces an artifact is considered for durable memory "
                     "by virtue of producing one. ArtifactStore announces each complete "
                     "unit to a single promotion hook; the runtime decides eligibility "
                     "and enqueues. No subsystem writes episodes itself.",
            rationale="The alternative is each producer persisting its own history, and "
                      "producers do not agree for long. MCP is the proof: McpManager "
                      "already stored an artifact with server, remote tool, arguments, "
                      "schema hash and injection flag, so it became durable the day the "
                      "hook existed — no MCP-specific persistence and no second path to "
                      "keep in sync. A hook on the hot store is also the only place that "
                      "sees every producer, including ones not written yet.",
            alternatives=["Persisting inside the tool loop (rejected: misses MCP and "
                          "capabilities, which do not go through it)",
                          "Each subsystem writing its own episodes (rejected: two paths "
                          "disagree within a release, and trust/provenance handling gets "
                          "reimplemented per subsystem — how a security invariant erodes)"],
            evidence=["tests/bench_episodic_v41.py: enqueue 0.035ms on the turn vs 41.7ms "
                      "to complete the write in background — three orders of magnitude",
                      "tests/test_episodic_integration_v41.py covers MCP promotion, "
                      "duplicate delivery, shutdown drain and failure isolation"],
            subsystem="memory",
            source_refs=["memory/artifacts.py::ArtifactStore._notify",
                         "core/runtime.py::_on_artifact_captured",
                         "core/workers/episodic_ingest.py"],
            constraints="The hook runs ON the turn path: it must stay synchronous, cheap "
                        "and non-raising — decide and enqueue, never await, never touch "
                        "the database. Persistence is a background worker that drains "
                        "BEFORE it stops and drops rather than blocks when saturated. An "
                        "episode may be lost to back-pressure; a reply may not. "
                        "REFINED BY D9 (P4.2): the single-path principle held, but the "
                        "assumption that every promotable event is artifact-backed did "
                        "not. Read D9 before changing promotion.",
            decided_at="2026-08-14T00:00:00+00:00",
        ),
        Decision(
            id="D8",
            title="Explicit past wording outranks the result set on screen",
            decision="Ordinals resolve in a fixed order: present-tense wording resolves "
                     "against the HOT set; past-tense wording resolves against the "
                     "historical set EVEN when something is on screen, and the hot "
                     "selection is then dropped; when neither is clearly meant, Nova asks "
                     "instead of choosing.",
            rationale="'The second drive we looked at yesterday' matches the set currently "
                      "on screen too — positionally, by coincidence — so both layers "
                      "resolve it. Presenting both leaves the model a prompt saying 'he "
                      "means the LG monitor' AND 'he means the WD Gold', which is worse "
                      "than either answer alone. The user's tense is the only evidence "
                      "available about which set they mean, and it is unambiguous.",
            alternatives=["Hot always wins (rejected: ignores the word 'yesterday', which "
                          "is the user saying exactly what they mean)",
                          "Most-recent historical set wins ties (rejected: a guess wearing "
                          "a confident face)",
                          "Ask an LLM to pick (rejected: makes a deterministic operation "
                          "probabilistic)"],
            evidence=["tests/test_episodic_integration_v41.py::"
                      "test_current_ordinal_precedence",
                      "tests/test_episodic_integration_v41.py::"
                      "test_historical_wording_outranks_hot",
                      "tests/test_episodic_integration_v41.py::"
                      "test_ambiguity_is_not_resolved_arbitrarily"],
            subsystem="memory",
            source_refs=["core/runtime.py::_episodic_context",
                         "memory/episodic_recall.py::resolve_historical_reference"],
            constraints="Ambiguity must survive as ambiguity. Two historical result sets "
                        "within 1.25x of each other resolve to a QUESTION, and must never "
                        "be settled by whatever happens to be on screen.",
            decided_at="2026-08-14T00:00:00+00:00",
        ),
        Decision(
            id="D9",
            title="One promotion POLICY, not one promotion SOURCE",
            decision="Durable memory is decided in exactly one place "
                     "(core/episodic_promoter.py) and written by exactly one thing (the "
                     "P4.1 queue and worker) — but events may come from several sources "
                     "and are no longer required to be artifact-backed. "
                     "EpisodicPersistEvent carries either a live artifact or a "
                     "self-describing event with its own stable identity.",
            rationale="D7's real content — no subsystem writes its own episodes — held. "
                      "What it ASSUMED, because every case in front of it was "
                      "artifact-backed, is that hanging promotion off ArtifactStore was "
                      "sufficient. It is not: a correction is not an artifact, a project "
                      "milestone is not an artifact, a recurring failure is not an "
                      "artifact. The alternative was manufacturing artifacts for events "
                      "that have none purely to keep D7's wording true, which means "
                      "fabricating evidence to fit a data shape — a synthetic artifact "
                      "would carry a trust class, a freshness class and provenance "
                      "describing something that never existed.",
            alternatives=["Fake artifacts for non-artifact events (rejected: invents "
                          "evidence)",
                          "A second promoter per source (rejected: two policies drift, "
                          "and each reimplements trust and provenance handling — how a "
                          "security invariant erodes)",
                          "Passing worth_remembering() more booleans from a new detector "
                          "(rejected: a second correction/failure classifier disagreeing "
                          "with the one fact memory and ErrorLog already run)"],
            evidence=["tests/test_episodic_events_v42.py: 34 interactions -> 7 episodes; "
                      "three phrasings of one choice -> one; seven occurrences of one "
                      "failure -> one; redelivery of every event type is idempotent",
                      "tests/bench_episodic_v42.py: promotion decisions 1.3-10.7us; a bus "
                      "publish costs 6.1us with a subscriber vs 4.0us without"],
            subsystem="memory",
            source_refs=["core/episodic_promoter.py", "core/events.py",
                         "core/workers/episodic_ingest.py"],
            supersedes=None,
            constraints="Every source MUST supply a deterministic stable identity — "
                        "artifact id, error signature, entity+attribute, or project slug "
                        "plus publish timestamp. Random ids are prohibited on this path: "
                        "background delivery means every event can arrive twice, and "
                        "'Marcus chose the WD Gold' must not accumulate copies. The "
                        "promoter never writes and never calls a model. Refines D7 "
                        "rather than superseding it.",
            decided_at="2026-08-14T00:00:00+00:00",
        ),
    ]


async def ensure_seeded(store: "EpisodicStore") -> int:
    """Write any seed decision that is not already present. Idempotent.

    Never overwrites: a decision Nova or Marcus edited later must survive a
    restart, so this only fills gaps.
    """
    written = 0
    for dec in seed_decisions():
        try:
            if await store.get_decision(dec.id) is None:
                await store.record_decision(dec)
                written += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("decision_seed_failed", decision=dec.id, error=str(e)[:200])
    if written:
        logger.info("decision_memory_seeded", count=written)
    return written
