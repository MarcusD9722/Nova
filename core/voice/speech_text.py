from __future__ import annotations

"""Display text vs spoken text.

The UI renders Markdown. XTTS does not — handed the raw reply it will happily
pronounce asterisks, read a URL character by character, and recite a code fence.
So the same turn produces two strings:

    display_text  what the user reads   **RTX 5090:** 32 GB GDDR7
    spoken_text   what Nova says        RTX 5090: 32 gigabytes GDDR7

Two rules govern every transformation here:

* **Never change a technical fact.** Units are expanded, not rounded; model
  numbers, identifiers and quantities pass through untouched. "32 GB" becomes
  "32 gigabytes", never "about 32 gigs".
* **Never invent content.** Anything unspeakable (a table, a code block) is
  replaced by an honest short phrase or dropped, never paraphrased into
  something Nova did not compute.
"""

import re

# ── Units. Longest-first so "GHz" is not eaten by "Hz". ──────────────────────
_UNITS: tuple[tuple[str, str, str], ...] = (
    # (pattern token, singular, plural)
    ("TB", "terabyte", "terabytes"),
    ("GB", "gigabyte", "gigabytes"),
    ("MB", "megabyte", "megabytes"),
    ("KB", "kilobyte", "kilobytes"),
    ("TiB", "tebibyte", "tebibytes"),
    ("GiB", "gibibyte", "gibibytes"),
    ("GHz", "gigahertz", "gigahertz"),
    ("MHz", "megahertz", "megahertz"),
    ("kHz", "kilohertz", "kilohertz"),
    ("Gbps", "gigabits per second", "gigabits per second"),
    ("Mbps", "megabits per second", "megabits per second"),
    ("ms", "millisecond", "milliseconds"),
    ("fps", "frames per second", "frames per second"),
    ("rpm", "R P M", "R P M"),
    ("W", "watt", "watts"),
    ("V", "volt", "volts"),
)

_UNIT_RE = re.compile(
    r'(?<![\w.])(\d+(?:\.\d+)?)\s*(' + "|".join(u[0] for u in _UNITS) + r')(?![\w])'
)
_UNIT_LOOKUP = {u[0]: (u[1], u[2]) for u in _UNITS}

_CODE_FENCE = re.compile(r'```[\s\S]*?```|~~~[\s\S]*?~~~')
_INLINE_CODE = re.compile(r'`([^`\n]+)`')
_MD_LINK = re.compile(r'\[([^\]]*)\]\(([^)]+)\)')
_MD_IMAGE = re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')
_BARE_URL = re.compile(r'(?<![\w@])(?:https?://|www\.)\S+')
_HEADING = re.compile(r'^\s{0,3}#{1,6}\s*(.+?)\s*#*\s*$', re.M)
_HRULE = re.compile(r'^\s{0,3}(?:[-*_]\s*){3,}$', re.M)
_BULLET = re.compile(r'^\s*[-*+]\s+', re.M)
_NUMBERED = re.compile(r'^\s*(\d{1,2})[.)]\s+', re.M)
_BLOCKQUOTE = re.compile(r'^\s*>\s?', re.M)
_TABLE_ROW = re.compile(r'^\s*\|.*\|\s*$', re.M)
_TABLE_SEP = re.compile(r'^\s*\|?[\s:|-]{5,}\|?\s*$', re.M)
_BOLD_ITALIC = re.compile(r'(\*{1,3}|_{1,3})(?=\S)(.+?)(?<=\S)\1', re.S)
_STRIKE = re.compile(r'~~(.+?)~~', re.S)
_FOOTNOTE = re.compile(r'\[\^[^\]]+\]')
_CITATION = re.compile(r'\[(\d+)\]')
_EMOJI = re.compile(
    "[" "\U0001F300-\U0001FAFF" "\U00002700-\U000027BF" "\U0001F000-\U0001F0FF"
    "\U00002600-\U000026FF" "\U0000FE00-\U0000FE0F" "\U00002190-\U000021FF" "]+"
)
_MULTI_PUNCT = re.compile(r'([!?])\1{1,}')
_ELLIPSIS = re.compile(r'\.{3,}')
_WS = re.compile(r'[ \t]+')
_BLANKS = re.compile(r'\n{2,}')


def _expand_units(text: str) -> str:
    def repl(m: re.Match) -> str:
        value, unit = m.group(1), m.group(2)
        singular, plural = _UNIT_LOOKUP[unit]
        try:
            is_one = float(value) == 1.0
        except ValueError:
            is_one = False
        return f"{value} {singular if is_one else plural}"

    return _UNIT_RE.sub(repl, text)


def to_spoken(text: str, *, keep_code_note: bool = True) -> str:
    """Markdown display text -> something XTTS can read aloud naturally."""
    if not text:
        return ""

    s = text

    # Code blocks first, before anything else mangles their contents.
    s = _CODE_FENCE.sub(" (code shown on screen) " if keep_code_note else " ", s)

    # Images carry no spoken value; links keep their label, not their href.
    s = _MD_IMAGE.sub(lambda m: (m.group(1) or "an image"), s)
    s = _MD_LINK.sub(lambda m: (m.group(1) or "a link"), s)

    # Tables: a spoken table is noise. Say that one exists and move on.
    if _TABLE_ROW.search(s):
        s = _TABLE_SEP.sub("", s)
        s = _TABLE_ROW.sub(" (table shown on screen) ", s)
        s = re.sub(r'(?:\s*\(table shown on screen\)\s*)+', " (table shown on screen) ", s)

    s = _HRULE.sub("", s)
    s = _BLOCKQUOTE.sub("", s)
    s = _FOOTNOTE.sub("", s)
    s = _CITATION.sub("", s)

    # Headings and list items read as their own sentences, so the voice lands a
    # full stop instead of running the next item straight on.
    s = _HEADING.sub(lambda m: _as_sentence(m.group(1)), s)
    s = _NUMBERED.sub("", s)
    s = _BULLET.sub("", s)

    s = _INLINE_CODE.sub(r"\1", s)
    s = _BOLD_ITALIC.sub(r"\2", s)
    s = _STRIKE.sub(r"\1", s)

    s = _BARE_URL.sub("a link", s)
    s = _EMOJI.sub(" ", s)

    s = _expand_units(s)

    # "!!!" and "???" are visual emphasis; XTTS just gets confused.
    s = _MULTI_PUNCT.sub(r"\1", s)
    s = _ELLIPSIS.sub("...", s)

    # Each remaining line becomes its own sentence so list items keep their
    # boundaries after the newlines collapse.
    lines = [_as_sentence(ln.strip()) for ln in s.split("\n")]
    s = " ".join(ln for ln in lines if ln)

    s = _WS.sub(" ", s).strip()
    s = re.sub(r'\s+([,.!?;:])', r'\1', s)
    s = re.sub(r'\.\s*\.(?!\.)', ".", s)
    return s.strip()


def _as_sentence(line: str) -> str:
    """Give a fragment a terminator so the chunker can treat it as speakable."""
    line = line.strip()
    if not line:
        return ""
    if line[-1] in ".!?:,;":
        return line
    return line + "."


def has_speakable_content(text: str) -> bool:
    """True when there is anything worth sending to the voice at all."""
    spoken = to_spoken(text, keep_code_note=False)
    return bool(re.search(r'[A-Za-z0-9]', spoken))


def split_display_and_spoken(text: str) -> tuple[str, str]:
    """The pair a turn should carry: what is shown, and what is said."""
    return text, to_spoken(text)
