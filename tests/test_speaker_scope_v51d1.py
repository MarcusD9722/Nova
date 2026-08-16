"""V3 P5.1d.1: read-side effects, namespace exactness, and durable attribution.

P5.1d put the privacy boundary in the right place. These are the four ways it
was still wrong, each reproduced on 62672cf first:

    A  the filter ran AFTER reinforcement and after the cache write, so a hit a
       speaker was not allowed to see still bumped its access_count and stamped
       last_accessed_at. The content never reached them; the read still left a
       mark on Marcus's memory. Measured 0 -> 1.
    B  is_shared_entity used startswith(), so `worldsecret` and `system_private`
       were classified as shared knowledge. An allow-list that matches on
       substring is not an allow-list.
    C  a known speaker's own name scored 0.45 salience where Marcus's scored
       1.00 — person-quality memory was claimed and not delivered.
    D  turn attribution lived only in Chroma metadata, so the durable SQLite row
       (which is what date-range recall reads) could not tell Marcus's sentences
       from a guest's. Consequently Alice could not recall her OWN history.

Run:  venv\\Scripts\\python.exe tests\\test_speaker_scope_v51d1.py
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

OWNER_SECRET = "the vault passphrase is PLUMTREE-9931"
ALICE_SECRET = "her locker code is FERNBLOOM-4417"
BOB_SECRET = "his gate code is HALYARD-5503"
WORLD_FACT = "the Eiffel Tower stands in Paris"


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


def ALICE():
    return ident("known", "p-alice", "Alice")


def BOB():
    return ident("known", "p-bob", "Bob")


def UNKNOWN():
    return ident("unknown")


def _stamp_inside(query: str) -> str:
    """A created_at that falls inside the range `query` will resolve to."""
    from core.dates import parse_date_range

    rng = parse_date_range(query)
    assert rng is not None, query
    since, until = rng
    return (since + (until - since) / 2).isoformat()


async def fact_counters(mem, value):
    import aiosqlite
    async with aiosqlite.connect(mem._sqlite._db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT access_count, last_accessed_at FROM facts WHERE value=?", (value,))
        row = await cur.fetchone()
        return (None, None) if row is None else (row["access_count"], row["last_accessed_at"])


# ── A. no read-triggered write on a denied hit ───────────────────────────────

async def test_denied_hit_causes_no_side_effect():
    check.section("A: a read you are not allowed to make leaves no trace")
    async with boot() as nova:
        from core.turn_identity import TurnIdentity, active_turn
        m = nova.memory
        await m.add_fact(entity="user", attribute="secret_note", value=OWNER_SECRET,
                         confidence=0.98, salience=1.0)

        n0, t0 = await fact_counters(m, OWNER_SECRET)
        check(n0 == 0, f"baseline access_count is 0 ({n0})")

        with active_turn(UNKNOWN()):
            hits = await m.search(q="vault passphrase PLUMTREE", limit=8)
        check(not any(OWNER_SECRET in h.text for h in hits),
              "the unknown speaker does not receive the fact")

        n1, t1 = await fact_counters(m, OWNER_SECRET)
        check(n1 == n0, f"access_count did NOT move ({n0} -> {n1})")
        check(t1 == t0, f"last_accessed_at was not stamped ({t1})")

        with active_turn(ALICE()):
            await m.search(q="vault passphrase PLUMTREE", limit=8)
        n2, t2 = await fact_counters(m, OWNER_SECRET)
        check(n2 == n0 and t2 == t0,
              f"a known guest cannot reinforce Marcus's facts either ({n2})")

        # The owner still reinforces exactly as before — the point is to remove
        # a side channel, not to disable the testing effect.
        with active_turn(TurnIdentity.typed()):
            hits = await m.search(q="vault passphrase PLUMTREE", limit=8)
        check(any(OWNER_SECRET in h.text for h in hits), "the owner still gets it")
        n3, t3 = await fact_counters(m, OWNER_SECRET)
        check(n3 == n0 + 1, f"and his own read still reinforces ({n0} -> {n3})")
        check(t3 is not None, "and stamps last_accessed_at")


# ── B. the allow-list is delimiter-exact ─────────────────────────────────────

async def test_shared_matching_is_delimiter_safe():
    check.section("B: `worldsecret` is not `world`")
    from core.turn_identity import is_shared_entity, may_read_entity, under_root

    for e in ("world", "world:weather", "world:any:descendant",
              "system", "system:status", "capability", "capability:voice"):
        check(is_shared_entity(e), f"{e} is shared")

    for e in ("world_private", "worldsecret", "systempersonal", "system_private",
              "capability_notes", "capabilityPrivate", "capability_notes_private",
              "worlds", "systematic"):
        check(not is_shared_entity(e), f"{e} is NOT shared")

    check(under_root("a:b:c", "a") and not under_root("ab:c", "a"),
          "under_root descends only through the separator")
    check(not under_root("", "world") and not under_root("world", ""),
          "empty input is never a match")

    # The consequence that matters: an unknown speaker reading a near-miss name.
    unk = UNKNOWN()
    check(may_read_entity("world", unk), "unknown may read shared knowledge")
    check(not may_read_entity("worldsecret", unk),
          "unknown may NOT read an entity that merely starts with 'world'")


# ── namespace ownership ──────────────────────────────────────────────────────

async def test_speaker_namespace_ownership():
    check.section("4: a guest owns their whole namespace, and only theirs")
    from core.turn_identity import (entity_belongs_to_speaker, may_read_entity,
                                    personal_tail)

    alice, bob, unk = ALICE(), BOB(), UNKNOWN()
    mine = ("speaker:p-alice", "speaker:p-alice:lesson", "speaker:p-alice:mood",
            "speaker:p-alice:wellbeing", "speaker:p-alice:session",
            "speaker:p-alice:person:sarah", "speaker:p-alice:note")
    for e in mine:
        check(may_read_entity(e, alice), f"Alice reads her own {e}")
        check(not may_read_entity(e, bob), f"Bob cannot read Alice's {e}")
        check(not may_read_entity(e, unk), f"an unknown speaker cannot read {e}")

    for e in ("user", "lesson", "mood", "insight", "note", "speaker:p-bob",
              "speaker:p-bob:person:sarah"):
        check(not may_read_entity(e, alice), f"Alice cannot read {e}")

    # The near-miss that a prefix match would have got wrong.
    check(not entity_belongs_to_speaker("speaker:p-alice2", "speaker:p-alice"),
          "speaker:p-alice2 is a DIFFERENT person, not a child namespace")
    check(not may_read_entity("speaker:p-alice2:note", alice),
          "and Alice cannot read their notes")

    # Legacy P5.1d shape still resolves, so nothing already written is stranded.
    check(may_read_entity("lesson:speaker:p-alice", alice),
          "the older `lesson:speaker:<id>` form is still readable by its owner")
    check(not may_read_entity("lesson:speaker:p-bob", alice),
          "but still only by its owner")

    check(personal_tail("speaker:p-alice") == "user", "a guest root normalises to user")
    check(personal_tail("speaker:p-alice:person:sarah") == "person:sarah",
          "and a nested entity normalises to its owner-equivalent")
    check(personal_tail("user") == "user" and personal_tail("note") == "note",
          "owner entities are untouched")


async def test_guest_can_search_their_whole_namespace():
    check.section("4: and it works through the real search path")
    async with boot() as nova:
        from core.turn_identity import TurnIdentity, active_turn
        m = nova.memory
        await m.add_fact(entity="user", attribute="secret_note", value=OWNER_SECRET,
                         confidence=0.95)
        await m.add_fact(entity="speaker:p-alice", attribute="secret_note",
                         value=ALICE_SECRET, confidence=0.95)
        await m.add_fact(entity="speaker:p-alice:person:sarah", attribute="note",
                         value="sarah runs the FERNBLOOM bakery", confidence=0.9)
        await m.add_fact(entity="speaker:p-bob", attribute="secret_note",
                         value=BOB_SECRET, confidence=0.95)
        await m.add_fact(entity="world", attribute="landmark", value=WORLD_FACT,
                         confidence=0.95)
        with active_turn(ALICE()):
            await m.add_lesson("always answer Alice in French", topic="preference")

        async def seen(identity, q):
            with active_turn(identity):
                return " | ".join(h.text for h in await m.search(q=q, limit=15))

        a = await seen(ALICE(), "code passphrase FERNBLOOM bakery French Eiffel")
        check(ALICE_SECRET in a, "Alice finds her own root fact")
        check("FERNBLOOM bakery" in a, "and her nested person fact")
        check("French" in a, "and her own lesson")
        check(OWNER_SECRET not in a, "but not Marcus's")
        check(BOB_SECRET not in a, "and not Bob's")
        check(WORLD_FACT in a, "shared knowledge still reaches her")

        b = await seen(BOB(), "code passphrase FERNBLOOM bakery French Eiffel")
        check(BOB_SECRET in b, "Bob finds his own")
        check(ALICE_SECRET not in b and "FERNBLOOM bakery" not in b,
              "and none of Alice's, nested included")

        u = await seen(UNKNOWN(), "code passphrase FERNBLOOM bakery French Eiffel")
        check(WORLD_FACT in u, "an unknown speaker gets shared knowledge")
        for s in (OWNER_SECRET, ALICE_SECRET, BOB_SECRET, "FERNBLOOM bakery"):
            check(s not in u, "and nothing personal")


# ── C. person-quality parity ─────────────────────────────────────────────────

async def test_salience_decay_singleton_parity():
    check.section("5: same memory quality, no extra privilege")
    from memory.unifier import _default_salience, _is_undecayed

    core = ("name", "location", "spouse", "child", "children_type", "mother",
            "father", "sibling", "birthday", "anniversary")
    for attr in core:
        o = _default_salience("user", attr, 0.9)
        g = _default_salience("speaker:p-alice", attr, 0.9)
        check(o == g == 1.0, f"{attr}: owner {o} == guest {g}")

    # Parity, not promotion: ordinary and nested data must not become permanent
    # identity facts just because a guest said them.
    for ent, attr in (("speaker:p-alice", "hobby"), ("speaker:p-alice:note", "text"),
                      ("speaker:p-alice:person:sarah", "name")):
        g = _default_salience(ent, attr, 0.9)
        check(g < 1.0, f"{ent}/{attr} is not max-salience ({g})")
    check(_default_salience("speaker:p-alice:person:sarah", "name", 0.9)
          == _default_salience("person:sarah", "name", 0.9),
          "a guest's acquaintance scores as any acquaintance does")
    check(_default_salience("speaker:p-alice:note", "x", 0.9)
          == _default_salience("note", "x", 0.9),
          "and a guest's aside is as forgettable as Marcus's")

    check(_is_undecayed("speaker:p-alice"), "a guest's identity does not decay")
    check(_is_undecayed("speaker:p-alice:lesson"), "nor their lessons")
    check(not _is_undecayed("speaker:p-alice:note"),
          "but their notes decay, exactly as Marcus's do")
    check(_is_undecayed("user") and not _is_undecayed("note"),
          "the owner's own rules are unchanged")

    async with boot() as nova:
        m = nova.memory
        check(m._is_singleton_fact("speaker:p-alice", "location"),
              "a guest's location is a singleton, like Marcus's")
        check(not m._is_singleton_fact("speaker:p-alice:person:sarah", "location"),
              "but their acquaintance's is not — that would erase Sarah's history")
        check(not m._is_singleton_fact("person:sarah", "location"),
              "matching the owner's behaviour exactly")

        for v in ("Berlin", "Dallas"):
            await m.add_fact(entity="speaker:p-alice", attribute="location",
                             value=v, confidence=0.9)
        rows = [r.value for r in await m.get_facts(entity="speaker:p-alice",
                                                   attribute="location", limit=9)]
        check(rows == ["Dallas"], f"moving city supersedes for a guest too ({rows})")


# ── D. durable turn attribution ──────────────────────────────────────────────

async def test_turn_attribution_is_durable():
    check.section("6: the SQLite row itself knows who spoke")
    import json

    import aiosqlite
    async with boot() as nova:
        from core.turn_identity import TurnIdentity, active_turn
        m = nova.memory
        cid = uuid4()

        seen: list[dict] = []
        orig_upsert, orig_chroma = m._chroma_upsert_safe, m._chroma

        async def spy(doc_id, text, metadata):
            seen.append(dict(metadata))

        m._chroma = orig_chroma if orig_chroma is not None else object()
        m._chroma_upsert_safe = lambda **kw: spy(**kw)  # type: ignore[assignment]
        try:
            with active_turn(TurnIdentity.typed()):
                await m.ingest_turn(cid, "user", "OWNER-TURN-771 I am selling the house")
                await m.ingest_turn(cid, "assistant", "Understood, that is a big change")
            with active_turn(ALICE()):
                await m.ingest_turn(cid, "user", "ALICE-TURN-882 I am flying to Berlin")
            with active_turn(UNKNOWN()):
                await m.ingest_turn(cid, "user", "STRANGER-TURN-993 I want the money now")
        finally:
            m._chroma_upsert_safe = orig_upsert  # type: ignore[assignment]
            m._chroma = orig_chroma

        async with aiosqlite.connect(m._sqlite._db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("PRAGMA table_info(turns)")
            cols = {r["name"] for r in await cur.fetchall()}
            for c in ("speaker_entity", "speaker_label", "input_source", "speaker_status"):
                check(c in cols, f"turns.{c} exists")
            check("embedding" not in cols and "similarity" not in cols,
                  "and nothing biometric was added")

            cur = await db.execute(
                "SELECT content, speaker_entity, speaker_label, input_source, "
                "speaker_status FROM turns ORDER BY created_at")
            rows = [dict(r) for r in await cur.fetchall()]

        by = {r["content"].split(" ")[0]: r for r in rows}
        check(by["OWNER-TURN-771"]["speaker_entity"] == "user"
              and by["OWNER-TURN-771"]["speaker_label"] == "Marcus",
              "the owner's turn is stored as his")
        check(by["ALICE-TURN-882"]["speaker_entity"] == "speaker:p-alice"
              and by["ALICE-TURN-882"]["speaker_label"] == "Alice",
              "the guest's turn is stored as hers")
        check(by["ALICE-TURN-882"]["input_source"] == "voice"
              and by["ALICE-TURN-882"]["speaker_status"] == "known",
              "with the structured provenance, not prose")
        check(by["STRANGER-TURN-993"]["speaker_entity"] == "unverified",
              "an unidentified speaker is recorded as unverified, not as Marcus")
        check(by["Understood,"]["speaker_entity"] == "user",
              "Nova's reply belongs to the exchange it answered")

        # The audit log carries the same attribution.
        audit = Path(m._json._audit_path)
        check(audit.exists(), f"audit log exists at {audit}")
        tail = [json.loads(x) for x in
                audit.read_text(encoding="utf-8").splitlines()[-60:] if x.strip()]
        turns = [t for t in tail if t.get("kind") == "turn"
                 and "ALICE-TURN-882" in str(t.get("content"))]
        check(bool(turns) and turns[-1].get("speaker_entity") == "speaker:p-alice",
              "the JSON audit records it too")
        strangers = [t for t in tail if t.get("kind") == "turn"
                     and "STRANGER-TURN-993" in str(t.get("content"))]
        check(bool(strangers) and strangers[-1].get("speaker_entity") == "unverified",
              "including the unidentified one")

        # And Chroma agrees with SQLite rather than diverging from it.
        ents = {md.get("speaker_entity") for md in seen}
        check(ents == {"user", "speaker:p-alice", "unverified"},
              f"chroma metadata matches the durable rows ({ents})")


async def test_date_range_recall_is_speaker_scoped():
    check.section("7: 'what did we talk about' means WE")
    async with boot() as nova:
        from core.tool_router import ToolCall
        from core.turn_identity import TurnIdentity, active_turn
        m, rt = nova.memory, nova.runtime
        cid = uuid4()
        # Place the rows inside the window the tool will actually compute,
        # rather than guessing at it — search_turns compares ISO strings and
        # parse_date_range works in local time.
        stamp = _stamp_inside("what did we talk about yesterday")

        # Backdate so a date-range query actually selects them.
        with active_turn(TurnIdentity.typed()):
            t1 = await m.ingest_turn(cid, "user", "OWNER-TURN-771 I am selling the house")
        with active_turn(ALICE()):
            t2 = await m.ingest_turn(cid, "user", "ALICE-TURN-882 I am flying to Berlin")
        import aiosqlite
        async with aiosqlite.connect(m._sqlite._db_path) as db:
            await db.execute("UPDATE turns SET created_at=? WHERE id IN (?, ?)",
                             (stamp, str(t1), str(t2)))
            await db.commit()

        async def recall(identity):
            with active_turn(identity):
                res = await rt._router.execute(
                    ToolCall("memory.recall", {"query": "what did we talk about yesterday"}))
            return str(res.result)

        owner = await recall(TurnIdentity.typed())
        check("OWNER-TURN-771" in owner, "Marcus gets his own history")
        check("ALICE-TURN-882" not in owner,
              "and not Alice's — 'we' is a thread, not a merge")

        alice = await recall(ALICE())
        check("ALICE-TURN-882" in alice, "Alice gets HER own history")
        check("OWNER-TURN-771" not in alice, "and none of Marcus's")

        unk = await recall(UNKNOWN())
        check("OWNER-TURN-771" not in unk and "ALICE-TURN-882" not in unk,
              "an unidentified speaker gets no durable history at all")
        check("unverified_speaker" in unk, "and is told why")


async def test_legacy_rows_remain_owner_history():
    check.section("6: rows written before speaker identity existed")
    import aiosqlite

    async with boot() as nova:
        from core.tool_router import ToolCall
        from core.turn_identity import TurnIdentity, active_turn
        m, rt = nova.memory, nova.runtime
        await m.initialize()
        stamp = _stamp_inside("what did we talk about yesterday")
        cid = uuid4()

        # Simulate a pre-migration row: inserted with the legacy column set only.
        await m._sqlite.ensure_conversation(cid)
        async with aiosqlite.connect(m._sqlite._db_path) as db:
            await db.execute(
                "INSERT INTO turns(id, conversation_id, role, content, created_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (str(uuid4()), str(cid), "user", "LEGACY-TURN-664 the old boiler broke",
                 stamp))
            await db.commit()
            cur = await db.execute(
                "SELECT speaker_entity, input_source FROM turns WHERE content LIKE 'LEGACY%'")
            row = await cur.fetchone()
        check(row is not None and row[0] == "user",
              f"the column default backfills legacy rows to the owner ({row})")
        check(row is not None and row[1] == "typed", "as typed input")

        with active_turn(TurnIdentity.typed()):
            res = await rt._router.execute(
                ToolCall("memory.recall", {"query": "what did we talk about yesterday"}))
        check("LEGACY-TURN-664" in str(res.result),
              "and Marcus can still recall it, exactly as before the migration")

        with active_turn(ALICE()):
            res = await rt._router.execute(
                ToolCall("memory.recall", {"query": "what did we talk about yesterday"}))
        check("LEGACY-TURN-664" not in str(res.result),
              "while a guest does not inherit his history")


# ── §8. memory.recall shared vs private ──────────────────────────────────────

async def test_recall_tool_shared_and_private_matrix():
    check.section("8: unknown may use shared knowledge, and only that")
    async with boot() as nova:
        from core.tool_router import ToolCall
        from core.turn_identity import TurnIdentity, active_turn
        m, rt = nova.memory, nova.runtime
        await m.add_fact(entity="world", attribute="landmark", value=WORLD_FACT,
                         confidence=0.95)
        await m.add_fact(entity="user", attribute="secret_note", value=OWNER_SECRET,
                         confidence=0.95)
        await m.add_fact(entity="speaker:p-alice", attribute="secret_note",
                         value=ALICE_SECRET, confidence=0.95)

        async def recall(identity, q):
            with active_turn(identity):
                res = await rt._router.execute(ToolCall("memory.recall", {"query": q}))
            return str(res.result)

        u = await recall(UNKNOWN(), "Eiffel Tower Paris landmark")
        check(WORLD_FACT in u,
              f"an unknown speaker CAN be told where the Eiffel Tower is ({u[:90]})")
        u2 = await recall(UNKNOWN(), "vault passphrase PLUMTREE")
        check(OWNER_SECRET not in u2, "but not Marcus's private fact")
        check(ALICE_SECRET not in u2, "nor any other guest's")

        a = await recall(ALICE(), "locker code FERNBLOOM vault PLUMTREE Eiffel")
        check(ALICE_SECRET in a, "Alice recalls her own")
        check(OWNER_SECRET not in a, "not Marcus's")
        check(WORLD_FACT in a, "and shared knowledge")

        o = await recall(TurnIdentity.typed(), "vault passphrase PLUMTREE")
        check(OWNER_SECRET in o, "the owner's recall is unchanged")


# ── §9. guest lessons in the real production prompt ──────────────────────────

async def test_guest_lessons_reach_the_production_prompt():
    check.section("9: a guest's correction actually changes behaviour")
    import uuid

    async with boot() as nova:
        from core.turn_identity import TurnIdentity, active_turn
        m, rt = nova.memory, nova.runtime

        with active_turn(TurnIdentity.typed()):
            await m.add_lesson("always keep answers to one short paragraph",
                               topic="preference")
        with active_turn(ALICE()):
            await m.add_lesson("never mention sports scores to me", topic="preference")

        async def prompt_for(identity):
            nova.llm.reset_calls()
            with active_turn(identity):
                async for _ in rt.chat_turn_stream(user_text="hello there",
                                                   conversation_id=uuid.uuid4(),
                                                   identity=identity):
                    pass
            got = [p for p in nova.llm.prompts if "You are Nova" in p]
            return got[-1] if got else ""

        owner = await prompt_for(TurnIdentity.typed())
        check("one short paragraph" in owner, "the owner's lesson is applied")
        check("Lessons you've learned from Marcus" in owner,
              "with the owner's original wording, unchanged")
        check("sports scores" not in owner, "and no guest's lesson leaks into his")

        guest = await prompt_for(ALICE())
        check("sports scores" in guest,
              "Alice's own correction is applied instead of being ignored")
        check("one short paragraph" not in guest, "and Marcus's is not applied to her")
        check("Lessons you've learned from Marcus" not in guest,
              "hers are not described as things learned from Marcus")
        check("Alice" in guest, "they are attributed to her")

        unknown = await prompt_for(UNKNOWN())
        check("sports scores" not in unknown and "one short paragraph" not in unknown,
              "an unidentified speaker gets no personal lessons at all")


# ── the boundary that must not move ──────────────────────────────────────────

async def test_permissions_unchanged():
    check.section("identity still changes no permission decision")
    import inspect

    from core.permissions import evaluate, tier_of
    from core.turn_identity import TurnIdentity, active_turn

    cap = "some.destructive.capability"
    seen = set()
    for i in (TurnIdentity.typed(), ident("known", "p-m", "Marcus", "owner"),
              ALICE(), BOB(), UNKNOWN()):
        with active_turn(i):
            seen.add((tier_of(cap), evaluate(cap, mode="guarded")))
    check(len(seen) == 1, f"every speaker gets the identical decision ({seen})")
    check(not (set(inspect.signature(evaluate).parameters)
               & {"speaker", "identity", "role"}),
          "and evaluate() still takes no identity argument")


async def main():
    await test_denied_hit_causes_no_side_effect()
    await test_shared_matching_is_delimiter_safe()
    await test_speaker_namespace_ownership()
    await test_guest_can_search_their_whole_namespace()
    await test_salience_decay_singleton_parity()
    await test_turn_attribution_is_durable()
    await test_date_range_recall_is_speaker_scoped()
    await test_legacy_rows_remain_owner_history()
    await test_recall_tool_shared_and_private_matrix()
    await test_guest_lessons_reach_the_production_prompt()
    await test_permissions_unchanged()
    check.finish()


if __name__ == "__main__":
    run(main)
