from __future__ import annotations

"""Memory provenance vocabulary (Goal #19, Phase 3.5).

Every stored fact can carry WHERE it came from and HOW trustworthy it is, so
Nova never presents an assumption as a confirmed fact. This module owns the
small fixed vocabulary and the conservative default classification. The storage
columns live on the `facts` table (schema v3); the plumbing lives in
memory/unifier.py + memory/backends/sqlite_backend.py.

Design note: the `facts` table already carries `confidence` (Bayesian-ish trust)
and `last_reinforced_at` (decay/reinforcement). Provenance is the orthogonal
"where did this come from and have we re-checked it" axis. "Related memories"
(the seventh item in the roadmap goal) is already served by the knowledge-graph
`edges` table, so it is deliberately not duplicated here.
"""

# ── Verification status: how a fact came to be believed (loosely trust-ordered) ──
STATED = "stated"              # the user directly asserted it
OBSERVED = "observed"          # read from a tool/sensor (calendar, weather, file, clock)
EXTERNAL = "external"          # from the web / a third party (evidence should cite it)
CONFIRMED = "confirmed"        # re-verified against a fresh observation
INFERRED = "inferred"          # Nova's own inference — an ASSUMPTION, not a fact
CONTRADICTED = "contradicted"  # later found to conflict with newer evidence
UNVERIFIED = "unverified"      # unknown / legacy — provenance not recorded

STATUSES = frozenset(
    {STATED, OBSERVED, EXTERNAL, CONFIRMED, INFERRED, CONTRADICTED, UNVERIFIED}
)

# Statuses that mean "this is an assumption — hedge accordingly." This is the
# crux of the goal's rule: never confuse assumptions with facts.
_ASSUMPTION = frozenset({INFERRED, CONTRADICTED, UNVERIFIED})

# Statuses that represent a direct observation at write time, so last_confirmed_at
# starts equal to created_at — we had evidence in hand when we wrote it. Inferred/
# unverified facts have NEVER been confirmed, so their last_confirmed_at stays NULL.
_OBSERVED_AT_WRITE = frozenset({STATED, OBSERVED, EXTERNAL, CONFIRMED})


def normalize_status(status: str | None) -> str:
    s = (status or "").strip().lower()
    return s if s in STATUSES else UNVERIFIED


def is_assumption(status: str | None) -> bool:
    """True when a fact should be presented tentatively rather than as settled."""
    return normalize_status(status) in _ASSUMPTION


def observed_at_write(status: str | None) -> bool:
    return normalize_status(status) in _OBSERVED_AT_WRITE


def classify_default(entity: str, attribute: str) -> tuple[str, str]:
    """Conservative (source, verification_status) when a caller doesn't specify.

    This encodes what is genuinely known about each internal write path — it is
    NOT a guess about whether the fact is true, only about where the write comes
    from. A lesson really is distilled by reflection; a mood reading really is
    inferred from behavior; a clock/bookkeeping timestamp really is observed.
    """
    ent = (entity or "").strip().lower()
    if ent == "lesson":
        return ("reflection", INFERRED)          # distilled by Nova from turns
    if ent in {"mood", "wellbeing", "interest_focus"}:
        return ("inference", INFERRED)           # Nova's read of behavioral signals
    if ent == "self_eval":
        return ("metrics", OBSERVED)             # measured, not guessed
    if ent == "session":
        return ("system", OBSERVED)              # bookkeeping timestamps
    if ent == "note":
        return ("conversation", UNVERIFIED)
    if ent == "user":
        return ("user", STATED)                  # identity/relationships Marcus told Nova
    if ent == "projects" or ent.startswith("project:"):
        return ("system", OBSERVED)              # project state Nova tracks
    if ent.startswith("conversation:"):
        return ("summary", INFERRED)             # rolling summaries are synthesized
    return ("conversation", UNVERIFIED)


def reverification_due(
    verification_status: str | None,
    last_confirmed_at: str | None,
    created_at: str | None,
    *,
    now_iso: str,
    max_age_days: float = 180.0,
) -> bool:
    """Should this fact be re-checked? True for a directly-observed fact whose
    last confirmation is older than `max_age_days`. Assumptions (never confirmed)
    and freshly-confirmed facts both return False here — an assumption isn't
    "due for re-verification," it's simply flagged as an assumption already.
    """
    from datetime import datetime, timezone

    if is_assumption(verification_status):
        return False
    anchor = last_confirmed_at or created_at
    if not anchor:
        return False

    def _p(s: str) -> "datetime | None":
        try:
            dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None

    a, n = _p(anchor), _p(now_iso)
    if a is None or n is None:
        return False
    return (n - a).total_seconds() / 86400.0 > max_age_days
