from __future__ import annotations

"""Authoritative answers for "when is <someone>'s birthday?".

WHY THIS EXISTS
---------------
Live, Marcus asked:

    "When is Leslie's birthday and when is my birthday?"

and got "Sorry — I came up empty on that one." Both birthdays were in memory:
asked one at a time, immediately afterwards, Nova answered both correctly.

Nothing was wrong with the memory. The route was wrong. A stored, exact,
structured fact was being answered by: semantic search -> prompt injection ->
stochastic generation, and when that generation came back empty the whole turn
fell through to a generic apology. SQLite is the authoritative store in this
architecture; a question whose answer is a row in it should not depend on a
model producing tokens.

So this module resolves the question deterministically and only reports what is
actually stored. When a value is genuinely missing it says so — an honest "I
don't have that" is the point, not a fallback.

AGE IS NOT A SCALAR
-------------------
"Mateo is three years old and he turns four on September 16th" must not be
stored as `age = 3`. That is true for a few months and silently false forever
after. Age is recorded as an OBSERVATION with the date it was made, and the
birth date is derived from it only when the arithmetic is unambiguous — and is
then marked as derived rather than stated, because Marcus never said the year.
"""

import re
from dataclasses import dataclass, field
from datetime import date

__all__ = [
    "DateQuery",
    "PersonDate",
    "parse_date_query",
    "age_on",
    "derive_birth_year",
    "format_month_day",
]

#: Attribute names this module reads and writes.
BIRTHDAY_KEY = "birthday"
BIRTH_DATE_KEY = "birth_date"
AGE_OBSERVATION_KEY = "age_observation"
AGE_OBSERVED_ON_KEY = "age_observed_on"
BIRTH_DATE_SOURCE_KEY = "birth_date_source"

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December"]

#: "when is my birthday", "when's Leslie's birthday", "what are their birthdays"
_DATE_WORD = r"(?:birthdays?|bday|anniversar(?:y|ies))"
_ASKS_RE = re.compile(
    r"\b(?:when(?:'?s)?|what(?:'?s)?|which\s+day|what\s+day)\b[^?]{0,80}?\b" + _DATE_WORD + r"\b"
    r"|\b" + _DATE_WORD + r"\b[^?]{0,40}?\b(?:when|what\s+day|which\s+day)\b",
    re.IGNORECASE,
)

#: A possessive subject: "my", "Leslie's", "Mateo and Liam's".
_POSSESSIVE_RE = re.compile(
    r"\b(?P<who>my|your|his|her|their|"
    r"(?:[A-Z][\w'-]+(?:\s+and\s+[A-Z][\w'-]+)*))\s*(?:'s|s'|')?\s+" + _DATE_WORD,
    re.IGNORECASE,
)

_SELF_WORDS = {"my", "mine", "i", "me"}

#: Words that look like a capitalised name but are not one.
_NOT_A_NAME = {
    "when", "what", "which", "who", "the", "a", "an", "and", "or", "is", "are",
    "was", "were", "do", "does", "did", "tell", "me", "please", "nova", "hey",
    "so", "also", "both", "everyone", "their", "there",
}


@dataclass
class DateQuery:
    """A parsed "when is X's birthday" question, possibly about several people."""

    attribute: str                      # "birthday" | "anniversary"
    subjects: list[str] = field(default_factory=list)   # "" means the speaker

    def __bool__(self) -> bool:
        return bool(self.subjects)


@dataclass
class PersonDate:
    """What is actually stored for one subject."""

    subject: str                # display name, or "" for the speaker
    known: bool
    month: int | None = None
    day: int | None = None
    year: int | None = None
    derived_year: bool = False


