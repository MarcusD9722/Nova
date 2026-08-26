"""Concurrent turns must not inherit each other's speaker (Stage 13B entry gate).

WHY THIS EXISTS. `test_step9_privacy_v52.py` flaked about 1 run in 10 on an
unverified speaker "reaching the model", and the reported suspicion was that
identity resolution intermittently stopped being unverified. Instrumented, that
was not what happened: the guard was taken correctly every time (in_turn=True,
unverified=True, memory_entity=None), and the counted model call was the
background memory-ingest EXTRACTOR draining an earlier turn, attributed to the
foreground turn by a global counter.

"The identity was fine in a sequential test" is not the same claim as "the
identity is fine under concurrency", and the ContextVar isolation that makes the
second claim true had no direct test. This is that test.

HOW PROMPTS ARE ATTRIBUTED, and why it matters more than it sounds.
`nova.llm.prompts` is ONE list shared by every turn and by the background
workers. Slicing it by index attributes whatever landed in the window to
whichever turn was measuring — which under concurrency is nonsense. Writing this
suite the naive way produced a confident, reproducible, entirely fake "the
guest's private fact leaked into the owner's prompt": the captured text was the
GUEST's own system prompt, recognisable because it carried the guest-facing line
"never share Marcus's personal information ... with them".

The model call happens INSIDE the turn's context, so `current_identity()` at
call time is the correct attribution key. That is the same defect class as the
flake this suite was written to close, which is why it is spelled out here.

Run:  venv\\Scripts\\python.exe tests\\test_identity_context_isolation_s13b.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections import defaultdict
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, boot, run  # noqa: E402

from core.turn_identity import current_identity  # noqa: E402

check = Checks()

OWNER_CANARY = "sourdough starter named Bruce"
GUEST_CANARY = "allergic to shellfish"
GUEST_PID = "p-leslie"
UNVERIFIED = "unverified"


class PromptLog:
    """Chat prompts, bucketed by the identity that was active when asked."""

    def __init__(self) -> None:
        self.by_identity: dict[str, list[str]] = defaultdict(list)

    def handler(self, prompt: str) -> str:
        if "You are Nova" in prompt:
            ident = current_identity()
            self.by_identity[ident.memory_entity or UNVERIFIED].append(prompt)
        return "sure."

    def clear(self) -> None:
        self.by_identity.clear()

    def text(self, key: str) -> str:
        return "\n".join(self.by_identity.get(key, []))

    def count(self, key: str) -> int:
        return len(self.by_identity.get(key, []))


def _ident_typed():
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


async def _seed(nova) -> None:
    await nova.memory.add_fact(entity="user", attribute="hobby",
                               value=OWNER_CANARY, confidence=0.95)
    await nova.memory.add_fact(entity=f"speaker:{GUEST_PID}",
                               attribute="allergy", value=GUEST_CANARY,
                               confidence=0.95)


async def test_concurrent_turns_keep_their_own_speaker():
    check.section("identity: concurrent turns keep their own speaker")

    log = PromptLog()
    async with boot() as nova:
        nova.llm.when(lambda _p: True, log.handler, label="tagging")
        await _seed(nova)
        q = "What do you remember about me?"

        for round_no in range(4):
            log.clear()
            await asyncio.gather(
                nova.brain.chat(q, conversation_id=str(uuid4()),
                                identity=_ident_typed()),
                nova.brain.chat(q, conversation_id=str(uuid4()),
                                identity=_ident_guest()),
                nova.brain.chat(q, conversation_id=str(uuid4()),
                                identity=_ident_unverified()),
            )
            owner = log.text("user")
            guest = log.text(f"speaker:{GUEST_PID}")

            check(log.count("user") == 1 and log.count(f"speaker:{GUEST_PID}") == 1,
                  f"[{round_no}] each speaker asked the model exactly once "
                  f"(owner={log.count('user')} guest="
                  f"{log.count(f'speaker:{GUEST_PID}')})")
            check(OWNER_CANARY in owner,
                  f"[{round_no}] the owner's turn gets his own fact")
            check(GUEST_CANARY not in owner,
                  f"[{round_no}] and NOT the guest's")
            check(GUEST_CANARY in guest,
                  f"[{round_no}] the guest's turn gets hers")
            check(OWNER_CANARY not in guest,
                  f"[{round_no}] and NOT the owner's")
            check(log.count(UNVERIFIED) == 0,
                  f"[{round_no}] the unverified turn asked the model nothing "
                  f"({log.count(UNVERIFIED)})")


async def test_a_shared_conversation_id_does_not_merge_speakers():
    """The nastier shape: same conversation, different speakers, at once.

    A per-conversation cache that ignored identity would hand one speaker the
    other's material here and nowhere else.
    """
    check.section("identity: one conversation, three speakers, no merging")

    log = PromptLog()
    async with boot() as nova:
        nova.llm.when(lambda _p: True, log.handler, label="tagging")
        await _seed(nova)
        shared = str(uuid4())

        for round_no in range(4):
            log.clear()
            await asyncio.gather(
                nova.brain.chat("What do you remember about me?",
                                conversation_id=shared, identity=_ident_typed()),
                nova.brain.chat("What do you remember about me?",
                                conversation_id=shared, identity=_ident_guest()),
                nova.brain.chat("Tell me everything you know about me.",
                                conversation_id=shared,
                                identity=_ident_unverified()),
            )
            owner = log.text("user")
            guest = log.text(f"speaker:{GUEST_PID}")
            check(GUEST_CANARY not in owner,
                  f"[{round_no}] the guest's fact stayed out of the owner's turn")
            check(OWNER_CANARY not in guest,
                  f"[{round_no}] the owner's fact stayed out of the guest's turn")
            check(log.count(UNVERIFIED) == 0,
                  f"[{round_no}] and the unverified speaker still asked nothing")


async def test_a_background_model_call_is_not_a_turns_call():
    """The measurement lesson, pinned.

    The privacy suite counted every entry on the shared scripted model, so the
    ingest worker's extractor looked like the guarded turn asking a question.
    This asserts the two are distinguishable — which is what makes the corrected
    assertion meaningful rather than merely quieter.
    """
    check.section("identity: a background model call is not a turn's call")

    log = PromptLog()
    async with boot() as nova:
        nova.llm.when(lambda _p: True, log.handler, label="tagging")
        await _seed(nova)
        cid = str(uuid4())

        await nova.brain.chat("My favourite tea is lapsang.",
                              conversation_id=cid, identity=_ident_typed())
        log.clear()
        await nova.brain.chat("Tell me everything you know about me.",
                              conversation_id=cid, identity=_ident_unverified())
        check(log.count(UNVERIFIED) == 0,
              f"the guarded turn produced no chat prompt ({log.count(UNVERIFIED)})")

        worker = getattr(nova.runtime, "_memory_worker", None)
        if worker is not None:
            try:
                await worker._drain_queue_for_shutdown(budget_s=20.0)
            except Exception:
                pass
        non_chat = [p for p in nova.llm.prompts if "You are Nova" not in p]
        check(bool(non_chat),
              "and background model work DID happen, so the distinction is real")


async def main():
    await test_concurrent_turns_keep_their_own_speaker()
    await test_a_shared_conversation_id_does_not_merge_speakers()
    await test_a_background_model_call_is_not_a_turns_call()
    check.finish()


if __name__ == "__main__":
    run(main)
