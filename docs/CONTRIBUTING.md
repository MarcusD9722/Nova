# Contributing to Nova (humans and AI sessions alike)

Nova is edited by Marcus, by Claude sessions, and occasionally by other AI
coding sessions. These rules exist because breaking them has already caused a
real production incident.

## The one-writer rule

**Never edit this repo while another session is editing it, and never edit
source files while Nova is running if the change spans multiple files.**

Postmortem (2026-07-17): a session edited `memory/unifier.py` ~13 minutes
after Nova booted. The running process held the old bytecode for one module
and the new code's expectations for another — every conversation turn crashed
with a NameError until restart, silently losing that session's memory ingest.

Practical form:
1. One editing session at a time. Finish, verify, commit — then hand off.
2. After edits, **restart Nova** before judging behavior.
3. If you find uncommitted changes you didn't make, stop and ask — don't
   revert, don't build on top blindly.

## The verification ritual (every change set)

1. `.\run_tests.ps1` — all suites green (add suites for new behavior).
2. `cd frontend && npm run build` — clean.
3. Real boot + a live chat exercise of the changed path.
4. Clean up any test data written to `memory_data/`.
5. **Commit.** Small, per-milestone commits with honest messages. The
   five-months-uncommitted era is over and stays over.

## Invariants (do not weaken — see docs/ARCHITECTURE.md §1)

- Honest failure: no capability ever fakes success or invents state.
- Self-edits to Nova's own code are propose → approve → apply → boot-test →
  auto-rollback. Always.
- SQLite is the source of truth; Chroma is a rebuildable index.
- Secrets only in `.env` / `credentials/` — both deny-listed and gitignored.
- Schema changes go through `_MIGRATIONS` in
  `memory/backends/sqlite_backend.py` (plus the create block), never ad-hoc.
- Screen/camera capture requires explicit user action. No silent senses.
