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
    # "thur" is Thursday. It was mapped to 4 (Friday), so "every thur at 8am"
    # scheduled every Friday (audit 2026-11-02, L1).
    "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3,
    "fri": 4, "sat": 5, "sun": 6,
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
    # MORE SPECIFIC FIRST. "day before yesterday" contains "yesterday", so with
    # the plain check first it was unreachable and the phrase silently meant
    # yesterday (audit, L2). Same for "the day before yesterday".
    if re.search(r"\bday\s+before\s+yesterday\b", t):
        return _day_bounds(today - timedelta(days=2))
    if re.search(r"\byesterday\b", t):
        return _day_bounds(today - timedelta(days=1))

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

#: Default times for a named part of the day, used when no clock time is given
#: ("remind me Saturday morning").
_DAY_PARTS = {"morning": (9, 0), "afternoon": (14, 0), "evening": (18, 0),
              "night": (20, 0), "tonight": (20, 0), "noon": (12, 0),
              "midday": (12, 0), "midnight": (0, 0)}

#: "the 15th", "on the 3rd" — a day of the month with no month named.
_DAY_OF_MONTH_RE = re.compile(r"\b(?:on\s+)?the\s+(\d{1,2})(?:st|nd|rd|th)\b", re.IGNORECASE)

#: "December 5th", "Dec 5", "5 December", "12/5".
_MONTH_DAY_RE = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", re.IGNORECASE)
_DAY_MONTH_RE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(" + "|".join(_MONTHS) + r")\b", re.IGNORECASE)
_NUMERIC_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")

#: "on Friday", "next Monday", "this Saturday" — ONE occurrence, not a series.
#: "every Friday" is handled before this and stays recurring.
_ONE_SHOT_WEEKDAY_RE = re.compile(
    r"\b(?:(next|this|on|come)\s+)?"
    r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"mon|tues?|wed|thur?s?|fri|sat|sun)\b",
    re.IGNORECASE,
)

#: "in 2 weeks", "in 3 months", "in a fortnight".
_IN_LONG_RE = re.compile(r"\bin\s+(?:(\d+)|a|an)\s*(week|month|year)s?\b", re.IGNORECASE)


def _explicit_time(text: str) -> tuple[int, int, bool] | None:
    """The clock time named in `text` as (hour, minute, half_of_day_known).

    A BARE hour (no am/pm, no part of the day) resolves to whichever of the two
    readings comes sooner — see `_resolve_bare_hour`; this returns the 24-hour
    value only when the text actually says which half of the day is meant, and
    otherwise returns the raw 1-12 hour with `half_of_day_known` False, leaving
    the choice to the caller that knows "now".

    That third element is the ONE answer to "did they say am or pm?". Callers
    used to re-derive it with their own `\\bam\\b` search, which can never match
    "9am" — no word boundary sits between a digit and a letter — so "at 9am"
    was re-read as a bare 9 and scheduled for 9 PM.
    """
    half_known = False
    for word, (h, m) in _DAY_PARTS.items():
        if re.search(rf"\b{word}\b", text, re.IGNORECASE):
            # A day part plus an explicit time ("Saturday morning at 8") means
            # the explicit time; the part only sets the half of the day.
            m_time = _TIME_RE.search(text)
            if m_time is None:
                return (h, m, True)
            half_known = True
            break
    m_time = _TIME_RE.search(text)
    if m_time is None:
        return None
    hour = int(m_time.group(1))
    minute = int(m_time.group(2) or 0)
    ampm = (m_time.group(3) or "").lower()
    if ampm == "pm":
        hour = 12 if hour == 12 else hour + 12
        half_known = True
    elif ampm == "am":
        hour = 0 if hour == 12 else hour
        half_known = True
    elif hour <= 12:
        # Bare hour: a day part in the sentence disambiguates it.
        if re.search(r"\b(?:afternoon|evening|night|tonight)\b", text, re.IGNORECASE) and hour < 12:
            hour += 12
        elif re.search(r"\b(?:morning)\b", text, re.IGNORECASE) and hour == 12:
            hour = 0
    if hour > 23 or minute > 59:
        return None
    return (hour, minute, half_known)


