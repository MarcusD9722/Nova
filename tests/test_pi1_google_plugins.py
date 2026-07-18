"""Offline test for the PI1 Google Calendar / Gmail plugins, using a mocked
httpx transport so no real network call or real OAuth token is needed. Verifies:
- calendar.today formats events correctly from a fake API response
- email.recent formats sender/subject/snippet correctly
- email.draft_reply builds a proper reply (threadId, In-Reply-To, base64 raw)
  and never touches gmail.send
"""
import asyncio
import base64
import json
import sys
from email import message_from_bytes

import httpx

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

import plugins._google_oauth as goauth  # noqa: E402
import plugins.google_calendar as gcal  # noqa: E402
import plugins.gmail as gmail  # noqa: E402

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


async def fake_get_access_token() -> str:
    return "fake-token-123"


def handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.startswith("/calendar/v3"):
        return httpx.Response(200, json={
            "items": [
                {"summary": "Dentist", "start": {"dateTime": "2026-07-16T14:00:00-06:00"}},
                {"summary": "Marcus's Birthday Planning", "start": {"date": "2026-07-16"}},
            ]
        })
    if path.endswith("/messages") and request.method == "GET":
        return httpx.Response(200, json={"messages": [{"id": "m1"}, {"id": "m2"}]})
    if path.endswith("/messages/m1"):
        return httpx.Response(200, json={
            "snippet": "Let's sync tomorrow",
            "payload": {"headers": [
                {"name": "From", "value": "boss@example.com"},
                {"name": "Subject", "value": "Sync tomorrow?"},
                {"name": "Message-Id", "value": "<abc123@example.com>"},
            ]},
            "threadId": "thread-1",
        })
    if path.endswith("/messages/m2"):
        return httpx.Response(200, json={
            "snippet": "Invoice attached",
            "payload": {"headers": [
                {"name": "From", "value": "billing@example.com"},
                {"name": "Subject", "value": "Invoice #42"},
            ]},
            "threadId": "thread-2",
        })
    if path.endswith("/drafts") and request.method == "POST":
        body = json.loads(request.content)
        return httpx.Response(200, json={"id": "draft-1", "message": body["message"]})
    return httpx.Response(404, json={"error": "unhandled path in test", "path": path})


async def main():
    goauth.get_access_token = fake_get_access_token
    gcal.get_access_token = fake_get_access_token
    gmail.get_access_token = fake_get_access_token

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    class _CM:
        async def __aenter__(self):
            return mock_client
        async def __aexit__(self, *a):
            return False

    original_async_client = httpx.AsyncClient
    httpx.AsyncClient = lambda *a, **k: _CM()  # type: ignore

    try:
        today = await gcal.calendar_today({})
        check(today["count"] == 2, f"calendar.today returns 2 events (got {today['count']})")
        check(today["events"][0]["summary"] == "Dentist", "first event summary correct")
        check(today["events"][1]["all_day"] is True, "all-day event (date, no dateTime) flagged correctly")
        check(today["events"][0]["all_day"] is False, "timed event not flagged all-day")

        recent = await gmail.email_recent({"unread_only": True, "limit": 10})
        check(recent["count"] == 2, f"email.recent returns 2 messages (got {recent['count']})")
        check(recent["emails"][0]["from"] == "boss@example.com", "first email sender correct")
        check(recent["emails"][0]["subject"] == "Sync tomorrow?", "first email subject correct")
        check(recent["emails"][0]["snippet"] == "Let's sync tomorrow", "first email snippet correct")

        draft = await gmail.email_draft_reply({"message_id": "m1", "body": "Sounds good, see you then."})
        check(draft["ok"] is True, "draft created ok")
        check(draft["to"] == "boss@example.com", "draft addressed to original sender")
        check(draft["subject"] == "Re: Sync tomorrow?", f"draft subject prefixed with Re: (got {draft['subject']!r})")
        check("nothing was sent" in draft["note"].lower(), "draft note clarifies nothing was sent")

        # Decode the raw MIME to confirm threading headers + no send call was made
        raw_used = None
        def capture_handler(request: httpx.Request) -> httpx.Response:
            nonlocal raw_used
            if request.url.path.endswith("/drafts"):
                raw_used = json.loads(request.content)
            return handler(request)
        mock_client2 = original_async_client(transport=httpx.MockTransport(capture_handler))

        class _CM2:
            async def __aenter__(self):
                return mock_client2
            async def __aexit__(self, *a):
                return False
        httpx.AsyncClient = lambda *a, **k: _CM2()  # type: ignore
        await gmail.email_draft_reply({"message_id": "m1", "body": "Second test body."})
        check(raw_used is not None and raw_used["message"].get("threadId") == "thread-1", "draft attached to original thread")
        raw_bytes = base64.urlsafe_b64decode(raw_used["message"]["raw"])
        msg = message_from_bytes(raw_bytes)
        check(msg["In-Reply-To"] == "<abc123@example.com>", "In-Reply-To header set from original Message-Id")
        check(msg["To"] == "boss@example.com", "MIME To header correct")

        # No gmail.send scope/tool exists at all -- structural guarantee
        from plugins.registry import REGISTRY
        tool_names = set(REGISTRY.get_tools().keys())
        check("email.send" not in tool_names, "no email.send tool exists in the registry")
        check("gmail.send" not in " ".join(goauth.SCOPES), "gmail.send scope never requested")

    finally:
        httpx.AsyncClient = original_async_client
        await mock_client.aclose()

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


asyncio.run(main())
