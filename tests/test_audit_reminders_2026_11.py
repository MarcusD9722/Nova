"""Audit 2026-11-02, H14: reminder phrasings that were silently scheduled wrong.

The finding's own table, at Wednesday 2pm:

    "at 3"                 -> Thursday 3 AM
    "on Friday at 3pm"     -> Today 3pm
    "next Monday at 9am"   -> Tomorrow 9am
    "in 2 weeks"           -> Tomorrow 2 AM
    "in 3 months"          -> Tomorrow 3 AM
    "December 5th at noon" -> Tomorrow noon

Every row is here, plus the phrasings the fix could plausibly have broken
(recurring series, "in N minutes", bare times) and the ones that must return
None so the tool asks instead of guessing.

The parser returns UTC, so every expectation is written in local wall-clock
time and converted the way the parser converts: an expectation written in UTC
would pass on a machine in UTC and fail everywhere else.

Run as a script (`python tests/test_audit_reminders_2026_11.py`), which is what
run_tests.ps1 does — a pytest-only file exits 0 under the gate without running
a single assertion.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from core.dates import parse_reminder_time  # noqa: E402
from harness import Checks  # noqa: E402

check = Checks()

#: Wednesday 4 November 2026, 14:37:12 local. Not on the hour, so a bare hour
#: can never coincide exactly with "now" and make an assertion accidentally
#: true; the odd seconds also pin down which branches zero them.
NOW = datetime(2026, 11, 4, 14, 37, 12)


def _local(y: int, mo: int, d: int, h: int, mi: int, s: int = 0) -> datetime:
    """A local wall-clock expectation, in UTC, the way the parser returns it."""
    return datetime(y, mo, d, h, mi, s).astimezone(timezone.utc)


def _show(dt: datetime) -> str:
    return dt.astimezone().strftime("%a %Y-%m-%d %H:%M:%S")


# (text, expected due time, expected recurrence)
SCHEDULED = [
    # ── the finding's own table ──────────────────────────────────────────────
    # "at 3" at 2:37pm is 3pm, twenty minutes away, not 3 AM tomorrow.
    ("remind me at 3", _local(2026, 11, 4, 15, 0), "none"),
    ("remind me on Friday at 3pm", _local(2026, 11, 6, 15, 0), "none"),
    ("remind me next Monday at 9am", _local(2026, 11, 9, 9, 0), "none"),
    # The phrase's own digits are not a clock time.
    ("remind me in 2 weeks", _local(2026, 11, 18, 14, 37), "none"),
    ("remind me in 3 months", _local(2027, 2, 4, 14, 37), "none"),
    ("remind me December 5th at noon", _local(2026, 12, 5, 12, 0), "none"),

    # ── dates and days ──────────────────────────────────────────────────────
    ("remind me at noon on the 15th", _local(2026, 11, 15, 12, 0), "none"),
    ("remind me Saturday morning at 8", _local(2026, 11, 7, 8, 0), "none"),
    ("remind me tomorrow at 9am", _local(2026, 11, 5, 9, 0), "none"),
    # The month must not be read as an hour: this was scheduling 12:00.
    ("remind me 12/25 at 9am", _local(2026, 12, 25, 9, 0), "none"),
    # The day of the month already past this month: next month's.
    ("remind me on the 3rd at 10am", _local(2026, 12, 3, 10, 0), "none"),
    # Wednesday named on a Wednesday, time already past: next Wednesday.
    ("remind me on Wednesday at 9am", _local(2026, 11, 11, 9, 0), "none"),
    # Wednesday named on a Wednesday, time still ahead: today.
    ("remind me on Wednesday at 6pm", _local(2026, 11, 4, 18, 0), "none"),
    # "next Wednesday" ON a Wednesday skips this one.
    ("remind me next Wednesday at 6pm", _local(2026, 11, 11, 18, 0), "none"),

    # ── relative offsets ────────────────────────────────────────────────────
    # A plain offset keeps the seconds: "in 30 minutes" means thirty minutes,
    # not thirty minutes rounded down to the start of a minute.
    ("remind me in 30 minutes", _local(2026, 11, 4, 15, 7, 12), "none"),
    ("remind me in 2 hours", _local(2026, 11, 4, 16, 37, 12), "none"),
    ("remind me in 3 days", _local(2026, 11, 7, 14, 37, 12), "none"),
    # Weeks and months land on a wall-clock minute instead.
    ("remind me in a week", _local(2026, 11, 11, 14, 37), "none"),

    # ── bare times, today or tomorrow ───────────────────────────────────────
    ("remind me at 7:30", _local(2026, 11, 4, 19, 30), "none"),
    ("remind me at 9pm", _local(2026, 11, 4, 21, 0), "none"),
    ("remind me at 6am", _local(2026, 11, 5, 6, 0), "none"),
    ("remind me tonight at 9", _local(2026, 11, 4, 21, 0), "none"),
    ("remind me 5pm", _local(2026, 11, 4, 17, 0), "none"),

    # ── series ──────────────────────────────────────────────────────────────
    ("remind me every day at 8pm", _local(2026, 11, 4, 20, 0), "daily"),
    ("remind me every morning at 7", _local(2026, 11, 5, 7, 0), "daily"),
    ("remind me every weekday at 7am", _local(2026, 11, 5, 7, 0), "weekday"),
    ("remind me every Monday at 9am", _local(2026, 11, 9, 9, 0), "weekly"),
    ("remind me every Friday at 5pm", _local(2026, 11, 6, 17, 0), "weekly"),
]


#: Phrasings with no schedulable time. The contract is None, which
#: `_reminder_set` in core/tooling.py turns into "ask the user to be more
#: specific" — never a guess.
ASK_INSTEAD = [
    "remind me sometime",
    "remind me later",
    "remind me when you can",
    "remind me soon",
    # A day with no time at all.
    "remind me on Friday",
    "remind me tomorrow",
    "remind me next Monday",
    "remind me December 5th",
    # A day named with a bare hour: 3 AM or 3 PM is a coin toss, and unlike
    # "at 3" today there is no sooner reading to break the tie.
    "remind me on Friday at 3",
    "remind me tomorrow at 3",
    "remind me every Monday at 9",
]


def test_scheduled_at_the_time_actually_said() -> None:
    check.section("scheduled for the time that was actually said")
    for text, expected, recurrence in SCHEDULED:
        parsed = parse_reminder_time(text, now=NOW)
        if parsed is None:
            check(False, f"{text!r} -> found no time at all "
                         f"(expected {_show(expected)})")
            continue
        due, got = parsed
        check(due == expected,
              f"{text!r} -> {_show(due)}"
              + ("" if due == expected else f"  EXPECTED {_show(expected)}"))
        check(got == recurrence,
              f"{text!r} recurrence {got!r}"
              + ("" if got == recurrence else f"  EXPECTED {recurrence!r}"))


def test_ambiguous_phrasings_ask_rather_than_guess() -> None:
    check.section("ambiguous phrasings return None so she asks")
    for text in ASK_INSTEAD:
        parsed = parse_reminder_time(text, now=NOW)
        check(parsed is None,
              f"{text!r} -> "
              + ("None" if parsed is None
                 else f"GUESSED {_show(parsed[0])} instead of asking"))


def test_due_times_are_utc_aware() -> None:
    """A naive local datetime sorts as UTC elsewhere and fires immediately."""
    check.section("due_at is timezone-aware UTC")
    bad = []
    for text, _expected, _rec in SCHEDULED:
        parsed = parse_reminder_time(text, now=NOW)
        if parsed is None or parsed[0].tzinfo is None:
            bad.append(text)
        elif parsed[0].utcoffset() != timezone.utc.utcoffset(parsed[0]):
            bad.append(text)
    check(not bad, f"every due_at is aware UTC ({len(SCHEDULED)} phrasings)"
                   + ("" if not bad else f"  NAIVE: {bad[:3]}"))


def test_no_reminder_is_due_in_the_past() -> None:
    """Whatever the day, the first firing must still be ahead."""
    check.section("no reminder is created already overdue")
    now_utc = NOW.astimezone(timezone.utc)
    past = [t for t, _e, _r in SCHEDULED
            if (p := parse_reminder_time(t, now=NOW)) is not None
            and p[0] <= now_utc]
    check(not past, f"every first occurrence is in the future"
                    + ("" if not past else f"  PAST: {past}"))


def main() -> None:
    test_scheduled_at_the_time_actually_said()
    test_ambiguous_phrasings_ask_rather_than_guess()
    test_due_times_are_utc_aware()
    test_no_reminder_is_due_in_the_past()
    check.finish()


if __name__ == "__main__":
    main()