def _is_bare_hour(hour: int, half_known: bool) -> bool:
    """True when nothing was said about whether a 1-12 hour is AM or PM.

    For TODAY that is resolvable — `_resolve_bare_hour` takes whichever reading
    comes sooner. For any other day both readings are equally plausible, and
    guessing is what H14 was about, so the caller returns None and asks.
    """
    return hour <= 12 and not half_known


def _resolve_bare_hour(now: datetime, hour: int, minute: int, half_known: bool) -> tuple[int, int]:
    """For a bare hour with no am/pm, pick the reading that comes SOONEST.

    "at 3" at 2pm means 3pm, an hour away — not 3 AM tomorrow, which is what
    the old morning-by-default rule produced (audit, H14).
    """
    if not _is_bare_hour(hour, half_known):
        return (hour, minute)
    today = now.replace(hour=hour % 24, minute=minute, second=0, microsecond=0)
    other = now.replace(hour=(hour + 12) % 24, minute=minute, second=0, microsecond=0)
    options = sorted(c for c in (today, other) if c > now)
    if options:
        return (options[0].hour, options[0].minute)
    return (hour, minute)


def _add_months(d: datetime, n: int) -> datetime:
    month = d.month - 1 + n
    year = d.year + month // 12
    month = month % 12 + 1
    day = d.day
    while day > 28:
        try:
            return d.replace(year=year, month=month, day=day)
        except ValueError:
            day -= 1
    return d.replace(year=year, month=month, day=day)


