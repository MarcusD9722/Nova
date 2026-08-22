"""Personal dates and age memory are scoped to whoever is speaking (V3 P10 C4).

THE DEFECT, measured on `cea3e25`:

    guest: "When is my birthday?"      -> "Your birthday is July 2."   (Marcus's)
    guest: "When is Robin's birthday?" -> "March 14."                  (Marcus's)
    guest: "Robin is three years old and turns four on December 5th."
           -> Marcus's GLOBAL Robin record was rewritten:
              age_observation 10 -> 3, birthday 03-14 -> 12-05

The third is the serious one: not a leak but destruction of Marcus's data by
somebody else's sentence.

`core/tooling.py` already had the right rule for the person TOOLS — the global
`people` table is Marcus's social map; a known guest lives in
`speaker:<id>:person:<key>`; an unverified speaker gets nothing. The date and
age paths simply did not consult it. This suite pins all of them to the one
canonical helper, `core/turn_identity.scoped_person_entity`.

Run:  venv\\Scripts\\python.exe tests\\test_personal_scope_p10c4.py
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

from harness import Checks, run  # noqa: E402

check = Checks()

# Seeded fixture dates. Generic people, never Marcus's real records.
OWNER_SELF = "07-02"          # July 2
OWNER_ROBIN = "03-14"         # March 14
GUEST_SELF = "01-03"          # January 3
GUEST_ROBIN = "12-05"         # December 5


def _tmp():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


class _NoLLM:
    """Reaching the model means the structured path did not answer."""

    gpu_status = type("S", (), {"status": "stub"})()

    async def initialize(self):
        return None

    async def chat(self, *a, **k):
        raise AssertionError("a scoped date must not require a generation")

    async def chat_stream(self, *a, **k):
        raise AssertionError("a scoped date must not require a generation")
        yield ""


def ident(status, pid=None, name=None, role="guest"):
    from core.speaker.matcher import SpeakerMatch
    from core.turn_identity import TurnIdentity

    class _P:
        pass
    prof = _P()
    prof.role = role
    return TurnIdentity.from_match(
        SpeakerMatch(status=status, profile_id=pid, display_name=name,
                     attempted=True),
        profile=(prof if pid else None))


def OWNER():
    from core.turn_identity import TurnIdentity
    return TurnIdentity.typed()


def GUEST():
    return ident("known", "p-leslie", "Leslie")


def OTHER_GUEST():
    return ident("known", "p-sam", "Sam")


def UNVERIFIED():
    return ident("unknown")


async def _runtime(td: str):
    from core.runtime import RuntimeManager
    from core.tooling import build_tool_router
    from memory.unifier import MemoryUnifier

    root = Path(td)
    (root / "projects").mkdir(parents=True, exist_ok=True)
    (root / "memory").mkdir(parents=True, exist_ok=True)
    m = MemoryUnifier(root / "memory", enable_chroma=False)
    await m.initialize()
    router = build_tool_router(repo_root=root, projects_dir=root / "projects",
                               memory=m)
    rt = RuntimeManager(repo_root=root, projects_dir=root / "projects", memory=m,
                        llm=_NoLLM(), router=router, memory_dir=root / "memory")
    return rt, m


async def _seed(m, *, guest_robin: bool = True):
    """Owner and guest each get a self birthday and (optionally) a Robin."""
    from core.turn_identity import person_key

    await m.add_fact(entity="user", attribute="birthday", value=OWNER_SELF,
                     confidence=0.95, source="user", verification_status="stated")
    await m.upsert_person("Robin", {"birthday": OWNER_ROBIN,
                                    "age_observation": "10"})

    await m.add_fact(entity="speaker:p-leslie", attribute="birthday",
                     value=GUEST_SELF, confidence=0.95, source="user",
                     verification_status="stated")
    if guest_robin:
        root = f"speaker:p-leslie:person:{person_key('Robin')}"
        await m.add_fact(entity=root, attribute="name", value="Robin",
                         confidence=0.9, source="user",
                         verification_status="stated")
        await m.add_fact(entity=root, attribute="birthday", value=GUEST_ROBIN,
                         confidence=0.9, source="user",
                         verification_status="stated")


async def _ask(rt, who, question: str) -> str:
    from core.turn_identity import active_turn
    with active_turn(who):
        return await rt._personal_date_reply(question) or ""


# ── the matrix ───────────────────────────────────────────────────────────────

async def test_self_birthday_is_the_speakers_own():
    check.section("C4: 'my birthday' means MINE, not Marcus's")

    with _tmp() as td:
        rt, m = await _runtime(td)
        await _seed(m)

        owner = await _ask(rt, OWNER(), "When is my birthday?")
        guest = await _ask(rt, GUEST(), "When is my birthday?")

        check("July 2" in owner, f"owner gets his own July 2 ({owner!r})")
        check("January 3" in guest, f"guest gets her own January 3 ({guest!r})")
        # The actual defect: the guest was answered with Marcus's date.
        check("July 2" not in guest,
              f"and the guest is NOT told Marcus's July 2 ({guest!r})")


async def test_named_person_is_read_from_the_speakers_own_people():
    check.section("C4: a guest's Robin is not Marcus's Robin")

    with _tmp() as td:
        rt, m = await _runtime(td)
        await _seed(m)

        owner = await _ask(rt, OWNER(), "When is Robin's birthday?")
        guest = await _ask(rt, GUEST(), "When is Robin's birthday?")

        check("March 14" in owner, f"owner still gets March 14 ({owner!r})")
        check("December 5" in guest,
              f"guest gets HER Robin, December 5 ({guest!r})")
        check("March 14" not in guest,
              f"and never Marcus's March 14 ({guest!r})")


async def test_a_guest_without_their_own_record_gets_no_fallback():
    check.section("C4: missing means missing, never Marcus's copy")

    with _tmp() as td:
        rt, m = await _runtime(td)
        await _seed(m, guest_robin=False)          # guest has NO Robin

        guest = await _ask(rt, GUEST(), "When is Robin's birthday?")

        check("March 14" not in guest,
              f"no silent fallback to the owner's Robin ({guest!r})")
        check("don't have" in guest.lower() or not guest.strip(),
              f"it is admitted as missing instead ({guest!r})")

        # A different guest must not see Leslie's either.
        other = await _ask(rt, OTHER_GUEST(), "When is Robin's birthday?")
        check("December 5" not in other,
              f"and one guest cannot read another guest's Robin ({other!r})")


async def test_an_unverified_speaker_gets_no_structured_private_date():
    check.section("C4: an unrecognised voice gets nobody's dates")

    with _tmp() as td:
        rt, m = await _runtime(td)
        await _seed(m)

        for q in ("When is my birthday?", "When is Robin's birthday?"):
            got = await _ask(rt, UNVERIFIED(), q)
            check(not got.strip(),
                  f"{q!r} returns no structured personal answer ({got!r})")
            check("July 2" not in got and "March 14" not in got,
                  "and leaks neither of Marcus's dates")


async def test_the_store_itself_refuses_an_unverified_read():
    check.section("C4: the refusal is in the store read, not only the caller")

    from core.turn_identity import active_turn

    with _tmp() as td:
        rt, m = await _runtime(td)
        await _seed(m)

        # Bypassing `_personal_date_reply`'s own guard: the lower layer must
        # refuse too, or the boundary is one caller wide.
        with active_turn(UNVERIFIED()):
            mine = await rt._stored_person_date("", "birthday")
            theirs = await rt._stored_person_date("Robin", "birthday")
        check(not mine.known, "unverified self lookup is not known")
        check(not theirs.known, "unverified named lookup is not known")

        with active_turn(GUEST()):
            robin = await rt._stored_person_date("Robin", "birthday")
        check(robin.known and robin.month == 12 and robin.day == 5,
              f"the guest's own Robin still resolves ({robin.month}-{robin.day})")


# ── age write isolation, through the REAL ingestion path ─────────────────────

async def test_a_guests_age_statement_never_touches_the_owners_people_table():
    check.section("C4: a guest's age statement writes THEIR namespace")

    from core.turn_identity import active_turn, person_key

    with _tmp() as td:
        rt, m = await _runtime(td)
        await _seed(m, guest_robin=False)

        before = ((await m.recall_person("Robin")) or {}).get("attributes") or {}
        check(before.get("birthday") == OWNER_ROBIN
              and before.get("age_observation") == "10",
              f"owner's Robin starts at 03-14 / 10 ({before})")

        # The real quick-fact ingestion path, as a known guest.
        with active_turn(GUEST()):
            await rt._extract_quick_facts(
                "Robin is three years old and turns four on December 5th.")

        after = ((await m.recall_person("Robin")) or {}).get("attributes") or {}
        check(after == before,
              f"owner's GLOBAL Robin is untouched ({after})")

        root = f"speaker:p-leslie:person:{person_key('Robin')}"
        rows = await m.get_facts(entity=root, limit=40)
        got = {r.attribute: str(r.value) for r in rows}

        check(got.get("birthday") == GUEST_ROBIN,
              f"the guest's Robin has birthday 12-05 ({got.get('birthday')!r})")
        check(got.get("age_observation") == "3",
              f"and age_observation 3 ({got.get('age_observation')!r})")
        check(bool(got.get("age_observed_on")),
              f"and the date it was observed ({got.get('age_observed_on')!r})")
        check(got.get("birth_date_source") == "derived",
              f"a derived birth date is marked derived ({got.get('birth_date_source')!r})")
        check("age" not in got,
              f"and never a timeless scalar age ({sorted(got)})")

        # Back to the owner: his answer is unchanged.
        owner = await _ask(rt, OWNER(), "When is Robin's birthday?")
        check("March 14" in owner,
              f"the owner still sees March 14 afterwards ({owner!r})")
        check("December 5" not in owner,
              f"and not the guest's December 5 ({owner!r})")


async def test_an_unverified_age_statement_is_written_nowhere():
    check.section("C4: an unrecognised voice writes no age anywhere")

    from core.turn_identity import active_turn, person_key

    with _tmp() as td:
        rt, m = await _runtime(td)
        await _seed(m, guest_robin=False)
        before = ((await m.recall_person("Robin")) or {}).get("attributes") or {}

        with active_turn(UNVERIFIED()):
            await rt._extract_quick_facts(
                "Robin is three years old and turns four on December 5th.")

        after = ((await m.recall_person("Robin")) or {}).get("attributes") or {}
        check(after == before, f"the owner's Robin is untouched ({after})")
        for root in ("speaker:p-leslie:person:" + person_key("Robin"),
                     "person:robin", "robin"):
            rows = await m.get_facts(entity=root, limit=40)
            ages = [r for r in rows if r.attribute.startswith("age")]
            check(not ages, f"no age landed under {root!r} ({len(ages)})")


async def test_the_owner_path_is_byte_for_byte_unchanged():
    check.section("C4: Marcus's own behaviour is not altered by any of this")

    from core.turn_identity import active_turn

    with _tmp() as td:
        rt, m = await _runtime(td)
        await _seed(m, guest_robin=False)

        with active_turn(OWNER()):
            await rt._extract_quick_facts(
                "Mateo is three years old and he turns four on September 16th.")

        rec = await m.recall_person("Mateo")
        attrs = (rec or {}).get("attributes") or {}
        check(bool(rec), "the owner's statement still lands in the people table")
        check(attrs.get("birthday") == "09-16",
              f"with the birthday ({attrs.get('birthday')!r})")
        check(attrs.get("age_observation") == "3",
              f"and the observation ({attrs.get('age_observation')!r})")

        owner = await _ask(rt, OWNER(), "When is Mateo's birthday?")
        check("September 16" in owner, f"and it reads back ({owner!r})")


async def test_one_canonical_helper_serves_every_person_path():
    check.section("C4: the scoping rule exists in exactly one place")

    import inspect

    from core import runtime as rt_mod
    from core import tooling as tooling_mod
    from core.turn_identity import scoped_person_entity

    # The helper itself decides all three cases.
    with_owner = scoped_person_entity("Robin", OWNER())
    with_guest = scoped_person_entity("Robin", GUEST())
    with_unver = scoped_person_entity("Robin", UNVERIFIED())
    check(with_owner.is_global_people, "owner -> the global people table")
    check(with_guest.is_scoped_facts
          and with_guest.entity == "speaker:p-leslie:person:robin",
          f"guest -> their own namespace ({with_guest.entity!r})")
    check(with_unver.refused, "unverified -> refused")

    # And every person path routes through it rather than re-deriving the rule.
    tool_src = inspect.getsource(tooling_mod.build_tool_router)
    check("scoped_person_entity" in tool_src,
          "the person tools use the canonical helper")
    check(tool_src.count("def _person_key") == 0,
          "and no longer carry their own copy of the key logic")

    date_src = inspect.getsource(rt_mod.RuntimeManager._stored_person_date)
    check("scoped_person_entity" in date_src,
          "structured date lookup uses it")
    check('entity="user"' not in date_src,
          "and no longer hardcodes the owner entity")

    age_src = inspect.getsource(rt_mod.RuntimeManager._extract_quick_facts)
    check("scoped_person_entity" in age_src, "age capture uses it")


async def main():
    await test_self_birthday_is_the_speakers_own()
    await test_named_person_is_read_from_the_speakers_own_people()
    await test_a_guest_without_their_own_record_gets_no_fallback()
    await test_an_unverified_speaker_gets_no_structured_private_date()
    await test_the_store_itself_refuses_an_unverified_read()
    await test_a_guests_age_statement_never_touches_the_owners_people_table()
    await test_an_unverified_age_statement_is_written_nowhere()
    await test_the_owner_path_is_byte_for_byte_unchanged()
    await test_one_canonical_helper_serves_every_person_path()
    check.finish()


if __name__ == "__main__":
    run(main)