def _clean_name(raw: str) -> str:
    """Trim a captured subject to the bare name.

    The possessive travels with the capture ("Robin's birthday" -> "Robin's")
    because a name may legitimately contain an apostrophe (O'Brien), so the
    trailing possessive is removed here rather than excluded from the pattern.
    """
    name = re.sub(r"\s+", " ", (raw or "").strip()).strip(",.;:\"")
    name = re.sub(r"(?:'s|'S|s'|')$", "", name).strip()
    return name


def parse_date_query(text: str) -> DateQuery | None:
    """Parse a date question into its subjects, or None if it isn't one.

    Handles the combined form that failed live — "When is Leslie's birthday and
    when is my birthday?" — by collecting EVERY possessive in the sentence
    rather than the first, which is why one-at-a-time worked and the pair did
    not.
    """
    raw = (text or "").strip()
    if not raw or not _ASKS_RE.search(raw):
        return None

    attribute = "anniversary" if re.search(r"anniversar", raw, re.IGNORECASE) else BIRTHDAY_KEY

    subjects: list[str] = []
    for m in _POSSESSIVE_RE.finditer(raw):
        who = _clean_name(m.group("who"))
        low = who.lower()
        if low in _SELF_WORDS:
            if "" not in subjects:
                subjects.append("")
            continue
        # "Mateo and Liam's birthdays" — one possessive, two people.
        for part in re.split(r"\s+and\s+", who):
            name = _clean_name(part)
            if not name or name.lower() in _NOT_A_NAME:
                continue
            if name not in subjects:
                subjects.append(name)

    if not subjects:
        return None
    return DateQuery(attribute=attribute, subjects=subjects)


def parse_stored_date(value: str) -> tuple[int | None, int | None, int | None] | None:
    """(year, month, day) from a stored value. Year is None for a bare MM-DD."""
    v = (value or "").strip()
    if not v:
        return None

    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", v)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))

    m = re.match(r"^(\d{1,2})-(\d{1,2})$", v)
    if m:
        return None, int(m.group(1)), int(m.group(2))

    # "September 16", "Sept 16th", "16 September"
    m = re.search(r"\b([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?\b", v)
    if m and m.group(1).lower() in _MONTHS:
        return None, _MONTHS[m.group(1).lower()], int(m.group(2))
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\b", v)
    if m and m.group(2).lower() in _MONTHS:
        return None, _MONTHS[m.group(2).lower()], int(m.group(1))

    m = re.match(r"^(\d{1,2})/(\d{1,2})(?:/(\d{4}))?$", v)
    if m:
        year = int(m.group(3)) if m.group(3) else None
        return year, int(m.group(1)), int(m.group(2))
    return None


def format_month_day(month: int, day: int, year: int | None = None) -> str:
    name = _MONTH_NAMES[month] if 1 <= month <= 12 else str(month)
    return f"{name} {day}" + (f", {year}" if year else "")


def age_on(birth_year: int, birth_month: int, birth_day: int, on: date) -> int:
    """Age in whole years on a given day. The reason age is never stored flat."""
    years = on.year - birth_year
    if (on.month, on.day) < (birth_month, birth_day):
        years -= 1
    return years


def derive_birth_year(*, stated_age: int, birth_month: int, birth_day: int,
                      observed_on: date, turns_next: int | None = None) -> int | None:
    """Birth year implied by "X is N and turns N+1 on <month day>", or None.

    Only returns a year when the arithmetic is unambiguous. The caller MUST
    record the result as derived — Marcus said an age and a day, never a year,
    and a derived value must not be presented as a stated one.
    """
    if stated_age < 0 or not (1 <= birth_month <= 12) or not (1 <= birth_day <= 31):
        return None
    if turns_next is not None and turns_next != stated_age + 1:
        # "is three and turns five" — contradictory; refuse rather than guess.
        return None

    # The next occurrence of the birthday on/after the observation date is the
    # day they reach stated_age + 1.
    next_year = observed_on.year
    if (birth_month, birth_day) < (observed_on.month, observed_on.day):
        next_year += 1
    return next_year - (stated_age + 1)
