"""V3 P5.1 final closure: conversation-local state is per speaker.

Speaker identity is per COMMAND. Nova's conversation-local state was per
CONVERSATION — and every bit of it predates P5, so all of it was implicitly
Marcus's. Once a guest can speak into the same conversation, each becomes a
cross-speaker channel.

Reproduced on `1a17b58`, ONE conversation id throughout:

    recent chat       Alice/Bob/unknown all read OWNER-RECENT-901
    hot result set    all three resolved "the second one" to Marcus's item,
                      and doing so moved its access_count 3 -> 5
    summary / story   OWNER-SUMMARY-904 and OWNER-STORY-903 readable by anyone
    prompt wording    "say to Marcus out loud", "Marcus is referring to item 2"
                      addressed to Alice
    superseded        "Nova retired N beliefs after Marcus said: ..." for a
                      guest's correction

Four owner sentinels reached Alice's actual model-bound prompt.

The fix is ONE scope helper (`scoped_conversation_key`) applied at each store's
key, plus a structured `privacy_scope` on artifacts filtered at the single read
choke point. The OWNER's keys are unchanged, so nothing existing is orphaned.

Run:  venv\\Scripts\\python.exe tests\\test_speaker_conversation_scope_v51.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")
os.environ.setdefault("NOVA_REPO_ROOT", str(REPO))

from harness import Checks, boot, run  # noqa: E402

check = Checks()

O_RECENT = "OWNER-RECENT-901"
O_A, O_B = "OWNER-ARTIFACT-902-A", "OWNER-ARTIFACT-902-B"
O_STORY, O_SUM = "OWNER-STORY-903", "OWNER-SUMMARY-904"
A_RECENT, A_ART, A_STORY = "ALICE-RECENT-911", "ALICE-ARTIFACT-912", "ALICE-STORY-913"
B_RECENT = "BOB-RECENT-921"
OWNER_SENTINELS = (O_RECENT, O_A, O_B, O_STORY, O_SUM)
ALICE_SENTINELS = (A_RECENT, A_ART, A_STORY)


class _Prof:
    role = "guest"


def guest(pid, name):
    from core.speaker.matcher import SpeakerMatch
    from core.turn_identity import TurnIdentity
    return TurnIdentity.from_match(
        SpeakerMatch(status="known", profile_id=pid, display_name=name, attempted=True),
        profile=_Prof())


def OWNER():
    from core.turn_identity import TurnIdentity
    return TurnIdentity.typed()


def ALICE():
    return guest("p-alice", "Alice")


def BOB():
    return guest("p-bob", "Bob")


def UNKNOWN():
    from core.speaker.matcher import SpeakerMatch
    from core.turn_identity import TurnIdentity
    return TurnIdentity.from_match(SpeakerMatch(status="unknown", attempted=True))


# ── B. the scope model itself ────────────────────────────────────────────────

async def test_scope_model():
    check.section("B: one reusable scope, owner keys unchanged")
    from core.turn_identity import (active_turn, conversation_scope,
                                    is_ephemeral_scope, scoped_conversation_key)

    cid = "abc-123"
    with active_turn(OWNER()):
        check(conversation_scope() == "user", "owner scope is `user`")
        check(scoped_conversation_key(cid) == cid,
              "and his key is the bare conversation id — nothing is orphaned")
        check(not is_ephemeral_scope(), "his state is durable")
    with active_turn(ALICE()):
        check(conversation_scope() == "speaker:p-alice", "a guest gets their own")
        check(scoped_conversation_key(cid) == f"{cid}#speaker:p-alice",
              "with a distinct key")
    with active_turn(BOB()):
        check(scoped_conversation_key(cid) != f"{cid}#speaker:p-alice",
              "and two guests never share one")
    with active_turn(UNKNOWN()):
        check(conversation_scope() == "unverified", "an unknown speaker is `unverified`")
        check(is_ephemeral_scope(),
              "and is flagged ephemeral — the next stranger is not the same person")


# ── D. each subsystem ────────────────────────────────────────────────────────

async def test_conversation_state_is_partitioned():
    check.section("D: recent chat / assistant replies")
    async with boot() as nova:
        from core.turn_identity import active_turn
        st, cid = nova.runtime._state_store, uuid.uuid4()

        async def say(who, text):
            with active_turn(who):
                await st.record_turn(conversation_id=cid, user_message=text,
                                     assistant_reply="ok", follow_up_question=None,
                                     mode="chat")

        async def heard(who):
            with active_turn(who):
                return await st.recent_chat_text(cid)

        await say(OWNER(), O_RECENT)
        await say(ALICE(), A_RECENT)
        await say(BOB(), B_RECENT)
        await say(UNKNOWN(), "STRANGER-RECENT-931")

        o = await heard(OWNER())
        check(O_RECENT in o, "the owner still reads his own turns")
        check(A_RECENT not in o and B_RECENT not in o,
              "and not the guests' — his history is not a shared log")

        a = await heard(ALICE())
        check(A_RECENT in a, "Alice reads her own")
        check(O_RECENT not in a and B_RECENT not in a, "and neither his nor Bob's")

        b = await heard(BOB())
        check(B_RECENT in b and A_RECENT not in b, "Bob likewise")

        u = await heard(UNKNOWN())
        check("STRANGER-RECENT-931" not in u,
              f"an unknown speaker gets NO cross-turn history at all ({u[:40]!r})")
        for s in (O_RECENT, A_RECENT, B_RECENT):
            check(s not in u, f"and none of anyone else's ({s})")


async def test_working_context_is_partitioned():
    check.section("D: working context (topic, project, tool traces, selection)")
    async with boot() as nova:
        from core.turn_identity import active_turn
        w, cid = nova.runtime._working, str(uuid.uuid4())

        with active_turn(OWNER()):
            w.get(cid).record_user(O_RECENT)
        with active_turn(ALICE()):
            w.get(cid).record_user(A_RECENT)

        with active_turn(OWNER()):
            o = w.get(cid).describe_for_prompt() + w.get(cid).recent_text(6)
        with active_turn(ALICE()):
            a = w.get(cid).describe_for_prompt() + w.get(cid).recent_text(6)
        with active_turn(BOB()):
            b = w.get(cid).describe_for_prompt() + w.get(cid).recent_text(6)

        check(O_RECENT in o and A_RECENT not in o, "the owner's context is his")
        check(A_RECENT in a and O_RECENT not in a, "Alice's is hers")
        check(O_RECENT not in b and A_RECENT not in b, "Bob starts clean")


# ── G. hot artifacts and the side-effect rule ────────────────────────────────

async def test_hot_artifacts_and_zero_side_effects():
    check.section("G: 'the second one' resolves only within your own scope")
    async with boot() as nova:
        from core.turn_identity import active_turn
        store, cid = nova.runtime._artifacts, str(uuid.uuid4())

        with active_turn(OWNER()):
            store.add_result_set(conversation_id=cid, turn_id="t1", summary="drives",
                                 source_tool="web.search",
                                 items=[{"title": O_A}, {"title": O_B}])
            items = store.active_items(cid)
            check(len(items) == 2, f"the owner's result set is live ({len(items)})")
            item2 = [a for a in items if a.item_index == 2][0]
            before_n, before_t = item2.access_count, item2.last_accessed
            hit = store.resolve("tell me about the second one", cid)
            check(hit is not None and O_B in hit.summary,
                  "and he resolves the ordinal, exactly as before")

        n_after_owner = item2.access_count
        for label, who in (("Alice", ALICE()), ("Bob", BOB()),
                           ("an unknown speaker", UNKNOWN())):
            with active_turn(who):
                check(store.latest_result_set(cid) is None,
                      f"{label} sees no result set")
                check(store.active_items(cid) == [], f"{label} has no active items")
                ref = store.resolve("tell me about the second one", cid)
                check(ref is None, f"{label} cannot resolve his ordinal ({ref})")
        check(item2.access_count == n_after_owner,
              f"and none of them moved access_count ({n_after_owner} -> "
              f"{item2.access_count})")
        check(item2.last_accessed is not None, "his own touch still worked")

        # Each guest gets their own working set.
        with active_turn(ALICE()):
            store.add_result_set(conversation_id=cid, turn_id="t2", summary="hers",
                                 source_tool="web.search", items=[{"title": A_ART}])
            check(len(store.active_items(cid)) == 1, "Alice has her own result set")
        with active_turn(BOB()):
            check(store.active_items(cid) == [], "which Bob cannot see")
        with active_turn(OWNER()):
            got = [a.summary for a in store.active_items(cid)]
            check(any(O_B in g for g in got) and not any(A_ART in g for g in got),
                  f"and the owner still has his, not hers ({got})")


# ── D. summaries and story ───────────────────────────────────────────────────

async def test_summary_and_story_are_partitioned():
    check.section("D: rolling summary and story state")
    async with boot() as nova:
        from core.runtime import _conv_entity
        from core.turn_identity import active_turn
        m, cid = nova.memory, uuid.uuid4()

        with active_turn(OWNER()):
            e, es = _conv_entity(cid), _conv_entity(cid, ":story")
            check(e == f"conversation:{cid}",
                  f"the owner's summary entity is unchanged ({e})")
            await m.add_fact(entity=e, attribute="summary", value=O_SUM, confidence=0.9)
            await m.add_fact(entity=es, attribute="state", value=O_STORY, confidence=0.9)
        with active_turn(ALICE()):
            ae, aes = _conv_entity(cid), _conv_entity(cid, ":story")
            check(ae.startswith("speaker:p-alice:"), f"Alice's is under her root ({ae})")
            await m.add_fact(entity=ae, attribute="summary", value="ALICE-SUMMARY-914",
                             confidence=0.9)
            await m.add_fact(entity=aes, attribute="state", value=A_STORY, confidence=0.9)
        with active_turn(UNKNOWN()):
            ue = _conv_entity(cid)
            check(ue.startswith("unverified:"), f"unknown gets an ephemeral key ({ue})")

        async def read(who):
            with active_turn(who):
                sm = await m.get_latest_fact(entity=_conv_entity(cid), attribute="summary")
                st = await m.get_latest_fact(entity=_conv_entity(cid, ":story"),
                                             attribute="state")
            return (sm.value if sm else None, st.value if st else None)

        check(await read(OWNER()) == (O_SUM, O_STORY), "the owner reads his own")
        check(await read(ALICE()) == ("ALICE-SUMMARY-914", A_STORY), "Alice hers")
        check(await read(BOB()) == (None, None), "Bob has neither")
        check(await read(UNKNOWN()) == (None, None), "and an unknown speaker none")


# ── F. the acceptance sequence, one conversation, real runtime ───────────────

def _where(prompt: str, sentinel: str) -> str:
    """Surrounding text for a leaked sentinel, so a failure names its source."""
    j = prompt.find(sentinel)
    if j < 0:
        return ""
    return prompt[max(0, j - 160):j + 40].replace(chr(10), " ~ ")


async def test_same_conversation_acceptance():
    check.section("F: Marcus -> Alice -> Bob -> unknown -> Marcus, one conversation")
    async with boot() as nova:
        from core.turn_identity import active_turn
        rt, m, cid = nova.runtime, nova.memory, uuid.uuid4()

        async def turn(who, text):
            """The model-bound prompt for THIS turn.

            Filtered to Nova's own system prompt. The background ingest worker
            shares this LLM and drains asynchronously, so an unfiltered join
            picks up the memory EXTRACTOR echoing the previous speaker's text
            back at itself — which is that worker's own input, not anything the
            current speaker was shown.
            """
            nova.llm.reset_calls()
            with active_turn(who):
                async for _ in rt.chat_turn_stream(user_text=text, conversation_id=cid,
                                                   identity=who):
                    pass
            return " || ".join(p for p in nova.llm.prompts if "You are Nova" in p)

        # 1. Marcus seeds everything conversation-local.
        with active_turn(OWNER()):
            rt._artifacts.add_result_set(conversation_id=str(cid), turn_id="t1",
                                         summary="drives", source_tool="web.search",
                                         items=[{"title": O_A}, {"title": O_B}])
            from core.runtime import _conv_entity
            await m.add_fact(entity=_conv_entity(cid), attribute="summary",
                             value=O_SUM, confidence=0.9)
            await m.add_fact(entity=_conv_entity(cid, ":story"), attribute="state",
                             value=O_STORY, confidence=0.9)
        await turn(OWNER(), O_RECENT)

        # 2. Alice.
        a = await turn(ALICE(), "tell me about the second one")
        leaked = [s for s in OWNER_SENTINELS if s in a]
        check(not leaked, f"Alice's prompt carries ZERO owner sentinels ({leaked})")
        check("say to Marcus out loud" not in a,
              "and is not told to speak to Marcus")
        check("Marcus is referring" not in a and "He means" not in a,
              "nor that Marcus is referring to anything")
        check("Alice" in a, "she is addressed by name")

        with active_turn(ALICE()):
            rt._artifacts.add_result_set(conversation_id=str(cid), turn_id="t2",
                                         summary="hers", source_tool="web.search",
                                         items=[{"title": A_ART}])
        await turn(ALICE(), A_RECENT)

        # 3. Bob.
        b = await turn(BOB(), "what were we saying?")
        leaked = [s for s in OWNER_SENTINELS + ALICE_SENTINELS if s in b]
        check(not leaked, f"Bob's prompt carries neither his nor hers ({leaked}) {[_where(b, x) for x in leaked]}")

        # 4. unknown.
        u = await turn(UNKNOWN(), "what were we saying?")
        leaked = [s for s in OWNER_SENTINELS + ALICE_SENTINELS + (B_RECENT,) if s in u]
        check(not leaked, f"an unknown speaker gets nobody's private state ({leaked}) {[_where(u, x) for x in leaked]}")

        # 5. Marcus again — legacy context must still work.
        o = await turn(OWNER(), "remind me where we were")
        check(O_SUM in o or O_RECENT in o or O_A in o,
              "Marcus's own conversation context still reaches him")
        for s in ALICE_SENTINELS + (B_RECENT,):
            check(s not in o, f"and he does not inherit theirs ({s}) {_where(o, s)}")


# ── H. memory.superseded through the real path ───────────────────────────────

async def test_superseded_episode_is_speaker_correct():
    check.section("H: memory.superseded, BUS -> promoter -> worker -> SQLite")
    import aiosqlite

    from core.event_bus import BUS
    from core.turn_identity import active_turn

    async with boot() as nova:
        rt = nova.runtime
        with active_turn(ALICE()):
            BUS.publish("memory.superseded",
                        {"retired": 2, "because": "ALICE-BECAUSE-941 she moved"})
        await asyncio.sleep(1.5)
        await rt._promoter.stop()
        if rt._episodic_q is not None:
            await asyncio.wait_for(rt._episodic_q.join(), timeout=30)

        async with aiosqlite.connect(str(nova.memory._sqlite._db_path)) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT summary, actor_entity, privacy_scope "
                                   "FROM episodes")
            rows = [dict(r) for r in await cur.fetchall()]
        ep = next((r for r in rows if "ALICE-BECAUSE-941" in r["summary"]), None)
        check(ep is not None, f"the supersession became an episode ({rows})")
        if ep:
            check(ep["actor_entity"] == "speaker:p-alice", f"actor is Alice ({ep})")
            check(ep["privacy_scope"] == "speaker:p-alice", "privacy is Alice's")
            check("Alice" in ep["summary"], f"and she is named ({ep['summary']})")
            check("Marcus said" not in ep["summary"],
                  "with no claim that Marcus said it")


# ── I. the boundary that must not move ───────────────────────────────────────

async def test_permissions_unchanged():
    check.section("I: identity still changes no permission decision")
    import inspect

    from core.permissions import evaluate, tier_of
    from core.turn_identity import active_turn

    per_cap: dict[str, set] = {}
    for cap in ("some.destructive.capability", "memory.remember", "shell.exec"):
        for i in (OWNER(), ALICE(), BOB(), UNKNOWN()):
            with active_turn(i):
                per_cap.setdefault(cap, set()).add((tier_of(cap),
                                                    evaluate(cap, mode="guarded")))
    check(all(len(v) == 1 for v in per_cap.values()),
          f"identical across four identities ({per_cap})")
    check(not (set(inspect.signature(evaluate).parameters)
               & {"speaker", "identity", "role"}),
          "and evaluate() still takes no identity argument")


async def main():
    await test_scope_model()
    await test_conversation_state_is_partitioned()
    await test_working_context_is_partitioned()
    await test_hot_artifacts_and_zero_side_effects()
    await test_summary_and_story_are_partitioned()
    await test_same_conversation_acceptance()
    await test_superseded_episode_is_speaker_correct()
    await test_permissions_unchanged()
    check.finish()


if __name__ == "__main__":
    run(main)
