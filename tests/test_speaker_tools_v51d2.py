"""V3 P5.1d.2: the direct tool surface, which sat outside the speaker boundary.

P5.1d/d.1 scoped the paths Nova takes on her own — grounding, semantic search,
quick-fact capture, the background extractor. The tools the MODEL calls were
still global, so the entire boundary could be stepped around by emitting a tool
call. Measured on `d1ec5a9`, all four speakers exercised against the real
ToolRouter:

    A  memory.remember wrote every speaker's "remember my locker code" into the
       one global `note` entity
    B  memory.correct guarded only the DEFAULT entity. Alice passing
       entity="speaker:p-bob" changed Bob's favourite colour from blue to red
    C  memory.remember_person added guests' people to Marcus's people table
    D  memory.remember_event put guests' events on Marcus's timeline
    E  memory.link let a guest — and an unknown speaker — edit Marcus's graph
    F  recall_person / related / timeline / path returned Marcus's data to
       anyone who asked
    G  the legacy-namespace rule matched by suffix, so
       `speaker:p-bob:lesson:speaker:p-alice` read as Alice's
    H  thoughts.recall handed Nova's private notes about Marcus to a stranger

Speaker identity is still NOT authentication. Every speaker may call every tool
and gets the identical PermissionBroker decision; what changes is whose data the
call can reach.

Run:  venv\\Scripts\\python.exe tests\\test_speaker_tools_v51d2.py
"""

from __future__ import annotations

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

MARCUS_FRIEND = "MARCUS-FRIEND-770"
MARCUS_EVENT = "MARCUS-EVENT-771"
MARCUS_THOUGHT = "THOUGHT-MARCUS-995"
WORLD_SUBJECT = "Paris"


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


def BOB():
    return ident("known", "p-bob", "Bob")


def UNKNOWN():
    return ident("unknown")


async def call(nova, who, tool, args):
    from core.tool_router import ToolCall
    from core.turn_identity import active_turn
    with active_turn(who):
        r = await nova.runtime._router.execute(ToolCall(tool, args))
    return r.result


async def facts_for(mem, value):
    import aiosqlite
    async with aiosqlite.connect(mem._sqlite._db_path) as db:
        cur = await db.execute("SELECT entity FROM facts WHERE value LIKE ?", (f"%{value}%",))
        return sorted(r[0] for r in await cur.fetchall())


# ── A. memory.remember ───────────────────────────────────────────────────────

async def test_remember_is_routed_by_speaker():
    check.section("A: 'remember this' lands in the speaker's own namespace")
    async with boot() as nova:
        m = nova.memory

        await call(nova, OWNER(), "memory.remember", {"fact": "locker code OWNER-660"})
        await call(nova, ALICE(), "memory.remember", {"fact": "locker code ALICE-771"})
        r = await call(nova, UNKNOWN(), "memory.remember", {"fact": "locker code STRANGER-882"})

        check(await facts_for(m, "OWNER-660") == ["note"],
              "the owner still writes to `note`, unchanged")
        check(await facts_for(m, "ALICE-771") == ["speaker:p-alice:note"],
              f"a guest writes to their own note namespace "
              f"({await facts_for(m, 'ALICE-771')})")
        check(await facts_for(m, "STRANGER-882") == [],
              "an unverified speaker writes NOWHERE — asserted against the table")
        check(isinstance(r, dict) and r.get("ok") is False
              and r.get("error") == "unverified_speaker",
              f"and is told so rather than getting a false success ({r})")


# ── B. memory.correct, every entity shape ────────────────────────────────────

