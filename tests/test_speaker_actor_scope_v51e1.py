"""V3 P5.1e.1: off-turn attribution, and actor vs privacy owner.

Two defects in P5.1e, both mine, both reproduced on `edc458a`.

OFF-TURN ATTRIBUTION. `current_identity()` has a ContextVar default of typed
Marcus, so `_publisher_identity()` — whose docstring claimed "or None off the
turn path" — returned him for every background publish. Measured: an off-turn
`project.completed` persisted as `speaker_label="Marcus"` next to a summary
reading "Nova finished …". The provenance contradicted itself in one row.

ACTOR IS NOT PRIVACY OWNER. P5.1e used one field for both, which is unsafe in
both directions. A background build failure on Marcus's private project has
actor `system` and owner `user`:

    mark it `system`  -> accurate actor, and a guest can read his project
    mark it `user`    -> private, and it claims he ran the build

Neither is acceptable, so they are now two fields and neither lies. The privacy
of that row today was an accident of the first bug — correcting the actor
without splitting the concepts would have OPENED it.

Run:  venv\\Scripts\\python.exe tests\\test_speaker_actor_scope_v51e1.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")
os.environ.setdefault("NOVA_REPO_ROOT", str(REPO))

from harness import Checks, boot, run  # noqa: E402

check = Checks()

PROJECT_SECRET = "OWNER-PROJECT-SECRET-881"
FAILURE_SECRET = "OWNER-FAILURE-SECRET-882"
ALICE_SECRET = "ALICE-CORRECTION-661"


class _Prof:
    role = "guest"


def ALICE():
    from core.speaker.matcher import SpeakerMatch
    from core.turn_identity import TurnIdentity
    return TurnIdentity.from_match(
        SpeakerMatch(status="known", profile_id="p-alice", display_name="Alice",
                     attempted=True), profile=_Prof())


def UNKNOWN():
    from core.speaker.matcher import SpeakerMatch
    from core.turn_identity import TurnIdentity
    return TurnIdentity.from_match(SpeakerMatch(status="unknown", attempted=True))


def OWNER():
    from core.turn_identity import TurnIdentity
    return TurnIdentity.typed()


async def rows(db_path):
    import aiosqlite
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, kind, summary, actor_entity, actor_label, privacy_scope, "
            "speaker_entity FROM episodes")
        return [dict(r) for r in await cur.fetchall()]


# ── §2. active-turn detection ────────────────────────────────────────────────

async def test_active_turn_detection():
    check.section("2: 'no turn' is now distinguishable from 'typed Marcus'")
    from core.turn_identity import (active_turn, current_identity,
                                    current_identity_or_none, has_active_turn)

    check(current_identity().is_owner,
          "outside a turn, current_identity() is STILL typed owner — unchanged")
    check(current_identity_or_none() is None,
          "but current_identity_or_none() says nobody is here")
    check(not has_active_turn(), "and has_active_turn() is False")

    with active_turn(OWNER()):
        check(current_identity().is_owner, "typed active: current_identity() unchanged")
        check(current_identity_or_none() is not None,
              "typed active: an explicit typed turn IS a turn")
        check(has_active_turn(), "and is flagged as one")

    with active_turn(ALICE()):
        got = current_identity_or_none()
        check(got is not None and got.profile_id == "p-alice", "Alice active: Alice")

    check(current_identity_or_none() is None, "after reset: nobody again")
    check(current_identity().is_owner, "and the legacy default is restored")

    # Nesting and concurrency: the marker must reset exactly like the identity.
    with active_turn(ALICE()):
        with active_turn(UNKNOWN()):
            check(current_identity_or_none().is_unverified, "nested scope applies")
        check(current_identity_or_none().profile_id == "p-alice", "and unwinds")
    check(current_identity_or_none() is None, "fully unwound")

    async def concurrent(ident, expect):
        with active_turn(ident):
            await asyncio.sleep(0)
            got = current_identity_or_none()
            return got is not None and got.memory_entity == expect

    results = await asyncio.gather(
        concurrent(ALICE(), "speaker:p-alice"),
        concurrent(OWNER(), "user"),
        concurrent(UNKNOWN(), None),
    )
    check(all(results), f"concurrent turns keep their own marker ({results})")
    check(current_identity_or_none() is None, "and none leaks out")


# ── §3. bus snapshotting ─────────────────────────────────────────────────────

async def test_bus_snapshot_is_honest():
    check.section("3: the bus snapshots a real speaker, or nobody")
    from core.event_bus import BUS
    from core.turn_identity import active_turn

    async def publish_and_capture(scope):
        q = BUS.subscribe()
        try:
            if scope is None:
                BUS.publish("project.completed", {"project": "x"})
            else:
                with active_turn(scope):
                    BUS.publish("project.completed", {"project": "x"})
            await asyncio.sleep(0.02)
            got = []
            while not q.empty():
                got.append(q.get_nowait())
            return got[-1] if got else None
        finally:
            BUS.unsubscribe(q)

    ev = await publish_and_capture(ALICE())
    check(ev and ev.identity and ev.identity.profile_id == "p-alice",
          "a guest's publish carries the guest")
    ev = await publish_and_capture(OWNER())
    check(ev and ev.identity is not None and ev.identity.is_owner,
          "an owner turn's publish carries the owner")
    ev = await publish_and_capture(None)
    check(ev is not None and ev.identity is None,
          f"an OFF-TURN publish carries None, not Marcus "
          f"({None if ev is None else ev.identity})")
    check(ev is not None and "identity" not in ev.to_dict(),
          "and identity stays out of the SSE/debug representation")


# ── §4/§6/§11. the real path, and the core regression ────────────────────────

async def test_real_bus_path_actor_and_privacy():
    check.section("11: BUS -> promoter -> worker -> SQLite, four event shapes")
    from core.event_bus import BUS
    from core.turn_identity import active_turn

    async with boot() as nova:
        rt = nova.runtime

        with active_turn(ALICE()):
            BUS.publish("memory.corrected", {"entity": "speaker:p-alice",
                                             "attribute": "favorite_color",
                                             "was": "blue", "now": ALICE_SECRET})
        with active_turn(OWNER()):
            BUS.publish("memory.corrected", {"entity": "user", "attribute": "hobby",
                                             "was": "chess", "now": "running"})
        # Entirely off-turn, exactly as a background build or timer would.
        BUS.publish("project.completed", {"project": PROJECT_SECRET, "name": PROJECT_SECRET})
        BUS.publish("project.error", {"project": PROJECT_SECRET,
                                      "error": f"{FAILURE_SECRET} build failed"})

        await asyncio.sleep(1.5)
        await rt._promoter.stop()
        if rt._episodic_q is not None:
            await asyncio.wait_for(rt._episodic_q.join(), timeout=30)

        got = await rows(nova.memory._sqlite._db_path)
        by = {r["id"]: r for r in got}
        check(len(got) >= 4, f"all four became episodes ({len(got)})")

        alice = next((r for r in got if ALICE_SECRET in r["summary"]), None)
        check(alice and alice["actor_entity"] == "speaker:p-alice",
              f"A: Alice's correction — actor is Alice ({alice})")
        check(alice and alice["privacy_scope"] == "speaker:p-alice",
              "A: and privacy is Alice's")
        check(alice and alice["summary"].startswith("Alice corrected"),
              f"A: narrated as hers ({alice['summary'] if alice else None})")
        check(alice and "speaker:p-alice favorite_color" not in alice["summary"],
              f"10: and the subject reads cleanly ({alice['summary'] if alice else None})")

        owner = next((r for r in got if "hobby" in r["summary"]), None)
        check(owner and owner["actor_entity"] == "user"
              and owner["privacy_scope"] == "user",
              f"B: Marcus's correction is his, both fields ({owner})")
        check(owner and owner["summary"].startswith("Marcus corrected"),
              "B: and still narrated as his")

        proj = next((r for r in got if r["kind"] == "project_event"), None)
        check(proj and proj["actor_entity"] == "system",
              f"C: the off-turn project event's ACTOR is system ({proj})")
        check(proj and proj["privacy_scope"] == "user",
              "C: and its PRIVACY OWNER is Marcus — the core regression")
        check(proj and "Marcus" not in proj["summary"],
              f"C: wording does not claim he did it ({proj['summary'] if proj else None})")

        fail = next((r for r in got if r["kind"] == "failure"), None)
        check(fail and fail["actor_entity"] == "system"
              and fail["privacy_scope"] == "user",
              f"D: the off-turn failure is the same shape ({fail})")

        # THE assertion this whole phase exists for.
        check(any(r["actor_entity"] == "system" and r["privacy_scope"] == "user"
                  for r in got),
              "a durable row can be actor=system AND privacy=user simultaneously")


# ── §8. the read matrix ──────────────────────────────────────────────────────

async def test_read_matrix_uses_privacy_not_actor():
    check.section("8: authorisation reads privacy_scope, never the actor")
    from memory.episodes import Episode, EpisodicStore
    from memory.episodic_recall import retrieve
    from core.turn_identity import active_turn

    async with boot() as nova:
        store = EpisodicStore(Path(nova.memory._sqlite._db_path))
        await store.record_episode(Episode(
            id="e-sysowner", kind="failure",
            summary=f"Building {PROJECT_SECRET} failed",
            entities=[PROJECT_SECRET],
            actor_entity="system", actor_label="Nova", privacy_scope="user"))
        await store.record_episode(Episode(
            id="e-alice", kind="selection", summary=f"Alice chose {ALICE_SECRET}",
            entities=[ALICE_SECRET],
            actor_entity="speaker:p-alice", actor_label="Alice",
            privacy_scope="speaker:p-alice"))
        await store.record_episode(Episode(
            id="e-shared", kind="capability",
            summary="The SHARED-CAPABILITY-990 became available",
            entities=["SHARED-CAPABILITY-990"],
            actor_entity="system", actor_label="Nova", privacy_scope="system"))

        async def recall(ident, q):
            with active_turn(ident):
                return (await retrieve(store, q, limit=10)).prompt_text

        query = f"{PROJECT_SECRET} {ALICE_SECRET} SHARED-CAPABILITY-990"
        o = await recall(OWNER(), query)
        for s in (PROJECT_SECRET, ALICE_SECRET, "SHARED-CAPABILITY-990"):
            check(s in o, f"owner reads everything ({s})")

        a = await recall(ALICE(), query)
        check(ALICE_SECRET in a, "Alice reads her own")
        check(PROJECT_SECRET not in a,
              "and NOT the system-actor episode that belongs to Marcus")
        check("SHARED-CAPABILITY-990" in a, "but does read an explicitly shared one")

        u = await recall(UNKNOWN(), query)
        check(PROJECT_SECRET not in u and ALICE_SECRET not in u,
              "an unknown speaker reads nothing private")
        check("SHARED-CAPABILITY-990" in u, "only the explicitly shared one")


async def test_denied_system_episode_has_no_side_effects():
    check.section("11: a denied owner-private system episode leaves no trace")
    import aiosqlite

    from memory.episodes import Episode, EpisodicStore
    from memory.episodic_recall import retrieve
    from core.turn_identity import active_turn

    async with boot() as nova:
        store = EpisodicStore(Path(nova.memory._sqlite._db_path))
        ref = store.cold.put(f"full build log for {PROJECT_SECRET}")
        await store.record_episode(Episode(
            id="e-cold-sys", kind="failure",
            summary=f"Building {PROJECT_SECRET} failed",
            entities=[PROJECT_SECRET], actor_entity="system", actor_label="Nova",
            privacy_scope="user", provenance={"cold_ref": ref}))

        reads = {"n": 0}
        orig = store.cold.get
        store.cold.get = lambda r: (reads.__setitem__("n", reads["n"] + 1), orig(r))[1]  # type: ignore[assignment]

        async def counters():
            async with aiosqlite.connect(str(nova.memory._sqlite._db_path)) as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute(
                    "SELECT access_count, last_accessed_at FROM episodes WHERE id=?",
                    ("e-cold-sys",))
                r = await cur.fetchone()
                return (r["access_count"], r["last_accessed_at"])

        n0, t0 = await counters()
        for who in (ALICE(), UNKNOWN()):
            with active_turn(who):
                res = await retrieve(store, PROJECT_SECRET, limit=5, hydrate=True)
            check(PROJECT_SECRET not in res.prompt_text, "the episode is withheld")
        n1, t1 = await counters()
        check(n1 == n0 and t1 == t0,
              f"access_count and last_accessed_at untouched ({n0}->{n1})")
        check(reads["n"] == 0,
              f"and his build log was never pulled off disk ({reads['n']} cold reads)")

        with active_turn(OWNER()):
            res = await retrieve(store, PROJECT_SECRET, limit=5, hydrate=True)
        check(PROJECT_SECRET in res.prompt_text, "while the owner still reads it")
        check(reads["n"] >= 1, "and still hydrates his evidence")


# ── §7. honesty about what is actually shared ────────────────────────────────

async def test_child_tasks_inherit_turn_context():
    check.section("8A: a child task INHERITS the turn — this is why kind must win")
    from core.turn_identity import active_turn, current_identity_or_none

    seen = {}

    async def child():
        await asyncio.sleep(0)          # force a real suspension point
        i = current_identity_or_none()
        seen["who"] = None if i is None else i.memory_entity

    with active_turn(ALICE()):
        task = asyncio.create_task(child())
    await task
    check(seen["who"] == "speaker:p-alice",
          f"a task created inside Alice's turn still sees Alice after awaiting "
          f"({seen['who']})")
    check(current_identity_or_none() is None, "while the parent scope has exited")
    # Not a bug in itself — other background work legitimately wants request
    # context. The bug was reading it as episode OWNERSHIP.


async def test_system_kind_overrides_inherited_identity():
    check.section("9: every _SYSTEM_KINDS episode is Nova's, whoever's turn it was")
    from core.workers.episodic_ingest import (_SHARED_SYSTEM_KINDS, _SYSTEM_KINDS,
                                              _actor_fields, _privacy_for)

    check(_SHARED_SYSTEM_KINDS <= _SYSTEM_KINDS,
          "a shared kind cannot bypass the system classification")

    for kind in sorted(_SYSTEM_KINDS):
        for label, ident in (("Alice's turn", ALICE()), ("Marcus's turn", OWNER()),
                             ("an unknown turn", UNKNOWN()), ("no turn", None)):
            actor, actor_label, _src = _actor_fields(ident, kind)
            check(actor == "system" and actor_label == "Nova",
                  f"{kind} under {label}: actor is Nova")
            check(_privacy_for(ident, kind) == "user",
                  f"{kind} under {label}: privacy is owner-private")

    # And the override is NARROW — human kinds still follow the human.
    for kind in ("selection", "memory_corrected", "tool_result", ""):
        check(_privacy_for(ALICE(), kind) == "speaker:p-alice",
              f"a {kind or '(blank)'} episode under Alice is still hers")
        check(_actor_fields(ALICE(), kind)[0] == "speaker:p-alice",
              f"and she is still its actor ({kind or 'blank'})")
    check(_privacy_for(UNKNOWN(), "selection") == "unverified",
          "an unattributed human episode stays unverified")


async def test_real_project_builder_under_a_guest_turn():
    check.section("8B-E: the REAL ProjectBuilder, started inside Alice's turn")
    import aiosqlite

    from core.event_bus import BUS
    from core.turn_identity import active_turn, current_identity_or_none

    PROJ = "alice-started-secret-991"

    async with boot() as nova:
        rt = nova.runtime
        pb = getattr(rt, "_project_builder", None)
        check(pb is not None, "the production ProjectBuilder is reachable")
        if pb is None:
            return

        child_saw = {}

        async def fake_build(slug, brief):
            # Stands in for the expensive LLM/file work. Everything that matters
            # here — create_task, the inherited context, the published events —
            # is the real production shape.
            await asyncio.sleep(0)
            i = current_identity_or_none()
            child_saw["who"] = None if i is None else i.memory_entity
            BUS.publish("project.completed", {"project": slug, "name": slug})
            BUS.publish("project.error", {"project": slug, "error": f"{slug} build failed"})

        pb._build = fake_build
        with active_turn(ALICE()):
            await pb.start(name=PROJ, brief="a private thing")
        await asyncio.sleep(1.5)
        await rt._promoter.stop()
        if rt._episodic_q is not None:
            await asyncio.wait_for(rt._episodic_q.join(), timeout=30)

        check(child_saw.get("who") == "speaker:p-alice",
              f"the build child really did inherit Alice ({child_saw.get('who')})")

        got = await rows(nova.memory._sqlite._db_path)
        proj = [r for r in got if r["kind"] == "project_event"]
        fails = [r for r in got if r["kind"] == "failure"]
        check(len(proj) >= 2, f"project.started and .completed both promoted ({len(proj)})")
        check(len(fails) >= 1, f"and the error became a failure episode ({len(fails)})")

        for r in proj + fails:
            check(r["actor_entity"] == "system" and r["actor_label"] == "Nova",
                  f"{r['kind']}: actor is Nova, not Alice ({r['actor_entity']})")
            check(r["privacy_scope"] == "user",
                  f"{r['kind']}: privacy is owner-private ({r['privacy_scope']})")
        check(not any("Alice" in r["summary"] for r in proj + fails),
              "and no summary claims she did it")

        # E: recall matrix + zero side effects on the denied path.
        from memory.episodes import EpisodicStore
        from memory.episodic_recall import retrieve
        store = EpisodicStore(Path(nova.memory._sqlite._db_path))
        reads = {"n": 0}
        orig = store.cold.get
        store.cold.get = lambda r: (reads.__setitem__("n", reads["n"] + 1), orig(r))[1]  # type: ignore[assignment]

        async def counters():
            async with aiosqlite.connect(str(nova.memory._sqlite._db_path)) as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute(
                    "SELECT SUM(access_count) c, MAX(COALESCE(last_accessed_at,'')) t "
                    "FROM episodes WHERE kind IN ('project_event','failure')")
                r = await cur.fetchone()
                return (r["c"] or 0, r["t"] or "")

        n0, t0 = await counters()
        for label, who in (("Alice", ALICE()), ("an unknown speaker", UNKNOWN())):
            with active_turn(who):
                res = await retrieve(store, PROJ, limit=10, hydrate=True)
            check(PROJ not in res.prompt_text,
                  f"{label} cannot retrieve the project she started")
        n1, t1 = await counters()
        check(n1 == n0 and t1 == t0,
              f"and the denied reads changed no counters ({n0}->{n1})")
        check(reads["n"] == 0, f"and hydrated nothing ({reads['n']} cold reads)")

        with active_turn(OWNER()):
            res = await retrieve(store, PROJ, limit=10)
        check(PROJ in res.prompt_text, "while the owner still retrieves it")


async def test_shared_system_scope_is_empty_by_design():
    check.section("7: no current producer emits genuinely shared system history")
    from core.workers.episodic_ingest import (_SHARED_SYSTEM_KINDS, _privacy_for,
                                              SHARED_SCOPE)

    check(_SHARED_SYSTEM_KINDS == frozenset(),
          f"the shared-system list is empty, on purpose ({sorted(_SHARED_SYSTEM_KINDS)})")
    for kind in ("project_event", "failure", "project_milestone", "tool_result", ""):
        check(_privacy_for(None, kind) == "user",
              f"an off-turn {kind or '(blank)'} episode is owner-private")
    check(SHARED_SCOPE == "system",
          "the scope exists and is reserved, so a future public event has a home")


# ── §13. the boundary that must not move ─────────────────────────────────────

async def test_permissions_unchanged():
    check.section("13: identity still changes no permission decision")
    import inspect

    from core.permissions import evaluate, tier_of
    from core.turn_identity import active_turn

    per_cap: dict[str, set] = {}
    for cap in ("some.destructive.capability", "memory.remember", "shell.exec"):
        for i in (OWNER(), ALICE(), UNKNOWN()):
            with active_turn(i):
                per_cap.setdefault(cap, set()).add((tier_of(cap),
                                                    evaluate(cap, mode="guarded")))
        # And off-turn, which is now a distinct state.
        per_cap.setdefault(cap, set()).add((tier_of(cap), evaluate(cap, mode="guarded")))
    check(all(len(v) == 1 for v in per_cap.values()),
          f"identical, including off-turn ({per_cap})")
    check(not (set(inspect.signature(evaluate).parameters)
               & {"speaker", "identity", "role"}),
          "and evaluate() still takes no identity argument")


async def main():
    await test_active_turn_detection()
    await test_bus_snapshot_is_honest()
    await test_real_bus_path_actor_and_privacy()
    await test_read_matrix_uses_privacy_not_actor()
    await test_denied_system_episode_has_no_side_effects()
    await test_child_tasks_inherit_turn_context()
    await test_system_kind_overrides_inherited_identity()
    await test_real_project_builder_under_a_guest_turn()
    await test_shared_system_scope_is_empty_by_design()
    await test_permissions_unchanged()
    check.finish()


if __name__ == "__main__":
    run(main)
