from __future__ import annotations

"""Shared Google OAuth token handling for google_calendar.py and gmail.py.

Read-only concern: load the cached refresh/access token written by the
one-time `tools/google_oauth_setup.py` consent script, refresh it if
expired, and hand back a bearer token for direct REST calls (httpx, matching
every other plugin in this project — no googleapiclient dependency needed
for the handful of endpoints these two plugins use).

This module never performs the interactive consent flow itself — that
requires the user's own browser session under their own Google account, and
is deliberately kept in the standalone setup script, not the always-on
backend.
"""

import asyncio
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from plugins.registry import PluginConfigError

TOKEN_PATH = Path(__file__).resolve().parent.parent / "credentials" / "google_token.json"

# Scopes requested at consent time — read-only calendar, read + DRAFT-ONLY
# gmail. gmail.send is deliberately never requested: it must be structurally
# impossible for a draft-creating call to actually send an email, not just
# prompt-gated.
SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]


def _load_sync() -> Credentials:
    if not TOKEN_PATH.exists():
        raise PluginConfigError(
            "Google account not connected. Run `python tools/google_oauth_setup.py` once to authorize "
            "calendar/email access (see tools/README_google_oauth.md)."
        )
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        return creds
    raise PluginConfigError(
        "Google credentials exist but are invalid and can't be refreshed — re-run "
        "`python tools/google_oauth_setup.py` to reconnect."
    )


async def get_access_token() -> str:
    """Bearer token for direct REST calls, refreshing on disk if needed.
    Raises PluginConfigError with a clear next step if never connected."""
    creds = await asyncio.to_thread(_load_sync)
    return str(creds.token)
