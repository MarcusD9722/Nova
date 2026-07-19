from __future__ import annotations

"""One-time interactive Google OAuth setup for calendar + email (PI1).

Run this yourself, once, from a terminal:

    python tools/google_oauth_setup.py

It opens your browser for you to sign in and approve access under YOUR
Google account — Nova/Claude never sees your password, and this script
never runs unattended. On success it caches a refresh token to
credentials/google_token.json, which plugins/google_calendar.py and
plugins/gmail.py then use. See tools/README_google_oauth.md for how to
create the OAuth client this script needs.

Scopes requested: calendar (read-only), gmail (read-only + draft-only).
gmail.send is never requested, so Nova can create draft replies but can
never actually send an email — that's enforced by Google, not just by
prompting her not to.
"""

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402

from plugins._google_oauth import SCOPES, TOKEN_PATH  # noqa: E402


def main() -> None:
    client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        print(
            "Missing GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET in .env.\n"
            "See tools/README_google_oauth.md for how to create these in Google Cloud Console."
        )
        raise SystemExit(1)

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    print("Opening your browser to sign in and approve calendar/email access...")
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(port=0)

    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    print(f"\nConnected. Token saved to {TOKEN_PATH} (gitignored, never commit this file).")
    print("Nova can now use calendar.today, calendar.upcoming, email.recent, and email.draft_reply.")


if __name__ == "__main__":
    main()
