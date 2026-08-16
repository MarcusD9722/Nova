"""V3 P5.1e: live voice identity, end to end, and episodic speaker scope.

Everything before this phase built a boundary nothing reached. The frontend
requested no classification and sent no handle, so a live voice turn resolved as
typed/legacy owner — correct, and inert. This suite exercises the ACTUAL backend
contract the activated client now speaks, and closes P4's remaining gap:
episodes were labelled "Marcus chose …" unconditionally and retrieved with no
speaker boundary at all.

The two rules the whole phase rests on:

    a voice turn with NO handle is a VOICE turn, never typed
    a handle is single-use, and a retry that cannot redeem it falls to
    unverified — never back up to owner

Run:  venv\\Scripts\\python.exe tests\\test_speaker_live_v51e.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")
os.environ.setdefault("NOVA_REPO_ROOT", str(REPO))

from harness import Checks, boot, run  # noqa: E402

check = Checks()

OWNER_NAME = "OWNER-NAME-551"
OWNER_SPOUSE = "OWNER-SPOUSE-552"
OWNER_LESSON = "OWNER-LESSON-553"
OWNER_INSIGHT = "OWNER-INSIGHT-554"
OWNER_THOUGHT = "OWNER-THOUGHT-555"
OWNER_EPISODE = "OWNER-EPISODE-557"
ALICE_PROFILE = "ALICE-PROFILE-661"

OWNER_SENTINELS = (OWNER_NAME, OWNER_SPOUSE, OWNER_LESSON, OWNER_INSIGHT,
                   OWNER_THOUGHT, OWNER_EPISODE)


def match(status, pid=None, name=None):
    from core.speaker.matcher import SpeakerMatch
    return SpeakerMatch(status=status, profile_id=pid, display_name=name, attempted=True)


class _Prof:
    def __init__(self, role="guest"):
        self.role = role


def issue(status, pid=None, name=None):
    """Mint a REAL voice-turn handle the way /stt does."""
    from core.speaker.voice_turns import VOICE_TURNS
    return VOICE_TURNS.issue(match(status, pid, name))


async def resolve(handle, source="voice"):
    """Resolve through the REAL backend helper the chat endpoints call."""
    import backend.app as app
    return await app._resolve_turn_identity(handle, source)


# ── §12/§20. handle resolution, every failure mode ───────────────────────────

async def test_handle_resolution_matrix():
    check.section("12: every handle outcome, through the real backend helper")
    async with boot() as nova:
        # typed: unchanged legacy owner
        typed = await resolve(None, None)
        check(typed.memory_entity == "user" and typed.input_source == "typed",
              "typed stays owner, unchanged")

        # voice + valid handle
        owner_h = issue("known", "p-marcus", "Marcus")
        import backend.app as app
        from core.speaker.registry import SpeakerProfile  # noqa: F401
        ident = await resolve(owner_h)
        check(ident.input_source == "voice" and ident.backend_verified,
              "a valid handle produces a backend-verified voice turn")

        guest_h = issue("known", "p-alice", "Alice")
        g = await resolve(guest_h)
        check(g.speaker_status == "known" and g.profile_id == "p-alice",
              "a guest handle resolves to that guest")

        # THE rule: voice with no handle is unverified, NOT typed
        none_h = await resolve(None, "voice")
        check(none_h.input_source == "voice", "voice with no handle is still VOICE")
        check(none_h.is_unverified and none_h.memory_entity is None,
              "and is unverified — not typed, not owner")

        for label, bad in (("invented", "vt-does-not-exist"),
                           ("empty", ""),
                           ("garbage", "../../etc/passwd")):
            r = await resolve(bad, "voice")
            check(r.is_unverified and r.input_source == "voice",
                  f"a {label} handle is unverified voice")

        # replay: the fallback retry case, which must fall DOWN not up
        once = issue("known", "p-alice", "Alice")
        first = await resolve(once)
        second = await resolve(once)
        check(first.profile_id == "p-alice", "the first request redeems the handle")
        check(second.is_unverified, "the retry cannot redeem it again")
        check(second.memory_entity is None and second.input_source == "voice",
              "and falls to UNVERIFIED — never back up to owner")


# ── §9/§20. session speaker switch ───────────────────────────────────────────

async def test_session_commands_classify_independently():
    check.section("9: Marcus starts a session, Alice answers the next turn")
    async with boot() as nova:
        h1 = issue("known", "p-marcus", "Marcus")
        h2 = issue("known", "p-alice", "Alice")
        check(h1 != h2, "each command mints its own handle")

        i1 = await resolve(h1)
        i2 = await resolve(h2)
        check(i1.profile_id == "p-marcus", "command 1 is Marcus")
        check(i2.profile_id == "p-alice", "command 2 is Alice")
        check(i2.memory_entity != i1.memory_entity,
              "the second does not inherit the first's identity")


# ── §14/§15. episodic speaker provenance ─────────────────────────────────────

async def test_episode_wording_and_provenance():
    check.section("15: episodes name whoever actually did the thing")
    async with boot() as nova:
        from core.turn_identity import TurnIdentity, active_turn
        from core.speaker.matcher import SpeakerMatch

        rt = nova.runtime
        promoter = getattr(rt, "_episodic", None) or getattr(rt, "_promoter", None)
        if promoter is None:
            check(False, "episodic promoter not reachable from the runtime")
            return

        from core.episodic_promoter import _speaker_name
        with active_turn(TurnIdentity.typed()):
            check(_speaker_name() == "Marcus", "the owner is still named Marcus")
        alice = TurnIdentity.from_match(
            SpeakerMatch(status="known", profile_id="p-alice", display_name="Alice",
                         attempted=True), profile=_Prof())
        with active_turn(alice):
            check(_speaker_name() == "Alice", "a known guest is named")
        unk = TurnIdentity.from_match(SpeakerMatch(status="unknown", attempted=True))
        with active_turn(unk):
            check(_speaker_name() == "The user",
                  "an unattributed turn gets neutral wording, not a manufactured Marcus")

        # And the durable row carries structured provenance, not just prose.
        from core.workers.episodic_ingest import _actor_fields, _privacy_for
        # P5.1e.1: no identity now means NO HUMAN, not "assume Marcus". The
        # actor is Nova; the privacy owner is still Marcus (see _privacy_for).
        check(_actor_fields(None) == ("system", "Nova", "system"),
              "an off-turn event's ACTOR is Nova, not a fabricated Marcus")
        check(_privacy_for(None, "project_event") == "user",
              "while its PRIVACY OWNER is still Marcus")
        check(_actor_fields(TurnIdentity.typed())[0] == "user", "typed is owner")
        check(_actor_fields(alice)[0] == "speaker:p-alice", "a guest gets their namespace")
        e, lab, src = _actor_fields(unk)
        check(e == "unverified" and lab == "The user",
              f"and an unattributed turn is its OWN namespace, not Marcus ({e})")


async def test_episode_attribution_survives_the_bus():
    check.section("14: identity crosses the BUS by snapshot, not inheritance")
    import asyncio

    import aiosqlite
    from core.event_bus import BUS

    async with boot() as nova:
        from core.speaker.matcher import SpeakerMatch
        from core.turn_identity import TurnIdentity, active_turn

        rt = nova.runtime
        alice = TurnIdentity.from_match(
            SpeakerMatch(status="known", profile_id="p-alice", display_name="Alice",
                         attempted=True), profile=_Prof())

        # The promoter drains the bus ON ITS OWN TASK, so a consumer calling
        # current_identity() would read the typed default and file Alice's
        # correction as Marcus's. This exercises the real publish → promoter →
        # worker → SQLite path rather than the helper.
        with active_turn(alice):
            BUS.publish("memory.corrected", {"entity": "user", "attribute": "favorite_color",
                                             "was": "blue", "now": "red"})
        with active_turn(TurnIdentity.typed()):
            BUS.publish("memory.corrected", {"entity": "user", "attribute": "hobby",
                                             "was": "chess", "now": "running"})
        await asyncio.sleep(1.5)
        await rt._promoter.stop()
        if rt._episodic_q is not None:
            await asyncio.wait_for(rt._episodic_q.join(), timeout=30)

        async with aiosqlite.connect(str(nova.memory._sqlite._db_path)) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT summary, speaker_entity, speaker_label FROM episodes")
            rows = [dict(r) for r in await cur.fetchall()]

        by_attr = {("favorite_color" in r["summary"]): r for r in rows}
        check(len(rows) == 2, f"both corrections became episodes ({len(rows)})")
        guest = by_attr.get(True)
        owner = by_attr.get(False)
        check(guest and guest["speaker_entity"] == "speaker:p-alice",
              f"Alice's correction is HERS ({guest})")
        check(guest and guest["summary"].startswith("Alice corrected"),
              f"and is narrated as hers, not his ({guest['summary'] if guest else None})")
        check(owner and owner["speaker_entity"] == "user"
              and owner["summary"].startswith("Marcus corrected"),
              f"while his stays his, unchanged ({owner})")


async def test_episodic_read_scope_and_no_side_effects():
    check.section("16+17: episodic recall is scoped, and denied reads leave no trace")
    import aiosqlite

    from memory.episodes import Episode, EpisodicStore
    from memory.episodic_recall import retrieve

    async with boot() as nova:
        from core.speaker.matcher import SpeakerMatch
        from core.turn_identity import TurnIdentity, active_turn

        store = EpisodicStore(Path(nova.memory._sqlite._db_path))
        alice = TurnIdentity.from_match(
            SpeakerMatch(status="known", profile_id="p-alice", display_name="Alice",
                         attempted=True), profile=_Prof())
        bob = TurnIdentity.from_match(
            SpeakerMatch(status="known", profile_id="p-bob", display_name="Bob",
                         attempted=True), profile=_Prof())
        unk = TurnIdentity.from_match(SpeakerMatch(status="unknown", attempted=True))

        await store.record_episode(Episode(
            id="ep-owner", kind="selection", summary=f"Marcus chose the {OWNER_EPISODE} drive",
            entities=[OWNER_EPISODE], speaker_entity="user", speaker_label="Marcus",
            actor_entity="user", privacy_scope="user"))
        await store.record_episode(Episode(
            id="ep-alice", kind="selection", summary=f"Alice chose the {ALICE_PROFILE} option",
            entities=[ALICE_PROFILE], speaker_entity="speaker:p-alice", speaker_label="Alice",
            actor_entity="speaker:p-alice", privacy_scope="speaker:p-alice"))
        await store.record_episode(Episode(
            id="ep-sys", kind="project_milestone", summary="The SHARED-BUILD-990 finished",
            # Explicitly classified impersonal, which is what makes it shared —
            # NOT the fact that Nova was the actor (P5.1e.1).
            entities=["SHARED-BUILD-990"], speaker_entity="system",
            actor_entity="system", actor_label="Nova", privacy_scope="system"))
        # A legacy row with no attribution at all — must be treated as owner.
        async with aiosqlite.connect(str(nova.memory._sqlite._db_path)) as db:
            await db.execute(
                "INSERT OR REPLACE INTO episodes(id, kind, summary, entities, trust, "
                "freshness, provenance, importance, access_count, created_at, "
                "speaker_entity, speaker_label, input_source) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                # No attribution at all — the pre-P5.1e shape. Must read back as
                # owner-private, which is what the column default gives it.
                ("ep-legacy", "selection", "chose the LEGACY-EP-993 item", '["LEGACY-EP-993"]',
                 "TOOL_RESULT", "SESSION", "{}", 0.5, 0, "2026-01-01T00:00:00+00:00",
                 "", "", "typed"))
            await db.commit()

        async def recall(identity, q):
            with active_turn(identity):
                return await retrieve(store, q, limit=10)

        owner = await recall(TurnIdentity.typed(),
                             f"{OWNER_EPISODE} {ALICE_PROFILE} SHARED-BUILD-990 LEGACY-EP-993")
        check(OWNER_EPISODE in owner.prompt_text, "the owner recalls his own episode")
        check("LEGACY-EP-993" in owner.prompt_text, "and unattributed legacy history")

        a = await recall(alice, f"{OWNER_EPISODE} {ALICE_PROFILE} SHARED-BUILD-990 LEGACY-EP-993")
        check(ALICE_PROFILE in a.prompt_text, "Alice recalls her own episode")
        check(OWNER_EPISODE not in a.prompt_text, "and not Marcus's")
        check("LEGACY-EP-993" not in a.prompt_text,
              "nor unattributed history, which is conservatively his")
        check("SHARED-BUILD-990" in a.prompt_text,
              "system episodes stay shared")

        b = await recall(bob, ALICE_PROFILE)
        check(ALICE_PROFILE not in b.prompt_text, "Bob cannot read Alice's episodes")

        u = await recall(unk, f"{OWNER_EPISODE} {ALICE_PROFILE}")
        for s in (OWNER_EPISODE, ALICE_PROFILE):
            check(s not in u.prompt_text, f"an unknown speaker gets no private episode ({s})")

        # ── §17: a denied episode must not be reinforced ────────────────────
        async def counters(ep_id):
            async with aiosqlite.connect(str(nova.memory._sqlite._db_path)) as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute(
                    "SELECT access_count, last_accessed_at FROM episodes WHERE id=?", (ep_id,))
                r = await cur.fetchone()
                return (r["access_count"], r["last_accessed_at"])

        n0, t0 = await counters("ep-owner")
        await recall(alice, OWNER_EPISODE)
        await recall(unk, OWNER_EPISODE)
        n1, t1 = await counters("ep-owner")
        check(n1 == n0 and t1 == t0,
              f"Alice and an unknown speaker searching his exact sentinel left "
              f"access_count and last_accessed_at untouched ({n0}->{n1})")

        await recall(TurnIdentity.typed(), OWNER_EPISODE)
        n2, t2 = await counters("ep-owner")
        check(n2 > n1, f"while his own recall still reinforces ({n1}->{n2})")


async def test_cold_evidence_not_hydrated_for_denied_episode():
    check.section("17: a denied episode does not trigger a COLD read")
    from memory.episodes import Episode, EpisodicStore
    from memory.episodic_recall import retrieve

    async with boot() as nova:
        from core.speaker.matcher import SpeakerMatch
        from core.turn_identity import TurnIdentity, active_turn

        store = EpisodicStore(Path(nova.memory._sqlite._db_path))
        ref = store.cold.put(f"the full private evidence for {OWNER_EPISODE}")
        await store.record_episode(Episode(
            id="ep-cold", kind="selection", summary=f"Marcus chose {OWNER_EPISODE}",
            entities=[OWNER_EPISODE], speaker_entity="user", speaker_label="Marcus",
            actor_entity="user", privacy_scope="user", provenance={"cold_ref": ref}))

        reads = {"n": 0}
        orig_get = store.cold.get

        def counting_get(r):
            reads["n"] += 1
            return orig_get(r)

        store.cold.get = counting_get  # type: ignore[assignment]

        alice = TurnIdentity.from_match(
            SpeakerMatch(status="known", profile_id="p-alice", display_name="Alice",
                         attempted=True), profile=_Prof())
        with active_turn(alice):
            res = await retrieve(store, OWNER_EPISODE, limit=5, hydrate=True)
        check(OWNER_EPISODE not in res.prompt_text, "Alice gets nothing")
        check(reads["n"] == 0,
              f"and his evidence was never pulled off disk for her ({reads['n']} cold reads)")

        with active_turn(TurnIdentity.typed()):
            res = await retrieve(store, OWNER_EPISODE, limit=5, hydrate=True)
        check(reads["n"] >= 1, "while his own hydration still works")


# ── §22. the privacy sentinel, through the real turn path ────────────────────

async def test_production_sentinels_through_live_voice():
    check.section("22: sentinels through the actual chat path, per speaker")
    import uuid

    async with boot() as nova:
        from core.turn_identity import active_turn
        from memory.episodes import Episode, EpisodicStore

        m, rt = nova.memory, nova.runtime
        await m.add_fact(entity="user", attribute="name", value=OWNER_NAME, confidence=0.98)
        await m.add_fact(entity="user", attribute="spouse", value=OWNER_SPOUSE, confidence=0.9)
        await m.add_fact(entity="insight", attribute="pattern", value=OWNER_INSIGHT,
                         confidence=0.8)
        await m.add_fact(entity="speaker:p-alice", attribute="note", value=ALICE_PROFILE,
                         confidence=0.9)
        from core.turn_identity import TurnIdentity
        with active_turn(TurnIdentity.typed()):
            await m.add_lesson(OWNER_LESSON, topic="preference")
            await m.note_thought("note", OWNER_THOUGHT, topic="work")
        store = EpisodicStore(Path(m._sqlite._db_path))
        await store.record_episode(Episode(
            id="ep-sent", kind="selection", summary=f"Marcus chose {OWNER_EPISODE}",
            entities=[OWNER_EPISODE], speaker_entity="user", speaker_label="Marcus",
            actor_entity="user", privacy_scope="user"))

        async def prompts_for(handle, source="voice"):
            ident = await resolve(handle, source)
            nova.llm.reset_calls()
            with active_turn(ident):
                async for _ in rt.chat_turn_stream(
                        user_text="tell me everything you know",
                        conversation_id=uuid.uuid4(), identity=ident):
                    pass
            return ident, " || ".join(nova.llm.prompts)

        ident, guest = await prompts_for(issue("known", "p-alice", "Alice"))
        check(ident.profile_id == "p-alice", "the guest turn resolved from a real handle")
        leaked = [s for s in OWNER_SENTINELS if s in guest]
        check(not leaked, f"a known guest receives NO owner sentinel ({leaked})")

        _i, unknown = await prompts_for(None, "voice")
        leaked = [s for s in OWNER_SENTINELS if s in unknown]
        check(not leaked, f"an unverified voice turn receives none either ({leaked})")

        _i, owner = await prompts_for(None, None)
        check(OWNER_NAME in owner, "and the owner's own context still works")


# ── §23. the boundary that must not move ─────────────────────────────────────

async def test_permissions_unchanged():
    check.section("23: identity still changes no permission decision")
    import inspect

    from core.permissions import evaluate, tier_of
    from core.speaker.matcher import SpeakerMatch
    from core.turn_identity import TurnIdentity, active_turn

    idents = [TurnIdentity.typed()]
    for st, pid, nm, role in (("known", "p-m", "Marcus", "owner"),
                              ("known", "p-a", "Alice", "guest"),
                              ("unknown", None, None, "guest")):
        idents.append(TurnIdentity.from_match(
            SpeakerMatch(status=st, profile_id=pid, display_name=nm, attempted=True),
            profile=(_Prof(role) if pid else None)))

    per_cap: dict[str, set] = {}
    for cap in ("some.destructive.capability", "memory.remember", "shell.exec"):
        for i in idents:
            with active_turn(i):
                per_cap.setdefault(cap, set()).add((tier_of(cap),
                                                    evaluate(cap, mode="guarded")))
    check(all(len(v) == 1 for v in per_cap.values()),
          f"identical per capability across four identities ({per_cap})")
    check(not (set(inspect.signature(evaluate).parameters)
               & {"speaker", "identity", "role"}),
          "and evaluate() still takes no identity argument")


async def main():
    await test_handle_resolution_matrix()
    await test_session_commands_classify_independently()
    await test_episode_wording_and_provenance()
    await test_episode_attribution_survives_the_bus()
    await test_episodic_read_scope_and_no_side_effects()
    await test_cold_evidence_not_hydrated_for_denied_episode()
    await test_production_sentinels_through_live_voice()
    await test_permissions_unchanged()
    check.finish()


if __name__ == "__main__":
    run(main)