def _target_date(text: str, now: datetime) -> tuple[datetime, str] | None:
    """The DAY a one-shot reminder falls on, and the text with it REMOVED.

    Removing it matters: a date's own digits look exactly like a clock time.
    "12/25 at 9am" was scheduled for 12:00 because the month matched the time
    pattern first, and "in 2 weeks" became 02:00 for the same reason.
    """
    def _cut(m: "re.Match[str]") -> str:
        return (text[:m.start()] + " " + text[m.end():]).strip()

    # An explicit calendar date.
    for rx, order in ((_MONTH_DAY_RE, "md"), (_DAY_MONTH_RE, "dm")):
        m = rx.search(text)
        if m:
            if order == "md":
                month, day = _MONTHS[m.group(1).lower()], int(m.group(2))
            else:
                month, day = _MONTHS[m.group(2).lower()], int(m.group(1))
            try:
                target = now.replace(month=month, day=day)
            except ValueError:
                return None
            if target.date() < now.date():
                try:
                    target = target.replace(year=now.year + 1)
                except ValueError:
                    return None
            return target, _cut(m)
    m = _NUMERIC_DATE_RE.search(text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year = int(m.group(3) or now.year)
        if year < 100:
            year += 2000
        try:
            target = now.replace(year=year, month=month, day=day)
        except ValueError:
            return None
        if target.date() < now.date():
            return None
        return target, _cut(m)
    # A day of the month with no month: the next one to come round.
    m = _DAY_OF_MONTH_RE.search(text)
    if m:
        day = int(m.group(1))
        try:
            target = now.replace(day=day)
        except ValueError:
            return None
        if target.date() < now.date():
            target = _add_months(target, 1)
        return target, _cut(m)
    # A weekday.
    m = _ONE_SHOT_WEEKDAY_RE.search(text)
    if m:
        qualifier = (m.group(1) or "").lower()
        target_wd = _WEEKDAYS.get(m.group(2).lower())
        if target_wd is None:
            return None
        delta = (target_wd - now.weekday()) % 7
        if delta == 0 and qualifier == "next":
            # Named today: "next Monday" ON a Monday is the one after this.
            delta = 7
        return now + timedelta(days=delta), _cut(m)
    return None


def _next_time_today_or_tomorrow(
    text: str,
    now: datetime,
    *,
    force_tomorrow: bool = False,
    day_is_named: bool = False,
) -> datetime | None:
    """Resolve a clock time in `text` to its next occurrence.

    Today when it is still ahead, otherwise tomorrow. `force_tomorrow` is for
    "tomorrow at …", where the day is named and the time may not have passed
    yet. The hour itself comes from `_explicit_time`, so the am/pm and day-part
    rules live in exactly one place.

    `day_is_named` (implied by `force_tomorrow`) says the occurrence is NOT
    today, so the sooner-of-the-two-readings rule has nothing to work with: a
    bare 1-12 hour is then genuinely ambiguous and the caller should ask.
    """
    hm = _explicit_time(text)
    if hm is None:
        return None
    hour, minute, half_known = hm
    if force_tomorrow or day_is_named:
        if _is_bare_hour(hour, half_known):
            # "tomorrow at 3" names the day but not the half of it, and a day
            # that is not today gives no sooner reading to break the tie.
            # Guessing 3 AM is exactly what H14 was about, so return None and
            # let the tool ask which was meant.
            return None
    else:
        hour, minute = _resolve_bare_hour(now, hour, minute, half_known)
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

    # "in 2 weeks" / "in 3 months" / "in a year". These fell through to the
    # bare-number fallback at the bottom, which read the "2" as two o'clock and
    # scheduled TOMORROW at 2 AM (audit, H14).
    m = _IN_LONG_RE.search(t)
    if m:
        n = int(m.group(1)) if m.group(1) else 1
        unit = m.group(2)
        if unit == "week":
            target = now + timedelta(weeks=n)
        elif unit == "month":
            target = _add_months(now, n)
        else:
            target = _add_months(now, 12 * n)
        # The phrase's own number is not a clock time: "in 2 weeks" was being
        # scheduled for 02:00. With nothing else said, keep the current time of
        # day, which is the predictable reading of "in two weeks".
        rest = (t[:m.start()] + " " + t[m.end():]).strip()
        hm = _explicit_time(rest)
        if hm is not None:
            hour, minute = _resolve_bare_hour(now, *hm)
            target = target.replace(hour=hour, minute=minute, second=0, microsecond=0)
        else:
            target = target.replace(second=0, microsecond=0)
        return _to_utc(target), "none"

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
        # The series' day can be up to six days from today, so a bare hour's
        # "sooner reading today" would be moved to another day afterwards and
        # silently become a guess. day_is_named makes that an ask instead.
        due = _next_time_today_or_tomorrow(
            t, now, day_is_named=target is not None and target != now.weekday())
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
        if due is None:
            # The user said "tomorrow". No branch below may reinterpret that:
            # falling through sent "tomorrow at 3" to the bare-time branch,
            # which scheduled it for TODAY at 3pm.
            return None
        return _to_utc(due), "none"

    # A NAMED DAY OR DATE: "on Friday at 3pm", "next Monday at 9am",
    # "December 5th at noon", "at noon on the 15th", "Saturday morning at 8".
    #
    # None of these had any handling at all. They fell through to the bare-time
    # branch below, which only ever looks at today and tomorrow — so "on Friday
    # at 3pm" was scheduled for TODAY at 3pm and "next Monday at 9am" for
    # tomorrow (audit, H14). Checked AFTER the recurring branches so "every
    # Friday" stays a series.
    dated = _target_date(t, now)
    if dated is not None:
        target, rest = dated
        hm = _explicit_time(rest)
        if hm is None:
            # A day with no time at all is ambiguous enough to ask about, and
            # the caller's contract is that None means "ask, never guess".
            return None
        hour, minute, half_known = hm
        if target.date() == now.date():
            hour, minute = _resolve_bare_hour(now, hour, minute, half_known)
        elif _is_bare_hour(hour, half_known):
            # "on Friday at 3" — the day is named, the half of the day is not,
            # and there is no sooner reading to pick. Ask (H14).
            return None
        due = target.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if due <= now:
            # Named today but already past: the next time that day comes round.
            due += timedelta(days=7 if _ONE_SHOT_WEEKDAY_RE.search(t) else 1)
        return _to_utc(due), "none"

    # Bare "at HH(:MM)(am/pm)" — one-shot, next occurrence. A bare hour picks
    # the SOONER of the two readings: "at 3" at 2pm is 3pm, not 3 AM tomorrow.
    if re.search(r"\bat\s+\d", t):
        hm = _explicit_time(t)
        if hm is not None:
            hour, minute = _resolve_bare_hour(now, *hm)
            due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if due <= now:
                due += timedelta(days=1)
            return _to_utc(due), "none"
        due = _next_time_today_or_tomorrow(t, now)
        if due is not None:
            return _to_utc(due), "none"

    # A bare time expression with no "at"/"in"/"every" ("remind me 5pm")
    due = _next_time_today_or_tomorrow(t, now)
    if due is not None:
        return _to_utc(due), "none"

    return None
