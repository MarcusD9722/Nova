from __future__ import annotations

"""Autonomous skill learning (Goal #2, Phase 8).

When Marcus repeats the same multi-step sequence several times, Nova should
notice and OFFER to learn it — never silently automate it. This module owns the
deterministic detection (a recurring, non-overlapping subsequence in an activity
log) and the shape of a learned, parameterized, versioned, branchable workflow.
The store lives in MemoryUnifier; execution of a learned skill routes every step
back through the PermissionBroker, so a "learned" workflow is still gated.

Detection is pure and testable. Learning is always a proposal the human accepts.
"""

import re
from typing import Any


def _nonoverlap_count(seq: list[str], gram: tuple[str, ...]) -> int:
    L = len(gram)
    i = c = 0
    while i <= len(seq) - L:
        if tuple(seq[i:i + L]) == gram:
            c += 1
            i += L          # non-overlapping: skip past this match
        else:
            i += 1
    return c


def detect_repeated_workflow(
    activity: list[str], *, min_repeats: int = 3, min_len: int = 2, max_len: int = 6
) -> dict[str, Any] | None:
    """Find the LONGEST contiguous step-sequence that recurs at least
    `min_repeats` times (non-overlapping) in the activity log. Returns
    {steps, occurrences} or None. Longest-first so it captures the fullest
    workflow, not just a common pair inside it."""
    seq = [str(a).strip() for a in activity if str(a).strip()]
    n = len(seq)
    if n < min_len * min_repeats:
        return None
    upper = min(max_len, n // min_repeats)
    for L in range(upper, min_len - 1, -1):
        best: dict[str, Any] | None = None
        seen: set[tuple[str, ...]] = set()
        for i in range(n - L + 1):
            gram = tuple(seq[i:i + L])
            if gram in seen:
                continue
            seen.add(gram)
            occ = _nonoverlap_count(seq, gram)
            if occ >= min_repeats and (best is None or occ > best["occurrences"]):
                best = {"steps": list(gram), "occurrences": occ}
        if best is not None:
            return best  # longest length with a qualifying pattern wins
    return None


_VAR_RE = re.compile(r"\{([a-zA-Z_][\w]*)\}")


def workflow_parameters(steps: list[str]) -> list[str]:
    """The {placeholder} variables referenced by a workflow's steps."""
    out: list[str] = []
    for s in steps:
        for m in _VAR_RE.finditer(str(s)):
            if m.group(1) not in out:
                out.append(m.group(1))
    return out


def render_steps(steps: list[str], params: dict[str, str]) -> list[str]:
    """Substitute parameters into a workflow's steps (missing vars left as-is)."""
    def sub(s: str) -> str:
        return _VAR_RE.sub(lambda m: str(params.get(m.group(1), m.group(0))), str(s))
    return [sub(s) for s in steps]
