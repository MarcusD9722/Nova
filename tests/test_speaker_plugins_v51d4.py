"""V3 P5.1d.4: connected-account plugins, and the secondary LLM prompt.

Two activation blockers, both reproduced on `71fc0eb`.

PLUGINS. P5.1d.3's completeness test subtracted every plugin name before
checking for missing classifications — so the "complete" invariant excluded
exactly the tools that reach Marcus's connected accounts. Measured, with the
transports mocked and every call counted: a guest AND an unknown speaker each
received his unread mail (subject + snippet), his calendar events with
locations, and his Discord history, and could create a Gmail draft and send a
Discord message. One OAuth token fetch and one-to-two HTTP calls per attempt.

SOCIETY. Suppressing the private specialist notes (P5.1d.3) was not enough: the
coordinator's synthesis prompt still literally said "answer for Marcus" for
every speaker. The addressee is a second, independent way the same prompt
asserts the wrong person.

Nothing real is contacted here. OAuth and httpx are patched per module and every
call is counted, so "refused" is proven as ZERO external work rather than as an
error string.

Run:  venv\\Scripts\\python.exe tests\\test_speaker_plugins_v51d4.py
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

E_SUBJ, E_SNIP = "OWNER-EMAIL-SUBJECT-771", "OWNER-EMAIL-SNIPPET-772"
C_SECRET, C_LOC = "OWNER-CALENDAR-SECRET-881", "OWNER-CALENDAR-LOCATION-882"
D_SECRET = "OWNER-DISCORD-SECRET-991"
AGENT_SENTINEL = "AGENT-PRIVATE-SENTINEL-552"

#: Counts every external touch. A refused call must leave all of these at 0.
CALLS = {"token": 0, "http": 0, "draft": 0, "discord_send": 0}


def _reset():
    for k in CALLS:
        CALLS[k] = 0


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class _FakeClient:
    """Stands in for httpx.AsyncClient in the three account plugins."""

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kw):
        CALLS["http"] += 1
        if "gmail" in url and "/messages/" in url:
            return _Resp({"threadId": "t1", "snippet": E_SNIP, "payload": {"headers": [
                {"name": "From", "value": "someone@example.com"},
                {"name": "Subject", "value": E_SUBJ},
                {"name": "Message-Id", "value": "<m1>"}]}})
        if "gmail" in url:
            return _Resp({"messages": [{"id": "m1"}]})
        if "calendar" in url:
            return _Resp({"items": [{"summary": C_SECRET, "location": C_LOC,
                                     "start": {"dateTime": "2026-08-16T10:00:00Z"}}]})
        if "discord" in url:
            return _Resp([{"author": {"username": "bob"}, "content": D_SECRET}])
        return _Resp({})

    async def post(self, url, **kw):
        CALLS["http"] += 1
        if "drafts" in url:
            CALLS["draft"] += 1
            return _Resp({"id": "draft-123"})
        if "discord" in url:
            CALLS["discord_send"] += 1
            return _Resp({"id": "msg-1", "content": "sent"})
        return _Resp({})


async def _fake_token():
    CALLS["token"] += 1
    return "fake-access-token"


def _patch_account_plugins():
    import plugins.discord as d
    import plugins.gmail as g
    import plugins.google_calendar as c
    g.get_access_token = _fake_token
    c.get_access_token = _fake_token
    g.httpx.AsyncClient = _FakeClient
    c.httpx.AsyncClient = _FakeClient
    d.httpx.AsyncClient = _FakeClient


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


def UNKNOWN():
    return ident("unknown")


ACCOUNT_ENV = {"DISCORD_BOT_TOKEN": "fake-token", "DISCORD_CHANNEL_ID": "424242"}


async def call(nova, who, tool, args=None):
    """Run a tool and return (result, external-call counts for THIS call)."""
    from core.tool_router import ToolCall
    from core.turn_identity import active_turn
    _reset()
    with active_turn(who):
        r = await nova.runtime._router.execute(ToolCall(tool, args or {}))
    return r.result, dict(CALLS)


def refused(r) -> bool:
    return (isinstance(r, dict) and r.get("ok") is False
            and r.get("error") == "scoped_unavailable")


def no_external(c: dict) -> bool:
    return all(v == 0 for v in c.values())


# ── §4/§5/§8. the metadata itself ────────────────────────────────────────────

async def test_plugin_scope_metadata_is_mandatory():
    check.section("4: data_scope is required, not defaulted")
    import plugins.init  # noqa: F401
    import plugins.registry as preg

    specs = preg.REGISTRY.get_tools()
    check("data_scope" in preg.ToolSpec.__dataclass_fields__,
          "ToolSpec carries an explicit speaker-data classification")
    check(len(specs) == 14, f"all 14 plugin tools are loaded ({len(specs)})")

    unscoped = sorted(n for n, s in specs.items() if s.data_scope not in preg.DATA_SCOPES)
    check(not unscoped, f"every loaded plugin declares a valid scope ({unscoped})")

    owner_private = {n for n, s in specs.items() if s.data_scope == "owner_private"}
    check(owner_private == {"email.recent", "email.draft_reply", "calendar.today",
                            "calendar.upcoming", "discord.read", "discord.send"},
          f"the connected-account tools are exactly the six expected ({sorted(owner_private)})")
    shared = {n for n, s in specs.items() if s.data_scope == "shared"}
    check(shared == {"system.time", "weather.current", "web.search", "web.fetch",
                     "maps.geocode", "maps.place_search", "maps.directions",
                     "maps.places_nearby"},
          f"and the public ones are the other eight ({sorted(shared)})")

    # A new plugin cannot inherit public access by saying nothing.
    try:
        preg.tool(name="x.new", description="d")  # type: ignore[call-arg]
        check(False, "registering without data_scope should be an error")
    except TypeError:
        check(True, "omitting data_scope is a TypeError at import time")
    try:
        preg.ToolRegistry().register_sync(
            preg.ToolSpec(name="x.bad", description="d", fn=None, data_scope="public"))
        check(False, "an invalid scope should be rejected")
    except ValueError as e:
        check("data_scope" in str(e), f"and an invalid value is rejected ({str(e)[:60]})")
    # P5.1e §0: the dataclass field itself now has no default either, so a
    # direct ToolSpec construction fails at construction rather than relying on
    # registration-time validation of an empty sentinel.
    try:
        preg.ToolSpec(name="x.nodefault", description="d", fn=None)  # type: ignore[call-arg]
        check(False, "ToolSpec without data_scope should not construct")
    except TypeError:
        check(True, "ToolSpec has no data_scope default")


# ── §11. Gmail ───────────────────────────────────────────────────────────────

async def test_gmail_is_owner_only():
    check.section("11: Gmail — Marcus's connected mailbox")
    _patch_account_plugins()
    async with boot(env=ACCOUNT_ENV) as nova:
        r, c = await call(nova, OWNER(), "email.recent", {"limit": 2})
        check(E_SUBJ in str(r) and E_SNIP in str(r),
              "the owner still receives his own mail, unchanged")
        check(c["token"] == 1 and c["http"] == 2,
              f"with the same request pattern as before ({c})")

        for label, who in (("a guest", ALICE()), ("an unknown speaker", UNKNOWN())):
            r, c = await call(nova, who, "email.recent", {"limit": 2})
            check(refused(r), f"{label} is refused ({str(r)[:60]})")
            check(E_SUBJ not in str(r) and E_SNIP not in str(r),
                  f"{label} receives no mail content")
            check(no_external(c),
                  f"{label}: ZERO token fetch and ZERO HTTP — refused before "
                  f"his credentials were touched ({c})")

        r, c = await call(nova, OWNER(), "email.draft_reply",
                          {"message_id": "m1", "body": "thanks"})
        check(r.get("ok") and c["draft"] == 1,
              f"the owner's draft-only behaviour is unchanged ({c})")
        for label, who in (("a guest", ALICE()), ("an unknown speaker", UNKNOWN())):
            r, c = await call(nova, who, "email.draft_reply",
                              {"message_id": "m1", "body": "hello"})
            check(refused(r), f"{label} cannot draft from his account")
            check(c["draft"] == 0 and no_external(c),
                  f"{label}: zero draft API calls ({c})")


# ── §11. Calendar ────────────────────────────────────────────────────────────

async def test_calendar_is_owner_only():
    check.section("11: Calendar — his primary calendar")
    _patch_account_plugins()
    async with boot(env=ACCOUNT_ENV) as nova:
        for tool, args in (("calendar.today", {}), ("calendar.upcoming", {"days": 3})):
            r, c = await call(nova, OWNER(), tool, args)
            check(C_SECRET in str(r) and C_LOC in str(r),
                  f"the owner still sees his events via {tool}")
            check(c["token"] == 1 and c["http"] == 1, f"unchanged request pattern ({c})")

            for label, who in (("a guest", ALICE()), ("an unknown speaker", UNKNOWN())):
                r, c = await call(nova, who, tool, args)
                check(refused(r), f"{label} is refused by {tool}")
                check(C_SECRET not in str(r) and C_LOC not in str(r),
                      f"{label} learns neither what nor where")
                check(no_external(c), f"{label}: zero token/HTTP for {tool} ({c})")


# ── §11. Discord ─────────────────────────────────────────────────────────────

async def test_discord_is_owner_only():
    check.section("11: Discord — the configured owner bot and channel")
    _patch_account_plugins()
    async with boot(env=ACCOUNT_ENV) as nova:
        r, c = await call(nova, OWNER(), "discord.read", {"limit": 2})
        check(D_SECRET in str(r) and c["http"] == 1,
              f"the owner still reads his channel ({c})")
        r, c = await call(nova, OWNER(), "discord.send", {"content": "hello"})
        check(c["discord_send"] == 1, f"and can still send ({c})")

        for label, who in (("a guest", ALICE()), ("an unknown speaker", UNKNOWN())):
            r, c = await call(nova, who, "discord.read", {"limit": 2})
            check(refused(r) and D_SECRET not in str(r),
                  f"{label} cannot read his channel")
            check(no_external(c), f"{label}: zero read calls ({c})")

            r, c = await call(nova, who, "discord.send",
                              {"content": "a message from a guest"})
            check(refused(r), f"{label} cannot send as his bot")
            check(c["discord_send"] == 0 and no_external(c),
                  f"{label}: ZERO outbound send — nothing left the machine ({c})")


# ── §11. shared plugins keep working ─────────────────────────────────────────

async def test_shared_plugins_remain_available():
    check.section("11: public plugins stay available to every speaker")
    import httpx

    import plugins.google_maps as gm
    import plugins.weather as wx
    import plugins.web_search as ws

    class _SharedClient(_FakeClient):
        async def get(self, url, **kw):
            CALLS["http"] += 1
            if "geocode" in url:
                return _Resp({"status": "OK", "results": [
                    {"formatted_address": "Dallas, TX",
                     "geometry": {"location": {"lat": 32.7, "lng": -96.8}}}]})
            if "weather" in url or "forecast" in url:
                return _Resp({"current": {"temperature_2m": 21.0, "weather_code": 0,
                                          "wind_speed_10m": 5.0,
                                          "relative_humidity_2m": 40}})
            return _Resp({"results": [{"title": "a result", "url": "https://x",
                                       "content": "snippet"}]})

        async def post(self, url, **kw):
            CALLS["http"] += 1
            return _Resp({"results": [{"title": "a result", "url": "https://x",
                                       "content": "snippet"}]})

    for mod in (gm, wx, ws):
        if hasattr(mod, "httpx"):
            mod.httpx.AsyncClient = _SharedClient

    async with boot(env={**ACCOUNT_ENV, "WEATHER_API_KEY": "k",
                         "GOOGLE_MAPS_API_KEY": "k", "SEARXNG_URL": "http://x"}) as nova:
        for label, who in (("a guest", ALICE()), ("an unknown speaker", UNKNOWN())):
            r, _ = await call(nova, who, "system.time", {})
            check(isinstance(r, dict) and not refused(r),
                  f"{label}: system.time works ({str(r)[:50]})")
            for tool, args in (("weather.current", {"location": "Dallas"}),
                               ("web.search", {"query": "python"}),
                               ("maps.geocode", {"address": "Dallas, TX"})):
                r, _ = await call(nova, who, tool, args)
                check(not refused(r),
                      f"{label}: {tool} is not scope-refused ({str(r)[:60]})")


# ── §10. the secondary LLM prompt ────────────────────────────────────────────

async def test_society_coordinator_addressee():
    check.section("10: the coordinator's synthesis prompt, captured exactly")
    from core.orchestrator.society import roster

    # A question that routes to several specialists, so the coordinator
    # synthesis call actually happens — with one contributor it never runs and
    # the assertion would pass vacuously.
    QUESTION = "I keep putting off my training and my code is a mess"

    async with boot(env={"NOVA_AGENT_SOCIETY": "1"}) as nova:
        for spec in roster():
            await nova.memory.agent_remember(
                spec["id"], f"{AGENT_SENTINEL} Marcus prefers primary sources",
                topic="preferences")

        async def coordinator_prompt(who):
            from core.tool_router import ToolCall
            from core.turn_identity import active_turn
            nova.llm.reset_calls()
            with active_turn(who):
                await nova.runtime._router.execute(
                    ToolCall("society.consult", {"question": QUESTION}))
            got = [p for p in nova.llm.prompts if "Synthesize this into one" in p]
            return (got[-1] if got else ""), list(nova.llm.prompts)

        owner, owner_all = await coordinator_prompt(OWNER())
        check(owner, "the coordinator synthesis actually ran for the owner")
        check("answer for Marcus" in owner,
              "and still says the answer is for Marcus, unchanged")
        check(any(AGENT_SENTINEL in p for p in owner_all),
              "his specialists still get his accumulated notes")

        alice, alice_all = await coordinator_prompt(ALICE())
        check(alice, "it ran for a known guest too")
        check("answer for Marcus" not in alice,
              f"and does NOT tell the council the answer is for Marcus "
              f"({alice[-140:]!r})")
        check("Alice" in alice, "it names the actual speaker")
        check(not any(AGENT_SENTINEL in p for p in alice_all),
              "and no specialist prompt carries his private notes")

        unk, unk_all = await coordinator_prompt(UNKNOWN())
        check(unk, "it ran for an unrecognised speaker")
        check("Marcus" not in unk,
              f"whose synthesis prompt never mentions Marcus at all ({unk[-140:]!r})")
        check("the current speaker" in unk, "using neutral wording instead")
        check(not any(AGENT_SENTINEL in p for p in unk_all),
              "and carries none of his notes either")


# ── §13. the boundary that must not move ─────────────────────────────────────

async def test_permissions_unchanged():
    check.section("13: identity still changes no permission decision")
    import inspect

    from core.permissions import evaluate, tier_of
    from core.turn_identity import active_turn

    per_cap: dict[str, set] = {}
    for cap in ("some.destructive.capability", "email.recent", "discord.send",
                "calendar.today", "web.search"):
        for i in (OWNER(), ident("known", "p-m", "Marcus", "owner"), ALICE(),
                  UNKNOWN()):
            with active_turn(i):
                per_cap.setdefault(cap, set()).add((tier_of(cap),
                                                    evaluate(cap, mode="guarded")))
    check(all(len(v) == 1 for v in per_cap.values()),
          f"identical per capability across four identities ({per_cap})")
    check(not (set(inspect.signature(evaluate).parameters)
               & {"speaker", "identity", "role"}),
          "and evaluate() still takes no identity argument")

    # Honest about what this phase does NOT provide.
    from core.tool_router import ToolRouter
    src = inspect.getsource(ToolRouter)
    check("PermissionBroker" not in src,
          "ToolRouter still does not generically broker plugin calls — speaker "
          "data-scope is P5; generic actuator permission coverage is P8")


async def test_frontend_untouched():
    check.section("frontend: identity is ACTIVE, and still backend-derived")
    # Until V3 P5.1e this asserted the frontend sent nothing. It now does — that
    # was the whole point of P5.1e — so the invariant moves to the part that
    # still must hold: the client forwards an opaque handle and never asserts
    # who is speaking.
    from pathlib import Path as _P
    origin = (REPO / "frontend/src/voice/turnOrigin.ts").read_text(encoding="utf-8")
    check("input_source" in origin and "voice_turn_id" in origin,
          "the client sends transport + an opaque handle")
    for banned in ('"profile_id"', '"display_name"', '"role"'):
        check(f"out[{banned}]" not in origin and f"{banned}:" not in origin,
              f"and never asserts {banned}")
    app = (REPO / "frontend/src/App.jsx").read_text(encoding="utf-8")
    check("localStorage" not in app.split("voiceOrigin")[-1][:400],
          "no persisted owner flag near the voice path")


async def main():
    await test_plugin_scope_metadata_is_mandatory()
    await test_gmail_is_owner_only()
    await test_calendar_is_owner_only()
    await test_discord_is_owner_only()
    await test_shared_plugins_remain_available()
    await test_society_coordinator_addressee()
    await test_permissions_unchanged()
    await test_frontend_untouched()
    check.finish()


if __name__ == "__main__":
    run(main)
