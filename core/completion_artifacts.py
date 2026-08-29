"""Which files ARE the implementation (Stage 14).

Completion evidence is fenced to the implementation it examined, which means
something has to say precisely what "the implementation" is. Getting this set
wrong is not a detail — it breaks the fence in one of two directions:

TOO WIDE is the subtle one, and it is self-defeating. If the digest covers
files that Nova writes *while recording a verdict* — PROJECT.md, the status
line, a generated test file, an evidence log — then the act of writing down
"this criterion passed" changes the artifact the pass was about, and the
evidence invalidates itself the instant it is stored. Every check would come
back stale, for ever, and completion would be unreachable rather than merely
hard.

TOO NARROW is the obvious one. If the digest covers only `main.py`, editing a
module it imports changes behaviour without disturbing the fence, and stale
evidence keeps a changed project COMPLETE.

So the set is defined positively and the exclusions are named, each with the
reason it is excluded.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

#: DERIVED METADATA — written by the act of recording completion. Including any
#: of these would make a verdict invalidate its own evidence.
_DERIVED_NAMES = {
    "PROJECT.md",       # status, summary and progress log all live here
}
_DERIVED_DIRS = {
    ".nova",            # any Nova-owned metadata directory
}

#: VALIDATION SCAFFOLDING — written by the checks themselves, not by the
#: implementation. `_generate_and_run_tests` writes `test_<module>.py` during
#: verification and the repro check writes `nova_check.py`; if those counted,
#: running a check would change the digest the check is being recorded against.
def _is_validation_scaffold(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return (name.startswith("test_")
            or name == "tests.py"
            or name == "nova_check.py")


#: NOISE — never part of the implementation and often machine-specific.
_NOISE_DIRS = {"__pycache__", ".git", ".pytest_cache", ".mypy_cache",
               ".venv", "venv", "node_modules", ".trash"}
_NOISE_SUFFIXES = {".pyc", ".pyo", ".log", ".tmp"}


def implementation_files(project_path: Path) -> list[str]:
    """The project's implementation files, as sorted relative POSIX paths.

    Everything under the project that is not derived metadata, validation
    scaffolding or noise. Sorted so the result does not depend on the order the
    filesystem happens to hand them back.
    """
    root = Path(project_path)
    if not root.is_dir():
        return []
    out: list[str] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        try:
            rel = p.relative_to(root).as_posix()
        except ValueError:
            continue
        parts = rel.split("/")
        if any(part in _NOISE_DIRS or part in _DERIVED_DIRS for part in parts[:-1]):
            continue
        if parts[-1] in _DERIVED_NAMES:
            continue
        if p.suffix.lower() in _NOISE_SUFFIXES:
            continue
        if _is_validation_scaffold(rel):
            continue
        out.append(rel)
    return sorted(out)


def implementation_digest(project_path: Path) -> str:
    """A digest of the implementation, stable across unrelated churn.

    Covers each included file's relative path and its bytes, in sorted path
    order, so a rename counts as a change and a reordering of the directory
    listing does not. Returns "" for a project with no implementation at all,
    which the evaluator reads as "nothing to be complete about yet" rather than
    as a digest that happens to match nothing.
    """
    files = implementation_files(project_path)
    if not files:
        return ""
    h = hashlib.sha256()
    root = Path(project_path)
    for rel in files:
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        try:
            h.update((root / rel).read_bytes())
        except OSError:
            # An unreadable file is a real difference from a readable one, and
            # silently skipping it would let a permissions change slip the
            # fence. Fold the error in instead.
            h.update(b"<unreadable>")
        h.update(b"\n")
    return h.hexdigest()


def has_implementation(project_path: Path) -> bool:
    """Whether anything has been implemented at all."""
    return bool(implementation_files(project_path))
