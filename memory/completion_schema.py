"""Durable acceptance criteria and evidence (Stage 14 §4).

Referenced by BOTH the create block in `SQLiteMemoryBackend.initialize` and the
versioned migration, so a fresh database and an upgraded one cannot drift apart
— the same mistake `EPISODIC_DDL` exists to prevent, and the same fix.

WHY THESE ARE TABLES AND NOT A JSON BLOB IN PROJECT.md

A criterion has to survive a replan that forgot to mention it. If the
acceptance contract lives in a document that a model rewrites, then the model
can silently drop a requirement and nothing will ever notice, because the only
record of it was the thing that was overwritten. A criterion is superseded by
an explicit, attributable act at a named revision, or it stands.

Evidence is separate from criteria for the same reason: a verdict is an
observation with a time, a revision and an artifact behind it, and several of
them can accumulate against one criterion. Collapsing them into a single
"status" column would throw away exactly the history that makes a stale pass
recognisable as stale.
"""

from __future__ import annotations

COMPLETION_DDL: tuple[str, ...] = (
    # The user's own durable words, one row per revision. The text is never
    # regenerated from the finished code: an acceptance contract derived from
    # the artifact can only conclude that the artifact does what it does.
    """
    CREATE TABLE IF NOT EXISTS project_requirements (
        project_name TEXT NOT NULL,
        revision     INTEGER NOT NULL,
        request_text TEXT NOT NULL,
        source       TEXT NOT NULL,
        note         TEXT NOT NULL DEFAULT '',
        created_at   TEXT NOT NULL,
        -- When the acceptance contract for this revision was agreed to be the
        -- WHOLE of what was asked. Until then the criteria recorded are a
        -- draft: they may each be sound and still, together, miss half the
        -- request, and completing on a draft is how a forgotten requirement
        -- becomes a finished project.
        sealed_at    TEXT,
        PRIMARY KEY (project_name, revision)
    );
    """,
    # One acceptance criterion. `origin_quote` is the span of the request it was
    # derived from — a criterion that cannot point at the user's words is an
    # opinion, and this column is what makes that checkable rather than a rule
    # somebody remembers.
    """
    CREATE TABLE IF NOT EXISTS acceptance_criteria (
        criterion_id           TEXT PRIMARY KEY,
        project_name           TEXT NOT NULL,
        revision               INTEGER NOT NULL,
        text                   TEXT NOT NULL,
        origin_quote           TEXT NOT NULL,
        source                 TEXT NOT NULL,
        required               INTEGER NOT NULL DEFAULT 1,
        verify_kind            TEXT NOT NULL DEFAULT 'machine',
        created_at             TEXT NOT NULL,
        superseded_at          TEXT,
        superseded_by_revision INTEGER,
        supersede_reason       TEXT NOT NULL DEFAULT '',
        -- The criterion at the previous revision this one continues, when a
        -- correction carried an unchanged requirement forward. Without it a
        -- carried criterion looks brand new, and Nova cannot tell the user
        -- "you already saw this pass; the requirement moved, so it needs
        -- checking again" - which is a strictly worse answer than the truth.
        carried_from           TEXT
    );
    """,
    # One observation about one criterion. Fenced by the requirement revision it
    # was gathered under and the implementation digest it examined; both are
    # required, because evidence that cannot say what it looked at cannot be
    # ruled stale later.
    """
    CREATE TABLE IF NOT EXISTS acceptance_evidence (
        evidence_id     TEXT PRIMARY KEY,
        criterion_id    TEXT NOT NULL,
        project_name    TEXT NOT NULL,
        revision        INTEGER NOT NULL,
        artifact_digest TEXT NOT NULL,
        verdict         TEXT NOT NULL,
        detail          TEXT NOT NULL DEFAULT '',
        error           TEXT NOT NULL DEFAULT '',
        task_id         TEXT,
        generation      INTEGER,
        attempt         INTEGER,
        created_at      TEXT NOT NULL
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_criteria_project "
    "ON acceptance_criteria(project_name, revision, superseded_at);",
    "CREATE INDEX IF NOT EXISTS idx_evidence_criterion "
    "ON acceptance_evidence(criterion_id, created_at);",
    "CREATE INDEX IF NOT EXISTS idx_evidence_project "
    "ON acceptance_evidence(project_name, revision, created_at);",
)
