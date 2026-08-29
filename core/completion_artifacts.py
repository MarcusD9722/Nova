"""Which files ARE the implementation (Stage 14).

Completion evidence is fenced to the implementation it examined, which means
something has to say precisely what "the implementation" is. Getting this set
wrong breaks the fence in one of two directions.

TOO WIDE is the subtle one, and it is self-defeating. If the digest covers
files that Nova writes *while recording a verdict* — PROJECT.md, a generated
test, an evidence log — then storing "this criterion passed" changes the
artifact the pass was about, and the evidence invalidates itself the instant it
is written. Every check would come back stale for ever.

TOO NARROW is the obvious one. If the digest misses a file the program
imports, editing it changes behaviour without disturbing the fence, and stale
evidence keeps a changed project COMPLETE.

SCAFFOLDING IS KNOWN BY PROVENANCE, NOT BY ITS NAME.

The first version of this excluded anything called `test_*.py`, `tests.py` or
`nova_check.py`. That is a guess about who wrote a file, and it is wrong
exactly when it matters: a user asking Nova to build a test runner owns
`test_engine.py` and `tests.py`, and those files were then invisible to the
fence — they could be rewritten completely without invalidating a single piece
of evidence. Nova now DECLARES the files she writes as scaffolding, in
`.nova/scaffold.json`, and only declared files are excluded. Anything
undeclared is the user's, which is the safe direction to be wrong in.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

#: Nova's own metadata directory. Everything under it is derived: the scaffold
#: manifest, evidence caches, anything a later stage adds.
NOVA_DIR = ".nova"
SCAFFOLD_MANIFEST = f"{NOVA_DIR}/scaffold.json"

#: DERIVED METADATA — written by the act of recording completion. Including any
#: of these would make a verdict invalidate its own evidence.
_DERIVED_NAMES = {"PROJECT.md"}

#: NOISE — never part of an implementation, often machine-specific.
_NOISE_DIRS = {"__pycache__", ".git", ".pytest_cache", ".mypy_cache",
               ".venv", "venv", "node_modules", ".trash"}
_NOISE_SUFFIXES = {".pyc", ".pyo", ".log", ".tmp"}


def declared_scaffold(project_path: Path) -> set[str]:
    """Relative paths Nova has declared as her own validation scaffolding."""
    manifest = Path(project_path) / SCAFFOLD_MANIFEST
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if isinstance(raw, dict):
        raw = raw.get("paths", [])
    if not isinstance(raw, list):
        return set()
    return {str(p).replace("\\", "/").lstrip("/") for p in raw if str(p).strip()}


def declare_scaffold(project_path: Path, paths: list[str]) -> set[str]:
    """Record that Nova wrote these files as validation scaffolding.

    Additive: a later check declaring one more file does not un-declare the
    files an earlier one wrote. Returns the full declared set.
    """
    root = Path(project_path)
    current = declared_scaffold(root)
    current |= {str(p).replace("\\", "/").lstrip("/") for p in paths if str(p).strip()}
    manifest = root / SCAFFOLD_MANIFEST
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"paths": sorted(current)}, indent=2),
                        encoding="utf-8")
    return current


def implementation_files(project_path: Path) -> list[str]:
    """The project's implementation files, as sorted relative POSIX paths.

    Everything under the project that is not derived metadata, DECLARED
    scaffolding, or noise. Sorted, so the result does not depend on the order
    the filesystem happens to return entries in.
    """
    root = Path(project_path)
    if not root.is_dir():
        return []
    scaffold = declared_scaffold(root)
    out: list[str] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        try:
            rel = p.relative_to(root).as_posix()
        except ValueError:
            continue
        parts = rel.split("/")
        if parts[0] == NOVA_DIR:
            continue
        if any(part in _NOISE_DIRS for part in parts[:-1]):
            continue
        if parts[-1] in _DERIVED_NAMES:
            continue
        if p.suffix.lower() in _NOISE_SUFFIXES:
            continue
        if rel in scaffold:
            continue
        out.append(rel)
    return sorted(out)


def implementation_digest(project_path: Path) -> str:
    """A digest of the implementation, stable across unrelated churn.

    Covers each included file's relative path and its bytes in sorted path
    order, so a rename counts as a change and a reordering of the directory
    listing does not.

    Returns "" only when there is no implementation at all. That empty value is
    NOT a wildcard — `core.completion` compares digests for exact equality, so
    evidence recorded against nothing cannot certify something that arrives
    later.
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
            # An unreadable file differs from a readable one, and skipping it
            # would let a permissions change slip the fence.
            h.update(b"<unreadable>")
        h.update(b"\n")
    return h.hexdigest()


def has_implementation(project_path: Path) -> bool:
    return bool(implementation_files(project_path))
