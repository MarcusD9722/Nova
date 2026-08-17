"""Step 9 closure: retry contamination, and what an unverified turn can see.

Human run 2's step 9 produced two things worth chasing rather than assuming.

1. The retry reused the PREVIOUS attempt's sentinel conversation id, and
   `ConversationStateStore` keeps state for seven days. So the "new" trial ran
   on top of an old one. Both replies quoted `guest live 661` — a token from the
   earlier attempt, not from anything said during this one.

2. The unverified speaker's reply mentioned "your family goals and Cyberpunk
   story". That is either a real read-path leak or a 9B model confabulating an
   excuse for having no context. Those two have identical symptoms in a
   transcript and completely different consequences, so this file answers it by
   inspecting the EXACT prompt rather than the reply.

`tests/test_speaker_live_v51e.py` §22 already seeds facts, lessons, thoughts and
episodes. It does NOT cover the two surfaces implicated here: the rolling
CONVERSATION SUMMARY and the RECENT CONVERSATION STATE of a prior conversation.
Those are what this file adds.

Run:  venv\\Scripts\\python.exe tests\\test_step9_privacy_v52.py
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

# Unmistakable. Nothing a language model would emit by chance, so a hit is
# evidence of a read path and never of a coincidence.
OWNER_FAMILY = "OWNER_PRIVATE_FAMILY_CANARY_X7"
OWNER_STORY = "OWNER_PRIVATE_STORY_CANARY_Q9"
OWNER_SUMMARY = "OWNER_PRIVATE_SUMMARY_CANARY_R3"
OWNER_RECENT = "OWNER_PRIVATE_RECENT_CANARY_T5"
GUEST_PRIVATE = "GUEST_PRIVATE_CANARY_L4"
ALL_PRIVATE = (OWNER_FAMILY, OWNER_STORY, OWNER_SUMMARY, OWNER_RECENT, GUEST_PRIVATE)

GUEST_PID = "p-leslie"


def _ident_owner():
    from core.turn_identity import TurnIdentity
    return TurnIdentity.typed()


def _ident_guest():
    from core.speaker.matcher import STATUS_KNOWN, SpeakerMatch
    from core.turn_identity import TurnIdentity
    return TurnIdentity.from_match(
        SpeakerMatch(status=STATUS_KNOWN, profile_id=GUEST_PID,
                     display_name="Leslie", attempted=True), profile=None)


def _ident_unverified():
    from core.turn_identity import TurnIdentity
    return TurnIdentity.voice_unverified("not recognised")


async def _turn(nova, ident, text, cid):
    """One real turn. `identity=` is how backend/app.py passes it — the runtime
    enters active_turn() itself, and active_turn(None) would reset to the typed
    OWNER, silently overriding any wrapper applied from outside."""
    nova.llm.reset_calls()
    await nova.brain.chat(text, conversation_id=cid, identity=ident)
    return "\n||\n".join(p for p in nova.llm.prompts if "You are Nova" in p)


# ── 1. retry contamination ───────────────────────────────────────────────────

async def test_a_new_conversation_cannot_see_the_old_one():
    check.section("retry: a fresh conversation id isolates a re-run")
    async with boot(default_reply="Noted.") as nova:
        owner, guest = _ident_owner(), _ident_guest()

        # Conversation A — the abandoned first attempt.
        conv_a = uuid4()
        await _turn(nova, owner, "Remember these three words: OLD OWNER ALPHA.", conv_a)
        await _turn(nova, guest, "Remember these three words: OLD GUEST BETA.", conv_a)

        # Conversation B — the re-run, with a NEW id.
        conv_b = uuid4()
        await _turn(nova, owner, "Remember these three words: NEW OWNER GAMMA.", conv_b)
        await _turn(nova, guest, "Remember these three words: NEW GUEST DELTA.", conv_b)
        pb_owner = await _turn(nova, owner, "What three words did I ask you to remember?", conv_b)
        pb_guest = await _turn(nova, guest, "What three words did I ask you to remember?", conv_b)

        check("NEW OWNER GAMMA" in pb_owner, "the owner's B prompt has his B words")
        check("OLD OWNER ALPHA" not in pb_owner,
              "and NOT his words from conversation A — no carry-over")
        check("NEW GUEST DELTA" not in pb_owner, "nor the guest's B words")
        check("OLD GUEST BETA" not in pb_owner, "nor the guest's A words")

        check("NEW GUEST DELTA" in pb_guest, "the guest's B prompt has her B words")
        check("OLD GUEST BETA" not in pb_guest, "and not her A words")
        check("NEW OWNER GAMMA" not in pb_guest, "nor the owner's — no leak")
        check("OLD OWNER ALPHA" not in pb_guest, "in either conversation")

        # And A is still intact, so a new id isolates rather than destroys.
        pa_owner = await _turn(nova, owner, "What three words did I ask you to remember?", conv_a)
        check("OLD OWNER ALPHA" in pa_owner,
              "conversation A still holds its own history — isolation, not deletion")
        check("NEW OWNER GAMMA" not in pa_owner, "and has not absorbed B")


# ── 2. what an unverified turn can actually see ──────────────────────────────

async def _seed_private(nova):
    """Every surface production grounding can read, including the two the
    existing §22 test does not cover: the rolling summary and prior recent
    conversation state."""
    from core.turn_identity import active_turn
    from memory.episodes import Episode, EpisodicStore

    m = nova.memory
    owner, guest = _ident_owner(), _ident_guest()

    await m.add_fact(entity="user", attribute="family_goal", value=OWNER_FAMILY,
                     confidence=0.95)
    await m.add_fact(entity="user", attribute="story_project", value=OWNER_STORY,
                     confidence=0.95)
    await m.add_fact(entity=f"speaker:{GUEST_PID}", attribute="note",
                     value=GUEST_PRIVATE, confidence=0.9)
    with active_turn(owner):
        await m.add_lesson(f"Marcus prefers {OWNER_FAMILY} framing", topic="preference")
        await m.note_thought("note", OWNER_STORY, topic="writing")

    store = EpisodicStore(Path(m._sqlite._db_path))
    await store.record_episode(Episode(
        id="ep-priv", kind="selection", summary=f"Marcus chose {OWNER_STORY}",
        entities=[OWNER_STORY], speaker_entity="user", speaker_label="Marcus",
        actor_entity="user", privacy_scope="user"))

    # THE ROLLING CONVERSATION SUMMARY — read via _conv_entity() on every turn.
    from core.runtime import _conv_entity
    prior = uuid4()
    with active_turn(owner):
        ent = _conv_entity(prior)
        await m.add_fact(entity=ent, attribute="summary",
                         value=f"Marcus and Nova discussed {OWNER_SUMMARY}",
                         confidence=0.9)
    # PRIOR RECENT CONVERSATION STATE in the owner's scope.
    await _turn(nova, owner, f"Please remember {OWNER_RECENT} for later.", prior)
    return prior


async def test_unverified_prompt_contains_no_private_canary():
    check.section("unverified: the EXACT prompt, not the reply")
    async with boot(default_reply="I'm not sure who I'm speaking with.") as nova:
        prior = await _seed_private(nova)
        unver = _ident_unverified()

        # Questions that REACH the model — self-history phrasings are now
        # short-circuited by the guard and would produce no prompt to inspect.
        # These still pull full grounding, so a leak would show.
        p_same = await _turn(nova, unver, "What should I work on today?", prior)
        p_new = await _turn(nova, unver, "Give me a useful summary.", uuid4())

        for label, prompt in (("same conversation", p_same), ("new conversation", p_new)):
            check(bool(prompt.strip()), f"{label}: a prompt was captured")
            leaked = [c for c in ALL_PRIVATE if c in prompt]
            check(not leaked, f"{label}: NO private canary in the prompt ({leaked})")

        # The owner's own context still works — this must be isolation, not a
        # blanket emptying of grounding.
        p_owner = await _turn(nova, _ident_owner(), "What should I work on today?", prior)
        seen = [c for c in (OWNER_FAMILY, OWNER_STORY, OWNER_SUMMARY, OWNER_RECENT)
                if c in p_owner]
        check(seen, f"the OWNER still receives his own context ({seen})")
        check(GUEST_PRIVATE not in p_owner, "but not the guest's private note")


async def test_guest_prompt_contains_no_owner_canary():
    check.section("known guest: hers, never his")
    async with boot(default_reply="Noted.") as nova:
        prior = await _seed_private(nova)
        p_guest = await _turn(nova, _ident_guest(), "What do you remember about me?", prior)
        leaked = [c for c in (OWNER_FAMILY, OWNER_STORY, OWNER_SUMMARY, OWNER_RECENT)
                  if c in p_guest]
        check(not leaked, f"a recognised guest gets NO owner canary ({leaked})")


async def test_unverified_answer_does_not_fabricate_history():
    check.section("unverified: no invented personal history in the answer")
    # The live reply named "family goals" and a "Cyberpunk story". Whatever the
    # source, an unverified speaker must not be TOLD specifics about anyone. The
    # general behaviour is what is asserted — no hardcoded topic words.
    async with boot() as nova:
        prior = await _seed_private(nova)
        # Model-reaching questions of several shapes: the system prompt must
        # never hand the model private specifics to talk about.
        for question in ("What should I work on today?",
                         "Give me a useful summary.",
                         "Any suggestions for me?"):
            prompt = await _turn(nova, _ident_unverified(), question, prior)
            leaked = [c for c in ALL_PRIVATE if c in prompt]
            check(not leaked, f"'{question[:28]}…' leaks nothing ({leaked})")

        # The prompt must POSITIVELY instruct against invention, so that on the
        # ungated paths a fabricated history is the model disobeying rather than
        # the system inviting it.
        prompt = await _turn(nova, _ident_unverified(), "Any suggestions for me?", prior)
        check(bool(prompt.strip()), "this question does reach the model")
        low = prompt.lower()
        check("does not recognise" in low or "does not recognize" in low,
              "the prompt says the voice is not recognised")
        check("do not assume this is marcus" in low,
              "and forbids assuming it is Marcus")
        check("do not guess" in low,
              "and explicitly forbids guessing personal details")
        check("you do not know this person's personal details" in low,
              "stating plainly that Nova does not know this person")
        # The owner's system prompt is a DIFFERENT one, not the same text with
        # facts removed — worth pinning, because a regression that fell back to
        # the owner prompt would still pass a canary check on an empty memory.
        owner_prompt = await _turn(nova, _ident_owner(),
                                   "Tell me everything you know about me.", prior)
        check("marcus's ai companion" in owner_prompt.lower(),
              "while the owner still gets his own system prompt")
        check("does not recognise" not in owner_prompt.lower(),
              "and is not told his own voice is unrecognised")


async def _reply(nova, ident, text, cid):
    """The assistant text, plus how many model calls it took."""
    nova.llm.reset_calls()
    out = await nova.brain.chat(text, conversation_id=cid, identity=ident)
    txt = getattr(out, "assistant_text", None) or getattr(out, "reply", None) or str(out)
    return txt, len(nova.llm.prompts)


async def test_unverified_self_history_response_is_deterministic():
    check.section("H: the RESPONSE, not just the prompt — and no model call")
    # The prompt is already clean (proven above). The real Qwen still produced
    # "your family goals and Cyberpunk story", because a small model asked an
    # unanswerable question invents an answer. A confabulated history is
    # indistinguishable from a leak to the person hearing it, so this path does
    # not ask the model at all.
    from uuid import uuid4

    async with boot(default_reply="MODEL_WAS_CALLED_AND_SHOULD_NOT_HAVE") as nova:
        prior = await _seed_private(nova)
        unver = _ident_unverified()

        questions = ["What do you remember about me?",
                     "What did we talk about before?",
                     "Tell me everything you know about me.",
                     "Do you remember me?",
                     "Repeat my three words.",
                     "What three words did I ask you to remember?"]
        for q in questions:
            txt, calls = await _reply(nova, unver, q, prior)
            check(calls == 0, f"'{q[:30]}…' made NO model call ({calls})")
            check("MODEL_WAS_CALLED" not in txt,
                  f"'{q[:30]}…' did not reach the model")
            leaked = [c for c in ALL_PRIVATE if c in txt]
            check(not leaked, f"'{q[:30]}…' response leaks nothing ({leaked})")
            low = txt.lower()
            check("recognise" in low or "recognize" in low,
                  f"and says it cannot place the voice ({txt[:70]!r})")

        # An ordinary question from the same unknown speaker is UNCHANGED.
        txt, calls = await _reply(nova, unver, "What is the capital of France?", prior)
        check(calls >= 1,
              f"an ordinary question still reaches the model ({calls} calls)")

        # And a question about a NON-personal topic is not swallowed.
        txt, calls = await _reply(nova, unver,
                                  "What do you remember about the storage drives?", prior)
        check(calls >= 1,
              f"'what do you remember about <topic>' is not caught ({calls} calls)")


async def test_known_speakers_are_untouched_by_the_guard():
    check.section("H: Marcus and a recognised guest behave exactly as before")
    from uuid import uuid4

    async with boot(default_reply="Sure.") as nova:
        cid = uuid4()
        for label, ident in (("Marcus", _ident_owner()), ("Leslie", _ident_guest())):
            for q in ("What do you remember about me?", "Repeat my three words."):
                _txt, calls = await _reply(nova, ident, q, cid)
                check(calls >= 1,
                      f"{label}: '{q[:26]}…' still goes to the model ({calls})")

        from core.runtime import _looks_like_self_history_query
        # The matcher itself is narrow: it must not swallow ordinary requests.
        for q in ("What do you remember about me?", "Do you remember me?",
                  "Repeat my three words.", "What did we talk about before?"):
            check(_looks_like_self_history_query(q), f"matches: {q!r}")
        for q in ("What do you remember about the storage drives?",
                  "Remember these three words: BLUE TIGER SPOON.",
                  "What's the weather?", "Do you remember how to build a project?",
                  "What did we talk about regarding Python?"):
            hit = _looks_like_self_history_query(q)
            check(not hit or "talk about" in q.lower(),
                  f"does not over-match: {q!r} -> {hit}")


async def test_durable_ingestion_cannot_cross_first_person_canaries():
    check.section("ADVERSARIAL: after the ingest worker has durably indexed both")
    # Hot conversation state is already proven isolated. This is the harder
    # question: MemoryUnifier deliberately returns ALL semantic hits for the
    # OWNER, while a guest is filtered to their own speaker entity. Once the
    # background worker has durably indexed BOTH speakers' turns, does a
    # first-person question ("my three words") still resolve to the asker?
    from uuid import uuid4

    OWNER_C = "OWNER_DURABLE_CANARY_M8"
    GUEST_C = "GUEST_DURABLE_CANARY_N2"

    async with boot(default_reply="Noted.") as nova:
        owner, guest = _ident_owner(), _ident_guest()
        cid = uuid4()

        await _turn(nova, owner, f"Remember these three words: {OWNER_C}.", cid)
        await _turn(nova, guest, f"Remember these three words: {GUEST_C}.", cid)

        # FORCE the real worker to finish, so the ask happens against durable
        # memory rather than only the hot store.
        worker = nova.runtime._memory_worker
        await worker._drain_queue_for_shutdown(budget_s=20.0)
        check(worker._q.empty(), f"the ingest queue drained ({worker._q.qsize()} left)")

        p_owner = await _turn(nova, owner, "Repeat my three words.", cid)
        p_guest = await _turn(nova, guest, "Repeat my three words.", cid)

        check(OWNER_C in p_owner, "the owner's prompt still has HIS canary")
        check(GUEST_C not in p_owner,
              f"and NOT the guest's, after durable ingestion "
              f"({'LEAK' if GUEST_C in p_owner else 'clean'})")
        check(GUEST_C in p_guest, "the guest's prompt has HERS")
        check(OWNER_C not in p_guest, "and not the owner's")

        # Also on a FRESH conversation, where hot state cannot be the source and
        # durable retrieval is the only possible path.
        fresh = uuid4()
        f_owner = await _turn(nova, owner, "Repeat my three words.", fresh)
        f_guest = await _turn(nova, guest, "Repeat my three words.", fresh)
        check(GUEST_C not in f_owner,
              "on a fresh conversation the owner still gets no guest canary")
        check(OWNER_C not in f_guest,
              "and the guest gets no owner canary")

        # And an unverified speaker gets neither, from either source.
        f_unver = await _turn(nova, _ident_unverified(), "Repeat my three words.", cid)
        check(OWNER_C not in f_unver and GUEST_C not in f_unver,
              "an unverified speaker gets neither, durable or hot")


async def test_conversation_summary_is_speaker_scoped():
    check.section("the rolling summary is scoped, not shared")
    async with boot() as nova:
        from core.runtime import _conv_entity
        from core.turn_identity import active_turn

        cid = uuid4()
        with active_turn(_ident_owner()):
            owner_ent = _conv_entity(cid)
        with active_turn(_ident_guest()):
            guest_ent = _conv_entity(cid)
        with active_turn(_ident_unverified()):
            unver_ent = _conv_entity(cid)

        check(owner_ent == f"conversation:{cid}", f"owner keeps the bare key ({owner_ent})")
        check(guest_ent == f"speaker:{GUEST_PID}:conversation:{cid}",
              f"the guest is under her own root ({guest_ent})")
        check(unver_ent is None,
              f"and an unverified turn has NO durable summary key ({unver_ent})")
        check(owner_ent != guest_ent, "so a summary cannot cross between them")


async def main():
    await test_a_new_conversation_cannot_see_the_old_one()
    await test_unverified_prompt_contains_no_private_canary()
    await test_guest_prompt_contains_no_owner_canary()
    await test_unverified_answer_does_not_fabricate_history()
    await test_unverified_self_history_response_is_deterministic()
    await test_known_speakers_are_untouched_by_the_guard()
    await test_durable_ingestion_cannot_cross_first_person_canaries()
    await test_conversation_summary_is_speaker_scoped()
    check.finish()


if __name__ == "__main__":
    run(main)