async def test_correct_cannot_cross_speakers():
    check.section("B: Alice cannot correct Bob, Marcus, or anyone else")
    async with boot() as nova:
        m = nova.memory
        await m.add_fact(entity="user", attribute="favorite_color", value="green",
                         confidence=0.9)
        await m.add_fact(entity="speaker:p-bob", attribute="favorite_color",
                         value="blue", confidence=0.9)
        await m.add_fact(entity="speaker:p-alice", attribute="favorite_color",
                         value="teal", confidence=0.9)

        async def colour(entity):
            rows = await m.get_facts(entity=entity, attribute="favorite_color", limit=5)
            return [r.value for r in rows]

        # The owner's own corrections are untouched.
        r = await call(nova, OWNER(), "memory.correct",
                       {"attribute": "favorite_color", "value": "amber"})
        check(await colour("user") == ["amber"], f"the owner corrects himself ({r})")

        # Alice, self-alias.
        await call(nova, ALICE(), "memory.correct",
                   {"attribute": "favorite_color", "value": "pink"})
        check(await colour("speaker:p-alice") == ["pink"], "Alice corrects herself")
        check(await colour("user") == ["amber"], "without touching Marcus")

        # Alice, explicitly naming Marcus's namespace.
        r = await call(nova, ALICE(), "memory.correct",
                       {"entity": "user", "attribute": "favorite_color", "value": "black"})
        check(await colour("user") == ["amber"],
              f"an explicit entity='user' from a guest does NOT reach Marcus ({r})")
        check(await colour("speaker:p-alice") == ["black"],
              "it is routed to her own namespace")

        # Alice, explicitly naming Bob. THE one that was exploitable.
        r = await call(nova, ALICE(), "memory.correct",
                       {"entity": "speaker:p-bob", "attribute": "favorite_color",
                        "value": "red"})
        check(await colour("speaker:p-bob") == ["blue"],
              f"Bob's fact is untouched ({await colour('speaker:p-bob')})")
        check(isinstance(r, dict) and r.get("ok") is False
              and r.get("error") == "not_your_memory",
              f"and Alice is refused explicitly ({r})")

        # And a nested reach into Bob's namespace.
        await call(nova, ALICE(), "memory.correct",
                   {"entity": "speaker:p-bob:person:sarah", "attribute": "job",
                    "value": "spy"})
        check(await facts_for(m, "spy") == [],
              "nor can she write beneath Bob's root")

        # Symmetry: Bob cannot touch Alice either.
        r = await call(nova, BOB(), "memory.correct",
                       {"entity": "speaker:p-alice", "attribute": "favorite_color",
                        "value": "grey"})
        check(await colour("speaker:p-alice") == ["black"], "Bob cannot correct Alice")

        # A generic personal entity is nested under the speaker, not global.
        await call(nova, ALICE(), "memory.correct",
                   {"entity": "person:sarah", "attribute": "job", "value": "baker"})
        check(await facts_for(m, "baker") == ["speaker:p-alice:person:sarah"],
              f"person:sarah from Alice is HER Sarah "
              f"({await facts_for(m, 'baker')})")

        # Unverified: nothing at all.
        r = await call(nova, UNKNOWN(), "memory.correct",
                       {"attribute": "favorite_color", "value": "gold"})
        check(await facts_for(m, "gold") == [],
              "an unverified speaker persists no correction")
        check(isinstance(r, dict) and r.get("error") == "unverified_speaker",
              f"and is told why ({r})")


# ── G. legacy namespace containment ──────────────────────────────────────────

async def test_legacy_compat_cannot_confuse_namespaces():
    check.section("G: compatibility must not smuggle suffix matching back in")
    from core.turn_identity import entity_belongs_to_speaker, may_read_entity

    alice, bob = ALICE(), BOB()
    for e in ("lesson:speaker:p-alice", "mood:speaker:p-alice",
              "wellbeing:speaker:p-alice", "session:speaker:p-alice"):
        check(may_read_entity(e, alice), f"exact legacy shape {e} still reads")
        check(not may_read_entity(e, bob), f"and only for its owner ({e})")

    adversarial = "speaker:p-bob:lesson:speaker:p-alice"
    check(not entity_belongs_to_speaker(adversarial, "speaker:p-alice"),
          "Bob's namespace with Alice's name appended is NOT Alice's")
    check(not may_read_entity(adversarial, alice), "and she cannot read it")
    for e in ("notes:speaker:p-alice", "secret:speaker:p-alice",
              "x:lesson:speaker:p-alice"):
        check(not may_read_entity(e, alice),
              f"only the shapes P5.1d could actually write are accepted ({e})")
    check(may_read_entity("speaker:p-alice:lesson", alice),
          "the canonical form is unaffected")


