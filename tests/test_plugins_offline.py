"""Full-audit coverage for the plugins that had NONE.

plugins/web_search.py, plugins/system_tools.py and plugins/discord.py were
never exercised by a test. All three are reachable by the agent loop, so a
silent parse failure here looks to Nova like "the web returned nothing" and
she answers from stale knowledge instead of saying she couldn't search.

Fully offline: every HTTP call goes through a mocked httpx transport, so no
network, no rate limits, no Discord message ever sent.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Checks

import httpx

import plugins.web_search as ws
import plugins.discord as dc
from plugins.system_tools import system_time
from plugins.registry import PluginConfigError

check = Checks()


# Captured BEFORE any patching. `plugins.x.httpx` is the shared httpx module,
# so assigning to it replaces AsyncClient globally — and the mock would then
# resolve its own inner httpx.AsyncClient(...) back to itself, recursing.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def fake_client(handler):
    """Patch httpx.AsyncClient so a module's calls hit `handler` instead of the net."""
    class _Client:
        def __init__(self, *a, **kw):
            self._t = httpx.MockTransport(handler)
            self._kw = {k: v for k, v in kw.items() if k in {"timeout", "headers", "follow_redirects"}}
            self._c = None

        async def __aenter__(self):
            self._c = _REAL_ASYNC_CLIENT(transport=self._t, **self._kw)
            return self._c

        async def __aexit__(self, *a):
            if self._c is not None:
                await self._c.aclose()
            return False
    return _Client


DDG_HTML = """
<html><body>
<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fone">First <b>result</b></a>
<a class="result__snippet">Snippet <b>one</b> here</a>
<a class="result__a" href="//duckduckgo.com/l/?ad_domain=spam.com&amp;ad_provider=x">An advert</a>
<a class="result__snippet">Buy things</a>
<a class="result__a" href="//duckduckgo.com/y.js?tracking=1">Tracker</a>
<a class="result__snippet">Tracked</a>
<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Ftwo">Second</a>
<a class="result__snippet">Snippet two</a>
</body></html>
"""


async def test_web_search() -> None:
    check.section("web.search")

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, text=DDG_HTML)

    ws.httpx.AsyncClient = fake_client(handler)

    out = await ws.web_search({"query": "rtx 5090 price"})
    check(out["query"] == "rtx 5090 price", "the query is echoed back")
    check("q=rtx+5090+price" in seen["url"] or "rtx%205090" in seen["url"], "the query reaches DuckDuckGo")

    urls = [r["url"] for r in out["results"]]
    check(urls == ["https://example.com/one", "https://example.org/two"],
          f"organic results are extracted and un-wrapped from the redirect ({urls})")
    check(out["count"] == 2, "count matches the result list")
    check(out["results"][0]["title"] == "First result", "HTML tags are stripped from titles")
    check(out["results"][0]["snippet"] == "Snippet one here", "HTML tags are stripped from snippets")
    # Regression: ad redirects 400 when fetched, so a chained search->fetch died on them.
    check(not any("ad_domain" in u or "y.js" in u for u in urls), "ad and tracker links are dropped")

    out = await ws.web_search({"q": "alias key"})
    check(out["query"] == "alias key", "the 'q' alias works as well as 'query'")

    out = await ws.web_search({"query": "x", "max_results": 1})
    check(len(out["results"]) == 1, "max_results is honored")
    out = await ws.web_search({"query": "x", "max_results": 999})
    check(len(out["results"]) <= 10, "max_results is capped at 10")

    for bad in ({}, {"query": "   "}):
        raised = False
        try:
            await ws.web_search(bad)
        except ValueError:
            raised = True
        check(raised, f"a missing query raises loudly rather than searching for nothing ({bad})")

    # An empty results page must be distinguishable from a failure.
    ws.httpx.AsyncClient = fake_client(lambda r: httpx.Response(200, text="<html></html>"))
    out = await ws.web_search({"query": "nothing"})
    check(out["results"] == [] and out["count"] == 0, "a page with no results returns an empty list")

    # An HTTP error must NOT come back as "no results" — that would look to
    # Nova like the web genuinely had nothing to say.
    ws.httpx.AsyncClient = fake_client(lambda r: httpx.Response(503, text="down"))
    raised = False
    try:
        await ws.web_search({"query": "x"})
    except httpx.HTTPStatusError:
        raised = True
    check(raised, "an HTTP 503 raises instead of silently returning zero results")


