from __future__ import annotations

from pathlib import Path


class PathSafetyError(RuntimeError):
    pass


def ensure_within_repo(repo_root: Path, target: Path) -> Path:
    repo_root = repo_root.resolve()
    target = target.resolve()

    try:
        target.relative_to(repo_root)
    except Exception as e:  # noqa: BLE001
        raise PathSafetyError(f"Refusing to access path outside repo: {target}") from e

    return target


def ensure_within_any_root(allowed_roots: list[Path], target: Path) -> Path:
    """Ensure target is inside at least one allowed root.

    This is used to safely allow writes to multiple sandboxed directories
    (e.g. repo root and projects dir).
    """
    target = target.resolve()
    roots = [r.resolve() for r in (allowed_roots or []) if r is not None]
    for r in roots:
        try:
            target.relative_to(r)
            return target
        except Exception:
            continue
    raise PathSafetyError(f"Refusing to access path outside allowed roots: {target}")


def ensure_safe_subdir(repo_root: Path, subdir: Path, target: Path) -> Path:
    subdir = ensure_within_repo(repo_root, subdir)
    target = ensure_within_repo(repo_root, target)
    try:
        target.relative_to(subdir)
    except Exception as e:  # noqa: BLE001
        raise PathSafetyError(f"Refusing to access path outside allowed dir {subdir}: {target}") from e
    return target