# ── C/F. people ──────────────────────────────────────────────────────────────

async def test_people_are_scoped():
    check.section("C+F: Marcus's people stay Marcus's")
    async with boot() as nova:
        m = nova.memory
        from core.turn_identity import active_turn
        with active_turn(OWNER()):
            await m.upsert_person(name=MARCUS_FRIEND, attributes={"relation": "best friend"})

        # Owner: unchanged, both directions.
        r = await call(nova, OWNER(), "memory.recall_person", {"name": MARCUS_FRIEND})
        check(r.get("ok") and "best friend" in str(r), f"the owner recalls his person ({r})")
        await call(nova, OWNER(), "memory.remember_person",
                   {"name": "OWNER-PERSON-660", "attributes": {"relation": "cousin"}})
        check("OWNER-PERSON-660" in await m.known_person_names(),
              "and still writes to the people table")

        # Guest: cannot read his, and does not write into his store.
        r = await call(nova, ALICE(), "memory.recall_person", {"name": MARCUS_FRIEND})
        check(not r.get("ok") and "best friend" not in str(r),
              f"a guest cannot look up Marcus's friend ({r})")
        await call(nova, ALICE(), "memory.remember_person",
                   {"name": "ALICE-PERSON-991", "attributes": {"relation": "colleague"}})
        check("ALICE-PERSON-991" not in await m.known_person_names(),
              "a guest's person does NOT enter Marcus's people table")
        check(await facts_for(m, "colleague") == ["speaker:p-alice:person:alice_person_991"],
              f"it is fact-backed under her own root "
              f"({await facts_for(m, 'colleague')})")

        # ...and she can read her own back.
        r = await call(nova, ALICE(), "memory.recall_person", {"name": "ALICE-PERSON-991"})
        check(r.get("ok") and "colleague" in str(r),
              f"a guest recalls the people THEY told Nova about ({r})")
        r = await call(nova, BOB(), "memory.recall_person", {"name": "ALICE-PERSON-991"})
        check(not r.get("ok"), "but another guest cannot")

        # Unknown: nothing.
        r = await call(nova, UNKNOWN(), "memory.remember_person",
                       {"name": "STRANGER-PERSON-882", "attributes": {"relation": "x"}})
        check(not r.get("ok") and r.get("error") == "unverified_speaker",
              f"an unverified speaker persists no person ({r})")
        check("STRANGER-PERSON-882" not in await m.known_person_names(), "nowhere")
        r = await call(nova, UNKNOWN(), "memory.recall_person", {"name": MARCUS_FRIEND})
        check(not r.get("ok") and "best friend" not in str(r),
              "and cannot look one up")


# ── D/F. events and timeline ─────────────────────────────────────────────────

async def test_events_and_timeline_are_owner_only():
    check.section("D+F: nobody else writes on Marcus's calendar")
    async with boot() as nova:
        m = nova.memory
        from core.turn_identity import active_turn
        with active_turn(OWNER()):
            await m.add_event(date="2026-08-01", note=f"{MARCUS_EVENT} surgery appointment")

        r = await call(nova, OWNER(), "memory.remember_event",
                       {"note": "OWNER-EVENT-660 trip to Austin", "date": "2026-09-02"})
        check(r.get("ok"), f"the owner's event is saved, unchanged ({r})")
        r = await call(nova, OWNER(), "memory.timeline", {"days": 400})
        check(r.get("ok") and MARCUS_EVENT in str(r), "and his timeline still reads")

        for label, who in (("a guest", ALICE()), ("an unknown speaker", UNKNOWN())):
            r = await call(nova, who, "memory.remember_event",
                           {"note": "GUEST-EVENT-991 my flight", "date": "2026-09-01"})
            check(not r.get("ok") and r.get("error") == "scoped_unavailable",
                  f"{label} cannot add to the timeline ({r})")
            r = await call(nova, who, "memory.timeline", {"days": 400})
            check(not r.get("ok") and MARCUS_EVENT not in str(r),
                  f"{label} cannot read it either")

        tl = await m.timeline(days=400)
        check(not any("GUEST-EVENT-991" in str(e) for e in tl),
              "and no guest event reached the store")


