from __future__ import annotations

"""Lightweight natural-language date-range parsing for memory recall.

Turns phrases like "last Tuesday", "yesterday", "last week", "in June" into a
(since, until) datetime pair so Nova can answer "what did we talk about last
Tuesday". Deliberately small and dependency-free — good enough for the common
conversational cases, not a full temporal grammar.
"""

import re
from datetime import datetime, time, timedelta, timezone

__all__ = ["parse_date_range", "parse_reminder_time", "parse_month_day"]

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 4, "thurs": 3, "fri": 4, "sat": 5, "sun": 6,
}
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
    "oct": 10, "nov": 11, "dec": 12,
}


def _day_bounds(d: datetime) -> tuple[datetime, datetime]:
    start = datetime.combine(d.date(), time.min)
    return start, start + timedelta(days=1) - timedelta(microseconds=1)


def parse_date_range(text: str, now: datetime | None = None) -> tuple[datetime, datetime] | None:
    """Return (since, until) for a recognized date phrase in `text`, else None."""
    t = (text or "").lower()
    now = now or datetime.now()
    today = datetime.combine(now.date(), time.min)

    if re.search(r"\btoday\b", t):
        return _day_bounds(today)
    if re.search(r"\byesterday\b", t):
        return _day_bounds(today - timedelta(days=1))
    if re.search(r"\b(?:day before yesterday)\b", t):
        return _day_bounds(today - timedelta(days=2))

    m = re.search(r"\blast\s+(mon|tues?|wed|thur?s?|fri|sat|sun)\w*\b", t) or re.search(
        r"\b(?:this\s+past|past)\s+(mon|tues?|wed|thur?s?|fri|sat|sun)\w*\b", t
    )
    if m:
        target = _WEEKDAYS.get(m.group(1))
        if target is not None:
            delta = (today.weekday() - target) % 7
            delta = delta or 7  # "last Tuesday" means the most recent past one
            return _day_bounds(today - timedelta(days=delta))

    # bare weekday ("on tuesday") -> most recent past occurrence (incl. today)
    m = re.search(r"\bon\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", t)
    if m:
        target = _WEEKDAYS[m.group(1)]
        delta = (today.weekday() - target) % 7
        return _day_bounds(today - timedelta(days=delta))

    if re.search(r"\blast\s+week\b", t):
        start_this = today - timedelta(days=today.weekday())
        start_last = start_this - timedelta(days=7)
        return start_last, start_this - timedelta(microseconds=1)
    if re.search(r"\bthis\s+week\b", t):
        start_this = today - timedelta(days=today.weekday())
        return start_this, now
    if re.search(r"\blast\s+month\b", t):
        first_this = today.replace(day=1)
        last_month_end = first_this - timedelta(microseconds=1)
        first_last = last_month_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return first_last, last_month_end

    # "in June" / "in June 2026"
    m = re.search(r"\bin\s+(" + "|".join(_MONTHS) + r")\b(?:\s+(\d{4}))?", t)
    if m:
        month = _MONTHS[m.group(1)]
        year = int(m.group(2)) if m.group(2) else now.year
        if not m.group(2) and month > now.month:
            year -= 1  # a future month named without a year means last year's
        start = datetime(year, month, 1)
        end_month = start.replace(year=year + 1, month=1) if month == 12 else start.replace(month=month + 1)
        return start, end_month - timedelta(microseconds=1)

    m = re.search(r"\b(\d+)\s+days?\s+ago\b", t)
    if m:
        return _day_bounds(today - timedelta(days=int(m.group(1))))

    return None


def parse_month_day(text: str) -> tuple[int, int] | None:
    """Extract a (month, day) pair from a free-text date value like a stored
    birthday — "1990-04-12", "April 12", "Apr 12, 1990", "4/12". Returns None
    if nothing recognizable is found (callers should skip, never guess)."""
    t = (text or "").strip().lower()
    if not t:
        return None

    m = re.search(r"\b\d{4}-(\d{1,2})-(\d{1,2})\b", t)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12 and 1 <= day <= 31:
            return month, day

    m = re.search(r"\b(" + "|".join(_MONTHS) + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", t)
    if m:
        month = _MONTHS[m.group(1)]
        day = int(m.group(2))
        if 1 <= day <= 31:
            return month, day

    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(_MONTHS) + r")\b", t)
    if m:
        day = int(m.group(1))
        month = _MONTHS[m.group(2)]
        if 1 <= day <= 31:
            return month, day

    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-]\d{2,4})?\b", t)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12 and 1 <= day <= 31:
            return month, day

    return None


_TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.IGNORECASE)


def _next_time_today_or_tomorrow(text: str, now: datetime, *, force_tomorrow: bool = False) -> datetime | None:
    """Find the first HH:MM-ish mention in `text` and resolve it to the next
    occurrence (today if still upcoming, else tomorrow)."""
    if re.search(r"\bnoon\b", text):
        hour, minute = 12, 0
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if force_tomorrow or candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    if re.search(r"\bmidnight\b", text):
        hour, minute = 0, 0
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if force_tomorrow or candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    m = _TIME_RE.search(text)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = (m.group(3) or "").lower()
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    elif not ampm and hour <= 7:
        # Bare small numbers ("at 8") read as morning by default — "remind me
        # at 8" almost always means 8am, not 8pm, in casual speech.
        pass
    if hour > 23 or minute > 59:
        return None
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if force_tomorrow or candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def _to_utc(dt: datetime) -> datetime:
    """Normalize a naive (assumed local wall-clock) or aware datetime to aware
    UTC. `parse_reminder_time`'s arithmetic happens in local wall-clock time
    (so "at 5pm" means 5pm for Marcus, not 5pm UTC), but reminders are stored
    and compared against `datetime.now(timezone.utc)` elsewhere
    (memory/unifier.py, reminder_worker.py) — returning a naive/local
    datetime here made every reminder's due_at sort as if it were already
    UTC, which for any timezone behind UTC (e.g. US Central, UTC-5) made it
    look already in the past and fire immediately.
    """
    return dt.astimezone(timezone.utc)


def parse_reminder_time(text: str, now: datetime | None = None) -> tuple[datetime, str] | None:
    """Parse a reminder request into (next_due_at, recurrence).

    recurrence is one of: none | daily | weekday | weekly.
    Returns None if no schedulable time phrase is found — callers should treat
    that as "ask the user when", never guess.
    """
    t = (text or "").lower()
    now = now or datetime.now()

    # "in N minutes/hours/days"
    m = re.search(r"\bin\s+(\d+)\s*(minute|min|hour|hr|day)s?\b", t)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit in ("minute", "min"):
            return _to_utc(now + timedelta(minutes=n)), "none"
        if unit in ("hour", "hr"):
            return _to_utc(now + timedelta(hours=n)), "none"
        return _to_utc(now + timedelta(days=n)), "none"

    # "every weekday at ..." (Mon-Fri)
    if re.search(r"\bevery\s+week\s*day\b", t):
        due = _next_time_today_or_tomorrow(t, now)
        if due is None:
            return None
        while due.weekday() >= 5:  # skip Sat/Sun for the first occurrence
            due += timedelta(days=1)
        return _to_utc(due), "weekday"

    # "every monday/tuesday/... at ..."
    m = re.search(r"\bevery\s+(mon|tues?|wed|thur?s?|fri|sat|sun)\w*\b", t)
    if m:
        target = _WEEKDAYS.get(m.group(1))
        due = _next_time_today_or_tomorrow(t, now)
        if due is None or target is None:
            return None
        while due.weekday() != target:
            due += timedelta(days=1)
        return _to_utc(due), "weekly"

    # "every day" / "every morning" / "each morning" / "daily at ..."
    if re.search(r"\bevery\s+(?:day|morning|night|evening)\b|\beach\s+morning\b|\bdaily\b", t):
        due = _next_time_today_or_tomorrow(t, now)
        if due is None:
            return None
        return _to_utc(due), "daily"

    # "tomorrow at ..." (explicit tomorrow, even if the time hasn't passed yet today)
    if re.search(r"\btomorrow\b", t):
        due = _next_time_today_or_tomorrow(t, now, force_tomorrow=True)
        if due is not None:
            return _to_utc(due), "none"

    # Bare "at HH(:MM)(am/pm)" — one-shot, next occurrence
    if re.search(r"\bat\s+\d", t):
        due = _next_time_today_or_tomorrow(t, now)
        if due is not None:
            return _to_utc(due), "none"

    # A bare time expression with no "at"/"in"/"every" ("remind me 5pm")
    due = _next_time_today_or_tomorrow(t, now)
    if due is not None:
        return _to_utc(due), "none"

    return None
