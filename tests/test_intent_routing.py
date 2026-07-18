"""Phase 1 verification: question detection + project-name extraction.

These are the exact phrasings from Marcus's audit log that must NOT create a
project, alongside the ones that still must build.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.intent import is_question, strip_preamble  # noqa: E402
from core.project_builder import ProjectBuilder, NEEDS_NAME  # noqa: E402

_fail = []


def check(cond, msg):
    print(("  OK  " if cond else " FAIL ") + msg)
    if not cond:
        _fail.append(msg)


# ── is_question ──────────────────────────────────────────────────────────────
print("== is_question ==")
questions = [
    "What other improvements can we make to the flappybird game?",
    "I meant What other improvements can we make to the flappy-bird game?",
    "What other improvements can we make to flappy-bird",           # no '?'
    "So what should we add next?",
    "Nova, why doesn't the restart button show up?",
    "how do I run this",
    "is flappybird a good game?",
    "Any ideas for the next feature?",
]
not_questions = [
    "make a snake game called Cobra",
    "let's start a project named Serpent",
    "add a restart button to flappy-bird",
    "fix the collision bug",
    "Create a leaderboard for the flappy bird project",
    "improve the game",
    "it still doesn't work",
]
for q in questions:
    check(is_question(q), f"question: {q!r}")
for q in not_questions:
    check(not is_question(q), f"not-question: {q!r}")

check(strip_preamble("I meant What other improvements...").lower().startswith("what"),
      "strip_preamble removes 'I meant'")
check(strip_preamble("So, Nova, what's next?").lower().startswith("what"),
      "strip_preamble removes stacked 'So, Nova,'")

# ── extract_start_request ────────────────────────────────────────────────────
print("\n== extract_start_request ==")
# The junk-slug incidents: a build-shaped sentence with NO explicit name must
# ask for one, never scrape the sentence.
for junk in [
    "What other improvements can we make to the flappybird game?",
    "Create a leaderboard for the flappy bird project",
    "make me a snake game",
]:
    r = ProjectBuilder.extract_start_request(junk)
    is_needs_name = r is not None and r[0] == NEEDS_NAME
    # a pure question may also just not match START_RE at all — either way, no real name
    got_real_name = r is not None and r[0] not in (NEEDS_NAME,) and r[0]
    check(not got_real_name, f"no scraped name from: {junk!r} (got {r[0] if r else None!r})")

# Named requests still yield the real name.
named = {
    "make a snake game called Cobra": "Cobra",
    "let's start a project named Serpent": "Serpent",
    "create a project called Tetris": "Tetris",
    "build me a calculator app called QuickCalc": "QuickCalc",
    "let's start a project Pong": "Pong",
}
for msg, expected in named.items():
    r = ProjectBuilder.extract_start_request(msg)
    ok = r is not None and r[0].strip().lower() == expected.lower()
    check(ok, f"named build {msg!r} -> {expected!r} (got {r[0] if r else None!r})")

# Non-build chatter returns None.
for chatter in ["how are you tonight?", "I had a long day", "thanks Nova"]:
    check(ProjectBuilder.extract_start_request(chatter) is None, f"no build from: {chatter!r}")

print("\nRESULT:", "ALL PASS" if not _fail else f"{len(_fail)} FAILURES")
sys.exit(1 if _fail else 0)