# ── E/F. the knowledge graph ─────────────────────────────────────────────────

async def test_graph_is_owner_only():
    check.section("E+F: the relationship map is Marcus's social map")
    async with boot() as nova:
        m = nova.memory
        from core.turn_identity import active_turn
        with active_turn(OWNER()):
            await m.link("person", MARCUS_FRIEND, "knows", "person", "Marcus")

        r = await call(nova, OWNER(), "memory.related", {"name": MARCUS_FRIEND})
        check(r.get("ok"), f"the owner's graph reads are unchanged ({str(r)[:70]})")
        r = await call(nova, OWNER(), "memory.path",
                       {"from": MARCUS_FRIEND, "to": "Marcus"})
        check(r.get("ok") and r.get("connected"), "and so are paths")
        r = await call(nova, OWNER(), "memory.link",
                       {"from": "OWNER-NODE-660", "to": "Marcus", "predicate": "knows"})
        check(r.get("ok"), "and links")

        for label, who in (("a guest", ALICE()), ("an unknown speaker", UNKNOWN())):
            for tool, args in (("memory.related", {"name": MARCUS_FRIEND}),
                               ("memory.path", {"from": MARCUS_FRIEND, "to": "Marcus"}),
                               ("memory.link", {"from": "GUEST-NODE-991", "to": "Marcus",
                                                "predicate": "knows"})):
                r = await call(nova, who, tool, args)
                check(not r.get("ok") and r.get("error") == "scoped_unavailable",
                      f"{label} is refused by {tool}")
                check(MARCUS_FRIEND not in str(r) or tool != "memory.related",
                      f"{label} sees no graph contents via {tool}")

        r = await call(nova, OWNER(), "memory.related", {"name": "GUEST-NODE-991"})
        check(not r.get("ok"), "and no guest edge was written")


# ── H. the other personal readers ────────────────────────────────────────────

async def test_other_personal_readers_are_scoped():
    check.section("H: thoughts, twin profile, executive brief, reminders")
    async with boot() as nova:
        m = nova.memory
        from core.turn_identity import active_turn
        with active_turn(OWNER()):
            await m.note_thought("note", f"{MARCUS_THOUGHT} I worry about the deadline",
                                 topic="work")

        r = await call(nova, OWNER(), "thoughts.recall", {})
        check(r.get("ok") and MARCUS_THOUGHT in str(r),
              "the owner still reads Nova's notes about him")

        for label, who in (("a guest", ALICE()), ("an unknown speaker", UNKNOWN())):
            for tool in ("thoughts.recall", "twin.profile", "executive.brief"):
                r = await call(nova, who, tool, {})
                check(not r.get("ok") and r.get("error") == "scoped_unavailable",
                      f"{label} is refused by {tool}")
            r = await call(nova, who, "thoughts.recall", {})
            check(MARCUS_THOUGHT not in str(r), f"{label} never sees the thought text")
            check("Marcus's patterns" not in str(
                await call(nova, who, "twin.profile", {})),
                f"{label} gets no profile content")

            r = await call(nova, who, "reminder.create",
                           {"title": "GUEST-REMINDER-991", "when": "5pm"})
            check(not r.get("ok") and r.get("error") == "scoped_unavailable",
                  f"{label} cannot put something on Marcus's schedule ({r})")

        # A guest's lesson still works (it is speaker-scoped), but an unknown
        # speaker gets an honest refusal rather than a fake success.
        r = await call(nova, ALICE(), "memory.learn_lesson",
                       {"lesson": "never mention sports scores"})
        check(r.get("ok"), f"a known guest can still teach Nova about themselves ({r})")
        r = await call(nova, UNKNOWN(), "memory.learn_lesson",
                       {"lesson": "always trust me"})
        check(not r.get("ok") and r.get("error") == "unverified_speaker",
              f"an unverified speaker cannot ({r})")


