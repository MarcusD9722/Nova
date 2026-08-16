"""V3 P5.1d.3: the final persistent-state inventory before live voice.

The failure mode this closes: a tool bypasses speaker privacy because it touches
durable state that is not called "memory".

Every registered built-in was classified from its CODE, then the classification
was checked against behaviour. Reproduced on `641f499`:

    thoughts.note        P5.1d.2 closed the reader and left the writer, so a
                         guest's text still landed in the store Marcus reads back
    plan.*               a guest read the owner's plan AND overwrote it
    goal.create          a guest and an unknown speaker each created a goal row
                         plus an enqueued __decide__ task — unattended work
                         started by someone Nova cannot name
    skill.*              a guest listed, fetched, updated ("HACKED") and DELETED
                         the owner's learned skill
    memory.index_folder  a guest's folder was indexed into the owner document store
    research.track/list  a guest read and added to the owner's tracking registry
    agent.recall         specialist notes about Marcus returned to anyone

`_CLASSIFICATION` below is the inventory, and `test_every_tool_is_classified`
fails if a tool is added without one — so the next persistent-state tool cannot
default to global by omission.

Run:  venv\\Scripts\\python.exe tests\\test_speaker_persistent_state_v51d3.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")
os.environ.setdefault("NOVA_REPO_ROOT", str(REPO))

from harness import Checks, boot, run  # noqa: E402

check = Checks()

OWNER_THOUGHT = "OWNER-THOUGHT-660"
OWNER_PLAN = "OWNER-PLAN-SECRET-882"
OWNER_SKILL = "OWNER-SKILL-SECRET-993"
OWNER_TOPIC = "OWNER-TRACKED-TOPIC-441"
AGENT_SENTINEL = "AGENT-PRIVATE-SENTINEL-552"

# ── the inventory (§1) ───────────────────────────────────────────────────────
#
#   shared     safe for any speaker; no per-person meaning
#   owner      Marcus's durable store, no ownership column -> fail closed
#   scoped     has a real speaker:<id> representation
#   capability governed by PermissionBroker / dev mode, NOT by voice
#   ephemeral  no durable state
_CLASSIFICATION: dict[str, str] = {
    # personal memory, speaker-scoped (P5.1/P5.1d/P5.1d.2)
    "memory.remember": "scoped", "memory.recall": "scoped",
    "memory.correct": "scoped", "memory.learn_lesson": "scoped",
    "memory.remember_person": "scoped", "memory.recall_person": "scoped",
    # owner-private stores with no ownership column
    "memory.remember_event": "owner", "memory.timeline": "owner",
    "memory.related": "owner", "memory.path": "owner", "memory.link": "owner",
    "memory.index_folder": "owner", "thoughts.note": "owner",
    "thoughts.recall": "owner", "twin.profile": "owner",
    "executive.brief": "owner", "reminder.create": "owner",
    "plan.save": "owner", "plan.status": "owner", "plan.advance": "owner",
    "goal.create": "owner", "research.track": "owner", "research.list": "owner",
    "skill.detect": "owner", "skill.learn": "owner", "skill.list": "owner",
    "skill.get": "owner", "skill.update": "owner", "skill.branch": "owner",
    "skill.delete": "owner", "agent.recall": "owner",
    # Registered on the router by RuntimeManager rather than core/tooling.py.
    # The completeness check below is what surfaced these — `memory.synthesize`
    # reads the owner's indexed filesystem and `skill.run` reads (and reveals)
    # his learned workflows, so both were open until this pass.
    "memory.synthesize": "owner", "skill.run": "owner",
    # genuinely shared / system state
    "world.recall": "shared", "world.learn": "shared",
    "research.findings": "shared", "agents.roster": "shared",
    "experiment.record": "shared", "experiment.trial": "shared",
    "experiment.analyze": "shared", "experiment.list": "shared",
    "memory.rebuild_index": "shared",
    # society.consult stays available to everyone — it is a reasoning
    # capability, not a store. Its specialist prompts DID inject
    # `agent_recall` notes about Marcus, which is a leak one layer below
    # `agent.recall`; those notes are now omitted for a non-owner and the
    # council still deliberates.
    "society.consult": "shared",
    # capability-governed: permissions and dev mode decide, never the voice.
    # Restricting these by voice is precisely the "speaker = authentication"
    # mistake the whole phase refuses to make.
    "project.scaffold": "capability", "code.read": "capability",
    "code.write": "capability", "shell.exec": "capability",
    "self.list_code": "capability", "self.read_code": "capability",
    "self.propose_change": "capability", "self.register_project": "capability",
    "code.health": "capability", "code.impact": "capability",
    "code.index": "capability", "code.security": "capability",
    "code.symbols": "capability",
    "project.delete": "capability", "project.purge": "capability",
    "project.restore": "capability", "project.trash": "capability",
    "project.improve": "capability", "project.start_build": "capability",
    "project.status": "capability",
    "computer.act": "capability", "computer.observe": "capability",
    # Reads the live screen, which is Marcus's — but it is not durable state,
    # and it already requires broker consent per call. Gating it on voice would
    # convert a consent prompt into an identity check.
    "vision.look_at_screen": "capability",
    # no durable personal state
    "image.generate": "ephemeral", "video.generate": "ephemeral",
}

#: Owner-private tools that take no required arguments, so they can be swept.
_OWNER_SWEEP: list[tuple[str, dict]] = [
    ("thoughts.note", {"content": "GUEST-THOUGHT-771"}),
    ("thoughts.recall", {}),
    ("twin.profile", {}),
    ("executive.brief", {}),
    ("reminder.create", {"title": "GUEST-REMINDER", "when": "5pm"}),
    ("plan.save", {"goal_id": "g-owner", "vision": "GUEST-OVERWRITE-993"}),
    ("plan.status", {"goal_id": "g-owner"}),
    ("plan.advance", {"goal_id": "g-owner"}),
    ("goal.create", {"objective": "GUEST-GOAL-994"}),
    ("research.track", {"topic": "GUEST-TOPIC-998"}),
    ("research.list", {}),
    ("skill.detect", {}),
    ("skill.learn", {"name": "GUEST-SKILL-996", "steps": ["x"]}),
    ("skill.list", {}),
    ("memory.timeline", {"days": 30}),
    ("memory.link", {"from": "a", "to": "b", "predicate": "knows"}),
    ("memory.related", {"name": "a"}),
    ("memory.path", {"from": "a", "to": "b"}),
    ("memory.remember_event", {"note": "GUEST-EVENT", "date": "2026-09-01"}),
    ("agent.recall", {"agent_id": "research_scientist"}),
    ("memory.synthesize", {"topic": "mortgage paperwork"}),
    ("skill.run", {"skill_id": "anything"}),
]


def ident(status, pid=None, name=None, role="guest"):
    from core.speaker.matcher import SpeakerMatch
    from core.turn_identity import TurnIdentity

    class _P:
        pass
    prof = _P()
    prof.role = role
    return TurnIdentity.from_match(
        SpeakerMatch(status=status, profile_id=pid, display_name=name, attempted=True),
        profile=(prof if pid else None))


def OWNER():
    from core.turn_identity import TurnIdentity
    return TurnIdentity.typed()


def ALICE():
    return ident("known", "p-alice", "Alice")


def UNKNOWN():
    return ident("unknown")


async def call(nova, who, tool, args=None):
    from core.tool_router import ToolCall
    from core.turn_identity import active_turn
    with active_turn(who):
        r = await nova.runtime._router.execute(ToolCall(tool, args or {}))
    return r.result


async def count(mem, sql, params=()):
    import aiosqlite
    async with aiosqlite.connect(mem._sqlite._db_path) as db:
        cur = await db.execute(sql, params)
        row = await cur.fetchone()
        return int(row[0])


# ── §1 completeness ──────────────────────────────────────────────────────────

async def test_every_tool_is_classified():
    check.section("1: every registered built-in has an explicit classification")
    async with boot() as nova:
        registered = set(nova.runtime._router.list_tools())
        # P5.1d.4: plugins are NO LONGER subtracted. Subtracting them meant the
        # "complete" invariant excluded exactly the tools that reach Marcus's
        # connected Gmail, Calendar and Discord — the completeness claim was
        # true only of the set it had already narrowed to.
        import plugins.registry as preg
        specs = preg.REGISTRY.get_tools()
        plugin_names = set(specs)
        builtins = registered - plugin_names

        missing = sorted(builtins - set(_CLASSIFICATION))
        check(not missing,
              f"no built-in is unclassified — a new tool must be triaged, not "
              f"defaulted to global ({missing})")
        stale = sorted(set(_CLASSIFICATION) - builtins - {"shell.exec"})
        check(not stale, f"the inventory has no entries for tools that no longer exist ({stale})")
        check(len(builtins) >= 45, f"the registry is the real one ({len(builtins)} tools)")

        # Every plugin carries an explicit scope, and the two sets together
        # cover the live router exactly — no tool falls between them.
        unscoped = sorted(n for n, s in specs.items()
                          if s.data_scope not in preg.DATA_SCOPES)
        check(not unscoped, f"every plugin ToolSpec declares a data_scope ({unscoped})")
        uncovered = sorted(registered - set(_CLASSIFICATION) - plugin_names)
        check(not uncovered,
              f"EVERY tool in the live router is covered by one of the two "
              f"classifications ({uncovered})")


# ── the owner-private sweep ──────────────────────────────────────────────────

async def test_owner_private_tools_refuse_non_owners():
    check.section("every owner-private tool refuses a guest and an unknown")
    async with boot() as nova:
        for label, who in (("guest", ALICE()), ("unknown", UNKNOWN())):
            bad = []
            for tool, args in _OWNER_SWEEP:
                r = await call(nova, who, tool, args)
                if not (isinstance(r, dict) and r.get("ok") is False
                        and r.get("error") in {"scoped_unavailable", "unverified_speaker"}):
                    bad.append((tool, str(r)[:60]))
            check(not bad, f"a {label} is refused by all {len(_OWNER_SWEEP)} ({bad})")


# ── thoughts (§4) ────────────────────────────────────────────────────────────

async def test_thoughts_writer_matches_reader():
    check.section("4: the thoughts WRITER is gated too, not just the reader")
    async with boot() as nova:
        await call(nova, OWNER(), "thoughts.note", {"content": OWNER_THOUGHT})
        await call(nova, ALICE(), "thoughts.note", {"content": "GUEST-THOUGHT-771"})
        await call(nova, UNKNOWN(), "thoughts.note", {"content": "STRANGER-THOUGHT-882"})

        r = await call(nova, OWNER(), "thoughts.recall", {})
        check(r.get("ok") and OWNER_THOUGHT in str(r), "the owner's own note is kept")
        check("GUEST-THOUGHT-771" not in str(r),
              "a guest's text never enters the store he reads back")
        check("STRANGER-THOUGHT-882" not in str(r), "nor an unknown speaker's")

        # Asserted at the store, not just through the tool. Match the `content`
        # field with the FULL sentinel — thought ids are random hex, and a
        # 3-digit substring hits one by chance often enough to be flaky.
        from core.turn_identity import active_turn
        with active_turn(OWNER()):
            raw = await nova.memory.recall_thoughts()
        bodies = [str(t.get("content") or "") for t in raw]
        check(bodies == [OWNER_THOUGHT],
              f"and nothing was persisted underneath ({bodies})")


# ── plans (§5) ───────────────────────────────────────────────────────────────

async def test_plans_are_owner_only():
    check.section("5: plans")
    async with boot() as nova:
        r = await call(nova, OWNER(), "plan.save",
                       {"goal_id": "g-owner", "vision": OWNER_PLAN,
                        "milestones": [{"title": "m1", "target_date": "2026-12-01"}],
                        "items": [{"title": "i1", "cadence": "once", "due": "2026-12-01"}]})
        check(r.get("ok"), f"the owner saves a plan, unchanged ({r})")
        r = await call(nova, OWNER(), "plan.status", {"goal_id": "g-owner"})
        check(r.get("ok") and OWNER_PLAN in str(r), "and reads it back")

        for label, who in (("a guest", ALICE()), ("an unknown speaker", UNKNOWN())):
            r = await call(nova, who, "plan.status", {"goal_id": "g-owner"})
            check(OWNER_PLAN not in str(r), f"{label} cannot read the owner's plan")
            await call(nova, who, "plan.save",
                       {"goal_id": "g-owner", "vision": "GUEST-OVERWRITE-993"})
            await call(nova, who, "plan.advance", {"goal_id": "g-owner"})

        r = await call(nova, OWNER(), "plan.status", {"goal_id": "g-owner"})
        check(OWNER_PLAN in str(r) and "GUEST-OVERWRITE-993" not in str(r),
              f"and the owner's plan survives both, byte for byte ({str(r)[:80]})")
        check(r.get("ok") and r.get("progress", {}).get("milestones_total") == 1,
              "including its milestones")


# ── goals (§6) ───────────────────────────────────────────────────────────────

async def test_goals_create_no_rows_or_tasks_for_guests():
    check.section("6: a goal is a row AND unattended background work")
    async with boot() as nova:
        m = nova.memory
        g0 = await count(m, "SELECT COUNT(*) FROM goals")
        t0 = await count(m, "SELECT COUNT(*) FROM tasks")

        r = await call(nova, OWNER(), "goal.create", {"objective": "OWNER-GOAL-660 learn Spanish"})
        check(r.get("ok"), f"the owner's goal is created ({r})")
        g1 = await count(m, "SELECT COUNT(*) FROM goals")
        t1 = await count(m, "SELECT COUNT(*) FROM tasks")
        check(g1 == g0 + 1, f"one goal row ({g0} -> {g1})")
        check(t1 == t0 + 1, f"and one enqueued task ({t0} -> {t1})")
        d = await count(m, "SELECT COUNT(*) FROM tasks WHERE tool_name='__decide__'")
        check(d == 1, f"which is the __decide__ supervisor task ({d})")

        for label, who in (("a guest", ALICE()), ("an unknown speaker", UNKNOWN())):
            r = await call(nova, who, "goal.create", {"objective": f"GUEST-GOAL-{label}"})
            check(not r.get("ok") and r.get("error") == "scoped_unavailable",
                  f"{label} is refused ({str(r)[:60]})")
        check(await count(m, "SELECT COUNT(*) FROM goals") == g1,
              "no guest goal row was created")
        check(await count(m, "SELECT COUNT(*) FROM tasks") == t1,
              "and no background work was enqueued for anyone Nova cannot name")


# ── skills (§7) ──────────────────────────────────────────────────────────────

async def test_skills_are_owner_only():
    check.section("7: learned workflows, including the mutators")
    async with boot() as nova:
        r = await call(nova, OWNER(), "skill.learn",
                       {"name": OWNER_SKILL, "steps": ["step one", "step two"]})
        sid = r.get("skill_id")
        check(r.get("ok") and sid, f"the owner learns a skill ({r})")

        for label, who in (("a guest", ALICE()), ("an unknown speaker", UNKNOWN())):
            for tool, args in (("skill.list", {}), ("skill.get", {"skill_id": sid}),
                               ("skill.detect", {}),
                               ("skill.learn", {"name": "GUEST-SKILL", "steps": ["x"]}),
                               ("skill.update", {"skill_id": sid, "steps": ["HACKED"]}),
                               ("skill.branch", {"skill_id": sid, "new_name": "GUEST-FORK"}),
                               ("skill.delete", {"skill_id": sid})):
                r = await call(nova, who, tool, args)
                check(not r.get("ok") and r.get("error") == "scoped_unavailable",
                      f"{label} is refused by {tool}")
            check(OWNER_SKILL not in str(await call(nova, who, "skill.list", {})),
                  f"{label} never sees the owner's workflow name")

        r = await call(nova, OWNER(), "skill.list", {})
        names = [s.get("name") for s in r.get("skills", [])]
        check(names == [OWNER_SKILL],
              f"the owner's skill store is exactly as he left it ({names})")
        r = await call(nova, OWNER(), "skill.get", {"skill_id": sid})
        check(r.get("steps") == ["step one", "step two"] and r.get("version") == 1,
              f"unmodified and un-versioned by the attempts ({str(r)[:70]})")


# ── document index (§8) ──────────────────────────────────────────────────────

async def test_document_index_is_owner_only():
    check.section("8: the document index")
    async with boot() as nova:
        m = nova.memory
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "doc.txt").write_text("OWNER-DOC-660 mortgage paperwork",
                                              encoding="utf-8")
            d0 = await count(m, "SELECT COUNT(*) FROM documents")
            r = await call(nova, OWNER(), "memory.index_folder", {"path": td})
            check(r.get("ok") and r.get("indexed") == 1,
                  f"the owner indexes his own folder ({str(r)[:80]})")
            d1 = await count(m, "SELECT COUNT(*) FROM documents")
            check(d1 == d0 + 1, f"one document row ({d0} -> {d1})")

        with tempfile.TemporaryDirectory() as td2:
            (Path(td2) / "guest.txt").write_text("GUEST-DOC-997 confidential",
                                                 encoding="utf-8")
            for label, who in (("a guest", ALICE()), ("an unknown speaker", UNKNOWN())):
                r = await call(nova, who, "memory.index_folder", {"path": td2})
                check(not r.get("ok") and r.get("error") == "scoped_unavailable",
                      f"{label} is refused ({str(r)[:60]})")
            check(await count(m, "SELECT COUNT(*) FROM documents") == d1,
                  "and zero rows were added to the owner's index")


# ── research (§9) ────────────────────────────────────────────────────────────

async def test_research_registry_vs_public_findings():
    check.section("9: the tracking REGISTRY is his; sourced findings are public")
    async with boot() as nova:
        r = await call(nova, OWNER(), "research.track", {"topic": OWNER_TOPIC})
        check(r.get("ok"), f"the owner tracks a topic ({r})")

        for label, who in (("a guest", ALICE()), ("an unknown speaker", UNKNOWN())):
            r = await call(nova, who, "research.list", {})
            check(not r.get("ok") and OWNER_TOPIC not in str(r),
                  f"{label} cannot see what Marcus asked Nova to follow")
            r = await call(nova, who, "research.track", {"topic": f"GUEST-TOPIC-{label}"})
            check(not r.get("ok"), f"{label} cannot add to his registry")

        r = await call(nova, OWNER(), "research.list", {})
        topics = [t.get("topic") for t in r.get("topics", [])]
        check(topics == [OWNER_TOPIC], f"his registry is untouched ({topics})")

        # research.findings returns only sourced world facts — summary, source,
        # confidence — with no requester, timing, priority or rationale. That is
        # why it stays shared while the registry does not.
        from core.turn_identity import active_turn
        with active_turn(OWNER()):
            await nova.memory.world_learn(OWNER_TOPIC, "is", "an interesting field",
                                          source="https://example.org")
        for label, who in (("a guest", ALICE()), ("an unknown speaker", UNKNOWN())):
            r = await call(nova, who, "research.findings", {"topic": OWNER_TOPIC})
            check(r.get("ok"), f"{label} can still read sourced public findings")
            for f in r.get("findings", []):
                check(set(f) <= {"summary", "source", "confidence"},
                      f"and they carry no owner metadata ({sorted(f)})")


# ── agent memory (§10) ───────────────────────────────────────────────────────

async def test_agent_memory_classification():
    check.section("10: specialist notes — classified from evidence")
    async with boot() as nova:
        # Evidence for the classification: agent_remember has no production
        # caller, and the only note ever written through it (in
        # tests/test_society_p6.py) is "Marcus prefers primary sources over blog
        # posts." The store's designed content is HIS preferences.
        src = (REPO / "tests" / "test_society_p6.py").read_text(encoding="utf-8")
        check("Marcus prefers" in src,
              "the only writer in the tree stores a Marcus preference")

        await nova.memory.agent_remember(
            "research_scientist", f"{AGENT_SENTINEL} Marcus prefers primary sources",
            topic="preferences")

        r = await call(nova, OWNER(), "agent.recall", {"agent_id": "research_scientist"})
        check(r.get("ok") and AGENT_SENTINEL in str(r), "the owner reads them")
        for label, who in (("a guest", ALICE()), ("an unknown speaker", UNKNOWN())):
            r = await call(nova, who, "agent.recall", {"agent_id": "research_scientist"})
            check(not r.get("ok") and AGENT_SENTINEL not in str(r),
                  f"{label} does not")

        # agents.roster stays shared: specs and counters, never user content.
        for who in (ALICE(), UNKNOWN()):
            r = await call(nova, who, "agents.roster", {})
            check(r.get("ok"), "the neutral roster stays available")
            check(AGENT_SENTINEL not in str(r), "and carries no note content")


async def test_society_consult_does_not_leak_agent_notes():
    check.section("10: the layer below agent.recall — council prompts")
    # The society is off by default in the harness, so enable it: this is the
    # path that would have carried Marcus's notes into a guest's answer WITHOUT
    # anyone calling agent.recall, which is exactly the class of bug this phase
    # exists to find.
    async with boot(env={"NOVA_AGENT_SOCIETY": "1"}) as nova:
        # Seed every specialist: which ones get consulted depends on routing,
        # and the assertion should not quietly pass because the question
        # happened to miss the one specialist that held the sentinel.
        from core.orchestrator.society import roster
        for spec in roster():
            await nova.memory.agent_remember(
                spec["id"], f"{AGENT_SENTINEL} Marcus prefers primary sources",
                topic="preferences")

        nova.llm.reset_calls()
        r = await call(nova, ALICE(), "society.consult",
                       {"question": "what should I read first?"})
        guest_prompts = " || ".join(nova.llm.prompts)
        check(r.get("ok"), f"the council still deliberates for a guest ({str(r)[:70]})")
        check(nova.llm.prompts, "and actually ran")
        check(AGENT_SENTINEL not in guest_prompts,
              "but carries none of Marcus's accumulated notes")

        nova.llm.reset_calls()
        r = await call(nova, OWNER(), "society.consult",
                       {"question": "what should I read first?"})
        check(AGENT_SENTINEL in " || ".join(nova.llm.prompts),
              "while the owner's council still gets his context, unchanged")


# ── deliberately left shared (§11) ───────────────────────────────────────────

async def test_shared_families_stay_shared():
    check.section("11: correctly-shared families are NOT restricted")
    async with boot() as nova:
        from core.turn_identity import active_turn
        with active_turn(OWNER()):
            await nova.memory.world_learn("Paris", "is", "the capital of France",
                                          source="https://example.org")
        for label, who in (("guest", ALICE()), ("unknown", UNKNOWN())):
            r = await call(nova, who, "world.recall", {"subject": "Paris"})
            check(r.get("ok") and r.get("known"), f"{label}: world.recall works")
            r = await call(nova, who, "world.learn",
                           {"subject": "Berlin", "predicate": "is",
                            "object": "a city", "source": "https://example.org"})
            check(r.get("ok"), f"{label}: world.learn works")
            r = await call(nova, who, "experiment.record",
                           {"name": f"EXP-{label}", "hypothesis": "h"})
            check(r.get("ok"),
                  f"{label}: experiments are Nova's own A/B tests, not personal data")
            r = await call(nova, who, "agents.roster", {})
            check(r.get("ok"), f"{label}: the specialist roster is system state")


# ── the boundary that must not move ──────────────────────────────────────────

async def test_permissions_unchanged():
    check.section("identity still changes no permission decision")
    import inspect

    from core.permissions import evaluate, tier_of
    from core.turn_identity import active_turn

    per_cap: dict[str, set] = {}
    for cap in ("some.destructive.capability", "goal.create", "skill.delete",
                "shell.exec", "memory.index_folder"):
        for i in (OWNER(), ident("known", "p-m", "Marcus", "owner"), ALICE(),
                  ident("known", "p-bob", "Bob"), UNKNOWN()):
            with active_turn(i):
                per_cap.setdefault(cap, set()).add((tier_of(cap),
                                                    evaluate(cap, mode="guarded")))
    check(all(len(v) == 1 for v in per_cap.values()),
          f"identical per capability across five identities ({per_cap})")
    check(not (set(inspect.signature(evaluate).parameters)
               & {"speaker", "identity", "role"}),
          "and evaluate() still takes no identity argument")


async def test_frontend_untouched():
    check.section("frontend still not wired for speaker identity")
    for f in ("frontend/src/App.jsx", "frontend/src/voice/recorder.ts"):
        p = REPO / f
        if not p.exists():
            continue
        src = p.read_text(encoding="utf-8", errors="replace")
        check("speaker=true" not in src and "voice_turn_id" not in src,
              f"{f} sends no speaker identity")


async def main():
    await test_every_tool_is_classified()
    await test_owner_private_tools_refuse_non_owners()
    await test_thoughts_writer_matches_reader()
    await test_plans_are_owner_only()
    await test_goals_create_no_rows_or_tasks_for_guests()
    await test_skills_are_owner_only()
    await test_document_index_is_owner_only()
    await test_research_registry_vs_public_findings()
    await test_agent_memory_classification()
    await test_society_consult_does_not_leak_agent_notes()
    await test_shared_families_stay_shared()
    await test_permissions_unchanged()
    await test_frontend_untouched()
    check.finish()


if __name__ == "__main__":
    run(main)
