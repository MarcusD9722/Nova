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
    "MAX_COMPONENT_LEN",
    "MAX_SLUG_LEN",
    "WIN_RESERVED",
    "canonical_project_slug",
    "is_canonical_slug",
    "resolve_existing_identity",
    "safe_live_component",
    "safe_trash_entry",
]

#: The longest single path component every filesystem Nova runs on accepts. NTFS
#: and ext4 both stop at 255. This bounds an EXISTING identity, which is a very
#: different job from `MAX_SLUG_LEN` bounding a NEW one: 48 keeps new directory
#: names tidy, 255 is the point past which the name cannot exist at all.
MAX_COMPONENT_LEN = 255

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
    # NEVER truncate. This helper existed to preserve legacy identities, and the
    # old ProjectManager had no length cap at all, so a directory longer than any
    # bound we invent here can genuinely exist on disk. Silently shortening it
    # made Nova check, read, delete and status a DIFFERENT path — and if a
    # shorter sibling happened to exist, the wrong project.
    #
    # The bound below is a real platform limit, not a style choice, and exceeding
    # it FAILS rather than quietly producing a different identity.
    if len(v) > MAX_COMPONENT_LEN:
        raise ValueError(
            f"project identity is {len(v)} characters, beyond the "
            f"{MAX_COMPONENT_LEN}-character filesystem component limit; it cannot "
            f"be shortened without naming a different project")
    return v


def resolve_existing_identity(projects_dir: Path, candidate: str) -> str | None:
    """The ACTUAL on-disk identity for `candidate`, or None if no live project.

    Windows matches paths case-insensitively, so `(projects_dir / "my_old.project")
    .is_dir()` is True when the real directory is `My_Old.Project`. Returning the
    CALLER's spelling after such a match hands back an identity that does not exist
    as written, and every later exact comparison then fails —
    `active_projects()` membership (which is what stops a delete racing a build)
    and the `last_active` pointer comparison among them.

    `Path.resolve()` happens to repair this on Windows today, but that is a
    platform side effect, not a stated contract, and it behaves differently on a
    case-sensitive filesystem. So the identity is recovered EXPLICITLY here, by
    looking at what is actually on disk.

    Exact match wins. A unique case-insensitive match resolves to the real entry.
    An AMBIGUOUS case-insensitive match — possible only on a case-sensitive
    filesystem, where several siblings differ solely by case — returns None rather
    than picking one, because guessing between real distinct projects is worse
    than declining.
    """
    try:
        entries = [p.name for p in projects_dir.iterdir() if p.is_dir()]
    except (OSError, FileNotFoundError):
        return None

    if candidate in entries:
        return candidate
    lowered = candidate.lower()
    hits = [n for n in entries if n.lower() == lowered]
    if len(hits) == 1:
        return hits[0]
    return None


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