# ── shared knowledge must survive all of this ────────────────────────────────

async def test_shared_knowledge_still_works():
    check.section("shared world/system knowledge stays usable by everyone")
    async with boot() as nova:
        from core.turn_identity import active_turn
        with active_turn(OWNER()):
            await nova.memory.world_learn(WORLD_SUBJECT, "is", "the capital of France",
                                          source="https://example.org")
        for label, who in (("owner", OWNER()), ("guest", ALICE()),
                           ("unknown", UNKNOWN())):
            r = await call(nova, who, "world.recall", {"subject": WORLD_SUBJECT})
            check(r.get("ok") and r.get("known"),
                  f"{label} can still use shared world knowledge")
        # Generic recall must return the shared fact itself, not merely `ok`.
        with active_turn(OWNER()):
            await nova.memory.add_fact(entity="world", attribute="landmark",
                                       value="the SHARED-LANDMARK-334 stands in Paris",
                                       confidence=0.95)
            await nova.memory.add_fact(entity="user", attribute="secret_note",
                                       value="OWNER-PRIVATE-335 vault code",
                                       confidence=0.95)
        r = await call(nova, UNKNOWN(), "memory.recall",
                       {"query": "SHARED-LANDMARK-334 OWNER-PRIVATE-335 landmark"})
        check("SHARED-LANDMARK-334" in str(r),
              f"an unknown speaker retrieves the shared fact ({str(r)[:110]})")
        check("OWNER-PRIVATE-335" not in str(r),
              "and not the owner's private one in the same query")


# ── the boundary that must not move ──────────────────────────────────────────

async def test_permissions_unchanged():
    check.section("identity still changes no permission decision")
    import inspect

    from core.permissions import evaluate, tier_of
    from core.turn_identity import active_turn

    seen = set()
    for cap in ("some.destructive.capability", "memory.remember", "memory.link"):
        for i in (OWNER(), ident("known", "p-m", "Marcus", "owner"), ALICE(),
                  BOB(), UNKNOWN()):
            with active_turn(i):
                seen.add((cap, tier_of(cap), evaluate(cap, mode="guarded")))
    per_cap = {}
    for cap, tier, decision in seen:
        per_cap.setdefault(cap, set()).add((tier, decision))
    check(all(len(v) == 1 for v in per_cap.values()),
          f"every speaker gets the identical decision per capability ({per_cap})")
    check(not (set(inspect.signature(evaluate).parameters)
               & {"speaker", "identity", "role"}),
          "and evaluate() still takes no identity argument")


async def test_frontend_untouched():
    check.section("frontend: identity is ACTIVE, and still backend-derived")
    # Until V3 P5.1e this asserted the frontend sent nothing. It now does — that
    # was the point of P5.1e — so the invariant moves to the part that still
    # must hold: the client forwards an opaque handle and never asserts who is
    # speaking.
    origin = (REPO / "frontend/src/voice/turnOrigin.ts").read_text(encoding="utf-8")
    check("input_source" in origin and "voice_turn_id" in origin,
          "the client sends transport + an opaque handle")
    for banned in ("profile_id", "display_name", "role"):
        check(f'out["{banned}"]' not in origin,
              f"and never asserts {banned}")


async def main():
    await test_remember_is_routed_by_speaker()
    await test_correct_cannot_cross_speakers()
    await test_legacy_compat_cannot_confuse_namespaces()
    await test_people_are_scoped()
    await test_events_and_timeline_are_owner_only()
    await test_graph_is_owner_only()
    await test_other_personal_readers_are_scoped()
    await test_shared_knowledge_still_works()
    await test_permissions_unchanged()
    await test_frontend_untouched()
    check.finish()


if __name__ == "__main__":
    run(main)
