# Google Calendar + Gmail Setup (PI1)

Gives Nova read-only calendar awareness and read + **draft-only** email —
she can summarize your inbox and draft a reply, but can never send one
herself. This is the first cloud/OAuth dependency in an otherwise fully
local project, so it's opt-in and isolated: nothing here runs until you
complete this setup yourself.

## 1. Create a Google Cloud OAuth client (you do this, once)

This step needs your own Google account and can't be done on your behalf:

1. Go to https://console.cloud.google.com/ and create a project (or use an
   existing one).
2. Enable the **Google Calendar API** and **Gmail API** for that project
   (APIs & Services → Library).
3. Configure the OAuth consent screen (APIs & Services → OAuth consent
   screen) — choose **External**, fill in the required fields. While the
   app is in "Testing" mode, add your own Google account under **Test
   users** (required, or the consent screen will refuse to authorize you).
4. Create credentials (APIs & Services → Credentials → Create Credentials
   → OAuth client ID) with application type **Desktop app**.
5. Copy the generated **Client ID** and **Client Secret**.

## 2. Add the client to `.env`

```
GOOGLE_OAUTH_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_OAUTH_CLIENT_SECRET=your-client-secret
```

## 3. Run the one-time consent script

```powershell
.\venv\Scripts\python.exe tools\google_oauth_setup.py
```

This opens your browser to sign in and approve access under your own
account. On success it writes `credentials/google_token.json` (gitignored,
and protected the same way `.env`/`model`/`memory_data` are — Nova's guarded
self-editing can never read or write into `credentials/`).

## What Nova can and can't do

- `calendar.today` / `calendar.upcoming` — read-only. She can never create,
  edit, or delete an event.
- `email.recent` — read-only (sender, subject, snippet; not full bodies).
- `email.draft_reply` — creates a **Gmail draft** you review and send
  yourself. The OAuth scope requested (`gmail.compose`, not `gmail.send`)
  makes sending structurally impossible from this integration, not just
  something she's told not to do.

Calendar events and an unread-email *count* (never content) get folded into
the morning briefing once connected. Full inbox summarization stays
on-demand — she won't scan your email unless you ask.

## Until you do this

Every calendar/email tool call honestly reports "Google account not
connected" with a pointer back to this file — same as every other
not-yet-configured integration in this project (weather, maps, Discord).
