"""V3 P5.1d: the backend read/write leaks the P5.1c audit reproduced.

Each was measured on HEAD 78cba4d before being fixed. Sentinels are unique so a
"pass" cannot be an accident of substring matching.

    B  unknown speaker asked "what is my name?" -> "Your name is Zorbulon-Q7."
    C  the REAL production prompt handed a stranger "Marcus's AI companion",
       "react to what Marcus actually said", and Mateo / Liam / Leslie
    D  memory.search surfaced a private owner fact to any speaker
    H  speaker:<id> got no singleton semantics — Alice's location was Berlin
       AND Dallas at once, so a known guest's memory was structurally worse
       than Marcus's purely because she is not the owner
    I  the memory.recall TOOL returned the owner's private fact on request

D and I are the same underlying mistake: a privacy boundary enforced only in
grounding is one tool call wide, and the model can make that call.

A (frontend wiring), F (lessons/mood/wellbeing) and G (async ingest identity)
are deliberately NOT fixed here — see the report. F and G remain live gaps.

Run:  venv\\Scripts\\python.exe tests\\test_speaker_privacy_v51d.py
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

OWNER_NAME = "OWNER-ZORBULON-Q7"
OWNER_SECRET = "PLUMTREE-9931"
OWNER_SPOUSE = "OWNER-SPOUSE-6612"
OWNER_LESSON = "OWNER-LESSON-7751"
ALICE_PROFILE = "ALICE-PROFILE-8813"
SHARED_FACT = "the shared vault protocol is documented"


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


async def seed(nova):
    m = nova.memory
    await m.add_fact(entity="user", attribute="name", value=OWNER_NAME, confidence=0.98)
    await m.add_fact(entity="user", attribute="secret_note",
                     value=f"vault code {OWNER_SECRET}", confidence=0.95)
    await m.add_fact(entity="user", attribute="spouse", value=OWNER_SPOUSE, confidence=0.9)
    await m.add_fact(entity="speaker:p-alice", attribute="name", value="Alice",
                     confidence=0.95)
    await m.add_fact(entity="speaker:p-alice", attribute="secret_note",
                     value=f"vault note {ALICE_PROFILE}", confidence=0.95)
    await m.add_fact(entity="world", attribute="vault_doc", value=SHARED_FACT,
                     confidence=0.9)


# ── B. direct name read ──────────────────────────────────────────────────────

async def test_direct_name_read_is_scoped():
    check.section("B: 'what is my name?' answers about the SPEAKER")
    async with boot() as nova:
        from core.turn_identity import TurnIdentity, active_turn
        await seed(nova)
        rt = nova.runtime

        with active_turn(TurnIdentity.typed()):
            reply = await rt._direct_live_reply("What is my name?")
        check(reply and OWNER_NAME in reply[0],
              f"owner still gets his own name ({str(reply)[:60]})")

        with active_turn(ident("known", "p-alice", "Alice")):
            reply = await rt._direct_live_reply("What is my name?")
        check(reply and "Alice" in reply[0] and OWNER_NAME not in reply[0],
              f"a known guest gets THEIR name, not his ({str(reply)[:60]})")

        with active_turn(ident("unknown")):
            reply = await rt._direct_live_reply("What is my name?")
        check(reply and OWNER_NAME not in reply[0],
              "an unrecognised speaker is NOT told the owner's name")
        check(reply and ("recognise" in reply[0] or "don't know" in reply[0]),
              f"and is told Nova cannot identify them ({str(reply)[:70]})")


# ── C. the real production prompt ────────────────────────────────────────────

async def test_production_prompt_is_speaker_scoped():
    check.section("C: the ACTUAL prompt, not the helper")
    import uuid

    OWNER_MARKERS = ("Marcus's AI companion", "Marcus actually said",
                     "Mateo", "Liam", "Leslie")

    async with boot() as nova:
        from core.turn_identity import TurnIdentity, active_turn
        await seed(nova)
        rt = nova.runtime

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
        check(any(m in owner for m in OWNER_MARKERS),
              "the owner's prompt is unchanged — Nova's relationship survives")

        guest = await prompt_for(ident("known", "p-alice", "Alice"))
        leaked = [m for m in OWNER_MARKERS if m in guest]
        check(not leaked, f"a known guest gets NONE of the owner persona ({leaked})")
        check("Alice" in guest, "and is addressed by their own name")
        for sentinel in (OWNER_NAME, OWNER_SECRET, OWNER_SPOUSE):
            check(sentinel not in guest, f"owner sentinel {sentinel} absent for guest")

        unknown = await prompt_for(ident("unknown"))
        leaked = [m for m in OWNER_MARKERS if m in unknown]
        check(not leaked, f"an unknown speaker gets NONE of it either ({leaked})")
        check("does not recognise" in unknown,
              "addressee() is actually wired into the production prompt")
        for sentinel in (OWNER_NAME, OWNER_SECRET, OWNER_SPOUSE):
            check(sentinel not in unknown, f"owner sentinel {sentinel} absent for unknown")


# ── D. semantic search ───────────────────────────────────────────────────────

async def test_semantic_search_is_scoped():
    check.section("D: search cannot surface the owner's private facts")
    async with boot() as nova:
        from core.turn_identity import TurnIdentity, active_turn
        await seed(nova)

        async def hits_for(identity):
            with active_turn(identity):
                hits = await nova.memory.search(q="vault", limit=12)
            return " | ".join(h.text for h in hits)

        owner = await hits_for(TurnIdentity.typed())
        check(OWNER_SECRET in owner, "the owner still finds his own facts")

        guest = await hits_for(ident("known", "p-alice", "Alice"))
        check(OWNER_SECRET not in guest, "a guest does NOT get the owner's secret")
        check(ALICE_PROFILE in guest, "but does get their own")
        check(SHARED_FACT in guest, "and shared knowledge")

        unknown = await hits_for(ident("unknown"))
        check(OWNER_SECRET not in unknown, "unknown gets no owner secret")
        check(ALICE_PROFILE not in unknown, "and no OTHER guest's secret either")
        check(SHARED_FACT in unknown, "shared knowledge only")


async def test_search_cache_is_scoped():
    check.section("D: the result cache cannot serve one speaker's hits to another")
    async with boot() as nova:
        from core.turn_identity import TurnIdentity, active_turn
        await seed(nova)

        # Warm the cache AS THE OWNER first. This is the exact ordering that
        # made the first fix insufficient: the cached early-return skipped the
        # filter entirely, so the next guest was served his hits from disk.
        with active_turn(TurnIdentity.typed()):
            owner_hits = await nova.memory.search(q="vault", limit=12)
        check(any(OWNER_SECRET in h.text for h in owner_hits), "owner warmed the cache")

        with active_turn(ident("unknown")):
            hits = await nova.memory.search(q="vault", limit=12)
        text = " | ".join(h.text for h in hits)
        check(OWNER_SECRET not in text,
              "a cached owner result set is NOT replayed to an unknown speaker")


# ── H. person-quality memory for known speakers ──────────────────────────────

async def test_known_speaker_gets_person_semantics():
    check.section("H: a known guest's memory is not second-class")
    async with boot() as nova:
        m = nova.memory
        await m.add_fact(entity="speaker:p-alice", attribute="location",
                         value="Berlin", confidence=0.9)
        await m.add_fact(entity="speaker:p-alice", attribute="location",
                         value="Dallas", confidence=0.9)
        rows = await m.get_facts(entity="speaker:p-alice", attribute="location", limit=10)
        vals = [r.value for r in rows]
        check(vals == ["Dallas"],
              f"moving city SUPERSEDES, it does not accumulate ({vals})")

        check(m._is_singleton_fact("speaker:p-alice", "location"),
              "singleton attributes apply to a known speaker")
        check(m._is_singleton_fact("user", "location"),
              "and still apply to the owner")
        check(not m._is_singleton_fact("note", "location"),
              "without leaking to arbitrary entities")

        from memory.unifier import _is_undecayed
        check(_is_undecayed("speaker:p-alice"),
              "a known speaker's identity facts do not decay away")

        # Marcus is untouched by any of it.
        await m.add_fact(entity="user", attribute="location", value="Dallas",
                         confidence=0.9)
        owner_rows = await m.get_facts(entity="user", attribute="location", limit=10)
        check(len(owner_rows) == 1, "the owner's own singleton behaviour is unchanged")


# ── I. the recall tool ───────────────────────────────────────────────────────

async def test_memory_recall_tool_obeys_the_policy():
    check.section("I: the model cannot read around the boundary with a tool")
    async with boot() as nova:
        from core.tool_router import ToolCall
        from core.turn_identity import TurnIdentity, active_turn
        await seed(nova)
        rt = nova.runtime

        async def recall(identity, q="vault"):
            with active_turn(identity):
                res = await rt._router.execute(ToolCall("memory.recall", {"query": q}))
            return str(res.result)

        owner = await recall(TurnIdentity.typed())
        check(OWNER_SECRET in owner, "the owner's recall is unchanged")

        guest = await recall(ident("known", "p-alice", "Alice"))
        check(OWNER_SECRET not in guest, "a guest cannot recall the owner's secret")
        check(ALICE_PROFILE in guest, "but can recall their own")

        unknown = await recall(ident("unknown"))
        check(OWNER_SECRET not in unknown, "an unknown speaker gets nothing private")
        check("unverified_speaker" in unknown or ALICE_PROFILE not in unknown,
              "and is refused rather than served someone else's memory")

        # Cross-session history is the owner's, even when asked for by date.
        hist = await recall(ident("known", "p-alice", "Alice"),
                            q="what did we talk about last Tuesday")
        check(OWNER_SECRET not in hist and OWNER_NAME not in hist,
              "durable conversation history is not handed to a guest")


# ── E. the grounding signals, one sentinel each ──────────────────────────────

async def test_every_personal_grounding_signal_is_gated():
    check.section("E: each personal signal, measured rather than assumed")
    import json

    S = {"name": "SENT-NAME-01", "insight": "SENT-INSIGHT-02", "mood": "SENT-MOOD-03",
         "interest": "SENT-INTEREST-05", "person": "SENT-PERSON-10",
         "project": "SENT-PROJECT-11", "note": "SENT-NOTE-12"}

    async with boot() as nova:
        from core.turn_identity import TurnIdentity, active_turn
        m, rt = nova.memory, nova.runtime

        await m.add_fact(entity="user", attribute="name", value=S["name"], confidence=.98)
        await m.add_fact(entity="user", attribute="spouse", value=S["person"], confidence=.9)
        await m.add_fact(entity="user", attribute="note", value=S["note"], confidence=.9)
        await m.add_fact(entity="user", attribute="interest", value=S["interest"], confidence=.9)
        await m.add_fact(entity="insight", attribute="pattern", value=S["insight"], confidence=.8)
        await m.add_fact(entity="projects", attribute="last_active", value=S["project"], confidence=.9)
        # The active-project pointer is only honoured for a project that actually
        # EXISTS — `ProjectBuilder.last_active()` verifies it now, so a pointer
        # with nothing behind it is treated as stale and returns None. This
        # fixture has to reflect that, otherwise the project sentinel simply
        # never reaches grounding and the privacy claim below is untested.
        #
        # "Exists" means a real project, not a bare directory: Stage 13A gave
        # the codebase ONE definition and PROJECT.md is it. A mkdir alone used
        # to be enough here only because `last_active()` was the single surface
        # that accepted any directory.
        _proj = nova.runtime._projects_dir / S["project"]
        _proj.mkdir(parents=True, exist_ok=True)
        (_proj / "PROJECT.md").write_text("# " + S["project"] + chr(10), encoding="utf-8")
        with active_turn(TurnIdentity.typed()):
            await m.record_mood(S["mood"])

        async def ground(identity):
            with active_turn(identity):
                out = await rt._build_grounding_context(
                    user_text="tell me about my life", user_name=S["name"],
                    available_tools=[], conversation_id=None)
            return out if isinstance(out, str) else json.dumps(out, default=str)

        owner = await ground(TurnIdentity.typed())
        got = sorted(k for k, v in S.items() if v in owner)
        # The owner keeps every one of them; a "fix" that just deletes the
        # feature for everybody would otherwise pass this file.
        check(len(got) >= 6, f"the owner still receives his full profile ({got})")

        for label, i in (("known guest", ident("known", "p-alice", "Alice")),
                         ("unknown speaker", ident("unknown"))):
            blob = await ground(i)
            leaked = sorted(k for k, v in S.items() if v in blob)
            check(not leaked, f"a {label} receives none of them ({leaked})")

        # And the two that had no gate at all before this phase, named.
        for i in (ident("known", "p-alice", "Alice"), ident("unknown")):
            blob = await ground(i)
            check(S["insight"] not in blob, "noticed_patterns is gated")
            check(S["project"] not in blob, "current_focus is gated")


# ── §17. one whole turn, end to end, nothing stubbed in the middle ───────────

GUEST_SECRET = "FERNBLOOM-4417"


async def test_full_guest_turn_leaks_nothing_either_way():
    check.section("17: a complete guest turn, through the real path")
    import asyncio
    import uuid
    from datetime import datetime, timezone

    async with boot() as nova:
        from core.speaker.matcher import SpeakerMatch
        from core.speaker.voice_turns import VOICE_TURNS
        from core.turn_identity import TurnIdentity, active_turn
        from core.workers.memory_ingest import MemoryIngestWorker
        import backend.app as app

        await seed(nova)
        rt = nova.runtime

        # Identity comes from a real minted handle resolved by the real backend
        # helper — not a hand-built TurnIdentity. If the handle path ever stops
        # producing a guest, this test stops testing anything, so assert it.
        handle = VOICE_TURNS.issue(SpeakerMatch(status="known", profile_id="p-alice",
                                                display_name="Alice", attempted=True))
        guest = await app._resolve_turn_identity(handle, "voice")
        check(guest.memory_entity == "speaker:p-alice",
              f"the handle resolved to the guest ({guest.memory_entity})")

        nova.llm.reset_calls()
        cid = uuid.uuid4()
        said = f"my wife is {GUEST_SECRET} and we live in Berlin"
        with active_turn(guest):
            async for _ in rt.chat_turn_stream(user_text=said, conversation_id=cid,
                                               identity=guest):
                pass

        # Nothing of Marcus's reached the guest's prompt.
        prompts = " || ".join(nova.llm.prompts)
        for sentinel in (OWNER_NAME, OWNER_SECRET, OWNER_SPOUSE):
            check(sentinel not in prompts, f"{sentinel} never reached the guest")

        # Now drain the ASYNC path the same way production does: whatever the
        # turn queued, handed to the real worker with an extractor that behaves
        # like the real one (first person -> entity="user").
        class _F:
            entity, attribute, value = "user", "spouse", GUEST_SECRET
            confidence, persist = 0.9, True

        class _Out:
            facts = [_F()]

        class _Sem:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

        class _Ex:
            _sem, _llm = _Sem(), None
            async def extract(self, user_text): return _Out()

        class _Sm:
            async def summarize(self, transcript): raise AssertionError

        worker = MemoryIngestWorker(memory=nova.memory, extractor=_Ex(), summarizer=_Sm(),
                                    state=rt._state_store, queue=asyncio.Queue(),
                                    summarize_queue=asyncio.Queue())
        drained = 0
        while not rt._memory_ingest_q.empty():
            ev = rt._memory_ingest_q.get_nowait()
            check(ev.identity is not None and ev.identity.profile_id == "p-alice",
                  "the queued event carries the guest, not the default")
            await worker._handle_ingest(ev)
            drained += 1
        check(drained >= 1, f"the turn actually queued an ingest event ({drained})")

        owner_rows = await nova.memory.get_facts(entity="user", attribute="spouse", limit=10)
        check(all(GUEST_SECRET not in (r.value or "") for r in owner_rows),
              f"the guest's spouse never became Marcus's ({[r.value for r in owner_rows]})")
        check(any(r.value == OWNER_SPOUSE for r in owner_rows),
              "and his real one is still there")
        guest_rows = await nova.memory.get_facts(entity="speaker:p-alice",
                                                 attribute="spouse", limit=10)
        check([r.value for r in guest_rows] == [GUEST_SECRET],
              f"it was filed under the guest ({[r.value for r in guest_rows]})")

        # Finally: Marcus comes back. He must not be told about her.
        nova.llm.reset_calls()
        with active_turn(TurnIdentity.typed()):
            async for _ in rt.chat_turn_stream(user_text="who is my wife?",
                                               conversation_id=uuid.uuid4(),
                                               identity=TurnIdentity.typed()):
                pass
            hits = await nova.memory.search(q="wife spouse Berlin", limit=12)
        owner_prompts = " || ".join(nova.llm.prompts)
        check(f"user spouse = {OWNER_SPOUSE}" in owner_prompts
              or OWNER_SPOUSE in owner_prompts,
              "Marcus's own grounding still works")
        # The owner CAN read a guest's namespace. It is his machine and his
        # memory, and the threat this phase addresses is a guest reaching HIS
        # data, not the reverse. Asserted so it stays a stated decision rather
        # than drifting into a surprise later.
        check(any(GUEST_SECRET in h.text for h in hits),
              "the owner can still see what was stored for a guest (by design)")


# ── the boundary that must not move ──────────────────────────────────────────

async def test_permissions_unchanged():
    check.section("identity still changes no permission decision")
    import inspect

    from core.permissions import evaluate, tier_of
    from core.turn_identity import TurnIdentity, active_turn

    cap = "some.destructive.capability"
    seen = set()
    for i in (TurnIdentity.typed(), ident("known", "p-m", "Marcus", "owner"),
              ident("known", "p-a", "Alice"), ident("unknown")):
        with active_turn(i):
            seen.add((tier_of(cap), evaluate(cap, mode="guarded")))
    check(len(seen) == 1, f"every speaker gets the identical decision ({seen})")
    check(not (set(inspect.signature(evaluate).parameters)
               & {"speaker", "identity", "role"}),
          "and evaluate() still takes no identity argument")


async def main():
    await test_direct_name_read_is_scoped()
    await test_production_prompt_is_speaker_scoped()
    await test_semantic_search_is_scoped()
    await test_search_cache_is_scoped()
    await test_known_speaker_gets_person_semantics()
    await test_memory_recall_tool_obeys_the_policy()
    await test_every_personal_grounding_signal_is_gated()
    await test_full_guest_turn_leaks_nothing_either_way()
    await test_permissions_unchanged()
    check.finish()


if __name__ == "__main__":
    run(main)