async def test_web_fetch() -> None:
    check.section("web.fetch")

    page = "<html><head><style>p{color:red}</style><script>evil()</script></head>" \
           "<body><h1>Title</h1><p>Hello &amp; welcome &lt;friend&gt;</p></body></html>"
    ws.httpx.AsyncClient = fake_client(
        lambda r: httpx.Response(200, text=page, headers={"content-type": "text/html; charset=utf-8"}))

    out = await ws.web_fetch({"url": "https://example.com"})
    check("Hello & welcome <friend>" in out["content"], "entities are decoded")
    check("evil()" not in out["content"], "script contents are stripped")
    check("color:red" not in out["content"], "style contents are stripped")
    check("Title" in out["content"], "real text survives")
    check(out["chars"] == len(out["content"]), "chars matches the returned content")

    ws.httpx.AsyncClient = fake_client(
        lambda r: httpx.Response(200, text="x" * 50000, headers={"content-type": "text/html"}))
    out = await ws.web_fetch({"url": "https://example.com", "max_chars": 100})
    check(out["content"].endswith("[... truncated]"), "over-long pages are truncated with a marker")
    out = await ws.web_fetch({"url": "https://example.com", "max_chars": 999999})
    check(len(out["content"]) <= 16000 + 40, "max_chars is capped at 16000")

    ws.httpx.AsyncClient = fake_client(
        lambda r: httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"}))
    out = await ws.web_fetch({"url": "https://example.com/x.png"})
    check("Non-text content" in out["content"] and out["chars"] == 0,
          "binary content is reported honestly, not returned as garbage text")

    for bad, why in (({}, "missing url"), ({"url": "ftp://x"}, "non-http scheme"),
                     ({"url": "example.com"}, "scheme-less url")):
        raised = False
        try:
            await ws.web_fetch(bad)
        except ValueError:
            raised = True
        check(raised, f"{why} is rejected loudly")


async def test_system_time() -> None:
    check.section("system.time")
    out = await system_time({})
    for key in ("datetime_local", "date", "time", "timezone", "datetime_utc", "unix_timestamp"):
        check(key in out, f"reports {key}")
    check(isinstance(out["unix_timestamp"], int) and out["unix_timestamp"] > 1_700_000_000,
          "the unix timestamp is a plausible current epoch")
    check("UTC" in out["datetime_utc"], "the UTC field is labelled UTC")
    check(out["time"][-2:] in ("AM", "PM"), f"local time is 12-hour with a meridiem ({out['time']})")


async def test_discord_config_guard() -> None:
    check.section("discord — configuration is reported honestly")
    saved = {k: os.environ.get(k) for k in ("DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID")}
    os.environ["DISCORD_BOT_TOKEN"] = ""
    os.environ["DISCORD_CHANNEL_ID"] = ""
    try:
        # An unconfigured integration must raise PluginConfigError, NOT a
        # generic failure — the ToolRouter routes that specific type to
        # tool.not_configured so the self-improve loop stops filing bogus
        # code-fix proposals for a missing key.
        for fn, label in ((dc.read_messages, "discord.read"), (dc.send_message, "discord.send")):
            kind = None
            try:
                await fn({"content": "never sent", "limit": 1})
            except PluginConfigError:
                kind = "config"
            except Exception as e:  # noqa: BLE001
                kind = type(e).__name__
            check(kind == "config", f"{label} raises PluginConfigError when unconfigured (got {kind})")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


async def test_discord_read_mocked() -> None:
    check.section("discord.read against a mocked API (no real channel touched)")
    saved = {k: os.environ.get(k) for k in ("DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID")}
    os.environ["DISCORD_BOT_TOKEN"] = "fake-token"
    os.environ["DISCORD_CHANNEL_ID"] = "12345"
    payload = [
        {"id": "2", "content": "second", "author": {"username": "marcus"}, "timestamp": "2026-08-03T10:00:00Z"},
        {"id": "1", "content": "first", "author": {"username": "nova"}, "timestamp": "2026-08-03T09:00:00Z"},
    ]
    calls = {}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["url"] = str(request.url)
        calls["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json=payload)

    try:
        dc.httpx.AsyncClient = fake_client(handler)
        out = await dc.read_messages({"limit": 2})
        check(isinstance(out, dict), "discord.read returns a dict")
        check("12345" in calls["url"], "it reads the configured channel")
        check(calls["auth"].startswith("Bot "), "it authenticates as a bot")
        body = str(out)
        check("second" in body and "first" in body, f"message contents are returned ({body[:90]!r})")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


async def main() -> None:
    await test_web_search()
    await test_web_fetch()
    await test_system_time()
    await test_discord_config_guard()
    await test_discord_read_mocked()
    check.finish()


asyncio.run(main())
