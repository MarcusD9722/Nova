from __future__ import annotations

"""THE contract for what a live project directory may be called.

Before this module, two subsystems defined the same namespace differently:

    ProjectBuilder.slugify()     lowercase, ASCII, hyphens, max 48, Win32 remap
    ProjectManager._sanitize()   case-preserving, allowed . and _, no length cap,
                                 no reserved-device handling

So `project.start_build` and `project.scaffold` could produce two different
directories for one human name, and only one of the two paths was protected from
Win32 device names. `slugify("CON")` was fixed in isolation and
`ProjectManager.scaffold_project("CON")` still went straight to `mkdir`.

One namespace, one owner. `slugify()` remains as a compatibility wrapper that
delegates here.

TRASH ENTRIES ARE DELIBERATELY NOT COVERED. A trash id is
`<slug>--<YYYYmmdd-HHMMSS>`, whose timestamp needs its own rules; forcing the live
contract onto it would corrupt existing entries. `safe_trash_entry()` handles that
separately.
"""

import re

__all__ = [
    "MAX_SLUG_LEN",
    "WIN_RESERVED",
    "canonical_project_slug",
    "is_canonical_slug",
    "safe_live_component",
    "safe_trash_entry",
]

#: A live project directory component is exactly this.
_SLUG_RE = re.compile(r"\A[a-z0-9-]+\Z")

MAX_SLUG_LEN = 48

#: Win32 reserved device names. A directory named for one of these is a genuine
#: hazard on Windows, which is Nova's host platform.
WIN_RESERVED = frozenset(
    ["con", "prn", "aux", "nul"]
    + [f"com{i}" for i in range(1, 10)]
    + [f"lpt{i}" for i in range(1, 10)]
)


def canonical_project_slug(name: str) -> str:
    """The one identity a NEW live project directory may have.

    Lowercase ASCII `[a-z0-9-]`, separators collapsed, bounded length, never
    empty, and never a Win32 reserved device name. Deterministic: the same human
    name always yields the same slug, which is what lets delete, restore, status
    and the memory pointer agree on what they are talking about.
    """
    s = (name or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    s = s[:MAX_SLUG_LEN].strip("-") or "untitled"
    if s in WIN_RESERVED:
        s = f"project-{s}"
    return s


def is_canonical_slug(value: str) -> bool:
    """True iff `value` is already EXACTLY what `canonical_project_slug` produces.

    Stated as a fixed point rather than re-derived from a regex. The first version
    checked `[a-z0-9-]`, length and the reserved set separately, and so accepted
    `foo--bar`, `-foo` and `foo-` — all of which the canonicaliser would normalise.
    A helper whose docstring and behaviour disagree is worse than no helper.
    """
    v = (value or "")
    return bool(v) and canonical_project_slug(v) == v


def safe_live_component(value: str) -> str:
    """Make an ALREADY-STORED/LISTED project identity safe, WITHOUT canonicalising.

    This is the other half of the contract, and conflating the two caused three
    separate defects: a legacy directory `My_Old.Project` was silently renamed to
    `my-old-project` on restore, a stored `last_active` pointing at it was declared
    stale, and a status read went looking in the wrong place.

    A NEW human name gets `canonical_project_slug()`. An identity Nova ALREADY
    stored or listed is resolved to itself — only made path-safe. A canonical slug
    passes through unchanged, so this is safe to use everywhere an existing
    identity is resolved.
    """
    v = (value or "").strip().replace("/", "-").replace("\\", "-")
    v = re.sub(r"[^A-Za-z0-9._-]+", "-", v)
    v = v.strip("-.")
    if not v or v in {".", ".."}:
        raise ValueError("project identity is empty after sanitization")
    if v.lower() in WIN_RESERVED:
        v = f"project-{v.lower()}"
    return v[:MAX_SLUG_LEN * 2]


def safe_trash_entry(entry: str) -> str:
    """Sanitise a trash entry id WITHOUT imposing the live-project contract.

    A trash id carries a timestamp (`slug--20260818-062122`), so it legitimately
    contains a longer body than a live slug allows. This only strips what could
    escape the trash directory.
    """
    e = (entry or "").strip()
    e = re.sub(r"[^A-Za-z0-9._-]+", "-", e)
    e = e.strip("-.")
    if not e:
        raise ValueError("Trash entry is empty after sanitization")
    return e
