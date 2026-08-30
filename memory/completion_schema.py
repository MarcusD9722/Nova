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
        -- 'auto' when Nova decomposed the request and sealed it herself, or
        -- 'human' when a person confirmed the contract IS the request. Both
        -- are sealed; they are not equally strong provenance, and a report
        -- that hid the difference would be overclaiming. Auto-sealing proves
        -- the request is COVERED, never that each criterion means what the
        -- clause it quotes means.
        seal_mode    TEXT NOT NULL DEFAULT '',
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
        -- The human decision this evidence came from, for a waiver. A waived
        -- row without one is not honoured: it is an acceptance nobody asked
        -- for.
        decision_id     TEXT,
        created_at      TEXT NOT NULL
    );
    """,
    # A question Nova asked a person, and the answer if one came back.
    #
    # WHY THIS EXISTS. `record_human_decision(actor="Marcus")` was attribution,
    # not proof: any code could type the name. A waiver now requires a pending
    # row that Nova ASKED FOR first, and each row can be redeemed once. That
    # does not make a caller inside the process honest — nothing in-process can
    # — but it makes two specific forgeries impossible rather than merely
    # discouraged: a waiver nobody was ever asked for, and a waiver replayed
    # from an answer already given. What remains is auditable: every acceptance
    # points at the question that produced it, its channel, and its moment.
    """
    CREATE TABLE IF NOT EXISTS human_decisions (
        decision_id  TEXT PRIMARY KEY,
        project_name TEXT NOT NULL,
        criterion_id TEXT NOT NULL,
        revision     INTEGER NOT NULL,
        prompt       TEXT NOT NULL DEFAULT '',
        -- What the person was looking at when they were ASKED. The answer is
        -- about this, not about whatever the project became while they were
        -- deciding: a judgement of the old screen cannot certify the new one.
        artifact_digest TEXT NOT NULL DEFAULT '',
        requested_at TEXT NOT NULL,
        resolved_at  TEXT,
        accepted     INTEGER,
        actor        TEXT NOT NULL DEFAULT '',
        -- How the answer arrived. Only channels the API layer supplies for a
        -- real user interaction are honoured as acceptance.
        channel      TEXT NOT NULL DEFAULT ''
    );
    """,
    # The last state ANNOUNCED for a (project, revision).
    #
    # Measured: with this ledger in process memory only, a fresh runtime on the
    # same durable root re-announced project.completed for a revision that had
    # completed long ago and had not changed - once per restart, for ever,
    # because `previous` was always "" in a new process. Exactly-once has to be
    # scoped to the TRANSITION, and a transition outlives the process that saw
    # it, so the ledger has to as well.
    """
    CREATE TABLE IF NOT EXISTS completion_announcements (
        project_name TEXT NOT NULL,
        revision     INTEGER NOT NULL,
        state        TEXT NOT NULL,
        announced_at TEXT NOT NULL,
        PRIMARY KEY (project_name, revision)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_human_decisions_open "
    "ON human_decisions(project_name, criterion_id, resolved_at);",
    "CREATE INDEX IF NOT EXISTS idx_criteria_project "
    "ON acceptance_criteria(project_name, revision, superseded_at);",
    "CREATE INDEX IF NOT EXISTS idx_evidence_criterion "
    "ON acceptance_evidence(criterion_id, created_at);",
    "CREATE INDEX IF NOT EXISTS idx_evidence_project "
    "ON acceptance_evidence(project_name, revision, created_at);",
)
