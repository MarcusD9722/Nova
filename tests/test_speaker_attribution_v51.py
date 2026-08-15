"""V3 P5.1: speaker identity actually changes what Nova writes and reads.

The failure this suite exists to prevent, stated plainly:

    A guest says "my name is Alex" and Nova rewrites MARCUS's name.

Everything else here is a variation on that. Until this phase, `entity="user"`
meant Marcus because only Marcus could speak; once a guest can, the assumption
files a stranger's statements into his profile.

Two boundaries, both fail-closed:

    WRITE   an unverified voice turn has no personal-memory target at all,
            and a known non-owner writes only to `speaker:<profile_id>`.
    READ    Marcus's family, profile and personal history are loaded only for
            a turn that is actually his.

Blocking writes alone would be half a boundary: reciting his private profile to
whoever is standing there leaks it just as thoroughly.

NOT AUTHENTICATION. A recognised owner gets exactly the permission decision
typed Marcus gets, and a test asserts it.

Run:  venv\\Scripts\\python.exe tests\\test_speaker_attribution_v51.py
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


def ident_known(profile_id="p-alice", name="Alice", role="guest"):
    from core.speaker.matcher import SpeakerMatch
    from core.turn_identity import TurnIdentity

    class _P:
        pass
    prof = _P()
    prof.role = role
    return TurnIdentity.from_match(
        SpeakerMatch(status="known", profile_id=profile_id, display_name=name,
                     similarity=0.9, attempted=True), profile=prof)


def ident_status(status):
    from core.speaker.matcher import SpeakerMatch
    from core.turn_identity import TurnIdentity
    return TurnIdentity.from_match(SpeakerMatch(status=status, attempted=True))


# ── 1. the attribution matrix ────────────────────────────────────────────────

async def test_attribution_matrix():
    check.section("the contract: who may write where")
    from core.turn_identity import TurnIdentity

    cases = [
        ("typed", TurnIdentity.typed(), "user"),
        ("voice, speaker ID disabled", TurnIdentity.voice_legacy(), "user"),
        ("voice, known OWNER", ident_known("p-m", "Marcus", "owner"), "user"),
        ("voice, known non-owner", ident_known("p-alice", "Alice", "guest"),
         "speaker:p-alice"),
        ("voice, unknown", ident_status("unknown"), None),
        ("voice, ambiguous", ident_status("ambiguous"), None),
        ("voice, too_short", ident_status("too_short"), None),
        ("voice, unavailable", ident_status("unavailable"), None),
        ("voice, handle not redeemed", TurnIdentity.voice_unverified(), None),
    ]
    for label, ident, expected in cases:
        got = ident.memory_entity
        check(got == expected, f"{label:<28} -> {got!r} (expected {expected!r})")

    owner = ident_known("p-m", "Marcus", "owner")
    check(owner.is_owner and not owner.is_known_other, "owner is owner")
    alice = ident_known()
    check(alice.is_known_other and not alice.is_owner, "a guest is not the owner")
    check(alice.may_write_personal, "but may still write to their own namespace")
    check(ident_status("unknown").is_unverified, "unknown is unverified")


async def test_role_comes_from_the_profile_not_the_request():
    check.section("stored role is not client-assertable")
    from core.speaker.matcher import SpeakerMatch
    from core.turn_identity import TurnIdentity

    # A match with no profile looked up: role is unknown, so the speaker is
    # NOT treated as owner even though they are recognised.
    m = SpeakerMatch(status="known", profile_id="p-x", display_name="Marcus",
                     attempted=True)
    ident = TurnIdentity.from_match(m, profile=None)
    check(ident.memory_entity == "speaker:p-x",
          f"a display name of 'Marcus' does not confer owner ({ident.memory_entity})")
    check(not ident.is_owner, "role must come from the durable profile")


# ── 2. handle integrity at the chat boundary ─────────────────────────────────

async def test_handle_resolution():
    check.section("every handle failure resolves to unverified, never to Marcus")
    async with boot() as nova:
        import backend.app as app
        from core.speaker.matcher import SpeakerMatch
        from core.speaker.voice_turns import VOICE_TURNS

        good = VOICE_TURNS.issue(SpeakerMatch(status="known", profile_id="p-m",
                                              display_name="Marcus", attempted=True))

        ident = await app._resolve_turn_identity(good, "voice")
        check(ident.speaker_status == "known" and ident.backend_verified,
              "a valid handle resolves to backend-derived identity")

        replay = await app._resolve_turn_identity(good, "voice")
        check(replay.is_unverified,
              "a REPLAYED handle is unverified, not the identity it once held")
        check(replay.memory_entity is None, "and has no write target")

        invented = await app._resolve_turn_identity("vt-nope", "voice")
        check(invented.is_unverified, "an invented handle is unverified")

        missing = await app._resolve_turn_identity(None, "voice")
        check(missing.is_unverified,
              "a VOICE turn with no handle is unverified — not typed")
        check(missing.memory_entity is None, "and writes nowhere")

        typed = await app._resolve_turn_identity(None, None)
        check(typed.memory_entity == "user", "typed stays legacy owner")
        check(typed.input_source == "typed", "and is marked typed")


async def test_disabled_mode_is_legacy():
    check.section("NOVA_SPEAKER_ID=0 restores pre-P5 semantics")
    async with boot(env={"NOVA_SPEAKER_ID": "0"}) as nova:
        import backend.app as app

        ident = await app._resolve_turn_identity(None, "voice")
        check(ident.memory_entity == "user",
              "voice with the feature off writes to `user` as it always did")
        check(not ident.speaker_attempted, "and is marked not-attempted")
        check(not ident.is_unverified, "it is legacy, not a guest turn")


# ── 3. write isolation, through the real runtime ─────────────────────────────

async def test_guest_cannot_rewrite_marcus():
    check.section("a guest saying 'my name is Alex' leaves Marcus alone")
    async with boot() as nova:
        from core.turn_identity import active_turn

        await nova.memory.add_fact(entity="user", attribute="name", value="Marcus",
                                   confidence=0.98)

        rt = nova.runtime
        # Unverified voice: the highest-risk case.
        with active_turn(ident_status("unknown")):
            await rt._extract_quick_facts("my name is Alex and I live in Berlin")
        name = await nova.memory.get_latest_fact(entity="user", attribute="name")
        check(name is not None and name.value == "Marcus",
              f"Marcus's name is untouched by an unknown speaker ({name.value if name else None})")
        loc = await nova.memory.get_latest_fact(entity="user", attribute="location")
        check(loc is None, "and no location was written to his profile")

        # A KNOWN guest writes to their own namespace instead.
        with active_turn(ident_known("p-alice", "Alice")):
            await rt._extract_quick_facts("my name is Alex and I live in Berlin")
        name = await nova.memory.get_latest_fact(entity="user", attribute="name")
        check(name is not None and name.value == "Marcus",
              f"still Marcus after a known guest speaks ({name.value if name else None})")
        theirs = await nova.memory.get_latest_fact(entity="speaker:p-alice",
                                                   attribute="name")
        check(theirs is not None and theirs.value == "Alex",
              f"the guest's own namespace got it ({theirs.value if theirs else None})")

        # Typed still behaves exactly as before.
        from core.turn_identity import TurnIdentity
        with active_turn(TurnIdentity.typed()):
            await rt._extract_quick_facts("I live in Dallas")
        loc = await nova.memory.get_latest_fact(entity="user", attribute="location")
        check(loc is not None and loc.value == "Dallas",
              f"typed input still writes to `user` ({loc.value if loc else None})")


async def test_memory_correct_is_enforced_in_code():
    check.section("memory.correct cannot be aimed at Marcus by a guest")
    async with boot() as nova:
        from core.tool_router import ToolCall
        from core.turn_identity import TurnIdentity, active_turn

        await nova.memory.add_fact(entity="user", attribute="gpu", value="RTX 5080",
                                   confidence=0.95)

        # Unverified: refused, and nothing persists.
        with active_turn(ident_status("unknown")):
            res = await nova.runtime._router.execute(ToolCall(
                "memory.correct", {"attribute": "gpu", "value": "RTX 4090"}))
        gpu = await nova.memory.get_latest_fact(entity="user", attribute="gpu")
        check(gpu is not None and "5080" in gpu.value,
              f"Marcus's GPU survives an unverified correction ({gpu.value if gpu else None})")
        check(res.ok is False or (isinstance(res.result, dict)
                                  and res.result.get("ok") is False),
              "and the tool reports that it refused")

        # A known guest corrects THEIR OWN fact, not his.
        with active_turn(ident_known("p-alice", "Alice")):
            await nova.runtime._router.execute(ToolCall(
                "memory.correct", {"attribute": "gpu", "value": "RTX 4090"}))
        gpu = await nova.memory.get_latest_fact(entity="user", attribute="gpu")
        check(gpu is not None and "5080" in gpu.value,
              "Marcus's GPU still survives a known guest's correction")
        theirs = await nova.memory.get_latest_fact(entity="speaker:p-alice",
                                                   attribute="gpu")
        check(theirs is not None and "4090" in theirs.value,
              f"the guest corrected their own ({theirs.value if theirs else None})")

        # Typed Marcus corrects normally.
        with active_turn(TurnIdentity.typed()):
            await nova.runtime._router.execute(ToolCall(
                "memory.correct", {"attribute": "gpu", "value": "RTX 5090"}))
        gpu = await nova.memory.get_latest_fact(entity="user", attribute="gpu")
        check(gpu is not None and "5090" in gpu.value,
              f"typed Marcus can still correct his own fact ({gpu.value if gpu else None})")


# ── 4. read isolation ────────────────────────────────────────────────────────

async def test_grounding_does_not_leak_marcus():
    check.section("a guest does not get Marcus's profile read to them")
    async with boot() as nova:
        from core.turn_identity import TurnIdentity, active_turn

        await nova.memory.add_fact(entity="user", attribute="name", value="Marcus",
                                   confidence=0.98)
        await nova.memory.add_fact(entity="user", attribute="spouse", value="Leslie",
                                   confidence=0.9)
        await nova.memory.add_fact(entity="user", attribute="child", value="Mateo",
                                   confidence=0.9)
        await nova.memory.add_fact(entity="speaker:p-alice", attribute="favourite_colour",
                                   value="green", confidence=0.9)

        rt = nova.runtime

        with active_turn(TurnIdentity.typed()):
            owner_ctx = await rt._build_grounding_context(
                user_text="hi", user_name="Marcus",
                available_tools=rt._router.list_tools())
        check("Leslie" in owner_ctx or "Mateo" in owner_ctx,
              "typed Marcus still gets his own family context")

        with active_turn(ident_status("unknown")):
            unknown_ctx = await rt._build_grounding_context(
                user_text="hi", user_name="Marcus",
                available_tools=rt._router.list_tools())
        check("Leslie" not in unknown_ctx and "Mateo" not in unknown_ctx,
              "an unrecognised speaker gets NONE of his family")
        check("unrecognised" in unknown_ctx.lower() or "withheld" in unknown_ctx.lower(),
              "and the context says the profile was withheld")

        with active_turn(ident_known("p-alice", "Alice")):
            guest_ctx = await rt._build_grounding_context(
                user_text="hi", user_name="Marcus",
                available_tools=rt._router.list_tools())
        check("Leslie" not in guest_ctx and "Mateo" not in guest_ctx,
              "a known guest gets none of his family either")
        check("green" in guest_ctx,
              "but DOES get their own stored profile")


async def test_addressing():
    check.section("Nova does not tell a guest they are Marcus")
    from core.turn_identity import TurnIdentity

    check(TurnIdentity.typed().addressee() == "",
          "owner turns keep Nova's existing wording untouched")
    guest = ident_known("p-alice", "Alice").addressee()
    check("Alice" in guest and "not Marcus" in guest,
          f"a known guest is addressed as themselves ({guest[:50]})")
    unknown = ident_status("unknown").addressee()
    check("Marcus" in unknown and "not assume" in unknown,
          "and an unknown speaker is explicitly not assumed to be Marcus")
    for text in (guest, unknown):
        check("0." not in text and "similarity" not in text.lower(),
              "no biometric scores reach the prompt")


# ── 5. permissions and concurrency ───────────────────────────────────────────

async def test_permissions_are_speaker_independent():
    check.section("identity never changes a permission decision")
    import inspect

    from core.permissions import ADMIN, evaluate, tier_of
    from core.turn_identity import TurnIdentity, active_turn

    cap = "some.destructive.capability"
    decisions = {}
    for label, ident in (("typed", TurnIdentity.typed()),
                         ("owner voice", ident_known("p-m", "Marcus", "owner")),
                         ("guest voice", ident_known("p-a", "Alice")),
                         ("unknown voice", ident_status("unknown"))):
        with active_turn(ident):
            decisions[label] = (tier_of(cap), evaluate(cap, mode="guarded"))
    check(len(set(decisions.values())) == 1,
          f"every speaker gets the identical decision ({decisions})")
    check(decisions["owner voice"][0] == ADMIN,
          "and it is still ADMIN for an unknown capability")

    params = set(inspect.signature(evaluate).parameters)
    check(not (params & {"speaker", "identity", "profile_id", "role", "voice"}),
          f"evaluate() takes no identity argument ({sorted(params)})")


async def test_concurrent_turns_do_not_leak_identity():
    check.section("concurrent turns keep their own speaker")
    from core.turn_identity import TurnIdentity, active_turn, current_identity

    seen: dict[str, list[str | None]] = {}

    async def one(label, ident, delay):
        with active_turn(ident):
            await asyncio.sleep(delay)
            seen.setdefault(label, []).append(current_identity().memory_entity)
            await asyncio.sleep(delay)
            seen[label].append(current_identity().memory_entity)

    await asyncio.gather(
        one("typed", TurnIdentity.typed(), 0.01),
        one("guest", ident_known("p-alice", "Alice"), 0.005),
        one("unknown", ident_status("unknown"), 0.015),
        one("owner", ident_known("p-m", "Marcus", "owner"), 0.002),
    )
    check(seen["typed"] == ["user", "user"], f"typed stayed user ({seen['typed']})")
    check(seen["guest"] == ["speaker:p-alice"] * 2, f"guest stayed scoped ({seen['guest']})")
    check(seen["unknown"] == [None, None], f"unknown stayed unverified ({seen['unknown']})")
    check(seen["owner"] == ["user", "user"], f"owner stayed user ({seen['owner']})")

    # And nothing leaked out of the scopes.
    check(current_identity().memory_entity == "user",
          "the ambient default is restored afterwards")


async def test_background_work_does_not_inherit_a_speaker():
    check.section("a worker that never entered a turn sees the typed default")
    from core.turn_identity import current_identity, active_turn

    captured = {}

    async def background():
        captured["entity"] = current_identity().memory_entity

    with active_turn(ident_status("unknown")):
        # Spawned OUTSIDE the context's task would inherit nothing; spawned
        # inside, a copy is inherited — which is correct for work belonging to
        # this turn. What must never happen is a worker started before/after
        # seeing a stale speaker.
        pass
    await background()
    check(captured["entity"] == "user",
          f"background work defaults to legacy semantics ({captured['entity']})")


async def main():
    await test_attribution_matrix()
    await test_role_comes_from_the_profile_not_the_request()
    await test_handle_resolution()
    await test_disabled_mode_is_legacy()
    await test_guest_cannot_rewrite_marcus()
    await test_memory_correct_is_enforced_in_code()
    await test_grounding_does_not_leak_marcus()
    await test_addressing()
    await test_permissions_are_speaker_independent()
    await test_concurrent_turns_do_not_leak_identity()
    await test_background_work_does_not_inherit_a_speaker()
    check.finish()


if __name__ == "__main__":
    run(main)
