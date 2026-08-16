from __future__ import annotations

"""Gmail: read + DRAFT-ONLY (PI1).

The OAuth scope requested at consent time (plugins/_google_oauth.py) is
gmail.readonly + gmail.compose — gmail.send is never requested, so creating
a draft is the most this plugin can ever do; sending is structurally
impossible here, not just prompt-gated. email.draft_reply only ever creates
a Gmail draft that Marcus reviews and sends himself.
"""

import base64
from email.mime.text import MIMEText

import httpx

from plugins._google_oauth import get_access_token
from plugins.registry import tool

_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
_TIMEOUT = httpx.Timeout(10.0)


async def _get_message_headers(client: httpx.AsyncClient, token: str, message_id: str) -> tuple[dict, dict]:
    r = await client.get(
        f"{_BASE}/messages/{message_id}",
        params=[("format", "metadata"), ("metadataHeaders", "From"), ("metadataHeaders", "Subject"),
                ("metadataHeaders", "Message-Id")],
        headers={"Authorization": f"Bearer {token}"},
    )
    r.raise_for_status()
    data = r.json()
    headers = {h["name"]: h["value"] for h in (data.get("payload", {}).get("headers") or [])}
    return data, headers


@tool(
    name="email.recent",
    description=("List recent emails from MARCUS'S connected Gmail account — sender, subject, "
                 "snippet (read-only, no body). This is his mailbox; there is no per-speaker "
                 "account. args: {limit?, unread_only?}"),
    data_scope="owner_private",
)
async def email_recent(args: dict) -> dict:
    limit = max(1, min(int(args.get("limit") or 10), 25))
    unread_only = bool(args.get("unread_only", True))
    token = await get_access_token()
    query = "is:unread in:inbox" if unread_only else "in:inbox"

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.get(
            f"{_BASE}/messages", params={"maxResults": str(limit), "q": query},
            headers={"Authorization": f"Bearer {token}"},
        )
        r.raise_for_status()
        listing = r.json()
        ids = [m["id"] for m in (listing.get("messages") or [])]

        emails: list[dict] = []
        for mid in ids:
            data, headers = await _get_message_headers(client, token, mid)
            emails.append({
                "id": mid,
                "from": headers.get("From", ""),
                "subject": headers.get("Subject", "(no subject)"),
                "snippet": str(data.get("snippet") or ""),
            })

    return {"count": len(emails), "unread_only": unread_only, "emails": emails}


@tool(
    name="email.draft_reply",
    description=("Create a DRAFT reply in MARCUS'S connected Gmail account — never sends; he "
                  "reviews and sends it himself from Gmail. args: {message_id, body}"),
    data_scope="owner_private",
)
async def email_draft_reply(args: dict) -> dict:
    message_id = str(args.get("message_id") or "").strip()
    body = str(args.get("body") or "").strip()
    if not message_id or not body:
        raise ValueError("email.draft_reply requires 'message_id' and 'body'")

    token = await get_access_token()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        original, headers = await _get_message_headers(client, token, message_id)
        thread_id = original.get("threadId")
        to_addr = headers.get("From", "")
        subject = headers.get("Subject", "")
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        in_reply_to = headers.get("Message-Id", "")

        msg = MIMEText(body)
        msg["To"] = to_addr
        msg["Subject"] = subject
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

        message_payload: dict = {"raw": raw}
        if thread_id:
            message_payload["threadId"] = thread_id
        r = await client.post(
            f"{_BASE}/drafts", json={"message": message_payload},
            headers={"Authorization": f"Bearer {token}"},
        )
        r.raise_for_status()
        draft = r.json()

    return {
        "ok": True, "draft_id": draft.get("id"), "to": to_addr, "subject": subject,
        "note": "Draft created in Gmail — nothing was sent. Review and send it yourself.",
    }
